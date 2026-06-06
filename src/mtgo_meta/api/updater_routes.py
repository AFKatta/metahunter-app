"""Local /api/updater/* routes — surfaces the in-app updater state
to the React dashboard.

Endpoints:

  GET  /api/updater/state    poll current vs latest version + dl progress
  POST /api/updater/check    force a check (background sweep is hourly)
  POST /api/updater/install  trigger the downloaded installer and exit
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from mtgo_meta.updater.worker import get_singleton

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/updater", tags=["updater"])


def _serialize(state) -> dict[str, Any]:
    s = state.status
    return {
        "current_version": s.current_version,
        "latest_version": s.latest_version,
        "update_available": s.update_available,
        "installer_url": s.installer_url,
        "release_notes": s.release_notes,
        "error": s.error,
        "downloaded": bool(state.downloaded_installer),
        "download_pct": state.download_pct,
        "last_checked_at": state.last_checked_at,
        "last_error": state.last_error,
    }


@router.get("/state")
def get_state() -> dict[str, Any]:
    """Return the cached state without hitting GitHub. The background
    worker keeps this up-to-date roughly every 12 hours."""
    return _serialize(get_singleton().state())


@router.post("/check")
def check_now() -> dict[str, Any]:
    """Synchronously poll GitHub + download the new installer if one
    is available. Used by the dashboard's 'Check for updates' button
    and by a once-on-boot trigger so users see the banner without
    waiting 12 hours."""
    return _serialize(get_singleton().check_now())


@router.post("/install")
def install_now() -> dict[str, Any]:
    """Run the downloaded installer and exit the app. Returns
    immediately — the caller's HTTP request is racing the process
    teardown; the frontend should treat any response as 'goodbye'.

    The new install replaces our files; when it relaunches
    Metahunter.exe the user lands on the dashboard again."""
    try:
        get_singleton().install_and_exit()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    # We never actually return — the os._exit() above kills us. If
    # somehow we did, hand back the (last known) state so the client
    # at least gets a clean shape.
    return _serialize(get_singleton().state())
