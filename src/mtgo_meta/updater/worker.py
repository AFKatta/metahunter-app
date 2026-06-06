"""Background updater thread.

Started from serve.py on app boot. Polls GitHub Releases every N
hours, downloads the new installer in the background as soon as one
is available, then surfaces the "ready to install" state via the
local API for the dashboard's update banner to consume.

Only triggers the install when the user explicitly asks for it
(POST /api/updater/install) — we never silently restart the app
under the user.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import client, download

log = logging.getLogger(__name__)

DEFAULT_POLL_HOURS = 12
"""How often the background thread checks. 12h is friendly to GitHub
rate limits (unauthed: 60/hr per IP) while still surfacing updates
on the same calendar day they're released."""


@dataclass(slots=True)
class UpdaterState:
    status: client.UpdateStatus
    """Last polled status from GitHub."""
    downloaded_installer: Path | None = None
    """Filesystem path to the new installer, once downloaded."""
    download_pct: float | None = None
    """0–100 while downloading; None when idle or done."""
    last_checked_at: float = 0.0
    last_error: str | None = None


class Updater:
    """Single-process updater state + worker thread."""

    def __init__(self, *, poll_hours: int = DEFAULT_POLL_HOURS) -> None:
        self.poll_hours = poll_hours
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = UpdaterState(
            status=client.UpdateStatus(
                current_version=client.CURRENT_VERSION,
                latest_version=None,
                update_available=False,
                installer_url=None,
                release_notes=None,
            ),
        )
        self._lock = threading.Lock()

    # ---- thread loop --------------------------------------------------

    def _run(self) -> None:
        log.info("updater: thread started (poll %dh)", self.poll_hours)
        while not self._stop_event.is_set():
            try:
                self.check_now()
            except Exception:
                log.exception("updater: unexpected sweep error")
            self._stop_event.wait(self.poll_hours * 3600)
        log.info("updater: thread exiting")

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="metahunter-updater", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout)

    # ---- public API ---------------------------------------------------

    def state(self) -> UpdaterState:
        with self._lock:
            return UpdaterState(
                status=self._state.status,
                downloaded_installer=self._state.downloaded_installer,
                download_pct=self._state.download_pct,
                last_checked_at=self._state.last_checked_at,
                last_error=self._state.last_error,
            )

    def check_now(self) -> UpdaterState:
        """Synchronously poll GitHub + (if a new version is out)
        download the installer in the SAME thread. Returns the fresh
        state."""
        status = client.status()
        with self._lock:
            self._state.status = status
            self._state.last_checked_at = time.time()
            self._state.last_error = status.error
        if status.update_available and status.installer_url:
            # The downloaded_installer name encodes the target tag so
            # a partial older download can't be mistaken for the new
            # one across an app restart.
            target_name = f"Metahunter-Setup-{status.latest_version}.exe"
            existing = download.downloads_dir() / target_name
            if existing.exists():
                # Already downloaded — nothing to do.
                with self._lock:
                    self._state.downloaded_installer = existing
                    self._state.download_pct = None
                return self.state()
            try:
                with self._lock:
                    self._state.download_pct = 0.0
                path = download.download_installer(
                    status.installer_url,
                    target_name=target_name,
                    progress=self._on_progress,
                )
                with self._lock:
                    self._state.downloaded_installer = path
                    self._state.download_pct = None
            except Exception as exc:
                log.exception("updater: download failed")
                with self._lock:
                    self._state.last_error = f"download: {exc}"
                    self._state.download_pct = None
        return self.state()

    def _on_progress(self, done: int, total: int | None) -> None:
        if not total:
            return
        with self._lock:
            self._state.download_pct = min(100.0, 100.0 * done / total)

    # ---- install trigger ---------------------------------------------

    def install_and_exit(self) -> None:
        """Spawn the downloaded installer with /SILENT and exit this
        process. Inno Setup's installer takes care of closing any
        running Metahunter.exe (CloseApplications=force in the script)
        before overwriting the install dir.

        The caller is responsible for any pre-exit teardown — we just
        kick off the installer and die.
        """
        with self._lock:
            inst = self._state.downloaded_installer
        if inst is None or not inst.exists():
            raise RuntimeError("no installer downloaded yet")

        # Detached: parent (Python) exits, the installer keeps running.
        # CREATE_NEW_PROCESS_GROUP + DETACHED_PROCESS frees the child
        # from our console.
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        subprocess.Popen(
            [str(inst), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            creationflags=flags,
            close_fds=True,
        )
        # Give the installer a moment to spin up before we vanish.
        time.sleep(0.5)
        # Hard exit — uvicorn's normal shutdown can wedge if a request
        # is mid-flight, and we explicitly want the process gone so
        # Windows can overwrite Metahunter.exe.
        os._exit(0)


# Module-level singleton constructed once by serve.py.
_singleton: Updater | None = None


def get_singleton() -> Updater:
    global _singleton
    if _singleton is None:
        _singleton = Updater()
    return _singleton


# Used only in tests / dry-run.
def set_singleton(u: Updater) -> None:
    global _singleton
    _singleton = u


# Unused import suppressor (sys imported for hints elsewhere).
_ = sys
