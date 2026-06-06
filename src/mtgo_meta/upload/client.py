"""Thin HTTP client for metahunter-api.fly.dev.

Uses only stdlib (urllib) so the .exe doesn't need to bundle
requests. Endpoints wrapped:

  POST   /v1/register
  POST   /v1/matches
  PATCH  /v1/install/{id}
  DELETE /v1/install/{id}

The default server URL is the production Fly.io deployment; can be
overridden with the ``METAHUNTER_SERVER_URL`` environment variable
(used during dev to point at a local uvicorn) or by passing
``server_url=`` explicitly.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

log = logging.getLogger(__name__)

DEFAULT_SERVER_URL = "https://metahunter-api.fly.dev"
CLIENT_VERSION = "metahunter-app/0.2.0"


def server_url() -> str:
    return os.environ.get("METAHUNTER_SERVER_URL", DEFAULT_SERVER_URL).rstrip("/")


@dataclass(slots=True)
class UploadResponse:
    received: int
    accepted: int
    new_matches: int
    merged_matches: int
    errors: list[dict[str, Any]]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "UploadResponse":
        return cls(
            received=d.get("received", 0),
            accepted=d.get("accepted", 0),
            new_matches=d.get("new_matches", 0),
            merged_matches=d.get("merged_matches", 0),
            errors=d.get("errors", []),
        )


class UploadClient:
    def __init__(self, url: str | None = None, *, timeout: float = 30.0) -> None:
        self.url = (url or server_url()).rstrip("/")
        self.timeout = timeout

    # ---- low-level -----------------------------------------------------

    def _request(
        self, method: str, path: str, body: dict | None = None,
    ) -> tuple[int, dict[str, Any]]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urlrequest.Request(
            f"{self.url}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method=method,
        )
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else {}
        except urlerror.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read())
            except Exception:
                return exc.code, {"detail": str(exc)}
        except urlerror.URLError as exc:
            # Network-level failure (DNS, refused, timeout).
            log.warning("metahunter-server unreachable: %s", exc)
            return 0, {"detail": f"network: {exc}"}

    # ---- endpoints -----------------------------------------------------

    def register(
        self,
        install_id: uuid.UUID,
        *,
        consented_at: str,
        leaderboard_opt_in: bool,
        parser_version: int | None = None,
    ) -> tuple[int, dict[str, Any]]:
        return self._request("POST", "/v1/register", {
            "install_id": str(install_id),
            "leaderboard_opt_in": leaderboard_opt_in,
            "consent_at": consented_at,
            "client_version": CLIENT_VERSION,
            "parser_version": parser_version,
        })

    def upload_batch(
        self, install_id: uuid.UUID, matches: list[dict],
    ) -> tuple[int, UploadResponse | dict[str, Any]]:
        status, body = self._request("POST", "/v1/matches", {
            "install_id": str(install_id),
            "matches": matches,
        })
        if status == 200:
            return status, UploadResponse.from_dict(body)
        return status, body

    def patch_leaderboard(
        self, install_id: uuid.UUID, opt_in: bool,
    ) -> tuple[int, dict[str, Any]]:
        return self._request(
            "PATCH", f"/v1/install/{install_id}",
            {"leaderboard_opt_in": opt_in},
        )

    def delete_install(self, install_id: uuid.UUID) -> tuple[int, dict[str, Any]]:
        return self._request("DELETE", f"/v1/install/{install_id}")

    def health(self) -> tuple[int, dict[str, Any]]:
        return self._request("GET", "/v1/health")
