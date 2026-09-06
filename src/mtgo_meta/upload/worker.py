"""Background uploader thread.

Started from ``serve.py`` after consent has been recorded. Wakes up
every ``upload_interval_seconds`` (default 5 minutes), looks at the
local SQLite for matches newer than the last successful upload, and
streams them to ``metahunter-api.fly.dev/v1/matches`` in batches.

Idempotent on the server side via the (match_id, install_id)
UNIQUE constraint, so a worker restart can safely re-process recent
matches without inflating counts.

Crash-safe: progress is tracked by ``last_uploaded_log_mtime`` in
the SQLite ``meta`` table, updated after each batch lands
successfully. A crash mid-batch only re-sends ≤ N matches on
restart, never loses anything.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from metahunter_core.deck_files import maindeck_signature
from metahunter_core.classifier import (
    build_card_colors, build_card_weights, classify_by_similarity,
    recompute_deck_color_identity,
)
from mtgo_meta.paths import corpus_path, default_db_path
from mtgo_meta.store import open_store

from . import consent as consent_mod
from .client import CLIENT_VERSION, UploadClient
from .state import hash_username, load_or_create_state

log = logging.getLogger(__name__)

META_KEY_LAST_UPLOADED = "last_uploaded_log_mtime"
DEFAULT_INTERVAL_SECONDS = 5 * 60
BATCH_SIZE = 100


def _signature_for(registered, match_id):
    """Maindeck fingerprint for a match, or None when unknown."""
    if not registered:
        return None
    row = registered.get(match_id)
    if not row or not row.get("cards"):
        return None
    try:
        return maindeck_signature(row["cards"])[:128]
    except Exception:  # noqa: BLE001 - a malformed row is not fatal
        return None


class Uploader:
    """Owns the worker thread + the classifier corpus.

    Single-instance per process. Spun up from serve.py via ``start()``
    and stopped on shutdown via ``stop()``.
    """

    def __init__(
        self,
        *,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        batch_size: int = BATCH_SIZE,
        server_url: str | None = None,
    ) -> None:
        self.interval_seconds = interval_seconds
        self.batch_size = batch_size
        self._client = UploadClient(server_url)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        # Lazy-loaded corpus — built on the first sweep so app startup
        # isn't blocked.
        self._corpus_loaded = False
        self._decks: list[dict] = []
        self._weights: dict = {}
        self._card_colors: dict = {}

    # ---- corpus --------------------------------------------------------

    def _ensure_corpus(self, fmt: str = "Legacy") -> bool:
        if self._corpus_loaded:
            return True
        cp = corpus_path(fmt)
        if not cp.exists():
            log.warning("uploader: no corpus at %s; cannot classify", cp)
            return False
        corpus = json.loads(cp.read_text(encoding="utf-8"))
        self._decks = corpus.get("decks", [])
        for d in self._decks:
            d["color_identity"] = recompute_deck_color_identity(d)
        self._weights = build_card_weights(self._decks)
        self._card_colors = build_card_colors(self._decks)
        self._corpus_loaded = True
        log.info("uploader: corpus loaded (%d decks)", len(self._decks))
        return True

    # ---- one sweep -----------------------------------------------------

    def sweep(self) -> dict[str, int]:
        """One pass through new local matches. Returns a small
        diagnostic dict so the caller (or tests) can assert."""
        c = consent_mod.load()
        if not c.has_consented:
            return {"skipped": -1, "reason": "no consent"}

        install_id, install_salt = load_or_create_state()
        if not self._ensure_corpus():
            return {"skipped": -1, "reason": "no corpus"}

        with open_store(default_db_path()) as store:
            since_str = store.get_meta(META_KEY_LAST_UPLOADED, "0")
            since = float(since_str or 0)
            rows = store.iter_matches(since_mtime=since)
            parser_version_str = store.get_meta("parser_version", "0")
            try:
                parser_version = int(parser_version_str)
            except ValueError:
                parser_version = 0

            # Matches we now know the registered deck for, even if they
            # were uploaded before we knew it. The watermark only moves
            # forward, so without this an older match could never gain
            # its attribution — the server can only fill that gap from
            # an upload that carries the signature. The set is small
            # (MTGO's log reaches back days, not months) and re-sending
            # is idempotent, so this is cheap insurance.
            known = set(store.registered_by_match())
            if known:
                seen = {m.match_id for m in rows}
                rows = list(rows) + [
                    m for m in store.iter_matches()
                    if m.match_id in known and m.match_id not in seen
                ]

        if not rows:
            return {"new": 0}

        # Identify the user — most frequent username across the full DB.
        # (We compute on the FULL DB, not just the new batch, so the
        # answer stays stable when the user has 3 matches.)
        with open_store(default_db_path()) as store:
            all_counter: Counter[str] = Counter()
            for m in store.iter_matches():
                for p in m.players:
                    all_counter[p] += 1
        if not all_counter:
            return {"skipped": -1, "reason": "no players"}
        user = all_counter.most_common(1)[0][0]

        # Which decklist MTGO recorded as registered, per match. Only
        # covers matches the rolling text log still reaches, which is a
        # small and recent slice — everything else uploads without a
        # signature and simply is not attributed to a decklist.
        try:
            with open_store(default_db_path()) as store:
                registered = store.registered_by_match()
        except Exception:  # noqa: BLE001 - attribution is a bonus, not a gate
            registered = {}

        # Build payloads.
        batch: list[dict] = []
        sent = new = merged = errors = 0
        max_seen_mtime = since

        def flush() -> None:
            nonlocal sent, new, merged, errors
            if not batch:
                return
            status, resp = self._client.upload_batch(install_id, batch)
            if status != 200 or not hasattr(resp, "received"):
                errors += len(batch)
                log.warning("uploader: batch failed (%s)", resp)
            else:
                sent += resp.received
                new += resp.new_matches
                merged += resp.merged_matches
                errors += len(resp.errors)
            batch.clear()

        for m in rows:
            payload = self._build_payload(
                m, user, install_salt, parser_version, registered,
            )
            if payload is None:
                continue
            batch.append(payload)
            max_seen_mtime = max(max_seen_mtime, m.log_mtime or 0)
            if len(batch) >= self.batch_size:
                flush()
        flush()

        if sent > 0 and errors == 0:
            with open_store(default_db_path()) as store:
                # max(), because the batch may now include older matches
                # re-sent for attribution; letting one of those set the
                # watermark would re-upload everything after it forever.
                store.set_meta(
                    META_KEY_LAST_UPLOADED, str(max(max_seen_mtime, since))
                )
        return {"sent": sent, "new": new, "merged": merged, "errors": errors}

    def _build_payload(self, m, user, install_salt, parser_version,
                       registered=None):
        if user not in m.players:
            return None
        opp = next((p for p in m.players if p != user), None)
        if opp is None or not m.match_winner:
            return None

        you_cards = m.cards_by_player.get(user, [])
        opp_cards = m.cards_by_player.get(opp, [])
        you_cast = m.cards_cast_by_player.get(user, [])
        opp_cast = m.cards_cast_by_player.get(opp, [])

        you_label, _, _ = classify_by_similarity(
            you_cards, self._decks, self._weights, self._card_colors,
            cast_cards=you_cast,
        )
        opp_label, _, _ = classify_by_similarity(
            opp_cards, self._decks, self._weights, self._card_colors,
            cast_cards=opp_cast,
        )
        you_won = m.match_winner == user
        return {
            "match_id": m.match_id,
            "format": m.format or "Legacy",
            "log_mtime": datetime.fromtimestamp(
                m.log_mtime or 0, timezone.utc).isoformat(),
            "turns": m.turns or 0,
            "you": {
                "username": user,
                # Present only when MTGO actually recorded which deck was
                # registered. Absent is the normal case and must stay
                # meaningful: the server treats it as "unknown", never as
                # "no deck".
                "deck_signature": _signature_for(registered, m.match_id),
                "deck": you_label, "deck_colors": "",
                "on_play": (m.first_player == user) if m.first_player else None,
                "won": you_won,
                "games_won": sum(1 for g in m.games if g.get("winner") == user),
                "cards_observed": you_cards,
                "cards_cast": you_cast,
            },
            "opponent": {
                "username_hash": hash_username(install_salt, opp),
                "deck": opp_label, "deck_colors": "",
                "on_play": (m.first_player == opp) if m.first_player else None,
                "won": not you_won,
                "games_won": sum(1 for g in m.games if g.get("winner") == opp),
                "cards_observed": opp_cards,
                "cards_cast": opp_cast,
            },
            "client_version": CLIENT_VERSION,
            "parser_version": parser_version,
        }

    # ---- thread plumbing ----------------------------------------------

    def _run(self) -> None:
        log.info("uploader: thread started (interval %ds)", self.interval_seconds)
        while not self._stop_event.is_set():
            try:
                result = self.sweep()
                if result.get("new", 0) > 0:
                    log.info("uploader: pushed %d new matches", result["new"])
            except Exception:  # noqa: BLE001
                log.exception("uploader: unexpected error in sweep")
            self._stop_event.wait(self.interval_seconds)
        log.info("uploader: thread exiting")

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="metahunter-uploader", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout)
