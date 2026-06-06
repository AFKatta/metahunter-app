"""Push the local SQLite match history into a running metahunter-server.

What this does:

  1. Generates (or reuses) a per-install identity:
       * install_id   — random UUIDv4, stored in user-data-dir
       * install_salt — random 32-byte secret, stored in user-data-dir
  2. Calls POST /v1/register to make sure the server knows about us.
  3. Reads every match from the local SQLite store, builds a
     MatchUpload payload for each one, HMACs the opponent's MTGO
     username with the install salt (the user's own name stays
     plaintext because they're consenting on their own behalf).
  4. POSTs the matches in batches of N to /v1/matches.

Idempotent: the server's (match_id, install_id) unique constraint
means re-running the script just no-ops on already-uploaded matches.

Usage:
    python scripts/backfill.py
    python scripts/backfill.py --server-url http://localhost:8001
    python scripts/backfill.py --dry-run
    python scripts/backfill.py --limit 50
    python scripts/backfill.py --since 2026-05-01

Local state lives at:
    %LOCALAPPDATA%/Metahunter/upload_state.json     (install_id +
                                                     install_salt,
                                                     never sent to the
                                                     server)
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import urllib.error
import urllib.request

from metahunter_core.classifier import (
    build_card_colors, build_card_weights, classify_by_similarity,
    recompute_deck_color_identity,
)
from mtgo_meta.paths import corpus_path, default_db_path, user_data_dir
from mtgo_meta.store import open_store

# Bump this when the wire format changes so the server can pin/reject.
CLIENT_VERSION = "metahunter-app/backfill/0.1.0"

DEFAULT_SERVER_URL = "http://localhost:8001"
DEFAULT_BATCH_SIZE = 100


# ---------------------------------------------------------------------------
# Per-install identity (install_id + install_salt)
# ---------------------------------------------------------------------------

def _state_path() -> Path:
    return user_data_dir() / "upload_state.json"


def load_or_create_state() -> tuple[uuid.UUID, bytes]:
    """Return (install_id, install_salt), creating both if missing.

    The salt is 32 bytes of os.urandom, hex-encoded to JSON. It
    NEVER leaves this machine — it's used only to HMAC opponent
    usernames before they're sent up.
    """
    path = _state_path()
    if path.exists():
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
            return uuid.UUID(blob["install_id"]), bytes.fromhex(blob["install_salt"])
        except (KeyError, ValueError, json.JSONDecodeError):
            # corrupted state — fall through and regenerate
            pass
    install_id = uuid.uuid4()
    install_salt = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "install_id": str(install_id),
            "install_salt": install_salt.hex(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2),
        encoding="utf-8",
    )
    print(f"  new install identity at {path}")
    return install_id, install_salt


def hash_username(salt: bytes, username: str) -> str:
    """HMAC-SHA256 of the MTGO username, hex-encoded.

    Same name from two different installs hashes to two different
    values because of the per-install salt — so the server can't
    correlate "this opponent on install A == this opponent on
    install B" without one of those installs leaking its salt.
    """
    return hmac.new(salt, username.encode("utf-8"), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _http_post(url: str, body: dict, timeout: float = 30.0) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:
            return exc.code, {"detail": str(exc)}


# ---------------------------------------------------------------------------
# Local → wire-format conversion
# ---------------------------------------------------------------------------

def primary_user_of(store) -> str | None:
    """Most-frequently-seen MTGO username in the local store. We treat
    this as the user's own MTGO account."""
    counter: Counter[str] = Counter()
    for m in store.iter_matches():
        for p in m.players:
            counter[p] += 1
    return counter.most_common(1)[0][0] if counter else None


def build_match_upload(
    m, user: str, decks, weights, card_colors,
    install_salt: bytes, parser_version: int,
) -> dict | None:
    """Build the MatchUpload dict for a single local match.

    Returns None if the match is too thin to classify, missing a
    winner, or otherwise drop-worthy — same filters the local
    dashboard already applies."""
    if user not in m.players:
        return None
    opp = next((p for p in m.players if p != user), None)
    if opp is None or not m.match_winner:
        return None

    you_cards = m.cards_by_player.get(user, [])
    opp_cards = m.cards_by_player.get(opp, [])
    you_cast = m.cards_cast_by_player.get(user, [])
    opp_cast = m.cards_cast_by_player.get(opp, [])

    # Classify both sides locally — we send the label up, the server
    # stores it as-is for now.
    you_label, _, _ = classify_by_similarity(
        you_cards, decks, weights, card_colors, cast_cards=you_cast,
    )
    opp_label, _, _ = classify_by_similarity(
        opp_cards, decks, weights, card_colors, cast_cards=opp_cast,
    )

    you_won = m.match_winner == user
    you_games = sum(1 for g in m.games if g.get("winner") == user)
    opp_games = sum(1 for g in m.games if g.get("winner") == opp)

    return {
        "match_id": m.match_id,
        "format": m.format or "Legacy",
        "log_mtime": datetime.fromtimestamp(m.log_mtime or 0, timezone.utc).isoformat(),
        "turns": m.turns or 0,
        "you": {
            "username": user,
            "deck": you_label,
            "deck_colors": "",
            "on_play": (m.first_player == user) if m.first_player else None,
            "won": you_won,
            "games_won": you_games,
            "cards_observed": you_cards,
            "cards_cast": you_cast,
        },
        "opponent": {
            "username_hash": hash_username(install_salt, opp),
            "deck": opp_label,
            "deck_colors": "",
            "on_play": (m.first_player == opp) if m.first_player else None,
            "won": not you_won,
            "games_won": opp_games,
            "cards_observed": opp_cards,
            "cards_cast": opp_cast,
        },
        "client_version": CLIENT_VERSION,
        "parser_version": parser_version,
    }


