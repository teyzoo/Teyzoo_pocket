from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from config import (
    PAIRS,
    MIN_PROBABILITY,
    MIN_QUALITY,
)

from database import db
from market import market_client
from signal_engine import SignalEngine, Signal


# Интервал автоматического анализа.
# Не берём его из config.py, чтобы не было ошибки импорта.
SIGNAL_INTERVAL_MINUTES = 5

# Московское время.
try:
    from zoneinfo import ZoneInfo

    MOSCOW_TZ = ZoneInfo("Europe/Moscow")
except Exception:
    MOSCOW_TZ = None


class SignalScheduler:
    """
    Планировщик сигналов.

    Совместим с:
        scheduler = SignalScheduler(bot)

    и:
        scheduler.run()
    """

    def __init__(self, bot=None):
        self.bot = bot
        self.engine = SignalEngine()

        self.running = False
        self.scan_lock = asyncio.Lock()

        print("[SCHEDULER] Инициализирован")
        print(
            f"[SCHEDULER] MIN_QUALITY = {MIN_QUALITY}"
        )
        print(
            f"[SCHEDULER] MIN_PROBABILITY = "
            f"{MIN_PROBABILITY}%"
        )

    # ============================================================
    # BOT
    # ============================================================

    def set_bot(self, bot):
        self.bot = bot

    # ============================================================
    # TIME
    # ============================================================

    def now(self) -> datetime:
        if MOSCOW_TZ is not None:
            return datetime.now(MOSCOW_TZ)

        return datetime.now()

    def next_analysis_time(
        self,
        interval: int = SIGNAL_INTERVAL_MINUTES,
    ) -> datetime:

        now = self.now()

        current = now.replace(
            second=0,
            microsecond=0,
        )

        remainder = current.minute % interval

        if remainder == 0:
            return current + timedelta(
                minutes=interval
            )

        return current + timedelta(
            minutes=interval - remainder
        )

    # ============================================================
    # AVAILABLE PAIRS
    # ============================================================

    async def get_available_pairs(
        self,
    ) -> list[str]:

        """
        Пытаемся получить динамический список
        из market.py.

        Если market.py пока не умеет динамически
        получать пары — используем PAIRS из config.py.
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

                    cleaned = []

                    for pair in result:

                        if not pair:
                            continue

                        pair = str(
                            pair
                        ).strip().upper()

                        # Не используем OTC,
                        # потому что обычный Twelve Data
                        # не даёт OTC-котировки Pocket Option.
                        if "OTC" in pair:
                            continue

                        if "/" not in pair:
                            continue

                        if pair not in cleaned:
                            cleaned.append(pair)

                    if cleaned:
                        print(
                            "[PAIRS] Динамически найдено: "
                            f"{len(cleaned)}"
                        )

                        return cleaned

        except Exception as exc:
            print(
                "[PAIRS] Ошибка динамического "
                f"получения: {exc}"
            )

        # Резервный список.
        fallback = []

        for pair in PAIRS:

            pair = str(
                pair
            ).strip().upper()

            if "/" not in pair:
                continue

            if "OTC" in pair:
                continue

            if pair not in fallback:
                fallback.append(pair)

        print(
            "[PAIRS] Использую PAIRS из config.py: "
            f"{len(fallback)}"
        )

        return fallback

    # ============================================================
    # MARKET
    # ============================================================

    async def get_candles(
        self,
        pair: str,
    ):

        """
        Совместимость с разными версиями market.py.
        """

        method_names = (
            "get_candles",
            "fetch_candles",
            "get_history",
            "get_data",
        )

        for method_name in method_names:

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
                    f"{method_name}: {exc}"
                )

        print(
            f"[MARKET] {pair}: "
            "свечи не получены"
        )

        return None

    # ============================================================
    # ENGINE
    # ============================================================

    async def analyze_pair(
        self,
        pair: str,
    ) -> Signal | None:

        pair = str(
            pair
        ).strip().upper()

        try:

            candles = await self.get_candles(
                pair
            )

            if candles is None:
                print(
                    f"[ANALYZE] {pair}: "
                    "нет данных"
                )
                return None

            try:
                candle_count = len(
                    candles
                )
            except Exception:
                candle_count = 0

            if candle_count < 50:

                print(
                    f"[ANALYZE] {pair}: "
                    f"мало свечей: "
                    f"{candle_count}/50"
                )

                return None

            signal = None

            # Поддержка нескольких API SignalEngine.
            for method_name in (
                "analyze",
                "generate_signal",
                "get_signal",
            ):

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

                        if asyncio.iscoroutine(
                            signal
                        ):
                            signal = await signal

                        break

                    except Exception:
                        continue

                except Exception as exc:

                    print(
                        f"[ENGINE] {pair}: "
                        f"{exc}"
                    )

                    return None

            if signal is None:

                print(
                    f"[ANALYZE] {pair}: "
                    "сигнал не сформирован"
                )

                return None

            quality = self.get_value(
                signal,
                "quality",
                0,
            )

            probability = self.get_value(
                signal,
                "probability",
                0,
            )

            try:
                quality = float(
                    quality
                )
            except Exception:
                quality = 0.0

            try:
                probability = float(
                    probability
                )
            except Exception:
                probability = 0.0

            print(
                f"[ANALYZE] {pair}: "
                f"Q={quality:.1f} "
                f"P={probability:.1f}%"
            )

            # ====================================================
            # QUALITY FILTER
            # ====================================================

            if quality < float(
                MIN_QUALITY
            ):

                print(
                    f"[REJECT] {pair}: "
                    f"quality {quality:.1f} < "
                    f"{MIN_QUALITY}"
                )

                return None

            # ====================================================
            # PROBABILITY FILTER
            # ====================================================

            if probability < float(
                MIN_PROBABILITY
            ):

                print(
                    f"[REJECT] {pair}: "
                    f"probability "
                    f"{probability:.1f}% < "
                    f"{MIN_PROBABILITY}%"
                )

                return None

            direction = self.get_value(
                signal,
                "direction",
                "",
            )

            if not direction:
                return None

            direction = str(
                direction
            ).upper()

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
                f"ошибка {exc}"
            )

            return None

    # ============================================================
    # VALUE
    # ============================================================

    @staticmethod
    def get_value(
        obj: Any,
        name: str,
        default=None,
    ):

        if obj is None:
            return default

        if isinstance(
            obj,
            dict,
        ):
            return obj.get(
                name,
                default,
            )

        return getattr(
            obj,
            name,
            default,
        )

    # ============================================================
    # BEST SIGNAL
    # ============================================================

    def choose_best(
        self,
        signals: list[Signal],
    ) -> Signal | None:

        if not signals:
            return None

        valid = []

        for signal in signals:

            quality = self.get_value(
                signal,
                "quality",
                0,
            )

            probability = self.get_value(
                signal,
                "probability",
                0,
            )

            try:
                quality = float(
                    quality
                )
            except Exception:
                quality = 0

            try:
                probability = float(
                    probability
                )
            except Exception:
                probability = 0

            if quality < float(
                MIN_QUALITY
            ):
                continue

            if probability < float(
                MIN_PROBABILITY
            ):
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

            # Конкретная пара.
            if pair:

                pair = str(
                    pair
                ).strip().upper()

                print(
                    f"[MANUAL] Проверяю {pair}"
                )

                return await self.analyze_pair(
                    pair
                )

            # Любая пара.
            print(
                "[MANUAL] Проверяю все доступные пары"
            )

            pairs = await self.get_available_pairs()

            if not pairs:
                return None

            signals = []

            for current_pair in pairs:

                signal = await self.analyze_pair(
                    current_pair
                )

                if signal is not None:
                    signals.append(
                        signal
                    )

            best = self.choose_best(
                signals
            )

            if best is None:

                print(
                    "[MANUAL] Сильный сигнал "
                    "не найден"
                )

                return None

            print(
                "[MANUAL] Лучший сигнал: "
                f"{self.get_value(best, 'pair', '?')}"
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
                "[SCAN] Bot не установлен"
            )

            return None

        async with self.scan_lock:

            print("")
            print("=" * 60)
            print(
                "[SCAN] "
                f"{self.now().strftime('%d.%m.%Y %H:%M:%S')} МСК"
            )
            print("=" * 60)

            pairs = await self.get_available_pairs()

            if not pairs:

                print(
                    "[SCAN] Нет доступных пар"
                )

                return None

            print(
                f"[SCAN] Проверяю "
                f"{len(pairs)} пар"
            )

            signals = []

            for pair in pairs:

                try:

                    signal = await self.analyze_pair(
                        pair
                    )

                    if signal is not None:
                        signals.append(
                            signal
                        )

                except Exception as exc:

                    print(
                        f"[SCAN] {pair}: "
                        f"{exc}"
                    )

            best = self.choose_best(
                signals
            )

            if best is None:

                print(
                    "[SCAN] Сильного сигнала "
                    "не найдено"
                )

                return None

            print(
                "[SCAN] ============================="
            )

            print(
                "[SCAN] НАЙДЕН СИГНАЛ"
            )

            print(
                self.format_signal(
                    best
                )
            )

            print(
                "[SCAN] ============================="
            )

            await self.save_signal(
                best
            )

            await self.send_to_users(
                best
            )

            return best

    # ============================================================
    # SAVE
    # ============================================================

    async def save_signal(
        self,
        signal: Signal,
    ):

        method = getattr(
            db,
            "save_signal",
            None,
        )

        if method is None:
            return

        try:

            data = {
                "pair": self.get_value(
                    signal,
                    "pair",
                    "",
                ),
                "direction": self.get_value(
                    signal,
                    "direction",
                    "",
                ),
                "quality": self.get_value(
                    signal,
                    "quality",
                    0,
                ),
                "probability": self.get_value(
                    signal,
                    "probability",
                    0,
                ),
                "entry_time": self.get_value(
                    signal,
                    "entry_time",
                    None,
                ),
                "expiry_time": self.get_value(
                    signal,
                    "expiry_time",
                    None,
                ),
                "confirmations": self.get_value(
                    signal,
                    "confirmations",
                    [],
                ),
                "reasons": self.get_value(
                    signal,
                    "reasons",
                    [],
                ),
            }

            if isinstance(
                data["confirmations"],
                (list, tuple),
            ):

                data["confirmations"] = ", ".join(
                    map(
                        str,
                        data["confirmations"],
                    )
                )

            if isinstance(
                data["reasons"],
                (list, tuple),
            ):

                data["reasons"] = ", ".join(
                    map(
                        str,
                        data["reasons"],
                    )
                )

            # datetime -> ISO.
            for key in (
                "entry_time",
                "expiry_time",
            ):

                value = data[key]

                if isinstance(
                    value,
                    datetime,
                ):

                    data[key] = value.isoformat()

            try:

                result = method(
                    **data
                )

                if asyncio.iscoroutine(
                    result
                ):
                    await result

            except TypeError:

                # Совместимость со старой БД.
                try:

                    result = method(
                        signal
                    )

                    if asyncio.iscoroutine(
                        result
                    ):
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
    # SEND USERS
    # ============================================================

    async def send_to_users(
        self,
        signal: Signal,
    ):

        if self.bot is None:
            return

        try:

            users = db.get_approved_users()

        except Exception as exc:

            print(
                f"[SEND] Ошибка БД: {exc}"
            )

            return

        if not users:

            print(
                "[SEND] Нет одобренных пользователей"
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
                    chat_id=int(
                        user_id
                    ),
                    text=text,
                )

                sent += 1

            except Exception as exc:

                print(
                    f"[SEND] Ошибка "
                    f"{user}: {exc}"
                )

        print(
            f"[SEND] Отправлено: "
            f"{sent}/{len(users)}"
        )

    # ============================================================
    # FORMAT SIGNAL
    # ============================================================

    def format_signal(
        self,
        signal: Signal,
    ) -> str:

        pair = self.get_value(
            signal,
            "pair",
            "UNKNOWN",
        )

        direction = self.get_value(
            signal,
            "direction",
            "",
        )

        quality = self.get_value(
            signal,
            "quality",
            0,
        )

        probability = self.get_value(
            signal,
            "probability",
            0,
        )

        entry_time = self.get_value(
            signal,
            "entry_time",
            None,
        )

        expiry_time = self.get_value(
            signal,
            "expiry_time",
            None,
        )

        confirmations = self.get_value(
            signal,
            "confirmations",
            [],
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

        # --------------------------------------------------------
        # TIME
        # --------------------------------------------------------

        if isinstance(
            entry_time,
            datetime,
        ):

            entry_text = (
                entry_time.strftime(
                    "%H:%M"
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
                expiry_time.strftime(
                    "%H:%M"
                )
                + " МСК"
            )

        else:

            expiry_text = (
                str(expiry_time)
                if expiry_time
                else "—"
            )

        # --------------------------------------------------------
        # NUMBERS
        # --------------------------------------------------------

        try:

            quality_text = (
                f"{float(quality):.0f}"
            )

        except Exception:

            quality_text = str(
                quality
            )

        try:

            probability_text = (
                f"{float(probability):.0f}%"
            )

        except Exception:

            probability_text = (
                f"{probability}%"
            )

        # --------------------------------------------------------
        # CONFIRMATIONS
        # --------------------------------------------------------

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

        return "\n".join(
            lines
        )

    # ============================================================
    # MAIN LOOP
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
            "режим запущен"
        )

        while self.running:

            try:

                next_time = (
                    self.next_analysis_time()
                )

                now = self.now()

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

                # Защита от повторного запуска.
                await asyncio.sleep(2)

            except asyncio.CancelledError:

                print(
                    "[SCHEDULER] Получена команда "
                    "остановки"
                )

                break

            except Exception as exc:

                print(
                    f"[SCHEDULER] Ошибка: "
                    f"{exc}"
                )

                await asyncio.sleep(
                    10
                )

        self.running = False

        print(
            "[SCHEDULER] Остановлен"
        )

    # ============================================================
    # STOP
    # ============================================================

    def stop(self):

        self.running = False


# Глобальный экземпляр для совместимости
# с другими файлами проекта.
scheduler_instance = SignalScheduler()
