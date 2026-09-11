"""League entries: five matches, a list change, or a marked drop ends one.

The sequence tested first is the one a player actually reported. They
went 4-1, then 2-3, dropped an entry at 0-3, finished a 3-2, and started
a new list. Grouped five at a time, that read as a 1-4 and "2-1 in
progress". With the drop marked it must read as what happened.
"""
from __future__ import annotations

from mtgo_meta.leagues import (
    COMPLETE,
    DROPPED,
    GAP_SECONDS,
    IN_PROGRESS,
    UNFINISHED,
    league_runs,
)

HALF_HOUR = 1800
START = 1_789_000_000.0


def match(i: int, result: str, at: float | None = None, kind: str = "league") -> dict:
    return {
        "match_id": f"m{i}",
        "played_at": START + i * HALF_HOUR if at is None else at,
        "result": result,
        "event_kind": kind,
        "opponent_archetype": "Something",
    }


# 4-1 | 2-3 | 0-3 dropped | 3-2 | new list: 1-0
REPORTED = "WWWLW" "LWWLL" "LLL" "WLLWW"


def reported_history():
    rows = [match(i + 1, r) for i, r in enumerate(REPORTED)]
    rows.append(match(19, "W"))
    keys = {f"m{i}": "list-A" for i in range(1, 19)}
    keys["m19"] = "list-B"
    return rows, keys


def oldest_first(result: dict) -> list[tuple[str, int, int]]:
    return [(e["status"], e["wins"], e["losses"]) for e in reversed(result["entries"])]


def test_marked_drop_gives_what_was_actually_played():
    rows, keys = reported_history()
    result = league_runs(rows, marks={"m13"}, list_keys=keys, now=rows[-1]["played_at"] + 60)

    assert oldest_first(result) == [
        (COMPLETE, 4, 1),
        (COMPLETE, 2, 3),
        (DROPPED, 0, 3),
        (COMPLETE, 3, 2),
        (IN_PROGRESS, 1, 0),
    ]
    ladder = {r["record"]: r["runs"] for r in result["ladder"]}
    assert ladder["4-1"] == 1 and ladder["3-2"] == 1 and ladder["2-3"] == 1
    assert ladder["1-4"] == 0
    assert result["completed_runs"] == 3
    assert result["dropped_runs"] == 1
    assert result["in_progress"] == {
        "wins": 1, "losses": 0, "matches": 1, "started_at": rows[-1]["played_at"],
    }


def test_without_a_mark_the_list_change_still_ends_the_entry():
    """Unmarked, five-at-a-time is an assumption — but the new list is a fact."""
    rows, keys = reported_history()
    result = league_runs(rows, list_keys=keys, now=rows[-1]["played_at"] + 60)

    assert oldest_first(result) == [
        (COMPLETE, 4, 1),
        (COMPLETE, 2, 3),
        (COMPLETE, 1, 4),
        (DROPPED, 2, 1),       # ended by the list change, not by five matches
        (IN_PROGRESS, 1, 0),   # the new list is a new entry, not "2-1 after 3"
    ]
    assert result["entries"][1]["ended_by"] == "list_changed"


def test_every_entry_carries_its_matches_so_a_drop_can_be_marked():
    rows, keys = reported_history()
    result = league_runs(rows, list_keys=keys, now=rows[-1]["played_at"] + 60)
    third = list(reversed(result["entries"]))[2]
    assert [p["match_id"] for p in third["played"]] == ["m11", "m12", "m13", "m14", "m15"]
    assert not any(p["marked"] for p in third["played"])


def test_a_mark_on_the_fifth_match_is_still_a_complete_entry():
    rows = [match(i, "W") for i in range(1, 7)]
    result = league_runs(rows, marks={"m5"}, now=rows[-1]["played_at"] + 60)
    assert oldest_first(result) == [(COMPLETE, 5, 0), (IN_PROGRESS, 1, 0)]
    assert result["trophies"] == 1


def test_a_long_silence_is_unfinished_not_dropped():
    rows = [match(1, "W"), match(2, "L")]
    rows.append(match(3, "W", at=rows[-1]["played_at"] + GAP_SECONDS + 1))
    result = league_runs(rows, now=rows[-1]["played_at"] + 60)
    assert oldest_first(result) == [(UNFINISHED, 1, 1), (IN_PROGRESS, 1, 0)]
    assert result["entries"][1]["ended_by"] == "gap"


def test_a_stale_newest_entry_is_not_in_progress():
    rows = [match(1, "W"), match(2, "W"), match(3, "L")]
    result = league_runs(rows, now=rows[-1]["played_at"] + GAP_SECONDS + 1)
    assert oldest_first(result) == [(UNFINISHED, 2, 1)]
    assert result["in_progress"] is None


def test_friendlies_and_unfinished_matches_are_not_league_matches():
    rows = [
        match(1, "W"),
        match(2, "W", kind="casual"),
        {**match(3, "L"), "result": None},
        match(4, "L"),
    ]
    result = league_runs(rows, now=rows[-1]["played_at"] + 60)
    assert result["league_matches"] == 2
    assert oldest_first(result) == [(IN_PROGRESS, 1, 1)]


def test_the_ladder_is_always_every_rung():
    result = league_runs([], now=START)
    assert [r["record"] for r in result["ladder"]] == ["5-0", "4-1", "3-2", "2-3", "1-4", "0-5"]
    assert result["entries"] == [] and result["average_wins"] is None
