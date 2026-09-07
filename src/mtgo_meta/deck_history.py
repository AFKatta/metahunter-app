"""Remember every version of every deck, because MTGO does not.

MTGO keeps one file per deck and overwrites it in place when you edit.
The list you registered for a league on Tuesday simply stops existing on
Wednesday, so the matches you played with it match nothing and lose
their attribution. That is not hypothetical — eleven league games did
exactly that, and the deck page showed a single run instead of two.

So this module does two things:

* **Snapshot.** Every time the saved decks are read, any list we have
  not seen before is written to ``deck_versions`` and never changed
  again. The file is mutable; the record is not.

* **Recover.** For matches MTGO already told us about, we hold the exact
  registered decklist. Where that list matches no version we know, it is
  a version we missed — usually one that existed before this feature, or
  before the app was installed. It gets stored, and attached to the deck
  it most resembles.

Recovery is the one inferential step here and it is bounded: the *list*
is exact, taken from MTGO's own record. The only guess is which named
deck a lost version belonged to, decided by card overlap against known
versions, and only above a threshold high enough that "a different deck
entirely" is not a plausible reading.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

from metahunter_core.deck_files import maindeck_signature

log = logging.getLogger(__name__)

# How much of the maindeck two lists must share before we will call them
# versions of the same deck. Between editions of one deck the overlap is
# normally well above 0.9; two genuinely different decks in a format
# share lands and staples and land far below this.
SAME_DECK_OVERLAP = 0.70


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


def sync(store, decks) -> dict[str, int]:
    """Snapshot the current files, then recover anything missing."""
    new = snapshot_decks(store, decks)
    # Recovery runs second so it can compare against everything on disk.
    recovered = recover_from_registrations(store)
    return {"new": new, "recovered": recovered}


def upload_payload(versions: list[dict]) -> list[dict[str, Any]]:
    """Serialise deck versions for the server.

    Shared by the manual "upload decks" button and the background
    uploader so the two cannot disagree about what a deck upload is.

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
    import hashlib

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
