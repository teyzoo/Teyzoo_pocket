import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from config import DATABASE_PATH


class Database:
    def __init__(self, path: str = DATABASE_PATH):
        self.path = path
        self.lock = threading.Lock()
        self._init_db()

    def _connect(self):
        connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self):
        with self.lock:
            conn = self._connect()

            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        username TEXT,
                        first_name TEXT,
                        status TEXT NOT NULL DEFAULT 'PENDING',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS signal_requests (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS signals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        pair TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        quality REAL NOT NULL,
                        entry_time TEXT NOT NULL,
                        expiry_time TEXT NOT NULL,
                        analysis_time TEXT NOT NULL,
                        confirmations TEXT,
                        reasons TEXT,
                        result TEXT
                    );

                    CREATE INDEX IF NOT EXISTS idx_signals_entry
                    ON signals(entry_time);

                    CREATE INDEX IF NOT EXISTS idx_users_status
                    ON users(status);
                    """
                )

                conn.commit()

            finally:
                conn.close()

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def get_user(self, user_id: int) -> Optional[dict]:
        with self.lock:
            conn = self._connect()

            try:
                row = conn.execute(
                    """
                    SELECT *
                    FROM users
                    WHERE user_id = ?
                    """,
                    (user_id,),
                ).fetchone()

                return dict(row) if row else None

            finally:
                conn.close()

    def create_or_update_user(
        self,
        user_id: int,
        username: Optional[str],
        first_name: Optional[str],
    ) -> dict:

        now = self.now_iso()

        with self.lock:
            conn = self._connect()

            try:
                existing = conn.execute(
                    """
                    SELECT *
                    FROM users
                    WHERE user_id = ?
                    """,
                    (user_id,),
                ).fetchone()

                if existing:
                    conn.execute(
                        """
                        UPDATE users
                        SET username = ?,
                            first_name = ?,
                            updated_at = ?
                        WHERE user_id = ?
                        """,
                        (
                            username,
                            first_name,
                            now,
                            user_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO users (
                            user_id,
                            username,
                            first_name,
                            status,
                            created_at,
                            updated_at
                        )
                        VALUES (?, ?, ?, 'PENDING', ?, ?)
                        """,
                        (
                            user_id,
                            username,
                            first_name,
                            now,
                            now,
                        ),
                    )

                conn.commit()

                row = conn.execute(
                    """
                    SELECT *
                    FROM users
                    WHERE user_id = ?
                    """,
                    (user_id,),
                ).fetchone()

                return dict(row)

            finally:
                conn.close()

    def set_status(self, user_id: int, status: str):
        now = self.now_iso()

        with self.lock:
            conn = self._connect()

            try:
                conn.execute(
                    """
                    UPDATE users
                    SET status = ?,
                        updated_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        status,
                        now,
                        user_id,
                    ),
                )

                conn.commit()

            finally:
                conn.close()

    def get_pending_users(self) -> list[dict]:
        with self.lock:
            conn = self._connect()

            try:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM users
                    WHERE status = 'PENDING'
                    ORDER BY created_at ASC
                    """
                ).fetchall()

                return [dict(row) for row in rows]

            finally:
                conn.close()

    def get_approved_users(self) -> list[int]:
        with self.lock:
            conn = self._connect()

            try:
                rows = conn.execute(
                    """
                    SELECT user_id
                    FROM users
                    WHERE status = 'APPROVED'
                    """
                ).fetchall()

                return [int(row["user_id"]) for row in rows]

            finally:
                conn.close()

    def add_signal_request(self, user_id: int):
        with self.lock:
            conn = self._connect()

            try:
                conn.execute(
                    """
                    INSERT INTO signal_requests (
                        user_id,
                        created_at
                    )
                    VALUES (?, ?)
                    """,
                    (
                        user_id,
                        self.now_iso(),
                    ),
                )

                conn.commit()

            finally:
                conn.close()

    def signal_exists(
        self,
        pair: str,
        direction: str,
        entry_time: str,
    ) -> bool:

        with self.lock:
            conn = self._connect()

            try:
                row = conn.execute(
                    """
                    SELECT id
                    FROM signals
                    WHERE pair = ?
                      AND direction = ?
                      AND entry_time = ?
                    LIMIT 1
                    """,
                    (
                        pair,
                        direction,
                        entry_time,
                    ),
                ).fetchone()

                return row is not None

            finally:
                conn.close()

    def save_signal(
        self,
        pair: str,
        direction: str,
        quality: float,
        entry_time: str,
        expiry_time: str,
        analysis_time: str,
        confirmations: list[str],
        reasons: list[str],
    ) -> int:

        with self.lock:
            conn = self._connect()

            try:
                cursor = conn.execute(
                    """
                    INSERT INTO signals (
                        pair,
                        direction,
                        quality,
                        entry_time,
                        expiry_time,
                        analysis_time,
                        confirmations,
                        reasons
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pair,
                        direction,
                        quality,
                        entry_time,
                        expiry_time,
                        analysis_time,
                        json.dumps(
                            confirmations,
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            reasons,
                            ensure_ascii=False,
                        ),
                    ),
                )

                conn.commit()

                return int(cursor.lastrowid)

            finally:
                conn.close()

    def get_recent_signals(self, limit: int = 20) -> list[dict]:
        with self.lock:
            conn = self._connect()

            try:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM signals
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()

                return [dict(row) for row in rows]

            finally:
                conn.close()

    def set_signal_result(
        self,
        signal_id: int,
        result: str,
    ):
        with self.lock:
            conn = self._connect()

            try:
                conn.execute(
                    """
                    UPDATE signals
                    SET result = ?
                    WHERE id = ?
                    """,
                    (
                        result,
                        signal_id,
                    ),
                )

                conn.commit()

            finally:
                conn.close()


db = Database()
