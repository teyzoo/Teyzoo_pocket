from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Any

from config import (
    MIN_PROBABILITY,
    MIN_QUALITY,
    PAIRS,
    TIMEZONE,
)

from market import market_client
from signal_engine import Signal, SignalEngine

from database import db as default_database

from backtest import (
    DEFAULT_TEST_POINTS,
    ExpiryBacktester,
)


logger = logging.getLogger(__name__)


# ================================================================
# SETTINGS
# ================================================================

MAX_PAIRS_PER_SCAN = 6

REQUEST_DELAY_SECONDS = 2.0
RATE_LIMIT_COOLDOWN_SECONDS = 65

MIN_EXPIRY_MINUTES = 1
MAX_EXPIRY_MINUTES = 20

DEFAULT_EXPIRY_MINUTES = 5

ANY_EXPIRY = "any"

# Кэш исторического backtest.
BACKTEST_CACHE_SECONDS = 300

# Минимум реальных исторических сделок,
# прежде чем использовать экспирацию для ANY.
MIN_BACKTEST_TRADES = 10

# Как часто проверяем завершившиеся сигналы.
RESULT_CHECK_INTERVAL_SECONDS = 30

# Максимум сигналов за один проход проверки.
MAX_RESULT_CHECK_BATCH = 100

# Сколько минут истории запрашиваем для определения
# цены входа/закрытия.
RESULT_CANDLE_LIMIT = 300

# ================================================================
# GLOBAL RESULT LOCK
# ================================================================

_RESULT_LOCK = asyncio.Lock()


