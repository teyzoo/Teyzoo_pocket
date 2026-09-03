from __future__ import annotations

import asyncio
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
            "300",
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

# Разрешённый диапазон:
# от 1 до 20 минут.
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
# HELPERS
# =========================================================

def _parse_chat_id(
    value: str,
) -> int | str | None:
    """
    Преобразует SIGNAL_CHAT_ID.

    Поддерживает:
        123456789
        -1001234567890
        @channel
    """

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
    """
    Всегда возвращает срок сигнала 1–20 минут.
    """

    try:
        minutes = int(value)

    except (TypeError, ValueError):
        minutes = 5

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
    """
    Приводит значение к datetime.

    Поддерживает:
        datetime
        ISO string
    """

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
    """
    Получает время создания сигнала.
    """

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
    """
    Вычисляет фактическое время окончания сигнала.

    Приоритет:
        1. expiry_time
        2. expiration_time
        3. expires_at
        4. close_time, если это полноценный timestamp
        5. created_at + SIGNAL_EXPIRY_MINUTES
    """

    for attribute in (
        "expiry_time",
        "expiration_time",
        "expires_at",
        "close_time",
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
    """
    Форматирует окончание сигнала для Telegram.
    """

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

    SignalScanner продолжает самостоятельно
    выполнять непрерывное сканирование рынка.
    """

    def __init__(
        self,
        bot: Bot,
        market: MarketClient,
    ) -> None:

        # -------------------------------------------------
        # DEPENDENCIES
        # -------------------------------------------------

        self.bot = bot
        self.market = market

        # -------------------------------------------------
        # STATE
        # -------------------------------------------------

        self._running = False
        self._started = False

        self._stop_event = asyncio.Event()

        # -------------------------------------------------
        # TASKS
        # -------------------------------------------------

        self._signal_generation_task: (
            asyncio.Task[None] | None
        ) = None

        self._signal_warning_task: (
            asyncio.Task[None] | None
        ) = None

        self._signal_result_checker_task: (
            asyncio.Task[None] | None
        ) = None

        # -------------------------------------------------
        # SCANNER
        # -------------------------------------------------

        self.scanner: SignalScanner | None = None

        # -------------------------------------------------
        # ACTIVE SIGNALS
        # -------------------------------------------------

        self._active_signals: dict[
            str,
            TradingSignal,
        ] = {}

        # -------------------------------------------------
        # STATISTICS
        # -------------------------------------------------

        self.generation_cycles = 0
        self.generated_signals = 0
        self.warning_cycles = 0
        self.result_checker_cycles = 0
        self.errors = 0

        logger.info(
            "Scheduler object created."
        )

    # =====================================================
    # START
    # =====================================================

    async def start(
        self,
    ) -> None:
        """
        Запускает scheduler.

        Повторный вызов безопасен.
        """

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

        # -------------------------------------------------
        # CREATE SCANNER
        # -------------------------------------------------

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

        # -------------------------------------------------
        # START TASKS
        # -------------------------------------------------

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

        logger.info(
            "Scheduler tasks:"
        )

        logger.info(
            " - signal_generation"
        )

        logger.info(
            " - signal_warning"
        )

        logger.info(
            " - signal_result_checker"
        )

        logger.info(
            "Signal expiry: %s minutes.",
            SIGNAL_EXPIRY_MINUTES,
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

        # -------------------------------------------------
        # STOP SCANNER
        # -------------------------------------------------

        if self.scanner is not None:
            try:
                await self.scanner.stop()

            except Exception:
                logger.exception(
                    "Error while stopping SignalScanner."
                )

        # -------------------------------------------------
        # COLLECT TASKS
        # -------------------------------------------------

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

        # -------------------------------------------------
        # CANCEL
        # -------------------------------------------------

        for task in tasks:
            if not task.done():
                task.cancel()

        # -------------------------------------------------
        # WAIT
        # -------------------------------------------------

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

        # -------------------------------------------------
        # RESET
        # -------------------------------------------------

        self._signal_generation_task = None
        self._signal_warning_task = None
        self._signal_result_checker_task = None

        self.scanner = None

        self._started = False

        logger.info(
            "TEYZUS scheduler stopped."
        )

    # =====================================================
    # SIGNAL GENERATION
    # =====================================================

    async def _signal_generation_loop(
        self,
    ) -> None:
        """
        Запускает SignalScanner один раз.

        Сам scanner имеет собственный continuous loop.
        """

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
        """
        Получает сигнал от SignalScanner.
        """

        if not self._running:
            return

        self.generated_signals += 1

        # -------------------------------------------------
        # UNIQUE KEY
        # -------------------------------------------------

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
                "SIGNAL RECEIVED BY SCHEDULER | "
                "symbol=%s | "
                "direction=%s | "
                "quality=%.2f | "
                "expires=%s"
            ),
            signal.symbol,
            signal.direction,
            signal.quality_score,
            expiry.isoformat(),
        )

        logger.info(
            "Signal active key=%s",
            key,
        )

        # -------------------------------------------------
        # TELEGRAM
        # -------------------------------------------------

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
    # TELEGRAM
    # =====================================================

    async def _send_signal_to_telegram(
        self,
        signal: TradingSignal,
    ) -> None:
        """
        Отправляет найденный сигнал.

        Если SIGNAL_CHAT_ID не задан,
        scanner продолжает работать.
        """

        chat_id = _parse_chat_id(
            SIGNAL_CHAT_ID_RAW
        )

        if chat_id is None:

            logger.warning(
                (
                    "SIGNAL_CHAT_ID is not configured. "
                    "Signal will not be sent to Telegram: %s"
                ),
                signal.symbol,
            )

            return

        try:

            text = SignalScanner.format_signal(
                signal
            )

            # -------------------------------------------------
            # Добавляем срок действия, если formatter
            # его ещё не показывает.
            # -------------------------------------------------

            expiry_text = _format_expiry(
                signal
            )

            if "Закрытие" not in text:
                text += (
                    "\n"
                    f"⏱ Закрытие: {expiry_text} МСК"
                )

            await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
            )

            logger.info(
                (
                    "Signal sent to Telegram | "
                    "symbol=%s | "
                    "direction=%s | "
                    "quality=%.2f | "
                    "expiry=%s"
                ),
                signal.symbol,
                signal.direction,
                signal.quality_score,
                expiry_text,
            )

        except Exception:
            self.errors += 1

            logger.exception(
                (
                    "Failed to send signal "
                    "to Telegram | symbol=%s"
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

        # -------------------------------------------------
        # FIRST
        # -------------------------------------------------

        try:

            result = function(
                bot=self.bot,
                market=self.market,
            )

            if asyncio.iscoroutine(result):
                await result

            return

        except TypeError:
            pass

        # -------------------------------------------------
        # SECOND
        # -------------------------------------------------

        try:

            result = function(
                bot=self.bot,
            )

            if asyncio.iscoroutine(result):
                await result

            return

        except TypeError:
            pass

        # -------------------------------------------------
        # THIRD
        # -------------------------------------------------

        try:

            result = function(
                market=self.market,
            )

            if asyncio.iscoroutine(result):
                await result

            return

        except TypeError:
            pass

        # -------------------------------------------------
        # FOURTH
        # -------------------------------------------------

        try:

            result = function()

            if asyncio.iscoroutine(result):
                await result

        except TypeError as exc:

            logger.warning(
                (
                    "signal_warning() has unsupported "
                    "signature: %s"
                ),
                exc,
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

                # -------------------------------------------------
                # CLEAN LOCAL ACTIVE SIGNALS
                # -------------------------------------------------

                self._cleanup_old_signals()

                # -------------------------------------------------
                # WAIT
                # -------------------------------------------------

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

        # -------------------------------------------------
        # FIRST
        # -------------------------------------------------

        try:

            result = function(
                bot=self.bot,
                market=self.market,
            )

            if asyncio.iscoroutine(result):
                await result

            return

        except TypeError:
            pass

        # -------------------------------------------------
        # SECOND
        # -------------------------------------------------

        try:

            result = function(
                market=self.market,
            )

            if asyncio.iscoroutine(result):
                await result

            return

        except TypeError:
            pass

        # -------------------------------------------------
        # THIRD
        # -------------------------------------------------

        try:

            result = function(
                bot=self.bot,
            )

            if asyncio.iscoroutine(result):
                await result

            return

        except TypeError:
            pass

        # -------------------------------------------------
        # FOURTH
        # -------------------------------------------------

        try:

            result = function()

            if asyncio.iscoroutine(result):
                await result

        except TypeError as exc:

            logger.warning(
                (
                    "signal_result_checker() has "
                    "unsupported signature: %s"
                ),
                exc,
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
            SIGNAL_EXPIRY_MINUTES * 60
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

        if expired:

            logger.debug(
                (
                    "Cleaned %s expired "
                    "signals from scheduler cache."
                ),
                len(expired),
            )

    # =====================================================
    # MANUAL SCAN
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
            "scanner": scanner_stats,
        }


# =========================================================
# FACTORY
# =========================================================

def create_scheduler(
    bot: Bot,
    market: MarketClient,
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
    "create_scheduler",
]
