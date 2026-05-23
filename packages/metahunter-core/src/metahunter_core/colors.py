"""Compute deck color identity and map to standard combo names."""

from __future__ import annotations

# MTGO encodes colors per card as a list of strings like "COLOR_BLUE".
_MTGO_COLOR_MAP = {
    "COLOR_WHITE": "W",
    "COLOR_BLUE": "U",
    "COLOR_BLACK": "B",
    "COLOR_RED": "R",
    "COLOR_GREEN": "G",
}
_COLOR_ORDER = "WUBRG"

# Standard guild / shard / wedge / 4c names. Keys are canonical
# WUBRG-ordered strings.
_COMBO_NAMES: dict[str, str] = {
    "": "Colorless",
    "W": "Mono-White",
    "U": "Mono-Blue",
    "B": "Mono-Black",
    "R": "Mono-Red",
    "G": "Mono-Green",
    "WU": "Azorius",
    "UB": "Dimir",
    "BR": "Rakdos",
    "RG": "Gruul",
    "WG": "Selesnya",
    "WB": "Orzhov",
    "UR": "Izzet",
    "BG": "Golgari",
    "WR": "Boros",
    "UG": "Simic",
    "WUB": "Esper",
    "UBR": "Grixis",
    "BRG": "Jund",
    "WRG": "Naya",
    "WUG": "Bant",
    "WBR": "Mardu",
    "URG": "Temur",
    "WBG": "Abzan",
    "UBG": "Sultai",
    "WUR": "Jeskai",
    "WUBR": "4c (no green)",
    "WUBG": "4c (no red)",
    "WURG": "4c (no black)",
    "WBRG": "4c (no blue)",
    "UBRG": "4c (no white)",
    "WUBRG": "5-Color",
}


def normalize_colors(colors: list[str]) -> str:
    """Take a list like ['U', 'B'] or ['COLOR_BLUE', 'COLOR_BLACK'] and
    return the canonical WUBRG-ordered string, e.g. 'UB'.
    """
    out: set[str] = set()
    for c in colors:
        if c in _MTGO_COLOR_MAP:
            out.add(_MTGO_COLOR_MAP[c])
        elif c in _COLOR_ORDER:
            out.add(c)
    return "".join(sorted(out, key=_COLOR_ORDER.index))


def deck_color_identity(
    cards: list[dict],
    exclude_lands: bool = True,
) -> str:
    """Given a list of card dicts with ``card_attributes.colors`` and
    ``card_attributes.card_type``, return the canonical WUBRG-ordered
    color identity of the *non-land* spells (the standard convention).
    """
    seen: set[str] = set()
    for c in cards:
        attrs = c.get("card_attributes") or {}
        if exclude_lands and attrs.get("card_type") == "LAND":
            continue
        for col in attrs.get("colors") or []:
            mapped = _MTGO_COLOR_MAP.get(col)
            if mapped:
                seen.add(mapped)
    return "".join(sorted(seen, key=_COLOR_ORDER.index))


def combo_name(color_identity: str) -> str:
    """Map a canonical WUBRG-ordered identity to its standard nickname."""
    return _COMBO_NAMES.get(color_identity, color_identity or "Colorless")
