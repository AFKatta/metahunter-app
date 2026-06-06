"""Per-install identity: install_id (UUID) + install_salt (32 bytes).

The salt NEVER leaves the user's machine — it's used as the HMAC
secret for opponent usernames before they're sent to the server.
Same salt produces the same hash for the same opponent name across
all uploads from this install, so the server can correlate "this
opponent appeared 12 times in my matches" without ever knowing the
plaintext name.

State is stored at::

    %LOCALAPPDATA%/Metahunter/upload_state.json   (production)
    <repo>/data/upload_state.json                  (dev)

This module owns the file format; everything else reads/writes
through ``load_or_create_state()``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from mtgo_meta.paths import user_data_dir


def _state_path() -> Path:
    return user_data_dir() / "upload_state.json"


def load_or_create_state() -> tuple[uuid.UUID, bytes]:
    """Return (install_id, install_salt), creating both if missing.

    Idempotent — repeated calls return the same values for the life
    of the install. Generates new values only if the state file is
    missing or corrupted.
    """
    path = _state_path()
    if path.exists():
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
            return uuid.UUID(blob["install_id"]), bytes.fromhex(blob["install_salt"])
        except (KeyError, ValueError, json.JSONDecodeError):
            # Corrupted state — regenerate. The user loses the
            # ability to correlate previous hashed opponents with
            # new uploads, but the server still accepts everything
            # under the (new) install_id.
            pass
    install_id = uuid.uuid4()
    install_salt = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "install_id": str(install_id),
            "install_salt": install_salt.hex(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2),
        encoding="utf-8",
    )
    return install_id, install_salt


def get_install_id() -> uuid.UUID:
    """Convenience: just the install_id."""
    install_id, _ = load_or_create_state()
    return install_id


def hash_username(salt: bytes, username: str) -> str:
    """HMAC-SHA256 of an MTGO username, hex-encoded.

    The same MTGO opponent produces different hashes on different
    installs (because each has its own random salt), so the server
    can't trivially correlate "opponent X on install A = opponent X
    on install B" — they'd need to brute-force the 16-char username
    space against a salt that never leaves the user's machine.
    """
    return hmac.new(salt, username.encode("utf-8"), hashlib.sha256).hexdigest()
