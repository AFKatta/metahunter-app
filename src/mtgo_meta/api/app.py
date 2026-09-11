"""FastAPI backend for Metahunter.

Serves a JSON API for the React frontend at /api/*. Reads parsed match
data from the local SQLite store written by scripts/ingest.py (and
kept current by scripts/watch.py). Archetype classification runs on
the fly per request and is cached per process via a small LRU.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from metahunter_core.card_index import CardIndex
from metahunter_core.card_index import build_index as build_card_index
from metahunter_core.deck_files import (
    DeckCard,
    deck_signature,
    load_decks,
    maindeck_signature,
    mtgo_history_files,
)
from metahunter_core.event_kind import (
    CASUAL,
    LEAGUE,
    resolve as resolve_event_kind,
)
from metahunter_core.player_index import (
    MIN_CONSISTENCY,
    build_player_index,
    lookup_opponent_deck,
)
from metahunter_core.parser.game_log import OPENING_HAND_SIZE
from metahunter_core.classifier import (
    SKIP_LABEL,
    build_card_colors,
    build_card_weights,
    classify,
    classify_by_similarity,
    colour_prefix,
    colour_prefix_to_set,
    infer_color_identity,
    load_legacy,
    recompute_deck_color_identity,
)
from mtgo_meta.paths import (
    card_index_path,
    available_corpora,
    corpus_path,
    default_db_path,
    format_data_dir,
    web_dist_dir,
)
from mtgo_meta.store import open_store
from mtgo_meta import account, deck_history, net
from mtgo_meta.leagues import league_runs
from mtgo_meta.upload.client import server_url as upload_server_url

log = logging.getLogger(__name__)


def _local_port() -> int:
    """The port this server is actually listening on.

    Sign-in needs it: the browser is told to come back to
    127.0.0.1:<port>/auth/callback, and that has to be the real port,
    which is usually 8765 but steps aside when something else holds it.
    serve.py publishes it here once it has bound.
    """
    try:
        return int(os.environ.get("METAHUNTER_LOCAL_PORT") or 8765)
    except ValueError:
        return 8765


def _auth_result_page(ok: bool, message: str) -> str:
    """The page the browser shows after a sign-in attempt.

    Deliberately plain and self-contained — it is served from the
    desktop app's own local server, so it cannot reach out for fonts or
    stylesheets, and it is only on screen for a moment.
    """
    title = "You're signed in" if ok else "Sign-in failed"
    tone = "#22c55e" if ok else "#ef4444"
    body = (
        "Metahunter is ready. You can close this tab and go back to the app."
        if ok else message
    )
    who = f"<p class='who'>{message}</p>" if ok and message else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{title} — Metahunter</title>
<style>
 body {{ margin:0; min-height:100vh; display:grid; place-items:center;
        background:#0b0b0c; color:#e8e8ea;
        font:15px/1.6 system-ui,-apple-system,Segoe UI,sans-serif; }}
 .card {{ max-width:26rem; padding:2.5rem; text-align:center; }}
 .dot {{ width:.6rem; height:.6rem; border-radius:50%; background:{tone};
        display:inline-block; margin-right:.5rem; }}
 h1 {{ font-size:1.35rem; margin:0 0 .75rem; }}
 p {{ color:#a1a1aa; margin:0; }}
 .who {{ margin-top:.75rem; color:#e8e8ea; font-weight:600; }}
</style></head>
<body><div class="card">
 <h1><span class="dot"></span>{title}</h1>
 <p>{body}</p>{who}
</div></body></html>"""


FORMAT_DATA = format_data_dir()
DEFAULT_DB = default_db_path()
WEB_DIST = web_dist_dir()


SIGNATURE_NOISE = {
    "Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes",
    "Snow-Covered Plains", "Snow-Covered Island", "Snow-Covered Swamp",
    "Snow-Covered Mountain", "Snow-Covered Forest",
    "Orc Army Token", "Samurai Token", "Skeleton Token", "Clue Token",
    "Treasure Token", "Food Token", "Goblin Token", "Soldier Token",
    "Spirit Token", "Zombie Token", "Servo Token", "Thopter Token",
    "Dwarf Token", "Goat Token", "Beast Token", "Snake Token",
    "Insect Token", "Wolf Token", "Bird Token", "Cat Token",
    "Saproling Token", "Elemental Token", "Dragon Token",
    "Angel Token", "Knight Token", "Demon Token", "Frog Token",
    "Eldrazi Token", "Plant Token", "Construct Token",
    "Copy Token", "Emblem Token", "Monk Token",
    "The Initiative", "The Monarch", "Undercity",
}


def _signature(cards: list[str], n: int = 8) -> list[tuple[str, int]]:
    return Counter(c for c in cards if c not in SIGNATURE_NOISE).most_common(n)


