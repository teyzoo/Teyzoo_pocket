from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from typing import Any

from config import (
    PAIRS,
    MIN_PROBABILITY,
    MIN_QUALITY,
    SIGNAL_INTERVAL_MINUTES,
    TIMEZONE,
)

from database import db
from market import market_client
from signal_engine import Signal, SignalEngine


class SignalScheduler:
    """
    Планировщик автоматических и ручных сигналов.

    Совместим с:
        scheduler = SignalScheduler(bot)
        scheduler.run()

    Также поддерживает:
        scheduler = SignalScheduler()
        await scheduler.run(bot)
    """

    def __init__(self, bot=None):
        self.bot = bot
        self.engine = SignalEngine()

        self.running = False
        self.last_signal_time: dict[str, datetime] = {}

        self.scan_lock = asyncio.Lock()

        print("[SCHEDULER] Инициализирован.")
        print(f"[SCHEDULER] Минимальное качество: {MIN_QUALITY}")
        print(f"[SCHEDULER] Минимальный шанс: {MIN_PROBABILITY}%")

    # ============================================================
    # BOT
    # ============================================================

    def set_bot(self, bot):
        self.bot = bot

    # ============================================================
    # TIME
    # ============================================================

    def _now(self) -> datetime:
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(TIMEZONE))
        except Exception:
            return datetime.now()

    def _next_signal_time(self, minutes: int = 5) -> datetime:
        """
        Возвращает ближайшее будущее время, кратное minutes.
        Например:
        21:07 -> 21:10
        21:10 -> 21:15
        """

        now = self._now()

        base = now.replace(second=0, microsecond=0)

        remainder = base.minute % minutes

        if remainder == 0:
            return base + timedelta(minutes=minutes)

        return base + timedelta(minutes=minutes - remainder)

    def _format_time(self, value: datetime) -> str:
        return value.strftime("%H:%M")

    # ============================================================
    # PAIRS
    # ============================================================

    async def get_available_pairs(self) -> list[str]:
        """
        Получает доступные пары.

        Сначала пытаемся использовать динамический список
        market_client.

        Если он недоступен — используем PAIRS из config.py.
        """

        try:
            method = getattr(
                market_client,
                "get_available_pairs",
                None,
            )

            if method is not None:
                result = method()

                if asyncio.iscoroutine(result):
                    result = await result

                if result:
                    pairs = []

                    for pair in result:
                        if not pair:
                            continue

                        pair = str(pair).strip().upper()

                        if "/" not in pair:
                            continue

                        if pair.endswith(" OTC"):
                            continue

                        if " OTC" in pair:
                            continue

                        if pair not in pairs:
                            pairs.append(pair)

                    if pairs:
                        print(
                            f"[PAIRS] Динамически найдено пар: "
                            f"{len(pairs)}"
                        )

                        return pairs

        except Exception as exc:
            print(
                f"[PAIRS] Ошибка динамического списка: {exc}"
            )

        fallback = []

        for pair in PAIRS:
            pair = str(pair).strip().upper()

            if "/" not in pair:
                continue

            if pair.endswith(" OTC"):
                continue

            if pair not in fallback:
                fallback.append(pair)

        print(
            f"[PAIRS] Используется резервный список: "
            f"{len(fallback)} пар"
        )

        return fallback

    # ============================================================
    # MARKET DATA
    # ============================================================

    async def _get_candles(self, pair: str):
        """
        Получение свечей через market_client.

        Поддерживает несколько возможных названий методов,
        чтобы scheduler не ломался при изменении market.py.
        """

        methods = (
            "get_candles",
            "fetch_candles",
            "get_history",
            "get_data",
        )

        for method_name in methods:
            method = getattr(
                market_client,
                method_name,
                None,
            )

            if method is None:
                continue

            try:
                result = method(pair)

                if asyncio.iscoroutine(result):
                    result = await result

                if result is not None:
                    return result

            except TypeError:
                try:
                    result = method(
                        symbol=pair
                    )

                    if asyncio.iscoroutine(result):
                        result = await result

                    if result is not None:
                        return result

                except Exception:
                    pass

            except Exception as exc:
                print(
                    f"[MARKET] {pair}: "
                    f"{method_name} ошибка: {exc}"
                )

        print(
            f"[MARKET] {pair}: "
            "не удалось получить свечи"
        )

        return None

    # ============================================================
    # SIGNAL ANALYSIS
    # ============================================================

    async def analyze_pair(
        self,
        pair: str,
    ) -> Signal | None:

        pair = str(pair).strip().upper()

        try:
            candles = await self._get_candles(pair)

            if candles is None:
                print(
                    f"[ANALYZE] {pair}: "
                    "нет данных"
                )
                return None

            try:
                length = len(candles)
            except Exception:
                length = 0

            if length < 50:
                print(
                    f"[ANALYZE] {pair}: "
                    f"недостаточно свечей ({length})"
                )
                return None

            # ----------------------------------------------------
            # Поддержка разных API SignalEngine
            # ----------------------------------------------------

            methods = (
                "analyze",
                "generate_signal",
                "get_signal",
            )

            signal = None

            for method_name in methods:
                method = getattr(
                    self.engine,
                    method_name,
                    None,
                )

                if method is None:
                    continue

                try:
                    signal = method(
                        pair,
                        candles,
                    )

                    if asyncio.iscoroutine(signal):
                        signal = await signal

                    break

                except TypeError:
                    try:
                        signal = method(
                            candles,
                            pair,
                        )

                        if asyncio.iscoroutine(signal):
                            signal = await signal

                        break

                    except Exception:
                        continue

                except Exception as exc:
                    print(
                        f"[ENGINE] {pair}: "
                        f"{method_name}: {exc}"
                    )
                    return None

            if signal is None:
                print(
                    f"[ANALYZE] {pair}: "
                    "сигнал не сформирован"
                )
                return None

            # ----------------------------------------------------
            # Проверяем вероятность
            # ----------------------------------------------------

            probability = self._get_value(
                signal,
                "probability",
                0,
            )

            quality = self._get_value(
                signal,
                "quality",
                0,
            )

            try:
                probability = float(probability)
            except Exception:
                probability = 0.0

            try:
                quality = float(quality)
            except Exception:
                quality = 0.0

            print(
                f"[ANALYZE] {pair}: "
                f"quality={quality:.1f}, "
                f"probability={probability:.1f}%"
            )

            if quality < float(MIN_QUALITY):
                print(
                    f"[REJECT] {pair}: "
                    f"quality {quality:.1f} < "
                    f"{MIN_QUALITY}"
                )
                return None

            if probability < float(MIN_PROBABILITY):
                print(
                    f"[REJECT] {pair}: "
                    f"probability {probability:.1f}% < "
                    f"{MIN_PROBABILITY}%"
                )
                return None

            direction = self._get_value(
                signal,
                "direction",
                "",
            )

            if not direction:
                print(
                    f"[REJECT] {pair}: "
                    "нет направления"
                )
                return None

            direction = str(direction).upper()

            if direction not in {
                "CALL",
                "PUT",
                "UP",
                "DOWN",
            }:
                print(
                    f"[REJECT] {pair}: "
                    f"неизвестное направление "
                    f"{direction}"
                )
                return None

            return signal

        except Exception as exc:
            print(
                f"[ANALYZE] {pair}: "
                f"критическая ошибка: {exc}"
            )
            return None

    # ============================================================
    # VALUE HELPER
    # ============================================================

    @staticmethod
    def _get_value(
        obj: Any,
        name: str,
        default=None,
    ):
        if obj is None:
            return default

        if isinstance(obj, dict):
            return obj.get(name, default)

        return getattr(
            obj,
            name,
            default,
        )

    # ============================================================
    # SIGNAL RANKING
    # ============================================================

    def _signal_score(
        self,
        signal: Signal,
    ) -> tuple[float, float]:
        quality = self._get_value(
            signal,
            "quality",
            0,
        )

        probability = self._get_value(
            signal,
            "probability",
            0,
        )

        try:
            quality = float(quality)
        except Exception:
            quality = 0

        try:
            probability = float(probability)
        except Exception:
            probability = 0

        return quality, probability

    def _choose_best(
        self,
        signals: list[Signal],
    ) -> Signal | None:

        if not signals:
            return None

        valid = []

        for signal in signals:
            quality, probability = self._signal_score(
                signal
            )

            if quality < float(MIN_QUALITY):
                continue

            if probability < float(MIN_PROBABILITY):
                continue

            valid.append(
                (
                    quality,
                    probability,
                    signal,
                )
            )

        if not valid:
            return None

        valid.sort(
            key=lambda item: (
                item[0],
                item[1],
            ),
            reverse=True,
        )

        return valid[0][2]

    # ============================================================
    # MANUAL SIGNAL
    # ============================================================

    async def get_manual_signal(
        self,
        pair: str | None = None,
    ) -> Signal | None:

        async with self.scan_lock:

            if pair:
                pair = str(pair).strip().upper()

                print(
                    f"[MANUAL] Анализ пары {pair}"
                )

                return await self.analyze_pair(
                    pair
                )

            print(
                "[MANUAL] Анализ всех доступных пар"
            )

            pairs = await self.get_available_pairs()

            if not pairs:
                print(
                    "[MANUAL] Нет доступных пар"
                )
                return None

            signals: list[Signal] = []

            for current_pair in pairs:

                signal = await self.analyze_pair(
                    current_pair
                )

                if signal is not None:
                    signals.append(signal)

            best = self._choose_best(signals)

            if best is None:
                print(
                    "[MANUAL] Сильных сигналов "
                    "не найдено"
                )
                return None

            pair_name = self._get_value(
                best,
                "pair",
                "?",
            )

            probability = self._get_value(
                best,
                "probability",
                0,
            )

            quality = self._get_value(
                best,
                "quality",
                0,
            )

            print(
                f"[MANUAL] Лучший сигнал: "
                f"{pair_name} | "
                f"Q={quality} | "
                f"P={probability}%"
            )

            return best

    # ============================================================
    # AUTOMATIC SCAN
    # ============================================================

    async def scan_once(
        self,
        bot=None,
    ):

        if bot is not None:
            self.bot = bot

        if self.bot is None:
            print(
                "[SCAN] Bot не передан."
            )
            return None

        async with self.scan_lock:

            print("=" * 60)
            print(
                f"[SCAN] Начало анализа "
                f"{self._now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            print("=" * 60)

            pairs = await self.get_available_pairs()

            if not pairs:
                print(
                    "[SCAN] Нет доступных пар."
                )
                return None

            print(
                f"[SCAN] Проверяю {len(pairs)} пар"
            )

            signals: list[Signal] = []

            for pair in pairs:

                try:
                    signal = await self.analyze_pair(
                        pair
                    )

                    if signal is not None:
                        signals.append(signal)

                except Exception as exc:
                    print(
                        f"[SCAN] Ошибка {pair}: "
                        f"{exc}"
                    )

            best = self._choose_best(
                signals
            )

            if best is None:
                print(
                    "[SCAN] Сильного сигнала "
                    "не найдено."
                )
                return None

            print(
                "[SCAN] Лучший сигнал найден:"
            )

            print(
                self.format_signal(best)
            )

            await self._save_signal(
                best
            )

            await self._send_to_users(
                best
            )

            return best

    # ============================================================
    # DATABASE
    # ============================================================

    async def _save_signal(
        self,
        signal: Signal,
    ):

        try:
            pair = self._get_value(
                signal,
                "pair",
                "",
            )

            direction = self._get_value(
                signal,
                "direction",
                "",
            )

            quality = self._get_value(
                signal,
                "quality",
                0,
            )

            probability = self._get_value(
                signal,
                "probability",
                0,
            )

            entry_time = self._get_value(
                signal,
                "entry_time",
                None,
            )

            expiry_time = self._get_value(
                signal,
                "expiry_time",
                None,
            )

            confirmations = self._get_value(
                signal,
                "confirmations",
                [],
            )

            reasons = self._get_value(
                signal,
                "reasons",
                [],
            )

            if isinstance(confirmations, list):
                confirmations = ", ".join(
                    str(x)
                    for x in confirmations
                )

            if isinstance(reasons, list):
                reasons = ", ".join(
                    str(x)
                    for x in reasons
                )

            save_method = getattr(
                db,
                "save_signal",
                None,
            )

            if save_method is None:
                return

            data = {
                "pair": pair,
                "direction": direction,
                "quality": quality,
                "probability": probability,
                "entry_time": (
                    entry_time.isoformat()
                    if isinstance(
                        entry_time,
                        datetime,
                    )
                    else entry_time
                ),
                "expiry_time": (
                    expiry_time.isoformat()
                    if isinstance(
                        expiry_time,
                        datetime,
                    )
                    else expiry_time
                ),
                "confirmations": confirmations,
                "reasons": reasons,
            }

            try:
                result = save_method(**data)

                if asyncio.iscoroutine(result):
                    await result

            except TypeError:
                # Совместимость с более старым
                # save_signal(signal)
                try:
                    result = save_method(
                        signal
                    )

                    if asyncio.iscoroutine(result):
                        await result

                except Exception as exc:
                    print(
                        f"[DB] Ошибка сохранения: "
                        f"{exc}"
                    )

        except Exception as exc:
            print(
                f"[DB] Ошибка: {exc}"
            )

    # ============================================================
    # SEND SIGNAL
    # ============================================================

    async def _send_to_users(
        self,
        signal: Signal,
    ):

        if self.bot is None:
            print(
                "[SEND] Bot отсутствует."
            )
            return

        try:
            users = db.get_approved_users()

        except Exception as exc:
            print(
                f"[SEND] Не удалось получить "
                f"пользователей: {exc}"
            )
            return

        if not users:
            print(
                "[SEND] Нет одобренных пользователей."
            )
            return

        text = self.format_signal(
            signal
        )

        sent = 0

        for user in users:

            try:

                if isinstance(
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
                        user,
                    )

                if not user_id:
                    continue

                await self.bot.send_message(
                    chat_id=int(user_id),
                    text=text,
                )

                sent += 1

            except Exception as exc:
                print(
                    f"[SEND] Ошибка пользователю "
                    f"{user}: {exc}"
                )

        print(
            f"[SEND] Сигнал отправлен "
            f"{sent}/{len(users)} пользователям."
        )

    # ============================================================
    # FORMAT
    # ============================================================

    def format_signal(
        self,
        signal: Signal,
    ) -> str:

        pair = self._get_value(
            signal,
            "pair",
            "UNKNOWN",
        )

        direction = self._get_value(
            signal,
            "direction",
            "",
        )

        quality = self._get_value(
            signal,
            "quality",
            0,
        )

        probability = self._get_value(
            signal,
            "probability",
            0,
        )

        entry_time = self._get_value(
            signal,
            "entry_time",
            None,
        )

        expiry_time = self._get_value(
            signal,
            "expiry_time",
            None,
        )

        confirmations = self._get_value(
            signal,
            "confirmations",
            [],
        )

        if isinstance(
            confirmations,
            (list, tuple),
        ):
            confirmations_text = ", ".join(
                str(x)
                for x in confirmations
            )
        else:
            confirmations_text = str(
                confirmations or ""
            )

        direction = str(
            direction
        ).upper()

        if direction in {
            "CALL",
            "UP",
        }:
            emoji = "🟢"
            direction_text = "CALL ↑"
        else:
            emoji = "🔴"
            direction_text = "PUT ↓"

        if isinstance(
            entry_time,
            datetime,
        ):
            entry_text = (
                self._format_time(
                    entry_time
                )
                + " МСК"
            )
        else:
            entry_text = (
                str(entry_time)
                if entry_time
                else "—"
            )

        if isinstance(
            expiry_time,
            datetime,
        ):
            expiry_text = (
                self._format_time(
                    expiry_time
                )
                + " МСК"
            )
        else:
            expiry_text = (
                str(expiry_time)
                if expiry_time
                else "—"
            )

        try:
            quality_text = f"{float(quality):.0f}"
        except Exception:
            quality_text = str(quality)

        try:
            probability_text = (
                f"{float(probability):.0f}%"
            )
        except Exception:
            probability_text = (
                f"{probability}%"
            )

        lines = [
            f"{emoji} {direction_text}",
            "",
            f"💱 {pair}",
            f"⏰ ВХОД: {entry_text}",
            f"🎯 ЭКСПИРАЦИЯ: {expiry_text}",
            f"📊 QUALITY: {quality_text}/100",
            f"📈 ШАНС: {probability_text}",
        ]

        if confirmations_text:
            lines.extend(
                [
                    "",
                    "✅ Подтверждения:",
                    confirmations_text,
                ]
            )

        return "\n".join(lines)

    # ============================================================
    # LOOP
    # ============================================================

    async def run(
        self,
        bot=None,
    ):

        if bot is not None:
            self.bot = bot

        self.running = True

        print(
            "[SCHEDULER] Автоматический "
            "планировщик запущен."
        )

        while self.running:

            try:

                next_time = (
                    self._next_signal_time(
                        SIGNAL_INTERVAL_MINUTES
                    )
                )

                now = self._now()

                wait_seconds = (
                    next_time - now
                ).total_seconds()

                if wait_seconds < 0:
                    wait_seconds = 0

                print(
                    "[SCHEDULER] Следующий анализ: "
                    f"{next_time.strftime('%H:%M:%S')} МСК"
                )

                await asyncio.sleep(
                    wait_seconds
                )

                if not self.running:
                    break

                await self.scan_once()

                # Небольшая защита от двойного запуска
                await asyncio.sleep(2)

            except asyncio.CancelledError:
                print(
                    "[SCHEDULER] Остановка."
                )
                break

            except Exception as exc:
                print(
                    f"[SCHEDULER] Ошибка цикла: "
                    f"{exc}"
                )

                await asyncio.sleep(
                    10
                )

        self.running = False

    # ============================================================
    # STOP
    # ============================================================

    def stop(self):
        self.running = False


# ================================================================
# GLOBAL INSTANCE
# ================================================================

scheduler_instance = SignalScheduler()
