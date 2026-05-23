"""Diagnose how a single match was classified.

Usage:
    python scripts/diagnose_match.py <match_guid>

Shows the observed cards per player, the top corpus matches with scores,
and which side ended up being labeled what.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mtgo_meta.classifier import (
    build_card_weights,
    classify_by_similarity,
    load_legacy,
)
from mtgo_meta.config import find_mtgo_appfiles_dir
from mtgo_meta.parser import parse_game_log

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_PATH = REPO_ROOT / "data" / "corpus" / "legacy.json"


def top_matches(
    cards: list[str], decks: list[dict], weights: dict[str, float], k: int = 5
) -> list[tuple[float, dict]]:
    obs = set(cards)
    scored = []
    for d in decks:
        deck_unique = {e[1] for e in d["main_deck"]} | {e[1] for e in d["sideboard"]}
        overlap = obs & deck_unique
        if not overlap:
            continue
        score = sum(weights.get(c, 1.0) for c in overlap)
        scored.append((score, overlap, d))
    scored.sort(key=lambda x: -x[0])
    return scored[:k]


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: diagnose_match.py <match_guid>", file=sys.stderr)
        return 2
    guid = sys.argv[1]

    folder = find_mtgo_appfiles_dir()
    if not folder:
        print("Could not locate MTGO AppFiles folder.", file=sys.stderr)
        return 1
    matches = list(folder.glob(f"Match_GameLog_{guid}*.dat"))
    if not matches:
        print(f"No log file matching {guid}", file=sys.stderr)
        return 1
    pm = parse_game_log(matches[0])

    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    decks = corpus["decks"]
    weights = build_card_weights(decks)

    print(f"Match: {pm.match_id}")
    print(f"Players: {pm.players}  |  First: {pm.first_player}  |  Turns: {pm.turns}")
    print(f"Score: {pm.match_score}  |  Winner: {pm.match_winner}")
    print()

    for player in pm.players:
        cards = pm.cards_by_player.get(player, [])
        unique = sorted(set(cards))
        counts = Counter(cards)
        print(f"================================================================")
        print(f"{player}  —  {len(cards)} card observations, {len(unique)} unique")
        print(f"================================================================")
        print("All cards observed (count × name):")
        for name, n in counts.most_common():
            print(f"  {n:>3}  {name}")
        print()

        print("Top 5 corpus deck matches:")
        for score, overlap, d in top_matches(cards, decks, weights):
            print(
                f"  score={score:6.1f}  archetype={d['archetype']!r}  "
                f"player={d.get('player','?')}  date={d.get('event_date','?')}"
            )
            print(f"     overlap ({len(overlap)} cards): {sorted(overlap)}")
        print()

        chosen, score, deck = classify_by_similarity(cards, decks, weights)
        print(f"  CHOSEN: {chosen!r}  (score={score:.1f})")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
