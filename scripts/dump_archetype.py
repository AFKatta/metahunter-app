"""Dump diagnostic detail for every match the classifier labelled as
the given archetype(s). For each match shows:

  * the player's observed cards (with counts)
  * the inferred colour identity
  * the top 5 corpus candidates with scores
  * the score of a comparison archetype (default Grixis Control for
    user side, configurable via --compare)

By default the script dumps matches where YOU were classified as the
target. Pass ``--side opponent`` to dump matches where the OPPONENT
was classified as the target — useful for verifying "why is every
match against deck X getting labelled Y?"

Usage:
    # Your matches you were classified as Sultai Tempo
    python scripts/dump_archetype.py "Sultai Tempo"

    # Opponent matches classified as Blue Artifacts
    python scripts/dump_archetype.py "Blue Artifacts" --side opponent

    # Multiple targets / longer window
    python scripts/dump_archetype.py "Azorius Control" "Dimir Tempo" 60

    # Compare against a different deck
    python scripts/dump_archetype.py "Sultai Tempo" --compare "Grixis Control"
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import json
from mtgo_meta.classifier import (
    build_card_colors,
    build_card_weights,
    classify_by_similarity,
    infer_color_identity,
    recompute_deck_color_identity,
)
from mtgo_meta.paths import corpus_path, default_db_path
from mtgo_meta.store import open_store


def deck_cards(deck: dict) -> set[str]:
    out: set[str] = set()
    for e in deck.get("main_deck", []):
        if len(e) >= 2:
            out.add(e[1])
    for e in deck.get("sideboard", []):
        if len(e) >= 2:
            out.add(e[1])
    out.discard("")
    return out


def main() -> int:
    targets: list[str] = []
    compare: list[str] = ["Grixis Control"]
    days = 30
    db_override: Path | None = None
    side: str = "you"   # "you" or "opponent"
    args = sys.argv[1:]
    while args:
        a = args.pop(0)
        if a == "--compare" and args:
            compare = [args.pop(0)]
        elif a == "--db" and args:
            db_override = Path(args.pop(0))
        elif a == "--side" and args:
            side = args.pop(0).lower()
            if side not in ("you", "opponent"):
                print(f"--side must be 'you' or 'opponent', got: {side!r}",
                      file=sys.stderr)
                return 2
        elif a.isdigit():
            days = int(a)
        else:
            targets.append(a)
    if not targets:
        print("Usage: dump_archetype.py <archetype-name> [more names] "
              "[days] [--side you|opponent] [--compare X] [--db <path>]",
              file=sys.stderr)
        return 2

    cp = corpus_path("Legacy")
    corpus = json.loads(cp.read_text(encoding="utf-8"))
    decks = corpus.get("decks", [])
    for d in decks:
        d["color_identity"] = recompute_deck_color_identity(d)
    weights = build_card_weights(decks)
    card_colors = build_card_colors(decks)
    arch_counts = Counter(d.get("archetype", "") for d in decks)
    kept_arch = {a for a, _ in arch_counts.most_common(80)}

    cutoff = time.time() - days * 86400
    db = db_override or default_db_path()
    print(f"Using DB: {db}")
    with open_store(db) as store:
        matches = store.iter_matches(since_mtime=cutoff)

    user_counter: Counter[str] = Counter()
    for m in matches:
        for p in m.players:
            user_counter[p] += 1
    user = user_counter.most_common(1)[0][0] if user_counter else None
    if not user:
        print("No user inferred."); return 0
    print(f"User: {user}  |  Window: last {days} days  |  "
          f"Side: {side}  |  Targets: {targets}\n")

    compare_set = set(compare)

    # Classify the requested side of every match, pick the ones that
    # land on a target archetype.
    hits = []
    for m in matches:
        if user not in m.players:
            continue
        opp = next((p for p in m.players if p != user), None)
        if not opp:
            continue
        target_player = user if side == "you" else opp
        cards = m.cards_by_player.get(target_player, [])
        cast = m.cards_cast_by_player.get(target_player, [])
        name, score, _ = classify_by_similarity(
            cards, decks, weights, card_colors, cast_cards=cast
        )
        if name in targets:
            hits.append((m, opp, cards, cast, name, score, target_player))

    if not hits:
        print(f"No matches found with {side}-side classified as: {targets}")
        return 0

    for idx, (m, opp, cards, cast, label, won_score, target_player) in enumerate(hits, 1):
        obs = set(cards)
        counts = Counter(cards)
        observed_colors = set(infer_color_identity(cards, card_colors, cast))
        date_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.log_mtime or 0))
        other = opp if target_player == user else user

        print("=" * 78)
        print(f"[{idx}/{len(hits)}] {m.match_id[:8]}  {date_str}  "
              f"{target_player} vs {other}  ->{m.match_winner or '?'}")
        print(f"   labelled ({target_player}): {label} (score {won_score:.1f})")
        print(f"   inferred colours: "
              f"{''.join(sorted(observed_colors, key='WUBRG'.index)) or '(none)'}")
        print(f"   cards observed ({len(obs)} unique, {len(cards)} total):")
        for c, n in counts.most_common():
            print(f"     {n:>3}  {c}")

        # Score every corpus deck.
        scored = []
        for d in decks:
            d_unique = deck_cards(d)
            overlap = obs & d_unique
            if not overlap:
                continue
            s = sum(weights.get(c, 1.0) for c in overlap)
            scored.append((s, d, overlap))
        scored.sort(key=lambda x: -x[0])

        passing = [t for t in scored if t[1].get("archetype") in kept_arch]
        print(f"\n   top 5 surviving candidates (passed all gates):")
        for s, d, overlap in passing[:5]:
            arch = d.get("archetype", "?")
            marker = " <-- winner" if arch == label and abs(s - won_score) < 0.01 else ""
            print(f"     {s:6.1f}  {arch}  ({d.get('player','?')}){marker}")
            print(f"            shared ({len(overlap)}): "
                  f"{', '.join(sorted(overlap)[:10])}"
                  f"{' ...' if len(overlap) > 10 else ''}")

        # Comparison archetype panel — same side as the target.
        comp_top = [t for t in scored if t[1].get("archetype") in compare_set][:3]
        if comp_top:
            print(f"\n   for comparison, top {compare[0]} candidates:")
            for s, d, overlap in comp_top:
                lag = ""
                if s < won_score:
                    lag = f" (LOST by {won_score - s:.1f})"
                print(f"     {s:6.1f}  {d.get('archetype','?')}  "
                      f"({d.get('player','?')}){lag}")
                comp_cards = deck_cards(d)
                missing = comp_cards - obs
                disc = [c for c in sorted(missing) if weights.get(c, 0) >= 3.0][:6]
                if disc:
                    print(f"            DIDN'T cast (high-signal): "
                          f"{', '.join(disc)}")
                shared = obs & comp_cards
                ovl = [c for c in sorted(shared) if weights.get(c, 0) >= 3.0][:6]
                if ovl:
                    print(f"            DID cast (high-signal):    "
                          f"{', '.join(ovl)}")
        else:
            print(f"\n   no {compare[0]} candidates had any overlap.")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
