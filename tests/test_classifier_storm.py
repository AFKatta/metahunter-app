"""Storm decks must be named as storm decks.

Three bags taken verbatim from real uploaded matches. All three were
reported as "4c (no white) Tempo", which is not an archetype anybody
plays — it is Badaro's placeholder for a four-colour list its rules
could not name, and it reached the user because:

* signature-card requirements matched archetype names by raw substring,
  so "Storm" (Past in Flames / Tendrils / Cabal Ritual — none of which
  modern TES runs) also applied to "The EPIC Storm" and vetoed it; and
* corpus decks carrying one of those placeholder labels were allowed to
  name a deck at all.

Both of those turned the most distinctive shell in Legacy — Lion's Eye
Diamond, Burning Wish, Dark Ritual, Echo of Eons — into a colour code
with a suffix.
"""
from __future__ import annotations

import json

import pytest

from metahunter_core.classifier import (
    build_card_colors,
    build_card_weights,
    classify_by_similarity,
    recompute_deck_color_identity,
)
from mtgo_meta.paths import corpus_path

STORM_NAMES = {"The EPIC Storm", "Ad Nauseam Tendrils", "Ruby Storm", "Necro Storm"}


@pytest.fixture(scope="module")
def legacy():
    path = corpus_path("Legacy")
    if not path.exists():
        pytest.skip("no Legacy corpus in this checkout")
    decks = json.loads(path.read_text(encoding="utf-8")).get("decks", [])
    for d in decks:
        d["color_identity"] = recompute_deck_color_identity(d)
    return decks, build_card_weights(decks), build_card_colors(decks)


def name_for(legacy, observed, cast):
    decks, weights, colors = legacy
    return classify_by_similarity(observed, decks, weights, colors, cast_cards=cast)[0]


# 21 Sep. Burning Wish, Echo of Eons, LED, Gamble, Dark Ritual.
TES_A = (
    ['Bloodstained Mire', 'Burning Wish', 'Carpet of Flowers', 'Chrome Mox', 'Dark Ritual',
     'Echo of Eons', 'Gamble', "Giant's Boulder", 'Hexing Squelcher', "Lion's Eye Diamond",
     'Lotus Petal', 'Raucous Theater', "Urza's Saga", 'Verdant Catacombs'],
    ['Carpet of Flowers', 'Chrome Mox', 'Dark Ritual', 'Echo of Eons', 'Gamble',
     "Giant's Boulder", 'Hexing Squelcher', "Lion's Eye Diamond", 'Lotus Petal'],
)

# 18 Sep. The same shell with Beseech the Mirror and Gaea's Will.
TES_B = (
    ['Badlands', 'Beseech the Mirror', 'Bloodstained Mire', 'Burning Wish', 'Chrome Mox',
     'Dark Ritual', 'Echo of Eons', "Gaea's Will", 'Gamble', "Lion's Eye Diamond", 'Lotus Petal',
     'Mox Opal', 'Scalding Tarn', 'Taiga', 'Thoughtseize', 'Undercity Sewers', 'Veil of Summer',
     'Verdant Catacombs'],
    ['Beseech the Mirror', 'Burning Wish', 'Chrome Mox', 'Dark Ritual', 'Echo of Eons', 'Gamble',
     "Lion's Eye Diamond", 'Lotus Petal', 'Mox Opal', 'Thoughtseize', 'Veil of Summer'],
)

# 14 Sep. Ad Nauseam and Tendrils of Agony, on the artifact-land build.
ANT = (
    ['Abrupt Decay', 'Ad Nauseam', 'Beseech the Mirror', 'Burning Wish', 'Chrome Mox',
     'Dark Ritual', 'Duress', "Gaea's Will", 'Great Hall of the Biblioplex', 'Haywire Mite',
     'Into the Flood Maw', "Lion's Eye Diamond", 'Lotus Petal', 'Mox Opal', 'Skateboard',
     'Spire of Industry', 'Tendrils of Agony', 'Thoughtseize', "Urza's Saga",
     'Vault of Whispers', 'Veil of Summer'],
    ['Abrupt Decay', 'Beseech the Mirror', 'Burning Wish', 'Duress', 'Haywire Mite',
     'Into the Flood Maw', "Lion's Eye Diamond", 'Lotus Petal', 'Mox Opal', 'Thoughtseize',
     'Veil of Summer'],
)


@pytest.mark.parametrize("bag", [TES_A, TES_B], ids=["21 Sep", "18 Sep"])
def test_the_epic_storm_is_named(legacy, bag):
    assert name_for(legacy, *bag) == "The EPIC Storm"


def test_ad_nauseam_tendrils_is_a_storm_deck(legacy):
    """Which storm deck is a fair argument; a colour code is not.

    This bag is Ad Nauseam Tendrils on the artifact-land build, and it
    shares most of its shell with The EPIC Storm. Either name is a
    defensible reading of what was seen, so the test demands a storm
    deck rather than pretending the distinction is settled.
    """
    assert name_for(legacy, *ANT) in STORM_NAMES


@pytest.mark.parametrize("bag", [TES_A, TES_B, ANT], ids=["21 Sep", "18 Sep", "14 Sep"])
def test_never_a_placeholder_label(legacy, bag):
    """No storm deck may be reported as a colour with a suffix."""
    name = name_for(legacy, *bag)
    assert "4c" not in name and "4-Color" not in name, name
    assert not name.endswith(("Midrange", "Tempo")), name