def create_app(db_path: Path | None = None) -> FastAPI:
    db = db_path or DEFAULT_DB
    app = FastAPI(title="Metahunter", version="0.0.1")

    # Phase 2: surface the local /api/upload/* routes — consent
    # gate, leaderboard toggle, server-data wipe button.
    from mtgo_meta.api.upload_routes import router as upload_router
    app.include_router(upload_router)

    # Phase 6: in-app auto-updater. Routes are no-ops when no newer
    # release exists upstream — the background worker handles the
    # GitHub poll + download, the frontend banner consumes state.
    from mtgo_meta.api.updater_routes import router as updater_router
    app.include_router(updater_router)

    # Allow the Vite dev server (default 5173) to talk to us in dev.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://metahunter.localhost:5173",
        ],
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    # ---- shared lazy-loaded resources -----------------------------------
    archetypes, fallbacks = load_legacy(FORMAT_DATA)

    # Load every per-format corpus file we can find. The dict keys are
    # the format names as the API exposes them ("Legacy", "Vintage", …).
    # When a request comes in with ?format=X, we look up that corpus
    # for similarity classification; if the corpus is missing the
    # classifier falls back to a colour-code label.
    #
    # ``universal_card_colors`` is a merged colour-lookup table built
    # from all loaded corpora — used as a fallback so colour inference
    # still works for formats without their own corpus.
    corpora: dict[str, dict] = {}
    # Player name -> the decklists they have published, per format.
    player_indexes: dict[str, dict] = {}
    universal_card_colors: dict[str, str] = {}
    for fmt_name in available_corpora():
        cp = corpus_path(fmt_name)
        if not cp.exists():
            continue
        try:
            data = json.loads(cp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        decks = data.get("decks", [])
        if not decks:
            continue
        for d in decks:
            d["color_identity"] = recompute_deck_color_identity(d)
        weights = build_card_weights(decks)
        colors = build_card_colors(decks)
        corpora[fmt_name] = {"decks": decks, "weights": weights, "colors": colors}
        player_indexes[fmt_name] = build_player_index(decks)
        # Merge into universal table — first-seen wins. Legacy gets
        # priority simply because it's the largest dataset.
        for name, col in colors.items():
            if name not in universal_card_colors:
                universal_card_colors[name] = col

    # Legacy-shaped variables kept around so call sites that don't yet
    # care about format still work. They reference whichever corpus is
    # the default ("Legacy" if it loaded, else the first available).
    default_fmt = "Legacy" if "Legacy" in corpora else next(iter(corpora), "Legacy")
    corpus_decks = corpora.get(default_fmt, {}).get("decks", [])
    card_weights = corpora.get(default_fmt, {}).get("weights", {})
    card_colors = universal_card_colors or corpora.get(default_fmt, {}).get("colors", {})

    @lru_cache(maxsize=20000)
    def _classify_cached(
        cards_key: tuple[str, ...], cast_key: tuple[str, ...], fmt: str,
    ) -> str:
        """Classify a bag of cards into a specific archetype name.

        Format-aware: looks up ``fmt`` in the loaded corpora and uses
        that one for similarity scoring. When no corpus exists for the
        requested format, falls back to the colour-identity inference
        (returning "UR", "WUR", etc.) so the user still sees something
        meaningful — they just won't get archetype names without a
        format-specific corpus.
        """
        cards = list(cards_key)
        cast = list(cast_key) if cast_key else None
        fmt_corpus = corpora.get(fmt)
        if fmt_corpus and fmt_corpus["decks"]:
            name, _s, _d = classify_by_similarity(
                cards, fmt_corpus["decks"],
                fmt_corpus["weights"], fmt_corpus["colors"],
                cast_cards=cast,
            )
            return name
        # No corpus for this format — produce a colour-code label.
        from metahunter_core.classifier import (
            SKIP_LABEL, _LAND_NAMES, _TOKENS, infer_color_identity,
        )
        if not cards:
            return SKIP_LABEL
        obs = set(cards)
        informative = obs - _LAND_NAMES - _TOKENS
        if len(informative) < 5:
            return SKIP_LABEL
        cols = infer_color_identity(cards, universal_card_colors, cast)
        return cols or "Colourless"

    @lru_cache(maxsize=20000)
    def _published_lookup_cached(
        cards_key: tuple[str, ...], fmt: str, player: str,
        min_consistency: float = MIN_CONSISTENCY,
    ) -> str | None:
        """Archetype from the player's own published list, if it fits.

        MTGO publishes the full 75 for league 5-0s and challenge top-32
        finishes, and the corpus already stores the player name against
        each. When an opponent appears there and what we are seeing
        agrees with what they registered, that beats inferring from a
        handful of revealed cards: it identifies the deck from about
        three cards rather than a dozen.

        Returns None when the player is unknown, the list is stale, or
        the cards contradict it, so the caller falls through.
        """
        fmt_index = player_indexes.get(fmt)
        if not fmt_index or not player:
            return None
        hit = lookup_opponent_deck(
            fmt_index, player, cards_key, min_consistency=min_consistency
        )
        return hit[0].archetype if hit else None

    def _classify(
        cards: list[str],
        cast: list[str] | None = None,
        fmt: str = "Legacy",
        player: str | None = None,
    ) -> str:
        cards_key = tuple(sorted(set(cards)))
        cast_key = tuple(sorted(set(cast))) if cast else ()
        fmt = fmt or "Legacy"

        similar = _classify_cached(cards_key, cast_key, fmt)
        if not player:
            return similar

        # A published decklist is consulted only when the cards in front
        # of us say nothing usable — too few of them, or a bare colour
        # code. It never overrules a real archetype.
        #
        # It used to, at 75% agreement, and that was a clear regression:
        # two decks in the same colours share lands and staples, so 75%
        # is easy to reach between lists that are nothing alike. It
        # relabelled a Naya Initiative deck "Mystic Forge Combo", and a
        # Dimir Tempo deck "Dimir Delver" when no Delver was ever cast,
        # purely because those players had published something else
        # within the last six weeks. What somebody registered a month
        # ago is not evidence about the deck they are playing now; the
        # cards on the battlefield are.
        if not (similar == SKIP_LABEL or _is_colour_code(similar)):
            return similar

        known = _published_lookup_cached(
            cards_key, fmt, player.lower(), MIN_CONSISTENCY
        )
        return known or similar

    def _is_colour_code(label: str) -> bool:
        if not label or label == SKIP_LABEL:
            return False
        if label == "Colourless":
            return True
        return 1 <= len(label) <= 5 and all(c in "WUBRG" for c in label)

    def _consensus(nbrs: list[str]) -> str | None:
        """Pick a consensus archetype from neighbour labels.

        Rules in order of strength — first to fire wins:

        1. **Majority** — the most-frequent name has strictly more than
           half the votes. Clean and conservative.

        2. **Colour-family** — every neighbour shares the same colour
           prefix (Jeskai, Grixis, Bant, 5-Color, …), even if the
           tail differs. Pick the most-common specific name within the
           family. Catches "Jeskai Control"/"Jeskai Tempo" streaks.

        3. **Plurality** — when neither above fires but ≥2 neighbours
           agree on a single name AND that name beats every other name
           individually, use it. This rescues the case where the focal
           match is an outlier in a streak that has SOME variety
           (e.g. 2 Grixis Control + 1 Jeskai Control + 1 Grixis Tempo
           — Grixis Control wins as plurality even though it's not a
           strict majority).
        """
        if not nbrs:
            return None
        count = Counter(nbrs)
        most_common, n = count.most_common(1)[0]
        # 1) Strict majority
        if n * 2 > len(nbrs):
            return most_common
        # 2) Colour-family
        prefixes = {colour_prefix(name) for name in nbrs}
        prefixes.discard(None)
        if len(prefixes) == 1:
            family = next(iter(prefixes))
            in_family = [name for name in nbrs if colour_prefix(name) == family]
            return Counter(in_family).most_common(1)[0][0]
        # 3) Plurality with ≥2 votes and clear leader
        if n >= 2:
            ties = [name for name, c in count.items() if c == n]
            if len(ties) == 1:
                return most_common
        return None

    def _session_user_decks(
        matches: list, user: str, fmt: str = "Legacy"
    ) -> dict[str, str]:
        """Classify each match's user-side deck, then upgrade any
        colour-code or single-anomaly labels using nearby league
        history.

        Neighbour window: the previous 2 and next 2 matches (by time),
        skipping SKIP_LABEL and colour-code labels — i.e. we look at
        the four nearest specific-archetype matches regardless of how
        many days apart they are. This is the user's preference: a
        league game today and another league game three days from now
        are still the same deck.

        Consensus rule: majority OR same colour-family (see
        ``_consensus``). The strict unanimous gate has been retired;
        a single mis-labelled match in a 5-game streak no longer blocks
        the override.

        SKIP_LABEL labels are NEVER upgraded — the user wants too-thin
        matches dropped from every aggregation, not patched up by
        guesswork from neighbours.
        """
        sorted_m = sorted(
            [m for m in matches if user in m.players],
            key=lambda m: m.log_mtime or 0,
        )
        labels: dict[str, str] = {}
        # Cache each match's observed colour identity so the session
        # inference loop can reject colour-incompatible upgrades.
        # Looked up from the per-format corpus's card_colors table.
        fmt_colors = corpora.get(fmt, {}).get("colors") or universal_card_colors
        observed_colors_by_id: dict[str, frozenset[str]] = {}
        for m in sorted_m:
            cards = m.cards_by_player.get(user, [])
            cast = m.cards_cast_by_player.get(user, [])
            labels[m.match_id] = _classify(cards, cast, fmt=fmt)
            observed_colors_by_id[m.match_id] = frozenset(
                infer_color_identity(cards, fmt_colors, cast)
            )

        NEIGHBOUR_COUNT = 3  # ±3 specific-archetype games on each side
        # First pass: snapshot the original labels so neighbour-lookup
        # is based on what was independently classified, not on what
        # we've already overridden in this loop. Otherwise an early
        # override could propagate to its successors.
        original = dict(labels)
        for i, m in enumerate(sorted_m):
            focal = original[m.match_id]
            if focal == SKIP_LABEL:
                # The user explicitly wants too-thin matches ignored.
                continue
            nbrs: list[str] = []
            before = 0
            j = i - 1
            while j >= 0 and before < NEIGHBOUR_COUNT:
                lbl = original[sorted_m[j].match_id]
                if lbl != SKIP_LABEL and not _is_colour_code(lbl):
                    nbrs.append(lbl)
                    before += 1
                j -= 1
            after = 0
            j = i + 1
            while j < len(sorted_m) and after < NEIGHBOUR_COUNT:
                lbl = original[sorted_m[j].match_id]
                if lbl != SKIP_LABEL and not _is_colour_code(lbl):
                    nbrs.append(lbl)
                    after += 1
                j += 1
            consensus = _consensus(nbrs)
            if consensus is None or consensus == focal:
                continue
            # Colour-compatibility gate. The consensus archetype's
            # name-prefix encodes its colour pool (Grixis = U+B+R,
            # Jeskai = W+U+R, Sultai = U+B+G, …). For the upgrade to
            # be honest, the focal match's observed colours must fit
            # INSIDE that pool — the user may have failed to cast a
            # given colour in a single short game (UB observed for a
            # Grixis deck where no red spell hit the table), but they
            # can never have a colour the deck doesn't run.
            #
            # Examples:
            #   * UB observed,  Grixis (UBR) consensus → ALLOW  (no
            #     red cast this match — fine, the deck is Grixis).
            #   * UR observed,  Jeskai (WUR) consensus → ALLOW  (no
            #     white cast — fine).
            #   * BRG observed, Sultai (UBG) consensus → REJECT (R
            #     can't be in a Sultai deck regardless of neighbours).
            #
            # Same shape as the raw classifier's name-prefix check,
            # but with the subset direction matching the "I-may-have-
            # missed-a-colour-but-never-added-one" semantics for
            # session-based upgrades.
            con_prefix = colour_prefix(consensus)
            if con_prefix is not None:
                pool = colour_prefix_to_set(con_prefix)
                obs_cols = observed_colors_by_id.get(m.match_id, frozenset())
                if pool and obs_cols and not obs_cols.issubset(pool):
                    continue
            # Override when consensus disagrees with the focal label.
            # We always override colour-codes; for specific-archetype
            # focals we require ≥2 neighbours so a single-match anomaly
            # can't flip what's actually a real deck switch.
            if _is_colour_code(focal) or len(nbrs) >= 2:
                labels[m.match_id] = consensus
        return labels

    # Shared cache for the derived tables below: saved decks, the card
    # index, exact deck links and event kinds. All are expensive to
    # rebuild and none change within a request.
    _deck_cache: dict[str, Any] = {
        "decks": None, "at": 0.0, "links": None, "index": None,
        "casual_ids": None, "event_kinds": None, "registered": None,
        "exact_links": None, "fingerprint": None, "versions": None,
        "version_index": None, "match_decks": None,
    }

    # Everything above except the card index is derived from the match
    # store, so all of it has to go when the store moves.
    _STORE_DERIVED = ("casual_ids", "event_kinds", "registered",
                      "exact_links", "links", "versions",
                      "version_index", "match_decks")

    def _sync_cache() -> None:
        """Drop store-derived caches when the store has changed.

        The log watcher writes while the app is running: a deck the
        player registers mid-session must appear without a restart,
        which a process-lifetime cache would prevent. The fingerprint
        query is two indexed COUNT/MAX reads, cheap enough to run on
        every request.
        """
        try:
            with open_store(db) as st:
                fp = st.fingerprint()
        except Exception:  # noqa: BLE001
            return  # store unreadable; keep whatever we already had
        if fp == _deck_cache.get("fingerprint"):
            return
        _deck_cache["fingerprint"] = fp
        for k in _STORE_DERIVED:
            _deck_cache[k] = None

    def _casual_match_ids() -> set[str]:
        """Match ids MTGO positively identifies as friendly games.

        Two sources, neither complete: the text log's ``League Set``
        flag and the history file's event blurb. A match is only listed
        here when one of them actually says it was casual — never on
        absence of evidence.

        Computed once per process. It reads the store directly rather
        than through ``_all_matches``, which would recurse.
        """
        cached = _deck_cache.get("casual_ids")
        if cached is not None:
            return cached

        import bisect

        import metahunter_core.parser.game_history as _gh

        recs: list[tuple[float, str]] = []
        try:
            for h in mtgo_history_files():
                p = _gh._Parser(h.read_bytes())
                try:
                    p.parse()
                except Exception:  # noqa: BLE001
                    pass
                recs += p.matches
        except Exception:  # noqa: BLE001
            recs = []
        recs.sort()
        starts = [r[0] for r in recs]

        def blurb_for(mtime: float | None) -> str | None:
            if mtime is None or not recs:
                return None
            k = bisect.bisect_left(starts, mtime)
            best, best_delta = None, 7200.0
            for x in (k - 1, k, k + 1):
                if 0 <= x < len(recs):
                    delta = abs(recs[x][0] - mtime)
                    if delta < best_delta:
                        best, best_delta = recs[x][1], delta
            return best

        try:
            with open_store(db) as st:
                registered = st.registered_by_match()
                rows = st.iter_matches()
        except Exception:  # noqa: BLE001
            _deck_cache["casual_ids"] = set()
            return set()

        out: set[str] = set()
        for m in rows:
            rd = registered.get(m.match_id) or {}
            kind = resolve_event_kind(
                text_log_kind=rd.get("event_kind"),
                league_flag=rd.get("is_league"),
                description=blurb_for(getattr(m, "log_mtime", None)),
            )
            if kind == CASUAL:
                out.add(m.match_id)
        _deck_cache["casual_ids"] = out
        return out

    def _all_matches(
        days: int | None,
        from_ts: float | None = None,
        to_ts: float | None = None,
        fmt: str = "Legacy",
        include_friendly: bool = False,
    ) -> list:
        """Pull matches from the store, scoped to one MTG format.

        Filtering precedence:
          * If ``from_ts`` is given it's the lower bound (inclusive).
          * Else if ``days`` is given the lower bound is now - days * 86400.
          * ``to_ts`` is an inclusive upper bound when present.
          * ``fmt`` filters by detected format. Defaults to "Legacy" so
            a Vintage-playing friend on the same MTGO client doesn't
            pollute the dashboard. Pass an empty string to disable.

        Old rows that pre-date format detection have format=NULL; we
        treat those as Legacy by default since the corpus is Legacy.
        """
        # Every endpoint reaches the store through here, which makes it
        # the one place that has to notice the watcher writing new rows.
        _sync_cache()

        cutoff_low: float | None = None
        if from_ts is not None:
            cutoff_low = from_ts
        elif days is not None:
            cutoff_low = time.time() - days * 86400
        with open_store(db) as store:
            rows = store.iter_matches(since_mtime=cutoff_low)
        if to_ts is not None:
            rows = [r for r in rows if (r.log_mtime or 0) <= to_ts]
        if fmt:
            # NULL format (legacy data from older parser versions) is
            # treated as the default ("Legacy").
            rows = [r for r in rows if (r.format or "Legacy") == fmt]

        # Friendly games never count, anywhere. Someone testing a brew or
        # conceding a practice game after one look at the board is not
        # evidence about anything, and folding those in makes every
        # figure slightly wrong in a direction no one can see.
        #
        # Filtered here rather than per-endpoint so a future caller
        # cannot forget: this is the only route to the match store.
        # Only matches MTGO positively identifies as casual are dropped;
        # an unknown event type is kept, since most of a long history
        # predates any record of what it was.
        if include_friendly:
            return rows
        casual = _casual_match_ids()
        if casual:
            rows = [r for r in rows if r.match_id not in casual]
        return rows

    @lru_cache(maxsize=1)
    def _primary_user() -> str | None:
        """The single MTGO username this dashboard belongs to.

        Computed once from the FULL database — not the per-request
        filtered subset — so format filtering or date ranges that drop
        all of the user's own matches don't suddenly cause the
        dashboard to claim some opponent or roommate's account is "the
        user". Cached for the lifetime of the app process.

        MTGO's AppFiles folders accumulate data from every account that
        ever logged in on a given Windows user, so a casual second
        account played by a relative still ends up in our DB. We
        unambiguously pick the player with the most matches as the
        owner of this dashboard.
        """
        with open_store(db) as store:
            rows = store.iter_matches()
        c: Counter[str] = Counter()
        for r in rows:
            for p in r.players:
                c[p] += 1
        return c.most_common(1)[0][0] if c else None

    # ---- endpoints ------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "db": str(db),
            "corpus_decks": len(corpus_decks),
            "archetypes": len(archetypes),
        }

    def _resolve_user(override: str | None) -> str | None:
        """Pick the MTGO account this request applies to.

        Honours an explicit ``?user=X`` query param when present, so
        the dashboard can be re-pointed at any second account on the
        same machine via the AccountPicker. Falls back to the
        primary user when no override is given.
        """
        return override or _primary_user()

    @app.get("/api/accounts")
    def accounts() -> list[dict[str, Any]]:
        """Local MTGO accounts that have played on this machine.

        An MTGO ``AppFiles\\<hash>`` folder is per-account: every match
        log inside it has that account as one of the two players. So
        we group matches by their containing folder, find the player
        who dominates each folder, and call them a local account.

        Opponents who happened to face the user many times are NOT
        local — they appear in one match per folder, never as the
        dominant player.

        Threshold: a folder needs ≥3 matches before we consider it
        large enough to host a real account (avoids quirks from a
        bootstrap folder MTGO might create with one stale log).
        """
        from collections import defaultdict
        from pathlib import Path as _Path

        with open_store(db) as store:
            rows = store.iter_matches()
        folder_matches: dict[str, list] = defaultdict(list)
        for r in rows:
            if r.log_path:
                folder = _Path(r.log_path).parent.name
                folder_matches[folder].append(r)

        # Per folder: pick the player with the highest share of
        # appearances. Aggregate across folders so an account that's
        # spread over multiple MTGO update folders shows up once.
        locals_: dict[str, dict[str, Any]] = {}
        for folder, ms in folder_matches.items():
            if len(ms) < 3:
                continue
            c: Counter[str] = Counter()
            for m in ms:
                for p in m.players:
                    c[p] += 1
            if not c:
                continue
            local_name, local_n = c.most_common(1)[0]
            last = max((m.log_mtime or 0.0) for m in ms if local_name in m.players)
            entry = locals_.setdefault(
                local_name,
                {"user": local_name, "matches": 0, "last_played": 0.0},
            )
            entry["matches"] += local_n
            if last > entry["last_played"]:
                entry["last_played"] = last

        out = sorted(locals_.values(), key=lambda x: -x["matches"])
        return out

    @app.get("/api/formats")
    def formats(
        user_override: str | None = Query(None, alias="user"),
    ) -> dict[str, int]:
        """Match counts grouped by detected format, for the selected user.

        Restricting to one user keeps a second MTGO account on the
        same Windows session — Samu_27 played by a relative, a friend
        who logged in once — from inflating the format counts and
        luring you into a section that's actually their data.
        """
        user = _resolve_user(user_override)
        with open_store(db) as store:
            rows = store.iter_matches()
        c: Counter[str] = Counter()
        for r in rows:
            if user is None or user in r.players:
                c[r.format or "Legacy"] += 1
        return dict(c)

    @app.get("/api/me")
    def me(
        days: int | None = Query(None, ge=1, le=3650),
        from_ts: float | None = Query(None, alias="from"),
        to_ts: float | None = Query(None, alias="to"),
        fmt: str = Query("Legacy", alias="format"),
        user_override: str | None = Query(None, alias="user"),
    ) -> dict[str, Any]:
        matches = _all_matches(days, from_ts, to_ts, fmt=fmt)
        user = _resolve_user(user_override)
        if not user:
            return {"user": None, "total_matches": 0, "window_days": days}
        user_decks = _session_user_decks(matches, user, fmt=fmt)
        wins = losses = games_w = games_l = 0
        counted = 0
        # On-the-play / on-the-draw splits. Determined by whose name is
        # m.first_player (the player who chose to play first in game 1).
        # Matches where the parser couldn't capture first_player drop
        # out of these counters but still count toward overall stats.
        play_w = play_l = draw_w = draw_l = 0
        first_mtime = None
        last_mtime = None
        for m in matches:
            if user not in m.players:
                continue
            if user_decks.get(m.match_id, SKIP_LABEL) == SKIP_LABEL:
                continue  # too little data — user wants these ignored
            opponent = next((p for p in m.players if p != user), None)
            # MTGO has no draws, so a missing match_winner is a parser
            # miss — user wants those ignored everywhere, not surfaced
            # as "undecided" filler that inflates the counters.
            if not opponent or not m.match_winner:
                continue
            counted += 1
            won = m.match_winner == user
            if won:
                wins += 1
            elif m.match_winner == opponent:
                losses += 1
            for g in m.games:
                if g.get("winner") == user:
                    games_w += 1
                elif g.get("loser") == user:
                    games_l += 1
            # Play / draw split. Skip when first_player wasn't captured.
            if m.first_player == user:
                if won: play_w += 1
                else:   play_l += 1
            elif m.first_player == opponent:
                if won: draw_w += 1
                else:   draw_l += 1
            if first_mtime is None or m.log_mtime < first_mtime:
                first_mtime = m.log_mtime
            if last_mtime is None or m.log_mtime > last_mtime:
                last_mtime = m.log_mtime
        decided = counted
        play_total = play_w + play_l
        draw_total = draw_w + draw_l
        return {
            "user": user,
            "window_days": days,
            "total_matches": counted,
            "decided_matches": decided,
            "match_wins": wins,
            "match_losses": losses,
            "match_winrate": (wins / decided) if decided else None,
            "game_wins": games_w,
            "game_losses": games_l,
            "game_winrate": (games_w / (games_w + games_l)) if (games_w + games_l) else None,
            "play_wins": play_w,
            "play_losses": play_l,
            "play_winrate": (play_w / play_total) if play_total else None,
            "draw_wins": draw_w,
            "draw_losses": draw_l,
            "draw_winrate": (draw_w / draw_total) if draw_total else None,
            "first_match_at": first_mtime,
            "last_match_at": last_mtime,
        }

    def _signature_for_bag(bag: list[str]) -> list[tuple[str, int]]:
        return Counter(
            c for c in bag if c not in SIGNATURE_NOISE
        ).most_common(8)

    @app.get("/api/decks")
    def decks(
        days: int | None = Query(None, ge=1, le=3650),
        from_ts: float | None = Query(None, alias="from"),
        to_ts: float | None = Query(None, alias="to"),
        fmt: str = Query("Legacy", alias="format"),
        user_override: str | None = Query(None, alias="user"),
    ) -> list[dict[str, Any]]:
        matches = _all_matches(days, from_ts, to_ts, fmt=fmt)
        user = _resolve_user(user_override)
        if not user:
            return []
        user_decks = _session_user_decks(matches, user, fmt=fmt)
        agg: dict[str, dict[str, float]] = defaultdict(
            lambda: {"wins": 0, "losses": 0, "last_played": 0.0}
        )
        for m in matches:
            if user not in m.players:
                continue
            opponent = next((p for p in m.players if p != user), None)
            if not opponent or not m.match_winner:
                continue
            arch = user_decks.get(m.match_id, SKIP_LABEL)
            if arch == SKIP_LABEL:
                continue  # too little data — user wants these ignored
            if m.match_winner == user:
                agg[arch]["wins"] += 1
            elif m.match_winner == opponent:
                agg[arch]["losses"] += 1
            if (m.log_mtime or 0) > agg[arch]["last_played"]:
                agg[arch]["last_played"] = m.log_mtime or 0

        # Noise pruning per user request: drop buckets with fewer than 5
        # matches whose last appearance is more than 30 days ago. Those
        # are almost always misclassifications (a colour code that
        # session inference couldn't upgrade, a one-shot brew that got
        # mis-labelled, etc.) and they clutter the panel without
        # carrying any actionable signal.
        #
        # We only apply this pruning when the window has enough data
        # for the threshold to be meaningful. If the user's format
        # selection (or a tight date range) leaves only a handful of
        # matches, hiding sub-5-match buckets would empty the panel.
        # Threshold: pruning kicks in once aggregations total ≥ 25 or
        # so — under that we surface everything we have.
        MIN_MATCHES = 5
        STALE_SECONDS = 30 * 86400
        PRUNE_FLOOR = 25
        now = time.time()
        window_total = sum(
            int(rec["wins"]) + int(rec["losses"]) for rec in agg.values()
        )
        prune_enabled = window_total >= PRUNE_FLOOR

        out = []
        for name, rec in agg.items():
            total = int(rec["wins"]) + int(rec["losses"])
            stale = (now - rec["last_played"]) > STALE_SECONDS
            if prune_enabled and total < MIN_MATCHES and stale:
                continue
            out.append({
                "archetype": name,
                "wins": int(rec["wins"]),
                "losses": int(rec["losses"]),
                "total": total,
                "last_played": rec["last_played"],
                "winrate": rec["wins"] / total if total else None,
            })
        out.sort(key=lambda x: -x["total"])
        return out

    @app.get("/api/opponents")
    def opponents(
        days: int | None = Query(None, ge=1, le=3650),
        from_ts: float | None = Query(None, alias="from"),
        to_ts: float | None = Query(None, alias="to"),
        fmt: str = Query("Legacy", alias="format"),
        user_override: str | None = Query(None, alias="user"),
    ) -> list[dict[str, Any]]:
        matches = _all_matches(days, from_ts, to_ts, fmt=fmt)
        user = _resolve_user(user_override)
        if not user:
            return []
        agg: dict[str, dict[str, int]] = defaultdict(lambda: {"wins": 0, "losses": 0})
        for m in matches:
            if user not in m.players:
                continue
            opponent = next((p for p in m.players if p != user), None)
            if not opponent or not m.match_winner:
                continue
            arch = _classify(
                m.cards_by_player.get(opponent, []),
                m.cards_cast_by_player.get(opponent, []),
                fmt=fmt,
            )
            if arch == SKIP_LABEL:
                continue
            if m.match_winner == user:
                agg[arch]["wins"] += 1
            elif m.match_winner == opponent:
                agg[arch]["losses"] += 1
        out = []
        for name, rec in agg.items():
            total = rec["wins"] + rec["losses"]
            out.append({
                "archetype": name,
                "wins": rec["wins"],
                "losses": rec["losses"],
                "total": total,
                "winrate": rec["wins"] / total if total else None,
            })
        out.sort(key=lambda x: -x["total"])
        return out

    @app.get("/api/matches")
    def matches(
        days: int | None = Query(None, ge=1, le=3650),
        from_ts: float | None = Query(None, alias="from"),
        to_ts: float | None = Query(None, alias="to"),
        your_deck: str | None = None,
        their_deck: str | None = None,
        opponent: str | None = None,
        result: str | None = Query(None, pattern="^(W|L)$"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=500),
        fmt: str = Query("Legacy", alias="format"),
        user_override: str | None = Query(None, alias="user"),
    ) -> dict[str, Any]:
        all_m = _all_matches(days, from_ts, to_ts, fmt=fmt)
        user = _resolve_user(user_override)
        user_decks = _session_user_decks(all_m, user, fmt=fmt) if user else {}
        rows = []
        for m in all_m:
            if user not in m.players:
                continue
            opp = next((p for p in m.players if p != user), None)
            if not opp:
                continue
            yarch = user_decks.get(m.match_id, SKIP_LABEL)
            oarch = _classify(
                m.cards_by_player.get(opp, []),
                m.cards_cast_by_player.get(opp, []),
                fmt=fmt,
            )
            # Honour the user's rule: matches with insufficient data
            # are completely ignored — never shown in the table.
            if yarch == SKIP_LABEL or oarch == SKIP_LABEL:
                continue
            # MTGO has no draws — a missing match_winner means we
            # couldn't parse a clear winner. Drop the row entirely
            # rather than display it as a "?" placeholder.
            if not m.match_winner:
                continue
            won = m.match_winner == user
            res = "W" if won else "L"
            # Filters are case-insensitive substring matches so "delv"
            # finds "Izzet Delver" / "Sultai Delver" / etc.
            if your_deck and your_deck.lower() not in yarch.lower():
                continue
            if their_deck and their_deck.lower() not in oarch.lower():
                continue
            if opponent and opponent.lower() not in opp.lower():
                continue
            if result and res != result:
                continue
            # MTGO logs "X chooses to play first" at the start of game 1
            # and we capture that as m.first_player. Match-level "on the
            # play" status is conventionally defined by game 1.
            if m.first_player is None:
                on_play: bool | None = None
            else:
                on_play = m.first_player == user
            rows.append({
                "match_id": m.match_id,
                "log_mtime": m.log_mtime,
                "opponent": opp,
                "your_deck": yarch,
                "their_deck": oarch,
                "match_winner": m.match_winner,
                "result": res,
                "your_games": sum(1 for g in m.games if g.get("winner") == user),
                "their_games": sum(1 for g in m.games if g.get("winner") == opp),
                "turns": m.turns,
                "on_play": on_play,
            })
        rows.sort(key=lambda r: -(r["log_mtime"] or 0))
        total = len(rows)
        start = (page - 1) * page_size
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": rows[start:start + page_size],
        }

    @app.get("/api/matchups")
    def matchups(
        days: int | None = Query(None, ge=1, le=3650),
        from_ts: float | None = Query(None, alias="from"),
        to_ts: float | None = Query(None, alias="to"),
        your_deck: str | None = None,
        fmt: str = Query("Legacy", alias="format"),
        user_override: str | None = Query(None, alias="user"),
    ) -> list[dict[str, Any]]:
        all_m = _all_matches(days, from_ts, to_ts, fmt=fmt)
        user = _resolve_user(user_override)
        if not user:
            return []
        user_decks = _session_user_decks(all_m, user, fmt=fmt)
        cell: dict[tuple[str, str], dict[str, int]] = defaultdict(
            lambda: {"wins": 0, "losses": 0}
        )
        for m in all_m:
            if user not in m.players:
                continue
            opp = next((p for p in m.players if p != user), None)
            if not opp or not m.match_winner:
                continue
            yarch = user_decks.get(m.match_id, SKIP_LABEL)
            oarch = _classify(
                m.cards_by_player.get(opp, []),
                m.cards_cast_by_player.get(opp, []),
                fmt=fmt,
            )
            if yarch == SKIP_LABEL or oarch == SKIP_LABEL:
                continue
            if your_deck and yarch != your_deck:
                continue
            key = (yarch, oarch)
            if m.match_winner == user:
                cell[key]["wins"] += 1
            elif m.match_winner == opp:
                cell[key]["losses"] += 1
        out = []
        for (y, o), rec in cell.items():
            total = rec["wins"] + rec["losses"]
            out.append({
                "your_deck": y,
                "their_deck": o,
                "wins": rec["wins"],
                "losses": rec["losses"],
                "total": total,
                "winrate": rec["wins"] / total if total else None,
            })
        out.sort(key=lambda r: (-r["total"], r["your_deck"], r["their_deck"]))
        return out

    @app.get("/api/timeline")
    def timeline(
        days: int | None = Query(None, ge=1, le=3650),
        from_ts: float | None = Query(None, alias="from"),
        to_ts: float | None = Query(None, alias="to"),
        fmt: str = Query("Legacy", alias="format"),
        user_override: str | None = Query(None, alias="user"),
    ) -> list[dict[str, Any]]:
        """Per-day match aggregates for the active user.

        Returns ascending-by-date list of:
          { date, matches, wins, losses, winrate, ts }
        Days without any matches are filled with zeros so the chart's
        x-axis is continuous.
        """
        from datetime import date, datetime, timedelta, timezone

        all_m = _all_matches(days, from_ts, to_ts, fmt=fmt)
        user = _resolve_user(user_override)
        if not user:
            return []

        buckets: dict[str, dict[str, Any]] = {}
        first_d: date | None = None
        last_d: date | None = None
        for m in all_m:
            if user not in m.players:
                continue
            opp = next((p for p in m.players if p != user), None)
            # Skip matches with no detected winner — MTGO has no draws,
            # so these are parser misses the user wants invisible.
            if not opp or not m.log_mtime or not m.match_winner:
                continue
            d = datetime.fromtimestamp(m.log_mtime, tz=timezone.utc).date()
            key = d.isoformat()
            b = buckets.setdefault(key, {"date": key, "matches": 0, "wins": 0, "losses": 0})
            b["matches"] += 1
            if m.match_winner == user:
                b["wins"] += 1
            elif m.match_winner == opp:
                b["losses"] += 1
            if first_d is None or d < first_d:
                first_d = d
            if last_d is None or d > last_d:
                last_d = d

        if not buckets or first_d is None or last_d is None:
            return []

        # Fill missing days with zero rows.
        out: list[dict[str, Any]] = []
        cursor = first_d
        while cursor <= last_d:
            key = cursor.isoformat()
            b = buckets.get(key) or {"date": key, "matches": 0, "wins": 0, "losses": 0}
            decided = b["wins"] + b["losses"]
            b["winrate"] = (b["wins"] / decided) if decided else None
            b["ts"] = int(datetime(cursor.year, cursor.month, cursor.day, tzinfo=timezone.utc).timestamp())
            out.append(b)
            cursor += timedelta(days=1)
        return out

    @app.get("/api/match/{match_id}")
    def match(
        match_id: str,
        user_override: str | None = Query(None, alias="user"),
    ) -> dict[str, Any]:
        # Disable the format filter for a direct lookup — the user came
        # in with an exact match_id and shouldn't get an empty response
        # just because the dashboard happens to be on a different format.
        all_m = _all_matches(None, fmt="")
        for m in all_m:
            if m.match_id == match_id:
                user = _resolve_user(user_override)
                cards_by_player = m.cards_by_player
                cards_cast_by_player = m.cards_cast_by_player
                # Classify each player using the match's own detected
                # format so a Vintage match isn't matched against the
                # Legacy corpus and vice-versa.
                match_fmt = m.format or "Legacy"
                arch = {
                    p: _classify(
                        cards_by_player.get(p, []),
                        cards_cast_by_player.get(p, []),
                        fmt=match_fmt,
                    )
                    for p in m.players
                }
                signatures = {
                    p: _signature(cards_by_player.get(p, []), n=15) for p in m.players
                }
                return {
                    "match_id": m.match_id,
                    "log_path": m.log_path,
                    "log_mtime": m.log_mtime,
                    "players": m.players,
                    "first_player": m.first_player,
                    "turns": m.turns,
                    "match_winner": m.match_winner,
                    "score": [m.score_won, m.score_lost] if m.score_won is not None else None,
                    "games": m.games,
                    "archetypes": arch,
                    "signatures": {p: [{"name": n, "count": c} for n, c in sigs]
                                   for p, sigs in signatures.items()},
                    "cards_by_player": cards_by_player,
                    "user": user,
                }
        raise HTTPException(404, "match not found")

    @app.get("/api/match/{match_id}/log")
    def match_log(match_id: str) -> dict[str, Any]:
        """Return the human-readable text content of the match's .dat
        log file.

        MTGO's Match_GameLog_*.dat is a mostly-text binary format with
        sparse control bytes scattered through it. We decode the raw
        bytes as latin-1 (round-trips arbitrary bytes safely) and then
        substitute the binary noise with a thin marker so the
        printable parts (player names, card tokens, attack/cast/draw
        events) stay readable in the UI.

        Capped at 200 KB of input to keep huge logs from bloating the
        response. That's well above any real MTGO match log.
        """
        all_m = _all_matches(None, fmt="")
        m = next((row for row in all_m if row.match_id == match_id), None)
        if m is None:
            raise HTTPException(404, "match not found")
        try:
            raw = Path(m.log_path).read_bytes()
        except (OSError, FileNotFoundError):
            raise HTTPException(404, "log file unavailable")
        MAX_BYTES = 200_000
        truncated = len(raw) > MAX_BYTES
        if truncated:
            raw = raw[:MAX_BYTES]
        # latin-1 is the standard "decode any byte to a single
        # codepoint" trick. We then strip the non-printable control
        # bytes — the C0 control range (\x00-\x1f except \t\n\r),
        # DEL (\x7f), the C1 control range (\x80-\x9f) and the rest of
        # the high-byte range (\xa0-\xff) which is binary record
        # padding in MTGO's log format. Whatever survives is
        # printable ASCII (player names, card tokens, event verbs) —
        # exactly what the user wants to read.
        text = raw.decode("latin-1", errors="replace")
        import re as _re
        # Replace every non-ASCII-printable byte with a newline so the
        # readable text segments stand on their own lines.
        text = _re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\xff]+", "\n", text)
        # Collapse runs of blank lines / whitespace.
        text = _re.sub(r"\n{2,}", "\n", text)
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        return {
            "match_id": match_id,
            "log_path": m.log_path,
            "size_bytes": len(raw),
            "truncated": truncated,
            "text": text,
        }

    # Serve the built frontend in production. The SPA's client-side
    # ---- saved MTGO decks -----------------------------------------------
    #
    # MTGO writes every deck the player saves to its own XML file and keeps
    # it current as they edit, which makes those files the authoritative
    # decklist — far better than inferring one from cards observed in play,
    # which only ever sees what happened to be drawn.
    #
    # Attributing a *match* to a specific deck is inference, though, and is
    # reported as such. MTGO records which deck was registered only in its
    # rolling text log, which this app does not read yet, so we fall back to
    # matching on the cards actually cast. That cannot reliably separate
    # near-identical variants of the same shell, and the response says so
    # rather than quietly picking one.



    def _build_card_index_worker() -> None:
        """Download and write the Scryfall index. Runs off the request."""
        try:
            n = build_card_index(card_index_path())
            log.info("card index rebuilt: %s cards", f"{n:,}")
        except Exception as exc:  # noqa: BLE001
            # A failed download must not take the app down or retry in a
            # tight loop; the next start tries again.
            log.warning("card index build failed: %s", exc)
        finally:
            _deck_cache["index"] = None
            _deck_cache["index_building"] = False

    def _card_index():
        """Scryfall index, loaded once per process.

        Builds it in the background when it is missing, stale, or from
        an older index format. This has to be automatic: without it a
        fresh install shows every card as an unresolved catalog number
        until someone finds the refresh button, and a set released after
        the index was built never resolves at all.

        The build is a 74 MB download, so it never happens inline — the
        request returns whatever index exists now (possibly empty) and
        the next one picks up the result.
        """
        index = _deck_cache.get("index")
        if index is None:
            index = CardIndex.load(card_index_path())
            _deck_cache["index"] = index

        if (len(index) == 0 or index.is_stale) and not _deck_cache.get(
            "index_building"
        ):
            _deck_cache["index_building"] = True
            threading.Thread(
                target=_build_card_index_worker,
                name="card-index-build",
                daemon=True,
            ).start()
        return index

    def _saved_decks(force: bool = False) -> list:
        """Saved decks, re-read at most every 30 seconds.

        MTGO rewrites these files as the player edits, so a long cache
        would go stale mid-session. Re-reading ~65 small XML files is
        cheap enough that a short TTL beats a file watcher.
        """
        now = time.time()
        if force or _deck_cache["decks"] is None or now - _deck_cache["at"] > 30:
            try:
                _deck_cache["decks"] = load_decks(constructed_only=True)
            except Exception:  # noqa: BLE001
                _deck_cache["decks"] = []
            _deck_cache["at"] = now
            # Both link tables are keyed by deck contents, so an edit
            # that changes a maindeck invalidates them too.
            _deck_cache["links"] = None
            _deck_cache["exact_links"] = None
            _deck_cache["versions"] = None
            _deck_cache["version_index"] = None
            _deck_cache["match_decks"] = None
        return _deck_cache["decks"] or []

    def _match_event_kinds() -> dict[str, str]:
        """match id -> league / tournament / casual / unknown.

        Two sources, neither complete: MTGO's text log (exact, but the
        log rotates) and the history file's event blurb (wider, but
        pruned). Combined so a match is only called friendly when one of
        them actually says so.
        """
        cached = _deck_cache.get("event_kinds")
        if cached is not None:
            return cached

        import bisect

        import metahunter_core.parser.game_history as _gh

        # Event blurbs, by match start time.
        recs: list[tuple[float, str]] = []
        try:
            for h in mtgo_history_files():
                p = _gh._Parser(h.read_bytes())
                try:
                    p.parse()
                except Exception:  # noqa: BLE001
                    pass
                recs += p.matches
        except Exception:  # noqa: BLE001
            recs = []
        recs.sort()
        starts = [r[0] for r in recs]

        def blurb_for(mtime: float | None) -> str | None:
            # The log's mtime lands within a couple of hours of the
            # match start, so the nearest record inside that window is
            # the right one.
            if mtime is None or not recs:
                return None
            k = bisect.bisect_left(starts, mtime)
            best, best_delta = None, 7200.0
            for x in (k - 1, k, k + 1):
                if 0 <= x < len(recs):
                    delta = abs(recs[x][0] - mtime)
                    if delta < best_delta:
                        best, best_delta = recs[x][1], delta
            return best

        try:
            with open_store(db) as st:
                registered = st.registered_by_match()
        except Exception:  # noqa: BLE001
            registered = {}

        out: dict[str, str] = {}
        for m in _all_matches(None, fmt=""):
            rd = registered.get(m.match_id) or {}
            out[m.match_id] = resolve_event_kind(
                text_log_kind=rd.get("event_kind"),
                league_flag=rd.get("is_league"),
                description=blurb_for(getattr(m, "log_mtime", None)),
            )
        _deck_cache["event_kinds"] = out
        return out

    def _registered_by_match() -> dict:
        """Captured deck registrations, keyed by match id."""
        if _deck_cache.get("registered") is None:
            try:
                with open_store(db) as st:
                    _deck_cache["registered"] = st.registered_by_match()
            except Exception:  # noqa: BLE001
                _deck_cache["registered"] = {}
        return _deck_cache["registered"] or {}

    def _versions() -> list[dict[str, Any]]:
        """Every version of every deck we have ever seen.

        Kept in the store rather than read from disk, because MTGO
        overwrites a deck file when you edit it: the list you registered
        last week stops existing this week. Synced here so opening any
        deck page also captures whatever has changed since last time.
        """
        cached = _deck_cache.get("versions")
        if cached is not None:
            return cached
        try:
            with open_store(db) as st:
                deck_history.sync(st, _saved_decks())
                rows = st.deck_versions()
        except Exception as exc:  # noqa: BLE001 - history must never block
            log.warning("deck versions unavailable: %s", exc)
            rows = []
        _deck_cache["versions"] = rows
        return rows

    def _version_index() -> dict[str, dict[str, Any]]:
        """signature -> version."""
        cached = _deck_cache.get("version_index")
        if cached is None:
            cached = {v["signature"]: v for v in _versions()}
            _deck_cache["version_index"] = cached
        return cached

    def _deck_key(version: dict[str, Any]) -> str:
        """Which deck a version belongs to, for grouping.

        A version whose owning deck could not be established keeps its
        own key rather than being folded into a deck it may not belong
        to — the list is exact either way, only its parentage is not.
        """
        return version.get("deck_id") or f"orphan:{version['signature']}"

    def _exact_deck_links() -> dict[str, str]:
        """match id -> the *signature* of the list registered for it.

        Only where MTGO said so. Signatures compare the maindeck alone:
        players edit the sideboard between rounds of one league run,
        which rewrites the file, and demanding all 75 agree would reject
        the very games most worth attributing.
        """
        if _deck_cache.get("exact_links") is not None:
            return _deck_cache["exact_links"]

        known = _version_index()
        links: dict[str, str] = {}
        for match_id, rd in _registered_by_match().items():
            sig = maindeck_signature(rd["cards"])
            if sig in known:
                links[match_id] = sig
        _deck_cache["exact_links"] = links
        return links

    def _match_deck_ids() -> dict[str, str]:
        """match id -> deck key, by way of the version that was played."""
        cached = _deck_cache.get("match_decks")
        if cached is not None:
            return cached
        index = _version_index()
        out = {
            mid: _deck_key(index[sig])
            for mid, sig in _exact_deck_links().items()
            if sig in index
        }
        _deck_cache["match_decks"] = out
        return out

    _WUBRG = "WUBRG"

    def _deck_summary(deck, index) -> dict[str, Any]:
        """Deck colours, mana curve and how much of the list resolved.

        Colours come from what the cards actually cost, not from
        Scryfall's ``color_identity``. Identity is a Commander rule: it
        counts every mana symbol anywhere on the card, including rules
        text and the far side of a double-faced card. Tamiyo,
        Inquisitive Student is identity GU because her back face has a
        green symbol, which put a green pip on a Grixis deck that
        contains no green card and cannot produce green mana.
        """
        colors: set[str] = set()
        curve: dict[str, int] = defaultdict(int)
        resolved = 0
        for c in deck.maindeck:
            rec = index.get(c.mtgo_id)
            if rec is None:
                continue
            resolved += 1
            colors.update(rec.colors or "")
            if not rec.is_land:
                key = str(int(rec.cmc)) if rec.cmc < 7 else "7+"
                curve[key] += c.quantity
        ordered = sorted(colors, key=lambda ch: _WUBRG.index(ch)
                         if ch in _WUBRG else 99)
        return {
            "colors": "".join(ordered),
            "curve": dict(sorted(curve.items())),
            "resolved_cards": resolved,
        }

    def _deck_summary_cards(cards, index) -> dict[str, Any]:
        """Colours, curve and resolution for a list of cards.

        The version-aware twin of _deck_summary, which takes a parsed
        deck file. A version we recovered has no file behind it.
        """
        colors: set[str] = set()
        curve: dict[str, int] = defaultdict(int)
        resolved = 0
        for c in cards:
            rec = index.get(c.mtgo_id)
            if rec is None:
                continue
            resolved += 1
            colors.update(rec.colors or "")
            if not rec.is_land:
                key = str(int(rec.cmc)) if rec.cmc < 7 else "7+"
                curve[key] += c.quantity
        ordered = sorted(colors, key=lambda ch: _WUBRG.index(ch)
                         if ch in _WUBRG else 99)
        return {
            "colors": "".join(ordered),
            "curve": dict(sorted(curve.items())),
            "resolved_cards": resolved,
        }

    def _named_changes(raw: dict[str, list], index) -> dict[str, list]:
        """Turn a catalog-id diff into card names the UI can print.

        Without names a version list reads "+4 #112116 / -4 #52913",
        which tells a player nothing about what they changed.
        """
        def name_of(cid: int) -> str:
            rec = index.get(cid)
            return rec.name if rec else f"#{cid}"

        out: dict[str, list] = {}
        for key, entries in raw.items():
            merged: dict[str, int] = {}
            for cid, qty in entries:
                merged[name_of(cid)] = merged.get(name_of(cid), 0) + qty
            out[key] = [
                {"name": n, "quantity": q}
                for n, q in sorted(merged.items(), key=lambda kv: -kv[1])
            ]
        return out

    def _deck_card_frequency(decks: list, index) -> dict[str, int]:
        """How many of the player's own decks contain each card."""
        freq: dict[str, int] = defaultdict(int)
        for d in decks:
            seen = set()
            for c in d.cards:
                rec = index.get(c.mtgo_id)
                if rec and rec.name:
                    seen.add(rec.name)
            for name in seen:
                freq[name] += 1
        return freq

    def _key_cards(
        deck, index, freq: dict[str, int], total_decks: int, limit: int = 4,
    ) -> list[dict[str, Any]]:
        """The cards that make this deck recognisable at a glance.

        Sorting by copies and mana value put Force of Will on every blue
        deck, which told the reader nothing: a card in most of the
        collection is exactly the card that cannot identify one deck
        within it.

        So score by rarity across the player's own decks instead. A card
        in one list out of sixty-five names that list; a card in fifty
        is wallpaper. Copies still act as a tie-break, because a
        four-of is more defining than a singleton at equal rarity, and
        lands are held back unless the deck is land-defined and has
        little else to show.
        """
        rows = []
        for c in deck.maindeck:
            rec = index.get(c.mtgo_id)
            if rec is None or rec.is_basic_land:
                continue
            rows.append((c.quantity, rec))

        nonland = [r for r in rows if not r[1].is_land]
        pool = nonland or rows
        if not pool:
            return []

        def distinctiveness(rec) -> float:
            # Fraction of decks *without* this card: 0 when everyone runs
            # it, approaching 1 when almost nobody does.
            in_decks = freq.get(rec.name, 1)
            return 1.0 - (in_decks / max(total_decks, 1))

        pool.sort(
            key=lambda t: (-distinctiveness(t[1]), -t[0], -t[1].cmc, t[1].name)
        )
        return [
            {
                "name": rec.name,
                "quantity": qty,
                "mana_cost": rec.mana_cost,
                "type_line": rec.type_line,
                "image": rec.image_url("normal"),
                "art": rec.image_url("art_crop"),
                # Surfaced so the UI can pick a face that is actually
                # specific to this deck rather than the first card.
                "decks_with_card": freq.get(rec.name, 1),
            }
            for qty, rec in pool[:limit]
        ]

    @app.get("/api/decklists")
    def decklists(
        user_override: str | None = Query(None, alias="user"),
        fmt: str | None = Query(None, alias="format"),
        refresh: bool = Query(False),
    ) -> dict[str, Any]:
        """Every deck saved in the MTGO client, with how it has performed."""
        index = _card_index()
        decks = _saved_decks(force=refresh)
        user = _resolve_user(user_override)

        matches = _all_matches(None, fmt="") if user else []
        # Keyed by deck, resolved through whichever *version* was
        # registered — so a match played before an edit still counts
        # toward the deck it was played with.
        links = _match_deck_ids() if user else {}

        kinds = _match_event_kinds() if user else {}

        agg: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"wins": 0, "losses": 0, "last_played": 0.0}
        )
        for m in matches:
            deck_id = links.get(m.match_id)
            if deck_id is None or not m.match_winner:
                continue
            # A practice game against a friend says nothing about how a
            # deck performs, so it never reaches a win rate.
            if kinds.get(m.match_id) == CASUAL:
                continue
            a = agg[deck_id]
            if m.match_winner == user:
                a["wins"] += 1
            else:
                a["losses"] += 1
            a["last_played"] = max(a["last_played"], m.log_mtime or 0.0)

        freq = _deck_card_frequency(decks, index)
        total_decks = len(decks)

        out = []
        for d in decks:
            if fmt and d.format.lower() != fmt.lower():
                continue
            rec = agg.get(d.deck_id)
            wins = rec["wins"] if rec else 0
            losses = rec["losses"] if rec else 0
            played = wins + losses
            out.append({
                "id": d.deck_id,
                "name": d.name,
                "format": d.format,
                "maindeck_count": d.maindeck_count,
                "sideboard_count": d.sideboard_count,
                "modified_at": d.modified_at,
                "wins": wins,
                "losses": losses,
                "matches": played,
                "winrate": (wins / played) if played else None,
                "last_played": (rec["last_played"] or None) if rec else None,
                "key_cards": _key_cards(d, index, freq, total_decks),
                **_deck_summary(d, index),
            })

        out.sort(key=lambda r: (r["last_played"] or 0, r["modified_at"]),
                 reverse=True)
        return {
            "decks": out,
            "card_index_size": len(index),
            # True while the Scryfall index is downloading. Card names
            # are missing until it lands, and the UI should say so
            # rather than let the list look broken.
            "card_index_building": bool(_deck_cache.get("index_building")),
            # Only matches MTGO itself recorded a registered deck for.
            # This grows while the app is open and cannot be backfilled:
            # the client rotates the log that carries it.
            "attributed_matches": len(links),
            "total_matches": len(matches),
            # Counted from the casual set itself, not from `kinds`: by
            # the time a match reaches here it has already been dropped,
            # so the surviving rows can never account for the exclusion.
            "excluded_friendly": len(_casual_match_ids()) if user else 0,
        }

    # Games a hand size needs before its win rate is worth stating. A
    # 3-1 is not a 75% keep, and printing it as one invents a certainty
    # the sample does not carry.
    MIN_OPENING_GAMES = 8

    def _opening_summary(openings: list[dict[str, Any]]) -> dict[str, Any]:
        """Mulligan depth and how those games went.

        One row per game, because a mulligan is a per-game decision and
        a three-game match contributes three openings.

        Hand SIZE only. MTGO names a card in the log once it is played
        or revealed, and nothing is revealed before turn one, so what
        was actually in an opening hand is in no file MTGO writes.
        There is no "lands kept" here because there is no honest way to
        produce one.
        """
        if not openings:
            return {"games": 0, "by_kept": []}
        buckets: dict[int, dict[str, int]] = defaultdict(
            lambda: {"games": 0, "wins": 0, "decided": 0}
        )
        for o in openings:
            b = buckets[int(o["kept"])]
            b["games"] += 1
            if o["won"] is not None:
                b["decided"] += 1
                b["wins"] += 1 if o["won"] else 0
        total = sum(b["games"] for b in buckets.values())
        return {
            "games": total,
            "by_kept": [
                {
                    "kept": kept,
                    "mulligans": OPENING_HAND_SIZE - kept,
                    "games": b["games"],
                    "share": b["games"] / total if total else 0.0,
                    "wins": b["wins"],
                    "winrate": (
                        b["wins"] / b["decided"]
                        if b["decided"] >= MIN_OPENING_GAMES else None
                    ),
                }
                for kept, b in sorted(buckets.items(), reverse=True)
            ],
        }

    def _weekly_trend(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Win rate week by week, oldest first.

        Weeks with no play are absent rather than drawn as zero: a week
        you did not play is not a week you lost every game.
        """
        weeks: dict[str, dict[str, int]] = defaultdict(
            lambda: {"matches": 0, "wins": 0}
        )
        for h in history:
            if not h.get("result") or not h.get("played_at"):
                continue
            # Monday of the week the match was played, so buckets line
            # up with how leagues are actually run.
            t = time.localtime(h["played_at"])
            monday = time.mktime(
                (t.tm_year, t.tm_mon, t.tm_mday - t.tm_wday, 0, 0, 0, 0, 0, -1)
            )
            key = time.strftime("%Y-%m-%d", time.localtime(monday))
            weeks[key]["matches"] += 1
            weeks[key]["wins"] += 1 if h["result"] == "W" else 0
        return [
            {
                "week": week,
                "matches": v["matches"],
                "wins": v["wins"],
                "winrate": v["wins"] / v["matches"] if v["matches"] else None,
            }
            for week, v in sorted(weeks.items())
        ]

    @app.get("/api/decklists/{deck_id}")
    def decklist_detail(
        deck_id: str,
        user_override: str | None = Query(None, alias="user"),
        version: str | None = Query(
            None, description="Signature of the version to show; latest by default"
        ),
    ) -> dict[str, Any]:
        """One deck: the full 75, who it has faced, and when."""
        index = _card_index()

        # Stored versions are only used to find the deck here. Which lists
        # are shown is decided below, from what was actually played.
        all_versions = [
            v for v in _versions() if _deck_key(v) == deck_id
        ]
        deck = next((d for d in _saved_decks() if d.deck_id == deck_id), None)
        if deck is None and not all_versions:
            raise HTTPException(status_code=404, detail="deck not found")

        def name_of(cid: int) -> str:
            rec = index.get(int(cid))
            return rec.name if rec else f"#{int(cid)}"

        def render(cards) -> list[dict[str, Any]]:
            # MTGO gives a foil printing its own catalog number, so a
            # deck holding two foil Bilbo and one regular arrives as two
            # rows. They are the same card to a reader, so they are
            # merged here on the resolved card and their copies summed.
            merged: dict[Any, Any] = {}
            for c in cards:
                rec = index.get(c.mtgo_id)
                key = rec.name if rec else ("unresolved", c.mtgo_id)
                if key in merged:
                    merged[key][1] += c.quantity
                else:
                    merged[key] = [c.mtgo_id, c.quantity, rec]

            rows = []
            for mtgo_id, quantity, rec in merged.values():
                rows.append({
                    "mtgo_id": mtgo_id,
                    "quantity": quantity,
                    # An unresolved catalog id still appears, labelled, so
                    # the list stays a true 75 instead of silently short.
                    # These are nearly always cards from a set released
                    # in the last few weeks: MTGO has the catalog number
                    # before Scryfall publishes the mapping for it.
                    "name": rec.name if rec else "New card #%d" % mtgo_id,
                    "mana_cost": rec.mana_cost if rec else "",
                    "type_line": rec.type_line if rec else "",
                    "cmc": rec.cmc if rec else 0.0,
                    "colors": rec.color_identity if rec else "",
                    "rarity": rec.rarity if rec else "",
                    "set": rec.set_code if rec else "",
                    "image": rec.image_url("normal") if rec else None,
                    "art": rec.image_url("art_crop") if rec else None,
                    "resolved": rec is not None,
                })
            rows.sort(key=lambda r: (
                "land" in (r["type_line"] or "").lower(),
                -r["quantity"], r["cmc"], r["name"],
            ))
            return rows

        user = _resolve_user(user_override)
        matches = _all_matches(None, fmt="") if user else []
        deck_by_match = _match_deck_ids() if user else {}
        registered = _registered_by_match() if user else {}
        kinds_all = _match_event_kinds() if user else {}

        # The lists this deck was actually played with.
        #
        # Built from MTGO's registrations, not from the saved versions.
        # The deck file is rewritten on every click in the editor, so the
        # saved versions are mostly states nobody played — seventeen for
        # one deck, two of them played. And a registration is the exact 75
        # that was used, sideboard included, where a saved version keeps
        # whichever sideboard the file had when that maindeck first
        # appeared.
        #
        # Grouped by card name: MTGO numbers every printing separately, and
        # a list re-saved with foil copies is the same list. Two versions
        # once showed twenty-one cards apart that were identical.
        played: dict[str, dict[str, Any]] = {}
        list_key_by_match: dict[str, str] = {}
        for m in matches:
            if deck_by_match.get(m.match_id) != deck_id:
                continue
            if kinds_all.get(m.match_id) == CASUAL or not m.match_winner:
                continue
            reg = registered.get(m.match_id)
            if not reg or not reg.get("cards"):
                continue
            key = deck_history.name_fingerprint(reg["cards"], name_of)
            list_key_by_match[m.match_id] = key
            at = m.log_mtime or 0.0
            slot = played.get(key)
            if slot is None:
                slot = played[key] = {
                    "key": key, "cards": reg["cards"], "wins": 0, "losses": 0,
                    "first_played": at, "last_played": at,
                }
            slot["wins" if m.match_winner == user else "losses"] += 1
            slot["first_played"] = min(slot["first_played"], at)
            if at >= slot["last_played"]:
                slot["last_played"] = at
                slot["cards"] = reg["cards"]

        # The list saved in MTGO right now, exactly as the file has it.
        current_key = None
        if deck is not None:
            current_cards = [
                [c.mtgo_id, c.quantity, 1 if c.sideboard else 0] for c in deck.cards
            ]
            current_key = deck_history.name_fingerprint(current_cards, name_of)
            if current_key in played:
                played[current_key]["cards"] = current_cards
            else:
                played[current_key] = {
                    "key": current_key, "cards": current_cards, "wins": 0,
                    "losses": 0, "first_played": None, "last_played": None,
                }

        # Oldest first, so each list's changes are against the one before
        # it. A current list nobody has played yet is the newest of all.
        chronological = sorted(
            played.values(),
            key=lambda x: (x["first_played"] is None, x["first_played"] or 0.0),
        )
        version_rows = []
        previous = None
        for item in chronological:
            w, ls = item["wins"], item["losses"]
            version_rows.append({
                "signature": item["key"],
                "current": item["key"] == current_key,
                # When this list first saw play; for a current list nobody
                # has played yet, when the file was saved.
                "changed_at": item["first_played"]
                if item["first_played"] is not None
                else (deck.modified_at if deck else 0.0),
                "first_played": item["first_played"],
                "last_played": item["last_played"],
                "source": "played" if (w + ls) else "current",
                "maindeck_count": sum(int(c[1]) for c in item["cards"] if not c[2]),
                "sideboard_count": sum(int(c[1]) for c in item["cards"] if c[2]),
                "wins": w,
                "losses": ls,
                "matches": w + ls,
                "winrate": (w / (w + ls)) if (w + ls) else None,
                "changes": deck_history.name_diff(
                    previous["cards"], item["cards"], name_of
                ) if previous else None,
            })
            previous = item
        # Current first, then by when each list was last played.
        version_rows.sort(
            key=lambda r: (not r["current"], -(r["last_played"] or 0.0))
        )

        # `chosen` decides which cards are shown; `pinned` decides whether
        # the numbers narrow with them. Opening a deck shows the list saved
        # now, with the whole deck's record: a list edited an hour ago has
        # no games, and "no confirmed games" for a deck that has played
        # twenty would answer a question nobody asked. A version that no
        # longer exists — a stale selection — falls back rather than
        # breaking the page.
        pinned = bool(version) and version in played
        if pinned:
            chosen = played[version]
        elif current_key is not None:
            chosen = played[current_key]
        elif played:
            chosen = max(played.values(), key=lambda x: x["last_played"] or 0.0)
        else:
            chosen = None

        if chosen is not None:
            shown_cards = chosen["cards"]
        elif all_versions:
            shown_cards = all_versions[0]["cards"]
        else:
            shown_cards = []
        main_cards = [
            DeckCard(mtgo_id=int(c[0]), quantity=int(c[1]), sideboard=False)
            for c in shown_cards if not c[2]
        ]
        side_cards = [
            DeckCard(mtgo_id=int(c[0]), quantity=int(c[1]), sideboard=True)
            for c in shown_cards if c[2]
        ]

        history: list[dict[str, Any]] = []
        # Mulligan depth, one entry per GAME rather than per match.
        openings: list[dict[str, Any]] = []
        opponents: Counter = Counter()
        vs: dict[str, dict[str, int]] = defaultdict(
            lambda: {"wins": 0, "losses": 0}
        )
        kinds = _match_event_kinds() if user else {}

        for m in matches:
            if deck_by_match.get(m.match_id) != deck_id:
                continue
            # Narrow only when a version was explicitly requested. See
            # `pinned` above.
            if pinned and list_key_by_match.get(m.match_id) != chosen["key"]:
                continue
            if kinds.get(m.match_id) == CASUAL:
                continue
            opp = next((p for p in m.players if p != user), None)
            if not opp:
                continue
            opp_arch = _classify(
                m.cards_by_player.get(opp, []),
                m.cards_cast_by_player.get(opp, []),
                fmt=m.format or "Legacy",
                player=opp,
            )
            won = m.match_winner == user
            if m.match_winner:
                vs[opp_arch]["wins" if won else "losses"] += 1
                opponents[opp] += 1
            history.append({
                "match_id": m.match_id,
                "played_at": m.log_mtime,
                "opponent": opp,
                "opponent_archetype": opp_arch,
                "result": ("W" if won else "L") if m.match_winner else None,
                # The parser records the score as the *winner* wrote it
                # ("X wins the match 2-0"), so it must be flipped when the
                # user lost, or their losses read as 2-0 wins.
                "score": (
                    ("%s-%s" % (m.score_won, m.score_lost)) if won
                    else ("%s-%s" % (m.score_lost, m.score_won))
                ) if m.score_won is not None else None,
                # MTGO's own record of the registered deck, so there
                # is no confidence to report — it either matched or the
                # match is absent entirely.
                "event_kind": kinds.get(m.match_id, "unknown"),
            })
            for g in m.games:
                for hand in g.get("opening_hands") or []:
                    if hand.get("player") != user:
                        continue
                    winner = g.get("winner")
                    openings.append({
                        "kept": int(hand.get("kept", 0)),
                        "won": (winner == user) if winner else None,
                    })

        history.sort(key=lambda r: r["played_at"] or 0, reverse=True)
        matchups = sorted(
            (
                {
                    "archetype": k,
                    "wins": v["wins"],
                    "losses": v["losses"],
                    "matches": v["wins"] + v["losses"],
                    "winrate": (v["wins"] / (v["wins"] + v["losses"]))
                    if (v["wins"] + v["losses"]) else None,
                }
                for k, v in vs.items()
            ),
            key=lambda r: -r["matches"],
        )

        try:
            with open_store(db) as st:
                marks = st.league_marks()
        except Exception:  # noqa: BLE001 - a missing mark table must not break the page
            marks = set()

        wins = sum(1 for h in history if h["result"] == "W")
        losses = sum(1 for h in history if h["result"] == "L")

        return {
            "id": deck_id,
            "openings": _opening_summary(openings),
            "trend": _weekly_trend(history),
            "name": deck.name if deck else all_versions[0]["name"],
            "format": deck.format if deck else all_versions[0]["format"],
            "modified_at": deck.modified_at if deck
                           else ((chosen or {}).get("last_played") or 0.0),
            "maindeck": render(main_cards),
            "sideboard": render(side_cards),
            "maindeck_count": sum(c.quantity for c in main_cards),
            "sideboard_count": sum(c.quantity for c in side_cards),
            "version": chosen["key"] if chosen else None,
            # The list saved in MTGO now, when the deck still exists.
            "current_version": current_key,
            "versions": version_rows,
            # False when the record covers every version of the deck,
            # True when it covers only the list on screen.
            "version_pinned": pinned,
            "wins": wins,
            "losses": losses,
            "winrate": (wins / (wins + losses)) if (wins + losses) else None,
            "matchups": matchups,
            "history": history[:100],
            # Runs are scored over the full history, not the truncated
            # slice the UI lists.
            "leagues": league_runs(
                history, marks=marks, list_keys=list_key_by_match
            ),
            "distinct_opponents": len(opponents),
            **_deck_summary_cards(main_cards, index),
        }

    @app.post("/api/decklists/league-mark")
    def mark_league_entry(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Say a league entry ended on this match, or take that back.

        MTGO records no drop in any file the app can read, so without this
        a dropped entry is indistinguishable from one still running and
        every entry after it is grouped wrong. The mark stays on this
        machine.
        """
        match_id = str(payload.get("match_id") or "").strip()
        if not match_id:
            raise HTTPException(status_code=400, detail="match_id is required")
        ended = bool(payload.get("ended", True))
        with open_store(db) as st:
            st.set_league_mark(match_id, ended)
        return {"ok": True, "match_id": match_id, "ended": ended}

    @app.post("/api/decklists/refresh-cards")
    def refresh_card_index() -> dict[str, Any]:
        """Re-download the Scryfall card index."""
        try:
            n = build_card_index(card_index_path())
            _deck_cache["index"] = None
            return {"ok": True, "cards": n}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # Account: signing in, and linking this machine's MTGO player
    # ------------------------------------------------------------------
    #
    # Sign-in happens in the system browser, not in a window we drew, so
    # the user types their password into Discord's own page and we never
    # have the chance to see it. The browser returns to /auth/callback
    # below with a session token.

    @app.get("/api/auth/status")
    def auth_status(refresh: bool = Query(False)) -> dict[str, Any]:
        """Who is signed in, and which MTGO accounts they have claimed."""
        acct = account.refresh(force=refresh) if refresh else account.load()
        # A cached profile is used while offline, so the app keeps
        # working on a train; it is re-checked in the background.
        if acct.signed_in and not refresh:
            threading.Thread(target=account.refresh, daemon=True).start()
        return {
            "signed_in": acct.signed_in,
            "display_name": acct.display_name,
            "avatar_url": acct.avatar_url,
            "players": acct.players,
            "server": upload_server_url(),
        }

    @app.get("/api/auth/providers")
    def auth_providers() -> dict[str, Any]:
        """Which sign-in buttons to offer, straight from the server."""
        try:
            # Generous timeout: the API machine stops when idle, so the
            # first call of the day pays for a cold start. Reporting
            # "offline" because we gave up after ten seconds would send
            # people to check a connection that is perfectly fine.
            with net.urlopen(
                f"{upload_server_url()}/v1/auth/providers", timeout=30
            ) as resp:
                return json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001 - offline, or the API is asleep
            log.warning("could not fetch sign-in providers: %s", exc)
            return {"providers": [], "offline": True}

    @app.post("/api/auth/start")
    def auth_start(provider: str = Query("discord")) -> dict[str, Any]:
        """Open the browser to sign in. Returns the URL for the UI too."""
        # The callback has to name this server's real port: it is
        # usually 8765 but steps aside when that is taken.
        callback = f"http://127.0.0.1:{_local_port()}/auth/callback"
        url = account.start_sign_in(provider, callback)
        try:
            import webbrowser
            webbrowser.open(url)
            opened = True
        except Exception:  # noqa: BLE001
            opened = False
        return {"url": url, "opened": opened}

    @app.get("/auth/callback", include_in_schema=False)
    def auth_callback(
        token: str | None = Query(None),
        nonce: str = Query(""),
        error: str | None = Query(None),
    ):
        """Where the browser lands after the provider signs the user in.

        Registered before the SPA catch-all below, which would otherwise
        swallow it and hand back index.html.
        """
        from fastapi.responses import HTMLResponse

        if error or not token:
            return HTMLResponse(
                _auth_result_page(False, error or "Sign-in was cancelled."),
                status_code=400,
            )
        try:
            acct = account.accept_callback(nonce, token)
        except ValueError as exc:
            # Either a replayed callback or one this app never started.
            return HTMLResponse(_auth_result_page(False, str(exc)), status_code=400)
        return HTMLResponse(
            _auth_result_page(True, acct.display_name or "You are signed in.")
        )

    @app.post("/api/auth/logout", status_code=204)
    def auth_logout() -> None:
        account.sign_out()

    @app.get("/api/auth/claim-candidates")
    def claim_candidates() -> dict[str, Any]:
        """Local MTGO accounts, and whether each can still be claimed.

        The app only ever offers usernames it can actually see in the
        MTGO folder on this machine — that is what makes the claim mean
        something more than typing a name into a box.
        """
        acct = account.load()
        if not acct.signed_in:
            raise HTTPException(401, "sign in first")

        out = []
        for row in accounts():
            name = row["user"]
            status: dict[str, Any] = {
                "mtgo_username": name,
                "matches": row.get("matches", 0),
                "last_played": row.get("last_played"),
                "claimed_by_you": name in acct.claimed_usernames,
            }
            if not status["claimed_by_you"]:
                try:
                    avail = account.availability(name)
                    status["claimable"] = bool(avail.get("claimable"))
                except Exception:  # noqa: BLE001 - offline: let them try
                    status["claimable"] = True
                    status["unknown"] = True
            else:
                status["claimable"] = False
            out.append(status)

        out.sort(key=lambda r: -(r.get("matches") or 0))
        return {"candidates": out}

    @app.post("/api/auth/claim")
    def claim(mtgo_username: str = Query(...)) -> dict[str, Any]:
        """Link one MTGO username to the signed-in account."""
        from mtgo_meta.upload.state import load_or_create_state
        try:
            install_id, _salt = load_or_create_state()
        except Exception:  # noqa: BLE001
            install_id = None
        try:
            return account.claim_player(mtgo_username, str(install_id) if install_id else None)
        except PermissionError as exc:
            raise HTTPException(401, str(exc)) from exc
        except urllib.error.HTTPError as exc:
            detail = "could not claim this account"
            try:
                detail = json.loads(exc.read()).get("detail", detail)
            except Exception:  # noqa: BLE001
                pass
            raise HTTPException(exc.code, detail) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"could not reach the server: {exc}") from exc

    @app.post("/api/auth/sync-decks")
    def sync_decks() -> dict[str, Any]:
        """Push the saved decks to the server for the website."""
        try:
            return _push_decks()
        except PermissionError as exc:
            raise HTTPException(401, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"could not reach the server: {exc}") from exc

    def _push_decks() -> dict[str, Any]:
        """Hand the server the lists worth keeping: played, or current.

        The server deletes whatever a deck upload leaves out, so this is
        also how never-played saves leave the website.
        """
        decks = _saved_decks(force=True)
        if not decks:
            # An unreadable deck folder must not read as every deck deleted.
            return {"stored": 0, "removed": 0}
        versions = _versions()
        with open_store(db) as st:
            keep = deck_history.upload_keep(st.registered_decks(), decks)
        return account.upload_decks(
            deck_history.upload_payload(versions, keep=keep)
        )

    # router owns every non-/api route, so we serve index.html as the
    # fallback for anything not found in /assets/.
    if WEB_DIST.exists() and (WEB_DIST / "index.html").exists():
        from fastapi.responses import FileResponse

        # Asset filenames carry a content hash, so a given URL's bytes
        # can never change. Caching them forever is both safe and the
        # whole point of hashing them.
        app.mount(
            "/assets",
            StaticFiles(directory=str(WEB_DIST / "assets")),
            name="web-assets",
        )

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa_fallback(full_path: str):  # noqa: ARG001
            # index.html must never be cached. It is the only file whose
            # URL stays the same while its contents change, so a cached
            # copy pins the app to an old set of asset hashes — which
            # are themselves cached, and still on disk in the old
            # install. The result is an app that updates its backend and
            # goes on rendering the previous release's interface, with
            # no error anywhere to explain it. That happened.
            return FileResponse(
                WEB_DIST / "index.html",
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate",
                    "Pragma": "no-cache",
                },
            )

    return app


# Convenience for `uvicorn mtgo_meta.api.app:app`.
app = create_app()
