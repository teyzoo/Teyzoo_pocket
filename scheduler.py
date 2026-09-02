from __future__ import annotations
import asyncio
import traceback
from datetime import datetime, timezone
from typing import Optional
from config import (
    AUTO_SCAN_SECONDS,
    MIN_PROBABILITY,
    PAIRS,
    TIMEZONE,
)
from database import db
from market import market_client
from signal_engine import Signal, SignalEngine
class SignalScheduler:
    """
    Планировщик автоматического и ручного поиска сигналов.
    Диагностика каждой пары выводится в Render logs.
    """
    def __init__(self):
        self.engine = SignalEngine(
            min_probability=MIN_PROBABILITY,
        )
        self._running = False
        self._lock = asyncio.Lock()
    # ============================================================
    # TIME
    # ============================================================
    @staticmethod
    def _to_moscow(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TIMEZONE)
    # ============================================================
    # MARKET ANALYSIS
    # ============================================================
    async def _analyze_pair(
        self,
        pair: str,
    ) -> Optional[Signal]:
        """
        Получает свечи и анализирует конкретную пару.
        """
        print("")
        print(f"🔍 Проверка пары: {pair}")
        try:
            candles = await market_client.get_candles(
                pair
            )
        except Exception as exc:
            print(
                f"❌ {pair}: ошибка получения свечей:"
            )
            print(
                f"   {type(exc).__name__}: {exc}"
            )
            return None
        if candles is None:
            print(
                f"❌ {pair}: MarketClient вернул None"
            )
            return None
        try:
            candle_count = len(candles)
        except Exception:
            candle_count = "unknown"
        print(
            f"📥 {pair}: получено свечей = "
            f"{candle_count}"
        )
        try:
            signal = self.engine.analyze(
                pair=pair,
                candles=candles,
            )
        except Exception as exc:
            print("")
            print(
                f"💥 {pair}: ошибка SignalEngine"
            )
            print(
                f"   {type(exc).__name__}: {exc}"
            )
            traceback.print_exc()
            return None
        if signal is None:
            print(
                f"⚪ {pair}: сильного сигнала нет"
            )
            return None
        print(
            f"🟢 {pair}: сигнал найден — "
            f"{signal.direction}"
        )
        print(
            f"   Quality: {signal.quality:.1f}"
        )
        print(
            f"   Chance: {signal.probability:.1f}%"
        )
        return signal
    # ============================================================
    # FIND BEST SIGNAL
    # ============================================================
    async def find_best_signal(
        self,
        pair: Optional[str] = None,
    ) -> Optional[Signal]:
        """
        Если pair задан:
            анализируется только указанная пара.
        Если pair=None:
            анализируются все PAIRS и выбирается лучший сигнал.
        """
        async with self._lock:
            # ----------------------------------------------------
            # SPECIFIC PAIR
            # ----------------------------------------------------
            if pair is not None:
                pair = pair.strip()
                print("")
                print("#" * 70)
                print(
                    f"🎯 MANUAL ANALYSIS: {pair}"
                )
                print("#" * 70)
                signal = await self._analyze_pair(
                    pair
                )
                print("")
                print(
                    f"🏁 Результат {pair}: "
                    f"{'SIGNAL' if signal else 'NO SIGNAL'}"
                )
                return signal
            # ----------------------------------------------------
            # ALL PAIRS
            # ----------------------------------------------------
            print("")
            print("#" * 70)
            print("🔀 ANALYZING ALL PAIRS")
            print(
                f"📋 Pairs: {', '.join(PAIRS)}"
            )
            print(
                f"📈 Minimum chance: "
                f"{MIN_PROBABILITY}%"
            )
            print(
                f"📊 Minimum quality: "
                f"{self.engine.min_quality}"
            )
            print("#" * 70)
            if not PAIRS:
                print(
                    "❌ PAIRS пустой"
                )
                return None
            tasks = [
                self._analyze_pair(pair)
                for pair in PAIRS
            ]
            results = await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )
            valid_signals: list[Signal] = []
            print("")
            print("-" * 70)
            print("📋 ANALYSIS SUMMARY")
            print("-" * 70)
            for pair, result in zip(
                PAIRS,
                results,
            ):
                if isinstance(
                    result,
                    Exception,
                ):
                    print(
                        f"❌ {pair}: "
                        f"exception "
                        f"{type(result).__name__}: "
                        f"{result}"
                    )
                    continue
                if result is None:
                    print(
                        f"⚪ {pair}: "
                        f"NO VALID SIGNAL"
                    )
                    continue
                valid_signals.append(result)
                print(
                    f"🟢 {pair}: "
                    f"{result.direction} | "
                    f"Q={result.quality:.1f} | "
                    f"P={result.probability:.1f}%"
                )
            print("-" * 70)
            # ----------------------------------------------------
            # NO SIGNALS
            # ----------------------------------------------------
            if not valid_signals:
                print(
                    "⚪ ALL PAIRS: "
                    "NO VALID SIGNAL"
                )
                print(
                    f"📈 Требовался шанс >= "
                    f"{MIN_PROBABILITY}%"
                )
                print("#" * 70)
                return None
            # ----------------------------------------------------
            # BEST SIGNAL
            # ----------------------------------------------------
            best_signal = max(
                valid_signals,
                key=lambda signal: (
                    signal.probability,
                    signal.quality,
                    len(signal.confirmations),
                ),
            )
            print("")
            print(
                "🏆 BEST SIGNAL"
            )
            print(
                f"   Pair: "
                f"{best_signal.pair}"
            )
            print(
                f"   Direction: "
                f"{best_signal.direction}"
            )
            print(
                f"   Quality: "
                f"{best_signal.quality:.1f}/100"
            )
            print(
                f"   Chance: "
                f"{best_signal.probability:.1f}%"
            )
            print(
                f"   Confirmations: "
                f"{len(best_signal.confirmations)}"
            )
            print("#" * 70)
            return best_signal
    # ============================================================
    # FORMAT SIGNAL
    # ============================================================
    def format_signal(
        self,
        signal: Signal,
    ) -> str:
        """
        Форматирует сигнал для Telegram.
        """
        if signal.direction == "CALL":
            direction_text = "🟢 CALL ↑"
        else:
            direction_text = "🔴 PUT ↓"
        entry_moscow = self._to_moscow(
            signal.entry_time
        )
        expiry_moscow = self._to_moscow(
            signal.expiry_time
        )
        lines = [
            direction_text,
            "",
            f"💱 {signal.pair}",
            "",
            (
                "⏰ ВХОД: "
                f"{entry_moscow.strftime('%H:%M')} МСК"
            ),
            (
                "🎯 ЭКСПИРАЦИЯ: "
                f"{expiry_moscow.strftime('%H:%M')} МСК"
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
            for confirmation in signal.confirmations:
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
        """
        Сохраняет сигнал в БД без создания дубликата.
        """
        entry_time = signal.entry_time.isoformat()
        if db.signal_exists(
            pair=signal.pair,
            direction=signal.direction,
            entry_time=entry_time,
        ):
            print(
                f"⚠️ Дубликат сигнала: "
                f"{signal.pair} "
                f"{signal.direction} "
                f"{entry_time}"
            )
            return None
        confirmations = "\n".join(
            signal.confirmations
        )
        reasons = "\n".join(
            signal.reasons
        )
        signal_id = db.save_signal(
            pair=signal.pair,
            direction=signal.direction,
            quality=signal.quality,
            entry_time=entry_time,
            expiry_time=signal.expiry_time.isoformat(),
            analysis_time=signal.analysis_time.isoformat(),
            confirmations=confirmations,
            reasons=reasons,
        )
        print(
            f"💾 Signal saved: "
            f"id={signal_id} "
            f"{signal.pair} "
            f"{signal.direction}"
        )
        return signal_id
    # ============================================================
    # SEND TO USERS
    # ============================================================
    async def send_signal_to_users(
        self,
        bot,
        signal: Signal,
    ) -> int:
        """
        Отправляет сигнал всем одобренным пользователям.
        Возвращает количество успешных отправок.
        """
        users = db.get_approved_users()
        if not users:
            print(
                "ℹ️ Нет APPROVED пользователей."
            )
            return 0
        message = self.format_signal(
            signal
        )
        sent_count = 0
        for user in users:
            user_id = user.get("user_id")
            if not user_id:
                continue
            try:
                await bot.send_message(
                    chat_id=int(user_id),
                    text=message,
                )
                sent_count += 1
            except Exception as exc:
                print(
                    f"❌ Не удалось отправить "
                    f"сигнал пользователю "
                    f"{user_id}: "
                    f"{type(exc).__name__}: {exc}"
                )
        print(
            f"📨 Signal sent: "
            f"{sent_count}/{len(users)}"
        )
        return sent_count
    # ============================================================
    # AUTO SCAN
    # ============================================================
    async def scan_once(
        self,
        bot=None,
    ) -> Optional[Signal]:
        """
        Один автоматический цикл анализа всех пар.
        """
        print("")
        print("=" * 70)
        print("🤖 AUTO SCAN STARTED")
        print("=" * 70)
        signal = await self.find_best_signal()
        if signal is None:
            print(
                "⚪ AUTO SCAN: "
                "сильного сигнала сейчас нет."
            )
            print("=" * 70)
            return None
        self.save_signal(
            signal
        )
        if bot is not None:
            await self.send_signal_to_users(
                bot,
                signal,
            )
        print(
            "✅ AUTO SCAN FINISHED"
        )
        print("=" * 70)
        return signal
    # ============================================================
    # MANUAL SIGNAL
    # ============================================================
    async def get_manual_signal(
        self,
        pair: Optional[str] = None,
    ) -> Optional[Signal]:
        """
        Ручной запрос сигнала.
        pair=None -> анализ всех пар.
        """
        print("")
        print("🎯 MANUAL SIGNAL REQUEST")
        signal = await self.find_best_signal(
            pair=pair,
        )
        if signal is not None:
            self.save_signal(
                signal
            )
        return signal
    # ============================================================
    # RUN LOOP
    # ============================================================
    async def run(
        self,
        bot=None,
    ) -> None:
        """
        Бесконечный цикл автоматического анализа.
        """
        if self._running:
            print(
                "⚠️ SignalScheduler уже запущен."
            )
            return
        self._running = True
        print("")
        print("=" * 70)
        print("🚀 SIGNAL SCHEDULER STARTED")
        print("=" * 70)
        print(
            f"⏱ Интервал: "
            f"{AUTO_SCAN_SECONDS} секунд"
        )
        print(
            f"📋 Пары: "
            f"{', '.join(PAIRS)}"
        )
        print(
            f"📊 Minimum Quality: "
            f"{self.engine.min_quality}"
        )
        print(
            f"📈 Minimum Probability: "
            f"{self.engine.min_probability}%"
        )
        print("=" * 70)
        try:
            while True:
                started_at = (
                    datetime.now(timezone.utc)
                )
                try:
                    await self.scan_once(
                        bot=bot
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print("")
                    print(
                        "💥 AUTO SCAN ERROR"
                    )
                    print(
                        f"{type(exc).__name__}: "
                        f"{exc}"
                    )
                    traceback.print_exc()
                elapsed = (
                    datetime.now(timezone.utc)
                    - started_at
                ).total_seconds()
                sleep_seconds = max(
                    1,
                    AUTO_SCAN_SECONDS - elapsed,
                )
                print("")
                print(
                    f"💤 Следующее сканирование "
                    f"через {sleep_seconds:.1f} сек."
                )
                await asyncio.sleep(
                    sleep_seconds
                )
        except asyncio.CancelledError:
            print(
                "🛑 Signal Scheduler остановлен."
            )
            raise
        finally:
            self._running = False
# ================================================================
# GLOBAL INSTANCE
# ================================================================
scheduler_instance = SignalScheduler()
