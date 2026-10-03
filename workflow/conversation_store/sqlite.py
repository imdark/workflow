"""SQLite conversation store with FTS5 full-text search.

Default backend: a single file at ~/.wf/conversations.db, no server, and
`wf mem search` comes essentially free. Falls back to a LIKE scan when the
Python build lacks FTS5.
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from workflow.conversation_store.base import (
    ConversationStore,
    SessionMeta,
    Summary,
    Turn,
)

DEFAULT_PATH = Path.home() / ".wf" / "conversations.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    agent       TEXT NOT NULL DEFAULT 'unknown',
    protocol    TEXT NOT NULL DEFAULT 'anthropic',
    model       TEXT NOT NULL DEFAULT '',
    task_key    TEXT,
    project     TEXT,
    repo        TEXT,
    started_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_task ON sessions(task_key, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    session_id   TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    ts           REAL NOT NULL,
    tool_name    TEXT,
    tool_call_id TEXT,
    fingerprint  TEXT NOT NULL,
    raw          TEXT,
    PRIMARY KEY (session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);

CREATE TABLE IF NOT EXISTS summaries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT NOT NULL,
    session_id TEXT,
    task_key   TEXT,
    content    TEXT NOT NULL,
    model      TEXT NOT NULL DEFAULT '',
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_session ON summaries(session_id, level);
CREATE INDEX IF NOT EXISTS idx_summaries_task ON summaries(task_key, level, ts DESC);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    session_id UNINDEXED,
    seq UNINDEXED,
    tokenize = 'porter unicode61'
);
"""


class SqliteStore(ConversationStore):
    name = "sqlite"

    def __init__(self, path=None):
        self.path = Path(path).expanduser() if path else DEFAULT_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the proxy writes from the event loop while
        # the ladder worker summarizes on a background thread.
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._fts = self._try_enable_fts()
        self._db.commit()

    def _try_enable_fts(self) -> bool:
        try:
            self._db.executescript(FTS_SCHEMA)
            return True
        except sqlite3.OperationalError:
            # FTS5 not compiled in; search degrades to LIKE.
            return False

    # ── sessions ──────────────────────────────────────────────────────────

    def ensure_session(self, meta: SessionMeta) -> None:
        now = time.time()
        self._db.execute(
            """
            INSERT INTO sessions (session_id, agent, protocol, model, task_key,
                                  project, repo, started_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                updated_at = excluded.updated_at,
                -- A session's model can change mid-conversation (a /model
                -- switch); keep the most recent non-empty value.
                model = CASE WHEN excluded.model != '' THEN excluded.model ELSE sessions.model END,
                task_key = COALESCE(excluded.task_key, sessions.task_key)
            """,
            (
                meta.session_id, meta.agent, meta.protocol, meta.model,
                meta.task_key, meta.project, meta.repo, meta.started_at, now,
            ),
        )
        self._db.commit()

    def list_sessions(self, task_key: Optional[str] = None, limit: int = 50) -> list:
        sql = "SELECT * FROM sessions"
        args = []
        if task_key:
            sql += " WHERE task_key = ?"
            args.append(task_key)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        return [_row_to_meta(r) for r in self._db.execute(sql, args)]

    # ── messages ──────────────────────────────────────────────────────────

    def fingerprints(self, session_id: str) -> list:
        rows = self._db.execute(
            "SELECT fingerprint FROM messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        )
        return [r["fingerprint"] for r in rows]

    def append_turns(self, session_id: str, turns: list) -> int:
        if not turns:
            return 0
        row = self._db.execute(
            "SELECT COALESCE(MAX(seq), -1) AS m FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        seq = row["m"] + 1
        written = 0
        for turn in turns:
            self._db.execute(
                """INSERT OR IGNORE INTO messages
                   (session_id, seq, role, content, ts, tool_name, tool_call_id,
                    fingerprint, raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id, seq, turn.role, turn.content, turn.ts,
                    turn.tool_name, turn.tool_call_id, turn.fingerprint(),
                    json.dumps(turn.raw) if turn.raw else None,
                ),
            )
            if self._fts:
                self._db.execute(
                    "INSERT INTO messages_fts (content, session_id, seq) VALUES (?, ?, ?)",
                    (turn.content, session_id, seq),
                )
            seq += 1
            written += 1
        self._db.commit()
        return written

    def get_turns(self, session_id: str, limit: Optional[int] = None, offset: int = 0) -> list:
        sql = "SELECT * FROM messages WHERE session_id = ? ORDER BY seq"
        args = [session_id]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args += [limit, offset]
        return [_row_to_turn(r) for r in self._db.execute(sql, args)]

    def search(self, query: str, limit: int = 20, task_key: Optional[str] = None) -> list:
        if self._fts:
            sql = """
                SELECT m.* FROM messages_fts f
                JOIN messages m ON m.session_id = f.session_id AND m.seq = f.seq
                JOIN sessions s ON s.session_id = m.session_id
                WHERE messages_fts MATCH ?
            """
            args = [query]
            if task_key:
                sql += " AND s.task_key = ?"
                args.append(task_key)
            sql += " ORDER BY rank LIMIT ?"
            args.append(limit)
            try:
                return [_row_to_turn(r) for r in self._db.execute(sql, args)]
            except sqlite3.OperationalError:
                # Malformed FTS query (bare punctuation, unbalanced quotes) --
                # fall through to LIKE rather than surfacing a syntax error.
                pass
        sql = """
            SELECT m.* FROM messages m
            JOIN sessions s ON s.session_id = m.session_id
            WHERE m.content LIKE ?
        """
        args = [f"%{query}%"]
        if task_key:
            sql += " AND s.task_key = ?"
            args.append(task_key)
        sql += " ORDER BY m.ts DESC LIMIT ?"
        args.append(limit)
        return [_row_to_turn(r) for r in self._db.execute(sql, args)]

    # ── summaries ─────────────────────────────────────────────────────────

    def save_summary(self, summary: Summary) -> None:
        self._db.execute(
            """INSERT INTO summaries (level, session_id, task_key, content, model, ts)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (summary.level, summary.session_id, summary.task_key,
             summary.content, summary.model, summary.ts),
        )
        self._db.commit()

    def get_summaries(self, session_id=None, task_key=None, level=None) -> list:
        sql = "SELECT * FROM summaries WHERE 1=1"
        args = []
        for column, value in (("session_id", session_id), ("task_key", task_key), ("level", level)):
            if value:
                sql += f" AND {column} = ?"
                args.append(value)
        sql += " ORDER BY ts DESC"
        return [
            Summary(level=r["level"], content=r["content"], session_id=r["session_id"],
                    task_key=r["task_key"], model=r["model"], ts=r["ts"])
            for r in self._db.execute(sql, args)
        ]

    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.ProgrammingError:
            pass


def _row_to_meta(r) -> SessionMeta:
    return SessionMeta(
        session_id=r["session_id"], agent=r["agent"], protocol=r["protocol"],
        model=r["model"], task_key=r["task_key"], project=r["project"],
        repo=r["repo"], started_at=r["started_at"], updated_at=r["updated_at"],
    )


def _row_to_turn(r) -> Turn:
    return Turn(
        session_id=r["session_id"], seq=r["seq"], role=r["role"],
        content=r["content"], ts=r["ts"], tool_name=r["tool_name"],
        tool_call_id=r["tool_call_id"],
        raw=json.loads(r["raw"]) if r["raw"] else None,
    )
