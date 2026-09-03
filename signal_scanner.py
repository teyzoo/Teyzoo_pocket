from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from market import (
    Candle,
    MarketClient,
    MarketDataError,
    MarketRateLimitError,
)
from models import Direction
from quality_filter import (
    QualityResult,
    TimeframeAnalysis,
    analyze_timeframe,
    quality_filter,
)


logger = logging.getLogger("signal_scanner")


# =========================================================
# CONFIGURATION
# =========================================================


SCAN_INTERVAL_SECONDS = max(
    30,
    int(
        os.getenv(
            "SIGNAL_SCAN_INTERVAL",
            "60",
        )
    ),
)


CANDLE_LIMIT = max(
    20,
    int(
        os.getenv(
            "SIGNAL_CANDLE_LIMIT",
            "200",
        )
    ),
)


# Минимальное качество сигнала.
# По умолчанию 75%.
MINIMUM_QUALITY = float(
    os.getenv(
        "SIGNAL_MINIMUM_QUALITY",
        "75",
    )
)


# ---------------------------------------------------------
# Срок жизни сигнала.
#
# Разрешено:
#     1–20 минут
#
# Значение можно менять через Render Environment Variables:
#
# SIGNAL_EXPIRY_MINUTES=1
# SIGNAL_EXPIRY_MINUTES=5
# SIGNAL_EXPIRY_MINUTES=10
# SIGNAL_EXPIRY_MINUTES=20
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# Таймфреймы для подтверждения.
#
# По умолчанию:
#
# 1m
# 5m
# 15m
# ---------------------------------------------------------

TIMEFRAMES = tuple(
    item.strip()
    for item in os.getenv(
        "SIGNAL_TIMEFRAMES",
        "1m,5m,15m",
    ).split(",")
    if item.strip()
)


# =========================================================
# TYPES
# =========================================================


SignalCallback = Callable[
    ["TradingSignal"],
    Awaitable[None],
]


# =========================================================
# TRADING SIGNAL
# =========================================================


@dataclass(slots=True)
class TradingSignal:
    symbol: str

    direction: Direction

    quality_score: float

    confirmations: int

    total_checks: int

    timeframe_results: list[
        TimeframeAnalysis
    ]

    reasons: list[str]

    created_at: datetime

    # -----------------------------------------------------
    # Срок сигнала.
    #
    # Это новое поле необязательное с точки зрения
    # совместимости: если старый код создаёт TradingSignal
    # без него, будет использовано значение по умолчанию.
    # -----------------------------------------------------

    expiry_minutes: int = SIGNAL_EXPIRY_MINUTES

    @property
    def expiry_time(self) -> datetime:
        """
        Фактическое время окончания сигнала.
        """

        minutes = max(
            1,
            min(
                20,
                int(
                    self.expiry_minutes
                ),
            ),
        )

        created = self.created_at

        if created.tzinfo is None:
            created = created.replace(
                tzinfo=timezone.utc
            )

        return (
            created
            + timedelta(
                minutes=minutes
            )
        )

    @property
    def close_time(self) -> datetime:
        """
        Алиас для совместимости с другими модулями.
        """

        return self.expiry_time

    @property
    def direction_text(self) -> str:
        if self.direction == Direction.UP:
            return "🟢 CALL / UP"

        if self.direction == Direction.DOWN:
            return "🔴 PUT / DOWN"

        return "⚪ UNKNOWN"

    @property
    def emoji(self) -> str:
        if self.direction == Direction.UP:
            return "🟢"

        if self.direction == Direction.DOWN:
            return "🔴"

        return "⚪"


# =========================================================
# SCANNER STATISTICS
# =========================================================


@dataclass(slots=True)
class ScannerStats:
    cycles: int = 0

    pairs_seen: int = 0

    pairs_analyzed: int = 0

    signals_found: int = 0

    signals_sent: int = 0

    rejected: int = 0

    errors: int = 0

    rate_limits: int = 0

    last_cycle_at: datetime | None = None

    last_signal_at: datetime | None = None


# =========================================================
# SIGNAL SCANNER
# =========================================================


