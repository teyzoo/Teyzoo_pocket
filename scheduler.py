import asyncio
import logging
from typing import Optional

from aiogram import Bot

from config import (
    AUTO_SIGNALS_ENABLED,
    DEFAULT_PAIRS,
    EXPIRY_MINUTES,
    MIN_QUALITY,
    TIMEZONE,
)
from database import Database
from market import MarketClient
from signal_engine import Signal, SignalEngine


logger = logging.getLogger(__name__)


class SignalScheduler:

    def __init__(
        self,
        bot: Bot,
        database: Database,
        market: MarketClient,
        engine: SignalEngine,
    ):
        self.bot = bot
        self.database = database
        self.market = market
        self.engine = engine

        self.running = False


    async def find_best_signal(
        self,
    ) -> Optional[Signal]:

        best_signal = None

        for pair in DEFAULT_PAIRS:

            try:
                candles = await self.market.get_candles(
                    pair
                )

                if candles is None:
                    continue

                signal = self.engine.analyze(
                    pair,
                    candles,
                )

                if signal is None:
                    continue

                if (
                    best_signal is None
                    or signal.quality
                    > best_signal.quality
                ):
                    best_signal = signal

            except Exception:
                logger.exception(
                    "Ошибка анализа %s",
                    pair,
                )

        return best_signal


    async def run(
        self,
    ) -> None:

        if not AUTO_SIGNALS_ENABLED:
            logger.info(
                "Автоматические сигналы отключены."
            )
            return

        if self.running:
            return

        self.running = True

        logger.info(
            "Signal scheduler started."
        )

        try:

            while True:

                signal = (
                    await self.find_best_signal()
                )

                if signal is not None:

                    await self.send_signal(
                        signal
                    )

                else:

                    logger.info(
                        "Сильный сигнал не найден."
                    )

                # Проверяем рынок каждые 60 секунд.
                await asyncio.sleep(60)

        except asyncio.CancelledError:

            logger.info(
                "Signal scheduler stopped."
            )

            raise

        except Exception:

            logger.exception(
                "Scheduler crashed."
            )

            await asyncio.sleep(10)

        finally:
            self.running = False


    async def send_signal(
        self,
        signal: Signal,
    ) -> None:

        confirmations_text = []

        for name, direction in (
            signal.confirmations.items()
        ):

            if direction == "CALL":
                mark = "🟢"
            elif direction == "PUT":
                mark = "🔴"
            else:
                mark = "⚪"

            confirmations_text.append(
                f"{mark} {name}"
            )

        confirmations = "\n".join(
            confirmations_text
        )

        direction_text = (
            "🟢 CALL ↑"
            if signal.direction == "CALL"
            else "🔴 PUT ↓"
        )

        message = (
            f"<b>{direction_text}</b>\n\n"
            f"💱 <b>{signal.pair}</b>\n\n"
            f"⏰ <b>ВХОД:</b> "
            f"{signal.entry_time.strftime('%H:%M')} МСК\n"
            f"🎯 <b>ЭКСПИРАЦИЯ:</b> "
            f"{signal.expiry_time.strftime('%H:%M')} МСК\n\n"
            f"📊 <b>QUALITY:</b> "
            f"{signal.quality}/100\n\n"
            f"<b>Подтверждения:</b>\n"
            f"{confirmations}\n\n"
            f"⚠️ Сигнал не является гарантией "
            f"прибыльной сделки."
        )

        self.database.save_signal(
            pair=signal.pair,
            direction=signal.direction,
            entry_time=signal.entry_time.isoformat(),
            expiry_time=signal.expiry_time.isoformat(),
            quality=signal.quality,
            score_details=message,
        )

        # Пока рассылаем только одобренным пользователям.
        # Список берём из БД.
        await self._send_to_approved_users(
            message
        )


    async def _send_to_approved_users(
        self,
        message: str,
    ) -> None:

        # SQLite-запрос здесь сделаем через
        # отдельный метод ниже.
        users = self._get_approved_users()

        for user_id in users:

            try:

                await self.bot.send_message(
                    user_id,
                    message,
                )

            except Exception:

                logger.exception(
                    "Не удалось отправить сигнал %s",
                    user_id,
                )


    def _get_approved_users(
        self,
    ) -> list[int]:

        with self.database.lock:

            cursor = (
                self.database.connection.cursor()
            )

            cursor.execute(
                """
                SELECT user_id
                FROM users
                WHERE status = 'approved'
                """
            )

            rows = cursor.fetchall()

        return [
            int(row["user_id"])
            for row in rows
        ]
