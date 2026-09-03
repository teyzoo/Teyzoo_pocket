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
from signal_engine import Signal, SignalEngine


logger = logging.getLogger(__name__)


MAX_PAIRS_PER_SCAN = 6
REQUEST_DELAY_SECONDS = 2.0
RATE_LIMIT_COOLDOWN_SECONDS = 65

MIN_EXPIRY_MINUTES = 1
MAX_EXPIRY_MINUTES = 20
DEFAULT_EXPIRY_MINUTES = 5

ANY_EXPIRY = "any"


class SignalScheduler:
    """
    Планировщик автоматических и ручных сигналов.

    Поддерживает:

        1..20 минут
        Любое время

    Автоматические сигналы:
        работают без запроса пользователя.

    Ручные сигналы:
        get_manual_signal(...)

    Важно:
        probability является расчётной оценкой модели,
        а не гарантированной вероятностью выигрыша.
    """

    def __init__(
        self,
        bot=None,
        database=None,
    ) -> None:

        self.bot = bot
        self.database = database

        self.engine = SignalEngine(
            min_quality=MIN_QUALITY,
            min_probability=MIN_PROBABILITY,
        )

        self.running = False
        self.task: asyncio.Task | None = None

        self.auto_expiry_minutes = (
            DEFAULT_EXPIRY_MINUTES
        )

        self.auto_pair: Optional[str] = None

        self._last_request_time = 0.0

        self._cache: dict[
            tuple[str, int],
            Signal | None,
        ] = {}

        self._cache_time: dict[
            tuple[str, int],
            datetime,
        ] = {}

        self._signal_lock = asyncio.Lock()

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

            value = expiry_minutes.strip().lower()

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
                expiry_minutes = int(value)

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

        normalized = self.normalize_expiry(
            expiry_minutes
        )

        self.auto_expiry_minutes = normalized

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

        dt = dt.astimezone(
            TIMEZONE
        )

        result = (
            dt.replace(
                second=0,
                microsecond=0,
            )
            + timedelta(minutes=1)
        )

        return result

    # ============================================================
    # PAIRS
    # ============================================================

    def _select_pairs_for_scan(
        self,
        requested_pair: Optional[str] = None,
    ) -> list[str]:

        if requested_pair:

            if requested_pair in {
                "any",
                "any_regular",
                "any_otc",
                "all",
            }:

                requested_pair = None

            else:

                return [
                    requested_pair
                ]

        pairs = list(
            PAIRS
        )

        return pairs[
            :MAX_PAIRS_PER_SCAN
        ]

    # ============================================================
    # RATE LIMIT
    # ============================================================

    async def _respect_rate_limit(
        self,
    ) -> None:

        now = asyncio.get_running_loop().time()

        elapsed = (
            now
            - self._last_request_time
        )

        if elapsed < REQUEST_DELAY_SECONDS:

            await asyncio.sleep(
                REQUEST_DELAY_SECONDS
                - elapsed
            )

        self._last_request_time = (
            asyncio.get_running_loop().time()
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
            pair,
            limit=220,
            interval="1min",
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
                "[SCHEDULER] %s: "
                "candles unavailable",
                pair,
            )

            return None

        # --------------------------------------------------------
        # конкретное время
        # --------------------------------------------------------

        if normalized_expiry is not None:

            signal = (
                self.engine.analyze(
                    pair=pair,
                    candles=candles,
                    expiry_minutes=normalized_expiry,
                )
            )

            if signal:

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

        # --------------------------------------------------------
        # ЛЮБОЕ ВРЕМЯ
        # --------------------------------------------------------

        signal = (
            self.engine.choose_best_expiry(
                pair=pair,
                candles=candles,
            )
        )

        if signal:

            logger.info(
                "[SCHEDULER] "
                "%s | ANY | selected=%sm | "
                "%s | quality=%.1f | "
                "probability=%.1f",
                pair,
                signal.expiry_minutes,
                signal.direction,
                signal.quality,
                signal.probability,
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

        signals: list[Signal] = []

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
                    "analysis error %s: %s",
                    pair,
                    exc,
                )

        if not signals:
            return None

        signals.sort(
            key=lambda signal: (
                float(signal.probability),
                float(signal.quality),
                len(signal.confirmations),
                -int(signal.expiry_minutes),
            ),
            reverse=True,
        )

        return signals[0]

    # ============================================================
    # MANUAL SIGNAL
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

            signal = await self._find_best_signal(
                requested_pair=pair,
                expiry_minutes=normalized_expiry,
            )

        return signal

    # ============================================================
    # DATABASE
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

                result = self.database.save_signal(
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

                if asyncio.iscoroutine(result):
                    await result

        except Exception as exc:

            logger.exception(
                "[SCHEDULER] "
                "database save error: %s",
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

        confirmations = signal.confirmations[
            :8
        ]

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
            "⚠️ Расчётная вероятность — "
            "оценка модели, а не гарантия "
            "результата."
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

                if asyncio.iscoroutine(result):
                    result = await result

                users = result or []

            text = self.format_signal(
                signal
            )

            for user in users:

                try:

                    user_id = (
                        user
                        if isinstance(
                            user,
                            int,
                        )
                        else getattr(
                            user,
                            "user_id",
                            None,
                        )
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

                # Работаем на каждой новой минуте.
                # Это необходимо для поддержки
                # экспираций 1..20 минут.
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
