"""Stream the installer asset to a known location under the user
data dir, then return its filesystem path."""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

from mtgo_meta.paths import user_data_dir

log = logging.getLogger(__name__)


def downloads_dir() -> Path:
    """Where downloaded installers land. Under the user-data dir so
    we don't need write access to %TEMP% (which some corporate boxes
    restrict)."""
    d = user_data_dir() / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def download_installer(
    url: str, target_name: str = "Metahunter-Setup-pending.exe",
    *, timeout: float = 60.0, progress=None,
) -> Path:
    """Stream ``url`` to ``downloads_dir()/target_name`` and return
    the final path. Writes to a .part file first, renames on success
    — interrupted downloads never leave a partial file the updater
    would mistakenly try to run.

    ``progress`` callback (optional) is called with
    ``(bytes_done, total_bytes)`` every chunk; total may be None
    if the server didn't send Content-Length.
    """
    target = downloads_dir() / target_name
    part = target.with_suffix(target.suffix + ".part")

    req = urlrequest.Request(url, headers={"User-Agent": "metahunter-updater"})
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            total = resp.headers.get("Content-Length")
            total_i = int(total) if total and total.isdigit() else None
            done = 0
            with open(part, "wb") as fp:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    fp.write(chunk)
                    done += len(chunk)
                    if progress:
                        try:
                            progress(done, total_i)
                        except Exception:
                            pass
    except urlerror.URLError as exc:
        log.warning("updater: download failed: %s", exc)
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass
        raise

    # Atomic-ish rename — on Windows the rename fails if the target
    # exists, so wipe it first.
    if target.exists():
        target.unlink()
    shutil.move(str(part), str(target))
    log.info("updater: installer ready at %s", target)
    return target
