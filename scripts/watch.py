"""Watch the MTGO AppFiles folder and ingest new/changed logs live.

Debounces by waiting briefly after each filesystem event so we don't
try to parse a file MTGO is still actively writing.

Usage:
    python scripts/watch.py            # default DB at data/mtgo-meta.sqlite
    python scripts/watch.py --db ...
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from threading import Lock, Timer

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mtgo_meta.config import find_mtgo_appfiles_dir
from mtgo_meta.parser import parse_game_log
from mtgo_meta.store import open_store

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "mtgo-meta.sqlite"

# Debounce delay: how long to wait after the last filesystem event
# before reading a file. MTGO writes the log incrementally as a match
# plays out, so we don't want to read partial files.
DEBOUNCE_SECONDS = 3.0


class LogHandler(FileSystemEventHandler):
    def __init__(self, store, watched_folder: Path):
        self.store = store
        self.watched = watched_folder
        self._timers: dict[str, Timer] = {}
        self._lock = Lock()

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if not path.name.startswith("Match_GameLog_") or path.suffix != ".dat":
            return
        with self._lock:
            t = self._timers.get(str(path))
            if t is not None:
                t.cancel()
            t = Timer(DEBOUNCE_SECONDS, self._process, args=(path,))
            self._timers[str(path)] = t
            t.start()

    def _process(self, path: Path) -> None:
        with self._lock:
            self._timers.pop(str(path), None)
        if not path.exists():
            return
        if self.store.has_fresh(path):
            return
        try:
            pm = parse_game_log(path)
        except Exception as e:
            print(f"  parse error {path.name}: {e}", file=sys.stderr)
            return
        if not pm.players:
            return
        self.store.upsert_match(path, pm)
        score = f"{pm.match_score[0]}-{pm.match_score[1]}" if pm.match_score else "?"
        winner = pm.match_winner or "?"
        print(
            f"[{time.strftime('%H:%M:%S')}] new match: {pm.match_id[:8]}  "
            f"{' vs '.join(pm.players)}  → {winner} {score}",
            flush=True,
        )


def main() -> int:
    args = sys.argv[1:]
    db = DEFAULT_DB
    while args:
        a = args.pop(0)
        if a == "--db" and args:
            db = Path(args.pop(0))
        else:
            print(f"Unknown arg: {a}", file=sys.stderr)
            return 2

    folder = find_mtgo_appfiles_dir()
    if folder is None:
        print("Could not locate MTGO AppFiles folder.", file=sys.stderr)
        return 1
    print(f"Watching: {folder}\nDB: {db}\nCtrl-C to stop.\n")

    with open_store(db) as store:
        handler = LogHandler(store, folder)
        obs = Observer()
        obs.schedule(handler, str(folder), recursive=False)
        obs.start()
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("\nShutting down.")
        finally:
            obs.stop()
            obs.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
