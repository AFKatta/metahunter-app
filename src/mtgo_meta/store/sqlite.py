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
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from metahunter_core.parser.game_log import ParsedMatch

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

CREATE TABLE IF NOT EXISTS registered_decks (
    game_id       TEXT PRIMARY KEY,   -- MTGO's numeric game id
    match_uuid    TEXT,               -- links to Match_GameLog_<uuid>.dat
    username      TEXT NOT NULL,
    signature     TEXT NOT NULL,      -- catalog:qty:sideboard, sorted
    cards_json    TEXT NOT NULL,      -- [[catalog, qty, sideboard], ...]
    is_league     INTEGER,            -- 1 league, 0 friendly, NULL unknown
    event_kind    TEXT,               -- league / tournament / casual
    captured_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_regdecks_match ON registered_decks(match_uuid);

-- Every distinct version of every saved deck, kept forever.
--
-- MTGO stores one file per deck and *overwrites* it when you edit, so
-- the list you played last week stops existing the moment you change a
-- card. Matches from that week then match nothing and lose their
-- attribution — which is exactly what happened: eleven league games
-- played on a Tuesday became unattributable on the Wednesday.
--
-- So each time we read the deck files we record any list we have not
-- seen before. The file is mutable; this table is not.
CREATE TABLE IF NOT EXISTS deck_versions (
    signature     TEXT PRIMARY KEY,   -- sha256 of the maindeck
    deck_id       TEXT,               -- MTGO's file uuid; NULL if recovered
    name          TEXT NOT NULL,
    format        TEXT NOT NULL,
    cards_json    TEXT NOT NULL,      -- [[catalog, qty, sideboard], ...]
    first_seen    REAL NOT NULL,      -- when this list first appeared to us
    last_seen     REAL NOT NULL,
    modified_at   REAL,               -- the deck file's mtime, when known
    -- 'file'         read from the saved deck
    -- 'registration' reconstructed from a match MTGO recorded, because
    --                the file had already been overwritten
    source        TEXT NOT NULL DEFAULT 'file'
);
CREATE INDEX IF NOT EXISTS idx_deck_versions_deck ON deck_versions(deck_id);

-- Where the player says a league entry ended early.
--
-- MTGO writes no drop anywhere this app can read: the game log has no
-- event, the history file's Round is 0 for every league match, and the
-- text log is rewritten each session. So a drop is recorded when the
-- player marks it, against the last match of the entry they left.
CREATE TABLE IF NOT EXISTS league_marks (
    match_id      TEXT PRIMARY KEY,
    marked_at     REAL NOT NULL
);

-- League lines from MTGO's client log, kept as written.
--
-- Nothing reads these to decide anything yet. They exist so that the
-- first league join or drop made with the app running shows what MTGO
-- actually logs for it, instead of a guess.
CREATE TABLE IF NOT EXISTS league_messages (
    key           TEXT PRIMARY KEY,   -- file, offset and text, hashed
    at            REAL NOT NULL,      -- when it happened, dated on capture
    kind          TEXT NOT NULL,      -- send / handler / panel
    detail        TEXT,
    captured_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_league_messages_at ON league_messages(at);

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

    # ---- registered decks (captured from MTGO's text log) ---------------

    def upsert_registered_deck(
        self,
        game_id: str,
        username: str,
        signature: str,
        cards: list,
        match_uuid: str | None = None,
        is_league: bool | None = None,
        event_kind: str | None = None,
    ) -> None:
        """Record which list was registered for one game.

        Idempotent by game id. Fields that arrive later — the match uuid
        and event kind often appear in a different part of the log than
        the decklist — are filled in without clobbering what is already
        stored, so a second pass can only add information.
        """
        import json as _json
        import time as _time

        self.conn.execute(
            "INSERT INTO registered_decks"
            " (game_id, match_uuid, username, signature, cards_json,"
            "  is_league, event_kind, captured_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(game_id) DO UPDATE SET"
            "   match_uuid = COALESCE(excluded.match_uuid, registered_decks.match_uuid),"
            "   is_league  = COALESCE(excluded.is_league,  registered_decks.is_league),"
            "   event_kind = COALESCE(excluded.event_kind, registered_decks.event_kind)",
            (
                str(game_id),
                match_uuid,
                username,
                signature,
                _json.dumps(cards),
                None if is_league is None else int(is_league),
                event_kind,
                _time.time(),
            ),
        )
        self.conn.commit()

    def registered_decks(self) -> list[dict]:
        """Every captured registration, newest first."""
        import json as _json

        rows = self.conn.execute(
            "SELECT game_id, match_uuid, username, signature, cards_json,"
            "       is_league, event_kind, captured_at"
            "  FROM registered_decks ORDER BY captured_at DESC"
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "game_id": r[0],
                "match_uuid": r[1],
                "username": r[2],
                "signature": r[3],
                "cards": _json.loads(r[4]),
                "is_league": None if r[5] is None else bool(r[5]),
                "event_kind": r[6],
                "captured_at": r[7],
            })
        return out

    def registered_by_match(self) -> dict:
        """match uuid -> registration, for matches we have one for."""
        return {
            r["match_uuid"]: r
            for r in self.registered_decks()
            if r["match_uuid"]
        }

    # ---- deck versions -------------------------------------------------

    def record_deck_version(
        self,
        *,
        signature: str,
        deck_id: str | None,
        name: str,
        format: str,
        cards: list,
        modified_at: float | None = None,
        source: str = "file",
        seen_at: float | None = None,
    ) -> bool:
        """Remember one version of a deck. Returns True if it was new.

        Idempotent on the signature: re-reading an unchanged deck only
        moves ``last_seen``. The stored cards are never rewritten — the
        whole point is that a version, once captured, is immutable even
        after MTGO overwrites the file it came from.

        A version first recovered from a match can later be upgraded to
        ``source='file'`` if the same list turns up on disk, and can
        gain a ``deck_id`` and a real name that way.
        """
        import json as _json

        now = seen_at if seen_at is not None else time.time()
        cur = self.conn.execute(
            "SELECT deck_id, source FROM deck_versions WHERE signature = ?",
            (signature,),
        ).fetchone()

        if cur is None:
            self.conn.execute(
                "INSERT INTO deck_versions (signature, deck_id, name, format,"
                " cards_json, first_seen, last_seen, modified_at, source)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (signature, deck_id, name, format, _json.dumps(cards),
                 now, now, modified_at, source),
            )
            self.conn.commit()
            return True

        # Seen before. Refresh last_seen, and let a file reading fill in
        # what a recovered version could not know.
        if source == "file" and cur[1] != "file":
            self.conn.execute(
                "UPDATE deck_versions SET last_seen = ?, deck_id = ?,"
                " name = ?, format = ?, modified_at = ?, source = 'file'"
                " WHERE signature = ?",
                (now, deck_id, name, format, modified_at, signature),
            )
        else:
            self.conn.execute(
                "UPDATE deck_versions SET last_seen = ? WHERE signature = ?",
                (now, signature),
            )
        self.conn.commit()
        return False

    def deck_versions(self, deck_id: str | None = None) -> list[dict]:
        """Stored deck versions, newest first."""
        import json as _json

        sql = ("SELECT signature, deck_id, name, format, cards_json,"
               " first_seen, last_seen, modified_at, source FROM deck_versions")
        args: tuple = ()
        if deck_id is not None:
            sql += " WHERE deck_id = ?"
            args = (deck_id,)
        sql += " ORDER BY COALESCE(modified_at, first_seen) DESC"

        out = []
        for r in self.conn.execute(sql, args).fetchall():
            out.append({
                "signature": r[0],
                "deck_id": r[1],
                "name": r[2],
                "format": r[3],
                "cards": _json.loads(r[4]),
                "first_seen": r[5],
                "last_seen": r[6],
                "modified_at": r[7],
                "source": r[8],
            })
        return out

    def attach_deck_version(self, signature: str, deck_id: str, name: str) -> None:
        """Give a recovered version the deck it belongs to."""
        self.conn.execute(
            "UPDATE deck_versions SET deck_id = ?, name = ? WHERE signature = ?",
            (deck_id, name, signature),
        )
        self.conn.commit()

    def forget_deck_versions(self, signatures) -> int:
        """Delete stored versions by signature. Returns how many went.

        Only for lists that were saved and never played — see
        ``deck_history.forget_unplayed``, which decides what qualifies.
        """
        sigs = [s for s in signatures if s]
        removed = 0
        for i in range(0, len(sigs), 500):
            chunk = sigs[i:i + 500]
            cur = self.conn.execute(
                "DELETE FROM deck_versions WHERE signature IN (%s)"
                % ",".join("?" * len(chunk)),
                chunk,
            )
            removed += cur.rowcount
        if sigs:
            self.conn.commit()
        return removed

    # ---- league entries ------------------------------------------------

    def league_marks(self) -> set[str]:
        """Matches the player marked as the last of a league entry."""
        return {r[0] for r in self.conn.execute("SELECT match_id FROM league_marks")}

    def set_league_mark(self, match_id: str, ended: bool) -> None:
        """Mark, or unmark, a match as the one an entry ended on."""
        if ended:
            self.conn.execute(
                "INSERT OR REPLACE INTO league_marks (match_id, marked_at)"
                " VALUES (?, ?)",
                (match_id, time.time()),
            )
        else:
            self.conn.execute(
                "DELETE FROM league_marks WHERE match_id = ?", (match_id,)
            )
        self.conn.commit()

    def record_league_messages(self, rows) -> int:
        """Keep league lines from the client log. Returns how many were new.

        ``rows`` is ``(key, at, kind, detail)``. The log is re-read whole
        whenever it grows, so the same lines arrive again and again; the
        key makes that free.
        """
        now = time.time()
        added = 0
        for key, at, kind, detail in rows:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO league_messages"
                " (key, at, kind, detail, captured_at) VALUES (?, ?, ?, ?, ?)",
                (key, at, kind, detail, now),
            )
            added += cur.rowcount
        self.conn.commit()
        return added

    def fingerprint(self) -> tuple:
        """A value that changes whenever the stored data does.

        The API derives several expensive tables from this store and
        holds them for the life of the process. That was fine when the
        store only changed between runs, but the log watcher now writes
        while the app is open — a deck registered mid-session must show
        up without a restart. Callers cache against this instead of
        caching forever.

        Counts plus the newest timestamps are enough: rows are only ever
        inserted or updated in place, never deleted, so either the count
        moves or a timestamp does. Both queries hit indexed columns and
        cost microseconds, which is what makes it safe to call on every
        request.
        """
        m = self.conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(parsed_at), 0) FROM matches"
        ).fetchone()
        r = self.conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(captured_at), 0)"
            "  FROM registered_decks"
        ).fetchone()
        return (m[0], m[1], r[0], r[1])


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
