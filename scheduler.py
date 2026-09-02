import asyncio
import logging
from datetime import datetime

from config import (
    ADMIN_ID,
    AUTO_SCAN_SECONDS,
    PAIRS,
    TIMEZONE,
)
from database import db
from market import market_client
from signal_engine import Signal, SignalEngine

logger = logging.getLogger(__name__)


class SignalScheduler:
    def __init__(self, bot):
        self.bot = bot
        self.engine = SignalEngine()
        self.analysis_lock = asyncio.Lock()

    async def find_best_signal(self) -> Signal | None:
        async with self.analysis_lock:

            async def analyze_pair(pair: str):
                candles = await market_client.get_candles(
                    pair
                )

                if candles is None:
                    return None

                try:
                    return self.engine.analyze(
                        pair,
                        candles,
                    )
                except Exception:
                    logger.exception(
                        "Ошибка анализа %s",
                        pair,
                    )
                    return None

            results = await asyncio.gather(
                *[
                    analyze_pair(pair)
                    for pair in PAIRS
                ],
                return_exceptions=True,
            )

            signals = []

            for result in results:
                if isinstance(result, Signal):
                    signals.append(result)

            if not signals:
                return None

            signals.sort(
                key=lambda signal: signal.quality,
                reverse=True,
            )

            return signals[0]

    @staticmethod
    def format_signal(signal: Signal) -> str:
        entry_msk = signal.entry_time.astimezone(
            TIMEZONE
        )

        expiry_msk = signal.expiry_time.astimezone(
            TIMEZONE
        )

        if signal.direction == "CALL":
            direction = "🟢 CALL ↑"
        else:
            direction = "🔴 PUT ↓"

        confirmations = "\n".join(
            f"• {item}"
            for item in signal.confirmations[:6]
        )

        return (
            "🚨 <b>НОВЫЙ СИГНАЛ</b>\n\n"
            f"<b>{direction}</b>\n\n"
            f"💱 Пара: <b>{signal.pair}</b>\n"
            f"⏰ ВХОД: <b>{entry_msk:%H:%M} МСК</b>\n"
            f"🎯 ЭКСПИРАЦИЯ: "
            f"<b>{expiry_msk:%H:%M} МСК</b>\n"
            f"📊 QUALITY: "
            f"<b>{signal.quality:.0f}/100</b>\n\n"
            "🔎 <b>Подтверждения:</b>\n"
            f"{confirmations}\n\n"
            "⚠️ Сигнал является аналитическим "
            "прогнозом, а не гарантией результата."
        )

    async def save_signal(
        self,
        signal: Signal,
    ) -> int | None:

        entry_iso = signal.entry_time.isoformat()

        if db.signal_exists(
            signal.pair,
            signal.direction,
            entry_iso,
        ):
            return None

        signal_id = db.save_signal(
            pair=signal.pair,
            direction=signal.direction,
            quality=signal.quality,
            entry_time=entry_iso,
            expiry_time=signal.expiry_time.isoformat(),
            analysis_time=signal.analysis_time.isoformat(),
            confirmations=signal.confirmations,
            reasons=signal.reasons,
        )

        return signal_id

    async def send_to_user(
        self,
        user_id: int,
        signal: Signal,
    ):
        try:
            await self.bot.send_message(
                user_id,
                self.format_signal(signal),
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning(
                "Не удалось отправить сигнал %s: %s",
                user_id,
                exc,
            )

    async def send_automatic_signal(
        self,
        signal: Signal,
    ):
        signal_id = await self.save_signal(
            signal
        )

        if signal_id is None:
            logger.info(
                "Сигнал уже существует: %s %s %s",
                signal.pair,
                signal.direction,
                signal.entry_time,
            )
            return

        users = db.get_approved_users()

        if ADMIN_ID and ADMIN_ID not in users:
            users.append(ADMIN_ID)

        if not users:
            logger.info(
                "Нет пользователей для отправки сигнала"
            )
            return

        await asyncio.gather(
            *[
                self.send_to_user(
                    user_id,
                    signal,
                )
                for user_id in users
            ],
            return_exceptions=True,
        )

        logger.info(
            "Сигнал #%s отправлен: %s %s %.0f",
            signal_id,
            signal.pair,
            signal.direction,
            signal.quality,
        )

    async def run(self):
        logger.info(
            "Автоматический сканер запущен"
        )

        while True:
            try:
                signal = await self.find_best_signal()

                if signal:
                    await self.send_automatic_signal(
                        signal
                    )
                else:
                    logger.info(
                        "Сильного сигнала сейчас нет"
                    )

            except asyncio.CancelledError:
                raise

            except Exception:
                logger.exception(
                    "Ошибка scheduler"
                )

            await asyncio.sleep(
                AUTO_SCAN_SECONDS
            )


scheduler_instance = None
