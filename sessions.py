"""
Session persistence for Telegram conversations.
Maps topic_id → Claude session UUID (resumable via `claude -p --resume <uuid>`)
so Claude maintains context across messages. Sessions expire after 24h of
inactivity, or once cumulative spend crosses SESSION_COST_CAP_USD.

cost_usd/turns are populated from each `claude -p` result's total_cost_usd /
num_turns, accumulated across the session's turns.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "jobs.db"
SESSION_TTL_HOURS = 24
SESSION_COST_CAP_USD = 2.0  # auto-clear session above this spend


class SessionStore:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = str(db_path)
        self._init_db()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS telegram_sessions (
                    topic_id    INTEGER PRIMARY KEY,
                    project     TEXT NOT NULL,
                    session_id  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
                    turns       INTEGER NOT NULL DEFAULT 0,
                    cost_usd    REAL NOT NULL DEFAULT 0.0
                )
            """)
            # Add columns to existing databases that predate cost/turn tracking
            for col, typedef in [("turns", "INTEGER NOT NULL DEFAULT 0"),
                                  ("cost_usd", "REAL NOT NULL DEFAULT 0.0")]:
                try:
                    conn.execute(f"ALTER TABLE telegram_sessions ADD COLUMN {col} {typedef}")
                except sqlite3.OperationalError:
                    pass  # column already exists

    def get_active(self, topic_id: int) -> str | None:
        """Return session_id if within TTL and below cost cap, else None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT session_id, updated_at, cost_usd FROM telegram_sessions WHERE topic_id=?",
                (topic_id,),
            ).fetchone()
        if not row:
            return None
        session_id, updated_at, cost_usd = row
        try:
            age = datetime.utcnow() - datetime.fromisoformat(updated_at)
            if age >= timedelta(hours=SESSION_TTL_HOURS):
                return None
        except ValueError:
            return None
        if cost_usd >= SESSION_COST_CAP_USD:
            return None
        return session_id

    def upsert(self, topic_id: int, project: str, session_id: str,
               turns: int = 0, cost_usd: float = 0.0) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO telegram_sessions (topic_id, project, session_id, updated_at, turns, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(topic_id) DO UPDATE SET
                    project    = excluded.project,
                    session_id = excluded.session_id,
                    updated_at = excluded.updated_at,
                    turns      = telegram_sessions.turns + excluded.turns,
                    cost_usd   = telegram_sessions.cost_usd + excluded.cost_usd
                """,
                (topic_id, project, session_id, datetime.utcnow().isoformat(), turns, cost_usd),
            )

    def get_stats(self, topic_id: int) -> dict | None:
        """Return {session_id, turns, cost_usd} for a topic, or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT session_id, turns, cost_usd FROM telegram_sessions WHERE topic_id=?",
                (topic_id,),
            ).fetchone()
        if not row:
            return None
        return {"session_id": row[0], "turns": row[1], "cost_usd": row[2]}

    def delete(self, topic_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM telegram_sessions WHERE topic_id=?", (topic_id,))

    def pop_if_stale(self, topic_id: int) -> str | None:
        """
        If the row exists but is past TTL or cost cap, return its session_id
        and delete the row. Otherwise return None.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT session_id, updated_at, cost_usd FROM telegram_sessions WHERE topic_id=?",
                (topic_id,),
            ).fetchone()
            if not row:
                return None
            session_id, updated_at, cost_usd = row
            stale = False
            try:
                if datetime.utcnow() - datetime.fromisoformat(updated_at) >= timedelta(hours=SESSION_TTL_HOURS):
                    stale = True
            except ValueError:
                stale = True
            if cost_usd >= SESSION_COST_CAP_USD:
                stale = True
            if not stale:
                return None
            conn.execute("DELETE FROM telegram_sessions WHERE topic_id=?", (topic_id,))
            return session_id