class SignalScheduler:
    """
    Планировщик автоматических и ручных сигналов.

    Возможности:

        1..20 минут
        Любое время
        автоматический backtest
        автоматическая рассылка
        автоматическое определение WIN/LOSS
        статистика по паре
        статистика по экспирации
        адаптивный выбор экспирации

    ВАЖНО:

        Расчётная probability модели НЕ является
        гарантированной вероятностью выигрыша.

        Реальный WINRATE считается только по
        завершённым сигналам.
    """

    def __init__(
        self,
        bot=None,
        database=None,
    ) -> None:

        self.bot = bot

        # Если main.py не передаст database,
        # используем глобальную БД проекта.
        self.database = (
            database
            if database is not None
            else default_database
        )

        self.engine = SignalEngine(
            min_quality=MIN_QUALITY,
            min_probability=MIN_PROBABILITY,
        )

        self.backtester = ExpiryBacktester(
            engine=self.engine,
            min_trades=MIN_BACKTEST_TRADES,
        )

        self.running = False

        self.task: asyncio.Task | None = None
        self.result_task: asyncio.Task | None = None

        self.auto_expiry_minutes = (
            DEFAULT_EXPIRY_MINUTES
        )

        self.auto_pair: Optional[str] = None

        self._last_request_time = 0.0

        self._signal_lock = asyncio.Lock()

        # --------------------------------------------------------
        # Backtest cache
        # --------------------------------------------------------

        self._backtest_cache: dict[
            str,
            tuple[
                float,
                Optional[int],
                list,
            ],
        ] = {}

    # ============================================================
    # EXPIRY
    # ============================================================

    @staticmethod
    def normalize_expiry(
        expiry_minutes,
    ) -> Optional[int]:

        if expiry_minutes is None:
            return DEFAULT_EXPIRY_MINUTES

        if isinstance(
            expiry_minutes,
            str,
        ):

            value = (
                expiry_minutes
                .strip()
                .lower()
            )

            if value in {
                "any",
                "all",
                "auto",
                "any_time",
                "любое",
                "любое время",
            }:

                return None

            try:

                expiry_minutes = int(
                    value
                )

            except ValueError:

                return DEFAULT_EXPIRY_MINUTES

        try:

            value = int(
                expiry_minutes
            )

        except (
            TypeError,
            ValueError,
        ):

            return DEFAULT_EXPIRY_MINUTES

        return max(
            MIN_EXPIRY_MINUTES,
            min(
                MAX_EXPIRY_MINUTES,
                value,
            ),
        )

    def set_auto_expiry(
        self,
        expiry_minutes,
    ) -> None:

        self.auto_expiry_minutes = (
            self.normalize_expiry(
                expiry_minutes
            )
        )

    def set_auto_pair(
        self,
        pair: Optional[str],
    ) -> None:

        self.auto_pair = pair

    # ============================================================
    # TIME
    # ============================================================

    def _now(self) -> datetime:

        try:

            return datetime.now(
                TIMEZONE
            )

        except Exception:

            from zoneinfo import ZoneInfo

            return datetime.now(
                ZoneInfo(
                    "Europe/Moscow"
                )
            )

    def _next_minute(
        self,
        dt: Optional[datetime] = None,
    ) -> datetime:

        if dt is None:
            dt = self._now()

        if dt.tzinfo is None:

            dt = dt.replace(
                tzinfo=TIMEZONE
            )

        dt = dt.astimezone(
            TIMEZONE
        )

        return (
            dt.replace(
                second=0,
                microsecond=0,
            )
            + timedelta(minutes=1)
        )

    # ============================================================
    # DATETIME HELPERS
    # ============================================================

    @staticmethod
    def _parse_datetime(
        value: Any,
    ) -> Optional[datetime]:

        if value is None:
            return None

        if isinstance(
            value,
            datetime,
        ):

            dt = value

        else:

            text = str(value).strip()

            if not text:
                return None

            try:

                dt = datetime.fromisoformat(
                    text.replace(
                        "Z",
                        "+00:00",
                    )
                )

            except Exception:

                return None

        if dt.tzinfo is None:

            dt = dt.replace(
                tzinfo=TIMEZONE
            )

        return dt.astimezone(
            TIMEZONE
        )

    # ============================================================
    # PAIRS
    # ============================================================

    def _select_pairs_for_scan(
        self,
        requested_pair: Optional[str] = None,
    ) -> list[str]:

        if requested_pair:

            normalized = str(
                requested_pair
            ).strip().lower()

            if normalized not in {
                "any",
                "any_regular",
                "any_otc",
                "all",
            }:

                return [
                    requested_pair
                ]

        return list(
            PAIRS[
                :MAX_PAIRS_PER_SCAN
            ]
        )

    # ============================================================
    # RATE LIMIT
    # ============================================================

    async def _respect_rate_limit(
        self,
    ) -> None:

        loop = (
            asyncio.get_running_loop()
        )

        now = loop.time()

        elapsed = (
            now
            - self._last_request_time
        )

        if (
            elapsed
            < REQUEST_DELAY_SECONDS
        ):

            await asyncio.sleep(
                REQUEST_DELAY_SECONDS
                - elapsed
            )

        self._last_request_time = (
            loop.time()
        )

    # ============================================================
    # CANDLES
    # ============================================================

    async def get_candles(
        self,
        pair: str,
        limit: int = 220,
        interval: str = "1min",
    ):

        """
        Получает свечи нужного таймфрейма.

        Сначала используем совместимый get_history(),
        потому что текущий market.py уже предоставляет
        этот метод.

        Если его нет — пробуем get_candles().
        """

        await self._respect_rate_limit()

        # --------------------------------------------------------
        # Основной вариант:
        # market.py -> get_history(pair, interval, limit)
        # --------------------------------------------------------

        try:

            if hasattr(
                market_client,
                "get_history",
            ):

                candles = (
                    await market_client.get_history(
                        pair=pair,
                        interval=interval,
                        limit=limit,
                    )
                )

                if candles is not None:
                    return candles

        except TypeError:

            # Совместимость со старой сигнатурой.
            try:

                candles = (
                    await market_client.get_history(
                        pair,
                        interval,
                        limit,
                    )
                )

                if candles is not None:
                    return candles

            except Exception as exc:

                logger.debug(
                    "[MARKET] "
                    "get_history positional error "
                    "%s: %s",
                    pair,
                    exc,
                )

        except Exception as exc:

            logger.warning(
                "[MARKET] "
                "get_history error %s: %s",
                pair,
                exc,
            )

        # --------------------------------------------------------
        # Fallback
        # --------------------------------------------------------

        try:

            return await market_client.get_candles(
                pair=pair,
                limit=limit,
            )

        except TypeError:

            try:

                return await market_client.get_candles(
                    pair,
                    limit,
                )

            except Exception as exc:

                logger.warning(
                    "[MARKET] "
                    "get_candles fallback error "
                    "%s: %s",
                    pair,
                    exc,
                )

                return None

        except Exception as exc:

            logger.warning(
                "[MARKET] "
                "get_candles error %s: %s",
                pair,
                exc,
            )

            return None

    # ============================================================
    # BACKTEST
    # ============================================================

    async def _get_best_expiry_from_backtest(
        self,
        pair: str,
        candles,
    ) -> tuple[
        Optional[int],
        list,
    ]:

        now = time.time()

        cached = (
            self._backtest_cache.get(
                pair
            )
        )

        if cached is not None:

            (
                cached_at,
                best_expiry,
                results,
            ) = cached

            if (
                now - cached_at
                < BACKTEST_CACHE_SECONDS
            ):

                return (
                    best_expiry,
                    results,
                )

        logger.info(
            "[BACKTEST] "
            "%s: историческая проверка "
            "экспираций 1-20m",
            pair,
        )

        try:

            best_expiry, results = (
                await asyncio.to_thread(
                    self.backtester.choose_best_expiry,
                    pair,
                    candles,
                    DEFAULT_TEST_POINTS,
                )
            )

        except Exception as exc:

            logger.exception(
                "[BACKTEST] "
                "%s error: %s",
                pair,
                exc,
            )

            return (
                None,
                [],
            )

        self._backtest_cache[
            pair
        ] = (
            now,
            best_expiry,
            results,
        )

        if best_expiry is None:

            logger.info(
                "[BACKTEST] "
                "%s: подходящей экспирации "
                "не найдено",
                pair,
            )

        else:

            selected = next(
                (
                    item
                    for item in results
                    if (
                        item.expiry_minutes
                        == best_expiry
                    )
                ),
                None,
            )

            if selected is not None:

                logger.info(
                    "[BACKTEST] "
                    "%s: BEST=%sm | "
                    "WINRATE=%.1f%% | "
                    "W=%s | L=%s | N=%s",
                    pair,
                    best_expiry,
                    selected.winrate,
                    selected.wins,
                    selected.losses,
                    selected.total,
                )

        return (
            best_expiry,
            results,
        )

    # ============================================================
    # ANALYZE PAIR
    # ============================================================

    async def analyze_pair(
        self,
        pair: str,
        expiry_minutes=None,
    ) -> Optional[Signal]:

        normalized_expiry = (
            self.normalize_expiry(
                expiry_minutes
            )
        )

        candles = await self.get_candles(
            pair=pair,
            limit=220,
            interval="1min",
        )

        if candles is None:

            logger.info(
                "[SCHEDULER] "
                "%s: свечи недоступны",
                pair,
            )

            return None

        # ========================================================
        # КОНКРЕТНАЯ ЭКСПИРАЦИЯ
        # ========================================================

        if normalized_expiry is not None:

            signal = self.engine.analyze(
                pair=pair,
                candles=candles,
                expiry_minutes=normalized_expiry,
            )

            if signal is not None:

                logger.info(
                    "[SCHEDULER] "
                    "%s | %sm | %s | "
                    "quality=%.1f | "
                    "probability=%.1f",
                    pair,
                    normalized_expiry,
                    signal.direction,
                    signal.quality,
                    signal.probability,
                )

            return signal

        # ========================================================
        # ЛЮБОЕ ВРЕМЯ
        # ========================================================

        best_expiry, results = (
            await self._get_best_expiry_from_backtest(
                pair=pair,
                candles=candles,
            )
        )

        if best_expiry is None:

            return None

        logger.info(
            "[SCHEDULER] "
            "%s | ANY → %sm",
            pair,
            best_expiry,
        )

        signal = self.engine.analyze(
            pair=pair,
            candles=candles,
            expiry_minutes=best_expiry,
        )

        return signal

    # ============================================================
    # BEST SIGNAL
    # ============================================================

    async def _find_best_signal(
        self,
        requested_pair: Optional[str] = None,
        expiry_minutes=None,
    ) -> Optional[Signal]:

        pairs = (
            self._select_pairs_for_scan(
                requested_pair
            )
        )

        if not pairs:
            return None

        signals: list[
            Signal
        ] = []

        for pair in pairs:

            try:

                signal = await self.analyze_pair(
                    pair=pair,
                    expiry_minutes=expiry_minutes,
                )

                if signal is not None:

                    signals.append(
                        signal
                    )

            except Exception as exc:

                logger.exception(
                    "[SCHEDULER] "
                    "%s analysis error: %s",
                    pair,
                    exc,
                )

        if not signals:
            return None

        # --------------------------------------------------------
        # Выбираем самый сильный сигнал.
        #
        # 1. Probability
        # 2. Quality
        # 3. Подтверждения
        # --------------------------------------------------------

        signals.sort(
            key=lambda signal: (
                float(
                    signal.probability
                ),
                float(
                    signal.quality
                ),
                len(
                    signal.confirmations
                ),
            ),
            reverse=True,
        )

        return signals[0]

    # ============================================================
    # MANUAL
    # ============================================================

    async def get_manual_signal(
        self,
        pair: Optional[str] = None,
        expiry_minutes=None,
    ) -> Optional[Signal]:

        normalized_expiry = (
            self.normalize_expiry(
                expiry_minutes
            )
        )

        async with self._signal_lock:

            return await self._find_best_signal(
                requested_pair=pair,
                expiry_minutes=normalized_expiry,
            )

    # ============================================================
    # SIGNAL PRICE
    # ============================================================

    @staticmethod
    def _extract_candle_datetime(
        row: Any,
    ) -> Optional[datetime]:

        # --------------------------------------------------------
        # dict / sqlite Row / mapping
        # --------------------------------------------------------

        if hasattr(
            row,
            "keys",
        ):

            try:

                keys = set(
                    row.keys()
                )

                for key in (
                    "datetime",
                    "timestamp",
                    "time",
                    "date",
                ):

                    if key in keys:

                        value = row[key]

                        dt = (
                            SignalScheduler
                            ._parse_datetime(
                                value
                            )
                        )

                        if dt is not None:
                            return dt

            except Exception:
                pass

        # --------------------------------------------------------
        # pandas Series
        # --------------------------------------------------------

        try:

            if hasattr(
                row,
                "name",
            ):

                dt = (
                    SignalScheduler
                    ._parse_datetime(
                        row.name
                    )
                )

                if dt is not None:
                    return dt

        except Exception:
            pass

        return None

    @staticmethod
    def _extract_candle_close(
        row: Any,
    ) -> Optional[float]:

        # --------------------------------------------------------
        # dict / mapping
        # --------------------------------------------------------

        if hasattr(
            row,
            "keys",
        ):

            try:

                keys = set(
                    row.keys()
                )

                for key in (
                    "close",
                    "Close",
                    "CLOSE",
                ):

                    if key in keys:

                        value = row[key]

                        if value is None:
                            continue

                        return float(
                            value
                        )

            except Exception:
                pass

        # --------------------------------------------------------
        # pandas Series
        # --------------------------------------------------------

        try:

            for key in (
                "close",
                "Close",
                "CLOSE",
            ):

                if key in row:

                    value = row[key]

                    if value is None:
                        continue

                    return float(
                        value
                    )

        except Exception:
            pass

        # --------------------------------------------------------
        # Object attribute
        # --------------------------------------------------------

        for key in (
            "close",
            "Close",
        ):

            try:

                value = getattr(
                    row,
                    key,
                )

                if value is not None:

                    return float(
                        value
                    )

            except Exception:
                continue

        return None

    def _normalize_candles_for_result(
        self,
        candles,
    ) -> list[tuple[
        Optional[datetime],
        float,
    ]]:

        if candles is None:
            return []

        result = []

        # --------------------------------------------------------
        # pandas DataFrame
        # --------------------------------------------------------

        try:

            columns = {
                str(column).lower()
                for column in candles.columns
            }

            if "close" in columns:

                for index, row in candles.iterrows():

                    dt = (
                        self._extract_candle_datetime(
                            row
                        )
                    )

                    if dt is None:

                        dt = (
                            self._parse_datetime(
                                index
                            )
                        )

                    price = (
                        self._extract_candle_close(
                            row
                        )
                    )

                    if price is not None:

                        result.append(
                            (
                                dt,
                                price,
                            )
                        )

                return result

        except Exception:
            pass

        # --------------------------------------------------------
        # list / tuple
        # --------------------------------------------------------

        try:

            for row in candles:

                dt = (
                    self._extract_candle_datetime(
                        row
                    )
                )

                price = (
                    self._extract_candle_close(
                        row
                    )
                )

                if price is not None:

                    result.append(
                        (
                            dt,
                            price,
                        )
                    )

        except Exception:
            return []

        return result

    def _find_price_at_or_after(
        self,
        candles,
        target_time: datetime,
    ) -> Optional[float]:

        rows = (
            self._normalize_candles_for_result(
                candles
            )
        )

        if not rows:
            return None

        target_time = (
            self._parse_datetime(
                target_time
            )
        )

        if target_time is None:
            return None

        # --------------------------------------------------------
        # Если есть timestamp — ищем первую свечу,
        # которая соответствует или идёт после нужного времени.
        # --------------------------------------------------------

        timestamp_rows = [
            item
            for item in rows
            if item[0] is not None
        ]

        if timestamp_rows:

            timestamp_rows.sort(
                key=lambda item: item[0]
            )

            for dt, price in timestamp_rows:

                if dt >= target_time:

                    return float(
                        price
                    )

            # Если нужная свеча ещё не пришла,
            # берём последнюю доступную только если
            # она не слишком старая.
            last_dt, last_price = (
                timestamp_rows[-1]
            )

            delta = (
                target_time - last_dt
            ).total_seconds()

            if 0 <= delta <= 120:

                return float(
                    last_price
                )

            return None

        # --------------------------------------------------------
        # Если timestamp отсутствует,
        # берём последнюю цену.
        #
        # Такой fallback нужен для старых источников.
        # --------------------------------------------------------

        try:

            return float(
                rows[-1][1]
            )

        except Exception:

            return None

    # ============================================================
    # SAVE SIGNAL
    # ============================================================

    async def _save_signal(
        self,
        signal: Signal,
    ) -> Optional[int]:

        if self.database is None:
            return None

        try:

            # ----------------------------------------------------
            # Проверяем дубликат ДО отправки.
            # ----------------------------------------------------

            if hasattr(
                self.database,
                "signal_exists",
            ):

                exists = (
                    self.database.signal_exists(
                        pair=signal.pair,
                        direction=signal.direction,
                        entry_time=signal.entry_time.isoformat(),
                    )
                )

                if asyncio.iscoroutine(
                    exists
                ):

                    exists = await exists

                if exists:

                    logger.info(
                        "[SCHEDULER] "
                        "Signal already exists: "
                        "%s %s %s",
                        signal.pair,
                        signal.direction,
                        signal.entry_time,
                    )

                    return None

            # ----------------------------------------------------
            # Получаем 1m candles для определения
            # максимально близкой цены входа.
            # ----------------------------------------------------

            entry_price = None

            try:

                candles = await self.get_candles(
                    pair=signal.pair,
                    limit=120,
                    interval="1min",
                )

                if candles is not None:

                    entry_price = (
                        self._find_price_at_or_after(
                            candles,
                            signal.entry_time,
                        )
                    )

            except Exception as exc:

                logger.debug(
                    "[SCHEDULER] "
                    "entry price unavailable "
                    "%s: %s",
                    signal.pair,
                    exc,
                )

            # ----------------------------------------------------
            # Сохраняем.
            # ----------------------------------------------------

            expiry_minutes = getattr(
                signal,
                "expiry_minutes",
                None,
            )

            result = (
                self.database.save_signal(
                    pair=signal.pair,
                    direction=signal.direction,
                    quality=signal.quality,
                    probability=signal.probability,
                    entry_time=signal.entry_time.isoformat(),
                    expiry_time=signal.expiry_time.isoformat(),
                    expiry_minutes=expiry_minutes,
                    analysis_time=signal.analysis_time.isoformat(),
                    confirmations=signal.confirmations,
                    reasons=signal.reasons,
                    entry_price=entry_price,
                )
            )

            if asyncio.iscoroutine(
                result
            ):

                result = await result

            signal_id = (
                int(result)
                if result is not None
                else None
            )

            if signal_id:

                logger.info(
                    "[SCHEDULER] "
                    "Signal saved id=%s | "
                    "%s | %s | %sm",
                    signal_id,
                    signal.pair,
                    signal.direction,
                    expiry_minutes,
                )

            return signal_id

        except Exception as exc:

            logger.exception(
                "[SCHEDULER] "
                "DB save error: %s",
                exc,
            )

            return None

    # ============================================================
    # RESULT CALCULATION
    # ============================================================

    @staticmethod
    def _calculate_result(
        direction: str,
        entry_price: float,
        expiry_price: float,
    ) -> str:

        direction = (
            str(direction)
            .upper()
            .strip()
        )

        # Небольшая защита от float noise.
        epsilon = 1e-12

        if abs(
            expiry_price
            - entry_price
        ) <= epsilon:

            return "DRAW"

        if direction == "CALL":

            if expiry_price > entry_price:
                return "WIN"

            return "LOSS"

        if direction == "PUT":

            if expiry_price < entry_price:
                return "WIN"

            return "LOSS"

        # Неизвестное направление.
        return "EXPIRED"

    # ============================================================
    # CHECK ONE RESULT
    # ============================================================

    async def _check_signal_result(
        self,
        signal_row: dict,
    ) -> bool:

        if not signal_row:
            return False

        signal_id = signal_row.get(
            "id"
        )

        pair = signal_row.get(
            "pair"
        )

        direction = signal_row.get(
            "direction"
        )

        entry_time = self._parse_datetime(
            signal_row.get(
                "entry_time"
            )
        )

        expiry_time = self._parse_datetime(
            signal_row.get(
                "expiry_time"
            )
        )

        if not signal_id:
            return False

        if not pair:
            return False

        if not direction:
            return False

        if entry_time is None:
            return False

        if expiry_time is None:
            return False

        # --------------------------------------------------------
        # Если expiry ещё не наступил — ничего не делаем.
        # --------------------------------------------------------

        now = self._now()

        if now < expiry_time:

            return False

        try:

            # ----------------------------------------------------
            # Получаем актуальные 1m candles.
            # ----------------------------------------------------

            candles = await self.get_candles(
                pair=pair,
                limit=RESULT_CANDLE_LIMIT,
                interval="1min",
            )

            if candles is None:

                logger.info(
                    "[RESULT] "
                    "%s id=%s: candles unavailable",
                    pair,
                    signal_id,
                )

                return False

            # ----------------------------------------------------
            # Цена входа.
            #
            # Если она уже сохранена — используем её.
            # Иначе пытаемся найти историческую цену.
            # ----------------------------------------------------

            entry_price = signal_row.get(
                "entry_price"
            )

            try:

                if entry_price is not None:

                    entry_price = float(
                        entry_price
                    )

            except Exception:

                entry_price = None

            if entry_price is None:

                entry_price = (
                    self._find_price_at_or_after(
                        candles,
                        entry_time,
                    )
                )

            # ----------------------------------------------------
            # Цена закрытия.
            # ----------------------------------------------------

            expiry_price = (
                self._find_price_at_or_after(
                    candles,
                    expiry_time,
                )
            )

            if entry_price is None:

                logger.warning(
                    "[RESULT] "
                    "%s id=%s: "
                    "entry price unavailable",
                    pair,
                    signal_id,
                )

                return False

            if expiry_price is None:

                logger.info(
                    "[RESULT] "
                    "%s id=%s: "
                    "expiry price not available yet",
                    pair,
                    signal_id,
                )

                return False

            # ----------------------------------------------------
            # Рассчитываем результат.
            # ----------------------------------------------------

            result = (
                self._calculate_result(
                    direction=direction,
                    entry_price=entry_price,
                    expiry_price=expiry_price,
                )
            )

            # ----------------------------------------------------
            # Записываем результат.
            # ----------------------------------------------------

            if hasattr(
                self.database,
                "set_signal_result",
            ):

                saved = (
                    self.database.set_signal_result(
                        signal_id=int(
                            signal_id
                        ),
                        result=result,
                        expiry_price=expiry_price,
                    )
                )

                if asyncio.iscoroutine(
                    saved
                ):

                    saved = await saved

                if saved:

                    logger.info(
                        "[RESULT] "
                        "%s id=%s | "
                        "%s | "
                        "entry=%.8f | "
                        "expiry=%.8f",
                        pair,
                        signal_id,
                        result,
                        entry_price,
                        expiry_price,
                    )

                    # ------------------------------------------------
                    # Логируем статистику после закрытия.
                    # ------------------------------------------------

                    try:

                        if hasattr(
                            self.database,
                            "get_signal_statistics",
                        ):

                            stats = (
                                self.database
                                .get_signal_statistics(
                                    pair=pair,
                                    expiry_minutes=(
                                        signal_row.get(
                                            "expiry_minutes"
                                        )
                                    ),
                                )
                            )

                            logger.info(
                                "[STATS] "
                                "%s | %sm | "
                                "WINRATE=%.2f%% | "
                                "W=%s | L=%s | N=%s",
                                pair,
                                signal_row.get(
                                    "expiry_minutes"
                                ),
                                stats.get(
                                    "winrate",
                                    0,
                                ),
                                stats.get(
                                    "wins",
                                    0,
                                ),
                                stats.get(
                                    "losses",
                                    0,
                                ),
                                stats.get(
                                    "total",
                                    0,
                                ),
                            )

                    except Exception as exc:

                        logger.debug(
                            "[STATS] "
                            "statistics error: %s",
                            exc,
                        )

                    return True

            return False

        except Exception as exc:

            logger.exception(
                "[RESULT] "
                "%s id=%s check error: %s",
                pair,
                signal_id,
                exc,
            )

            return False

    # ============================================================
    # CHECK ALL RESULTS
    # ============================================================

    async def check_pending_results(
        self,
    ) -> int:

        if self.database is None:
            return 0

        if not hasattr(
            self.database,
            "get_pending_signals",
        ):

            return 0

        async with _RESULT_LOCK:

            try:

                before_time = (
                    self._now().isoformat()
                )

                rows = (
                    self.database.get_pending_signals(
                        before_time=before_time,
                        limit=MAX_RESULT_CHECK_BATCH,
                    )
                )

                if asyncio.iscoroutine(
                    rows
                ):

                    rows = await rows

                if not rows:
                    return 0

                completed = 0

                for row in rows:

                    try:

                        success = (
                            await self._check_signal_result(
                                row
                            )
                        )

                        if success:

                            completed += 1

                    except Exception as exc:

                        logger.exception(
                            "[RESULT] "
                            "signal check error: %s",
                            exc,
                        )

                if completed:

                    # ------------------------------------------------
                    # Старый backtest cache может уже устареть
                    # относительно накопленной статистики.
                    # ------------------------------------------------

                    self._backtest_cache.clear()

                    logger.info(
                        "[RESULT] "
                        "Закрыто сигналов: %s",
                        completed,
                    )

                return completed

            except Exception as exc:

                logger.exception(
                    "[RESULT] "
                    "pending results error: %s",
                    exc,
                )

                return 0

    # ============================================================
    # FORMAT
    # ============================================================

    @staticmethod
    def format_signal(
        signal: Signal,
    ) -> str:

        direction_text = {
            "CALL": "🟢 CALL / ВВЕРХ",
            "PUT": "🔴 PUT / ВНИЗ",
        }.get(
            signal.direction,
            signal.direction,
        )

        confirmations = (
            signal.confirmations[:8]
        )

        confirmation_text = "\n".join(
            f"• {item}"
            for item in confirmations
        )

        return (
            "🎯 <b>СИЛЬНЫЙ СИГНАЛ</b>\n\n"
            f"💱 <b>Пара:</b> "
            f"{signal.pair}\n"
            f"📈 <b>Направление:</b> "
            f"{direction_text}\n"
            f"⏱️ <b>Время сделки:</b> "
            f"{signal.expiry_minutes} мин.\n\n"
            f"⭐ <b>Quality:</b> "
            f"{signal.quality:.1f}/100\n"
            f"🎯 <b>Расчётная вероятность:</b> "
            f"{signal.probability:.1f}%\n\n"
            f"🟢 <b>Вход:</b> "
            f"{signal.entry_time.strftime('%H:%M:%S')}\n"
            f"🔴 <b>Закрытие:</b> "
            f"{signal.expiry_time.strftime('%H:%M:%S')}\n\n"
            "🔍 <b>Подтверждения:</b>\n"
            f"{confirmation_text or '• —'}\n\n"
            "⚠️ Расчётная вероятность "
            "является оценкой модели. "
            "Фактический WINRATE считается "
            "по завершённым сигналам."
        )

    # ============================================================
    # SEND
    # ============================================================

    async def _send_to_users(
        self,
        signal: Signal,
    ) -> None:

        if self.bot is None:
            return

        if self.database is None:
            return

        try:

            users = []

            if hasattr(
                self.database,
                "get_active_users",
            ):

                result = (
                    self.database.get_active_users()
                )

                if asyncio.iscoroutine(
                    result
                ):

                    result = await result

                users = result or []

            text = self.format_signal(
                signal
            )

            for user in users:

                user_id = None

                try:

                    if isinstance(
                        user,
                        int,
                    ):

                        user_id = user

                    elif isinstance(
                        user,
                        dict,
                    ):

                        user_id = user.get(
                            "user_id"
                        )

                    else:

                        user_id = getattr(
                            user,
                            "user_id",
                            None,
                        )

                    if not user_id:
                        continue

                    await self.bot.send_message(
                        user_id,
                        text,
                        parse_mode="HTML",
                    )

                except Exception as exc:

                    logger.warning(
                        "[SCHEDULER] "
                        "send error user=%s: %s",
                        user_id,
                        exc,
                    )

        except Exception as exc:

            logger.exception(
                "[SCHEDULER] "
                "send users error: %s",
                exc,
            )

    # ============================================================
    # SCAN ONCE
    # ============================================================

    async def scan_once(
        self,
        pair: Optional[str] = None,
        expiry_minutes=None,
    ) -> Optional[Signal]:

        if pair is None:
            pair = self.auto_pair

        if expiry_minutes is None:

            expiry_minutes = (
                self.auto_expiry_minutes
            )

        signal = await self._find_best_signal(
            requested_pair=pair,
            expiry_minutes=expiry_minutes,
        )

        if signal is None:

            logger.info(
                "[SCHEDULER] "
                "Сильный сигнал не найден."
            )

            return None

        signal_id = (
            await self._save_signal(
                signal
            )
        )

        # --------------------------------------------------------
        # Если сигнал уже существует, повторно его не отправляем.
        # --------------------------------------------------------

        if signal_id is None:

            return None

        await self._send_to_users(
            signal
        )

        return signal

    # ============================================================
    # RESULT LOOP
    # ============================================================

    async def _result_loop(
        self,
    ) -> None:

        logger.info(
            "[RESULT] "
            "Automatic result checker started"
        )

        while self.running:

            try:

                await self.check_pending_results()

                await asyncio.sleep(
                    RESULT_CHECK_INTERVAL_SECONDS
                )

            except asyncio.CancelledError:

                break

            except Exception as exc:

                logger.exception(
                    "[RESULT] "
                    "result loop error: %s",
                    exc,
                )

                await asyncio.sleep(
                    RESULT_CHECK_INTERVAL_SECONDS
                )

        logger.info(
            "[RESULT] "
            "Automatic result checker stopped"
        )

    # ============================================================
    # RUN
    # ============================================================

    async def run(self) -> None:

        if self.running:
            return

        self.running = True

        logger.info(
            "[SCHEDULER] "
            "Automatic scheduler started"
        )

        # --------------------------------------------------------
        # Отдельный цикл проверки результатов.
        # --------------------------------------------------------

        self.result_task = asyncio.create_task(
            self._result_loop()
        )

        try:

            while self.running:

                try:

                    now = self._now()

                    next_run = (
                        self._next_minute(now)
                    )

                    delay = (
                        next_run - now
                    ).total_seconds()

                    if delay > 0:

                        await asyncio.sleep(
                            delay
                        )

                    if not self.running:
                        break

                    # ------------------------------------------------
                    # Сначала закрываем старые сделки.
                    # ------------------------------------------------

                    await self.check_pending_results()

                    # ------------------------------------------------
                    # Затем ищем новые.
                    # ------------------------------------------------

                    await self.scan_once()

                except asyncio.CancelledError:

                    break

                except Exception as exc:

                    logger.exception(
                        "[SCHEDULER] "
                        "scan loop error: %s",
                        exc,
                    )

                    await asyncio.sleep(
                        RATE_LIMIT_COOLDOWN_SECONDS
                    )

        finally:

            self.running = False

            if self.result_task is not None:

                self.result_task.cancel()

                try:

                    await self.result_task

                except asyncio.CancelledError:
                    pass

                self.result_task = None

        logger.info(
            "[SCHEDULER] "
            "Automatic scheduler stopped"
        )

    # ============================================================
    # START
    # ============================================================

    def start(self) -> None:

        if self.task is not None:
            return

        self.task = asyncio.create_task(
            self.run()
        )

    # ============================================================
    # STOP
    # ============================================================

    async def stop(self) -> None:

        self.running = False

        if self.task is not None:

            self.task.cancel()

            try:

                await self.task

            except asyncio.CancelledError:
                pass

            self.task = None

        if self.result_task is not None:

            self.result_task.cancel()

            try:

                await self.result_task

            except asyncio.CancelledError:
                pass

            self.result_task = None

        try:

            await market_client.close()

        except Exception:
            pass


# ================================================================
# GLOBAL SCHEDULER
# ================================================================

scheduler = SignalScheduler()
