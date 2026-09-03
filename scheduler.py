from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot

from market import MarketClient
from signal_scanner import (
    SignalScanner,
    TradingSignal,
)

logger = logging.getLogger("scheduler")


# =========================================================
# CONFIGURATION
# =========================================================

SIGNAL_GENERATION_INTERVAL = max(
    10,
    int(
        os.getenv(
            "SCHEDULER_SIGNAL_GENERATION_INTERVAL",
            "60",
        )
    ),
)

SIGNAL_WARNING_SECONDS = max(
    10,
    int(
        os.getenv(
            "SIGNAL_WARNING_SECONDS",
            "120",
        )
    ),
)

SIGNAL_RESULT_CHECK_INTERVAL = max(
    5,
    int(
        os.getenv(
            "SIGNAL_RESULT_CHECK_INTERVAL",
            "15",
        )
    ),
)

SIGNAL_EXPIRY_MINUTES = max(
    1,
    min(
        20,
        int(
            os.getenv(
                "SIGNAL_EXPIRY_MINUTES",
                "5",
            )
        ),
    ),
)

# Минимальный допустимый quality.
# Основная проверка также выполняется внутри SignalScanner.
MIN_SIGNAL_QUALITY = max(
    0.0,
    min(
        100.0,
        float(
            os.getenv(
                "SIGNAL_MINIMUM_QUALITY",
                os.getenv(
                    "SIGNAL_MIN_QUALITY",
                    os.getenv(
                        "MIN_SIGNAL_QUALITY",
                        "75",
                    ),
                ),
            )
        ),
    ),
)

# Twelve Data Free:
# 8 credits/min.
#
# SignalScanner уже ограничивает количество пар
# примерно до 2 пар × 3 таймфрейма = 6 запросов.
MAX_MANUAL_PAIRS = max(
    1,
    int(
        os.getenv(
            "MAX_MANUAL_SIGNAL_PAIRS",
            "2",
        )
    ),
)

SIGNAL_CHAT_ID_RAW = os.getenv(
    "SIGNAL_CHAT_ID",
    "",
).strip()


# =========================================================
# OPTIONAL MODULES
# =========================================================

try:
    from signal_warning import signal_warning

    SIGNAL_WARNING_AVAILABLE = True

except ImportError:
    signal_warning = None
    SIGNAL_WARNING_AVAILABLE = False

    logger.warning(
        "signal_warning module is not available."
    )


try:
    from signal_result_checker import (
        signal_result_checker,
    )

    SIGNAL_RESULT_CHECKER_AVAILABLE = True

except ImportError:
    signal_result_checker = None
    SIGNAL_RESULT_CHECKER_AVAILABLE = False

    logger.warning(
        "signal_result_checker module is not available."
    )


# =========================================================
# DATABASE
# =========================================================

try:
    import database as db

    DATABASE_AVAILABLE = True

except ImportError:
    db = None
    DATABASE_AVAILABLE = False

    logger.warning(
        "database module is not available."
    )


# =========================================================
# HELPERS
# =========================================================

def _parse_chat_id(
    value: str,
) -> int | str | None:
    value = value.strip()

    if not value:
        return None

    try:
        return int(value)

    except ValueError:
        return value


def _safe_task_name(
    task: asyncio.Task[Any] | None,
) -> str:
    if task is None:
        return "unknown"

    try:
        return task.get_name()

    except Exception:
        return "unknown"


def _clamp_expiry_minutes(
    value: int | float | str | None,
) -> int:
    try:
        minutes = int(value)

    except (TypeError, ValueError):
        minutes = SIGNAL_EXPIRY_MINUTES

    return max(
        1,
        min(
            20,
            minutes,
        ),
    )


def _ensure_datetime(
    value: Any,
) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        return value

    if isinstance(value, str):
        text = value.strip()

        if not text:
            return None

        try:
            return datetime.fromisoformat(
                text.replace(
                    "Z",
                    "+00:00",
                )
            )

        except ValueError:
            return None

    return None


