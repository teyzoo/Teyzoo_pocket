import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional


class Database:
    def __init__(self, path: str = "signal_bot.db"):
        self.path = path
        self._lock = threading.RLock()
        self._init_db()

    # ============================================================
    # CONNECTION
    # ============================================================

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=30,
            check_same_thread=False,
        )

        conn.row_factory = sqlite3.Row

        # Повышаем надёжность SQLite
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")

        return conn

    # ============================================================
    # HELPERS
    # ============================================================

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
        if row is None:
            return None
        return dict(row)

    # ============================================================
    # DATABASE INIT
    # ============================================================

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()

            try:
                cursor = conn.cursor()

                # ------------------------------------------------
                # USERS
                # ------------------------------------------------

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        username TEXT,
                        first_name TEXT,
                        status TEXT NOT NULL DEFAULT 'PENDING',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )

                # ------------------------------------------------
                # SIGNAL REQUESTS
                # ------------------------------------------------

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS signal_requests (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(user_id) REFERENCES users(user_id)
                    )
                    """
                )

                # ------------------------------------------------
                # SIGNALS
                # ------------------------------------------------

                cursor.execute(
                    """
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
                    )
                    """
                )

                # ------------------------------------------------
                # INDEXES
                # ------------------------------------------------

                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_users_status
                    ON users(status)
                    """
                )

                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_signal_requests_user
                    ON signal_requests(user_id)
                    """
                )

                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_signals_entry
                    ON signals(entry_time)
                    """
                )

                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_signals_pair
                    ON signals(pair)
                    """
                )

                conn.commit()

            finally:
                conn.close()

    # ============================================================
    # USERS
    # ============================================================

    def get_user(self, user_id: int) -> Optional[dict]:
        with self._lock:
            conn = self._connect()

            try:
                cursor = conn.execute(
                    """
                    SELECT
                        user_id,
                        username,
                        first_name,
                        status,
                        created_at,
                        updated_at
                    FROM users
                    WHERE user_id = ?
                    """,
                    (user_id,),
                )

                row = cursor.fetchone()
                return self._row_to_dict(row)

            finally:
                conn.close()

    def create_or_update_user(
        self,
        user_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> dict:
        """
        Создаёт пользователя или обновляет его данные.

        ВАЖНО:
        status специально является необязательным параметром.
        Это исправляет ошибку:

        TypeError:
        Database.create_or_update_user()
        got an unexpected keyword argument 'status'
        """

        now = self._now()

        with self._lock:
            conn = self._connect()

            try:
                cursor = conn.execute(
                    """
                    SELECT
                        user_id,
                        username,
                        first_name,
                        status,
                        created_at,
                        updated_at
                    FROM users
                    WHERE user_id = ?
                    """,
                    (user_id,),
                )

                existing = cursor.fetchone()

                if existing is None:
                    # Для нового пользователя:
                    # если статус не передан — PENDING.
                    new_status = status or "PENDING"

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
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            user_id,
                            username,
                            first_name,
                            new_status,
                            now,
                            now,
                        ),
                    )

                else:
                    # Если статус не передан — сохраняем существующий.
                    current_status = existing["status"]
                    new_status = (
                        status
                        if status is not None
                        else current_status
                    )

                    conn.execute(
                        """
                        UPDATE users
                        SET
                            username = ?,
                            first_name = ?,
                            status = ?,
                            updated_at = ?
                        WHERE user_id = ?
                        """,
                        (
                            username,
                            first_name,
                            new_status,
                            now,
                            user_id,
                        ),
                    )

                conn.commit()

                cursor = conn.execute(
                    """
                    SELECT
                        user_id,
                        username,
                        first_name,
                        status,
                        created_at,
                        updated_at
                    FROM users
                    WHERE user_id = ?
                    """,
                    (user_id,),
                )

                row = cursor.fetchone()

                if row is None:
                    raise RuntimeError(
                        f"Не удалось получить пользователя {user_id} "
                        "после сохранения."
                    )

                return dict(row)

            finally:
                conn.close()

    def set_status(
        self,
        user_id: int,
        status: str,
    ) -> bool:
        """
        Изменяет статус пользователя.

        Поддерживаемые статусы:
        PENDING
        APPROVED
        REJECTED
        BLOCKED
        """

        allowed_statuses = {
            "PENDING",
            "APPROVED",
            "REJECTED",
            "BLOCKED",
        }

        status = status.upper().strip()

        if status not in allowed_statuses:
            raise ValueError(
                f"Недопустимый статус пользователя: {status}"
            )

        with self._lock:
            conn = self._connect()

            try:
                cursor = conn.execute(
                    """
                    UPDATE users
                    SET
                        status = ?,
                        updated_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        status,
                        self._now(),
                        user_id,
                    ),
                )

                conn.commit()

                return cursor.rowcount > 0

            finally:
                conn.close()

    def get_pending_users(self) -> list[dict]:
        with self._lock:
            conn = self._connect()

            try:
                cursor = conn.execute(
                    """
                    SELECT
                        user_id,
                        username,
                        first_name,
                        status,
                        created_at,
                        updated_at
                    FROM users
                    WHERE status = 'PENDING'
                    ORDER BY created_at ASC
                    """
                )

                return [dict(row) for row in cursor.fetchall()]

            finally:
                conn.close()

    def get_approved_users(self) -> list[dict]:
        with self._lock:
            conn = self._connect()

            try:
                cursor = conn.execute(
                    """
                    SELECT
                        user_id,
                        username,
                        first_name,
                        status,
                        created_at,
                        updated_at
                    FROM users
                    WHERE status = 'APPROVED'
                    ORDER BY created_at ASC
                    """
                )

                return [dict(row) for row in cursor.fetchall()]

            finally:
                conn.close()

    # ============================================================
    # SIGNAL ACCESS REQUESTS
    # ============================================================

    def add_signal_request(
        self,
        user_id: int,
    ) -> int:
        """
        Создаёт заявку пользователя на доступ к сигналам.
        """

        # На всякий случай убеждаемся, что пользователь существует.
        user = self.get_user(user_id)

        if user is None:
            self.create_or_update_user(
                user_id=user_id,
                status="PENDING",
            )

        with self._lock:
            conn = self._connect()

            try:
                cursor = conn.execute(
                    """
                    INSERT INTO signal_requests (
                        user_id,
                        created_at
                    )
                    VALUES (?, ?)
                    """,
                    (
                        user_id,
                        self._now(),
                    ),
                )

                conn.commit()

                return int(cursor.lastrowid)

            finally:
                conn.close()

    # ============================================================
    # SIGNALS
    # ============================================================

    def signal_exists(
        self,
        pair: str,
        direction: str,
        entry_time: str,
    ) -> bool:
        with self._lock:
            conn = self._connect()

            try:
                cursor = conn.execute(
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
                )

                return cursor.fetchone() is not None

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
        confirmations: Optional[str] = None,
        reasons: Optional[str] = None,
        result: Optional[str] = None,
    ) -> int:
        """
        Сохраняет сигнал.

        Если такой сигнал уже существует, новый дубликат
        не создаётся — возвращается ID существующего сигнала.
        """

        with self._lock:
            conn = self._connect()

            try:
                cursor = conn.execute(
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
                )

                existing = cursor.fetchone()

                if existing is not None:
                    return int(existing["id"])

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
                        reasons,
                        result
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pair,
                        direction,
                        float(quality),
                        entry_time,
                        expiry_time,
                        analysis_time,
                        confirmations,
                        reasons,
                        result,
                    ),
                )

                conn.commit()

                return int(cursor.lastrowid)

            finally:
                conn.close()

    def get_recent_signals(
        self,
        limit: int = 20,
    ) -> list[dict]:
        """
        Возвращает последние сигналы.
        """

        limit = max(1, min(int(limit), 500))

        with self._lock:
            conn = self._connect()

            try:
                cursor = conn.execute(
                    """
                    SELECT
                        id,
                        pair,
                        direction,
                        quality,
                        entry_time,
                        expiry_time,
                        analysis_time,
                        confirmations,
                        reasons,
                        result
                    FROM signals
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )

                return [dict(row) for row in cursor.fetchall()]

            finally:
                conn.close()

    def set_signal_result(
        self,
        signal_id: int,
        result: str,
    ) -> bool:
        """
        Устанавливает результат сигнала.

        Например:
        WIN
        LOSS
        DRAW
        EXPIRED
        """

        result = result.upper().strip()

        with self._lock:
            conn = self._connect()

            try:
                cursor = conn.execute(
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

                return cursor.rowcount > 0

            finally:
                conn.close()


# ================================================================
# GLOBAL DATABASE INSTANCE
# ================================================================

db = Database()
