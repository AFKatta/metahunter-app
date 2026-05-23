"""Deep-dive a single match: cards both sides observed, what they cast,
inferred colour identity, and the top corpus candidates the classifier
scored — so you can see WHY a deck got the label it got.

Defaults to the most-recent match the primary user played; pass a
match_id (or a prefix) to inspect any specific one.

Usage:
    python scripts/inspect_match.py                       # latest match
    python scripts/inspect_match.py 67007274              # by prefix
    python scripts/inspect_match.py --opponent kozz27     # latest vs them
    python scripts/inspect_match.py --user Samu_27        # different account
    python scripts/inspect_match.py --candidates 10       # show top 10 fits
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from metahunter_core.classifier import (
    build_card_colors,
    build_card_weights,
    classify_by_similarity,
    infer_color_identity,
    recompute_deck_color_identity,
    _deck_unique_cards,
)
from mtgo_meta.paths import corpus_path, default_db_path
from mtgo_meta.store import open_store


def _primary_user(rows) -> str | None:
    c: Counter[str] = Counter()
    for r in rows:
        for p in r.players:
            c[p] += 1
    return c.most_common(1)[0][0] if c else None


def _print_player_section(
    label: str,
    player: str,
    cards: list[str],
    cast: list[str],
    decks: list[dict],
    weights: dict,
    card_colors: dict,
    top_n: int,
) -> None:
    print(f"  -- {label}: {player} --")
    if not cards:
        print("     (no cards observed)")
        print()
        return
    obs = set(cards)
    counts = Counter(cards)
    cast_set = set(cast)
    colours = infer_color_identity(cards, card_colors, cast)
    label_name, score, _best = classify_by_similarity(
        cards, decks, weights, card_colors, cast_cards=cast
    )
    print(f"     classified as : {label_name}  (score {score:.1f})")
    print(f"     inferred colours: {colours or '(none)'}")
    print(f"     cards observed ({len(obs)} unique, {len(cards)} total):")
    for c, n in counts.most_common():
        marker = "*" if c in cast_set else " "
        print(f"       {marker} {n:>3}  {c}")
    print("     (* = card was cast / flashbacked / cycled, counts toward colour identity)")

    # Top corpus candidates by raw overlap score
    scored = []
    for d in decks:
        dunique = _deck_unique_cards(d)
        overlap = obs & dunique
        if not overlap:
            continue
        s = sum(weights.get(c, 1.0) for c in overlap)
        scored.append((s, d, overlap))
    scored.sort(key=lambda x: -x[0])
    if scored:
        print(f"     top {top_n} corpus candidates (highest overlap):")
        for s, d, overlap in scored[:top_n]:
            ci = d.get("color_identity", "")
            print(
                f"       {s:6.1f}  {d.get('archetype', '?'):30}"
                f"  (ci={ci})  ({d.get('player', '?')})"
            )
            sample = sorted(overlap)[:8]
            tail = " …" if len(overlap) > 8 else ""
            print(f"              shared ({len(overlap)}): {', '.join(sample)}{tail}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("match_id", nargs="?", default=None,
                    help="Match ID (or any unique prefix). Default: latest match.")
    ap.add_argument("--user", type=str, default=None,
                    help="Filter to matches involving this account (default: primary).")
    ap.add_argument("--opponent", type=str, default=None,
                    help="Pick the latest match against this opponent (substring).")
    ap.add_argument("--candidates", type=int, default=5,
                    help="How many top corpus candidates to show per player (default 5).")
    ap.add_argument("--db", type=Path, default=None)
    args = ap.parse_args()

    db = args.db or default_db_path()
    cp = corpus_path("Legacy")
    if not cp.exists():
        print(f"No corpus found at {cp}", file=sys.stderr)
        return 1
    corpus = json.loads(cp.read_text(encoding="utf-8"))
    decks = corpus.get("decks", [])
    for d in decks:
        d["color_identity"] = recompute_deck_color_identity(d)
    weights = build_card_weights(decks)
    card_colors = build_card_colors(decks)

    with open_store(db) as store:
        rows = list(store.iter_matches())

    user = args.user or _primary_user(rows)
    if user is None:
        print("No matches in DB.", file=sys.stderr)
        return 1

    # Match selection.
    candidates = [r for r in rows if user in r.players]
    if args.opponent:
        oq = args.opponent.lower()
        candidates = [
            r for r in candidates
            if any(oq in p.lower() for p in r.players if p != user)
        ]
    if args.match_id:
        prefix = args.match_id.lower()
        candidates = [r for r in candidates if r.match_id.lower().startswith(prefix)]
    if not candidates:
        print("No matching matches found.", file=sys.stderr)
        return 1
    candidates.sort(key=lambda r: r.log_mtime or 0, reverse=True)
    m = candidates[0]

    opp = next((p for p in m.players if p != user), "?")
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m.log_mtime or 0))
    score = (
        f"{m.score_won}-{m.score_lost}"
        if m.score_won is not None else "?"
    )
    print(f"DB: {db}")
    print(f"Corpus: {cp}  ({len(decks)} decks)")
    print()
    print("=" * 78)
    print(f"  {when}  [{m.format or 'Legacy'}]")
    print(f"  {user}  vs  {opp}")
    print(f"  winner   : {m.match_winner or '(undecided)'}  ({score})")
    print(f"  turns    : {m.turns}")
    print(f"  match_id : {m.match_id}")
    print(f"  log file : {m.log_path}")
    print()

    _print_player_section(
        "YOU", user,
        m.cards_by_player.get(user, []),
        m.cards_cast_by_player.get(user, []),
        decks, weights, card_colors, args.candidates,
    )
    _print_player_section(
        "OPPONENT", opp,
        m.cards_by_player.get(opp, []),
        m.cards_cast_by_player.get(opp, []),
        decks, weights, card_colors, args.candidates,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
