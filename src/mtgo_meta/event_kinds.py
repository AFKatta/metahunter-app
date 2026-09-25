"""Which matches counted — the one place that decides it.

A friendly game is not evidence about a deck. MTGO says what an event
was in two places, neither complete on its own: the text log, beside
each deck registration, and the history file's event blurb. Reading
them lives here so the desktop dashboard and the uploader cannot
disagree about what a league match is — they did, and the consequence
was that every public number on the website counted practice games.

Matches neither source covers are UNKNOWN, which is not casual and must
never be treated as such. Most of a long history predates any record of
its event type, and discarding all of it to avoid a handful of practice
games would be the larger error.
"""

from __future__ import annotations

import bisect
from typing import Iterable, Mapping

import metahunter_core.parser.game_history as game_history
from metahunter_core.deck_files import mtgo_history_files
from metahunter_core.event_kind import CASUAL, resolve as resolve_event_kind


def _blurbs() -> tuple[list[float], list[tuple[float, str]]]:
    """Every event blurb MTGO has kept, sorted by match start time."""
    recs: list[tuple[float, str]] = []
    try:
        for path in mtgo_history_files():
            parser = game_history._Parser(path.read_bytes())
            try:
                parser.parse()
            except Exception:  # noqa: BLE001 - a truncated file still yields rows
                pass
            recs += parser.matches
    except Exception:  # noqa: BLE001 - no history file is not an error
        recs = []
    recs.sort()
    return [r[0] for r in recs], recs


def event_kinds(
    registered: Mapping[str, dict], matches: Iterable
) -> dict[str, str]:
    """``match id -> "league" / "tournament" / "casual" / "unknown"``.

    ``registered`` is the store's captured deck registrations by match
    id; ``matches`` is anything with ``match_id`` and ``log_mtime``.
    """
    starts, recs = _blurbs()

    def blurb_for(mtime: float | None) -> str | None:
        # A game log's mtime lands within a couple of hours of the match
        # start, so the nearest blurb inside that window is the one.
        if mtime is None or not recs:
            return None
        k = bisect.bisect_left(starts, mtime)
        best, best_delta = None, 7200.0
        for x in (k - 1, k, k + 1):
            if 0 <= x < len(recs):
                delta = abs(recs[x][0] - mtime)
                if delta < best_delta:
                    best, best_delta = recs[x][1], delta
        return best

    out: dict[str, str] = {}
    for m in matches:
        rd = registered.get(m.match_id) or {}
        out[m.match_id] = resolve_event_kind(
            text_log_kind=rd.get("event_kind"),
            league_flag=rd.get("is_league"),
            description=blurb_for(getattr(m, "log_mtime", None)),
        )
    return out


def casual_match_ids(registered: Mapping[str, dict], matches: Iterable) -> set[str]:
    """Matches MTGO positively identifies as friendly games.

    Only those: a match nothing covers is absent from this set, because
    the distinction that matters is between *known friendly* and *not
    known to be friendly*.
    """
    return {
        mid for mid, kind in event_kinds(registered, matches).items()
        if kind == CASUAL
    }