class SignalScanner:
    """
    Автоматический сканер торговых пар.

    Основная задача:

        1. Получить список пар.
        2. Выбрать очередные пары.
        3. Получить свечи.
        4. Проанализировать каждый таймфрейм.
        5. Передать результаты в QualityFilter.
        6. Если качество >= порога:
               создать TradingSignal
        7. Отправить сигнал через callback.
        8. Через заданный интервал повторить.

    Сканер работает последовательно.

    Это важно для Twelve Data, поскольку API имеет
    ограничение количества запросов в минуту.
    """

    def __init__(
        self,
        market_client: MarketClient,
        send_signal: SignalCallback | None = None,
        symbols: list[str] | None = None,
        timeframes: tuple[str, ...] = TIMEFRAMES,
        scan_interval: int = SCAN_INTERVAL_SECONDS,
        candle_limit: int = CANDLE_LIMIT,
        minimum_quality: float = MINIMUM_QUALITY,
        expiry_minutes: int = SIGNAL_EXPIRY_MINUTES,
    ) -> None:

        self.market_client = market_client

        self.send_signal = send_signal

        self.timeframes = tuple(
            timeframe.strip()
            for timeframe in timeframes
            if timeframe.strip()
        )

        self.scan_interval = max(
            30,
            int(scan_interval),
        )

        self.candle_limit = max(
            20,
            int(candle_limit),
        )

        self.minimum_quality = float(
            minimum_quality
        )

        self.expiry_minutes = max(
            1,
            min(
                20,
                int(
                    expiry_minutes
                ),
            ),
        )

        # -------------------------------------------------
        # PAIRS
        # -------------------------------------------------

        self.symbols = self._normalize_symbols(
            symbols
            if symbols is not None
            else self._load_symbols_from_env()
        )

        # -------------------------------------------------
        # ROTATION
        # -------------------------------------------------

        self._pair_index = 0

        # -------------------------------------------------
        # STATE
        # -------------------------------------------------

        self._running = False

        self._task: asyncio.Task[None] | None = None

        self._stop_event = asyncio.Event()

        self._scan_lock = asyncio.Lock()

        # -------------------------------------------------
        # DUPLICATE SIGNAL PROTECTION
        # -------------------------------------------------

        self._last_signal_key: dict[
            str,
            tuple[
                Direction,
                int,
            ],
        ] = {}

        self._last_signal_time: dict[
            str,
            float,
        ] = {}

        self.signal_cooldown = max(
            0,
            int(
                os.getenv(
                    "SIGNAL_COOLDOWN",
                    "300",
                )
            ),
        )

        # -------------------------------------------------
        # STATISTICS
        # -------------------------------------------------

        self.stats = ScannerStats()

    # =====================================================
    # LOAD SYMBOLS FROM ENV
    # =====================================================

    @staticmethod
    def _load_symbols_from_env() -> list[str]:
        """
        Загружает пары из MARKET_SYMBOLS.

        Если переменная не задана, используется
        расширенный стандартный список Forex.
        """

        raw = os.getenv(
            "MARKET_SYMBOLS",
            "",
        ).strip()

        if raw:
            return [
                item.strip()
                for item in raw.split(",")
                if item.strip()
            ]

        return [
            # Major
            "EUR/USD",
            "GBP/USD",
            "USD/JPY",
            "USD/CHF",
            "USD/CAD",
            "AUD/USD",
            "NZD/USD",

            # EUR
            "EUR/GBP",
            "EUR/JPY",
            "EUR/CHF",
            "EUR/AUD",
            "EUR/CAD",
            "EUR/NZD",

            # GBP
            "GBP/JPY",
            "GBP/CHF",
            "GBP/AUD",
            "GBP/CAD",
            "GBP/NZD",

            # CHF
            "CHF/JPY",

            # AUD
            "AUD/JPY",
            "AUD/CHF",
            "AUD/CAD",
            "AUD/NZD",

            # CAD
            "CAD/JPY",
            "CAD/CHF",

            # NZD
            "NZD/JPY",
            "NZD/CHF",
            "NZD/CAD",
        ]

    # =====================================================
    # NORMALIZE SYMBOLS
    # =====================================================

    @staticmethod
    def _normalize_symbols(
        symbols: list[str],
    ) -> list[str]:

        result: list[str] = []

        seen: set[str] = set()

        for symbol in symbols:

            normalized = (
                str(symbol)
                .strip()
                .upper()
            )

            if not normalized:
                continue

            # EURUSD -> EUR/USD
            if (
                "/"
                not in normalized
                and len(normalized) == 6
                and normalized.isalpha()
            ):
                normalized = (
                    normalized[:3]
                    + "/"
                    + normalized[3:]
                )

            if normalized in seen:
                continue

            seen.add(
                normalized
            )

            result.append(
                normalized
            )

        return result

    # =====================================================
    # SET SYMBOLS
    # =====================================================

    def set_symbols(
        self,
        symbols: list[str],
    ) -> None:

        normalized = (
            self._normalize_symbols(
                symbols
            )
        )

        if not normalized:
            raise ValueError(
                "Symbol list cannot be empty."
            )

        self.symbols = normalized

        self._pair_index = 0

        logger.info(
            "Scanner symbols updated: %s pairs.",
            len(self.symbols),
        )

    # =====================================================
    # GET SYMBOLS
    # =====================================================

    def get_symbols(self) -> list[str]:
        return list(
            self.symbols
        )

    # =====================================================
    # NEXT SYMBOLS
    # =====================================================

    def _get_next_symbols(
        self,
    ) -> list[str]:
        """
        Возвращает пары для очередного прохода.

        SIGNAL_PAIRS_PER_CYCLE позволяет ограничить
        количество пар за один цикл.
        """

        if not self.symbols:
            return []

        configured = os.getenv(
            "SIGNAL_PAIRS_PER_CYCLE",
            "",
        ).strip()

        if configured:

            try:
                pairs_per_cycle = max(
                    1,
                    int(configured),
                )

            except ValueError:
                pairs_per_cycle = len(
                    self.symbols
                )

        else:
            # -------------------------------------------------
            # Безопасный бюджет запросов.
            #
            # При 3 TF:
            #
            # 6 запросов / цикл
            # = примерно 2 пары.
            # -------------------------------------------------

            request_budget = 6

            pairs_per_cycle = max(
                1,
                request_budget
                // max(
                    1,
                    len(self.timeframes),
                ),
            )

        pairs_per_cycle = min(
            pairs_per_cycle,
            len(self.symbols),
        )

        result: list[str] = []

        for _ in range(
            pairs_per_cycle
        ):

            symbol = self.symbols[
                self._pair_index
            ]

            result.append(
                symbol
            )

            self._pair_index = (
                self._pair_index + 1
            ) % len(self.symbols)

        return result

    # =====================================================
    # START
    # =====================================================

    async def start(
        self,
    ) -> None:

        if self._running:
            logger.warning(
                "Signal scanner already running."
            )
            return

        if not self.symbols:
            raise RuntimeError(
                "Signal scanner has no symbols."
            )

        if not self.timeframes:
            raise RuntimeError(
                "Signal scanner has no timeframes."
            )

        self._running = True

        self._stop_event.clear()

        self._task = asyncio.create_task(
            self._run_loop(),
            name="signal-scanner",
        )

        logger.info(
            (
                "Signal scanner started | "
                "pairs=%s | "
                "timeframes=%s | "
                "interval=%ss | "
                "quality>=%.1f | "
                "expiry=%sm"
            ),
            len(self.symbols),
            self.timeframes,
            self.scan_interval,
            self.minimum_quality,
            self.expiry_minutes,
        )

    # =====================================================
    # STOP
    # =====================================================

    async def stop(
        self,
    ) -> None:

        if not self._running:
            return

        logger.info(
            "Stopping signal scanner..."
        )

        self._running = False

        self._stop_event.set()

        task = self._task

        self._task = None

        if task is not None:

            try:
                await task

            except asyncio.CancelledError:
                pass

        logger.info(
            "Signal scanner stopped."
        )

    # =====================================================
    # RUN LOOP
    # =====================================================

    async def _run_loop(
        self,
    ) -> None:
        """
        Главный бесконечный цикл.

        Сканирование происходит автоматически.
        Пользователю не нужно нажимать кнопку.
        """

        # Даём приложению закончить запуск.
        await asyncio.sleep(
            2
        )

        while self._running:

            cycle_started = time.monotonic()

            try:

                await self.scan_once()

            except asyncio.CancelledError:
                raise

            except Exception:

                self.stats.errors += 1

                logger.exception(
                    "Unexpected signal scanner error."
                )

            elapsed = (
                time.monotonic()
                - cycle_started
            )

            wait_for = max(
                1.0,
                self.scan_interval
                - elapsed,
            )

            logger.info(
                (
                    "Scanner cycle finished | "
                    "elapsed=%.1fs | "
                    "next cycle in=%.1fs"
                ),
                elapsed,
                wait_for,
            )

            try:

                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=wait_for,
                )

                break

            except asyncio.TimeoutError:
                pass

    # =====================================================
    # SCAN ONCE
    # =====================================================

    async def scan_once(
        self,
    ) -> list[TradingSignal]:
        """
        Один полный проход очередных пар.
        """

        async with self._scan_lock:

            self.stats.cycles += 1

            self.stats.last_cycle_at = (
                datetime.now(
                    timezone.utc
                )
            )

            symbols = (
                self._get_next_symbols()
            )

            self.stats.pairs_seen += len(
                symbols
            )

            logger.info(
                "================================================"
            )

            logger.info(
                (
                    "SIGNAL SCAN START | "
                    "cycle=%s | "
                    "pairs=%s | "
                    "timeframes=%s | "
                    "minimum_quality=%.1f"
                ),
                self.stats.cycles,
                len(symbols),
                self.timeframes,
                self.minimum_quality,
            )

            signals: list[
                TradingSignal
            ] = []

            for symbol in symbols:

                if not self._running:
                    break

                try:

                    signal = (
                        await self._analyze_symbol(
                            symbol
                        )
                    )

                    self.stats.pairs_analyzed += 1

                    if signal is None:

                        self.stats.rejected += 1

                        continue

                    signals.append(
                        signal
                    )

                    self.stats.signals_found += 1

                    logger.info(
                        (
                            "SIGNAL FOUND | "
                            "%s | "
                            "%s | "
                            "quality=%.2f | "
                            "confirmations=%s/%s | "
                            "expiry=%sm"
                        ),
                        signal.symbol,
                        signal.direction,
                        signal.quality_score,
                        signal.confirmations,
                        signal.total_checks,
                        signal.expiry_minutes,
                    )

                    await self._send_signal(
                        signal
                    )

                except MarketRateLimitError as exc:

                    self.stats.rate_limits += 1

                    logger.warning(
                        (
                            "RATE LIMIT while "
                            "analyzing %s: %s"
                        ),
                        symbol,
                        exc,
                    )

                    break

                except MarketDataError as exc:

                    logger.warning(
                        (
                            "Market data error "
                            "for %s: %s"
                        ),
                        symbol,
                        exc,
                    )

                except ValueError as exc:

                    logger.warning(
                        (
                            "Invalid market data "
                            "for %s: %s"
                        ),
                        symbol,
                        exc,
                    )

                except Exception:

                    self.stats.errors += 1

                    logger.exception(
                        "Error analyzing symbol %s.",
                        symbol,
                    )

            logger.info(
                (
                    "SIGNAL SCAN END | "
                    "signals=%s | "
                    "analyzed=%s"
                ),
                len(signals),
                len(symbols),
            )

            return signals

    # =====================================================
    # ANALYZE SYMBOL
    # =====================================================

    async def _analyze_symbol(
        self,
        symbol: str,
    ) -> TradingSignal | None:
        """
        Анализ одной пары.
        """

        logger.info(
            (
                "Analyzing pair %s | "
                "timeframes=%s"
            ),
            symbol,
            self.timeframes,
        )

        analyses: list[
            TimeframeAnalysis
        ] = []

        for timeframe in self.timeframes:

            if not self._running:
                return None

            logger.debug(
                (
                    "Loading candles | "
                    "symbol=%s | "
                    "timeframe=%s | "
                    "limit=%s"
                ),
                symbol,
                timeframe,
                self.candle_limit,
            )

            try:

                candles = (
                    await self.market_client.get_candles(
                        symbol=symbol,
                        timeframe=timeframe,
                        limit=self.candle_limit,
                    )
                )

            except MarketRateLimitError:
                raise

            except (
                MarketDataError,
                ValueError,
            ) as exc:

                logger.warning(
                    (
                        "Could not load candles "
                        "for %s %s: %s"
                    ),
                    symbol,
                    timeframe,
                    exc,
                )

                continue

            if len(candles) < 20:

                logger.warning(
                    (
                        "Too few candles "
                        "for %s %s: %s"
                    ),
                    symbol,
                    timeframe,
                    len(candles),
                )

                continue

            try:

                analysis = (
                    analyze_timeframe(
                        timeframe=timeframe,
                        candles=candles,
                    )
                )

            except Exception:

                self.stats.errors += 1

                logger.exception(
                    (
                        "Signal engine error "
                        "for %s %s"
                    ),
                    symbol,
                    timeframe,
                )

                continue

            analyses.append(
                analysis
            )

        # -------------------------------------------------
        # NO ANALYSIS
        # -------------------------------------------------

        if not analyses:

            logger.info(
                (
                    "No timeframe analysis "
                    "available for %s."
                ),
                symbol,
            )

            return None

        # -------------------------------------------------
        # QUALITY FILTER
        # -------------------------------------------------

        result = self._evaluate_quality(
            analyses
        )

        if not result.accepted:

            logger.info(
                (
                    "Signal rejected | "
                    "%s | "
                    "quality=%.2f | "
                    "reasons=%s"
                ),
                symbol,
                result.quality_score,
                result.rejected_reasons,
            )

            return None

        # -------------------------------------------------
        # DUPLICATE PROTECTION
        # -------------------------------------------------

        if self._is_duplicate_signal(
            symbol=symbol,
            result=result,
        ):

            logger.info(
                (
                    "Duplicate signal skipped | "
                    "%s | "
                    "direction=%s"
                ),
                symbol,
                result.direction,
            )

            return None

        # -------------------------------------------------
        # DIRECTION
        # -------------------------------------------------

        if result.direction is None:
            return None

        # -------------------------------------------------
        # CREATE SIGNAL
        # -------------------------------------------------

        created_at = datetime.now(
            timezone.utc
        )

        signal = TradingSignal(
            symbol=symbol,
            direction=result.direction,
            quality_score=result.quality_score,
            confirmations=result.confirmations,
            total_checks=result.total_checks,
            timeframe_results=result.timeframe_results,
            reasons=list(
                result.reasons
            ),
            created_at=created_at,
            expiry_minutes=self.expiry_minutes,
        )

        return signal

    # =====================================================
    # QUALITY
    # =====================================================

    def _evaluate_quality(
        self,
        analyses: list[
            TimeframeAnalysis
        ],
    ) -> QualityResult:
        """
        Использует существующий QualityFilter.
        """

        if abs(
            quality_filter.minimum_quality
            - self.minimum_quality
        ) < 0.0001:

            return quality_filter.evaluate(
                analyses
            )

        from quality_filter import QualityFilter

        local_filter = QualityFilter(
            minimum_quality=self.minimum_quality
        )

        return local_filter.evaluate(
            analyses
        )

    # =====================================================
    # DUPLICATE SIGNAL
    # =====================================================

    def _is_duplicate_signal(
        self,
        symbol: str,
        result: QualityResult,
    ) -> bool:

        if result.direction is None:
            return True

        now = time.monotonic()

        last_time = (
            self._last_signal_time.get(
                symbol
            )
        )

        last_key = (
            self._last_signal_key.get(
                symbol
            )
        )

        current_key = (
            result.direction,
            int(
                result.quality_score
                // 5
            ),
        )

        # -------------------------------------------------
        # Не спамим одинаковым направлением.
        # -------------------------------------------------

        if (
            last_time is not None
            and now - last_time
            < self.signal_cooldown
            and last_key is not None
            and last_key[0]
            == current_key[0]
        ):
            return True

        self._last_signal_time[
            symbol
        ] = now

        self._last_signal_key[
            symbol
        ] = current_key

        return False

    # =====================================================
    # SEND SIGNAL
    # =====================================================

    async def _send_signal(
        self,
        signal: TradingSignal,
    ) -> None:

        if self.send_signal is None:

            logger.warning(
                (
                    "Signal found but "
                    "send_signal callback "
                    "is not configured: %s"
                ),
                signal.symbol,
            )

            return

        try:

            await self.send_signal(
                signal
            )

            self.stats.signals_sent += 1

            self.stats.last_signal_at = (
                datetime.now(
                    timezone.utc
                )
            )

        except Exception:

            self.stats.errors += 1

            logger.exception(
                (
                    "Failed to send signal "
                    "for %s."
                ),
                signal.symbol,
            )

    # =====================================================
    # FORMAT SIGNAL
    # =====================================================

    @staticmethod
    def format_signal(
        signal: TradingSignal,
    ) -> str:
        """
        Готовый Telegram-текст сигнала.

        В сообщении сразу показывается:
        - пара
        - направление
        - качество
        - подтверждения
        - таймфреймы
        - причины
        - время формирования
        - время закрытия
        - срок
        """

        direction = (
            signal.direction_text
        )

        # -------------------------------------------------
        # CREATED TIME
        # -------------------------------------------------

        created_at = signal.created_at

        if created_at.tzinfo is None:
            created_at = created_at.replace(
                tzinfo=timezone.utc
            )

        # -------------------------------------------------
        # EXPIRY
        # -------------------------------------------------

        try:
            expiry = signal.expiry_time

        except Exception:
            expiry = (
                created_at
                + timedelta(
                    minutes=SIGNAL_EXPIRY_MINUTES
                )
            )

        lines = [
            "🚨 <b>НОВЫЙ СИГНАЛ</b>",
            "",
            f"💱 <b>Пара:</b> "
            f"<code>{signal.symbol}</code>",
            "",
            f"{signal.emoji} "
            f"<b>Направление:</b> "
            f"{direction}",
            "",
            f"⭐ <b>Качество:</b> "
            f"{signal.quality_score:.1f}%",
            "",
            f"✅ <b>Подтверждения:</b> "
            f"{signal.confirmations}/"
            f"{signal.total_checks}",
            "",
            f"⏱ <b>Срок:</b> "
            f"{signal.expiry_minutes} мин.",
            "",
            "📊 <b>Таймфреймы:</b>",
        ]

        # -------------------------------------------------
        # TIMEFRAME RESULTS
        # -------------------------------------------------

        for item in (
            signal.timeframe_results
        ):

            if item.direction == Direction.UP:
                direction_text = "🟢 UP"

            elif item.direction == Direction.DOWN:
                direction_text = "🔴 DOWN"

            else:
                direction_text = "⚪ НЕТ"

            lines.append(
                (
                    f"• <b>{item.timeframe}</b> — "
                    f"{direction_text} — "
                    f"{item.score:.1f}%"
                )
            )

        # -------------------------------------------------
        # REASONS
        # -------------------------------------------------

        if signal.reasons:

            lines.extend(
                [
                    "",
                    "🧠 <b>Подтверждения:</b>",
                ]
            )

            for reason in signal.reasons[:8]:

                lines.append(
                    f"• {reason}"
                )

        # -------------------------------------------------
        # TIMES
        # -------------------------------------------------

        lines.extend(
            [
                "",
                "🕐 <b>Сформирован:</b> "
                f"{created_at.strftime('%H:%M:%S')} UTC",
                "",
                "🔔 <b>Закрытие:</b> "
                f"{expiry.strftime('%H:%M:%S')} UTC",
            ]
        )

        return "\n".join(
            lines
        )

    # =====================================================
    # STATS
    # =====================================================

    def get_stats(
        self,
    ) -> ScannerStats:
        return self.stats

    # =====================================================
    # STATUS
    # =====================================================

    @property
    def running(
        self,
    ) -> bool:
        return self._running


# =========================================================
# TELEGRAM CALLBACK HELPER
# =========================================================


async def telegram_signal_sender(
    bot,
    chat_id: int,
    signal: TradingSignal,
) -> None:
    """
    Готовый callback для Aiogram Bot.
    """

    text = (
        SignalScanner.format_signal(
            signal
        )
    )

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
    )


# =========================================================
# SIMPLE FACTORY
# =========================================================


def create_signal_scanner(
    market_client: MarketClient,
    send_signal: SignalCallback | None = None,
) -> SignalScanner:
    """
    Создаёт scanner из Environment Variables.
    """

    return SignalScanner(
        market_client=market_client,
        send_signal=send_signal,
        symbols=None,
        timeframes=TIMEFRAMES,
        scan_interval=SCAN_INTERVAL_SECONDS,
        candle_limit=CANDLE_LIMIT,
        minimum_quality=MINIMUM_QUALITY,
        expiry_minutes=SIGNAL_EXPIRY_MINUTES,
    )


# =========================================================
# EXPORTS
# =========================================================


__all__ = [
    "TradingSignal",
    "ScannerStats",
    "SignalScanner",
    "telegram_signal_sender",
    "create_signal_scanner",
]
