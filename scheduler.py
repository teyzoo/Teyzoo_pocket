from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from config import (
    MIN_PROBABILITY,
    MIN_QUALITY,
    PAIRS,
    TIMEZONE,
)
from market import market_client
from signal_engine import Signal

from backtest import (
    DEFAULT_TEST_POINTS,
    ExpiryBacktester,
)


logger = logging.getLogger(__name__)


MAX_PAIRS_PER_SCAN = 6

REQUEST_DELAY_SECONDS = 2.0
RATE_LIMIT_COOLDOWN_SECONDS = 65

MIN_EXPIRY_MINUTES = 1
MAX_EXPIRY_MINUTES = 20

DEFAULT_EXPIRY_MINUTES = 5

ANY_EXPIRY = "any"

# Сколько минут держим кэш backtest.
BACKTEST_CACHE_SECONDS = 300

# Минимальное количество исторических сделок
# для выбора экспирации.
MIN_BACKTEST_TRADES = 10


class SignalScheduler:
    """
    Планировщик автоматических и ручных сигналов.

    Поддерживает:

        1..20 минут
        Любое время

    При выборе конкретной экспирации:

        анализируется именно выбранный срок.

    При выборе "Любое время":

        выполняется исторический backtest
        для 1..20 минут,

        после чего выбирается экспирация
        с лучшим фактическим историческим WINRATE.

    ВАЖНО:

        Исторический WINRATE не является гарантией
        будущего результата.
    """

    def __init__(
        self,
        bot=None,
        database=None,
    ) -> None:

        self.bot = bot
        self.database = database

        from signal_engine import SignalEngine

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
    ):

        await self._respect_rate_limit()

        return await market_client.get_candles(
            pair=pair,
            limit=220,
            interval="1min",
        )

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

        import time

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
            "%s: запускаю "
            "историческую проверку 1-20m",
            pair,
        )

        # Backtest тяжёлый, поэтому отдаём
        # выполнение CPU-коду в отдельный поток.
        best_expiry, results = (
            await asyncio.to_thread(
                self.backtester.choose_best_expiry,
                pair,
                candles,
                DEFAULT_TEST_POINTS,
            )
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
            pair
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
        # Основной выбор:
        #
        # 1. Расчётная вероятность
        # 2. Quality
        # 3. Количество подтверждений
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
    # SAVE
    # ============================================================

    async def _save_signal(
        self,
        signal: Signal,
    ) -> None:

        if self.database is None:
            return

        try:

            if hasattr(
                self.database,
                "save_signal",
            ):

                result = (
                    self.database.save_signal(
                        pair=signal.pair,
                        direction=signal.direction,
                        quality=signal.quality,
                        probability=signal.probability,
                        entry_time=signal.entry_time,
                        expiry_time=signal.expiry_time,
                        analysis_time=signal.analysis_time,
                        confirmations=signal.confirmations,
                        reasons=signal.reasons,
                    )
                )

                if asyncio.iscoroutine(
                    result
                ):

                    await result

        except Exception as exc:

            logger.exception(
                "[SCHEDULER] "
                "DB save error: %s",
                exc,
            )

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
            "Исторический WINRATE не "
            "гарантирует будущий результат."
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

        await self._save_signal(
            signal
        )

        await self._send_to_users(
            signal
        )

        return signal

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

                await self.scan_once()

            except asyncio.CancelledError:

                break

            except Exception as exc:

                logger.exception(
                    "[SCHEDULER] "
                    "run error: %s",
                    exc,
                )

                await asyncio.sleep(
                    RATE_LIMIT_COOLDOWN_SECONDS
                )

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

        try:

            await market_client.close()

        except Exception:
            pass


# ================================================================
# GLOBAL SCHEDULER
# ================================================================

scheduler = SignalScheduler()