def _signal_created_at(
    signal: TradingSignal,
) -> datetime:
    value = _ensure_datetime(
        getattr(
            signal,
            "created_at",
            None,
        )
    )

    if value is not None:
        return value

    return datetime.now().astimezone()


def _signal_expiry(
    signal: TradingSignal,
) -> datetime:
    for attribute in (
        "expiry_time",
        "expiration_time",
        "expires_at",
    ):
        value = _ensure_datetime(
            getattr(
                signal,
                attribute,
                None,
            )
        )

        if value is not None:
            return value

    # close_time может быть timestamp.
    close_time = _ensure_datetime(
        getattr(
            signal,
            "close_time",
            None,
        )
    )

    if close_time is not None:
        return close_time

    created_at = _signal_created_at(
        signal
    )

    expiry_minutes = _clamp_expiry_minutes(
        getattr(
            signal,
            "expiry_minutes",
            SIGNAL_EXPIRY_MINUTES,
        )
    )

    return (
        created_at
        + timedelta(
            minutes=expiry_minutes
        )
    )


def _format_expiry(
    signal: TradingSignal,
) -> str:
    expiry = _signal_expiry(
        signal
    )

    try:
        return expiry.astimezone().strftime(
            "%H:%M"
        )

    except Exception:
        return expiry.strftime(
            "%H:%M"
        )


def _normalize_pair(
    pair: str | None,
) -> str | None:
    if pair is None:
        return None

    value = str(pair).strip()

    if not value:
        return None

    # OTC пока НЕ отправляем через Twelve Data.
    # Не подменяем OTC обычным Forex.
    if value.lower().endswith("_otc"):
        return None

    value = value.replace(
        "/",
        "",
    ).replace(
        "_",
        "",
    ).replace(
        "-",
        "",
    )

    return value.upper()


def _signal_quality(
    signal: TradingSignal,
) -> float:
    try:
        return float(
            getattr(
                signal,
                "quality_score",
                0.0,
            )
        )

    except (
        TypeError,
        ValueError,
    ):
        return 0.0


def _is_good_signal(
    signal: TradingSignal,
) -> bool:
    return (
        _signal_quality(signal)
        >= MIN_SIGNAL_QUALITY
    )


async def _maybe_await(
    value: Any,
) -> Any:
    if inspect.isawaitable(value):
        return await value

    return value


# =========================================================
# SCHEDULER
# =========================================================

