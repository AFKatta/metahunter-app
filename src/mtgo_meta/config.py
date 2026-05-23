from __future__ import annotations

import os
from pathlib import Path


def find_mtgo_appfiles_dirs() -> list[Path]:
    """Locate every MTGO AppFiles folder that holds match logs.

    MTGO is a ClickOnce app: when it auto-updates it creates a new
    versioned folder and leaves the OLD one in place. Each version has
    its own ``AppFiles\\<hex>\\`` directory with its own slice of
    ``Match_GameLog_*.dat`` files. To see every match the user has
    ever played we have to read from all of them, not just the first.

    Returns the list sorted oldest → newest by directory mtime so the
    watcher and live UI naturally prefer the most-recent build.
    """
    local = Path(os.environ.get("LOCALAPPDATA", "")).resolve()
    apps_data = local / "Apps" / "2.0" / "Data"
    if not apps_data.is_dir():
        return []

    out: list[Path] = []
    for candidate in apps_data.rglob("AppFiles"):
        for sub in candidate.iterdir():
            if sub.is_dir() and any(sub.glob("Match_GameLog_*.dat")):
                out.append(sub)
    out.sort(key=lambda p: p.stat().st_mtime)
    return out


def find_mtgo_appfiles_dir() -> Path | None:
    """Backward-compat single-folder lookup.

    Prefer ``find_mtgo_appfiles_dirs()`` — this returns just the most
    recently-modified folder and silently ignores older MTGO versions.
    """
    dirs = find_mtgo_appfiles_dirs()
    return dirs[-1] if dirs else None
