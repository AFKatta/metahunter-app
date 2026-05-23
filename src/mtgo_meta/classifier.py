"""Archetype classification using Badaro/MTGOFormatData rules.

We treat the bag of cards observed in a match as the player's decklist.
Each archetype has a list of conditions that ALL must be satisfied.
Variants drill down into sub-archetypes once the parent matches.
If no archetype matches, fallbacks classify by macro-strategy.

Caveats from partial observation
--------------------------------
We only see cards that became visible during play — never the
sideboard, never cards stuck in opponent's hand all game. So:

* "InMainboard" / "InSideboard" / "InMainOrSideboard" are all treated
  as "card present in the observed bag".
* "DoesNotContain" rules are satisfied by default when we haven't
  seen the disqualifying card. This can cause false positives on
  short games. Tie-breaking by evidence count partially mitigates.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _loose_json(text: str) -> dict:
    """Parse JSON that may contain trailing commas (some MTGOFormatData files do)."""
    cleaned = _TRAILING_COMMA_RE.sub(r"\1", text)
    return json.loads(cleaned)


@dataclass
class Archetype:
    name: str
    include_color: bool = False
    conditions: list[dict] = field(default_factory=list)
    variants: list["Archetype"] = field(default_factory=list)
    common_cards: list[str] = field(default_factory=list)  # fallback only


def _make(data: dict) -> Archetype:
    return Archetype(
        name=data["Name"],
        include_color=data.get("IncludeColorInName", False),
        conditions=data.get("Conditions", []),
        variants=[_make(v) for v in data.get("Variants", [])],
        common_cards=data.get("CommonCards", []),
    )


def load_legacy(repo_root: Path) -> tuple[list[Archetype], list[Archetype]]:
    legacy = repo_root / "Formats" / "Legacy"
    arches = [
        _make(_loose_json(p.read_text(encoding="utf-8")))
        for p in sorted((legacy / "Archetypes").glob("*.json"))
    ]
    fallbacks = [
        _make(_loose_json(p.read_text(encoding="utf-8")))
        for p in sorted((legacy / "Fallbacks").glob("*.json"))
    ]
    return arches, fallbacks


def _conditions_match(conditions: list[dict], cards: set[str]) -> tuple[bool, int]:
    """Returns ``(passed, positive_evidence)``.

    ``positive_evidence`` is the count of cards from the conditions that
    were actually observed — used for tie-breaking. A "DoesNotContain"
    rule contributes 0 even when satisfied.
    """
    positive = 0
    for cond in conditions:
        ctype = cond.get("Type", "")
        cs = cond.get("Cards", [])
        if ctype in ("InMainboard", "InSideboard", "InMainOrSideboard"):
            if not any(c in cards for c in cs):
                return False, 0
            positive += sum(1 for c in cs if c in cards)
        elif ctype == "OneOrMoreInMainboard":
            hits = sum(1 for c in cs if c in cards)
            if hits < 1:
                return False, 0
            positive += hits
        elif ctype == "TwoOrMoreInMainboard":
            hits = sum(1 for c in cs if c in cards)
            if hits < 2:
                return False, 0
            positive += hits
        elif ctype in ("DoesNotContain", "DoesNotContainMainboard"):
            if any(c in cards for c in cs):
                return False, 0
        # unknown types: silently pass — we don't want to disqualify on novelty
    return True, positive


def classify(
    cards_seen: list[str],
    archetypes: list[Archetype],
    fallbacks: list[Archetype],
) -> tuple[str, int]:
    """Return ``(archetype_name, evidence_score)``.

    Score is the number of card-hits that supported the classification.
    Higher = more confident. Score of 0 means a fallback fired with no
    positive hits, or we found nothing at all ("Unknown").
    """
    if not cards_seen:
        return "Unknown", 0
    cs = set(cards_seen)

    best: tuple[str, int] | None = None
    for a in archetypes:
        ok, evidence = _conditions_match(a.conditions, cs)
        if not ok:
            continue
        # Pick the most specific variant whose conditions also match.
        name = a.name
        for v in a.variants:
            vok, vev = _conditions_match(v.conditions, cs)
            if vok:
                name = v.name
                evidence += vev
                break
        if best is None or evidence > best[1]:
            best = (name, evidence)

    if best is not None:
        return best

    # Macro-strategy fallbacks (any CommonCard observed wins).
    for f in fallbacks:
        if any(c in cs for c in f.common_cards):
            return f.name, 1

    return "Unknown", 0


# ---------------------------------------------------------------------------
# Similarity-based classification against a corpus of real decklists.
# ---------------------------------------------------------------------------

from math import log


def _card_name(entry: list) -> str:
    # Corpus entries are [qty, name] (old) or [qty, name, colors, card_type].
    return entry[1] if len(entry) >= 2 else ""


def _card_colors(entry: list) -> str:
    return entry[2] if len(entry) >= 3 else ""


def _card_type(entry: list) -> str:
    return entry[3] if len(entry) >= 4 else ""


def build_card_weights(corpus_decks: list[dict]) -> dict[str, float]:
    """IDF weights: rare cards in the corpus count more than staples.

    A card present in every deck gets weight ~1; a card in 1 of 1000 decks
    gets a much higher weight, so seeing it gives strong evidence.
    """
    n = max(len(corpus_decks), 1)
    df: Counter[str] = Counter()
    for d in corpus_decks:
        unique = {_card_name(e) for e in d.get("main_deck", [])}
        unique.update(_card_name(e) for e in d.get("sideboard", []))
        unique.discard("")
        for c in unique:
            df[c] += 1
    return {c: log((n + 1) / (df_c + 1)) + 1.0 for c, df_c in df.items()}


# The corpus json embeds each deck's pre-computed colour_identity string
# (e.g. "WUG"). The user's observed bag has no colour data on its own,
# but we can fold colour info back from the corpus by checking which
# colours each card name belongs to across the whole corpus.
def build_card_colors(corpus_decks: list[dict]) -> dict[str, str]:
    """card name → colour identity letters (e.g. 'U', 'WUR', '').

    Two sources:
      1. MTGO's ``card_attributes.colors`` saved into each card entry by
         ``build_corpus.py``. This works for spells.
      2. ``NONBASIC_LAND_COLORS`` for dual / fetch / shock lands, since
         MTGO marks lands as colourless even when they signal two
         colours strongly.
    """
    out: dict[str, str] = {}
    for d in corpus_decks:
        for entry in d.get("main_deck", []) + d.get("sideboard", []):
            name = _card_name(entry)
            if not name:
                continue
            cols = _card_colors(entry)
            if name not in out or len(cols) > len(out[name]):
                out[name] = cols
    # Overlay the manual non-basic-land table so a Plateau actually says
    # WR even though its MTGO ``colors`` field is empty.
    for name, cols in NONBASIC_LAND_COLORS.items():
        if cols:
            out[name] = cols
    return out


# ---------------------------------------------------------------------------
# Custom user-defined archetype rules. These fire BEFORE the corpus
# similarity matcher — when a custom rule matches, the bag is labelled
# with that name immediately. Useful for personal brews or local-meta
# decks that aren't in the MTGO Challenge corpus.
#
# Each rule has:
#   * "name":     the label to apply
#   * "colors":   exact colour identity required (e.g. "WUG"), or None
#                 for any colours
#   * "all_of":   list of card-groups (AND of OR). Every group must
#                 have at least one of its cards observed.
#   * "none_of":  cards that, if observed, disqualify this rule.
#
# Order matters — earlier rules win. Add new rules at the top.
# ---------------------------------------------------------------------------
CUSTOM_ARCHETYPE_RULES: list[dict] = [
    {
        "name": "Bant Uro",
        "colors": "WUG",
        "all_of": [
            # Any one of these distinct Bant-Uro defining cards.
            # Uro / Badgermole Cub / Wan Shi Tong are all unique to
            # this kind of shell — most non-Bant-Uro decks won't run any.
            ["Uro, Titan of Nature's Wrath", "Badgermole Cub",
             "Wan Shi Tong, Librarian"],
            # Plus one supporting card to anchor the colour identity
            # against generic 2-colour brews.
            ["Noble Hierarch", "Carpet of Flowers", "Tundra",
             "Tropical Island", "Savannah", "Hedge Maze"],
        ],
        "none_of": [
            "Up the Beanstalk",  # would make it Beanstalk Control
            "Yorion, Sky Nomad", # would make it a Yorion variant
            "Gaea's Cradle",     # would make it Cradle Control
        ],
    },
    {
        # Naya Initiative — the 60-card non-Yorion Initiative shell
        # with white starters, red disruption (Broadside Bombardiers),
        # and green ramp (Once Upon a Time, Elvish Spirit Guide).
        # Not in the corpus because it doesn't show up in MTGO
        # Challenges — too casual / off-meta — so we hand-roll it.
        "name": "Naya Initiative",
        "colors": "WRG",
        "all_of": [
            # Any Initiative dungeon-rider / starter is mandatory.
            ["Seasoned Dungeoneer", "White Plume Adventurer",
             "Caves of Chaos Adventurer", "Undermountain Adventurer",
             "The Initiative"],
            # Plus a Naya-specific support card so a Boros / Mono-White
            # Initiative match can't accidentally match this rule.
            ["Broadside Bombardiers", "Once Upon a Time",
             "Elvish Spirit Guide"],
        ],
        "none_of": [
            "Yorion, Sky Nomad",  # 80-card variant has its own name
            "Up the Beanstalk",
        ],
    },
    {
        # Boros Initiative — same shell minus the green. Listed AFTER
        # Naya so a WRG game with all three colours observed lands on
        # Naya, while a WR-only game (no green ever seen) lands here.
        "name": "Boros Initiative",
        "colors": "WR",
        "all_of": [
            ["Seasoned Dungeoneer", "White Plume Adventurer",
             "Caves of Chaos Adventurer", "Undermountain Adventurer",
             "The Initiative"],
        ],
        "none_of": [
            "Yorion, Sky Nomad",
            "Once Upon a Time",       # would be Naya Initiative
            "Elvish Spirit Guide",
        ],
    },
    {
        # Mono-White Initiative — last fallback for an Initiative
        # shell with no R or G observed.
        "name": "Mono-White Initiative",
        "colors": "W",
        "all_of": [
            ["Seasoned Dungeoneer", "White Plume Adventurer",
             "Caves of Chaos Adventurer", "Undermountain Adventurer",
             "The Initiative"],
        ],
        "none_of": [
            "Yorion, Sky Nomad",
            "Broadside Bombardiers",
            "Once Upon a Time",
            "Elvish Spirit Guide",
        ],
    },
    {
        # Reanimator — covers any bag with a reanimation spell + a
        # real reanimation target. The corpus's only "Reanimator"
        # entry has ci=UBRG (Atraxa-based 5c shell) which fails the
        # colour gate against narrower observed identities (e.g. a
        # BR Reanimator player on the modern Sire of Insanity /
        # Griselbrand build). This rule fires BEFORE the corpus
        # matcher, so we don't depend on corpus coverage.
        #
        # Cap: WUBR. Classic Reanimator is mono-B / BR / occasionally
        # UBR (Grixis Reanimator). Never G — a BRG bag with Reanimate
        # is a brewy Pile / Jund attempt, not Reanimator.
        "name": "Reanimator",
        "colors": "WUBR",
        "all_of": [
            # Reanimation engine spell.
            ["Reanimate", "Animate Dead", "Exhume", "Goryo's Vengeance",
             "Shallow Grave", "Unmarked Grave", "Persist"],
            # Reanimation target — the deck's win condition.
            ["Griselbrand", "Archon of Cruelty", "Atraxa, Grand Unifier",
             "Chancellor of the Annex", "Serra's Emissary",
             "Iona, Shield of Emeria", "Sphinx of the Steel Wind",
             "Worldspine Wurm", "Tidespout Tyrant",
             "Jin-Gitaxias, Core Augur", "Elesh Norn, Mother of Machines",
             "Sire of Insanity", "Reya Dawnbringer"],
        ],
        "none_of": [
            # Combo-engine signals from other archetypes
            "Worldgorger Dragon",       # Worldgorger Combo
            "Phyrexian Dreadnought",    # Stiflenought
        ],
    },
    {
        # Mono-Black aggressive / midrange — modern Dauthi Voidwalker
        # / Nethergoyf / Bowmasters shell with Hymn to Tourach + Dark
        # Ritual + Sheoldred's Edict. The corpus has one "Mono-Black
        # Delver" entry but it's signature-gated by Delver of Secrets
        # / DRC, which this archetype doesn't actually run. Custom
        # rule fills the gap so saintechapelle-style mono-B bags land
        # on a sensible name instead of falling to the "B" colour
        # code.
        #
        # Colours: only fires when the observed identity is mono-B
        # (so a Dimir tempo player who happened to cast no blue spell
        # AND has only weak Undercity Sewers as U source still lands
        # here — which is what the user wants).
        "name": "Mono-Black",
        "colors": "B",
        "all_of": [
            # One of the modern mono-B 1-2-mana threats (these replace
            # Delver of Secrets / DRC in the mono-B Lurrus-less shell).
            ["Dauthi Voidwalker", "Nethergoyf", "Orcish Bowmasters",
             "Bloodsoaked Champion", "Knight of the Ebon Legion"],
            # Plus a real discard / disruption staple. Stops a random
            # mono-B Reanimator bag from accidentally matching this.
            ["Hymn to Tourach", "Sheoldred's Edict", "Thoughtseize",
             "Liliana of the Veil", "Dark Ritual", "Inquisition of Kozilek"],
        ],
        "none_of": [
            # Real Delver/Dimir Tempo signals — those land on their
            # own corpus archetypes.
            "Delver of Secrets", "Dragon's Rage Channeler",
            # Reanimator / Doomsday / combo signals — different decks.
            "Reanimate", "Animate Dead", "Exhume", "Doomsday",
            # Stompy / Helm Combo signals.
            "Phyrexian Dreadnought", "Helm of Obedience",
        ],
    },
]


def _custom_rule_label(obs: set[str], observed_colors: set[str]) -> str | None:
    """Return the first custom-rule label that matches, else None.

    Colour semantics: ``rule["colors"]`` is the set of colours the deck
    CAN contain. The observed bag's colour identity must be a **subset**
    of that set. This is tolerant — a Bant (WUG) deck can show up as
    UG in a single match if the user happened to cast no W cards.
    Decks that show extra colours (e.g. UB, UBR) never match the rule.
    """
    for rule in CUSTOM_ARCHETYPE_RULES:
        wanted = rule.get("colors")
        if wanted is not None:
            if not observed_colors.issubset(set(wanted)):
                continue
        if any(c in obs for c in rule.get("none_of", [])):
            continue
        groups = rule.get("all_of", [])
        if not all(any(c in obs for c in group) for group in groups):
            continue
        return rule["name"]
    return None


# Manual colour-identity table for non-basic lands that *produce* mana
# of the listed colours. MTGO marks lands as colourless, so without
# this overlay Plateau + Pyroblast can't tell Jeskai from Azorius.
#
# IMPORTANT: fetchlands are NOT included here. A fetchland searches
# for one of two basic-land *types* and may fetch any dual sharing
# that type — so Bloodstained Mire ostensibly searches Mountain/Swamp,
# but Dimir decks happily run it to fetch Underground Sea (a Swamp
# dual). Treating fetches as definitive colour evidence would make a
# UB deck look UBR. The colour signal we trust comes only from lands
# that actually tap for mana of a known colour.
NONBASIC_LAND_COLORS: dict[str, str] = {
    # Original dual lands (produce two specific colours)
    "Tundra": "WU", "Underground Sea": "UB", "Badlands": "BR",
    "Taiga": "RG", "Savannah": "WG", "Plateau": "WR",
    "Scrubland": "WB", "Tropical Island": "UG", "Volcanic Island": "UR",
    "Bayou": "BG",
    # Surveil lands (Murders at Karlov Manor / OTJ) — two colours
    "Underground Sewers": "UB", "Undercity Sewers": "UB",
    "Thundering Falls": "UR", "Meticulous Archive": "WU",
    "Hedge Maze": "WG", "Boggart Bog": "BG", "Elegant Parlor": "WR",
    "Lush Portico": "WG", "Raucous Theater": "BR",
    "Shadowy Backstreet": "WB", "Commercial District": "RG",
    # Verge lands (Foundations Beyond) — two colours
    "Gloomlake Verge": "UB", "Floodfarm Verge": "WU",
    "Riverpyre Verge": "UR", "Hushwood Verge": "UG",
    "Bleachbone Verge": "WB", "Willowrush Verge": "WG",
    "Blazemire Verge": "BR", "Thornspire Verge": "RG",
    "Sunbillow Verge": "WR", "Wastewood Verge": "BG",
    # Horizon-canopy cycle — two colours, plus life pay
    "Horizon Canopy": "WG", "Fiery Islet": "UR",
    "Nurturing Peatland": "BG", "Silent Clearing": "WB",
    "Sunbaked Canyon": "WR", "Waterlogged Grove": "UG",
    # Triomes — three colours
    "Ketria Triome": "URG", "Indatha Triome": "WBG",
    "Raugrin Triome": "WUR", "Savai Triome": "WBR",
    "Zagoth Triome": "UBG",
    "Spara's Headquarters": "WUG", "Raffine's Tower": "WUB",
    "Xander's Lounge": "UBR", "Ziatora's Proving Ground": "BRG",
    "Jetmir's Garden": "WRG",
    # Channel lands — one specific colour
    "Karakas": "W",
    "Boseiju, Who Endures": "G", "Otawara, Soaring City": "U",
    "Eiganjo, Seat of the Empire": "W", "Takenuma, Abandoned Mire": "B",
    "Sokenzan, Crucible of Defiance": "R",
    "Bojuka Bog": "B", "Cabal Pit": "B",
    # Truly colourless utility lands — listed so they're explicitly
    # marked as not contributing colour evidence.
    "Wasteland": "", "City of Traitors": "", "Ancient Tomb": "",
    "Mishra's Workshop": "", "Cavern of Souls": "", "Mutavault": "",
    "Dark Depths": "", "Thespian's Stage": "", "Glacial Chasm": "",
    "The Tabernacle at Pendrell Vale": "", "Maze of Ith": "",
    # NOTE: Fetchlands (Flooded Strand, Polluted Delta, Bloodstained
    # Mire, Wooded Foothills, Windswept Heath, Marsh Flats, Scalding
    # Tarn, Verdant Catacombs, Arid Mesa, Misty Rainforest) are
    # intentionally absent — see the comment at the top.
}


# Mono-coloured legendary / channel / utility lands. These have a
# colour pip in NONBASIC_LAND_COLORS but the deck almost never actually
# CASTS spells of that colour — they're 1-2-of utility cards (legendary
# bounce, graveyard hate, channel removal). A 1-of Karakas in a green
# deck doesn't make it WG. A 1-of Bojuka Bog in a Lands deck doesn't
# make it BG-with-actual-black.
#
# Rule: a utility land's colour is credited to the deck/observed bag
# ONLY when reinforced by either (a) a real cast spell of that colour,
# or (b) a multi-colour dual / triome that contains that colour, or
# (c) a basic of that colour. Without reinforcement, the utility land
# is treated as colourless.
#
# This is the dual to ``_WEAK_COLOUR_SIGNAL_CARDS`` for spells — same
# spirit, applied to lands.
_UTILITY_LAND_COLORS: dict[str, str] = {
    "Karakas": "W",
    "Eiganjo, Seat of the Empire": "W",
    "Otawara, Soaring City": "U",
    "Bojuka Bog": "B",
    "Cabal Pit": "B",
    "Takenuma, Abandoned Mire": "B",
    "Sokenzan, Crucible of Defiance": "R",
    "Boseiju, Who Endures": "G",
}

# "Budget" dual lands — surveil cycle (Karlov Manor / OTJ), verge cycle
# (Foundations Beyond), and the horizon-canopy cycle (Modern Horizons).
# Each is a real dual on paper, but in practice these are 4-of free
# splash lands: a mono-Black deck routinely runs 4 Undercity Sewers
# because they enter tapped and produce B; the U mode is never used.
# A mono-Death's-Shadow deck runs 4 Silent Clearing for the card-draw
# mode, never actually casting a white spell.
#
# Rule: each colour pip is credited to the deck/observed bag ONLY when
# reinforced by a cast spell, a basic land, or a STRONG original dual
# (Underground Sea, Tropical Island, etc.) of that colour. Without
# reinforcement, the pip is treated as colourless.
#
# Bi-colour generalisation of ``_UTILITY_LAND_COLORS``.
_WEAK_DUAL_LAND_COLORS = {
    # Surveil lands (Murders at Karlov Manor + Outlaws of Thunder Junction)
    "Underground Sewers", "Undercity Sewers", "Thundering Falls",
    "Meticulous Archive", "Hedge Maze", "Boggart Bog", "Elegant Parlor",
    "Lush Portico", "Raucous Theater", "Shadowy Backstreet",
    "Commercial District",
    # Verge cycle (Foundations / Foundations Beyond)
    "Gloomlake Verge", "Floodfarm Verge", "Riverpyre Verge",
    "Hushwood Verge", "Bleachbone Verge", "Willowrush Verge",
    "Blazemire Verge", "Thornspire Verge", "Sunbillow Verge",
    "Wastewood Verge",
    # Horizon-canopy cycle (Modern Horizons) — played as 1-2-of utility
    # for the sacrifice-to-draw mode.
    "Horizon Canopy", "Fiery Islet", "Nurturing Peatland",
    "Silent Clearing", "Sunbaked Canyon", "Waterlogged Grove",
}


# Sentinel string the classifier returns when a match has too few
# observed cards to draw any conclusion. API endpoints drop these
# matches entirely — they don't appear in any aggregation.
SKIP_LABEL = "__SKIP__"


# Colour-identity prefixes used by archetype names in the corpus. We use
# this to (a) collapse "X Midrange" into "X Tempo"/"X Control" so we
# don't carry three near-identical labels per colour family, and (b)
# accept session-consensus when all neighbour matches share the same
# family even if their specific tail differs ("Jeskai Control" and
# "Jeskai Midrange" both have prefix "Jeskai"). Ordered longest-first so
# "Mono Black" matches before "Mono", "5-Color" before "5c", etc.
_COLOUR_PREFIXES: tuple[str, ...] = (
    # 4-colour identities with the specific missing colour spelled out
    # MUST come before the bare "4c" / "4-Color" entries below, so
    # ``colour_prefix("4c (no white) Midrange")`` matches the full
    # parenthetical form and doesn't get double-stamped on rename.
    "4c (no white)", "4c (no blue)", "4c (no black)",
    "4c (no red)", "4c (no green)",
    "4-Color (no white)", "4-Color (no blue)", "4-Color (no black)",
    "4-Color (no red)", "4-Color (no green)",
    # Mono-colour (with both common spellings)
    "Mono-White", "Mono-Blue", "Mono-Black", "Mono-Red", "Mono-Green",
    "Mono White", "Mono Blue", "Mono Black", "Mono Red", "Mono Green",
    # Generic 5-colour and 4-colour fallback prefixes
    "5-Color", "5-color", "5C", "5c",
    "4-Color", "4-color", "4C", "4c",
    # Three-colour (shards then wedges)
    "Bant", "Esper", "Grixis", "Jund", "Naya",
    "Jeskai", "Mardu", "Sultai", "Temur", "Abzan",
    # Two-colour guilds
    "Azorius", "Dimir", "Rakdos", "Gruul", "Selesnya",
    "Orzhov", "Izzet", "Golgari", "Boros", "Simic",
)


def colour_prefix(name: str) -> str | None:
    """Return the colour-identity prefix of an archetype name, or None.

    Used for session-inference family-consensus and for Midrange
    normalisation. Matches the longest prefix from ``_COLOUR_PREFIXES``
    that starts the name and is followed by a space (or is the whole
    name on its own). So "Jeskai Control" → "Jeskai", "Bant Uro" →
    "Bant", "5-Color Stompy" → "5-Color", "Goblins" → None.
    """
    for p in _COLOUR_PREFIXES:
        if name == p or name.startswith(p + " "):
            return p
    return None


# Reverse lookup: from a colour set to its canonical guild/shard/wedge
# name. Used to fix up corpus archetype names whose prefix lies about
# the deck's actual mana base — e.g. a corpus deck called "Azorius
# Midrange" whose deck list reveals red cards (true colour identity WUR)
# should display as "Jeskai Midrange" so the name agrees with reality.
_COLOUR_SET_TO_PREFIX: dict[frozenset, str] = {
    frozenset("W"): "Mono-White",
    frozenset("U"): "Mono-Blue",
    frozenset("B"): "Mono-Black",
    frozenset("R"): "Mono-Red",
    frozenset("G"): "Mono-Green",
    frozenset("WU"): "Azorius",
    frozenset("UB"): "Dimir",
    frozenset("BR"): "Rakdos",
    frozenset("RG"): "Gruul",
    frozenset("WG"): "Selesnya",
    frozenset("WB"): "Orzhov",
    frozenset("UR"): "Izzet",
    frozenset("BG"): "Golgari",
    frozenset("WR"): "Boros",
    frozenset("UG"): "Simic",
    frozenset("WUB"): "Esper",
    frozenset("UBR"): "Grixis",
    frozenset("BRG"): "Jund",
    frozenset("WRG"): "Naya",
    frozenset("WUG"): "Bant",
    frozenset("WUR"): "Jeskai",
    frozenset("WBR"): "Mardu",
    frozenset("UBG"): "Sultai",
    frozenset("URG"): "Temur",
    frozenset("WBG"): "Abzan",
    frozenset("WUBR"): "4c (no green)",
    frozenset("WUBG"): "4c (no red)",
    frozenset("WUBRG"): "5-Color",
}
# 4-colour identities that aren't always called the same thing. The
# canonical labels above use a parenthetical "no X" suffix; corpus may
# use "4c", "4-Color", or other variants. We normalise to the
# parenthetical form here for display consistency.
_COLOUR_SET_TO_PREFIX[frozenset("WUBR")] = "4c (no green)"
_COLOUR_SET_TO_PREFIX[frozenset("WUBG")] = "4c (no red)"
_COLOUR_SET_TO_PREFIX[frozenset("WURG")] = "4c (no black)"
_COLOUR_SET_TO_PREFIX[frozenset("WBRG")] = "4c (no blue)"
_COLOUR_SET_TO_PREFIX[frozenset("UBRG")] = "4c (no white)"


def colour_set_to_prefix(colours: set[str] | frozenset[str]) -> str | None:
    """Map a colour set ({'W','U','R'}) to its canonical name ("Jeskai")."""
    return _COLOUR_SET_TO_PREFIX.get(frozenset(colours))


# Reverse: archetype-name prefix to its required colour set. Used to
# enforce "Sultai means U + B + G, not just B + G". When an
# archetype name carries a colour prefix that names specific colours,
# the observed bag MUST contain ALL of those colours — the corpus
# tolerance of "1 extra in deck" doesn't apply, because seeing only
# 2 of Sultai's 3 colours unambiguously means the deck isn't Sultai.
_PREFIX_TO_COLOUR_SET: dict[str, frozenset[str]] = {}
for _cs, _name in _COLOUR_SET_TO_PREFIX.items():
    _PREFIX_TO_COLOUR_SET[_name] = _cs
# Alternative spellings handled by _COLOUR_PREFIXES (no hyphen, etc.)
for _c, _letter in (("White", "W"), ("Blue", "U"), ("Black", "B"),
                    ("Red", "R"), ("Green", "G")):
    _PREFIX_TO_COLOUR_SET[f"Mono {_c}"] = frozenset(_letter)
# "4-Color (no X)" alternate hyphenated spellings
_PREFIX_TO_COLOUR_SET["4-Color (no white)"] = frozenset("UBRG")
_PREFIX_TO_COLOUR_SET["4-Color (no blue)"]  = frozenset("WBRG")
_PREFIX_TO_COLOUR_SET["4-Color (no black)"] = frozenset("WURG")
_PREFIX_TO_COLOUR_SET["4-Color (no red)"]   = frozenset("WUBG")
_PREFIX_TO_COLOUR_SET["4-Color (no green)"] = frozenset("WUBR")
# "5-color" / "5C" / "5c" lower-case variants
_PREFIX_TO_COLOUR_SET["5-color"] = frozenset("WUBRG")
_PREFIX_TO_COLOUR_SET["5C"]      = frozenset("WUBRG")
_PREFIX_TO_COLOUR_SET["5c"]      = frozenset("WUBRG")


def colour_prefix_to_set(prefix: str) -> frozenset[str] | None:
    """Map "Sultai" → frozenset("UBG"), etc. None for unknown prefixes."""
    return _PREFIX_TO_COLOUR_SET.get(prefix)


def _normalise_midrange(name: str, arch_counts: Counter[str]) -> str:
    """Rewrite "X Midrange" labels to the closest "X Tempo" / "X Control".

    The Midrange/Tempo/Control distinction is too thin to draw from a
    handful of observed cards. We collapse Midrange into Tempo when an
    "X Tempo" exists in the corpus, else Control, else Tempo by default
    (Legacy midrange skews tempo).
    """
    suffix = " Midrange"
    if not name.endswith(suffix):
        return name
    prefix = name[:-len(suffix)]
    tempo = f"{prefix} Tempo"
    control = f"{prefix} Control"
    if arch_counts.get(tempo, 0) > 0:
        return tempo
    if arch_counts.get(control, 0) > 0:
        return control
    return tempo


def _show_and_tell_variant(observed_colors: set[str], obs: set[str]) -> str | None:
    """Name a Show-and-Tell deck by the cards + mana base it actually shows.

    Priority order:
      0. Reanimation spell in obs → NOT a Show-and-Tell variant.
         Real Sneak and Show / Omni-Tell / Aluren never run Reanimate;
         their combo is Show-and-Tell-into-fatty (or Sneak Attack-into-
         fatty), not reanimation. When Reanimate / Animate Dead /
         Exhume are present, the deck is REANIMATOR that sideboards
         Show and Tell as a backup plan against graveyard hate. Bail
         out and let the corpus matcher classify (it will land on
         "Sneak and Show with Reanimate", which the rename step in
         _normalise_archetype then converts to "Reanimator").
      1. R in observed colours  → Sneak and Show (Sneak Attack is R)
      2. Omniscience seen        → Omni-Tell (any colour combo)
      3. Aluren seen             → Aluren
      4. UG mana base (no combo) → Omni-Tell (the common UG no-show variant)
      5. anything else with Show and Tell observed → "Show and Tell"
    """
    if "Show and Tell" not in obs:
        return None
    if obs & {"Reanimate", "Animate Dead", "Exhume", "Shallow Grave",
              "Goryo's Vengeance", "Unmarked Grave"}:
        return None
    if "R" in observed_colors:
        return "Sneak and Show"
    if "Omniscience" in obs:
        return "Omni-Tell"
    if "Aluren" in obs:
        return "Aluren"
    if observed_colors == {"U", "G"}:
        return "Omni-Tell"
    return "Show and Tell"


def _rename_colour_prefix(name: str, deck_colors: set[str]) -> str:
    """Rewrite the colour prefix of an archetype name to match the
    deck's ACTUAL mana base.

    The corpus occasionally has decks labelled "Azorius Midrange" that
    actually splash red (deck_colors=WUR). Surfacing those as Azorius
    misleads the user — WU and WUR are different decks. We replace
    the prefix in the archetype name with the canonical guild/shard/
    wedge name for ``deck_colors``.

    If the deck has no colour-prefix on its name (e.g. "Doomsday",
    "Lands", "Cradle Control") we leave it alone — those names aren't
    keyed off colours.

    Convention exception: archetypes ending in " Depths" keep their
    original prefix even when the actual identity splashes a third
    colour (Bojuka Bog / Vampire Hexmage / etc.). The community
    canonically calls a WBG deck "Selesnya Depths" rather than
    "Abzan Depths" because the maindeck is overwhelmingly WG and the
    B is a single utility-card splash.
    """
    existing = colour_prefix(name)
    if existing is None:
        return name
    if name.endswith(" Depths"):
        return name
    canonical = colour_set_to_prefix(deck_colors)
    if canonical is None or canonical == existing:
        return name
    return canonical + name[len(existing):]


_BLUE_ARTIFACTS_BY_COLOR: dict[frozenset[str], str] = {
    # Mono-blue artifact shell — Pinnacle Emissary / Patchwork
    # Automaton / Kappa Cannoneer / Urza's Saga. The deck is the
    # community's "Affinity" — even though the corpus calls it "Blue
    # Artifacts" because most decklists are mono-U.
    frozenset("U"):  "Affinity",
    frozenset("UR"): "Izzet Artifacts",
    frozenset("UB"): "Dimir Artifacts",
    frozenset("WU"): "Azorius Artifacts",
    frozenset("UG"): "Simic Artifacts",
}


def _rename_blue_artifacts(name: str, observed_colors: set[str]) -> str:
    """Rewrite "Blue Artifacts" to a name that reflects the actual
    splash colours OBSERVED in this match.

    The corpus archetype "Blue Artifacts" covers a family of artifact
    shells from mono-U (Pinnacle/Cannoneer/Saga "Affinity") to UR
    (Tezzeret/Bolt/Welder "Izzet Artifacts"). The deck differentiation
    sits on whether the player casts REAL coloured spells — hybrid
    mana pips (Pinnacle Emissary's {U/R}) shouldn't promote a mono-U
    Affinity bag to Izzet. ``infer_color_identity`` filters hybrid
    cards already, so by the time we reach normalisation, the
    observed_colors set is honest.

    Only fires when the label is exactly one of the known artifact
    archetype names — defensive against accidentally renaming other
    blue-tagged decks (Blue Tempo, Blue Stompy, etc.).
    """
    if name not in {"Blue Artifacts", "Mono-Blue Artifacts"}:
        return name
    key = frozenset(observed_colors)
    return _BLUE_ARTIFACTS_BY_COLOR.get(key, name)


def _rename_sneak_show_with_reanimate(name: str) -> str:
    """Rewrite Badaro's "Sneak and Show with Reanimate" tag to plain
    "Reanimator".

    The Badaro rule fires whenever a deck contains Show and Tell + any
    reanimation spell (Reanimate / Animate Dead / Exhume). That's
    misleading: real Show-and-Tell archetypes (Sneak and Show /
    Omni-Tell / Aluren) never run Reanimate — their combo is
    Show-and-Tell-into-fatty, not reanimation. When Reanimate appears,
    the deck is a REANIMATOR deck that happens to sideboard 1-2 Show
    and Tell as a plan B against graveyard hate.

    Unconditional rename: any time the corpus winner is labelled
    "Sneak and Show with Reanimate", we replace it with "Reanimator".
    The bag's actual cards have already been validated by the
    Reanimator signature requirements (reanimation spell + fatty
    target) via the _veto_ok gate — so the deck genuinely IS
    Reanimator by every other classifier check.
    """
    if name == "Sneak and Show with Reanimate":
        return "Reanimator"
    return name


def _strip_yorion_suffix(name: str, obs: set[str]) -> str:
    """Drop the "(Yorion)" suffix when Yorion isn't observed.

    Yorion is a companion — MTGO reveals it at the start of every
    match it's the companion of. So if Yorion isn't anywhere in the
    observed bag, the player isn't running it, and the deck is the
    60-card non-Yorion variant of the same archetype.

    The corpus tags all Yorion-shell decks as "X (Yorion)". When the
    user faces a non-Yorion D&T / Stoneblade / etc., the bag still
    matches the same corpus deck's shell — we just shouldn't surface
    the Yorion tag in the label, because there's no companion.
    """
    if "(Yorion)" not in name:
        return name
    if "Yorion, Sky Nomad" in obs:
        return name
    return name.replace(" (Yorion)", "").replace("(Yorion)", "").rstrip()


def _normalise_archetype(
    name: str,
    obs: set[str],
    observed_colors: set[str],
    arch_counts: Counter[str],
    deck_colors: set[str],
) -> str:
    """Rename misleading corpus archetype labels.

    Passes, in priority order:
      1. Show-and-Tell variant rename (UR → "Sneak and Show" etc.).
      2. Blue Artifacts → Affinity / Izzet Artifacts / etc., based on
         the OBSERVED colour identity (hybrid mana already filtered).
      3. Strip "(Yorion)" suffix when Yorion isn't observed (companion
         is always revealed at start of match — absent = not running).
      4. Colour-prefix correction so a corpus deck mis-labelled
         "Azorius" but actually playing WUR comes out as "Jeskai".
      5. Midrange → Tempo/Control collapse.
    """
    variant = _show_and_tell_variant(observed_colors, obs)
    if variant is not None:
        return variant
    name = _rename_sneak_show_with_reanimate(name)
    name = _rename_blue_artifacts(name, observed_colors)
    name = _strip_yorion_suffix(name, obs)
    name = _rename_colour_prefix(name, deck_colors)
    return _normalise_midrange(name, arch_counts)


_LAND_NAMES = {
    # Bare basics — colourless evidence by themselves.
    "Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes",
    "Snow-Covered Plains", "Snow-Covered Island", "Snow-Covered Swamp",
    "Snow-Covered Mountain", "Snow-Covered Forest",
}

# Cards whose printed colour does NOT reliably reflect the deck's mana
# base, and therefore must be excluded from colour-identity inference.
# Three categories:
#
#   1. Phyrexian-mana and free-pact spells (Surgical Extraction in
#      mono-Blue, Pact of Negation in 5c Stompy). They have a coloured
#      printed cost but can be played without that colour in practice.
#
#   2. Companions (Yorion, Lurrus, …). A companion's printed cost (e.g.
#      Yorion 5WU) doesn't pin down the mana base — most companion
#      shells cast theirs via Cavern of Souls or a single colourless
#      utility-mana source. Naya Stompy (Yorion) is genuinely Naya
#      (WRG); the Yorion's U is purely cosmetic.
#
#   3. Cheat-into-play big creatures and finishers. Atraxa, Emrakul,
#      Griselbrand, Sphinx, Progenitus, Iona, Omniscience etc. all
#      enter the battlefield via Show and Tell / Sneak Attack /
#      Reanimate / Through the Breach. They're in the deck for their
#      ability, not because the mana base supports their cost.
#
# Without this exclusion, a Show-and-Tell deck cast on Atraxa looks
# WUBRG, a Naya Yorion deck looks WURG, and the colour-identity gate
# filters out the actual archetype in favour of the wrong 5c bucket.
_WEAK_COLOUR_SIGNAL_CARDS = {
    # ---- Phyrexian mana ----
    "Surgical Extraction",
    "Mental Misstep",
    "Gut Shot",
    "Mutagenic Growth",
    "Postmortem Lunge",
    "Dismember",
    "Vault Skirge",
    "Apostle's Blessing",
    "Phyrexian Metamorph",
    "Birthing Pod",
    # ---- Free 0-mana pacts ----
    "Pact of Negation",
    "Slaughter Pact",
    "Summoner's Pact",
    "Intervention Pact",
    "Pact of the Titan",
    # ---- "Spirit Guide" / any-colour ramp ----
    # Both Spirit Guides are activated for any-colour mana from exile;
    # they're almost never hard-cast. Decks like Oops! All Spells
    # include them purely as colourless ramp accelerants, so their
    # printed G / R doesn't reflect the deck's actual mana base.
    "Elvish Spirit Guide",
    "Simian Spirit Guide",
    # ---- Companions ----
    "Yorion, Sky Nomad",
    "Lurrus of the Dream-Den",
    "Jegantha, the Wellspring",
    "Kaheera, the Orphanguard",
    "Keruga, the Macrosage",
    "Obosh, the Preypiercer",
    "Umori, the Collector",
    "Zirda, the Dawnwaker",
    "Gyruda, Doom of Depths",
    "Lutri, the Spellchaser",
    # ---- Cheat-into-play fatties + finishers ----
    "Atraxa, Grand Unifier",
    "Emrakul, the Aeons Torn",
    "Emrakul, the Promised End",
    "Emrakul, the World Anew",
    "Griselbrand",
    "Iona, Shield of Emeria",
    "Progenitus",
    "Sphinx of the Steel Wind",
    "Worldspine Wurm",
    "Worldgorger Dragon",
    "Sundering Titan",
    "Archon of Cruelty",
    "Serra's Emissary",
    "Tidespout Tyrant",
    "Hullbreacher",
    "Ulamog, the Ceaseless Hunger",
    "Ulamog, the Infinite Gyre",
    "Kozilek, Butcher of Truth",
    "Kozilek, the Great Distortion",
    "Blightsteel Colossus",
    "Inkwell Leviathan",
    "Jin-Gitaxias, Core Augur",
    "Vorinclex, Voice of Hunger",
    # M11 titan cycle — Reanimator / Show-and-Tell targets
    "Sun Titan",
    "Frost Titan",
    "Grave Titan",
    "Inferno Titan",
    "Primeval Titan",
    # Omniscience: 10-mana enchantment, only ever cheated in via Show
    # and Tell or Hypergenesis. Mono-U printed but lives in any shell.
    "Omniscience",
}

# Lands that copy ANY target land in play, including the opponent's
# basics. When a player controls one of these, basic lands in their
# observed bag may actually be temporary copies of the opponent's
# basics — Lands deck routinely Stage-copies an opponent's Island to
# act as a fixer or to enable Dark Depths combo. Without this guard,
# a BG Lands player who Stages an opponent's Island gets credited a
# spurious U colour signal, and the classifier falsely diagnoses them
# as UG Lands (which then falls to "Blue Artifacts" once Lands is
# vetoed by colour cap).
_LAND_COPY_CARDS = {
    "Thespian's Stage",
    "Vesuva",
}

# Cards with hybrid mana costs ({U/R}, {W/B}, etc.). MTGO encodes both
# pips in the card's ``colors`` field, so a Pinnacle Emissary in a
# mono-blue Affinity deck looks like a UR card — and the classifier
# then tags the whole bag as Izzet Artifacts. Hybrid cards can be cast
# with either colour, so neither pip is real evidence of the deck's
# mana base; the rule should match the actual coloured spells the deck
# casts elsewhere.
#
# Same suppression treatment as ``_WEAK_COLOUR_SIGNAL_CARDS``: when
# inferring colour identity from observed / cast bags, skip these
# entries entirely. The real-vs-hybrid distinction is what separates
# Affinity (mono-U, hybrids only) from Izzet Artifacts (real Lightning
# Bolt / Pyroblast / Goblin Welder casts).
_HYBRID_MANA_CARDS = {
    "Pinnacle Emissary",          # {U/R}{U/R}
    "Cori-Steel Cutter",          # {U/R}
    "Boros Reckoner",             # {R/W}{R/W}{R/W}
    "Demigod of Revenge",         # {B/R}×5
    "Anathemancer",               # {B/R}{B/R}
    "Shedemon, Roilmaw of Geth",  # {U/B}{U/B}{U/B} (if seen)
    # NOTE: don't add Phyrexian-mana cards here (Mental Misstep, etc.)
    # — those already live in _WEAK_COLOUR_SIGNAL_CARDS.
}

# Game-generated tokens and emblem-like state objects. These appear in
# the parser's bag because of "X's [Token] creates...", "X attacks
# with [Token]", etc. — but they're not deck cards.
_TOKENS = {
    "Orc Army Token", "Samurai Token", "Skeleton Token", "Clue Token",
    "Treasure Token", "Food Token", "Goblin Token", "Soldier Token",
    "Spirit Token", "Zombie Token", "Servo Token", "Thopter Token",
    "Dwarf Token", "Goat Token", "Beast Token", "Snake Token",
    "Insect Token", "Wolf Token", "Bird Token", "Cat Token",
    "Saproling Token", "Elemental Token", "Dragon Token",
    "Angel Token", "Knight Token", "Demon Token", "Frog Token",
    "Eldrazi Token", "Plant Token", "Construct Token",
    "Copy Token", "Emblem Token", "Monk Token",
    # Game-state objects, not real cards
    "The Initiative", "The Monarch", "Undercity",
}


_BASIC_LAND_COLORS: dict[str, str] = {
    "Plains": "W", "Island": "U", "Swamp": "B",
    "Mountain": "R", "Forest": "G", "Wastes": "",
    "Snow-Covered Plains": "W", "Snow-Covered Island": "U",
    "Snow-Covered Swamp": "B", "Snow-Covered Mountain": "R",
    "Snow-Covered Forest": "G",
}


def infer_color_identity(
    observed: list[str],
    card_colors: dict[str, str],
    cast_cards: list[str] | None = None,
) -> str:
    """Colour identity = mana-producing lands + spells the player CAST.

    Two evidence sources, unioned:

    1. **Mana-producing lands** observed in the bag. Basic lands and
       ``NONBASIC_LAND_COLORS`` entries (duals, surveil lands, channel,
       triomes, verge, horizon cycle). Fetchlands are intentionally
       excluded — a Dimir deck can run Bloodstained Mire just to
       fetch Underground Sea, so its BR printed colour would lie.

       **Thespian's Stage / Vesuva exception.** When the player
       controls one of these "copy any land" lands, basic lands in
       their bag may not actually be theirs — Thespian's Stage can
       copy the OPPONENT's Island (or Mountain, etc.) and appear on
       the player's side. Lands deck commonly does exactly this with
       Dark Depths. To avoid Nazka-style false UG-Lands diagnoses,
       basic-land colour signal is only counted when the player also
       casts a spell of that colour OR has a non-basic dual of that
       colour. A lone Island in a Lands player's bag without any blue
       spell or non-basic blue dual is treated as a Stage-copy.

    2. **Cards the player actually CAST.** ``cast_cards`` is the
       subset of the bag where the actor paid the mana cost (verb
       ``cast`` / ``flashback`` / ``cycle``). Cards in the bag but NOT
       in this list — Show-and-Telled Atraxa, reanimated Griselbrand,
       hand-revealed Swords to Plowshares, milled Tarmogoyf — contribute
       nothing, because nobody spent mana on them so they don't pin
       down the mana base.

    Even when cast, ``_WEAK_COLOUR_SIGNAL_CARDS`` are filtered:
    Phyrexian-mana cards (pay 2 life, no coloured pip), free pacts
    (0-mana cost), companions (cast via Cavern of Souls), and
    cheat-into-play fatties that occasionally get hard-cast.
    """
    cast = set(cast_cards or ())
    obs_set = set(observed)
    seen: set[str] = set()

    # Detect "land-copy" cards. When present, basic lands are suspect.
    has_land_copier = bool(obs_set & _LAND_COPY_CARDS)

    # Pre-compute the "reinforced" colours — colours we have strong
    # independent evidence for, ignoring basics-when-Stage-is-out and
    # utility lands. Used below to decide whether to trust the weaker
    # signals (basics under Stage, utility lands).
    #
    # Strong signals: cast spells + non-utility duals (real fixers).
    reinforced: set[str] = set()
    for c in cast:
        if c in _BASIC_LAND_COLORS or c in NONBASIC_LAND_COLORS:
            continue
        if c in _WEAK_COLOUR_SIGNAL_CARDS:
            continue
        if c in _HYBRID_MANA_CARDS:
            # Hybrid cost — castable with either colour, so neither
            # pip is solid evidence of the deck's mana base.
            continue
        for col in card_colors.get(c, ""):
            if col in "WUBRG":
                reinforced.add(col)
    for c in obs_set:
        # Only STRONG multi-colour duals / triomes count as
        # reinforcement — mono-coloured utility lands (Karakas /
        # Otawara / Bojuka Bog) and bi-colour budget duals (surveil
        # cycle / verge cycle / canopy cycle) are exactly what we're
        # trying to filter. Original ABU duals (Tundra, Underground
        # Sea, Bayou) and triomes are NOT in either weak set, so they
        # reinforce normally.
        if (
            c in NONBASIC_LAND_COLORS
            and c not in _UTILITY_LAND_COLORS
            and c not in _WEAK_DUAL_LAND_COLORS
        ):
            for col in NONBASIC_LAND_COLORS[c]:
                if col in "WUBRG":
                    reinforced.add(col)

    # Pass 1: mana-producing lands.
    for c in obs_set:
        if c in _BASIC_LAND_COLORS:
            col = _BASIC_LAND_COLORS[c]
            if not col:
                continue
            # Basic land: only signals colour if (a) no land-copier is
            # on the table, or (b) the same colour is reinforced by a
            # cast spell / non-utility dual.
            if has_land_copier and col not in reinforced:
                continue
            seen.add(col)
            continue
        if c in _UTILITY_LAND_COLORS:
            # Utility land: needs reinforcement to count. A lone
            # Karakas in a Lands deck doesn't make the deck white.
            col = _UTILITY_LAND_COLORS[c]
            if col in reinforced:
                seen.add(col)
            continue
        if c in _WEAK_DUAL_LAND_COLORS:
            # Bi-colour budget dual: each pip needs reinforcement.
            # A mono-B deck with 4 Undercity Sewers and no blue spell
            # ever cast contributes B only, not U.
            for col in NONBASIC_LAND_COLORS.get(c, ""):
                if col in "WUBRG" and col in reinforced:
                    seen.add(col)
            continue
        if c in NONBASIC_LAND_COLORS:
            # Real dual / triome — always credit.
            for col in NONBASIC_LAND_COLORS[c]:
                if col in "WUBRG":
                    seen.add(col)

    # Pass 2: spells the player actually cast.
    for c in cast:
        if c in _BASIC_LAND_COLORS or c in NONBASIC_LAND_COLORS:
            continue
        if c in _WEAK_COLOUR_SIGNAL_CARDS:
            continue
        if c in _HYBRID_MANA_CARDS:
            # Hybrid cost — castable with either pip's colour, so we
            # can't tell which one the player actually paid. Treat as
            # colourless for identity purposes.
            continue
        for col in card_colors.get(c, ""):
            if col in "WUBRG":
                seen.add(col)

    return "".join(sorted(seen, key="WUBRG".index))


def _deck_unique_cards(deck: dict) -> set[str]:
    out = {_card_name(e) for e in deck.get("main_deck", [])}
    out.update(_card_name(e) for e in deck.get("sideboard", []))
    out.discard("")
    return out


def recompute_deck_color_identity(deck: dict) -> str:
    """Recompute a corpus deck's colour identity using the SAME rule
    as ``infer_color_identity`` applies to observed bags: lands +
    non-cheat non-land cards.

    The corpus JSON ships a precomputed ``color_identity`` that unions
    EVERY non-land card. That's wrong for shells with Show-and-Tell /
    Reanimate / companion targets — Atraxa (WUBRG) makes a true UR
    Sneak and Show deck look 5C, Yorion (WU) makes a Naya deck look 4c,
    and the colour gate then rejects the right archetype.

    Excluding ``_WEAK_COLOUR_SIGNAL_CARDS`` from the colour-identity
    union mirrors the rule we apply to the user's cast bag — any card
    that's only ever cheated into play, never genuinely cast for its
    mana cost, doesn't count toward identity.
    """
    seen: set[str] = set()

    # First pass — compute the colours reinforced by real spells +
    # multi-colour duals. Utility lands get credited in the second
    # pass only when reinforced (a Karakas in a deck whose only main
    # cards are green spells doesn't make the deck white).
    reinforced: set[str] = set()
    for entry in deck.get("main_deck", []):
        name = _card_name(entry)
        if not name:
            continue
        if name in _BASIC_LAND_COLORS or name in NONBASIC_LAND_COLORS:
            # Lands are handled in pass 2.
            continue
        if name in _WEAK_COLOUR_SIGNAL_CARDS:
            continue
        if name in _HYBRID_MANA_CARDS:
            # Hybrid pip — castable with either colour, so neither is
            # solid evidence of the deck's identity. Same suppression
            # as in infer_color_identity.
            continue
        if len(entry) >= 4 and entry[3].strip() == "LAND":
            continue
        for col in _card_colors(entry):
            if col in "WUBRG":
                reinforced.add(col)
    # Multi-colour STRONG duals count as reinforcement (real fixers).
    # Weak budget duals (surveil / verge / canopy) need reinforcement
    # themselves, so they don't get to provide it.
    for entry in deck.get("main_deck", []):
        name = _card_name(entry)
        if (
            name in NONBASIC_LAND_COLORS
            and name not in _UTILITY_LAND_COLORS
            and name not in _WEAK_DUAL_LAND_COLORS
        ):
            for col in NONBASIC_LAND_COLORS[name]:
                if col in "WUBRG":
                    reinforced.add(col)

    for entry in deck.get("main_deck", []):
        name = _card_name(entry)
        if not name:
            continue
        if name in _BASIC_LAND_COLORS:
            col = _BASIC_LAND_COLORS[name]
            if col:
                seen.add(col)
            continue
        if name in _UTILITY_LAND_COLORS:
            # Utility land: only credit if reinforced (see pass 1).
            col = _UTILITY_LAND_COLORS[name]
            if col in reinforced:
                seen.add(col)
            continue
        if name in _WEAK_DUAL_LAND_COLORS:
            # Budget dual: each pip needs reinforcement.
            for col in NONBASIC_LAND_COLORS.get(name, ""):
                if col in "WUBRG" and col in reinforced:
                    seen.add(col)
            continue
        if name in NONBASIC_LAND_COLORS:
            for col in NONBASIC_LAND_COLORS[name]:
                if col in "WUBRG":
                    seen.add(col)
            continue
        # Non-land card — contributes its printed colour unless it's
        # cheat-into-play / companion / Phyrexian / pact.
        if name in _WEAK_COLOUR_SIGNAL_CARDS:
            continue
        if name in _HYBRID_MANA_CARDS:
            continue
        # Skip generic lands marked LAND in the entry's card_type.
        # MTGO encodes the card type with trailing whitespace padding
        # ("LAND  "), so strip before comparing — otherwise utility
        # lands not in NONBASIC_LAND_COLORS (Karakas, Volrath's
        # Stronghold, Bojuka Bog, Tabernacle, Maze of Ith) fall
        # through and their printed colour (or supposed colour) gets
        # added to the deck's identity. That's what was inflating
        # Nazka's BG Lands deck to ci=WUBG.
        if len(entry) >= 4 and entry[3].strip() == "LAND":
            continue
        for col in _card_colors(entry):
            if col in "WUBRG":
                seen.add(col)
    return "".join(sorted(seen, key="WUBRG".index))


def classify_by_similarity(
    observed: list[str],
    corpus_decks: list[dict],
    weights: dict[str, float] | None = None,
    card_colors: dict[str, str] | None = None,
    min_score: float = 8.0,
    max_archetypes: int = 80,
    min_unique_cards: int = 5,
    cast_cards: list[str] | None = None,
) -> tuple[str, float, dict | None]:
    """Find the best-matching corpus deck and return its archetype.

    Filtering chain (in order, most-restrictive first):
      1. Hard vetoes: ``(Yorion)`` decks require Yorion in observed;
         ``Beanstalk`` archetypes require ``Up the Beanstalk``.
      2. Colour identity match (observed colours ⊆ deck colours, at
         most one extra deck colour).
    Falls back to looser filters if nothing matches with enough evidence.
    """
    obs = set(observed)
    if not obs or not corpus_decks:
        return SKIP_LABEL, 0.0, None
    if weights is None:
        weights = build_card_weights(corpus_decks)
    if card_colors is None:
        card_colors = build_card_colors(corpus_decks)

    # Build the full land-name set for this corpus. The bare
    # ``_LAND_NAMES`` constant only covers basics; we extend it with
    # every name tagged ``card_type == "LAND"`` in any corpus deck plus
    # the explicit non-basic dual table. This becomes the "lands"
    # exclusion set used by the distinctive-card check below — a bag
    # whose only overlap with a corpus deck is shared lands is NOT
    # evidence of that archetype.
    corpus_lands: set[str] = set(_LAND_NAMES) | set(NONBASIC_LAND_COLORS.keys())
    for d in corpus_decks:
        for entry in d.get("main_deck", []) + d.get("sideboard", []):
            # MTGO encodes card types with trailing whitespace padding
            # ("LAND  "), so strip before comparing.
            if len(entry) >= 4 and entry[3].strip() == "LAND":
                nm = _card_name(entry)
                if nm:
                    corpus_lands.add(nm)

    # Too-little-data gate: if we didn't see enough unique non-basic /
    # non-token cards, the user has been explicit that we should NOT
    # guess. These matches are dropped from every aggregation rather
    # than labelled "Unknown" — they simply don't appear.
    informative = obs - _LAND_NAMES - _TOKENS
    if len(informative) < min_unique_cards:
        return SKIP_LABEL, 0.0, None

    observed_colors = set(
        infer_color_identity(observed, card_colors, cast_cards)
    )

    # Custom user-defined rules fire FIRST. When one matches, the bag
    # is labelled immediately — the corpus matcher never runs. This is
    # how personal brews missing from the MTGO Challenge corpus get a
    # name (e.g. Bant Uro).
    custom = _custom_rule_label(obs, observed_colors)
    if custom is not None:
        # Custom rules don't pass through Midrange normalisation — they're
        # explicit names the user wrote (e.g. Bant Uro) and should appear
        # verbatim.
        return custom, 100.0, None

    # Hard "signature card" requirements per archetype.
    #
    # Schema is AND-of-OR: outer list = AND (all groups must be
    # satisfied), inner list = OR (any one card in the group needs to
    # be observed). So
    #     "Omni-Tell": [["Show and Tell"], ["Omniscience"]]
    # means we need BOTH a Show and Tell AND an Omniscience seen.
    # While
    #     "Delver": [["Dragon's Rage Channeler", "Delver of Secrets"]]
    # means we need at least one of those two creatures.
    #
    # If the observed bag fails any group, the deck cannot be classified
    # as that archetype regardless of how many other cards overlap.
    SIGNATURE_REQUIREMENTS: dict[str, list[list[str]]] = {
        # Pure-card decks (one specific card defines the archetype).
        "Omni-Tell": [["Show and Tell"], ["Omniscience"]],
        "Sneak and Show": [["Show and Tell"], ["Sneak Attack"]],
        "Sneak and Show with Reanimate": [["Show and Tell"], ["Reanimate", "Animate Dead", "Exhume"]],
        "Show and Tell": [["Show and Tell"]],
        "Doomsday": [["Doomsday"]],
        "Painter": [["Painter's Servant"]],
        "Aluren": [["Aluren"]],
        # Cradle Control's defining lands sometimes don't get to play
        # before the player concedes. Engine creatures (Wight / Knight
        # / Elvish Reclaimer / Ignoble Hierarch) are equally diagnostic
        # — the deck can't function without one of them in any game.
        "Cradle Control": [[
            "Gaea's Cradle",
            "Wight of the Reliquary",
            "Knight of the Reliquary",
            "Elvish Reclaimer",
            "Ignoble Hierarch",
        ]],
        "Cephalid Breakfast": [["Cephalid Illusionist"]],
        "Worldgorger Combo": [["Worldgorger Dragon"]],
        "Affinity Stompy": [["Arcbound Ravager"]],
        # Lands is the GW (occasionally splash) Loam-engine deck. The
        # diagnostic property is what it DOESN'T run — classic Lands has
        # essentially no creatures, just utility lands + Loam + Crop
        # Rotation + Punishing Fire / Maze of Ith. Any 1-2 mana
        # creature in the bag is a strong tell it's a different shell
        # (Maverick, Selesnya Depths, Knight Lands, etc.), so we list
        # those creatures as DOES-NOT-CONTAIN below.
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
        # Newly added:
        "Monastery Mentor": [["Monastery Mentor"]],
        "Delver": [["Dragon's Rage Channeler", "Delver of Secrets"]],
        "Death's Shadow": [["Death's Shadow"]],
        # Reanimator is defined by the FATTY, not the cheap reanimation
        # spell. A Pox / Jund / Nic Fit bag with a one-of Reanimate to
        # bring back Grist or Liliana isn't Reanimator — it's a midrange
        # deck. So we require BOTH:
        #   (1) a reanimation spell, AND
        #   (2) an actual reanimation target the deck wins with.
        # Bags that have (1) but not (2) fall through to whatever else
        # matches, or land on the colour-code label.
        "Reanimator": [
            ["Reanimate", "Animate Dead", "Exhume", "Goryo's Vengeance",
             "Shallow Grave", "Unmarked Grave", "Persist"],
            ["Griselbrand", "Archon of Cruelty", "Atraxa, Grand Unifier",
             "Chancellor of the Annex", "Serra's Emissary",
             "Iona, Shield of Emeria", "Sphinx of the Steel Wind",
             "Worldspine Wurm", "Tidespout Tyrant",
             "Jin-Gitaxias, Core Augur", "Elesh Norn, Mother of Machines",
             "Sire of Insanity", "Reya Dawnbringer"],
        ],
        "Dimir Reanimator": [
            ["Reanimate", "Animate Dead", "Exhume"],
            ["Griselbrand", "Archon of Cruelty", "Atraxa, Grand Unifier",
             "Chancellor of the Annex", "Serra's Emissary",
             "Iona, Shield of Emeria", "Sphinx of the Steel Wind",
             "Jin-Gitaxias, Core Augur", "Tidespout Tyrant"],
        ],
        "Initiative": [["Seasoned Dungeoneer", "White Plume Adventurer",
                        "Caves of Chaos Adventurer"]],
        "Boros Initiative": [["Seasoned Dungeoneer", "White Plume Adventurer",
                              "Caves of Chaos Adventurer"]],
        "Naya Initiative": [["Seasoned Dungeoneer", "White Plume Adventurer",
                             "Caves of Chaos Adventurer"]],
        "Tron": [["Urza's Mine", "Urza's Tower", "Urza's Power Plant"]],
        "Eldrazi": [["Thought-Knot Seer", "Reality Smasher", "Eldrazi Mimic",
                     "Eldrazi Temple", "Endless One", "Matter Reshaper"]],
        "Storm": [["Past in Flames", "Tendrils of Agony", "Cabal Ritual"]],
        # Bare "Cradle" matches as a substring of "Cradle Control",
        # so it MUST accept the same engine-creature list — otherwise
        # the substring rule re-tightens the requirement and rejects
        # the deck even when "Cradle Control" alone would pass.
        "Cradle": [[
            "Gaea's Cradle",
            "Wight of the Reliquary",
            "Knight of the Reliquary",
            "Elvish Reclaimer",
            "Ignoble Hierarch",
        ]],
        "Goblins": [["Goblin Lackey", "Goblin Recruiter", "Goblin Charbelcher",
                     "Goblin Piledriver", "Goblin Guide"]],
        "Maverick": [["Knight of the Reliquary"]],
        "Merfolk": [["Lord of Atlantis", "Master of the Pearl Trident",
                     "Master of Waves"]],
        "Burn": [["Lava Spike", "Eidolon of the Great Revel", "Goblin Guide"]],
        "Nic Fit": [["Veteran Explorer"]],
        "Smallpox": [["Smallpox"]],
        # Loam Pox: the deck's identity is the discard engine. Any ONE
        # of these cards is sufficient — modern Loam Pox can run
        # Eumidian Hatchery instead of Life from the Loam, and the
        # classic version doesn't always cast Smallpox in every game.
        "Loam Pox": [[
            "Smallpox",
            "Life from the Loam",
            "Eumidian Hatchery",
        ]],
        "Ninjas": [["Ninja of the Deep Hours", "Moon-Circuit Hacker",
                    "Ingenious Infiltrator", "Throat Slitter"]],
        "Dimir Ninjas": [["Ninja of the Deep Hours", "Moon-Circuit Hacker",
                          "Ingenious Infiltrator"]],
        # Depths-shell archetypes: the combo (Dark Depths +
        # Thespian's Stage) doesn't fire every game, and the deck's
        # real identity comes from the engine creatures it uses to
        # tutor those lands. Any ONE of these signature cards is
        # enough — the colour gate and similarity scoring decide
        # between Selesnya / Sultai / 4c Depths variants.
        "Selesnya Depths": [[
            "Dark Depths", "Thespian's Stage", "Vampire Hexmage",
            "Knight of the Reliquary", "Elvish Reclaimer",
        ]],
        "Sultai Depths": [[
            "Dark Depths", "Thespian's Stage", "Vampire Hexmage",
            "Knight of the Reliquary", "Elvish Reclaimer",
        ]],
        "4c Depths": [[
            "Dark Depths", "Thespian's Stage", "Vampire Hexmage",
            "Knight of the Reliquary", "Elvish Reclaimer",
        ]],
        "Depths": [[
            "Dark Depths", "Thespian's Stage", "Vampire Hexmage",
            "Knight of the Reliquary", "Elvish Reclaimer",
        ]],
        "Stoneblade": [["Stoneforge Mystic"]],
        "Azorius Stoneblade": [["Stoneforge Mystic"]],
        "Orzhov Stoneblade": [["Stoneforge Mystic"]],
        "Esper Stoneblade": [["Stoneforge Mystic"]],
        "Death & Taxes": [["Aether Vial", "Thalia, Guardian of Thraben"]],
        "Zenith Combo": [["Green Sun's Zenith", "Natural Order"]],
        "Zenith": [["Green Sun's Zenith", "Natural Order"]],
        "Landfall": [["Lotus Cobra", "Scute Swarm", "Omnath, Locus of Creation",
                      "Felidar Retreat"]],
        "Golgari Landfall": [["Lotus Cobra", "Scute Swarm", "Omnath, Locus of Creation"]],
        "Stax": [["Smokestack", "Trinisphere", "Sphere of Resistance",
                  "Chalice of the Void"]],
        "Mono-White Stax": [["Smokestack", "Trinisphere"]],
        # Cascade decks REQUIRE the cascade payoff. Without it, the
        # deck is just a 3/4-colour shell that happens to share lots
        # of cantrips and fetches with other tempo / control brews.
        "Cascade Rhinos": [["Crashing Footfalls"]],
        "Cascade": [["Crashing Footfalls", "Shardless Agent", "Glimpse the Cosmos"]],
        "Rhinos": [["Crashing Footfalls"]],
        # Oops! All Spells requires a mill-yourself enabler (cast a
        # creature, your library is all spells, so it mills out). Without
        # one of these the deck can't function; shell overlap on Chrome
        # Mox / Lotus Petal / Once Upon a Time / Elvish Spirit Guide
        # otherwise wrongly absorbs other 5c Yorion shells.
        "Oops! All Spells": [["Balustrade Spy", "Undercity Informer"]],
    }

    # Cards whose presence DISQUALIFIES an archetype, even if all
    # positive signature groups pass. Used to encode "this deck never
    # runs X" — e.g. classic Lands runs essentially no creatures, so
    # any 1-2 mana creature in the observed bag means it's a different
    # shell (Maverick, Selesnya Depths, Knight Lands, …).
    SIGNATURE_FORBIDS: dict[str, list[str]] = {
        "Lands": [
            "Knight of the Reliquary",
            "Elvish Reclaimer",
            "Sylvan Safekeeper",
            "Dryad Militant",
            "Noble Hierarch",
            "Birds of Paradise",
            "Stoneforge Mystic",
            "Keen-Eyed Curator",
        ],
    }

    # Hard colour cap per archetype name (substring match). The
    # observed colour identity must be a subset of this set — no
    # tolerance, no "splash". Used for archetypes whose colour pool is
    # FIXED by definition: Cradle Control + Loam Pox + Smallpox never
    # touch red, period; a BRG bag claiming to be one of those decks
    # is a misclassification regardless of how much shell overlap it
    # has. This sits ABOVE the generic ±1 colour-gate tolerance.
    ARCHETYPE_MAX_COLOURS: dict[str, frozenset[str]] = {
        "Cradle Control": frozenset("WBG"),
        "Loam Pox":       frozenset("WBG"),
        "Lands":          frozenset("WBG"),
        "Smallpox":       frozenset("BG"),   # BG or mono-B, never with W
        "Maverick":       frozenset("WG"),
        # Death & Taxes: classic mono-W base, but the modern Pre-War
        # Formalwear / Lion Sash / Shadowy Backstreet variant splashes
        # B. The Boros (WR) and Selesnya (WG) splash variants have
        # their own archetype names (Boros Initiative / Maverick), so
        # WB is the only legitimate D&T extension to allow.
        "Death & Taxes":  frozenset("WB"),
        # Tron is the colourless Urza-lands ramp deck. Any coloured
        # land in the observed bag means it's actually Post (or some
        # other Locus-style variant), not pure Tron — Tron NEVER runs
        # Islands or Cloudpost.
        "Tron":           frozenset(),
        # Classic Reanimator is mono-B / BR / occasionally UBR (Grixis
        # Reanimator). Never G — a BRG bag with Reanimate is almost
        # always a Jund Pox / Nic Fit / brewy midrange shell, not real
        # Reanimator. Cap denies G outright.
        "Reanimator":     frozenset("WUBR"),
    }

    # Keep only the top-N most frequent archetypes in the corpus. The
    # long tail (Echo of Eons, Riddlesmith combo, one-off brews) can never
    # out-score a real meta deck this way.
    arch_counts: Counter[str] = Counter(
        d.get("archetype", "") for d in corpus_decks
    )
    kept_archetypes = {a for a, _ in arch_counts.most_common(max_archetypes)}

    has_yorion = "Yorion, Sky Nomad" in obs
    has_beanstalk = "Up the Beanstalk" in obs

    def _veto_ok(deck: dict) -> bool:
        name = deck.get("archetype", "")
        # The corpus itself contains some "Unknown"-tagged decks where
        # Badaro's rules failed during build. They must never be
        # picked as a match — that's how we'd end up labelling an
        # observed bag "Unknown" via a high-scoring overlap.
        if name == "Unknown" or not name:
            return False
        # Archetype must clear the support threshold (no long-tail wins).
        if name not in kept_archetypes:
            return False
        # NOTE: previously a "(Yorion)" deck was outright rejected when
        # Yorion wasn't in the observed bag. That was too strict — the
        # corpus only contains 80-card Yorion variants for archetypes
        # like Death & Taxes / Esper Stoneblade / Mono-White Initiative,
        # so the rejection left non-Yorion bags falling to junk labels
        # ("Mono-White Stompy" for actual D&T). We now KEEP the (Yorion)
        # deck in the candidate list, and ``_strip_yorion_suffix`` in
        # ``_normalise_archetype`` rewrites the label to the bare
        # archetype name when Yorion isn't observed. End result: a 60-
        # card Death & Taxes bag still wins the D&T (Yorion) corpus
        # candidate's shell, then the suffix is stripped, and the user
        # sees "Death & Taxes" — honest about both the archetype and
        # the missing companion.
        # Hard rule: a "Beanstalk" deck must include Up the Beanstalk.
        if "Beanstalk" in name and not has_beanstalk:
            return False
        # Generic signature-card rules. Match against the base archetype
        # name (with colour prefix stripped) so e.g. "Mono-Black
        # Doomsday" still triggers the "Doomsday" requirement.
        for arch_name, groups in SIGNATURE_REQUIREMENTS.items():
            if arch_name in name:
                for group in groups:
                    # Each group needs ≥1 of its alternatives observed.
                    if not any(card in obs for card in group):
                        return False
        # Negative signature: any forbidden card observed disqualifies.
        for arch_name, forbids in SIGNATURE_FORBIDS.items():
            if arch_name in name:
                if any(card in obs for card in forbids):
                    return False
        # Hard colour cap per archetype. Cradle Control / Loam Pox /
        # Lands / Smallpox have a fixed colour pool by definition —
        # adding red (or any other off-list colour) means it's NOT
        # that archetype, no matter how much shell it shares.
        for arch_name, max_colours in ARCHETYPE_MAX_COLOURS.items():
            if arch_name in name:
                if not observed_colors.issubset(max_colours):
                    return False
        return True

    # Archetype-name suffixes too broad to allow colour extras. These
    # come from Badaro's broad "Midrange" archetype (matches any Uro
    # deck) and the Aggro/Stompy fallback rules. Without strict
    # matching, a 3-colour observed bag could falsely claim a 4-colour
    # "X Midrange" corpus deck via the +1 extras tolerance.
    #
    # "Control" is intentionally NOT here — the Control fallback's
    # CommonCards (Snapcaster, Teferi, Narset, Leyline Binding, etc.)
    # are genuinely discriminative, so it's a legitimate label even
    # when the user's bag is one colour short of the corpus deck.
    _STRICT_COLOR_SUFFIXES = (" Midrange", " Aggro", " Stompy")

    def _color_ok(deck: dict) -> bool:
        if not observed_colors:
            return True
        # Hard rule: an archetype name with a colour prefix that names
        # specific colours requires ALL of those colours to be observed.
        # "Sultai Tempo" is U+B+G — observing only B+G can't be Sultai
        # under any tolerance. This rule fires BEFORE the deck-colour
        # comparison, so a BG-observed bag can never resolve to a
        # Sultai/Grixis/Bant/etc. archetype just because the corpus
        # deck happens to have ci=UBG.
        name = deck.get("archetype", "")
        name_prefix = colour_prefix(name)
        if name_prefix is not None:
            required = colour_prefix_to_set(name_prefix)
            if required and not required.issubset(observed_colors):
                return False
        deck_colors = set(deck.get("color_identity", ""))
        if not deck_colors:
            # Colourless deck (Tron, Eldrazi, Mono-Brown Stax). Sideboard
            # cards like Leyline of the Void make the observed bag look
            # mono-B / mono-W / etc. Don't filter colourless archetypes
            # on colour identity — the signature-card requirement (e.g.
            # Tron needs an Urza land) gates them already.
            return True
        extras_in_deck = deck_colors - observed_colors
        extras_in_observed = observed_colors - deck_colors
        # Generic-suffix archetypes (Midrange / Aggro / Stompy) need
        # EXACT colour match — the corpus is too lax with those names.
        if any(name.endswith(s) for s in _STRICT_COLOR_SUFFIXES):
            return not extras_in_deck and not extras_in_observed
        # Otherwise allow at most 1 colour of slack on each side:
        # observed can have 1 colour the deck doesn't list (the user
        # splashed something into a typically-2c deck — e.g. BRG Loam
        # Pox against the corpus's BG Loam Pox) and the deck can have
        # 1 colour the user didn't cast this match (a short Grixis
        # game where no red spell happened to be cast).
        return len(extras_in_deck) <= 1 and len(extras_in_observed) <= 1

    def _score(decks: list[dict]) -> tuple[float, dict | None]:
        best_s, best_d = 0.0, None
        for deck in decks:
            dunique = _deck_unique_cards(deck)
            overlap = obs & dunique
            if not overlap:
                continue
            score = sum(weights.get(c, 1.0) for c in overlap)
            if score > best_s:
                best_s, best_d = score, deck
        return best_s, best_d

    # Strict pass: every gate has to clear (top-N support, signature
    # cards, Yorion/Beanstalk hard rules, colour identity). If no deck
    # clears, we'd rather show the colour code than guess.
    candidates = [d for d in corpus_decks if _veto_ok(d) and _color_ok(d)]

    # Two-tier colour preference, but only when the exact-match
    # candidate is COMPETITIVELY scored vs the overall best.
    #
    #   Tier 1: decks whose colour identity EXACTLY matches observed.
    #     WUR observed should resolve to Jeskai Control, not Azorius
    #     Control (WU) — colour identity is the defining feature.
    #
    #   Tier 2: tolerance-match decks. Used when no exact-match deck
    #     scores well enough, OR when a tolerance-match deck scores
    #     SUBSTANTIALLY higher than the best exact-match (e.g. a BG
    #     observed bag where the best exact match is some random
    #     "Post" deck at score 10 while the actual deck — Cradle
    #     Control at ci=WBG with a W splash — scores 81).
    #
    # The "substantially higher" threshold: tolerance wins only when
    # its score is BOTH ≥ 1.5× the exact score AND at least 20 points
    # ahead. Otherwise we trust the exact-colour signal.
    if observed_colors:
        bg_cands = [d for d in candidates if d.get("archetype")=="Cradle Control"]
        exact_candidates = [
            d for d in candidates
            if set(d.get("color_identity", "")) == observed_colors
        ]
        best_exact_score, best_exact_deck = _score(exact_candidates)
        best_overall_score, best_overall_deck = _score(candidates)
        prefer_overall = (
            best_overall_deck is not None
            and best_exact_deck is not best_overall_deck
            and best_overall_score >= 1.5 * max(best_exact_score, 1.0)
            and best_overall_score >= best_exact_score + 20.0
        )
        if best_exact_deck is not None and best_exact_score >= min_score and not prefer_overall:
            best_score, best_deck = best_exact_score, best_exact_deck
        else:
            best_score, best_deck = best_overall_score, best_overall_deck
    else:
        best_score, best_deck = _score(candidates)

    colour_label = (
        "".join(sorted(observed_colors, key="WUBRG".index))
        or "Colourless"
    )

    # Distinctive-card check: the winning deck must share at least one
    # high-IDF NON-LAND card (weight ≥ 3.0 ≈ in <10% of corpus). If only
    # staples (Brainstorm, Force of Will) AND/OR lands overlap, the
    # score is on the shell — not actual archetype evidence.
    #
    # Lands are deliberately excluded from the distinctive check: a BRG
    # bag sharing Mountain + Taiga + Wooded Foothills with a Creative
    # Technique deck is NOT evidence the user is playing Creative
    # Technique. The user wants colour-code fallback in that scenario.
    #
    # EXCEPTION: when the observed colour identity is an EXACT match
    # for the deck's identity (no missing, no extra colours), the
    # colour signal itself is enough evidence. A UBR bag with 11 shared
    # cards is unmistakably Grixis even if all 11 are Brainstorm /
    # Bowmasters / Thoughtseize / Force of Will / fetches — the shell
    # IS the differentiator at that point.
    if best_deck is not None and best_score >= min_score:
        overlap = obs & _deck_unique_cards(best_deck)
        non_land_overlap = overlap - corpus_lands
        deck_colors = set(best_deck.get("color_identity", ""))
        exact_colour = deck_colors and observed_colors == deck_colors
        if (
            exact_colour
            or any(weights.get(c, 0.0) >= 3.0 for c in non_land_overlap)
        ):
            archetype = best_deck.get("archetype", colour_label)
            archetype = _normalise_archetype(
                archetype, obs, observed_colors, arch_counts,
                set(best_deck.get("color_identity", "")),
            )
            return archetype, best_score, best_deck

    return colour_label, best_score, best_deck
