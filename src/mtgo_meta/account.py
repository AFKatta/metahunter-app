"""The signed-in Metahunter account, from the desktop side.

Sign-in uses the loopback redirect from RFC 8252, the standard shape
for a native app: we open the system browser at the API's sign-in URL,
the user authenticates with Discord or Google there, and the browser is
redirected back to this app's own local server carrying a session
token. The app never sees a password and no credential passes through
a field we drew.

Two things make that safe rather than merely convenient:

* The ``state`` nonce. We generate it, hand it to the API, and refuse
  any callback that comes back with a different one. Without it, any
  page the user visits could hit ``127.0.0.1:8765/auth/callback`` with
  a token of the attacker's choosing and silently sign the app into
  *their* account, quietly uploading this player's matches to it.
* A single-use handshake. The nonce is cleared the moment a callback
  is accepted, so a replayed URL does nothing.

The token is stored in the user data directory, not the install
directory, so it survives an app update.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mtgo_meta import net
from mtgo_meta.paths import user_data_dir
from mtgo_meta.upload.client import CLIENT_VERSION, server_url

log = logging.getLogger(__name__)

# A pending sign-in older than this is abandoned, so a nonce cannot sit
# around indefinitely waiting to be used.
PENDING_TTL_SECONDS = 600

# How long a cached copy of the account is trusted before we re-check
# with the server. The app has to work on a train, so this is a cache
# with a long fuse rather than a gate.
PROFILE_TTL_SECONDS = 6 * 60 * 60


def _token_path() -> Path:
    return user_data_dir() / "account.json"


@dataclass
class Account:
    """What we know about the signed-in user, cached locally."""

    token: str = ""
    user_id: str = ""
    display_name: str = ""
    avatar_url: str | None = None
    players: list[dict[str, Any]] = field(default_factory=list)
    checked_at: float = 0.0

    @property
    def signed_in(self) -> bool:
        return bool(self.token)

    @property
    def claimed_usernames(self) -> list[str]:
        return [p.get("mtgo_username", "") for p in self.players]

    def to_json(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "user_id": self.user_id,
            "display_name": self.display_name,
            "avatar_url": self.avatar_url,
            "players": self.players,
            "checked_at": self.checked_at,
        }


_lock = threading.Lock()
_cached: Account | None = None

# The in-flight sign-in: nonce -> started_at. Deliberately in memory
# only, so it cannot outlive the process that started it.
_pending: dict[str, float] = {}


def load() -> Account:
    """The stored account, or an empty one. Never raises."""
    global _cached
    with _lock:
        if _cached is not None:
            return _cached
        try:
            raw = json.loads(_token_path().read_text(encoding="utf-8"))
            _cached = Account(
                token=raw.get("token") or "",
                user_id=raw.get("user_id") or "",
                display_name=raw.get("display_name") or "",
                avatar_url=raw.get("avatar_url"),
                players=raw.get("players") or [],
                checked_at=float(raw.get("checked_at") or 0.0),
            )
        except (OSError, ValueError, TypeError):
            _cached = Account()
        return _cached


def save(account: Account) -> None:
    global _cached
    with _lock:
        _cached = account
        path = _token_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(account.to_json()), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:  # noqa: BLE001
            log.warning("could not save account: %s", exc)


def sign_out() -> None:
    """Forget the local session, and tell the server to revoke it."""
    account = load()
    if account.token:
        try:
            _request("POST", "/v1/auth/logout", token=account.token)
        except Exception:  # noqa: BLE001 - offline sign-out still signs out
            log.info("logout could not reach the server; clearing locally")
    save(Account())
    try:
        _token_path().unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# the browser hand-off
# ---------------------------------------------------------------------------

def start_sign_in(provider: str, callback_url: str) -> str:
    """Build the URL to open in the browser, and arm the nonce."""
    nonce = secrets.token_urlsafe(16)
    now = time.time()
    with _lock:
        # Drop anything stale so an abandoned attempt cannot be resumed.
        for key, started in list(_pending.items()):
            if now - started > PENDING_TTL_SECONDS:
                _pending.pop(key, None)
        _pending[nonce] = now

    redirect = f"{callback_url}?nonce={urllib.parse.quote(nonce)}"
    return (
        f"{server_url()}/v1/auth/{urllib.parse.quote(provider)}/start"
        f"?client=app&redirect={urllib.parse.quote(redirect, safe='')}"
    )


def accept_callback(nonce: str, token: str) -> Account:
    """Complete a sign-in. Raises ValueError if this was not our flow."""
    with _lock:
        started = _pending.pop(nonce, None)
    if started is None:
        raise ValueError("this sign-in was not started by the app")
    if time.time() - started > PENDING_TTL_SECONDS:
        raise ValueError("sign-in took too long; please try again")
    if not token:
        raise ValueError("no session token came back")

    account = Account(token=token)
    refresh(account, force=True)
    return load()


# ---------------------------------------------------------------------------
# server calls
# ---------------------------------------------------------------------------

def _request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: Any = None,
    timeout: int = 20,
) -> Any:
    data = None
    headers = {
        "Accept": "application/json",
        "User-Agent": CLIENT_VERSION,
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(
        f"{server_url()}{path}", data=data, headers=headers, method=method
    )
    with net.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def refresh(account: Account | None = None, *, force: bool = False) -> Account:
    """Re-read the profile from the server and cache it.

    A 401 means the session is genuinely gone — expired or signed out
    elsewhere — so the local copy is cleared. Any other failure is
    treated as "the network is down", which must not log anybody out:
    the app is expected to keep tracking matches offline.
    """
    account = account or load()
    if not account.token:
        return account

    fresh = time.time() - account.checked_at < PROFILE_TTL_SECONDS
    if fresh and not force:
        return account

    try:
        data = _request("GET", "/v1/me", token=account.token)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            log.info("session no longer valid; signing out locally")
            save(Account())
            return load()
        log.info("could not refresh account (%s); keeping cached copy", exc.code)
        return account
    except Exception as exc:  # noqa: BLE001 - offline, DNS, timeout, all equal
        log.info("could not reach the server (%s); keeping cached copy", exc)
        return account

    account.user_id = data.get("user_id") or ""
    account.display_name = data.get("display_name") or ""
    account.avatar_url = data.get("avatar_url")
    account.players = data.get("players") or []
    account.checked_at = time.time()
    save(account)
    return account


def claim_player(mtgo_username: str, install_id: str | None = None) -> dict[str, Any]:
    """Link an MTGO username to the signed-in account."""
    account = load()
    if not account.signed_in:
        raise PermissionError("not signed in")
    result = _request(
        "POST", "/v1/me/players",
        token=account.token,
        body={"mtgo_username": mtgo_username, "install_id": install_id},
    )
    refresh(force=True)
    return result or {}


def availability(mtgo_username: str) -> dict[str, Any]:
    """Whether an MTGO username can still be claimed."""
    account = load()
    if not account.signed_in:
        raise PermissionError("not signed in")
    quoted = urllib.parse.quote(mtgo_username, safe="")
    return _request(
        "GET", f"/v1/me/players/{quoted}/availability", token=account.token
    ) or {}


def upload_decks(decks: list[dict[str, Any]]) -> dict[str, Any]:
    """Replace the server's copy of this user's saved decks."""
    account = load()
    if not account.signed_in:
        raise PermissionError("not signed in")
    return _request(
        "PUT", "/v1/me/decks", token=account.token, body={"decks": decks}
    ) or {}
