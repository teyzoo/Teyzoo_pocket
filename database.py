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

        # Надёжность SQLite
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
    def _row_to_dict(
        row: Optional[sqlite3.Row],
    ) -> Optional[dict]:
        if row is None:
            return None

        return dict(row)

    @staticmethod
    def _column_exists(
        conn: sqlite3.Connection,
        table: str,
        column: str,
    ) -> bool:
        cursor = conn.execute(
            f"PRAGMA table_info({table})"
        )

        columns = {
            row["name"]
            for row in cursor.fetchall()
        }

        return column in columns

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
                #
                # Старые поля сохранены.
                # Новые поля используются для автоматической
                # проверки результата и статистики.
                # ------------------------------------------------

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS signals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        pair TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        quality REAL NOT NULL,
                        probability REAL,
                        entry_time TEXT NOT NULL,
                        expiry_time TEXT NOT NULL,
                        expiry_minutes INTEGER,
                        analysis_time TEXT NOT NULL,
                        confirmations TEXT,
                        reasons TEXT,
                        entry_price REAL,
                        expiry_price REAL,
                        result TEXT,
                        result_checked_at TEXT
                    )
                    """
                )

                # ------------------------------------------------
                # MIGRATION EXISTING DATABASE
                #
                # Если signal_bot.db уже существует от старой
                # версии — добавляем только отсутствующие поля.
                # Никакие существующие данные не удаляем.
                # ------------------------------------------------

                migrations = [
                    (
                        "probability",
                        "ALTER TABLE signals "
                        "ADD COLUMN probability REAL"
                    ),
                    (
                        "expiry_minutes",
                        "ALTER TABLE signals "
                        "ADD COLUMN expiry_minutes INTEGER"
                    ),
                    (
                        "entry_price",
                        "ALTER TABLE signals "
                        "ADD COLUMN entry_price REAL"
                    ),
                    (
                        "expiry_price",
                        "ALTER TABLE signals "
                        "ADD COLUMN expiry_price REAL"
                    ),
                    (
                        "result_checked_at",
                        "ALTER TABLE signals "
                        "ADD COLUMN result_checked_at TEXT"
                    ),
                ]

                for column, sql in migrations:
                    if not self._column_exists(
                        conn,
                        "signals",
                        column,
                    ):
                        cursor.execute(sql)

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

                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_signals_expiry
                    ON signals(expiry_minutes)
                    """
                )

                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_signals_result
                    ON signals(result)
                    """
                )

                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_signals_pair_expiry
                    ON signals(pair, expiry_minutes)
                    """
                )

                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_signals_pending
                    ON signals(result, expiry_time)
                    """
                )

                conn.commit()

            finally:
                conn.close()

    # ============================================================
    # USERS
    # ============================================================

    def get_user(
        self,
        user_id: int,
    ) -> Optional[dict]:
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

        status необязательный, чтобы старые вызовы
        метода продолжали работать.
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
                        f"Не удалось получить пользователя "
                        f"{user_id} после сохранения."
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

                return [
                    dict(row)
                    for row in cursor.fetchall()
                ]

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

                return [
                    dict(row)
                    for row in cursor.fetchall()
                ]

            finally:
                conn.close()

    def get_active_users(self) -> list[dict]:
        """
        Совместимый метод для автоматической рассылки.

        Активными считаются APPROVED пользователи.
        """

        return self.get_approved_users()

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
        probability: Optional[float] = None,
        expiry_minutes: Optional[int] = None,
        entry_price: Optional[float] = None,
        expiry_price: Optional[float] = None,
    ) -> int:
        """
        Сохраняет сигнал.

        Старые аргументы полностью поддерживаются.

        Новые:
            probability
            expiry_minutes
            entry_price
            expiry_price
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

                # Если duration не передан — пытаемся вычислить
                # его непосредственно из entry/expiry времени.
                if expiry_minutes is None:
                    try:
                        entry_dt = datetime.fromisoformat(
                            entry_time.replace("Z", "+00:00")
                        )

                        expiry_dt = datetime.fromisoformat(
                            expiry_time.replace("Z", "+00:00")
                        )

                        seconds = (
                            expiry_dt - entry_dt
                        ).total_seconds()

                        calculated_minutes = round(
                            seconds / 60
                        )

                        if calculated_minutes > 0:
                            expiry_minutes = calculated_minutes

                    except Exception:
                        expiry_minutes = None

                cursor = conn.execute(
                    """
                    INSERT INTO signals (
                        pair,
                        direction,
                        quality,
                        probability,
                        entry_time,
                        expiry_time,
                        expiry_minutes,
                        analysis_time,
                        confirmations,
                        reasons,
                        entry_price,
                        expiry_price,
                        result,
                        result_checked_at
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        pair,
                        direction,
                        float(quality),
                        (
                            float(probability)
                            if probability is not None
                            else None
                        ),
                        entry_time,
                        expiry_time,
                        (
                            int(expiry_minutes)
                            if expiry_minutes is not None
                            else None
                        ),
                        analysis_time,
                        confirmations,
                        reasons,
                        (
                            float(entry_price)
                            if entry_price is not None
                            else None
                        ),
                        (
                            float(expiry_price)
                            if expiry_price is not None
                            else None
                        ),
                        result,
                        (
                            self._now()
                            if result is not None
                            else None
                        ),
                    ),
                )

                conn.commit()

                return int(cursor.lastrowid)

            finally:
                conn.close()

    # ============================================================
    # SIGNAL READ
    # ============================================================

    def get_signal(
        self,
        signal_id: int,
    ) -> Optional[dict]:
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
                        probability,
                        entry_time,
                        expiry_time,
                        expiry_minutes,
                        analysis_time,
                        confirmations,
                        reasons,
                        entry_price,
                        expiry_price,
                        result,
                        result_checked_at
                    FROM signals
                    WHERE id = ?
                    LIMIT 1
                    """,
                    (signal_id,),
                )

                row = cursor.fetchone()

                return self._row_to_dict(row)

            finally:
                conn.close()

    def get_recent_signals(
        self,
        limit: int = 20,
    ) -> list[dict]:
        """
        Возвращает последние сигналы.
        """

        limit = max(
            1,
            min(int(limit), 500),
        )

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
                        probability,
                        entry_time,
                        expiry_time,
                        expiry_minutes,
                        analysis_time,
                        confirmations,
                        reasons,
                        entry_price,
                        expiry_price,
                        result,
                        result_checked_at
                    FROM signals
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )

                return [
                    dict(row)
                    for row in cursor.fetchall()
                ]

            finally:
                conn.close()

    # ============================================================
    # PENDING SIGNALS
    # ============================================================

    def get_pending_signals(
        self,
        before_time: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict]:
        """
        Возвращает сигналы, результат которых ещё не определён.

        Используется scheduler'ом для автоматической проверки
        WIN/LOSS после наступления expiry_time.
        """

        limit = max(
            1,
            min(int(limit), 1000),
        )

        if before_time is None:
            before_time = self._now()

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
                        probability,
                        entry_time,
                        expiry_time,
                        expiry_minutes,
                        analysis_time,
                        confirmations,
                        reasons,
                        entry_price,
                        expiry_price,
                        result,
                        result_checked_at
                    FROM signals
                    WHERE (
                        result IS NULL
                        OR result = ''
                        OR result = 'PENDING'
                    )
                    AND expiry_time <= ?
                    ORDER BY expiry_time ASC
                    LIMIT ?
                    """,
                    (
                        before_time,
                        limit,
                    ),
                )

                return [
                    dict(row)
                    for row in cursor.fetchall()
                ]

            finally:
                conn.close()

    # ============================================================
    # SIGNAL RESULT
    # ============================================================

    def set_signal_result(
        self,
        signal_id: int,
        result: str,
        expiry_price: Optional[float] = None,
    ) -> bool:
        """
        Устанавливает результат сигнала.

        Поддерживаются:

        WIN
        LOSS
        DRAW
        EXPIRED

        Дополнительно сохраняется цена закрытия.
        """

        result = result.upper().strip()

        allowed_results = {
            "WIN",
            "LOSS",
            "DRAW",
            "EXPIRED",
            "PENDING",
        }

        if result not in allowed_results:
            raise ValueError(
                f"Недопустимый результат сигнала: {result}"
            )

        with self._lock:
            conn = self._connect()

            try:
                cursor = conn.execute(
                    """
                    UPDATE signals
                    SET
                        result = ?,
                        expiry_price = COALESCE(?, expiry_price),
                        result_checked_at = ?
                    WHERE id = ?
                    """,
                    (
                        result,
                        (
                            float(expiry_price)
                            if expiry_price is not None
                            else None
                        ),
                        self._now(),
                        signal_id,
                    ),
                )

                conn.commit()

                return cursor.rowcount > 0

            finally:
                conn.close()

    # ============================================================
    # SIGNAL STATISTICS
    # ============================================================

    def get_signal_statistics(
        self,
        pair: Optional[str] = None,
        expiry_minutes: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """
        Возвращает статистику сигналов.

        Можно фильтровать:

            pair="EUR/USD"
            expiry_minutes=5

        Если фильтры не указаны — общая статистика.
        """

        conditions = [
            "result IN ('WIN', 'LOSS')"
        ]

        params: list[Any] = []

        if pair:
            conditions.append("pair = ?")
            params.append(pair)

        if expiry_minutes is not None:
            conditions.append("expiry_minutes = ?")
            params.append(int(expiry_minutes))

        where = " AND ".join(conditions)

        limit_sql = ""

        if limit is not None:
            limit_value = max(
                1,
                min(int(limit), 10000),
            )

            limit_sql = f" LIMIT {limit_value}"

        with self._lock:
            conn = self._connect()

            try:
                cursor = conn.execute(
                    f"""
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
                        ) AS losses,
                        AVG(quality) AS average_quality,
                        AVG(probability) AS average_probability
                    FROM (
                        SELECT
                            result,
                            quality,
                            probability
                        FROM signals
                        WHERE {where}
                        ORDER BY id DESC
                        {limit_sql}
                    )
                    """,
                    tuple(params),
                )

                row = cursor.fetchone()

                if row is None:
                    return {
                        "total": 0,
                        "wins": 0,
                        "losses": 0,
                        "winrate": 0.0,
                        "average_quality": 0.0,
                        "average_probability": 0.0,
                    }

                total = int(
                    row["total"] or 0
                )

                wins = int(
                    row["wins"] or 0
                )

                losses = int(
                    row["losses"] or 0
                )

                winrate = (
                    wins / total * 100
                    if total > 0
                    else 0.0
                )

                return {
                    "total": total,
                    "wins": wins,
                    "losses": losses,
                    "winrate": round(
                        winrate,
                        2,
                    ),
                    "average_quality": round(
                        float(
                            row["average_quality"] or 0
                        ),
                        2,
                    ),
                    "average_probability": round(
                        float(
                            row["average_probability"] or 0
                        ),
                        2,
                    ),
                }

            finally:
                conn.close()

    # ============================================================
    # EXPIRY STATISTICS
    # ============================================================

    def get_expiry_statistics(
        self,
        pair: Optional[str] = None,
    ) -> list[dict]:
        """
        Статистика отдельно по каждой экспирации.

        Например:

        1 минута -> 68%
        2 минуты -> 71%
        5 минут -> 79%
        ...
        20 минут -> 63%

        Если pair указан — статистика только для этой пары.
        """

        conditions = [
            "expiry_minutes IS NOT NULL",
            "result IN ('WIN', 'LOSS')",
        ]

        params: list[Any] = []

        if pair:
            conditions.append("pair = ?")
            params.append(pair)

        where = " AND ".join(conditions)

        with self._lock:
            conn = self._connect()

            try:
                cursor = conn.execute(
                    f"""
                    SELECT
                        expiry_minutes,
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
                        ) AS losses,
                        AVG(quality) AS average_quality,
                        AVG(probability) AS average_probability
                    FROM signals
                    WHERE {where}
                    GROUP BY expiry_minutes
                    ORDER BY expiry_minutes ASC
                    """,
                    tuple(params),
                )

                result = []

                for row in cursor.fetchall():
                    total = int(
                        row["total"] or 0
                    )

                    wins = int(
                        row["wins"] or 0
                    )

                    losses = int(
                        row["losses"] or 0
                    )

                    winrate = (
                        wins / total * 100
                        if total > 0
                        else 0.0
                    )

                    result.append(
                        {
                            "expiry_minutes": int(
                                row["expiry_minutes"]
                            ),
                            "total": total,
                            "wins": wins,
                            "losses": losses,
                            "winrate": round(
                                winrate,
                                2,
                            ),
                            "average_quality": round(
                                float(
                                    row["average_quality"]
                                    or 0
                                ),
                                2,
                            ),
                            "average_probability": round(
                                float(
                                    row[
                                        "average_probability"
                                    ]
                                    or 0
                                ),
                                2,
                            ),
                        }
                    )

                return result

            finally:
                conn.close()

    # ============================================================
    # PAIR + EXPIRY STATISTICS
    # ============================================================

    def get_pair_expiry_statistics(
        self,
        pair: str,
        expiry_minutes: int,
    ) -> dict:
        """
        Точная статистика:

            конкретная пара
            +
            конкретная экспирация.
        """

        return self.get_signal_statistics(
            pair=pair,
            expiry_minutes=expiry_minutes,
        )

    # ============================================================
    # BEST EXPIRY
    # ============================================================

    def get_best_expiry(
        self,
        pair: Optional[str] = None,
        min_trades: int = 10,
        min_winrate: float = 50.0,
    ) -> Optional[dict]:
        """
        Выбирает лучшую экспирацию на основании
        РЕАЛЬНЫХ завершённых сигналов.

        Важно:
        экспирация с 1-2 сделками не должна автоматически
        считаться лучшей.

        Поэтому используется min_trades.
        """

        min_trades = max(
            1,
            int(min_trades),
        )

        min_winrate = float(
            min_winrate
        )

        conditions = [
            "expiry_minutes BETWEEN 1 AND 20",
            "result IN ('WIN', 'LOSS')",
        ]

        params: list[Any] = []

        if pair:
            conditions.append("pair = ?")
            params.append(pair)

        where = " AND ".join(conditions)

        with self._lock:
            conn = self._connect()

            try:
                cursor = conn.execute(
                    f"""
                    SELECT
                        expiry_minutes,
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
                        ) AS losses,
                        AVG(quality) AS average_quality,
                        AVG(probability) AS average_probability
                    FROM signals
                    WHERE {where}
                    GROUP BY expiry_minutes
                    HAVING COUNT(*) >= ?
                    ORDER BY
                        (
                            CAST(
                                SUM(
                                    CASE
                                        WHEN result = 'WIN'
                                        THEN 1
                                        ELSE 0
                                    END
                                ) AS REAL
                            )
                            / COUNT(*)
                        ) DESC,
                        COUNT(*) DESC,
                        AVG(quality) DESC
                    """,
                    tuple(
                        params + [min_trades]
                    ),
                )

                rows = cursor.fetchall()

                candidates = []

                for row in rows:
                    total = int(
                        row["total"] or 0
                    )

                    wins = int(
                        row["wins"] or 0
                    )

                    losses = int(
                        row["losses"] or 0
                    )

                    winrate = (
                        wins / total * 100
                        if total > 0
                        else 0.0
                    )

                    if winrate < min_winrate:
                        continue

                    candidates.append(
                        {
                            "expiry_minutes": int(
                                row["expiry_minutes"]
                            ),
                            "total": total,
                            "wins": wins,
                            "losses": losses,
                            "winrate": round(
                                winrate,
                                2,
                            ),
                            "average_quality": round(
                                float(
                                    row["average_quality"]
                                    or 0
                                ),
                                2,
                            ),
                            "average_probability": round(
                                float(
                                    row[
                                        "average_probability"
                                    ]
                                    or 0
                                ),
                                2,
                            ),
                        }
                    )

                if not candidates:
                    return None

                return candidates[0]

            finally:
                conn.close()

    # ============================================================
    # ADAPTIVE STATISTICS REPORT
    # ============================================================

    def get_adaptive_report(
        self,
        pair: Optional[str] = None,
    ) -> dict:
        """
        Возвращает полный отчёт для адаптивного выбора.

        Включает:

        - общую статистику;
        - статистику по экспирациям;
        - лучшую экспирацию.
        """

        overall = self.get_signal_statistics(
            pair=pair,
        )

        expiries = self.get_expiry_statistics(
            pair=pair,
        )

        best = self.get_best_expiry(
            pair=pair,
        )

        return {
            "pair": pair,
            "overall": overall,
            "expiries": expiries,
            "best_expiry": best,
        }

    # ============================================================
    # MAINTENANCE
    # ============================================================

    def backfill_expiry_minutes(self) -> int:
        """
        Заполняет expiry_minutes для старых сигналов,
        где это поле ещё NULL.

        Возвращает количество обновлённых записей.
        """

        updated = 0

        with self._lock:
            conn = self._connect()

            try:
                cursor = conn.execute(
                    """
                    SELECT
                        id,
                        entry_time,
                        expiry_time
                    FROM signals
                    WHERE expiry_minutes IS NULL
                    """
                )

                rows = cursor.fetchall()

                for row in rows:
                    try:
                        entry_dt = datetime.fromisoformat(
                            row["entry_time"].replace(
                                "Z",
                                "+00:00",
                            )
                        )

                        expiry_dt = datetime.fromisoformat(
                            row["expiry_time"].replace(
                                "Z",
                                "+00:00",
                            )
                        )

                        seconds = (
                            expiry_dt - entry_dt
                        ).total_seconds()

                        minutes = round(
                            seconds / 60
                        )

                        if 1 <= minutes <= 20:
                            result = conn.execute(
                                """
                                UPDATE signals
                                SET expiry_minutes = ?
                                WHERE id = ?
                                """,
                                (
                                    minutes,
                                    row["id"],
                                ),
                            )

                            updated += result.rowcount

                    except Exception:
                        continue

                conn.commit()

                return updated

            finally:
                conn.close()


# ================================================================
# GLOBAL DATABASE INSTANCE
# ================================================================

db = Database()
