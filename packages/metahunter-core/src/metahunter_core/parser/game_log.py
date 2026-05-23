"""Parser for MTGO Match_GameLog_<GUID>.dat files.

The file is a binary container that frames text payloads from the
in-game "game log" chat channel. Every event line starts with ``@P``
followed by the acting player's username. Card references inside a
line are encoded as ``@[CardName@:CARD_ID,GAME_OBJECT_ID:@]``.

We do not attempt to reverse-engineer the binary framing. Decoding the
raw bytes as latin-1 keeps the ASCII markers intact, and regexes do the
rest. Card names with non-ASCII glyphs (e.g. "Lórien Revealed") are
re-decoded as UTF-8 after extraction.

Attribution
-----------
A card is attributed to a player only when the log explicitly states
the player did something with it that proves ownership. The four
sources of evidence we trust:

  1. The actor cast / played / activated / discarded / sacrificed it.
     ``@PAFKatto casts @[Lightning Bolt@:...]``  →  Bolt is AFKatto's.

  2. The card is referred to with the possessive "X's [Card]".
     ``@PAFKatto's @[Orcish Bowmasters@:...] creates an Orc Army Token``
     →  Bowmasters is AFKatto's.

  3. The card appears in a "X reveals N cards with [Spell]: A, B, C"
     list — A, B, C belong to the OPPONENT (it's a Thoughtseize-like
     reveal). [Spell] itself belongs to X.

  4. The card is named as a graveyard-resident reanimation target
     ("X reanimates @[Card]") — X is reanimating their own card.

Everything else — "targeting [X]", "destroys [X]", "counters [X]",
"exiles [X]" — is dropped. Those cards are already credited to their
true owner from a prior cast/play event in the same game.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

# Bumped whenever any attribution rule in this module changes so the
# store can auto-trigger a full re-ingest of existing rows. Without the
# bump, ``ingest_all`` would skip files whose mtime/size hasn't changed
# (most of them) and the user would keep seeing data parsed with the
# old rules. Increment by 1 for every behavioural change here.
#
# History
#   1  initial schema
#   2  hyphens allowed in usernames (doctor-x, Sol-e22558); FoW pitch
#      attribution; generalized reanimation suppression
#   3  cards_cast_by_player tracked separately from cards_by_player
#   4  no behavioural parser change, but the corpus colour-identity
#      recomputation downstream means cached classifications need to
#      refresh — easiest to do via a full re-ingest
#   5  colour inference reverted to LANDS ONLY (no spell contribution
#      whatsoever); Show-and-Tell variants named from mana base
#   6  colour inference = lands + CAST spells (paid-mana spells, not
#      the full bag — show-and-telled fatties and revealed-not-cast
#      cards still don't pollute identity); recompute corpus identity
#      consistently so colour gate compares apples to apples; ±3-game
#      neighbour window with plurality fallback in session inference
#   7  per-match format detection (Legacy vs Vintage) — friends who
#      play Vintage no longer pollute the Legacy dashboard
#   8  format now comes from MTGO's own mtgo_game_history file
#      (authoritative) when an entry matches by mtime; card-based
#      Power-9 detection is only a fallback for the few matches
#      MTGO didn't record
#   9  VINTAGE_ONLY_CARDS audited: Lodestone Golem (Legacy-legal) and
#      a few other false positives removed; old false-positive Vintage
#      flags get cleared on the auto-reingest
#  10  Card-based Vintage detection removed entirely. Format comes
#      from mtgo_game_history (authoritative); anything MTGO didn't
#      record stays at the default "Legacy" rather than being guessed
#      from cards. No more parallel heuristic fighting the truth.
#  11  Pitch-counter exile attribution now handles MTGO's two log
#      shapes (with vs without "'s ability") and credits the pitched
#      card to the actual caster in both cases.
#  12  Pitch-counter exile attribution now uses proximity matching
#      against the nearby "casts <spell> by paying" event rather than
#      trying to guess from the exile line's actor — handles every
#      MTGO log shape uniformly.
#  13  Pitch-counter exile attribution uses SEQUENTIAL pairing (exile
#      always precedes its cast), so a Force-of-Will-on-Force-of-Will
#      exchange no longer cross-pairs and misattributes pitched cards.
#  14  Elvish Spirit Guide / Simian Spirit Guide added to
#      _WEAK_COLOUR_SIGNAL_CARDS so the corpus's Oops! All Spells
#      stops being tagged as 4-colour because of those ramp cards.
#  15  Free-cast suffix ("without paying its mana cost") detection:
#      casts under Omniscience / Cunning Wish / cascade no longer
#      count toward colour-identity inference. Plus textual-Omniscience
#      rescue: the card never appears as a @[Omniscience@:] token in
#      MTGO logs, so we scan for "with Omniscience" and add it to the
#      observed bag so the Omni-Tell signature requirement fires.
PARSER_VERSION = 16

CARD_RE = re.compile(r"@\[([^@\]]+?)@:(\d+),(\d+):@\]")

# MTGO usernames can contain letters, digits, underscore, and hyphen
# (e.g. "doctor-x", "Sol-e22558"). The hyphen was being dropped, which
# truncated those names mid-string in JOIN_RE / POSSESS_RE / ACTOR_RE
# and caused those matches to be invisible in the dashboard.
_NAME = r"[A-Za-z0-9_-]+"

JOIN_RE = re.compile(rf"@P({_NAME}) joined the game\.")
FIRST_RE = re.compile(rf"@P({_NAME}) chooses to play first\.")
# Dice rolls from game-start coin flip. Used as a backup signal for
# the on-the-play detection when MTGO's "chooses to play first" line
# is missing from the log (truncated logs, very old matches, etc.).
DIE_ROLL_RE = re.compile(rf"@P({_NAME}) rolled a (\d+)")
TURN_RE = re.compile(rf"@PTurn (\d+): ({_NAME})")
WINS_GAME_RE = re.compile(rf"@P({_NAME}) wins the game\.")
LOSES_GAME_RE = re.compile(rf"@P({_NAME}) loses the game\.")
CONCEDE_RE = re.compile(rf"@P({_NAME}) has conceded from the game\.")
WINS_MATCH_RE = re.compile(rf"@P({_NAME}) wins the match (\d+)-(\d+)")

ACTOR_RE = re.compile(rf"@P({_NAME})")

# Verbs that prove the actor owns the card immediately following.
# The verb phrase ends with " @[", capturing only the next card token.
ACTOR_OWNS_RE = re.compile(
    r"\b(?P<verb>"
    r"casts?|"
    r"plays?|"                    # plays a land
    r"activates an ability of|"
    r"puts a triggered ability from|"
    r"discards?|"
    r"sacrifices?|"
    r"reanimates?|"
    r"cycles?|"
    r"flashbacks?|"
    r"forecasts?"
    r")\s+@\[(?P<card>[^@\]]+?)@:(?P<cid>\d+),\d+:@\]",
    re.IGNORECASE,
)

# Verbs from ACTOR_OWNS_RE that prove the actor PAID THE MANA COST of
# the card. Cards observed via these verbs contribute their colours to
# the actor's inferred colour identity. Everything else (possessives,
# reanimation, mill, sacrifice, discard, triggered abilities) only
# proves visibility, not mana payment — so it stays out of the colour
# inference. ``plays`` is here too because lands handle their own colour
# inference via NONBASIC_LAND_COLORS / _BASIC_LAND_COLORS in the
# classifier, but the verb itself is a clean ownership signal so we
# include it for consistency.
CAST_VERBS = {"cast", "casts", "flashback", "flashbacks", "cycle", "cycles"}

# "X's @[Card]" — X controls Card (typed possessive).
POSSESS_RE = re.compile(
    rf"@P(?P<actor>{_NAME})'s\s+@\[(?P<card>[^@\]]+?)@:(?P<cid>\d+),\d+:@\]"
)

# "X reveals N cards with [Spell]: [A], [B], ... ." — A, B... belong to X.
# This phrasing is used both for hand-reveal (Thoughtseize) and for
# library-mill (Undercity Informer, Balustrade Spy, Brain Freeze). The
# revealing player X is always the owner of the revealed cards in MTGO.
REVEAL_LIST_RE = re.compile(
    r"\breveals?\s+\d+\s+cards?\s+with\s+@\[[^@\]]+@:\d+,\d+:@\]\s*:"
)
# "X reveals their hand to [Spell], containing [A], [B], [C] ." — also X's.
REVEAL_HAND_RE = re.compile(
    r"\breveals?\s+their\s+hand\s+to\s+@\[[^@\]]+@:\d+,\d+:@\]\s*,\s*containing"
)

# Spells whose only effect is to reanimate / yoink from the OPPONENT's
# graveyard. The target of these is not the actor's deck card — it's
# the opponent's. When we see "X casts <one of these> targeting [Y]",
# Y must be excluded from X's bag (and any later "X's Y" possessives
# should be ignored too, because X just controls Y, doesn't own it).
OPPONENT_GRAVE_REANIMATORS = {
    "From the Catacombs",
    "Extraction Specialist",
}
# Reanimation spells that can target EITHER graveyard. We only suppress
# their target when our card-origin map says the target was first
# revealed / discarded / milled / cast by the OPPONENT — i.e., it lives
# in their deck. If the actor put it into a grave themselves, it's their
# own card and the verb pass already credits it correctly.
EITHER_GRAVE_REANIMATORS = {
    "Reanimate",
    "Animate Dead",
    "Exhume",
    "Goryo's Vengeance",
    "Necromancy",
    "Dance of the Dead",
    "Persist",
    "Apprentice Necromancer",
    "Doomed Necromancer",
    "Body Snatcher",
}
OPPONENT_GRAVE_CAST_RE = re.compile(
    r"\bcasts?\s+@\[(?P<spell>[^@\]]+?)@:\d+,\d+:@\]"
    r"\s+targeting\s+@\[(?P<target>[^@\]]+?)@:(?P<tid>\d+),(?P<gid>\d+):@\]",
    re.IGNORECASE,
)

# Pitch counters with an alt-cost that exiles a card from the caster's
# hand. MTGO logs the resulting exile under "@P<OPP> exiles @[<pitched>]
# with @[<spell>]" — where OPP is the player whose spell is being
# countered, NOT the player who cast the pitch counter. The pitched card
# actually belongs to OPP's opponent (the pitch-counter caster).
PITCH_COUNTERS = {
    "Force of Will",
    "Force of Negation",
    "Misdirection",
    "Commandeer",
    "Mindbreak Trap",
    "Pact of Negation",  # no exile, but listed for future use
}
# MTGO logs the pitch-cost exile in different shapes — and crucially
# the actor on the exile line is NOT reliably either the FoW caster
# or the player being countered. Sometimes it's the active player of
# the turn, sometimes the casting player, sometimes the countered
# player. So we can't use the exile-line actor at all.
#
# The reliable signal is the "by paying ... and exiling a blue card
# from your hand" CAST event that always appears nearby (typically
# immediately after the exile line, sometimes immediately before).
# That line's actor IS the FoW caster, and the pitched card belongs
# to them.
#
# At parse time we:
#   1. find every pitch-counter cast event with its position + caster
#   2. find every "exiles X with <spell>" event near a pitch counter
#   3. pair them up by proximity (closest cast within a window) and
#      attribute the pitched card to the cast's actor.
PITCH_EXILE_RE = re.compile(
    r"\bexiles?\s+@\[(?P<card>[^@\]]+?)@:(?P<cid>\d+),\d+:@\]"
    r"\s+with\s+(?:with\s+)?@\[(?P<spell>[^@\]]+?)@:\d+,\d+:@\]",
    re.IGNORECASE,
)
PITCH_CAST_RE = re.compile(
    r"@P(?P<actor>[A-Za-z0-9_-]+)\s+casts?\s+@\["
    r"(?P<spell>[^@\]]+?)@:\d+,\d+:@\]\s+by\s+paying",
    re.IGNORECASE,
)

# MTGO emits "casts @[X] without paying its mana cost with Omniscience"
# (or "with [other free-cast effect]") whenever Omniscience / Cunning
# Wish / etc. let the controller skip the mana cost. Two important
# consequences:
#   1. The cast's mana cost wasn't paid, so the card's printed colours
#      don't pin down the player's mana base. We DON'T add those cards
#      to the cast bag used for colour-identity inference.
#   2. Omniscience itself is never wrapped in @[Omniscience@:...]
#      tokens in this match — its name only appears as plain text in
#      the suffix. We rescue it by scanning for the textual suffix and
#      adding "Omniscience" to the actor's observed bag.
FREE_CAST_SUFFIX_RE = re.compile(
    r"\s+without\s+paying\s+its\s+mana\s+cost",
    re.IGNORECASE,
)
OMNI_TEXTUAL_RE = re.compile(
    r"@P(?P<actor>[A-Za-z0-9_-]+)\s+casts?\s+@\[[^@\]]+@:\d+,\d+:@\]"
    r"\s+without\s+paying\s+its\s+mana\s+cost\s+with\s+Omniscience",
    re.IGNORECASE,
)

# "X mills @[A], @[B], and @[C]." — multi-card mill. All cards listed
# come from X's own library, so they belong to X. Same shape applies to
# surveil and "puts ... from ... library into their graveyard".
MILL_LIST_RE = re.compile(r"\b(?:mills?|surveils?)\s+@\[")

# "X puts @[Card] into their graveyard." — Card is X's (was in X's
# library or hand). Only triggers for this exact phrasing; other
# graveyard-touching effects ("puts a triggered ability from", "puts a
# +1/+1 counter on") are unaffected.
PUTS_GRAVE_RE = re.compile(
    r"\bputs?\s+@\[(?P<card>[^@\]]+?)@:(?P<cid>\d+),\d+:@\]"
    r"\s+(?:from\s+(?:their|the)\s+(?:library|hand)\s+)?"
    r"into\s+(?:their|the)\s+graveyard",
    re.IGNORECASE,
)


class GameOutcome(BaseModel):
    """Outcome of a single game within a match."""

    winner: str | None = None
    loser: str | None = None
    by_concede: bool = False


class ParsedMatch(BaseModel):
    """Everything we can extract from a single Match_GameLog file."""

    match_id: str
    players: list[str] = Field(default_factory=list)
    first_player: str | None = None
    turns: int = 0
    cards_by_player: dict[str, list[str]] = Field(default_factory=dict)
    cards_unique_by_player: dict[str, list[str]] = Field(default_factory=dict)
    # Cards the player specifically CAST (or flashbacked / cycled) — i.e.
    # paid the mana cost for. Used by the classifier to infer colour
    # identity from real coloured spells, while ignoring cards that only
    # showed up via reanimation, mill, possessive, or triggered abilities
    # (none of which prove the actor had access to that card's colours).
    cards_cast_by_player: dict[str, list[str]] = Field(default_factory=dict)
    game_outcomes: list[GameOutcome] = Field(default_factory=list)
    match_winner: str | None = None
    match_score: tuple[int, int] | None = None
    # Best-effort format hint from the cards seen in the match.
    # "Legacy" is the default; "Vintage" fires when Power-Nine / other
    # Legacy-banned-but-Vintage-legal cards appear.
    format: str = "Legacy"

    @property
    def games_played(self) -> int:
        return len(self.game_outcomes)

    def wins_for(self, player: str) -> int:
        return sum(1 for g in self.game_outcomes if g.winner == player)


def _decode(raw: bytes) -> str:
    return raw.decode("latin-1", errors="ignore")


def _repair(name: str) -> str:
    """Reinterpret a latin-1-decoded string as UTF-8 (e.g. 'Lórien')."""
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _events(text: str) -> list[tuple[str, int, int]]:
    """Slice the text into per-actor events: ``(actor, start, end)``."""
    actors = list(ACTOR_RE.finditer(text))
    out: list[tuple[str, int, int]] = []
    for i, m in enumerate(actors):
        start = m.end()
        end = actors[i + 1].start() if i + 1 < len(actors) else len(text)
        out.append((m.group(1), start, end))
    return out


def _extract_cards(
    text: str, players: set[str]
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Walk the whole log and credit cards to players via evidence rules.

    Returns ``(cards, cast_cards)`` — the full observed bag plus a
    narrower bag of cards the actor specifically cast (paid the mana
    cost for). The cast bag is what colour-identity inference should
    use; the full bag is what similarity scoring uses.
    """
    cards: dict[str, list[str]] = {p: [] for p in players}
    cast_cards: dict[str, list[str]] = {p: [] for p in players}
    events = _events(text)

    # Pass -1: build a name → first-owner map.
    #
    # We walk every event in order and, when we see a card surface via an
    # owner-determining signal (an actor-owns verb, a mill / surveil list,
    # a reveal list, or a "puts ... into graveyard"), we record the first
    # player who interacted with it. This is what tells us, later, whose
    # graveyard a reanimation target was sitting in: if Atraxa first
    # appears as "PlayerB mills @[Atraxa]", Atraxa belongs to PlayerB —
    # and if PlayerA then casts Reanimate on it, PlayerA isn't actually
    # an Atraxa player, they just yoinked it from the opposing grave.
    card_first_owner: dict[str, str] = {}

    def _note(name: str, actor: str) -> None:
        card_first_owner.setdefault(name, actor)

    for actor, start, end in events:
        if actor not in players:
            continue
        body = text[start:end]
        for vm in ACTOR_OWNS_RE.finditer(body):
            _note(_repair(vm.group("card")), actor)
        for mm in MILL_LIST_RE.finditer(body):
            tail_start = mm.end() - 2
            for cm in CARD_RE.finditer(body, pos=tail_start):
                between = body[mm.end():cm.start()]
                if "@P" in between:
                    break
                _note(_repair(cm.group(1)), actor)
        for gm in PUTS_GRAVE_RE.finditer(body):
            _note(_repair(gm.group("card")), actor)
        reveal_cut = None
        m = REVEAL_LIST_RE.search(body)
        if m:
            reveal_cut = m.end()
        else:
            m = REVEAL_HAND_RE.search(body)
            if m:
                reveal_cut = m.end()
        if reveal_cut is not None:
            for c in CARD_RE.finditer(body, pos=reveal_cut):
                _note(_repair(c.group(1)), actor)

    # Pass 0a: suppression for reanimation from the opponent's grave.
    #
    # ``OPPONENT_GRAVE_REANIMATORS`` are spells that can ONLY target the
    # opponent's grave (From the Catacombs, Extraction Specialist) — any
    # target seen with them is suppressed unconditionally from the actor.
    #
    # ``EITHER_GRAVE_REANIMATORS`` (Reanimate, Animate Dead, Exhume, …)
    # can hit either grave, so we consult the first-owner map: suppress
    # the target only when we have evidence it lived in the opponent's
    # deck.
    suppressed: dict[str, set[str]] = {p: set() for p in players}
    for actor, start, end in events:
        if actor not in players:
            continue
        body = text[start:end]
        for sm in OPPONENT_GRAVE_CAST_RE.finditer(body):
            spell = sm.group("spell")
            target = _repair(sm.group("target"))
            if spell in OPPONENT_GRAVE_REANIMATORS:
                suppressed[actor].add(target)
            elif spell in EITHER_GRAVE_REANIMATORS:
                origin = card_first_owner.get(target)
                if origin is not None and origin != actor:
                    suppressed[actor].add(target)

    # Pass 0b: pitch-counter exile attribution.
    #
    # MTGO ALWAYS emits the pitch-cost exile BEFORE the matching
    # "casts <spell> by paying" event. So we walk both sequences in
    # log order and pair each exile with the NEXT unmatched cast of
    # the same spell. This correctly handles a Force of Will war
    # (exile_A, cast_A, exile_B, cast_B): closest-by-distance would
    # cross-pair the second exile to the first cast or vice versa.
    pitch_casts: list[list] = []  # [[pos, actor, spell, matched_bool], ...]
    for cm in PITCH_CAST_RE.finditer(text):
        spell = cm.group("spell")
        if spell in PITCH_COUNTERS:
            pitch_casts.append([cm.start(), cm.group("actor"), spell, False])

    pitch_exiles = [
        (em.start(), em) for em in PITCH_EXILE_RE.finditer(text)
        if em.group("spell") in PITCH_COUNTERS
    ]
    pitch_exiles.sort(key=lambda x: x[0])

    for exile_pos, em in pitch_exiles:
        spell = em.group("spell")
        pitched = _repair(em.group("card"))
        # First choice: the NEXT unmatched cast of the same spell.
        chosen: list | None = None
        for cast_entry in pitch_casts:
            if cast_entry[3] or cast_entry[2] != spell:
                continue
            if cast_entry[0] <= exile_pos:
                continue
            chosen = cast_entry
            break
        # Fallback: if no forward cast available (the cast event for
        # this exile might be earlier due to a log oddity), take the
        # nearest unmatched cast of the same spell within a window.
        if chosen is None:
            best: tuple[int, list] | None = None
            for cast_entry in pitch_casts:
                if cast_entry[3] or cast_entry[2] != spell:
                    continue
                dist = abs(cast_entry[0] - exile_pos)
                if dist > 2000:
                    continue
                if best is None or dist < best[0]:
                    best = (dist, cast_entry)
            if best is not None:
                chosen = best[1]
        if chosen is None:
            continue
        chosen[3] = True  # mark this cast as paired up
        real_caster = chosen[1]
        if real_caster not in players:
            continue
        cards[real_caster].append(pitched)
        for p in players:
            if p != real_caster:
                suppressed[p].add(pitched)

    # Pass 0c: textual Omniscience rescue. MTGO never wraps Omniscience
    # in a @[Omniscience@:...] card token in this match shape — its
    # name only appears in the "without paying its mana cost with
    # Omniscience" suffix. We scan for that suffix and credit the
    # Omniscience card to the player who's casting under it, so the
    # Omni-Tell signature requirement (Show and Tell + Omniscience)
    # can recognise the deck.
    for om in OMNI_TEXTUAL_RE.finditer(text):
        actor = om.group("actor")
        if actor in players:
            cards[actor].append("Omniscience")

    # Pass 1: actor-owns verbs, per event.
    for actor, start, end in events:
        if actor not in players:
            continue
        body = text[start:end]

        # "X reveals N cards with Y:" — listed cards belong to X (whether
        # X is the Thoughtseize target showing their hand, or the
        # Undercity Informer caster milling their own library).
        # "X reveals their hand to Y, containing A, B, C" — same: X's cards.
        reveal_cut = None
        m = REVEAL_LIST_RE.search(body)
        if m:
            reveal_cut = m.end()
        else:
            m = REVEAL_HAND_RE.search(body)
            if m:
                reveal_cut = m.end()
        if reveal_cut is not None:
            for c in CARD_RE.finditer(body, pos=reveal_cut):
                name = _repair(c.group(1))
                if name in suppressed[actor]:
                    continue
                cards[actor].append(name)

        # Verb-based attribution. Crop to the part BEFORE the reveal-list
        # so the "with @[Spell]" card name (often opponent's spell, e.g.
        # Thoughtseize) doesn't get over-counted on the wrong side.
        verb_scope = body[:reveal_cut] if reveal_cut is not None else body
        for vm in ACTOR_OWNS_RE.finditer(verb_scope):
            name = _repair(vm.group("card"))
            if name in suppressed[actor]:
                continue
            cards[actor].append(name)
            # The actor paid the mana cost iff the verb is cast /
            # flashback / cycle AND the cast wasn't free (Omniscience,
            # Cunning Wish, cascade, etc. emit a "without paying its
            # mana cost" suffix immediately after the card token).
            verb = vm.group("verb").lower()
            if verb in CAST_VERBS:
                trailing = verb_scope[vm.end():vm.end() + 60]
                if not FREE_CAST_SUFFIX_RE.match(trailing):
                    cast_cards[actor].append(name)

        # "X mills [A], [B], and [C]" — every card listed is from X's
        # own library, hence X's deck. Same for surveil.
        for mm in MILL_LIST_RE.finditer(body):
            tail_start = mm.end() - 2  # back up onto the "@[" so the
            # CARD_RE finditer also catches the first listed card.
            for cm in CARD_RE.finditer(body, pos=tail_start):
                between = body[mm.end():cm.start()]
                if "@P" in between:
                    break
                name = _repair(cm.group(1))
                if name in suppressed[actor]:
                    continue
                cards[actor].append(name)

        # "X puts [Card] into their graveyard."
        for gm in PUTS_GRAVE_RE.finditer(body):
            name = _repair(gm.group("card"))
            if name in suppressed[actor]:
                continue
            cards[actor].append(name)

    # Pass 2: possessive "X's [Card]" anywhere in the doc.
    for m in POSSESS_RE.finditer(text):
        actor = m.group("actor")
        if actor not in players:
            continue
        name = _repair(m.group("card"))
        if name in suppressed[actor]:
            continue
        cards[actor].append(name)

    return cards, cast_cards


