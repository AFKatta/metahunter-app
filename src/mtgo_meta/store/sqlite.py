"""Local SQLite store for parsed MTGO matches.

Schema is intentionally narrow — one row per match, with the parsed
game-by-game structure and bag-of-cards stored as JSON inside the row.
This keeps the indexing surface small (we mostly query by date, match
id, or username) and lets us evolve the parsed-data shape without
running ALTER TABLE migrations.

Tables
------
``matches``
    Per-match facts (players, format hint, outcome, parsed cards).

``logs``
    File-level metadata so we can do incremental ingest — skip
    already-parsed files unless their mtime advanced or hash changed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from mtgo_meta.parser.game_log import ParsedMatch

SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

-- General-purpose key/value table. Used right now for parser_version
-- so ingest can detect when the attribution rules have changed and
-- force a full re-parse of the existing logs.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    path        TEXT PRIMARY KEY,
    match_id    TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    sha1        TEXT NOT NULL,
    ingested_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    match_id      TEXT PRIMARY KEY,
    log_path      TEXT NOT NULL,
    log_mtime     REAL NOT NULL,
    players_json  TEXT NOT NULL,    -- JSON list, in join order
    first_player  TEXT,
    turns         INTEGER NOT NULL,
    match_winner  TEXT,
    score_won     INTEGER,
    score_lost    INTEGER,
    games_json    TEXT NOT NULL,    -- JSON list of {winner, loser, by_concede}
    cards_json    TEXT NOT NULL,    -- JSON {player: [card_name, ...]}
    cards_cast_json TEXT,           -- JSON {player: [card_name, ...]} – cast-only
    format        TEXT,             -- "Legacy" / "Vintage" / etc.
    parsed_at     REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_matches_mtime ON matches (log_mtime DESC);
"""

# Idempotent column adds for users upgrading from an older schema. SQLite
# will raise OperationalError("duplicate column") if the column already
# exists; we swallow that.
_ADD_COLUMN_SQL = [
    "ALTER TABLE matches ADD COLUMN cards_cast_json TEXT",
    "ALTER TABLE matches ADD COLUMN format TEXT",
]

# Indexes that reference columns added by _ADD_COLUMN_SQL must run AFTER
# the ALTERs, otherwise SQLite errors out on a fresh-old-schema upgrade.
_POST_MIGRATION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_matches_format ON matches (format)",
]


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class MatchStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.executescript(SCHEMA_SQL)
        for stmt in _ADD_COLUMN_SQL:
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError as e:
                # "duplicate column name: ..." means it's already there.
                if "duplicate column" not in str(e).lower():
                    raise
        for stmt in _POST_MIGRATION_INDEXES:
            self.conn.execute(stmt)
        self.conn.execute("INSERT OR IGNORE INTO schema_version VALUES (?)",
                          (SCHEMA_VERSION,))
        self.conn.commit()

    # ------------------------------------------------------------------
    def has_fresh(self, path: Path) -> bool:
        """Return True if this log file is already ingested and up-to-date."""
        try:
            st = path.stat()
        except OSError:
            return False
        row = self.conn.execute(
            "SELECT mtime, size_bytes FROM logs WHERE path = ?", (str(path),)
        ).fetchone()
        return bool(row and row[0] >= st.st_mtime and row[1] == st.st_size)

    def upsert_match(self, path: Path, pm: ParsedMatch) -> None:
        st = path.stat()
        score_won, score_lost = (
            (pm.match_score[0], pm.match_score[1])
            if pm.match_score else (None, None)
        )
        now = datetime.now(timezone.utc).timestamp()
        self.conn.execute(
            "INSERT OR REPLACE INTO matches "
            "(match_id, log_path, log_mtime, players_json, first_player, "
            " turns, match_winner, score_won, score_lost, games_json, "
            " cards_json, cards_cast_json, format, parsed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pm.match_id, str(path), st.st_mtime,
                json.dumps(pm.players),
                pm.first_player, pm.turns, pm.match_winner,
                score_won, score_lost,
                json.dumps([g.model_dump() for g in pm.game_outcomes]),
                json.dumps(pm.cards_by_player),
                json.dumps(pm.cards_cast_by_player),
                pm.format,
                now,
            ),
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO logs "
            "(path, match_id, size_bytes, mtime, sha1, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(path), pm.match_id, st.st_size, st.st_mtime,
                _sha1(path), now,
            ),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    def iter_matches(
        self, *, since_mtime: float | None = None
    ) -> "list[StoredMatch]":
        q = "SELECT * FROM matches"
        args: tuple = ()
        if since_mtime is not None:
            q += " WHERE log_mtime >= ?"
            args = (since_mtime,)
        q += " ORDER BY log_mtime DESC"
        cur = self.conn.execute(q, args)
        cols = [d[0] for d in cur.description]
        return [StoredMatch(**dict(zip(cols, row))) for row in cur.fetchall()]

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]

    # ------------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str | int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
        self.conn.commit()


# Mirrors the matches row. Kept lightweight (not a Pydantic model) so
# the store has no heavy deps.
class StoredMatch:
    __slots__ = (
        "match_id", "log_path", "log_mtime", "players_json", "first_player",
        "turns", "match_winner", "score_won", "score_lost", "games_json",
        "cards_json", "cards_cast_json", "format", "parsed_at",
    )

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def players(self) -> list[str]:
        return json.loads(self.players_json)

    @property
    def games(self) -> list[dict]:
        return json.loads(self.games_json)

    @property
    def cards_by_player(self) -> dict[str, list[str]]:
        return json.loads(self.cards_json)

    @property
    def cards_cast_by_player(self) -> dict[str, list[str]]:
        """Cast-only bag, or empty when an old row pre-dates this column."""
        raw = self.cards_cast_json
        return json.loads(raw) if raw else {}


@contextmanager
def open_store(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        yield MatchStore(conn)
    finally:
        conn.close()
