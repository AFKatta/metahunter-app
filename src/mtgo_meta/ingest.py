"""Ingest MTGO match logs into the local SQLite store.

This module is the single source of truth for the parsing-and-store
pipeline. The ``scripts/ingest.py`` CLI calls it. ``scripts/serve.py``
also calls it on startup so a frozen .exe doesn't need to shell out
to a separate script.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

from mtgo_meta.config import find_mtgo_appfiles_dirs
from metahunter_core.parser import parse_game_log
from metahunter_core.parser.game_history import build_format_index, format_for_mtime
from metahunter_core.parser.game_log import PARSER_VERSION
from mtgo_meta.paths import default_db_path
from mtgo_meta.store import open_store


@dataclass
class IngestResult:
    ingested: int
    skipped: int
    errored: int
    total_in_db: int
    elapsed_seconds: float


def ingest_all(
    db_path: Path | None = None,
    full: bool = False,
    folder: Path | None = None,
    quiet: bool = False,
) -> IngestResult:
    """Walk every MTGO AppFiles folder and (re)parse logs into SQLite.

    MTGO leaves old ClickOnce versions on disk after updating; each one
    has its own slice of match logs. We must scan all of them to get
    the user's full history.
    """
    db = db_path or default_db_path()
    if folder is not None:
        sources = [folder]
    else:
        sources = find_mtgo_appfiles_dirs()
    if not sources:
        if not quiet:
            print("Could not locate MTGO AppFiles folder.", file=sys.stderr)
        return IngestResult(0, 0, 0, 0, 0.0)

    logs: list[Path] = []
    history_paths: list[Path] = []
    for src in sources:
        if not quiet:
            print(f"  scanning {src}")
        logs.extend(src.glob("Match_GameLog_*.dat"))
        hist = src / "mtgo_game_history"
        if hist.exists():
            history_paths.append(hist)
    if not quiet:
        print(f"Found {len(logs)} log files across "
              f"{len(sources)} MTGO version folder(s).")

    # Build the format index from MTGO's own match-history records. Each
    # entry is (start_time_unix, format_name) — we look up by the log
    # file's mtime, which lands within ~2 hours of the match's start.
    # This is the authoritative source: MTGO knows what queue/event the
    # match came from. We only fall back to the card-based Vintage
    # heuristic when a log's mtime doesn't match any history entry.
    format_index = build_format_index(history_paths) if history_paths else []
    if not quiet and format_index:
        print(f"Loaded {len(format_index)} match-format records from "
              f"mtgo_game_history.")

    start = time.monotonic()
    ingested = skipped = errored = 0
    with open_store(db) as store:
        # If the parser's attribution rules have changed since the last
        # ingest, force a full re-parse so already-stored rows pick up
        # the new logic. This is what makes hyphen-name fixes /
        # reanimation suppression / FoW-pitch attribution actually
        # apply to old matches without the user knowing to delete the
        # database.
        try:
            stored_version = int(store.get_meta("parser_version") or 0)
        except ValueError:
            stored_version = 0
        if stored_version < PARSER_VERSION:
            if not quiet and stored_version > 0:
                print(
                    f"  Parser rules updated ({stored_version} -> "
                    f"{PARSER_VERSION}). Re-parsing every log so the "
                    "stored data reflects the new logic..."
                )
            full = True

        for p in logs:
            if not full and store.has_fresh(p):
                skipped += 1
                continue
            try:
                pm = parse_game_log(p)
            except Exception as e:  # noqa: BLE001
                errored += 1
                if not quiet:
                    print(f"  parse error {p.name}: {e}", file=sys.stderr)
                continue
            if not pm.players:
                skipped += 1
                continue
            # Set the match format from MTGO's own record. If the
            # mtime can't be looked up or the history file's
            # description doesn't include a recognised format keyword,
            # we leave the default ("Legacy") in place — there's no
            # card-based guess to fight with anymore, so an unresolved
            # match shows up under the default format rather than
            # disagreeing with what MTGO actually recorded.
            if format_index:
                try:
                    mt = p.stat().st_mtime
                except OSError:
                    mt = None
                if mt is not None:
                    resolved = format_for_mtime(format_index, mt)
                    if resolved is not None and resolved != "Unknown":
                        pm.format = resolved
            store.upsert_match(p, pm)
            ingested += 1
        store.set_meta("parser_version", PARSER_VERSION)
        total = store.count()

    elapsed = time.monotonic() - start
    if not quiet:
        print(
            f"Ingested {ingested} / skipped {skipped} / errors {errored} "
            f"in {elapsed:.1f}s. DB now holds {total} matches at {db}."
        )
    return IngestResult(ingested, skipped, errored, total, elapsed)
