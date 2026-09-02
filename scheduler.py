from __future__ import annotations

import asyncio
from datetime import datetime

from config import (
    ADMIN_ID,
    AUTO_SCAN_SECONDS,
    MIN_PROBABILITY,
    PAIRS,
    TIMEZONE,
)

from market import market_client
from signal_engine import SignalEngine, Signal

from database import db


class SignalScheduler:

    def __init__(self, bot):
        self.bot = bot
        self.engine = SignalEngine()

        self.analysis_lock = asyncio.Lock()

    async def _analyze_pair(self, pair: str) -> Signal | None:
        try:
            candles = await market_client.get_candles(pair)

            if candles is None:
                return None

            return self.engine.analyze(
                pair=pair,
                candles=candles,
            )

        except Exception as exc:
            print(
                f"[SIGNAL] Ошибка анализа {pair}: {exc}"
            )
            return None

    async def find_best_signal(
        self,
        pair: str | None = None,
    ) -> Signal | None:

        async with self.analysis_lock:

            if pair:
                return await self._analyze_pair(pair)

            tasks = [
                self._analyze_pair(current_pair)
                for current_pair in PAIRS
            ]

            results = await asyncio.gather(
                *tasks,
                return_exceptions=False,
            )

            valid_signals = [
                signal
                for signal in results
                if signal is not None
            ]

            if not valid_signals:
                return None

            # Сначала качество, затем вероятность.
            valid_signals.sort(
                key=lambda signal: (
                    signal.quality,
                    signal.probability,
                ),
                reverse=True,
            )

            return valid_signals[0]

    @staticmethod
    def format_signal(signal: Signal) -> str:
        direction_emoji = (
            "🟢"
            if signal.direction == "CALL"
            else "🔴"
        )

        direction_arrow = (
            "↑"
            if signal.direction == "CALL"
            else "↓"
        )

        entry_moscow = signal.entry_time.astimezone(
            TIMEZONE
        )

        expiry_moscow = signal.expiry_time.astimezone(
            TIMEZONE
        )

        confirmations_text = "\n".join(
            f"✅ {item}"
            for item in signal.confirmations[:8]
        )

        return (
            f"{direction_emoji} {signal.direction} {direction_arrow}\n\n"
            f"💱 {signal.pair}\n\n"
            f"⏰ ВХОД: {entry_moscow:%H:%M} МСК\n"
            f"🎯 ЭКСПИРАЦИЯ: {expiry_moscow:%H:%M} МСК\n\n"
            f"📊 QUALITY: {signal.quality}/100\n"
            f"📈 ШАНС: {signal.probability:.0f}%\n\n"
            f"{confirmations_text}"
        )

    async def save_signal(
        self,
        signal: Signal,
    ) -> bool:

        if db.signal_exists(
            pair=signal.pair,
            direction=signal.direction,
            entry_time=signal.entry_time.isoformat(),
        ):
            return False

        db.save_signal(
            pair=signal.pair,
            direction=signal.direction,
            quality=signal.quality,
            entry_time=signal.entry_time.isoformat(),
            expiry_time=signal.expiry_time.isoformat(),
            analysis_time=signal.analysis_time.isoformat(),
            confirmations="\n".join(signal.confirmations),
            reasons="\n".join(signal.reasons),
        )

        return True

    async def send_signal_to_users(
        self,
        signal: Signal,
    ):
        text = self.format_signal(signal)

        users = db.get_approved_users()

        sent_to = set()

        for user in users:
            user_id = int(user["user_id"])

            if user_id in sent_to:
                continue

            try:
                await self.bot.send_message(
                    chat_id=user_id,
                    text=text,
                )

                sent_to.add(user_id)

            except Exception as exc:
                print(
                    f"[SIGNAL] Не удалось отправить "
                    f"{user_id}: {exc}"
                )

        # Админ тоже получает сигнал.
        if ADMIN_ID and ADMIN_ID not in sent_to:
            try:
                await self.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=text,
                )
            except Exception as exc:
                print(
                    f"[SIGNAL] Ошибка отправки админу: {exc}"
                )

    async def scan_once(self):
        signal = await self.find_best_signal()

        if signal is None:
            print(
                "[SIGNAL] Сильного сигнала нет."
            )
            return None

        saved = await self.save_signal(signal)

        if saved:
            await self.send_signal_to_users(signal)

        return signal

    async def get_manual_signal(
        self,
        pair: str | None = None,
    ) -> Signal | None:

        return await self.find_best_signal(pair=pair)

    async def run(self):
        print("[SCHEDULER] Автоматический анализ запущен.")

        while True:
            try:
                await self.scan_once()

            except asyncio.CancelledError:
                print(
                    "[SCHEDULER] Остановка."
                )
                raise

            except Exception as exc:
                print(
                    f"[SCHEDULER] Ошибка: {exc}"
                )

            await asyncio.sleep(
                AUTO_SCAN_SECONDS
            )


scheduler_instance = None
