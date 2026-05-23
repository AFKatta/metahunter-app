"""Read parsed matches from the local SQLite store and produce a
report + CSV for the last N days.

This script no longer parses files itself — it relies on ``ingest.py``
or ``watch.py`` to keep the DB current. It runs an opportunistic
incremental ingest at startup so a one-liner still works.

Usage:
    python scripts/parse_all.py              # last 30 days
    python scripts/parse_all.py 60           # last 60 days
    python scripts/parse_all.py 30 --csv out.csv
    python scripts/parse_all.py --no-ingest  # skip the auto-ingest
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mtgo_meta.classifier import (
    build_card_colors,
    build_card_weights,
    classify,
    classify_by_similarity,
    load_legacy,
)
from mtgo_meta.store import open_store

REPO_ROOT = Path(__file__).resolve().parent.parent
FORMAT_DATA = REPO_ROOT / "data" / "MTGOFormatData"
CORPUS_PATH = REPO_ROOT / "data" / "corpus" / "legacy.json"
DEFAULT_DB = REPO_ROOT / "data" / "mtgo-meta.sqlite"


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
    "The Initiative", "The Monarch",
    "Undercity",
}


def infer_user(matches) -> str | None:
    c: Counter[str] = Counter()
    for m in matches:
        for p in m.players:
            c[p] += 1
    return c.most_common(1)[0][0] if c else None


def signature(cards: list[str], top_n: int = 8) -> list[tuple[str, int]]:
    c = Counter(card for card in cards if card not in SIGNATURE_NOISE)
    return c.most_common(top_n)


def signature_str(sig: list[tuple[str, int]]) -> str:
    return " / ".join(n for n, _ in sig)


def main() -> int:
    days = 30
    csv_path: Path | None = None
    db = DEFAULT_DB
    skip_ingest = False
    args = sys.argv[1:]
    while args:
        a = args.pop(0)
        if a == "--csv" and args:
            csv_path = Path(args.pop(0))
        elif a == "--db" and args:
            db = Path(args.pop(0))
        elif a == "--no-ingest":
            skip_ingest = True
        elif a.isdigit():
            days = int(a)
        else:
            print(f"Unknown arg: {a}", file=sys.stderr)
            return 2

    if not skip_ingest:
        ingest = REPO_ROOT / "scripts" / "ingest.py"
        if ingest.exists():
            print("Auto-ingest: ", end="", flush=True)
            subprocess.run([sys.executable, str(ingest), "--db", str(db)], check=False)

    if not db.exists():
        print(f"DB missing: {db}. Run scripts/ingest.py first.", file=sys.stderr)
        return 1

    cutoff_mtime = time.time() - days * 86400

    archetypes, fallbacks = load_legacy(FORMAT_DATA)
    corpus_decks: list[dict] = []
    card_weights: dict[str, float] = {}
    card_colors: dict[str, str] = {}
    if CORPUS_PATH.exists():
        corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
        corpus_decks = corpus.get("decks", [])
        card_weights = build_card_weights(corpus_decks)
        card_colors = build_card_colors(corpus_decks)

    def _classify(cards: list[str]) -> str:
        if corpus_decks:
            name, _s, _d = classify_by_similarity(
                cards, corpus_decks, card_weights, card_colors
            )
            if name != "Unknown":
                return name
        name, _ = classify(cards, archetypes, fallbacks)
        return name

    with open_store(db) as store:
        matches = store.iter_matches(since_mtime=cutoff_mtime)
    print(f"\nLoaded {len(matches)} matches from {db} (last {days} days).")

    user = infer_user(matches)
    print(f"Inferred MTGO username: {user}")
    if user is None:
        return 0
    print(
        f"Loaded {len(archetypes)} archetype rules + {len(fallbacks)} fallbacks "
        f"+ {len(corpus_decks)} corpus decks.\n"
    )

    rows: list[dict] = []
    wins = losses = games_w = games_l = 0
    your_arch_results: dict[str, list[str]] = defaultdict(list)
    opp_arch_results: dict[str, list[str]] = defaultdict(list)
    matchup_records: dict[tuple[str, str], list[str]] = defaultdict(list)

    for m in matches:
        if user not in m.players:
            continue
        opponent = next((p for p in m.players if p != user), None)
        if not opponent:
            continue
        your_cards = m.cards_by_player.get(user, [])
        opp_cards = m.cards_by_player.get(opponent, [])
        your_arch = _classify(your_cards)
        opp_arch = _classify(opp_cards)

        winner = m.match_winner
        won = winner == user
        if winner:
            if won:
                wins += 1
            elif winner == opponent:
                losses += 1
        for g in m.games:
            if g.get("winner") == user:
                games_w += 1
            elif g.get("loser") == user:
                games_l += 1

        if winner:
            tag = "W" if won else "L"
            your_arch_results[your_arch].append(tag)
            opp_arch_results[opp_arch].append(tag)
            matchup_records[(your_arch, opp_arch)].append(tag)

        rows.append({
            "match_id": m.match_id,
            "opponent": opponent,
            "your_games": sum(1 for g in m.games if g.get("winner") == user),
            "their_games": sum(1 for g in m.games if g.get("winner") == opponent),
            "match_winner": winner or "",
            "result_for_you": ("W" if won else "L") if winner else "?",
            "your_archetype": your_arch,
            "their_archetype": opp_arch,
            "your_signature": signature_str(signature(your_cards)),
            "their_signature": signature_str(signature(opp_cards)),
            "turns": m.turns,
        })

    rows.sort(key=lambda r: r["match_id"])

    total = len(rows)
    decided = wins + losses
    print(f"Matches with a recorded winner: {decided}/{total}")
    if decided:
        print(f"  Your record: {wins}-{losses}  ({wins / decided:.0%} win rate)")
    if games_w + games_l:
        gw = games_w + games_l
        print(f"  Game record: {games_w}-{games_l}  ({games_w / gw:.0%} game win rate)\n")

    def _print(title: str, data: dict[str, list[str]], limit: int = 25) -> None:
        print(f"\n{title}")
        for name, results in sorted(data.items(), key=lambda kv: -len(kv[1]))[:limit]:
            w = results.count("W")
            n = len(results)
            pct = f"{w / n:.0%}" if n else "-"
            print(f"  {n:>3}  {w}-{n - w:<3}  {pct:>4}  {name}")

    _print("Your decks (last %d days):" % days, your_arch_results)
    _print("Opponent archetypes faced:", opp_arch_results)

    top_your = sorted(your_arch_results, key=lambda k: -len(your_arch_results[k]))[:3]
    if top_your:
        print(f"\nMatchups for your top {len(top_your)} deck(s):")
        for yn in top_your:
            print(f"  -- as {yn} --")
            row = {opp: matchup_records[(yn, opp)] for (y, opp) in matchup_records if y == yn}
            for opp, results in sorted(row.items(), key=lambda kv: -len(kv[1]))[:15]:
                w = results.count("W")
                n = len(results)
                pct = f"{w / n:.0%}" if n else "-"
                print(f"     {n:>3}  {w}-{n - w:<3}  {pct:>4}  vs {opp}")

    if csv_path is None:
        csv_path = REPO_ROOT / f"matches_last_{days}d.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    print(f"\nCSV written: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
