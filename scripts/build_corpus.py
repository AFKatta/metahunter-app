"""Build a corpus of recent decklists for a given MTGO format, then
archetype-tag each deck with specific names like "Dimir Tempo" or
"Jeskai Control".

Pulls the last N days of events (Challenges + Leagues + …) from
mtgo.com for the chosen format, applies the Badaro/MTGOFormatData
rules + variant detection + colour prefixing + name overrides, and
saves ``data/corpus/<format>.json``.

Usage:
    # Default — Legacy, last 30 days.
    python scripts/build_corpus.py

    # A different format, last 30 days.
    python scripts/build_corpus.py --format vintage
    python scripts/build_corpus.py --format modern

    # Custom window.
    python scripts/build_corpus.py --format pauper --days 14
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from metahunter_core.classifier import (
    Archetype,
    _conditions_match,
    load_legacy,
)
from metahunter_core.naming import full_name
from metahunter_core.sources.mtgo_com import (
    Card, Deck, KNOWN_FORMAT_SLUGS, fetch_recent_decks,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FORMAT_DATA = REPO_ROOT / "data" / "MTGOFormatData"
CORPUS_DIR = REPO_ROOT / "data" / "corpus"


def cards_to_set(cards: list[Card]) -> set[str]:
    return {c.name for c in cards}


def cards_to_names(cards: list[Card]) -> list[str]:
    out: list[str] = []
    for c in cards:
        out.extend([c.name] * max(1, c.qty))
    return out


def deck_color_identity(deck: Deck) -> str:
    """WUBRG-ordered colour identity of the non-land mainboard."""
    seen: set[str] = set()
    for c in deck.main_deck:
        if c.card_type == "LAND":
            continue
        for col in c.colors:
            seen.add(col)
    return "".join(sorted(seen, key="WUBRG".index))


def classify_with_naming(
    deck: Deck, archetypes: list[Archetype], fallbacks: list[Archetype]
) -> tuple[str, int, bool]:
    """Returns ``(name, evidence_score, is_fallback)``.

    ``is_fallback=True`` means the deck was tagged via Badaro's
    Fallbacks/ rules (generic Midrange / Control / Aggro / Stompy / …
    with a colour prefix). The classifier never matches against these
    — they're just colour-shelled placeholders, not real archetypes.
    """
    main = cards_to_set(deck.main_deck)
    side = cards_to_set(deck.sideboard)
    all_cards = main | side

    best: tuple[Archetype, int, bool, str] | None = None
    for a in archetypes:
        ok, ev = _conditions_match(a.conditions, all_cards)
        if not ok:
            continue
        name = a.name
        include_color = a.include_color
        for v in a.variants:
            vok, vev = _conditions_match(v.conditions, all_cards)
            if vok:
                name = v.name
                include_color = v.include_color
                ev += vev
                break
        if best is None or ev > best[1]:
            best = (a, ev, include_color, name)

    if best:
        _a, ev, include_color, base_name = best
        color = deck_color_identity(deck)
        return full_name(base_name, color, main, side, include_color), ev, False

    for f in fallbacks:
        if any(c in all_cards for c in f.common_cards):
            color = deck_color_identity(deck)
            return full_name(f.name, color, main, side, f.include_color), 1, True

    return "Unknown", 0, False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--format",
        default="legacy",
        choices=KNOWN_FORMAT_SLUGS,
        help="MTGO format to scrape (default: legacy).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="How many days back to fetch (default: 30).",
    )
    args = parser.parse_args()

    fmt_slug = args.format.lower()
    days = args.days
    out_path = CORPUS_DIR / f"{fmt_slug}.json"

    print(f"Fetching MTGO {fmt_slug.capitalize()} events from last {days} days...")
    decks = fetch_recent_decks(fmt_slug, days=days)
    print(f"  fetched {len(decks)} decks")

    # Badaro's MTGOFormatData has rule sets per format. We currently
    # only load the Legacy rules — if you target a different format
    # without a matching rule directory you'll get colour-code fallback
    # names. The classifier handles that gracefully.
    archetypes, fallbacks = load_legacy(FORMAT_DATA)

    out_decks = []
    archetype_counts: Counter[str] = Counter()
    for d in decks:
        name, score, is_fallback = classify_with_naming(d, archetypes, fallbacks)
        archetype_counts[name] += 1
        out_decks.append({
            "archetype": name,
            "evidence_score": score,
            "is_fallback": is_fallback,
            "event_date": d.event_date,
            "event_type": d.event_type,
            "event_id": d.event_id,
            "player": d.player,
            "place": d.place,
            "color_identity": deck_color_identity(d),
            # We keep per-card colours + type so the classifier can do
            # honest colour-identity inference on observed bags later.
            "main_deck": [
                [c.qty, c.name, "".join(c.colors), c.card_type]
                for c in d.main_deck
            ],
            "sideboard": [
                [c.qty, c.name, "".join(c.colors), c.card_type]
                for c in d.sideboard
            ],
        })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "built_at": datetime.now(timezone.utc).isoformat(),
        "format": fmt_slug.capitalize(),
        "days": days,
        "deck_count": len(out_decks),
        "decks": out_decks,
    }, indent=2), encoding="utf-8")

    print(f"\nWrote {out_path}")
    print(f"\nArchetype distribution in corpus:")
    for name, n in archetype_counts.most_common(50):
        print(f"  {n:>4}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
