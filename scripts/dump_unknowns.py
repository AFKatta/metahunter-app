"""For every match the classifier labelled "Unknown", dump:
  - all cards observed for the user, with counts
  - top corpus-deck candidates with score
  - which gate each candidate failed (signature card / colour / support)

Designed so the user can scan for patterns in their own brews.

Usage:
    python scripts/dump_unknowns.py              # last 30 days
    python scripts/dump_unknowns.py 60           # last 60 days
    python scripts/dump_unknowns.py 30 --csv unknowns.csv
"""

from __future__ import annotations

import csv
import json
import sys
import time
from collections import Counter
from math import log
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from metahunter_core.classifier import (
    build_card_colors,
    build_card_weights,
    classify_by_similarity,
    infer_color_identity,
)
from mtgo_meta.store import open_store

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "mtgo-meta.sqlite"
CORPUS_PATH = REPO_ROOT / "data" / "corpus" / "legacy.json"


# Replicates the classifier's veto logic but reports WHICH gate fails.
# Keep this in sync with classifier.py when you change the rules there.
SIGNATURE_REQUIREMENTS: dict[str, list[list[str]]] = {
    "Omni-Tell": [["Show and Tell"], ["Omniscience"]],
    "Sneak and Show": [["Show and Tell"], ["Sneak Attack"]],
    "Sneak and Show with Reanimate": [["Show and Tell"], ["Reanimate", "Animate Dead", "Exhume"]],
    "Show and Tell": [["Show and Tell"]],
    "Doomsday": [["Doomsday"]],
    "Painter": [["Painter's Servant"]],
    "Aluren": [["Aluren"]],
    "Cradle Control": [["Gaea's Cradle"]],
    "Cephalid Breakfast": [["Cephalid Illusionist"]],
    "Worldgorger Combo": [["Worldgorger Dragon"]],
    "Affinity Stompy": [["Arcbound Ravager"]],
    "Lands": [["Life from the Loam"]],
    "LED Dredge": [["Lion's Eye Diamond"], ["Bridge from Below", "Narcomoeba", "Ichorid"]],
    "Necrodominance Combo": [["Necrodominance"]],
    "The EPIC Storm": [["Burning Wish"], ["Lion's Eye Diamond"]],
    "Ad Nauseam Tendrils": [["Ad Nauseam"]],
    "Hogaak": [["Hogaak, Arisen Necropolis"]],
    "Stiflenought": [["Phyrexian Dreadnought"]],
    "Mystic Forge Combo": [["Mystic Forge"]],
    "Ruby Storm": [["Ruby Medallion"]],
    "Echo of Eons": [["Echo of Eons"]],
    "Infect": [["Glistener Elf"]],
    "Belcher": [["Goblin Charbelcher"]],
    "Monastery Mentor": [["Monastery Mentor"]],
    "Delver": [["Dragon's Rage Channeler", "Delver of Secrets"]],
    "Death's Shadow": [["Death's Shadow"]],
    "Reanimator": [["Reanimate", "Animate Dead", "Exhume", "Goryo's Vengeance"]],
    "Dimir Reanimator": [["Reanimate", "Animate Dead", "Exhume"]],
    "Initiative": [["Seasoned Dungeoneer", "White Plume Adventurer", "Caves of Chaos Adventurer"]],
    "Boros Initiative": [["Seasoned Dungeoneer", "White Plume Adventurer", "Caves of Chaos Adventurer"]],
    "Naya Initiative": [["Seasoned Dungeoneer", "White Plume Adventurer", "Caves of Chaos Adventurer"]],
    "Tron": [["Urza's Mine", "Urza's Tower", "Urza's Power Plant"]],
    "Eldrazi": [["Thought-Knot Seer", "Reality Smasher", "Eldrazi Mimic", "Eldrazi Temple", "Endless One", "Matter Reshaper"]],
    "Storm": [["Past in Flames", "Tendrils of Agony", "Cabal Ritual"]],
    "Cradle": [["Gaea's Cradle"]],
    "Goblins": [["Goblin Lackey", "Goblin Recruiter", "Goblin Charbelcher", "Goblin Piledriver", "Goblin Guide"]],
    "Maverick": [["Knight of the Reliquary"]],
    "Merfolk": [["Lord of Atlantis", "Master of the Pearl Trident", "Master of Waves"]],
    "Burn": [["Lava Spike", "Eidolon of the Great Revel", "Goblin Guide"]],
    "Nic Fit": [["Veteran Explorer"]],
    "Smallpox": [["Smallpox"]],
    "Loam Pox": [["Smallpox"], ["Life from the Loam"]],
    "Ninjas": [["Ninja of the Deep Hours", "Moon-Circuit Hacker", "Ingenious Infiltrator", "Throat Slitter"]],
    "Dimir Ninjas": [["Ninja of the Deep Hours", "Moon-Circuit Hacker", "Ingenious Infiltrator"]],
    "Selesnya Depths": [["Dark Depths"], ["Thespian's Stage"]],
    "Sultai Depths": [["Dark Depths"], ["Thespian's Stage"]],
    "4c Depths": [["Dark Depths"], ["Thespian's Stage"]],
    "Depths": [["Dark Depths"], ["Thespian's Stage"]],
    "Stoneblade": [["Stoneforge Mystic"]],
    "Azorius Stoneblade": [["Stoneforge Mystic"]],
    "Orzhov Stoneblade": [["Stoneforge Mystic"]],
    "Esper Stoneblade": [["Stoneforge Mystic"]],
    "Death & Taxes": [["Aether Vial", "Thalia, Guardian of Thraben"]],
    "Zenith Combo": [["Green Sun's Zenith", "Natural Order"]],
    "Zenith": [["Green Sun's Zenith", "Natural Order"]],
    "Landfall": [["Lotus Cobra", "Scute Swarm", "Omnath, Locus of Creation", "Felidar Retreat"]],
    "Golgari Landfall": [["Lotus Cobra", "Scute Swarm", "Omnath, Locus of Creation"]],
    "Stax": [["Smokestack", "Trinisphere", "Sphere of Resistance", "Chalice of the Void"]],
    "Mono-White Stax": [["Smokestack", "Trinisphere"]],
}


