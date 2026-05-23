"""Map Badaro archetype-rule output + deck contents → user-facing name.

The Badaro rule for "Delver" returns the bare name "Delver" with
``IncludeColorInName=true``; we have to compute the color identity
ourselves and prepend the combo name to produce "Izzet Delver".

A handful of archetypes also need overrides — names the community uses
that differ from Badaro's internal labels (e.g. "TES" → "The EPIC
Storm", "Energy" → "Ocelot Pride Midrange").

And a few decks acquire a "(Yorion)" suffix when Yorion, Sky Nomad is
in the sideboard (signaling a +20-card variant).
"""

from __future__ import annotations

from metahunter_core.colors import combo_name


# Bare-name overrides applied before color prefixing. Map of
# Badaro name → user-facing name (color may still be prepended after).
NAME_OVERRIDES: dict[str, str] = {
    "Energy": "Ocelot Pride Midrange",
    "TES": "The EPIC Storm",
    "ANT": "Ad Nauseam Tendrils",
    "ReaShow": "Sneak and Show with Reanimate",
    "D&T": "Death & Taxes",
    "Cephalid breakfast": "Cephalid Breakfast",
    "Cephalid painter": "Cephalid Painter",
    "Arclight phoenix": "Arclight Phoenix",
    "8-cast": "8-Cast",
    "Artos aggro": "Artos Aggro",
    "Bant Toollbox": "Bant Toolbox",
    "Bombardiers combo": "Bombardiers Combo",
    "Breya aggro": "Breya Aggro",
    "Creative combo": "Creative Technique",
    "Deadly brew": "Deadly Brew",
    "Discover combo": "Discover Combo",
    "Echo of Eons": "Echo of Eons",
    "Hollow One Madness": "Hollow One Madness",
    "Initiative": "Initiative",       # color prefix added later
    "Initiative Stompy": "Initiative",
    "Mentor": "Monastery Mentor",
    "Mono Black Aggro": "Mono-Black Aggro",
    "Necrodominance": "Necrodominance Combo",
    "Stiflenought": "Stiflenought",
    "Zenith": "Zenith Combo",
    "Cradle Control": "Cradle Control",
    "Smallpox": "Loam Pox",
    "White Beanstalk": "Beanstalk Control",
    "GenericZoo": "Zoo",
    "Aggro": "Aggro",                 # fallback name
    "Midrange": "Midrange",           # fallback name
    "Control": "Control",             # fallback name
    "Stompy": "Stompy",               # generic stompy parent
}

# These archetypes are inherently specific (no color naming makes sense).
# Listed so we know not to prepend a color even if the Badaro rule said to.
COLOR_AGNOSTIC = {
    "Tron", "Show and Tell", "Sneak and Show", "Omni-Tell", "Oops! All Spells",
    "Eldrazi", "Lands", "Doomsday", "The EPIC Storm", "Painter", "Cradle Control",
    "Blue Artifacts", "Aluren", "Selesnya Depths", "Loam Pox", "Goblins",
    "Death's Shadow", "LED Dredge", "Dredge", "Beanstalk Control",
    "Ad Nauseam Tendrils", "Ruby Storm", "Creative Technique", "Stiflenought",
    "Burn", "Infect", "Mystic Forge Combo", "Cephalid Breakfast", "Maverick",
    "Merfolk", "Nic Fit", "Necrodominance Combo", "Mono-White Stax",
    "Affinity Stompy", "Death & Taxes", "Monastery Mentor", "Storm",
    "Reanimator",  # we'll let our subtype detection turn this into Dimir/Grixis Reanimator
    "Mono-Black Aggro", "Zoo", "Echo of Eons", "Worldgorger Combo",
    "Riddlesmith combo",
    "Patchwork",
    # Note: "Aggro", "Midrange", "Control", "Stompy", "Tempo", "Delver" are
    # intentionally NOT here — we want "Jeskai Control", "Izzet Delver",
    # "Dimir Tempo", "Boros Aggro", etc.
}


def _has_card(cards: set[str], *needles: str) -> bool:
    return any(n in cards for n in needles)


def specialise(base: str, main_cards: set[str], side_cards: set[str]) -> str:
    """Apply card-presence rules that reroute the archetype name.

    Example: a Badaro "Show and Tell" deck with Sneak Attack in the
    mainboard is "Sneak and Show"; with Omniscience it's "Omni-Tell".
    """
    all_cards = main_cards | side_cards

    if base == "Show and Tell":
        if _has_card(main_cards, "Sneak Attack"):
            return "Sneak and Show"
        if _has_card(all_cards, "Omniscience"):
            return "Omni-Tell"
        return "Show and Tell"

    if base == "Dredge":
        if _has_card(all_cards, "Lion's Eye Diamond"):
            return "LED Dredge"
        return "Dredge"

    if base == "Stompy":
        if _has_card(all_cards, "Frogmite", "Cranial Plating", "Arcbound Ravager",
                     "Springleaf Drum"):
            return "Affinity Stompy"
        return base  # color prefix will be added later

    return base


def apply_yorion(name: str, side_cards: set[str]) -> str:
    if "Yorion, Sky Nomad" in side_cards:
        return f"{name} (Yorion)"
    return name


def full_name(
    badaro_name: str,
    color_identity: str,
    main_cards: set[str],
    side_cards: set[str],
    include_color: bool,
) -> str:
    """Compose the final user-facing archetype label."""
    # First: subtype detection based on deck contents.
    base = specialise(badaro_name, main_cards, side_cards)
    base = NAME_OVERRIDES.get(base, base)

    # Color prefix if the rule asked for it and the base is not
    # color-agnostic (we mark "Lands", "Tron", etc. as color-agnostic
    # because their identity is the deck, not the color combo).
    if include_color and base not in COLOR_AGNOSTIC and color_identity:
        prefix = combo_name(color_identity)
        # "Mono-Blue Doomsday" is awkward; "Mono-X" prefix is fine on
        # generic archetype names but feels off on long combo names.
        base = f"{prefix} {base}"

    # Yorion variant tag.
    base = apply_yorion(base, side_cards)
    return base
