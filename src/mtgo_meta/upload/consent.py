"""Per-install consent record.

The user MUST consent to data sharing on first run; the app refuses
to surface its dashboard until consent is recorded. Two pieces of
state are stored:

  * ``consented_at``        — ISO-8601 timestamp of when the
                              consent dialog was accepted. Absent
                              when consent has never been recorded.
  * ``leaderboard_opt_in``  — Whether the user's MTGO username
                              appears on public leaderboards /
                              feeds. Defaults to True; the user can
                              flip it from the Settings page.

Stored at::

    %LOCALAPPDATA%/Metahunter/consent.json   (production)
    <repo>/data/consent.json                  (dev)

Distinct from upload_state.json because consent is a user-facing
preference (could be inspected / hand-edited if needed), while the
install salt is a secret that we never want to invite a user to
poke at.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from mtgo_meta.paths import user_data_dir


def _consent_path() -> Path:
    return user_data_dir() / "consent.json"


@dataclass(slots=True)
class Consent:
    consented_at: str | None = None
    """ISO-8601 timestamp, or None if not yet consented."""
    leaderboard_opt_in: bool = True
    """Default is True — opt-OUT, not opt-IN."""
    client_version: str | None = None
    """The client version that recorded the consent."""

    @property
    def has_consented(self) -> bool:
        return self.consented_at is not None


def load() -> Consent:
    """Read consent.json or return a fresh un-consented record."""
    path = _consent_path()
    if not path.exists():
        return Consent()
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        return Consent(
            consented_at=blob.get("consented_at"),
            leaderboard_opt_in=bool(blob.get("leaderboard_opt_in", True)),
            client_version=blob.get("client_version"),
        )
    except json.JSONDecodeError:
        return Consent()


def save(consent: Consent) -> None:
    path = _consent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(consent), indent=2), encoding="utf-8")


def record_consent(
    leaderboard_opt_in: bool = True, client_version: str | None = None
) -> Consent:
    """Mark the install as consented NOW. Idempotent — overwrites any
    previous record with the same timestamp."""
    c = Consent(
        consented_at=datetime.now(timezone.utc).isoformat(),
        leaderboard_opt_in=leaderboard_opt_in,
        client_version=client_version,
    )
    save(c)
    return c


def set_leaderboard_opt_in(opt_in: bool) -> Consent:
    """Flip the leaderboard preference without re-recording the
    consent timestamp."""
    c = load()
    c.leaderboard_opt_in = opt_in
    save(c)
    return c


def reset() -> None:
    """Delete the consent record. Used when the user wipes their
    server-side data — the next dashboard load will re-show the
    consent dialog."""
    path = _consent_path()
    if path.exists():
        path.unlink()