def signature_check(name: str, obs: set[str]) -> str | None:
    """Return name of a missing required card, or None if all satisfied."""
    for arch_name, groups in SIGNATURE_REQUIREMENTS.items():
        if arch_name in name:
            for group in groups:
                if not any(c in obs for c in group):
                    return f"need one of: {', '.join(group)}"
    return None


def deck_cards(deck: dict) -> set[str]:
    out = set()
    for e in deck.get("main_deck", []):
        if len(e) >= 2:
            out.add(e[1])
    for e in deck.get("sideboard", []):
        if len(e) >= 2:
            out.add(e[1])
    out.discard("")
    return out


def main() -> int:
    days = 30
    csv_path: Path | None = None
    args = sys.argv[1:]
    while args:
        a = args.pop(0)
        if a.isdigit():
            days = int(a)
        elif a == "--csv" and args:
            csv_path = Path(args.pop(0))
        else:
            print(f"Unknown arg: {a}", file=sys.stderr); return 2

    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    decks = corpus.get("decks", [])
    weights = build_card_weights(decks)
    card_colors = build_card_colors(decks)
    arch_counts = Counter(d.get("archetype", "") for d in decks)
    kept_arch = {a for a, _ in arch_counts.most_common(80)}

    cutoff = time.time() - days * 86400
    with open_store(DEFAULT_DB) as store:
        matches = store.iter_matches(since_mtime=cutoff)

    user_counter: Counter[str] = Counter()
    for m in matches:
        for p in m.players:
            user_counter[p] += 1
    user = user_counter.most_common(1)[0][0] if user_counter else None
    if not user:
        print("No user inferred."); return 0

    unknowns = []
    for m in matches:
        if user not in m.players:
            continue
        opp = next((p for p in m.players if p != user), None)
        if not opp:
            continue
        cards = m.cards_by_player.get(user, [])
        name, score, _ = classify_by_similarity(cards, decks, weights, card_colors)
        if name == "Unknown":
            unknowns.append((m, opp, cards, score))

    print(f"User: {user}")
    print(f"Window: last {days} days")
    print(f"Unknown matches: {len(unknowns)}\n")

    csv_rows = []

    for idx, (m, opp, cards, _score) in enumerate(unknowns, 1):
        obs = set(cards)
        observed_colors = set(infer_color_identity(cards, card_colors))
        counts = Counter(cards)
        date_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.log_mtime or 0))

        print("=" * 78)
        print(f"[{idx}/{len(unknowns)}] {m.match_id[:8]}  {date_str}  vs {opp}  ->{m.match_winner or '?'}")
        print(f"   inferred colours: {''.join(sorted(observed_colors, key='WUBRG'.index)) or '(none)'}")
        print(f"   cards observed ({len(obs)} unique, {len(cards)} total):")
        for c, n in counts.most_common():
            print(f"     {n:>3}  {c}")

        # Score every corpus deck, then explain why each top one was rejected.
        scored = []
        for d in decks:
            d_unique = deck_cards(d)
            overlap = obs & d_unique
            if not overlap:
                continue
            s = sum(weights.get(c, 1.0) for c in overlap)
            scored.append((s, d, overlap))
        scored.sort(key=lambda x: -x[0])

        print(f"\n   top 5 nearest corpus decks (and why they're rejected):")
        for s, d, overlap in scored[:5]:
            arch = d.get("archetype", "?")
            reasons = []
            if arch not in kept_arch:
                reasons.append(f"archetype outside top-80 ({arch_counts[arch]} in corpus)")
            d_colors = set(d.get("color_identity", ""))
            if observed_colors and not observed_colors.issubset(d_colors):
                missing = observed_colors - d_colors
                reasons.append(f"deck colour={''.join(sorted(d_colors, key='WUBRG'.index)) or '(none)'} lacks {''.join(sorted(missing, key='WUBRG'.index))}")
            elif observed_colors:
                extras = d_colors - observed_colors
                if len(extras) > 1:
                    reasons.append(f"deck colour={''.join(sorted(d_colors, key='WUBRG'.index))} has {len(extras)} extra colours")
            sig_miss = signature_check(arch, obs)
            if sig_miss:
                reasons.append(sig_miss)
            if "(Yorion)" in arch and "Yorion, Sky Nomad" not in obs:
                reasons.append("'(Yorion)' archetype, but Yorion not observed")
            if "Beanstalk" in arch and "Up the Beanstalk" not in obs:
                reasons.append("'Beanstalk' archetype, but Up the Beanstalk not observed")
            if not reasons:
                reasons.append("(no veto fired — score below threshold?)")
            print(f"     {s:6.1f}  {arch}  ({d.get('player','?')})")
            for r in reasons:
                print(f"            -{r}")
            print(f"            shared: {', '.join(sorted(overlap)[:10])}{' ...' if len(overlap) > 10 else ''}")

        csv_rows.append({
            "match_id": m.match_id,
            "date": date_str,
            "opponent": opp,
            "winner": m.match_winner or "",
            "inferred_colours": "".join(sorted(observed_colors, key='WUBRG'.index)),
            "cards": " | ".join(f"{n}x {c}" for c, n in counts.most_common()),
            "top_candidate": scored[0][1].get("archetype", "?") if scored else "",
            "top_candidate_score": f"{scored[0][0]:.1f}" if scored else "",
        })
        print()

    if csv_path:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()) if csv_rows else [])
            if csv_rows:
                w.writeheader()
                w.writerows(csv_rows)
        print(f"\nCSV: {csv_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
