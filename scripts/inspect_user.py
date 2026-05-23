"""Dump every match for a given MTGO username, with the path to the
raw .dat log file so you can open the source data directly.

Useful when the dashboard shows something surprising for an account
and you want to verify against the actual MTGO game log.

Usage:
    python scripts/inspect_user.py Samu_27
    python scripts/inspect_user.py Samu_27 --limit 5
    python scripts/inspect_user.py Samu_27 --since 2025-10-01
    python scripts/inspect_user.py Samu_27 --opponent Frengo1990
    python scripts/inspect_user.py Samu_27 --cards            # also dump card bags
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mtgo_meta.classifier import (
    build_card_colors,
    build_card_weights,
    classify_by_similarity,
    recompute_deck_color_identity,
)
from mtgo_meta.paths import corpus_path, default_db_path
from mtgo_meta.store import open_store

import json


def _parse_date(s: str) -> float:
    """Accept YYYY-MM-DD; return Unix epoch seconds (UTC midnight)."""
    return time.mktime(time.strptime(s, "%Y-%m-%d"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("user", help="MTGO username to inspect.")
    ap.add_argument("--limit", type=int, default=20,
                    help="How many matches to show (most-recent first). 0 = all.")
    ap.add_argument("--since", type=str, default=None,
                    help="Only matches on/after this YYYY-MM-DD.")
    ap.add_argument("--until", type=str, default=None,
                    help="Only matches on/before this YYYY-MM-DD.")
    ap.add_argument("--opponent", type=str, default=None,
                    help="Only matches against this opponent (substring).")
    ap.add_argument("--format", dest="fmt", type=str, default=None,
                    help="Only matches in this format (Legacy / Vintage / ...).")
    ap.add_argument("--cards", action="store_true",
                    help="Also dump each match's card bags.")
    ap.add_argument("--classify", action="store_true",
                    help="Show the classifier's deck label for both players.")
    ap.add_argument("--db", type=Path, default=None,
                    help="Path to mtgo-meta.sqlite (defaults to user-data location).")
    args = ap.parse_args()

    # Load corpus lazily only when --classify is requested. Saves
    # ~500ms when you just want the list of matches.
    decks_corpus: list[dict] = []
    weights: dict = {}
    card_colors: dict = {}
    if args.classify:
        cp = corpus_path("Legacy")
        if cp.exists():
            decks_corpus = json.loads(cp.read_text(encoding="utf-8")).get("decks", [])
            for d in decks_corpus:
                d["color_identity"] = recompute_deck_color_identity(d)
            weights = build_card_weights(decks_corpus)
            card_colors = build_card_colors(decks_corpus)
        else:
            print(f"  (no corpus at {cp}; --classify will produce colour-code labels)")

    db = args.db or default_db_path()
    print(f"DB: {db}")

    with open_store(db) as store:
        rows = store.iter_matches()

    # Filter to matches involving the requested user.
    matched = [r for r in rows if args.user in r.players]

    # Apply optional filters.
    if args.since:
        floor = _parse_date(args.since)
        matched = [r for r in matched if (r.log_mtime or 0) >= floor]
    if args.until:
        ceil = _parse_date(args.until) + 86400  # inclusive end-of-day
        matched = [r for r in matched if (r.log_mtime or 0) <= ceil]
    if args.opponent:
        oq = args.opponent.lower()
        matched = [
            r for r in matched
            if any(oq in p.lower() for p in r.players if p != args.user)
        ]
    if args.fmt:
        matched = [r for r in matched if (r.format or "Legacy") == args.fmt]

    matched.sort(key=lambda r: r.log_mtime or 0, reverse=True)

    print(f"\nFound {len(matched)} matches involving {args.user!r}.")
    if args.since or args.until or args.opponent or args.fmt:
        print(
            f"  filters:"
            + (f" since={args.since}" if args.since else "")
            + (f" until={args.until}" if args.until else "")
            + (f" opponent~={args.opponent}" if args.opponent else "")
            + (f" format={args.fmt}" if args.fmt else "")
        )
    print()

    # Summary stats.
    wins = sum(1 for r in matched if r.match_winner == args.user)
    losses = sum(
        1 for r in matched
        if r.match_winner and r.match_winner != args.user
    )
    undecided = sum(1 for r in matched if not r.match_winner)
    by_format: Counter[str] = Counter((r.format or "Legacy") for r in matched)
    by_opp: Counter[str] = Counter(
        p for r in matched for p in r.players if p != args.user
    )
    print(f"Record: {wins}-{losses}  ({undecided} undecided)")
    print(f"By format: {dict(by_format)}")
    print(f"Top 5 opponents: {by_opp.most_common(5)}")
    print()

    limit = args.limit if args.limit > 0 else len(matched)
    print(f"Showing the {min(limit, len(matched))} most-recent matches:\n")

    def _label(cards: list[str], cast: list[str]) -> str:
        if not args.classify:
            return ""
        name, _s, _d = classify_by_similarity(
            cards, decks_corpus, weights, card_colors, cast_cards=cast
        )
        return name

    for r in matched[:limit]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.log_mtime or 0))
        opp = next((p for p in r.players if p != args.user), "?")
        winner = r.match_winner or "(undecided)"
        score = (
            f"{r.score_won}-{r.score_lost}"
            if r.score_won is not None
            else "?"
        )
        fmt = r.format or "Legacy"
        print("=" * 78)
        print(f"  {when}  [{fmt}]  vs {opp}")
        print(f"    match_id : {r.match_id}")
        print(f"    log file : {r.log_path}")
        print(f"    winner   : {winner}  ({score})")
        print(f"    turns    : {r.turns}")
        if args.classify:
            you_lbl = _label(
                r.cards_by_player.get(args.user, []),
                r.cards_cast_by_player.get(args.user, []),
            )
            opp_lbl = _label(
                r.cards_by_player.get(opp, []),
                r.cards_cast_by_player.get(opp, []),
            )
            print(f"    you  ->  {you_lbl}")
            print(f"    them ->  {opp_lbl}")
        if args.cards:
            print(f"    cards observed:")
            for player, cards in r.cards_by_player.items():
                tally = Counter(cards).most_common(20)
                cast = r.cards_cast_by_player.get(player, [])
                print(f"      {player}  ({len(set(cards))} unique, "
                      f"{len(cards)} total, {len(set(cast))} cast)")
                for c, n in tally:
                    print(f"        {n:>3}  {c}")
        print()

    if not args.cards:
        print(
            "Tip: pass --cards to also print every match's observed bag, "
            "or open a log file directly:\n"
            "  Get-Content '<log file>' -Encoding Byte | "
            "Format-Hex | Out-Host"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
