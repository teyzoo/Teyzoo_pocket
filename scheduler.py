from __future__ import annotations

import asyncio
import traceback
from datetime import datetime, timezone
from typing import Optional

from config import (
    AUTO_SCAN_SECONDS,
    MIN_PROBABILITY,
    TIMEZONE,
)

from database import db
from market import market_client
from signal_engine import Signal, SignalEngine


class SignalScheduler:
    """
    Автоматический и ручной поиск сигналов.

    Совместим с текущим main.py:

        scheduler = SignalScheduler(bot)

    и:

        scheduler.run()

    Список пар получается динамически.
    """

    def __init__(self, bot=None):
        # Бот сохраняем внутри scheduler.
        # Это нужно потому, что main.py запускает:
        #
        # scheduler.run()
        #
        # без передачи bot.

        self.bot = bot

        self.engine = SignalEngine(
            min_probability=MIN_PROBABILITY,
        )

        self._running = False

        self._lock = asyncio.Lock()

        self._available_pairs: list[str] = []

    # ============================================================
    # BOT
    # ============================================================

    def set_bot(self, bot) -> None:
        """
        Позволяет установить/обновить Telegram Bot.
        """

        self.bot = bot

    # ============================================================
    # TIME
    # ============================================================

    @staticmethod
    def _to_moscow(
        dt: datetime,
    ) -> datetime:

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt.astimezone(
            TIMEZONE
        )

    # ============================================================
    # DISCOVER PAIRS
    # ============================================================

    async def refresh_pairs(
        self,
    ) -> list[str]:

        print("")
        print("=" * 70)
        print(
            "🌐 REFRESHING AVAILABLE POCKET OPTION PAIRS"
        )
        print("=" * 70)

        try:
            pairs = await (
                market_client.get_available_pairs()
            )

        except Exception as exc:

            print(
                "❌ Ошибка получения списка пар:"
            )

            print(
                f"   {type(exc).__name__}: {exc}"
            )

            traceback.print_exc()

            return self._available_pairs.copy()

        if pairs:

            self._available_pairs = list(
                dict.fromkeys(
                    pair.upper().strip()
                    for pair in pairs
                    if pair
                )
            )

        print("")

        print(
            f"📋 AVAILABLE PAIRS: "
            f"{len(self._available_pairs)}"
        )

        if self._available_pairs:

            print(
                "   "
                + ", ".join(
                    self._available_pairs
                )
            )

        print("=" * 70)

        return self._available_pairs.copy()

    # ============================================================
    # ANALYZE ONE PAIR
    # ============================================================

    async def _analyze_pair(
        self,
        pair: str,
    ) -> Optional[Signal]:

        print("")

        print(
            f"🔍 Проверка пары: {pair}"
        )

        try:

            candles = await (
                market_client.get_candles(
                    pair
                )
            )

        except Exception as exc:

            print(
                f"❌ {pair}: "
                "ошибка получения свечей"
            )

            print(
                f"   {type(exc).__name__}: "
                f"{exc}"
            )

            return None

        if candles is None:

            print(
                f"⚪ {pair}: "
                "нет доступных свечей"
            )

            return None

        print(
            f"📥 {pair}: "
            f"{len(candles)} candles"
        )

        try:

            signal = self.engine.analyze(
                pair=pair,
                candles=candles,
            )

        except Exception as exc:

            print(
                f"💥 {pair}: "
                "SignalEngine exception"
            )

            print(
                f"   {type(exc).__name__}: "
                f"{exc}"
            )

            traceback.print_exc()

            return None

        if signal is None:

            print(
                f"⚪ {pair}: "
                "NO VALID SIGNAL"
            )

            return None

        print(
            f"🟢 {pair}: "
            f"{signal.direction} | "
            f"Q={signal.quality:.1f} | "
            f"P={signal.probability:.1f}%"
        )

        return signal

    # ============================================================
    # FIND BEST SIGNAL
    # ============================================================

    async def find_best_signal(
        self,
        pair: Optional[str] = None,
    ) -> Optional[Signal]:

        async with self._lock:

            # ====================================================
            # SPECIFIC PAIR
            # ====================================================

            if pair is not None:

                pair = pair.strip().upper()

                print("")

                print(
                    "#" * 70
                )

                print(
                    f"🎯 MANUAL ANALYSIS: {pair}"
                )

                print(
                    "#" * 70
                )

                signal = await (
                    self._analyze_pair(
                        pair
                    )
                )

                if signal is None:

                    print(
                        f"⚪ {pair}: "
                        "сильного сигнала нет."
                    )

                return signal

            # ====================================================
            # ALL AVAILABLE PAIRS
            # ====================================================

            pairs = await (
                self.refresh_pairs()
            )

            if not pairs:

                print(
                    "❌ Нет доступных пар."
                )

                return None

            print("")

            print(
                "#" * 70
            )

            print(
                "🔀 ANALYZING ALL AVAILABLE PAIRS"
            )

            print(
                f"📋 Количество: {len(pairs)}"
            )

            print(
                f"📈 Minimum chance: "
                f"{MIN_PROBABILITY}%"
            )

            print(
                "#" * 70
            )

            # ====================================================
            # CONCURRENT ANALYSIS
            # ====================================================

            semaphore = asyncio.Semaphore(5)

            async def analyze_limited(
                current_pair: str,
            ):

                async with semaphore:

                    return await (
                        self._analyze_pair(
                            current_pair
                        )
                    )

            results = await asyncio.gather(
                *[
                    analyze_limited(
                        current_pair
                    )
                    for current_pair in pairs
                ],
                return_exceptions=True,
            )

            valid_signals: list[Signal] = []

            print("")

            print(
                "-" * 70
            )

            print(
                "📋 FINAL ANALYSIS SUMMARY"
            )

            print(
                "-" * 70
            )

            for current_pair, result in zip(
                pairs,
                results,
            ):

                if isinstance(
                    result,
                    Exception,
                ):

                    print(
                        f"💥 {current_pair}: "
                        f"{type(result).__name__}: "
                        f"{result}"
                    )

                    continue

                if result is None:

                    print(
                        f"⚪ {current_pair}: "
                        "NO SIGNAL"
                    )

                    continue

                valid_signals.append(
                    result
                )

                print(
                    f"🟢 {current_pair}: "
                    f"{result.direction} | "
                    f"Q={result.quality:.1f} | "
                    f"P={result.probability:.1f}%"
                )

            print(
                "-" * 70
            )

            # ====================================================
            # NO SIGNAL
            # ====================================================

            if not valid_signals:

                print(
                    "⚪ NO VALID SIGNALS"
                )

                print(
                    f"📈 Минимальный "
                    f"расчётный шанс: "
                    f"{MIN_PROBABILITY}%"
                )

                print(
                    "-" * 70
                )

                return None

            # ====================================================
            # BEST SIGNAL
            # ====================================================

            best_signal = max(
                valid_signals,
                key=lambda signal: (
                    signal.probability,
                    signal.quality,
                    len(
                        signal.confirmations
                    ),
                ),
            )

            print("")

            print(
                "🏆 BEST AVAILABLE SIGNAL"
            )

            print(
                f"💱 {best_signal.pair}"
            )

            print(
                f"🧭 {best_signal.direction}"
            )

            print(
                f"📊 Quality: "
                f"{best_signal.quality:.1f}/100"
            )

            print(
                f"📈 Chance: "
                f"{best_signal.probability:.1f}%"
            )

            print(
                f"✅ Confirmations: "
                f"{len(best_signal.confirmations)}"
            )

            print(
                "#" * 70
            )

            return best_signal

    # ============================================================
    # FORMAT SIGNAL
    # ============================================================

    def format_signal(
        self,
        signal: Signal,
    ) -> str:

        if signal.direction == "CALL":

            direction_text = (
                "🟢 CALL ↑"
            )

        else:

            direction_text = (
                "🔴 PUT ↓"
            )

        entry = self._to_moscow(
            signal.entry_time
        )

        expiry = self._to_moscow(
            signal.expiry_time
        )

        lines = [
            direction_text,
            "",
            f"💱 {signal.pair}",
            "",
            (
                "⏰ ВХОД: "
                f"{entry.strftime('%H:%M')} МСК"
            ),
            (
                "🎯 ЭКСПИРАЦИЯ: "
                f"{expiry.strftime('%H:%M')} МСК"
            ),
            "",
            (
                f"📊 QUALITY: "
                f"{signal.quality:.0f}/100"
            ),
            (
                f"📈 ШАНС: "
                f"{signal.probability:.0f}%"
            ),
        ]

        if signal.confirmations:

            lines.extend(
                [
                    "",
                    "✅ ПОДТВЕРЖДЕНИЯ:",
                ]
            )

            for confirmation in (
                signal.confirmations
            ):

                lines.append(
                    f"• {confirmation}"
                )

        return "\n".join(lines)

    # ============================================================
    # SAVE SIGNAL
    # ============================================================

    def save_signal(
        self,
        signal: Signal,
    ) -> Optional[int]:

        entry_time = (
            signal.entry_time.isoformat()
        )

        if db.signal_exists(
            pair=signal.pair,
            direction=signal.direction,
            entry_time=entry_time,
        ):

            print(
                f"⚠️ Дубликат сигнала: "
                f"{signal.pair} "
                f"{signal.direction}"
            )

            return None

        signal_id = db.save_signal(
            pair=signal.pair,
            direction=signal.direction,
            quality=signal.quality,
            entry_time=entry_time,
            expiry_time=(
                signal.expiry_time.isoformat()
            ),
            analysis_time=(
                signal.analysis_time.isoformat()
            ),
            confirmations="\n".join(
                signal.confirmations
            ),
            reasons="\n".join(
                signal.reasons
            ),
        )

        print(
            f"💾 Signal saved: "
            f"id={signal_id}"
        )

        return signal_id

    # ============================================================
    # SEND SIGNAL
    # ============================================================

    async def send_signal_to_users(
        self,
        bot,
        signal: Signal,
    ) -> int:

        if bot is None:

            print(
                "⚠️ Telegram Bot не передан."
            )

            return 0

        users = db.get_approved_users()

        if not users:

            print(
                "ℹ️ APPROVED пользователей нет."
            )

            return 0

        message = self.format_signal(
            signal
        )

        sent = 0

        for user in users:

            user_id = user.get(
                "user_id"
            )

            if not user_id:
                continue

            try:

                await bot.send_message(
                    chat_id=int(user_id),
                    text=message,
                )

                sent += 1

            except Exception as exc:

                print(
                    f"❌ Ошибка отправки "
                    f"{user_id}: "
                    f"{type(exc).__name__}: "
                    f"{exc}"
                )

        print(
            f"📨 Sent: "
            f"{sent}/{len(users)}"
        )

        return sent

    # ============================================================
    # AUTO SCAN
    # ============================================================

    async def scan_once(
        self,
        bot=None,
    ) -> Optional[Signal]:

        # Если bot передали непосредственно в scan_once,
        # сохраняем его.

        if bot is not None:
            self.bot = bot

        print("")

        print(
            "=" * 70
        )

        print(
            "🤖 AUTO SCAN"
        )

        print(
            "=" * 70
        )

        signal = await (
            self.find_best_signal()
        )

        if signal is None:

            print(
                "⚪ Автоматический сигнал "
                "не найден."
            )

            print(
                "=" * 70
            )

            return None

        self.save_signal(
            signal
        )

        # Используем сохранённый bot.
        if self.bot is not None:

            await (
                self.send_signal_to_users(
                    self.bot,
                    signal,
                )
            )

        else:

            print(
                "⚠️ Bot отсутствует — "
                "сигнал не отправлен."
            )

        print(
            "✅ AUTO SIGNAL READY"
        )

        print(
            "=" * 70
        )

        return signal

    # ============================================================
    # MANUAL SIGNAL
    # ============================================================

    async def get_manual_signal(
        self,
        pair: Optional[str] = None,
    ) -> Optional[Signal]:

        print("")

        print(
            "🎯 MANUAL SIGNAL REQUEST"
        )

        signal = await (
            self.find_best_signal(
                pair=pair
            )
        )

        if signal is not None:

            self.save_signal(
                signal
            )

        return signal

    # ============================================================
    # MAIN LOOP
    # ============================================================

    async def run(
        self,
        bot=None,
    ) -> None:

        # Если run получил bot — сохраняем его.
        #
        # В текущем main.py bot не передаётся:
        #
        #     scheduler.run()
        #
        # поэтому используется bot из:
        #
        #     SignalScheduler(bot)

        if bot is not None:
            self.bot = bot

        if self._running:

            print(
                "⚠️ Scheduler уже работает."
            )

            return

        self._running = True

        print("")

        print(
            "=" * 70
        )

        print(
            "🚀 SIGNAL SCHEDULER STARTED"
        )

        print(
            f"📈 Minimum probability: "
            f"{self.engine.min_probability}%"
        )

        print(
            f"📊 Minimum quality: "
            f"{self.engine.min_quality}"
        )

        print(
            f"🤖 Telegram bot: "
            f"{'CONNECTED' if self.bot else 'NOT SET'}"
        )

        print(
            "=" * 70
        )

        try:

            while True:

                started = (
                    datetime.now(
                        timezone.utc
                    )
                )

                try:

                    await self.scan_once()

                except asyncio.CancelledError:

                    raise

                except Exception as exc:

                    print(
                        "💥 AUTO SCAN ERROR"
                    )

                    print(
                        f"{type(exc).__name__}: "
                        f"{exc}"
                    )

                    traceback.print_exc()

                elapsed = (
                    datetime.now(
                        timezone.utc
                    ) - started
                ).total_seconds()

                sleep_seconds = max(
                    1,
                    AUTO_SCAN_SECONDS
                    - elapsed,
                )

                print(
                    f"💤 Следующее "
                    f"сканирование через "
                    f"{sleep_seconds:.1f} сек."
                )

                await asyncio.sleep(
                    sleep_seconds
                )

        except asyncio.CancelledError:

            print(
                "🛑 Scheduler stopped."
            )

            raise

        finally:

            self._running = False


# ================================================================
# GLOBAL INSTANCE
# ================================================================

scheduler_instance = SignalScheduler()
