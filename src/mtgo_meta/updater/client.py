"""GitHub Releases poller + version comparison.

Uses urllib (no extra deps so the frozen .exe stays slim). Returns
typed records for the updater worker to act on.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from urllib import error as urlerror
from urllib import request as urlrequest

from mtgo_meta import net

from mtgo_meta import __version__

log = logging.getLogger(__name__)

# Stable GitHub Releases URL. Falls through to the latest published
# release (drafts and pre-releases are excluded by default).
GITHUB_LATEST_URL = (
    "https://api.github.com/repos/AFKatta/metahunter-app/releases/latest"
)

# What the installer asset is called. Every release attaches a
# stable-named copy of the installer, so the URL doesn't bake in a
# version that breaks the moment we ship v0.3.
INSTALLER_ASSET_NAME = "Metahunter-Setup.exe"

CURRENT_VERSION = __version__


@dataclass(slots=True)
class ReleaseInfo:
    """What we know about the latest published release."""

    tag: str                 # e.g. "v0.2.0"
    name: str                # the release title
    body: str                # the release notes markdown
    installer_url: str | None
    """Direct browser_download_url for the stable-named installer
    asset, or None if the release doesn't have one (yet — v0.1.0
    only has the zip)."""


@dataclass(slots=True)
class UpdateStatus:
    current_version: str
    latest_version: str | None
    update_available: bool
    installer_url: str | None
    release_notes: str | None
    error: str | None = None


# ---------------------------------------------------------------------------
# Version parsing
# ---------------------------------------------------------------------------

def parse_version(s: str) -> tuple[int, ...]:
    """Strip ``v`` / ``-rc1`` etc, return a comparable tuple.

    Lexicographic comparison breaks ('0.10.0' < '0.9.0' as strings)
    so every comparison has to go through this.
    """
    s = (s or "").lstrip("vV")
    # Drop any pre-release / build suffix after the first '-'.
    s = s.split("-", 1)[0]
    parts = s.split(".")
    out: list[int] = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            break
    return tuple(out) or (0,)


def is_newer(remote: str, local: str = CURRENT_VERSION) -> bool:
    return parse_version(remote) > parse_version(local)


# ---------------------------------------------------------------------------
# GitHub fetch
# ---------------------------------------------------------------------------

def fetch_latest_release(*, timeout: float = 15.0) -> ReleaseInfo | None:
    """Hit GitHub Releases API. Returns None on network failure (we
    treat that as 'no update info, leave it alone' — never as an
    error the user should see)."""
    req = urlrequest.Request(
        GITHUB_LATEST_URL,
        headers={
            # GitHub's API requires a User-Agent; default urllib UA
            # gets rate-limited harder.
            "User-Agent": f"metahunter-updater/{CURRENT_VERSION}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with net.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (urlerror.URLError, json.JSONDecodeError) as exc:
        log.warning("updater: GitHub fetch failed: %s", exc)
        return None

    installer_url: str | None = None
    for asset in data.get("assets", []):
        if asset.get("name") == INSTALLER_ASSET_NAME:
            installer_url = asset.get("browser_download_url")
            break

    return ReleaseInfo(
        tag=str(data.get("tag_name") or ""),
        name=str(data.get("name") or ""),
        body=str(data.get("body") or ""),
        installer_url=installer_url,
    )


def status() -> UpdateStatus:
    """High-level: 'is there an update I should tell the user about?'.
    Always safe to call — network failures collapse to
    ``update_available=False`` + an error string the UI can ignore."""
    release = fetch_latest_release()
    if release is None:
        return UpdateStatus(
            current_version=CURRENT_VERSION,
            latest_version=None,
            update_available=False,
            installer_url=None,
            release_notes=None,
            error="github unreachable",
        )
    avail = is_newer(release.tag)
    return UpdateStatus(
        current_version=CURRENT_VERSION,
        latest_version=release.tag,
        update_available=avail,
        installer_url=release.installer_url if avail else None,
        release_notes=release.body if avail else None,
        error=None,
    )