def cards_cast_by_player(m):
    """Some StoredMatch variants don't expose cards_cast_by_player as
    a property — fall back to JSON read."""
    return m.cards_cast_by_player if hasattr(m, "cards_cast_by_player") else {}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--server-url", default=os.environ.get("METAHUNTER_SERVER_URL", DEFAULT_SERVER_URL))
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    ap.add_argument("--limit", type=int, default=0, help="0 = all matches")
    ap.add_argument("--since", type=str, help="YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true",
                    help="build payloads + classify, but don't POST")
    args = ap.parse_args()

    print("Metahunter — backfill")
    print(f"  server URL  : {args.server_url}")
    print(f"  batch size  : {args.batch_size}")
    print()

    install_id, install_salt = load_or_create_state()
    print(f"  install_id  : {install_id}")
    print(f"  salt        : (32 bytes, never leaves this machine)")
    print()

    # Load corpus for local classification.
    cp = corpus_path("Legacy")
    if not cp.exists():
        print(f"ERROR: no Legacy corpus at {cp}", file=sys.stderr)
        return 1
    corpus = json.loads(cp.read_text(encoding="utf-8"))
    decks = corpus.get("decks", [])
    for d in decks:
        d["color_identity"] = recompute_deck_color_identity(d)
    weights = build_card_weights(decks)
    card_colors = build_card_colors(decks)
    print(f"  corpus      : {len(decks)} decks ({cp.name})")

    # Open store.
    with open_store(default_db_path()) as store:
        rows = store.iter_matches()
        parser_version_str = store.get_meta("parser_version", "0")
        try:
            parser_version = int(parser_version_str)
        except ValueError:
            parser_version = 0
    print(f"  DB matches  : {len(rows)}")
    print(f"  parser ver  : {parser_version}")

    # Pick primary user from the full store.
    user_counter: Counter[str] = Counter()
    for m in rows:
        for p in m.players:
            user_counter[p] += 1
    user = user_counter.most_common(1)[0][0] if user_counter else None
    if user is None:
        print("ERROR: no matches found — nothing to upload", file=sys.stderr)
        return 1
    print(f"  primary user: {user}")

    # Apply optional --since filter.
    if args.since:
        floor = time.mktime(time.strptime(args.since, "%Y-%m-%d"))
        rows = [m for m in rows if (m.log_mtime or 0) >= floor]
        print(f"  after --since {args.since}: {len(rows)} matches")

    if args.limit > 0:
        rows = rows[: args.limit]
        print(f"  after --limit {args.limit}: {len(rows)} matches")

    print()

    # 1) Register install.
    if not args.dry_run:
        print(f"-> POST {args.server_url}/v1/register")
        status, body = _http_post(f"{args.server_url}/v1/register", {
            "install_id": str(install_id),
            "leaderboard_opt_in": True,
            "consent_at": datetime.now(timezone.utc).isoformat(),
            "client_version": CLIENT_VERSION,
            "parser_version": parser_version,
        })
        print(f"   {status}: {body}")
        if status >= 400:
            print("ERROR: register failed", file=sys.stderr)
            return 1
    else:
        print("(dry-run: skipping register)")

    # 2) Build payloads + ship in batches.
    print()
    print(f"-> POST {args.server_url}/v1/matches (batches of {args.batch_size})")
    batch: list[dict] = []
    sent = accepted = new = merged = skipped = errors = 0
    t0 = time.time()

    def flush():
        nonlocal sent, accepted, new, merged, errors
        if not batch:
            return
        if args.dry_run:
            sent += len(batch)
            batch.clear()
            return
        status, body = _http_post(f"{args.server_url}/v1/matches", {
            "install_id": str(install_id),
            "matches": batch,
        })
        if status >= 400:
            print(f"  ! batch failed {status}: {body}")
            errors += len(batch)
        else:
            sent += body.get("received", 0)
            accepted += body.get("accepted", 0)
            new += body.get("new_matches", 0)
            merged += body.get("merged_matches", 0)
            errs = body.get("errors", [])
            if errs:
                errors += len(errs)
                for e in errs[:3]:
                    print(f"  ! match {e.get('match_id', '?')[:8]}: {e.get('error')}")
        batch.clear()

    for i, m in enumerate(rows, 1):
        payload = build_match_upload(
            m, user, decks, weights, card_colors,
            install_salt, parser_version,
        )
        if payload is None:
            skipped += 1
            continue
        batch.append(payload)
        if len(batch) >= args.batch_size:
            flush()
            print(f"  ... {i}/{len(rows)} processed (sent={sent}, new={new}, merged={merged})")
    flush()

    dt = time.time() - t0
    print()
    print(f"done in {dt:.1f}s")
    print(f"  scanned : {len(rows)}")
    print(f"  skipped : {skipped} (no winner / not user's match / too thin)")
    print(f"  sent    : {sent}")
    print(f"  new     : {new} canonical matches created")
    print(f"  merged  : {merged} matches enriched by cross-install merge")
    print(f"  errors  : {errors}")
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