def parse_game_log(path: str | Path) -> ParsedMatch:
    path = Path(path)
    text = _decode(path.read_bytes())
    match_id = path.stem.replace("Match_GameLog_", "")
    pm = ParsedMatch(match_id=match_id)

    seen: list[str] = []
    for m in JOIN_RE.finditer(text):
        name = m.group(1)
        if name not in seen:
            seen.append(name)
    pm.players = seen
    player_set = set(seen)

    fp = FIRST_RE.search(text)
    if fp and fp.group(1) in player_set:
        pm.first_player = fp.group(1)
    else:
        # Fallback: MTGO logs the start-of-game dice rolls as
        # "@P<name> rolled a <N>." for both players. The higher
        # number wins the roll and (in practice always) chooses to
        # play first. On a tie, both players reroll until someone
        # wins, so we walk the rolls in order and pick the first
        # round where the two players' rolls disagree — that
        # player's the winner.
        rolls = [
            (m.group(1), int(m.group(2)))
            for m in DIE_ROLL_RE.finditer(text)
            if m.group(1) in player_set
        ]
        # Group consecutive (name, N) pairs into roll-rounds and
        # pick the first round with a clear winner. We don't assume
        # any particular order of names within a round — just look
        # at successive pairs of distinct players.
        i = 0
        while i + 1 < len(rolls):
            a_name, a_n = rolls[i]
            b_name, b_n = rolls[i + 1]
            if a_name != b_name and a_n != b_n:
                pm.first_player = a_name if a_n > b_n else b_name
                break
            i += 2 if a_name != b_name else 1

    turns = [int(m.group(1)) for m in TURN_RE.finditer(text)]
    pm.turns = max(turns) if turns else 0

    pm.cards_by_player, pm.cards_cast_by_player = _extract_cards(
        text, player_set
    )
    pm.cards_unique_by_player = {
        p: sorted(set(cards)) for p, cards in pm.cards_by_player.items()
    }
    # ``pm.format`` is set by the ingest layer via mtgo_game_history
    # (MTGO's own authoritative record). The default "Legacy" only
    # applies when that lookup can't find the match.

    events: list[tuple[int, str, str]] = []
    for m in WINS_GAME_RE.finditer(text):
        events.append((m.start(), "win", m.group(1)))
    for m in LOSES_GAME_RE.finditer(text):
        events.append((m.start(), "lose", m.group(1)))
    for m in CONCEDE_RE.finditer(text):
        events.append((m.start(), "concede", m.group(1)))
    events.sort()

    current: GameOutcome | None = None
    for _pos, kind, name in events:
        if current is None:
            current = GameOutcome()
        if kind == "concede":
            current.by_concede = True
            current.loser = name
            for p in player_set:
                if p != name:
                    current.winner = p
        elif kind == "lose":
            current.loser = name
            for p in player_set:
                if p != name:
                    current.winner = p
        elif kind == "win":
            current.winner = name
            for p in player_set:
                if p != name and current.loser is None:
                    current.loser = p
            pm.game_outcomes.append(current)
            current = None
    if current and (current.winner or current.loser):
        pm.game_outcomes.append(current)

    mm = WINS_MATCH_RE.search(text)
    if mm and mm.group(1) in player_set:
        pm.match_winner = mm.group(1)
        pm.match_score = (int(mm.group(2)), int(mm.group(3)))

    return pm


GameLog = ParsedMatch
