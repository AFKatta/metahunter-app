"""Deck versions: forget what was never played, compare lists by name.

MTGO saves the deck file on every click in the editor. One evening of
tuning left seventeen stored versions of one deck, two of them played.
And two versions shown as twenty-one cards apart were the same 75 with
different printings. Both are tested here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from metahunter_core.deck_files import maindeck_signature
from mtgo_meta import deck_history

NOW = 1_789_000_000.0
HOUR = 3600


@dataclass
class Card:
    mtgo_id: int
    quantity: int
    sideboard: bool = False


@dataclass
class Deck:
    deck_id: str
    cards: list[Card]
    name: str = "UB Bilbo"
    format: str = "Legacy"
    modified_at: float = NOW


@dataclass
class FakeStore:
    versions: dict = field(default_factory=dict)
    registrations: list = field(default_factory=list)

    def add(self, cards, last_seen, deck_id="deck-1"):
        sig = maindeck_signature(cards)
        self.versions[sig] = {
            "signature": sig, "deck_id": deck_id, "name": "UB Bilbo",
            "format": "Legacy", "cards": [list(c) for c in cards],
            "first_seen": last_seen, "last_seen": last_seen, "modified_at": last_seen,
            "source": "file",
        }
        return sig

    def deck_versions(self):
        return list(self.versions.values())

    def registered_decks(self):
        return self.registrations

    def forget_deck_versions(self, signatures):
        return sum(1 for s in signatures if self.versions.pop(s, None) is not None)


def _setup():
    store = FakeStore()
    current = [(1, 4, False), (99, 4, True)]
    played = [(2, 4, False)]
    friendly_only = [(3, 4, False)]
    old_edit = [(5, 4, False)]
    fresh_edit = [(6, 4, False)]

    sigs = {
        "current": store.add(current, NOW - HOUR),
        "played": store.add(played, NOW - 5 * HOUR),
        "friendly": store.add(friendly_only, NOW - 5 * HOUR),
        "old_edit": store.add(old_edit, NOW - 2 * HOUR),
        "fresh_edit": store.add(fresh_edit, NOW - 60),
    }
    store.registrations = [
        {"cards": [list(c) for c in played], "event_kind": "league", "is_league": True},
        {"cards": [list(c) for c in friendly_only], "event_kind": "casual", "is_league": None},
    ]
    deck = Deck("deck-1", [Card(1, 4), Card(99, 4, True)])
    return store, deck, sigs


def test_forgets_only_old_lists_nobody_played():
    store, deck, sigs = _setup()
    assert deck_history.forget_unplayed(store, [deck], now=NOW) == 1
    assert set(store.versions) == {
        sigs["current"], sigs["played"], sigs["friendly"], sigs["fresh_edit"],
    }


def test_an_unreadable_deck_folder_forgets_nothing():
    """No decks read must not look like every deck having been deleted."""
    store, _deck, _sigs = _setup()
    assert deck_history.forget_unplayed(store, [], now=NOW) == 0
    assert len(store.versions) == 5


def test_the_website_gets_played_and_current_but_not_friendly_only():
    store, deck, sigs = _setup()
    keep = deck_history.upload_keep(store.registrations, [deck])
    assert keep == {sigs["played"], sigs["current"]}

    payload = deck_history.upload_payload(store.deck_versions(), keep=keep)
    assert {p["signature"] for p in payload} == {sigs["played"], sigs["current"]}


def test_upload_without_a_keep_set_is_unchanged():
    store, _deck, _sigs = _setup()
    assert len(deck_history.upload_payload(store.deck_versions())) == 5


NAMES = {
    1: "Force of Will",
    2: "Force of Will",          # a different printing of the same card
    10: "Bilbo, Thief in the Night",
    11: "Thoughtseize",
    12: "Dark Ritual",
    20: "Island",
}
name_of = NAMES.__getitem__


def test_a_printing_swap_is_the_same_list():
    regular = [(1, 4, False), (20, 56, False)]
    foil = [(2, 4, False), (20, 56, False)]
    assert deck_history.name_fingerprint(regular, name_of) == deck_history.name_fingerprint(foil, name_of)
    assert deck_history.name_diff(regular, foil, name_of) == {
        "maindeck_added": [], "maindeck_removed": [],
        "sideboard_added": [], "sideboard_removed": [],
    }


def test_a_sideboard_change_is_a_different_list():
    before = [(1, 4, False)]
    after = [(1, 3, False), (1, 1, True)]
    assert deck_history.name_fingerprint(before, name_of) != deck_history.name_fingerprint(after, name_of)


def test_name_diff_reports_the_real_change():
    older = [(10, 2, False), (20, 58, False)]
    newer = [(11, 1, False), (12, 1, False), (20, 58, False)]
    d = deck_history.name_diff(older, newer, name_of)
    assert d["maindeck_removed"] == [{"name": "Bilbo, Thief in the Night", "quantity": 2}]
    assert sorted(x["name"] for x in d["maindeck_added"]) == ["Dark Ritual", "Thoughtseize"]
