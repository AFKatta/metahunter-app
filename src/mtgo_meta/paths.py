"""Filesystem path resolution that works both in dev and in a
PyInstaller-packaged .exe.

Two distinct roots:

* **bundled_resource(...)** — read-only files that ship with the app:
  the React build at ``web/dist``, the archetype corpus, the Badaro
  rule set. In dev these live at ``<repo>/...``; in a frozen build
  they live under ``sys._MEIPASS``.

* **user_data_dir()** — user-writable storage: the SQLite DB,
  optional updated corpus, future settings. In dev this is
  ``<repo>/data`` so we don't pollute the user's home with test
  matches. In a frozen build it's ``%LOCALAPPDATA%/Metahunter`` so
  each Windows user keeps their own data.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Metahunter"


def is_frozen() -> bool:
    """True if running inside a PyInstaller-built .exe."""
    return bool(getattr(sys, "frozen", False))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def bundled_resource(*parts: str | Path) -> Path:
    """Resolve a read-only resource shipped with the app."""
    if is_frozen():
        # PyInstaller extracts datas to _MEIPASS at runtime.
        base = Path(getattr(sys, "_MEIPASS"))
    else:
        base = _repo_root()
    return base.joinpath(*[str(p) for p in parts])


def user_data_dir() -> Path:
    """Per-user writable data dir; created if missing."""
    if is_frozen():
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA")
                        or Path.home() / "AppData" / "Local")
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path(os.environ.get("XDG_DATA_HOME")
                        or Path.home() / ".local" / "share")
        path = base / APP_NAME
    else:
        path = _repo_root() / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


# Convenient pre-resolved paths.
def default_db_path() -> Path:
    return user_data_dir() / "mtgo-meta.sqlite"


def corpus_path(format_name: str = "Legacy") -> Path:
    """Where the corpus JSON for ``format_name`` lives.

    Looks for a user-updated copy at
    ``%LOCALAPPDATA%/Metahunter/corpus/<format>.json`` first; falls
    back to the snapshot bundled with the .exe. Format names are
    normalised to lowercase for the filename — "Legacy" / "legacy"
    both map to ``legacy.json``.
    """
    fname = f"{format_name.lower()}.json"
    user_copy = user_data_dir() / "corpus" / fname
    if user_copy.exists():
        return user_copy
    return bundled_resource("data", "corpus", fname)


def available_corpora() -> list[str]:
    """Return the TitleCase format names that have a usable corpus file.

    Scans both the bundled snapshot dir and the user-writable copy
    dir. A format appears once even if both have a file (the user
    copy takes precedence when reading).
    """
    seen: set[str] = set()
    for base in (
        user_data_dir() / "corpus",
        bundled_resource("data", "corpus"),
    ):
        if base.exists():
            for p in base.glob("*.json"):
                seen.add(p.stem)
    # Capitalise so "legacy" → "Legacy" matches the API format param.
    return sorted({s.capitalize() for s in seen})


def format_data_dir() -> Path:
    """Where Badaro/MTGOFormatData lives (read-only, bundled)."""
    return bundled_resource("data", "MTGOFormatData")


def web_dist_dir() -> Path:
    """Where the built React frontend lives (read-only, bundled)."""
    return bundled_resource("web", "dist")
