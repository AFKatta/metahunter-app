"""Remember every version of every deck that was played, because MTGO does not.

MTGO keeps one file per deck and overwrites it in place when you edit.
The list you registered for a league on Tuesday simply stops existing on
Wednesday, so the matches you played with it match nothing and lose
their attribution. That is not hypothetical — eleven league games did
exactly that, and the deck page showed a single run instead of two.

So this module does three things:

* **Snapshot.** Every time the saved decks are read, any list we have
  not seen before is written to ``deck_versions``. The file is mutable;
  the record is not.

* **Recover.** For matches MTGO already told us about, we hold the exact
  registered decklist. Where that list matches no version we know, it is
  a version we missed — usually one that existed before this feature, or
  before the app was installed. It gets stored, and attached to the deck
  it most resembles.

* **Forget.** MTGO rewrites the file on every click in the deck editor,
  so snapshotting alone kept every intermediate state: one evening of
  tuning a deck left seventeen versions of it, most of them 57- or
  62-card lists nobody could have registered. A version is kept only if
  a match was played with it, or if it is the list saved right now.

Recovery is the one inferential step here and it is bounded: the *list*
is exact, taken from MTGO's own record. The only guess is which named
deck a lost version belonged to, decided by card overlap against known
versions, and only above a threshold high enough that "a different deck
entirely" is not a plausible reading.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Callable, Iterable

from metahunter_core.deck_files import maindeck_signature
from metahunter_core.event_kind import CASUAL, resolve as resolve_event_kind

log = logging.getLogger(__name__)

# How much of the maindeck two lists must share before we will call them
# versions of the same deck. Between editions of one deck the overlap is
# normally well above 0.9; two genuinely different decks in a format
# share lands and staples and land far below this.
SAME_DECK_OVERLAP = 0.70

# How long a list that was never played is kept once the deck file has
# moved past it. Long enough that a match started with it has certainly
# had its registration read from MTGO's log — the poller looks every
# fifteen seconds — and short enough that an evening of edits does not
# linger.
FORGET_AFTER_SECONDS = 30 * 60


def _maindeck_counts(cards: Iterable) -> dict[int, int]:
    """``{catalog_id: quantity}`` for the maindeck only."""
    out: dict[int, int] = {}
    for cid, qty, side in cards:
        if side:
            continue
        out[int(cid)] = out.get(int(cid), 0) + int(qty)
    return out


def overlap(a: Iterable, b: Iterable) -> float:
    """How much two maindecks share, 0 to 1.

    Counts copies, not just names: swapping a four-of for a four-of is a
    bigger change than trimming one copy, and the number should say so.
    """
    ca, cb = _maindeck_counts(a), _maindeck_counts(b)
    total = sum(ca.values()) + sum(cb.values())
    if total == 0:
        return 0.0
    shared = sum(min(ca.get(k, 0), cb.get(k, 0)) for k in set(ca) | set(cb))
    return (2 * shared) / total


def diff(previous: Iterable, current: Iterable) -> dict[str, list]:
    """What changed between two versions, as added/removed card counts.

    Reported per zone so "moved to the sideboard" does not read as a
    card being cut and an unrelated one added.
    """
    def by_zone(cards):
        main: dict[int, int] = {}
        side: dict[int, int] = {}
        for cid, qty, is_side in cards:
            bucket = side if is_side else main
            bucket[int(cid)] = bucket.get(int(cid), 0) + int(qty)
        return main, side

    pm, ps = by_zone(previous)
    cm, cs = by_zone(current)

    def delta(before, after):
        added, removed = [], []
        for cid in set(before) | set(after):
            d = after.get(cid, 0) - before.get(cid, 0)
            if d > 0:
                added.append([cid, d])
            elif d < 0:
                removed.append([cid, -d])
        added.sort(key=lambda x: -x[1])
        removed.sort(key=lambda x: -x[1])
        return added, removed

    ma, mr = delta(pm, cm)
    sa, sr = delta(ps, cs)
    return {
        "maindeck_added": ma,
        "maindeck_removed": mr,
        "sideboard_added": sa,
        "sideboard_removed": sr,
    }


# ---------------------------------------------------------------------------
# Lists by card name
# ---------------------------------------------------------------------------

def list_by_name(
    cards: Iterable, name_of: Callable[[int], str]
) -> tuple[dict[str, int], dict[str, int]]:
    """Maindeck and sideboard as ``{card name: copies}``.

    MTGO gives every printing its own catalog number, so the same list
    re-saved with a foil Force of Will reads as four cards out and four
    in. That happened: two "versions" shown twenty-one cards apart were
    the same seventy-five, differing only in which printings MTGO had
    picked. By name they are identical, which is what they are.
    """
    main: dict[str, int] = {}
    side: dict[str, int] = {}
    for cid, qty, is_side in cards:
        bucket = side if is_side else main
        name = name_of(int(cid))
        bucket[name] = bucket.get(name, 0) + int(qty)
    return main, side


def name_fingerprint(cards: Iterable, name_of: Callable[[int], str]) -> str:
    """Identity of a full 75 by card name, ignoring printings.

    The sideboard is included: a player who swaps two sideboard cards
    between leagues has registered a different list, and the record of
    each belongs to each.
    """
    main, side = list_by_name(cards, name_of)
    key = (
        "M|" + "|".join(f"{n}:{q}" for n, q in sorted(main.items()))
        + "#S|" + "|".join(f"{n}:{q}" for n, q in sorted(side.items()))
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def name_diff(
    older: Iterable, newer: Iterable, name_of: Callable[[int], str]
) -> dict[str, list[dict[str, Any]]]:
    """What changed between two lists, by card name, per zone."""
    om, osb = list_by_name(older, name_of)
    nm, nsb = list_by_name(newer, name_of)

    def delta(before: dict[str, int], after: dict[str, int]):
        added, removed = [], []
        for name in sorted(set(before) | set(after)):
            d = after.get(name, 0) - before.get(name, 0)
            if d > 0:
                added.append({"name": name, "quantity": d})
            elif d < 0:
                removed.append({"name": name, "quantity": -d})
        added.sort(key=lambda x: -x["quantity"])
        removed.sort(key=lambda x: -x["quantity"])
        return added, removed

    ma, mr = delta(om, nm)
    sa, sr = delta(osb, nsb)
    return {
        "maindeck_added": ma,
        "maindeck_removed": mr,
        "sideboard_added": sa,
        "sideboard_removed": sr,
    }


# ---------------------------------------------------------------------------
# What was played
# ---------------------------------------------------------------------------

def current_signatures(decks) -> set[str]:
    """Signatures of the lists saved in MTGO right now."""
    return {
        maindeck_signature((c.mtgo_id, c.quantity, c.sideboard) for c in d.cards)
        for d in decks
    }


def registered_signatures(registrations, *, counted_only: bool = False) -> set[str]:
    """Signatures MTGO recorded as registered for a match.

    ``counted_only`` leaves out lists registered only for friendly games.
    Friendly games never count toward anything, so a list only ever
    played in one is, for everything the player sees, unplayed.
    """
    out: set[str] = set()
    for r in registrations:
        cards = r.get("cards") or []
        if not cards:
            continue
        if counted_only and resolve_event_kind(
            text_log_kind=r.get("event_kind"), league_flag=r.get("is_league"),
        ) == CASUAL:
            continue
        out.add(maindeck_signature(cards))
    return out


def upload_keep(registrations, decks) -> set[str]:
    """Versions worth sending to the website: played for real, or current."""
    return (
        registered_signatures(registrations, counted_only=True)
        | current_signatures(decks)
    )


# ---------------------------------------------------------------------------
# Snapshot, recover, forget
# ---------------------------------------------------------------------------

def snapshot_decks(store, decks) -> int:
    """Record any saved deck whose list we have not seen. Returns new count."""
    new = 0
    for d in decks:
        cards = [[c.mtgo_id, c.quantity, 1 if c.sideboard else 0] for c in d.cards]
        sig = maindeck_signature((c.mtgo_id, c.quantity, c.sideboard) for c in d.cards)
        try:
            if store.record_deck_version(
                signature=sig,
                deck_id=d.deck_id,
                name=d.name,
                format=d.format,
                cards=cards,
                modified_at=d.modified_at,
                source="file",
            ):
                new += 1
                log.info("deck_history: new version of %r", d.name)
        except Exception as exc:  # noqa: BLE001 - history is never fatal
            log.warning("deck_history: could not record %r: %s", d.name, exc)
    return new


def recover_from_registrations(store) -> int:
    """Rebuild versions that existed before we started watching.

    Every registration is an exact decklist MTGO recorded. Any whose
    list we do not already know is a version that was overwritten before
    we could read it — the Tuesday list, in the case that prompted this.

    Returns the number of versions recovered.
    """
    try:
        registrations = store.registered_decks()
        known = {v["signature"]: v for v in store.deck_versions()}
    except Exception as exc:  # noqa: BLE001
        log.warning("deck_history: could not read history: %s", exc)
        return 0

    recovered = 0
    for reg in registrations:
        cards = reg.get("cards") or []
        if not cards:
            continue
        sig = maindeck_signature(cards)
        if sig in known:
            continue

        # An unknown list. Find the deck it most resembles, among the
        # versions we do know.
        best_deck_id, best_name, best_score = None, None, 0.0
        for v in known.values():
            score = overlap(cards, v["cards"])
            if score > best_score:
                best_deck_id, best_name, best_score = v["deck_id"], v["name"], score

        attach = best_score >= SAME_DECK_OVERLAP
        cards_norm = [[int(c[0]), int(c[1]), int(bool(c[2]))] for c in cards]
        try:
            store.record_deck_version(
                signature=sig,
                deck_id=best_deck_id if attach else None,
                # An unattached version still needs a label. Naming it
                # after the deck it resembles would overstate what we
                # know, so it says plainly where it came from.
                name=(best_name if attach else "Earlier list"),
                format="Legacy",
                cards=cards_norm,
                modified_at=reg.get("captured_at"),
                source="registration",
                seen_at=reg.get("captured_at") or time.time(),
            )
            known[sig] = {
                "signature": sig, "deck_id": best_deck_id,
                "name": best_name or "Earlier list", "cards": cards_norm,
            }
            recovered += 1
            log.info(
                "deck_history: recovered a version played %s (%.0f%% like %r)",
                time.strftime("%Y-%m-%d", time.localtime(reg.get("captured_at") or 0)),
                best_score * 100, best_name or "nothing known",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("deck_history: could not recover a version: %s", exc)

    return recovered


def forget_unplayed(store, decks, *, now: float | None = None) -> int:
    """Delete lists that were saved but never played. Returns how many.

    Kept: every list MTGO recorded as registered for any match, and every
    list saved in MTGO right now. Everything else is an editing state.

    Two guards. Nothing is forgotten when no deck files were read at all,
    because an unreadable folder must not look like every deck having
    been deleted. And a list is kept for a while after the file moves
    past it, so a match started with it has time to be seen first.

    A list registered only for friendly games is kept here and hidden
    from the player elsewhere: hiding is reversible, deleting is not.
    """
    if not decks:
        return 0
    now = time.time() if now is None else now
    try:
        keep = current_signatures(decks) | registered_signatures(
            store.registered_decks()
        )
        doomed = [
            v["signature"] for v in store.deck_versions()
            if v["signature"] not in keep
            and (v.get("last_seen") or 0) < now - FORGET_AFTER_SECONDS
        ]
        if not doomed:
            return 0
        gone = store.forget_deck_versions(doomed)
        log.info("deck_history: forgot %d never-played list(s)", gone)
        return gone
    except Exception as exc:  # noqa: BLE001 - history is never fatal
        log.warning("deck_history: could not forget old lists: %s", exc)
        return 0


def sync(store, decks) -> dict[str, int]:
    """Snapshot the current files, recover anything missing, forget the rest."""
    new = snapshot_decks(store, decks)
    # Recovery runs before forgetting, so a list that turns out to have
    # been played is known before anything decides it was not.
    recovered = recover_from_registrations(store)
    forgotten = forget_unplayed(store, decks)
    return {"new": new, "recovered": recovered, "forgotten": forgotten}


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def upload_payload(
    versions: list[dict], keep: set[str] | None = None
) -> list[dict[str, Any]]:
    """Serialise deck versions for the server.

    Shared by the manual "upload decks" button and the background
    uploader so the two cannot disagree about what a deck upload is.

    ``keep`` limits the upload to those signatures — in practice
    ``upload_keep``: lists played for real, and the one saved now. The
    server deletes whatever a deck upload leaves out, so this is also
    how never-played saves leave the website.

    Versions with no established owning deck are left out: their cards
    are exact, but which deck they belong to is not, and inventing a
    parent on the server would undo the care taken not to invent one
    here.
    """
    from datetime import datetime, timezone

    def iso(ts: float | None) -> str:
        return datetime.fromtimestamp(ts or 0, timezone.utc).isoformat()

    out: list[dict[str, Any]] = []
    for v in versions:
        deck_uid = v.get("deck_id")
        if not deck_uid:
            continue
        if keep is not None and v["signature"] not in keep:
            continue
        changed = v.get("modified_at") or v.get("first_seen")
        out.append({
            "deck_uid": str(deck_uid)[:64],
            "name": (v.get("name") or "Untitled")[:160],
            "format": (v.get("format") or "Legacy")[:32],
            "signature": v["signature"][:128],
            "modified_at": iso(changed),
            "first_seen": iso(v.get("first_seen")),
            "last_seen": iso(v.get("last_seen")),
            "source": v.get("source") or "file",
            "cards": [[int(c[0]), int(c[1]), int(bool(c[2]))] for c in v["cards"]],
        })
    return out


def payload_fingerprint(payload: list[dict[str, Any]]) -> str:
    """Identity of a deck payload, for skipping unchanged uploads."""
    key = "|".join(sorted(f"{d['deck_uid']}:{d['signature']}" for d in payload))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def group_versions(versions: list[dict]) -> dict[str, list[dict]]:
    """Versions grouped into decks, newest version first within each.

    Keyed by ``deck_id`` where we have one. Versions recovered without a
    confident owner are grouped by their own signature, so they appear
    as their own entry rather than being quietly folded into a deck they
    may not belong to.
    """
    groups: dict[str, list[dict]] = {}
    for v in versions:
        key = v.get("deck_id") or f"orphan:{v['signature']}"
        groups.setdefault(key, []).append(v)
    for rows in groups.values():
        rows.sort(key=lambda v: v.get("modified_at") or v["first_seen"], reverse=True)
    return groups