class Scheduler:
    """
    Главный scheduler.

    Управляет:

        1. SignalScanner
        2. предупреждениями
        3. проверкой результатов
        4. автоматической отправкой сигналов
        5. ручным поиском сигналов
    """

    def __init__(
        self,
        bot: Bot,
        market: MarketClient | None = None,
    ) -> None:

        self.bot = bot

        self.market = (
            market
            if market is not None
            else MarketClient()
        )

        self._running = False
        self._started = False

        self._stop_event = asyncio.Event()

        self._signal_generation_task: (
            asyncio.Task[None] | None
        ) = None

        self._signal_warning_task: (
            asyncio.Task[None] | None
        ) = None

        self._signal_result_checker_task: (
            asyncio.Task[None] | None
        ) = None

        self.scanner: SignalScanner | None = None

        self._active_signals: dict[
            str,
            TradingSignal,
        ] = {}

        self.generation_cycles = 0
        self.generated_signals = 0
        self.warning_cycles = 0
        self.result_checker_cycles = 0
        self.errors = 0

        logger.info(
            "Scheduler object created."
        )

        logger.info(
            "Minimum signal quality: %.2f",
            MIN_SIGNAL_QUALITY,
        )

        logger.info(
            "Default signal expiry: %s minutes.",
            SIGNAL_EXPIRY_MINUTES,
        )

    # =====================================================
    # START
    # =====================================================

    async def start(
        self,
    ) -> None:

        if self._started:
            logger.warning(
                "Scheduler is already started."
            )
            return

        logger.info(
            "================================================"
        )
        logger.info(
            "STARTING TEYZUS SCHEDULER"
        )
        logger.info(
            "================================================"
        )

        self._started = True
        self._running = True
        self._stop_event.clear()

        try:
            self.scanner = SignalScanner(
                market_client=self.market,
                send_signal=self._handle_signal,
            )

            logger.info(
                "SignalScanner initialized."
            )

        except Exception:
            self._running = False
            self._started = False

            logger.exception(
                "Failed to initialize SignalScanner."
            )

            raise

        self._signal_generation_task = (
            asyncio.create_task(
                self._signal_generation_loop(),
                name="signal_generation",
            )
        )

        self._signal_warning_task = (
            asyncio.create_task(
                self._signal_warning_loop(),
                name="signal_warning",
            )
        )

        self._signal_result_checker_task = (
            asyncio.create_task(
                self._signal_result_checker_loop(),
                name="signal_result_checker",
            )
        )

        logger.info(
            "Scheduler started: 3 tasks."
        )

    # =====================================================
    # STOP
    # =====================================================

    async def stop(
        self,
    ) -> None:

        if not self._started:
            return

        logger.info(
            "Stopping TEYZUS scheduler..."
        )

        self._running = False
        self._stop_event.set()

        if self.scanner is not None:
            try:
                await self.scanner.stop()

            except Exception:
                logger.exception(
                    "Error while stopping SignalScanner."
                )

        tasks: list[
            asyncio.Task[Any]
        ] = []

        for task in (
            self._signal_generation_task,
            self._signal_warning_task,
            self._signal_result_checker_task,
        ):
            if task is not None:
                tasks.append(task)

        for task in tasks:
            if not task.done():
                task.cancel()

        if tasks:
            results = await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

            for task, result in zip(
                tasks,
                results,
            ):
                if (
                    isinstance(
                        result,
                        Exception,
                    )
                    and not isinstance(
                        result,
                        asyncio.CancelledError,
                    )
                ):
                    logger.error(
                        (
                            "Scheduler task %s "
                            "stopped with error: %s"
                        ),
                        _safe_task_name(task),
                        result,
                    )

        self._signal_generation_task = None
        self._signal_warning_task = None
        self._signal_result_checker_task = None

        self.scanner = None
        self._started = False

        # Закрываем MarketClient только если scheduler
        # сам его создал.
        try:
            close_method = getattr(
                self.market,
                "close",
                None,
            )

            if close_method is not None:
                await _maybe_await(
                    close_method()
                )

        except Exception:
            logger.exception(
                "Error while closing MarketClient."
            )

        logger.info(
            "TEYZUS scheduler stopped."
        )

    # =====================================================
    # SIGNAL GENERATION
    # =====================================================

    async def _signal_generation_loop(
        self,
    ) -> None:

        logger.info(
            "================================================"
        )
        logger.info(
            "SIGNAL GENERATION LOOP STARTED"
        )
        logger.info(
            "================================================"
        )

        scanner = self.scanner

        if scanner is None:
            logger.error(
                "SignalScanner is not initialized."
            )
            return

        try:

            await scanner.start()

            logger.info(
                "SignalScanner started."
            )

            while self._running:

                self.generation_cycles += 1

                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=SIGNAL_GENERATION_INTERVAL,
                    )

                    break

                except asyncio.TimeoutError:
                    pass

        except asyncio.CancelledError:
            raise

        except Exception:
            self.errors += 1

            logger.exception(
                "Signal generation loop crashed."
            )

        finally:

            logger.info(
                "Signal generation loop stopped."
            )

    # =====================================================
    # HANDLE SIGNAL
    # =====================================================

    async def _handle_signal(
        self,
        signal: TradingSignal,
    ) -> None:

        if not self._running:
            return

        quality = _signal_quality(
            signal
        )

        if quality < MIN_SIGNAL_QUALITY:
            logger.info(
                (
                    "Signal rejected by scheduler quality "
                    "filter | symbol=%s | quality=%.2f | "
                    "minimum=%.2f"
                ),
                signal.symbol,
                quality,
                MIN_SIGNAL_QUALITY,
            )
            return

        self.generated_signals += 1

        key = self._signal_key(
            signal
        )

        self._active_signals[
            key
        ] = signal

        expiry = _signal_expiry(
            signal
        )

        logger.info(
            "================================================"
        )

        logger.info(
            (
                "SIGNAL RECEIVED | "
                "symbol=%s | "
                "direction=%s | "
                "quality=%.2f | "
                "expires=%s"
            ),
            signal.symbol,
            signal.direction,
            quality,
            expiry.isoformat(),
        )

        await self._send_signal_to_telegram(
            signal
        )

    # =====================================================
    # SIGNAL KEY
    # =====================================================

    @staticmethod
    def _signal_key(
        signal: TradingSignal,
    ) -> str:

        created = _signal_created_at(
            signal
        )

        return (
            f"{signal.symbol}:"
            f"{signal.direction}:"
            f"{created.timestamp():.0f}"
        )

    # =====================================================
    # GET APPROVED USERS
    # =====================================================

    async def _get_approved_user_ids(
        self,
    ) -> list[int]:

        if not DATABASE_AVAILABLE or db is None:
            return []

        try:
            function = getattr(
                db,
                "get_active_users",
                None,
            )

            if function is None:
                function = getattr(
                    db,
                    "get_approved_users",
                    None,
                )

            if function is None:
                logger.error(
                    (
                        "Database has neither "
                        "get_active_users() nor "
                        "get_approved_users()."
                    )
                )
                return []

            result = await _maybe_await(
                function()
            )

            if result is None:
                return []

            users: list[int] = []

            for item in result:

                user_id: Any = None

                if isinstance(
                    item,
                    int,
                ):
                    user_id = item

                elif isinstance(
                    item,
                    str,
                ):
                    try:
                        user_id = int(item)

                    except ValueError:
                        user_id = None

                elif isinstance(
                    item,
                    dict,
                ):
                    for key in (
                        "telegram_id",
                        "user_id",
                        "id",
                    ):
                        if key in item:
                            user_id = item[key]
                            break

                else:
                    for attribute in (
                        "telegram_id",
                        "user_id",
                        "id",
                    ):
                        if hasattr(
                            item,
                            attribute,
                        ):
                            user_id = getattr(
                                item,
                                attribute,
                            )
                            break

                try:
                    if user_id is not None:
                        users.append(
                            int(user_id)
                        )

                except (
                    TypeError,
                    ValueError,
                ):
                    continue

            return list(
                dict.fromkeys(users)
            )

        except Exception:
            self.errors += 1

            logger.exception(
                "Failed to load approved users."
            )

            return []

    # =====================================================
    # TELEGRAM
    # =====================================================

    async def _send_signal_to_telegram(
        self,
        signal: TradingSignal,
    ) -> None:

        quality = _signal_quality(
            signal
        )

        if quality < MIN_SIGNAL_QUALITY:
            return

        try:

            text = SignalScanner.format_signal(
                signal
            )

        except Exception:
            logger.exception(
                "Failed to format signal."
            )

            return

        expiry_text = _format_expiry(
            signal
        )

        if "Закрытие" not in text:
            text += (
                "\n"
                f"⏱ Закрытие: {expiry_text}"
            )

        # -------------------------------------------------
        # ОСНОВНОЙ РЕЖИМ:
        # отправляем всем одобренным пользователям.
        # -------------------------------------------------

        user_ids = await self._get_approved_user_ids()

        if user_ids:

            logger.info(
                (
                    "Broadcasting signal to "
                    "%s approved users."
                ),
                len(user_ids),
            )

            sent = 0
            failed = 0

            for user_id in user_ids:

                try:

                    await self.bot.send_message(
                        chat_id=user_id,
                        text=text,
                        parse_mode="HTML",
                    )

                    sent += 1

                except Exception:
                    failed += 1

                    logger.exception(
                        (
                            "Failed to send signal "
                            "to user %s | symbol=%s"
                        ),
                        user_id,
                        signal.symbol,
                    )

                # Небольшая пауза, чтобы не создавать
                # лишнюю нагрузку на Telegram API.
                await asyncio.sleep(
                    0.05
                )

            logger.info(
                (
                    "Signal broadcast finished | "
                    "symbol=%s | sent=%s | failed=%s | "
                    "quality=%.2f | expiry=%s"
                ),
                signal.symbol,
                sent,
                failed,
                quality,
                expiry_text,
            )

            return

        # -------------------------------------------------
        # FALLBACK:
        # если БД недоступна/пустая, сохраняем старую
        # возможность отправки через SIGNAL_CHAT_ID.
        # -------------------------------------------------

        chat_id = _parse_chat_id(
            SIGNAL_CHAT_ID_RAW
        )

        if chat_id is None:

            logger.warning(
                (
                    "No approved users found and "
                    "SIGNAL_CHAT_ID is not configured. "
                    "Signal was not sent | symbol=%s"
                ),
                signal.symbol,
            )

            return

        try:

            await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
            )

            logger.info(
                (
                    "Signal sent to SIGNAL_CHAT_ID | "
                    "symbol=%s | direction=%s | "
                    "quality=%.2f | expiry=%s"
                ),
                signal.symbol,
                signal.direction,
                quality,
                expiry_text,
            )

        except Exception:
            self.errors += 1

            logger.exception(
                (
                    "Failed to send signal "
                    "to SIGNAL_CHAT_ID | symbol=%s"
                ),
                signal.symbol,
            )

    # =====================================================
    # WARNING LOOP
    # =====================================================

    async def _signal_warning_loop(
        self,
    ) -> None:

        logger.info(
            "Signal warning scheduler started."
        )

        if not SIGNAL_WARNING_AVAILABLE:

            logger.info(
                (
                    "signal_warning.py is not available. "
                    "Warning scheduler is idle."
                )
            )

        try:

            while self._running:

                self.warning_cycles += 1

                if (
                    SIGNAL_WARNING_AVAILABLE
                    and signal_warning is not None
                ):

                    try:
                        await self._call_warning_function()

                    except asyncio.CancelledError:
                        raise

                    except Exception:

                        self.errors += 1

                        logger.exception(
                            "Signal warning cycle failed."
                        )

                try:

                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=15,
                    )

                    break

                except asyncio.TimeoutError:
                    pass

        except asyncio.CancelledError:
            raise

        finally:

            logger.info(
                "Signal warning scheduler stopped."
            )

    # =====================================================
    # CALL WARNING FUNCTION
    # =====================================================

    async def _call_warning_function(
        self,
    ) -> None:

        if signal_warning is None:
            return

        function = signal_warning

        variants = (
            {
                "bot": self.bot,
                "market": self.market,
            },
            {
                "bot": self.bot,
            },
            {
                "market": self.market,
            },
            {},
        )

        for kwargs in variants:

            try:

                result = function(
                    **kwargs
                )

                await _maybe_await(
                    result
                )

                return

            except TypeError:
                continue

        logger.warning(
            "signal_warning() has unsupported signature."
        )

    # =====================================================
    # RESULT CHECKER LOOP
    # =====================================================

    async def _signal_result_checker_loop(
        self,
    ) -> None:

        logger.info(
            "Result checker started."
        )

        if not SIGNAL_RESULT_CHECKER_AVAILABLE:

            logger.info(
                (
                    "signal_result_checker.py "
                    "is not available. "
                    "Result checker is idle."
                )
            )

        try:

            while self._running:

                self.result_checker_cycles += 1

                if (
                    SIGNAL_RESULT_CHECKER_AVAILABLE
                    and signal_result_checker is not None
                ):

                    try:

                        await self._call_result_checker()

                    except asyncio.CancelledError:
                        raise

                    except Exception:

                        self.errors += 1

                        logger.exception(
                            "Result checker cycle failed."
                        )

                self._cleanup_old_signals()

                try:

                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=SIGNAL_RESULT_CHECK_INTERVAL,
                    )

                    break

                except asyncio.TimeoutError:
                    pass

        except asyncio.CancelledError:
            raise

        finally:

            logger.info(
                "Result checker stopped."
            )

    # =====================================================
    # CALL RESULT CHECKER
    # =====================================================

    async def _call_result_checker(
        self,
    ) -> None:

        if signal_result_checker is None:
            return

        function = signal_result_checker

        variants = (
            {
                "bot": self.bot,
                "market": self.market,
            },
            {
                "market": self.market,
            },
            {
                "bot": self.bot,
            },
            {},
        )

        for kwargs in variants:

            try:

                result = function(
                    **kwargs
                )

                await _maybe_await(
                    result
                )

                return

            except TypeError:
                continue

        logger.warning(
            (
                "signal_result_checker() has "
                "unsupported signature."
            )
        )

    # =====================================================
    # CLEAN OLD SIGNALS
    # =====================================================

    def _cleanup_old_signals(
        self,
    ) -> None:

        if not self._active_signals:
            return

        max_age = (
            20 * 60
            + SIGNAL_WARNING_SECONDS
            + 60
        )

        expired: list[str] = []

        current_timestamp = time.time()

        for key, signal in (
            self._active_signals.items()
        ):

            try:

                created_timestamp = (
                    _signal_created_at(
                        signal
                    ).timestamp()
                )

                age = (
                    current_timestamp
                    - created_timestamp
                )

                if age > max_age:
                    expired.append(key)

            except Exception:

                expired.append(key)

        for key in expired:

            self._active_signals.pop(
                key,
                None,
            )

    # =====================================================
    # MANUAL SIGNAL
    # =====================================================

    async def get_manual_signal(
        self,
        pair: str | None = None,
        expiry_minutes: int | None = None,
    ) -> TradingSignal | None:
        """
        Ручной поиск одного сильного сигнала.

        Используется кнопкой:
            🎯 Получить сигнал

        expiry_minutes:
            None = любое время
            1..20 = конкретное время

        ВАЖНО:
        OTC не подменяется обычной парой.
        Twelve Data не используется для фиктивных OTC
        свечей.
        """

        if not self._running:
            raise RuntimeError(
                "Scheduler is not running."
            )

        expiry = (
            None
            if expiry_minutes is None
            else _clamp_expiry_minutes(
                expiry_minutes
            )
        )

        normalized_pair = _normalize_pair(
            pair
        )

        # -------------------------------------------------
        # Если пользователь выбрал OTC,
        # честно возвращаем отсутствие сигнала.
        # -------------------------------------------------

        if pair is not None and normalized_pair is None:

            logger.info(
                (
                    "Manual OTC signal requested but "
                    "Twelve Data OTC candles are unavailable."
                )
            )

            return None

        # -------------------------------------------------
        # Используем уже работающий scanner.
        # -------------------------------------------------

        scanner = self.scanner

        if scanner is None:
            raise RuntimeError(
                "SignalScanner is not initialized."
            )

        logger.info(
            (
                "Manual signal requested | "
                "pair=%s | expiry=%s"
            ),
            normalized_pair or "ANY",
            expiry if expiry is not None else "ANY",
        )

        # -------------------------------------------------
        # Если конкретная пара:
        # анализируем только её.
        # -------------------------------------------------

        if normalized_pair is not None:

            signal = await self._analyze_manual_pair(
                scanner,
                normalized_pair,
                expiry,
            )

            if signal is not None:
                self._remember_manual_signal(
                    signal
                )

            return signal

        # -------------------------------------------------
        # Любая пара.
        #
        # Не сканируем все пары сразу:
        # Twelve Data Free = 8 credits/min.
        #
        # Максимум MAX_MANUAL_PAIRS пар.
        # -------------------------------------------------

        try:
            from config import PAIRS

        except Exception:
            PAIRS = []

        candidates: list[str] = []

        for raw_pair in PAIRS:

            normalized = _normalize_pair(
                raw_pair
            )

            if normalized is None:
                continue

            if normalized not in candidates:
                candidates.append(
                    normalized
                )

        best_signal: TradingSignal | None = None

        checked = 0

        for candidate in candidates:

            if checked >= MAX_MANUAL_PAIRS:
                break

            checked += 1

            try:

                signal = await self._analyze_manual_pair(
                    scanner,
                    candidate,
                    expiry,
                )

            except Exception:
                self.errors += 1

                logger.exception(
                    (
                        "Manual analysis failed | "
                        "pair=%s"
                    ),
                    candidate,
                )

                continue

            if signal is None:
                continue

            if (
                best_signal is None
                or _signal_quality(signal)
                > _signal_quality(best_signal)
            ):
                best_signal = signal

        if best_signal is not None:

            self._remember_manual_signal(
                best_signal
            )

            logger.info(
                (
                    "Best manual signal selected | "
                    "symbol=%s | quality=%.2f"
                ),
                best_signal.symbol,
                _signal_quality(best_signal),
            )

        else:

            logger.info(
                (
                    "No strong manual signal found | "
                    "checked=%s | minimum_quality=%.2f"
                ),
                checked,
                MIN_SIGNAL_QUALITY,
            )

        return best_signal

    # =====================================================
    # MANUAL PAIR ANALYSIS
    # =====================================================

    async def _analyze_manual_pair(
        self,
        scanner: SignalScanner,
        pair: str,
        expiry_minutes: int | None,
    ) -> TradingSignal | None:
        """
        Выполняет ручной анализ через внутренний метод
        SignalScanner.

        Не запускает второй scanner.
        """

        normalized = _normalize_pair(
            pair
        )

        if normalized is None:
            return None

        # SignalScanner._analyze_symbol требует _running.
        was_running = getattr(
            scanner,
            "_running",
            False,
        )

        if not was_running:
            scanner._running = True

        try:

            result = scanner._analyze_symbol(
                normalized
            )

            result = await _maybe_await(
                result
            )

        finally:

            if not was_running:
                scanner._running = False

        if result is None:
            return None

        # -------------------------------------------------
        # _analyze_symbol может вернуть TradingSignal
        # или другой результат в зависимости от версии.
        # -------------------------------------------------

        if isinstance(
            result,
            TradingSignal,
        ):
            signal = result

        elif isinstance(
            result,
            (list, tuple),
        ):
            signals = [
                item
                for item in result
                if isinstance(
                    item,
                    TradingSignal,
                )
            ]

            if not signals:
                return None

            signal = max(
                signals,
                key=_signal_quality,
            )

        else:
            return None

        # -------------------------------------------------
        # Принудительно устанавливаем выбранное время.
        # -------------------------------------------------

        if expiry_minutes is not None:

            self._apply_expiry(
                signal,
                expiry_minutes,
            )

        if not _is_good_signal(
            signal
        ):
            logger.info(
                (
                    "Manual signal rejected | "
                    "pair=%s | quality=%.2f | "
                    "minimum=%.2f"
                ),
                normalized,
                _signal_quality(signal),
                MIN_SIGNAL_QUALITY,
            )

            return None

        return signal

    # =====================================================
    # APPLY EXPIRY
    # =====================================================

    @staticmethod
    def _apply_expiry(
        signal: TradingSignal,
        expiry_minutes: int,
    ) -> None:

        minutes = _clamp_expiry_minutes(
            expiry_minutes
        )

        now = datetime.now().astimezone()

        try:
            signal.expiry_minutes = minutes

        except Exception:
            pass

        expiry = (
            now
            + timedelta(
                minutes=minutes
            )
        )

        # Заполняем возможные поля объекта.
        for attribute in (
            "expiry_time",
            "expiration_time",
            "expires_at",
            "close_time",
        ):
            try:
                if hasattr(
                    signal,
                    attribute,
                ):
                    setattr(
                        signal,
                        attribute,
                        expiry,
                    )

            except Exception:
                pass

    # =====================================================
    # REMEMBER MANUAL SIGNAL
    # =====================================================

    def _remember_manual_signal(
        self,
        signal: TradingSignal,
    ) -> None:

        key = self._signal_key(
            signal
        )

        self._active_signals[
            key
        ] = signal

    # =====================================================
    # SCAN NOW
    # =====================================================

    async def scan_now(
        self,
    ) -> list[TradingSignal]:

        if not self._running:
            raise RuntimeError(
                "Scheduler is not running."
            )

        if self.scanner is None:
            raise RuntimeError(
                "SignalScanner is not initialized."
            )

        logger.info(
            "Manual signal scan requested."
        )

        return await self.scanner.scan_once()

    # =====================================================
    # STATUS
    # =====================================================

    @property
    def running(
        self,
    ) -> bool:

        return self._running

    @property
    def started(
        self,
    ) -> bool:

        return self._started

    # =====================================================
    # GET SCANNER
    # =====================================================

    def get_scanner(
        self,
    ) -> SignalScanner | None:

        return self.scanner

    # =====================================================
    # GET ACTIVE SIGNALS
    # =====================================================

    def get_active_signals(
        self,
    ) -> list[TradingSignal]:

        return list(
            self._active_signals.values()
        )

    # =====================================================
    # GET STATISTICS
    # =====================================================

    def get_stats(
        self,
    ) -> dict[str, Any]:

        scanner_stats = None

        if self.scanner is not None:

            try:

                scanner_stats = (
                    self.scanner.get_stats()
                )

            except Exception:

                scanner_stats = None

        return {
            "running": self._running,
            "started": self._started,
            "generation_cycles": (
                self.generation_cycles
            ),
            "generated_signals": (
                self.generated_signals
            ),
            "warning_cycles": (
                self.warning_cycles
            ),
            "result_checker_cycles": (
                self.result_checker_cycles
            ),
            "errors": self.errors,
            "active_signals": (
                len(
                    self._active_signals
                )
            ),
            "signal_expiry_minutes": (
                SIGNAL_EXPIRY_MINUTES
            ),
            "minimum_signal_quality": (
                MIN_SIGNAL_QUALITY
            ),
            "scanner": scanner_stats,
        }


# =========================================================
# COMPATIBILITY CLASS
# =========================================================

class SignalScheduler(Scheduler):
    """
    Совместимое имя для main.py.

    Позволяет создавать:

        SignalScheduler(bot)

    или:

        SignalScheduler(bot, market)
    """

    def __init__(
        self,
        bot: Bot,
        market: MarketClient | None = None,
    ) -> None:

        super().__init__(
            bot=bot,
            market=market,
        )


# =========================================================
# FACTORY
# =========================================================

def create_scheduler(
    bot: Bot,
    market: MarketClient | None = None,
) -> Scheduler:

    return Scheduler(
        bot=bot,
        market=market,
    )


# =========================================================
# EXPORTS
# =========================================================

__all__ = [
    "Scheduler",
    "SignalScheduler",
    "create_scheduler",
]
