import sqlite3
import threading
from datetime import datetime
from typing import Optional

from config import DATABASE_PATH


class Database:
    def __init__(self, path: str = DATABASE_PATH):
        self.path = path
        self.lock = threading.Lock()

        self.connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
        )

        self.connection.row_factory = sqlite3.Row

        self._create_tables()


    # ========================================================
    # TABLES
    # ========================================================

    def _create_tables(self) -> None:
        with self.lock:
            cursor = self.connection.cursor()

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_time TEXT NOT NULL,
                    expiry_time TEXT NOT NULL,
                    quality INTEGER NOT NULL,
                    score_details TEXT,
                    created_at TEXT NOT NULL,
                    result TEXT DEFAULT NULL
                )
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS signal_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

            self.connection.commit()


    # ========================================================
    # USERS
    # ========================================================

    def get_user(
        self,
        user_id: int,
    ) -> Optional[sqlite3.Row]:

        with self.lock:
            cursor = self.connection.cursor()

            cursor.execute(
                """
                SELECT *
                FROM users
                WHERE user_id = ?
                """,
                (user_id,),
            )

            return cursor.fetchone()


    def create_user(
        self,
        user_id: int,
        username: str,
        full_name: str,
    ) -> None:

        now = datetime.utcnow().isoformat()

        with self.lock:
            cursor = self.connection.cursor()

            cursor.execute(
                """
                INSERT INTO users (
                    user_id,
                    username,
                    full_name,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (
                    user_id,
                    username,
                    full_name,
                    now,
                    now,
                ),
            )

            self.connection.commit()


    def update_user_status(
        self,
        user_id: int,
        status: str,
    ) -> None:

        now = datetime.utcnow().isoformat()

        with self.lock:
            cursor = self.connection.cursor()

            cursor.execute(
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

            self.connection.commit()


    def get_status(
        self,
        user_id: int,
    ) -> Optional[str]:

        user = self.get_user(user_id)

        if user is None:
            return None

        return user["status"]


    # ========================================================
    # SIGNALS
    # ========================================================

    def save_signal(
        self,
        pair: str,
        direction: str,
        entry_time: str,
        expiry_time: str,
        quality: int,
        score_details: str,
    ) -> int:

        now = datetime.utcnow().isoformat()

        with self.lock:
            cursor = self.connection.cursor()

            cursor.execute(
                """
                INSERT INTO signals (
                    pair,
                    direction,
                    entry_time,
                    expiry_time,
                    quality,
                    score_details,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pair,
                    direction,
                    entry_time,
                    expiry_time,
                    quality,
                    score_details,
                    now,
                ),
            )

            self.connection.commit()

            return int(cursor.lastrowid)


    def get_signal_stats(self) -> dict:

        with self.lock:
            cursor = self.connection.cursor()

            cursor.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(
                        CASE
                            WHEN result = 'WIN'
                            THEN 1
                            ELSE 0
                        END
                    ) AS wins,
                    SUM(
                        CASE
                            WHEN result = 'LOSS'
                            THEN 1
                            ELSE 0
                        END
                    ) AS losses
                FROM signals
                """
            )

            row = cursor.fetchone()

        total = int(row["total"] or 0)
        wins = int(row["wins"] or 0)
        losses = int(row["losses"] or 0)

        finished = wins + losses

        if finished:
            winrate = wins / finished * 100
        else:
            winrate = 0.0

        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "finished": finished,
            "winrate": winrate,
        }


    # ========================================================
    # REQUESTS
    # ========================================================

    def save_signal_request(
        self,
        user_id: int,
    ) -> None:

        now = datetime.utcnow().isoformat()

        with self.lock:
            cursor = self.connection.cursor()

            cursor.execute(
                """
                INSERT INTO signal_requests (
                    user_id,
                    created_at
                )
                VALUES (?, ?)
                """,
                (
                    user_id,
                    now,
                ),
            )

            self.connection.commit()


    # ========================================================
    # CLOSE
    # ========================================================

    def close(self) -> None:
        with self.lock:
            self.connection.close()
