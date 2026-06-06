"""Launch the Metahunter local web app.

Behaviour:
  * Runs an incremental ingest first so the DB reflects the latest matches.
  * Starts the FastAPI backend on http://127.0.0.1:8765.
  * Opens the user's default browser to http://metahunter.localhost:8765
    (modern browsers resolve *.localhost to 127.0.0.1 automatically).
  * If the React build at web/dist exists, the backend serves the SPA;
    otherwise prints a hint to run ``npm run dev`` for the dev UI.

Usage:
    python scripts/serve.py
    python scripts/serve.py --port 9000
    python scripts/serve.py --no-open
"""

from __future__ import annotations

import argparse
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from threading import Lock, Timer

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from mtgo_meta.api.app import create_app
from mtgo_meta.config import find_mtgo_appfiles_dirs
from mtgo_meta.ingest import ingest_all
from metahunter_core.parser import parse_game_log
from mtgo_meta.paths import default_db_path, is_frozen, user_data_dir, web_dist_dir
from mtgo_meta.store import MatchStore

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = default_db_path()
WEB_DIST = web_dist_dir()

# How long to wait after the last filesystem event on a log file before
# trying to parse it — MTGO writes the file incrementally during a match.
DEBOUNCE_SECONDS = 3.0


class _LogHandler(FileSystemEventHandler):
    """Background watcher: parses new/changed Match_GameLog files into SQLite."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
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
        try:
            conn = sqlite3.connect(self.db_path)
            store = MatchStore(conn)
            if store.has_fresh(path):
                conn.close()
                return
            pm = parse_game_log(path)
            if not pm.players:
                conn.close()
                return
            store.upsert_match(path, pm)
            conn.close()
            score = f"{pm.match_score[0]}-{pm.match_score[1]}" if pm.match_score else "?"
            winner = pm.match_winner or "?"
            print(
                f"[{time.strftime('%H:%M:%S')}] new match: {pm.match_id[:8]} "
                f"{' vs '.join(pm.players)} → {winner} {score}",
                flush=True,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  watcher error on {path.name}: {e}", file=sys.stderr)


def _start_watcher(db_path: Path) -> Observer | None:
    folders = find_mtgo_appfiles_dirs()
    if not folders:
        print("  watcher: MTGO AppFiles folder not found; skipping.",
              file=sys.stderr)
        return None
    obs = Observer()
    handler = _LogHandler(db_path)
    for folder in folders:
        obs.schedule(handler, str(folder), recursive=False)
        print(f"  watcher: live on {folder}")
    obs.daemon = True
    obs.start()
    return obs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--no-open", action="store_true",
                        help="Don't auto-open the browser.")
    parser.add_argument("--no-ingest", action="store_true",
                        help="Don't run an incremental ingest at startup.")
    parser.add_argument("--no-watch", action="store_true",
                        help="Don't start the live file watcher.")
    args = parser.parse_args()

    # Welcome banner — visible to non-technical friends running the .exe.
    print("=" * 62)
    print("  Metahunter — your local MTGO match analyser")
    print(f"  Data dir: {user_data_dir()}")
    print("  Don't close this window while the dashboard is open.")
    print("=" * 62)
    print()

    if not args.no_ingest:
        print("Scanning your MTGO match logs...")
        ingest_all(db_path=Path(args.db))

    if not WEB_DIST.exists():
        print(
            "\n[hint] No React build found at web/dist.\n"
            "       For a live dev UI, run in another terminal:\n"
            "         cd web && npm run dev\n"
            "       and open http://metahunter.localhost:5173.\n"
            "       Or build a static bundle:  cd web && npm run build\n",
            file=sys.stderr,
        )

    # Auto-fall-back if the requested port is taken (e.g. an old serve.py
    # is still running). Walks up by 1 until something works.
    port = args.port
    for candidate in range(args.port, args.port + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((args.host, candidate))
                port = candidate
                break
            except OSError:
                continue
    else:
        print(
            f"Could not find a free port between {args.port} and "
            f"{args.port + 19} on {args.host}. Stop the conflicting process "
            "and retry, or pass --port.",
            file=sys.stderr,
        )
        return 1
    if port != args.port:
        print(
            f"Port {args.port} was busy — using {port} instead.",
            file=sys.stderr,
        )

    if not args.no_watch:
        _start_watcher(Path(args.db))

    app = create_app(db_path=Path(args.db))

    # Phase 2: spin up the background uploader. It runs in a daemon
    # thread, so process exit kills it without ceremony. The first
    # sweep bails out cheaply (no consent / no corpus); only AFTER
    # the user accepts the consent dialog will it start posting
    # matches.
    from mtgo_meta.upload.worker import Uploader
    _uploader = Uploader()
    _uploader.start()

    # Phase 6: spin up the auto-updater. Polls GitHub Releases every
    # 12h. First poll fires immediately so the dashboard's update
    # banner is correct on the very first dashboard load instead of
    # waiting half a day.
    from mtgo_meta.updater.worker import get_singleton as _get_updater
    _updater = _get_updater()
    _updater.start()

    public_url = f"http://metahunter.localhost:{port}"
    direct_url = f"http://{args.host}:{port}"
    print(f"\nMetahunter listening at:\n  {public_url}\n  {direct_url}\n")

    if not args.no_open:
        def _open() -> None:
            time.sleep(1.2)
            try:
                webbrowser.open(public_url)
            except Exception:
                webbrowser.open(direct_url)
        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(app, host=args.host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
