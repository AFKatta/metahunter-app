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
import os
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
from metahunter_core.text_log import find_log_files, parse_text_log
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


class _TextLogPoller(threading.Thread):
    """Reads MTGO's running text log for deck registrations.

    The decklist a player registers is written to that log and nowhere
    else, and MTGO rotates the file, so anything not captured while the
    app is open is gone. Polling rather than watching: the log is
    appended to constantly during play, and a filesystem event per write
    would be far noisier than simply looking every few seconds.

    Only files whose size has changed are re-read, so a steady state
    costs one stat() per file per tick.
    """

    #: MTGO appends continuously; this is often enough to catch a
    #: registration well within the game it belongs to.
    INTERVAL_SECONDS = 15.0

    def __init__(self, db_path: Path) -> None:
        super().__init__(name="metahunter-textlog", daemon=True)
        self.db_path = db_path
        self._stop = threading.Event()
        self._sizes: dict[str, int] = {}

    def stop(self) -> None:
        self._stop.set()

    def _snapshot_decks(self) -> None:
        """Record any deck list we have not seen before."""
        try:
            from metahunter_core.deck_files import load_decks
            from mtgo_meta import deck_history

            decks = load_decks(constructed_only=True)
            if not decks:
                return
            conn = sqlite3.connect(self.db_path)
            try:
                store = MatchStore(conn)
                result = deck_history.sync(store, decks)
                if result.get("new") or result.get("recovered") or result.get("forgotten"):
                    print(
                        f"  decks: {result['new']} new list(s), "
                        f"{result['recovered']} recovered, "
                        f"{result.get('forgotten', 0)} never-played forgotten"
                    )
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001 - never block the poller
            print(f"  decks: {e}", file=sys.stderr)

    @staticmethod
    def _store_league_messages(store, facts) -> int:
        """Keep MTGO's league lines, with a real date attached.

        A log line carries only a clock time. It is dated by when it is
        read: the log is rewritten every MTGO session, so a line is from
        today — unless its time is later than now, which means a session
        that ran past midnight, and so yesterday.
        """
        import hashlib
        from datetime import datetime, timedelta

        now = datetime.now()
        rows = []
        for msg in getattr(facts, "league_messages", []):
            try:
                hh, mm, ss = (int(x) for x in msg.time.split(":"))
                at = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
            except ValueError:
                continue
            if at > now + timedelta(minutes=1):
                at -= timedelta(days=1)
            key = hashlib.sha1(
                f"{msg.path}|{msg.offset}|{msg.time}|{msg.detail}".encode("utf-8")
            ).hexdigest()
            rows.append((key, at.timestamp(), msg.kind, msg.detail))
        return store.record_league_messages(rows) if rows else 0

    def run(self) -> None:
        # A first pass on startup picks up anything written while the
        # app was closed but before MTGO rotated the log.
        self._tick(first=True)
        while not self._stop.wait(self.INTERVAL_SECONDS):
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                print(f"  textlog: {e}", file=sys.stderr)

    def _tick(self, first: bool = False) -> None:
        # Snapshot the saved decks first, every tick. MTGO overwrites a
        # deck file the moment you edit it, so a list only exists between
        # one edit and the next — if we waited for someone to open the
        # Decks page, an edit made mid-league would take the list that
        # played it with it. This is the "memory" the deck history needs.
        self._snapshot_decks()

        changed = []
        for p in find_log_files():
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if self._sizes.get(str(p)) != size:
                self._sizes[str(p)] = size
                changed.append(p)
        if not changed:
            return

        facts = None
        for p in changed:
            facts = parse_text_log(p, facts)
        if facts is None:
            return

        stored = 0
        conn = sqlite3.connect(self.db_path)
        try:
            store = MatchStore(conn)
            # League lines can arrive in a session with no registration
            # in it, so they are kept before anything can return early.
            self._store_league_messages(store, facts)
            for game_id, rd in facts.decks_by_game.items():
                match_uuid = facts.match_by_game.get(game_id)
                store.upsert_registered_deck(
                    game_id=game_id,
                    username=rd.username,
                    signature=rd.signature(),
                    cards=[list(c) for c in rd.cards],
                    match_uuid=match_uuid,
                    is_league=facts.league_by_game.get(game_id),
                    event_kind=(
                        facts.event_for_match(match_uuid) if match_uuid else None
                    ),
                )
                stored += 1
        finally:
            conn.close()

        if stored and not first:
            print(
                f"[{time.strftime('%H:%M:%S')}] captured {stored} deck "
                f"registration(s) from MTGO's log",
                flush=True,
            )


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

    # Deck registrations live in MTGO's text log, not the .dat files the
    # observer above watches, so they need their own reader.
    poller = _TextLogPoller(db_path)
    poller.start()
    print("  watcher: reading MTGO text log for deck registrations")

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

    # Sign-in sends the browser back to 127.0.0.1:<port>/auth/callback,
    # so the API has to know the port we actually bound — which is not
    # always the one that was asked for.
    os.environ["METAHUNTER_LOCAL_PORT"] = str(port)

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
