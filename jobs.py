"""
Persistent store for async jobs posted via the HTTP API.
Each job tracks content sent to Telegram, optional buttons, how to handle
the response, and the outcome once the user interacts.
"""

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "jobs.db"


class JobStore:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = str(db_path)
        self._init_db()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id           TEXT PRIMARY KEY,
                    content      TEXT NOT NULL,
                    buttons      TEXT,
                    on_response  TEXT,
                    tg_msg_id    INTEGER,
                    status       TEXT DEFAULT 'pending',
                    action       TEXT,
                    created_at   TEXT,
                    responded_at TEXT,
                    topic_id     INTEGER,
                    metadata     TEXT
                )
            """)
            try:
                conn.execute("ALTER TABLE jobs ADD COLUMN topic_id INTEGER")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE jobs ADD COLUMN metadata TEXT")
            except Exception:
                pass

    def create(
        self,
        content: str,
        buttons: list | None,
        on_response: dict | None,
        topic_id: int | None = None,
        metadata: dict | None = None,
    ) -> str:
        job_id = uuid.uuid4().hex[:8]
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    content,
                    json.dumps(buttons) if buttons else None,
                    json.dumps(on_response) if on_response else None,
                    None,
                    "pending",
                    None,
                    datetime.utcnow().isoformat(),
                    None,
                    topic_id,
                    json.dumps(metadata) if metadata else None,
                ),
            )
        return job_id

    def set_tg_msg_id(self, job_id: str, tg_msg_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET tg_msg_id=? WHERE id=?", (tg_msg_id, job_id)
            )

    def get(self, job_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        if not row:
            return None
        keys = [
            "id", "content", "buttons", "on_response", "tg_msg_id",
            "status", "action", "created_at", "responded_at", "topic_id", "metadata",
        ]
        d = dict(zip(keys, row))
        d["buttons"] = json.loads(d["buttons"]) if d["buttons"] else None
        d["on_response"] = json.loads(d["on_response"]) if d["on_response"] else None
        d["metadata"] = json.loads(d["metadata"]) if d["metadata"] else None
        return d

    def update_metadata(self, job_id: str, metadata: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET metadata=? WHERE id=?",
                (json.dumps(metadata), job_id),
            )

    def respond(self, job_id: str, action: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET status='responded', action=?, responded_at=? WHERE id=?",
                (action, datetime.utcnow().isoformat(), job_id),
            )
