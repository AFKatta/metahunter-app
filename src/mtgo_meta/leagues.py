"""League entries rebuilt from the matches played in them.

MTGO does not write down which entry a match belonged to anywhere this
app can read. The game log has no event at all. ``mtgo_game_history``
has a ``Round`` field, but it is 0 for every league match and only set
for challenges, and its ``Id`` is unique per match. The running text
log is rewritten every time MTGO starts, so nothing of a previous
session survives it. Even mymtgo, which reads that log live, recorded a
single league across two weeks of play.

So an entry is reconstructed, and the rules are kept to what is known:

* An entry is five matches. The sixth is always a new one.
* The deck is locked for an entry. When a league match was registered
  with a different list from the one before it, the earlier entry is
  over, however many matches it had.
* A player can drop. No file says so, so the player says so: a mark on
  the last match of the entry they left ends it there.
* More than ten days without a league match on the deck ends an entry
  too. That one is a judgement rather than a fact — without it an
  abandoned entry and a new one would be welded into a finish nobody
  played — so an entry ended that way is "unfinished", not "dropped".

Without a mark, grouping five at a time assumes nobody dropped, and one
unmarked drop shifts every entry after it. That is the exact failure a
player reported — a 0-3 drop and then a 3-2 shown as a 1-4 and "2-1 in
progress" — and it is why every entry is returned with its matches, so
the grouping is visible and can be corrected in one click.
"""
from __future__ import annotations

import time
from typing import Any, Iterable, Mapping

from metahunter_core.event_kind import LEAGUE

#: Matches in one constructed league entry. No elimination, so every
#: record from 5-0 to 0-5 is reachable.
RUN_LENGTH = 5

#: Silence after which an entry is taken to have been left behind.
GAP_SECONDS = 10 * 24 * 3600

COMPLETE = "complete"
DROPPED = "dropped"
UNFINISHED = "unfinished"
IN_PROGRESS = "in_progress"


def league_runs(
    history: Iterable[Mapping[str, Any]],
    *,
    marks: Iterable[str] = (),
    list_keys: Mapping[str, str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Group a deck's league matches into entries and score them.

    ``history`` rows need ``match_id``, ``played_at``, ``result`` ("W",
    "L" or None) and ``event_kind``. ``marks`` are match ids the player
    said an entry ended on. ``list_keys`` maps a match id to the identity
    of the list registered for it; a change between consecutive matches
    ends an entry.
    """
    marks = set(marks)
    list_keys = list_keys or {}
    now = time.time() if now is None else now

    played = sorted(
        (h for h in history if h.get("event_kind") == LEAGUE and h.get("result")),
        key=lambda h: h.get("played_at") or 0.0,
    )

    entries: list[dict[str, Any]] = []
    for h in played:
        at = h.get("played_at") or 0.0
        key = list_keys.get(h.get("match_id"))
        cur = entries[-1] if entries else None

        reason = None
        if cur is not None:
            if cur["ended"] is not None:
                reason = cur["ended"]
            elif len(cur["matches"]) >= RUN_LENGTH:
                reason = COMPLETE
            elif key and cur["list_key"] and key != cur["list_key"]:
                reason = "list_changed"
            elif at - (cur["matches"][-1].get("played_at") or 0.0) > GAP_SECONDS:
                reason = "gap"

        if cur is None or reason is not None:
            if cur is not None and cur["ended"] is None:
                cur["ended"] = reason
            cur = {"list_key": key, "matches": [], "ended": None}
            entries.append(cur)
        elif key and not cur["list_key"]:
            cur["list_key"] = key

        cur["matches"].append(h)
        if h.get("match_id") in marks:
            cur["ended"] = "marked"

    finishes: dict[str, int] = {}
    scored: list[dict[str, Any]] = []
    in_progress = None
    for i, e in enumerate(entries):
        rows = e["matches"]
        n = len(rows)
        wins = sum(1 for h in rows if h["result"] == "W")
        last_at = rows[-1].get("played_at") or 0.0
        is_last = i == len(entries) - 1

        if n >= RUN_LENGTH:
            status = COMPLETE
            finishes[f"{wins}-{n - wins}"] = finishes.get(f"{wins}-{n - wins}", 0) + 1
        elif e["ended"] in ("marked", "list_changed"):
            status = DROPPED
        elif e["ended"] == "gap" or not is_last:
            status = UNFINISHED
        elif now - last_at > GAP_SECONDS:
            # The newest entry, but nothing has been played on it for so
            # long that calling it live would leave the deck permanently
            # mid-league.
            status = UNFINISHED
        else:
            status = IN_PROGRESS
            in_progress = {
                "wins": wins,
                "losses": n - wins,
                "matches": n,
                "started_at": rows[0].get("played_at"),
            }

        scored.append({
            "status": status,
            # Why it ended: five_matches, marked, list_changed, gap, or
            # None while it is still open.
            "ended_by": "five_matches" if status == COMPLETE else e["ended"],
            "wins": wins,
            "losses": n - wins,
            "matches": n,
            "started_at": rows[0].get("played_at"),
            "ended_at": last_at,
            "played": [
                {
                    "match_id": h.get("match_id"),
                    "played_at": h.get("played_at"),
                    "result": h.get("result"),
                    "score": h.get("score"),
                    "opponent_archetype": h.get("opponent_archetype"),
                    "marked": h.get("match_id") in marks,
                }
                for h in rows
            ],
        })

    # Every rung of the ladder, zeroes included, so the UI draws a stable
    # set of bars instead of a shifting subset.
    ladder = [
        {
            "record": f"{w}-{RUN_LENGTH - w}",
            "wins": w,
            "runs": finishes.get(f"{w}-{RUN_LENGTH - w}", 0),
        }
        for w in range(RUN_LENGTH, -1, -1)
    ]
    completed = sum(r["runs"] for r in ladder)

    return {
        "ladder": ladder,
        "completed_runs": completed,
        "trophies": finishes.get(f"{RUN_LENGTH}-0", 0),
        "in_progress": in_progress,
        # Across completed entries only, so decks compare like for like.
        "average_wins": (
            sum(r["wins"] * r["runs"] for r in ladder) / completed
        ) if completed else None,
        "league_matches": len(played),
        # Entries that ended short of five, whether dropped or left.
        "dropped_runs": sum(1 for e in scored if e["status"] in (DROPPED, UNFINISHED)),
        # Newest first, which is the order anyone reads them in.
        "entries": list(reversed(scored)),
    }
