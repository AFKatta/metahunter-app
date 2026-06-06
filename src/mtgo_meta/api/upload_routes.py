"""Local /api/upload/* routes used by the desktop frontend.

These are NOT exposed to the public internet — they're served on
localhost by serve.py, alongside the existing dashboard endpoints,
and only the bundled React frontend talks to them.

Endpoints:

  GET    /api/upload/state                read consent + identity
  POST   /api/upload/consent              record consent (first run)
  PATCH  /api/upload/leaderboard          flip leaderboard_opt_in
  DELETE /api/upload/all                  wipe local consent +
                                          server-side data
  POST   /api/upload/sweep                trigger an immediate
                                          worker sweep (debug /
                                          tests; usually the
                                          background thread handles
                                          this)
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mtgo_meta.upload import consent as consent_mod
from mtgo_meta.upload.client import CLIENT_VERSION, UploadClient
from mtgo_meta.upload.state import load_or_create_state

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/upload", tags=["upload"])


# ---------------------------------------------------------------------------
# State read
# ---------------------------------------------------------------------------

class UploadStateResponse(BaseModel):
    install_id: str
    consented_at: str | None
    has_consented: bool
    leaderboard_opt_in: bool
    server_url: str


@router.get("/state", response_model=UploadStateResponse)
def get_state() -> UploadStateResponse:
    install_id, _ = load_or_create_state()
    c = consent_mod.load()
    client = UploadClient()
    return UploadStateResponse(
        install_id=str(install_id),
        consented_at=c.consented_at,
        has_consented=c.has_consented,
        leaderboard_opt_in=c.leaderboard_opt_in,
        server_url=client.url,
    )


# ---------------------------------------------------------------------------
# Record consent (first-run dialog)
# ---------------------------------------------------------------------------

class ConsentRequest(BaseModel):
    leaderboard_opt_in: bool = True


@router.post("/consent", response_model=UploadStateResponse)
def post_consent(req: ConsentRequest) -> UploadStateResponse:
    """Record consent + register the install with the cloud server.

    Idempotent: the user can re-confirm (e.g. after re-checking the
    box) — we just refresh the consented_at timestamp and re-register.
    """
    c = consent_mod.record_consent(
        leaderboard_opt_in=req.leaderboard_opt_in,
        client_version=CLIENT_VERSION,
    )
    install_id, _ = load_or_create_state()
    client = UploadClient()
    status, body = client.register(
        install_id,
        consented_at=c.consented_at or "",
        leaderboard_opt_in=c.leaderboard_opt_in,
    )
    if status >= 400:
        # Roll back — don't claim "consented" if the server didn't
        # acknowledge. Without the server-side install row, uploads
        # would all fail with 403.
        consent_mod.reset()
        log.error("upload: register failed (%s): %s", status, body)
        raise HTTPException(status, f"server register failed: {body}")
    return UploadStateResponse(
        install_id=str(install_id),
        consented_at=c.consented_at,
        has_consented=c.has_consented,
        leaderboard_opt_in=c.leaderboard_opt_in,
        server_url=client.url,
    )


# ---------------------------------------------------------------------------
# Flip leaderboard preference
# ---------------------------------------------------------------------------

class LeaderboardRequest(BaseModel):
    leaderboard_opt_in: bool


@router.patch("/leaderboard", response_model=UploadStateResponse)
def patch_leaderboard(req: LeaderboardRequest) -> UploadStateResponse:
    c = consent_mod.load()
    if not c.has_consented:
        raise HTTPException(400, "must consent before changing this preference")
    c = consent_mod.set_leaderboard_opt_in(req.leaderboard_opt_in)
    install_id, _ = load_or_create_state()
    client = UploadClient()
    # Fire-and-forget — if the server is unreachable we keep the
    # local preference; the next successful upload's register call
    # will catch us up.
    client.patch_leaderboard(install_id, c.leaderboard_opt_in)
    return UploadStateResponse(
        install_id=str(install_id),
        consented_at=c.consented_at,
        has_consented=c.has_consented,
        leaderboard_opt_in=c.leaderboard_opt_in,
        server_url=client.url,
    )


# ---------------------------------------------------------------------------
# Wipe everything
# ---------------------------------------------------------------------------

@router.delete("/all", status_code=204)
def wipe_all() -> None:
    """Wipe server-side data + revoke local consent.

    Effects in order:
      1. DELETE /v1/install/{id} on the cloud server, which cascades
         through uploads and removes the canonical merged matches
         this install contributed (unless the OTHER side of those
         matches kept them alive via cross-install merge).
      2. Delete the local ``consent.json`` so the dashboard re-shows
         the consent dialog on next load.

    The local install_id + salt are KEPT — the user can re-consent
    later and re-upload their existing local match history; the
    same install_id will resurface on the server as a fresh row.
    """
    install_id, _ = load_or_create_state()
    client = UploadClient()
    status, body = client.delete_install(install_id)
    if status not in (200, 204):
        log.warning("upload: server delete returned %s: %s", status, body)
        # Don't fail the local wipe just because the cloud server
        # is unreachable — the user's intent is to stop sharing and
        # we honour that locally regardless.
    consent_mod.reset()
