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


# ============================================================
# НАСТРОЙКИ
# ============================================================

# Автоматический анализ каждые 5 минут.
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

    Поддерживает:

    - автоматический анализ;
    - ручной запрос сигнала;
    - анализ конкретной пары;
    - анализ любой доступной пары;
    - фильтр QUALITY;
    - фильтр PROBABILITY;
    - выбор лучшего сигнала;
    - сохранение сигнала в БД;
    - автоматическую отправку APPROVED пользователям;
    - московское время;
    - защиту от одновременного запуска анализа.
    """

    def __init__(self, bot=None):
        self.bot = bot

        self.engine = SignalEngine()

        self.running = False

        # Не позволяет одновременно выполнять
        # автоматический и ручной анализ.
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
        """
        Устанавливает экземпляр Telegram Bot.
        """

        self.bot = bot

    # ============================================================
    # TIME
    # ============================================================

    def now(self) -> datetime:
        """
        Возвращает текущее московское время.
        """

        if MOSCOW_TZ is not None:
            return datetime.now(MOSCOW_TZ)

        return datetime.now()

    def next_analysis_time(
        self,
        interval: int = SIGNAL_INTERVAL_MINUTES,
    ) -> datetime:
        """
        Возвращает ближайшую временную отметку
        для автоматического анализа.

        Например:

        12:01 -> 12:05
        12:04 -> 12:05
        12:05 -> 12:10
        12:09 -> 12:10
        """

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
        Пытается получить динамический список пар
        из market.py.

        Если market.py не предоставляет список,
        используется PAIRS из config.py.

        OTC намеренно исключаются, поскольку обычные
        рыночные котировки Twelve Data не являются
        OTC-котировками Pocket Option.
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

                        # OTC не используем.
                        if "OTC" in pair:
                            continue

                        # Нужен формат EUR/USD.
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

        # --------------------------------------------------------
        # FALLBACK
        # --------------------------------------------------------

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
        Получает свечи.

        Поддерживает разные названия методов market.py,
        чтобы scheduler оставался совместимым
        с разными версиями MarketClient.
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

            # ----------------------------------------------------
            # ПОЗИЦИОННЫЙ ВЫЗОВ
            # ----------------------------------------------------

            try:

                result = method(pair)

                if asyncio.iscoroutine(result):
                    result = await result

                if result is not None:
                    return result

            except TypeError:

                # ------------------------------------------------
                # ИМЕНОВАННЫЙ ВЫЗОВ
                # ------------------------------------------------

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
        """
        Анализирует одну пару.

        Сигнал будет возвращён только если:

        QUALITY >= MIN_QUALITY

        и

        PROBABILITY >= MIN_PROBABILITY
        """

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

            # ----------------------------------------------------
            # SIGNAL ENGINE API COMPATIBILITY
            # ----------------------------------------------------

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

            # ----------------------------------------------------
            # QUALITY
            # ----------------------------------------------------

            quality = self.get_value(
                signal,
                "quality",
                0,
            )

            # ----------------------------------------------------
            # PROBABILITY
            # ----------------------------------------------------

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

            # ====================================================
            # DIRECTION
            # ====================================================

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
        """
        Универсальное получение значения
        из dict или объекта.
        """

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
        """
        Выбирает самый сильный сигнал.

        Приоритет:

        1. QUALITY
        2. PROBABILITY
        """

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
        """
        Ручной запрос сигнала.

        Если pair указан:
            анализируется только эта пара.

        Если pair отсутствует:
            анализируются все доступные пары,
            после чего выбирается лучшая.
        """

        async with self.scan_lock:

            # ----------------------------------------------------
            # КОНКРЕТНАЯ ПАРА
            # ----------------------------------------------------

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

            # ----------------------------------------------------
            # ЛЮБАЯ ПАРА
            # ----------------------------------------------------

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
        """
        Один полный автоматический проход.

        Анализируются все доступные пары.
        Из них выбирается лучший сигнал.

        Если сильного сигнала нет —
        пользователям ничего не отправляется.

        Если сигнал есть —
        он сохраняется в БД и отправляется
        APPROVED пользователям.
        """

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

            # ----------------------------------------------------
            # SAVE
            # ----------------------------------------------------

            await self.save_signal(
                best
            )

            # ----------------------------------------------------
            # SEND
            # ----------------------------------------------------

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
        """
        Сохраняет сигнал в БД.

        Поддерживает новую и старую сигнатуру
        db.save_signal().
        """

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

            # ----------------------------------------------------
            # CONFIRMATIONS
            # ----------------------------------------------------

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

            # ----------------------------------------------------
            # REASONS
            # ----------------------------------------------------

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

            # ----------------------------------------------------
            # DATETIME
            # ----------------------------------------------------

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

            # ----------------------------------------------------
            # NEW DATABASE API
            # ----------------------------------------------------

            try:

                result = method(
                    **data
                )

                if asyncio.iscoroutine(
                    result
                ):

                    await result

            except TypeError:

                # ------------------------------------------------
                # OLD DATABASE API
                # ------------------------------------------------

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
        """
        Отправляет сигнал всем одобренным пользователям.

        Важно:
        автоматически отправляются только пользователям,
        которых возвращает db.get_approved_users().
        """

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
        """
        Формат Telegram-сообщения сигнала.

        Пример:

        🔴 PUT ↓

        💱 USD/JPY

        ⏰ ВХОД: 00:00 МСК
        🎯 ЭКСПИРАЦИЯ: 00:05 МСК

        📊 QUALITY: 90/100
        📈 ШАНС: 87%

        ✅ EMA тренд вниз
        ✅ Импульс вниз
        ✅ MACD подтверждает PUT
        """

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

        # ========================================================
        # DIRECTION
        # ========================================================

        if direction in {
            "CALL",
            "UP",
        }:

            emoji = "🟢"
            direction_text = "CALL ↑"

        else:

            emoji = "🔴"
            direction_text = "PUT ↓"

        # ========================================================
        # ENTRY TIME
        # ========================================================

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

        # ========================================================
        # EXPIRY TIME
        # ========================================================

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

        # ========================================================
        # QUALITY
        # ========================================================

        try:

            quality_text = (
                f"{float(quality):.0f}"
            )

        except Exception:

            quality_text = str(
                quality
            )

        # ========================================================
        # PROBABILITY
        # ========================================================

        try:

            probability_text = (
                f"{float(probability):.0f}%"
            )

        except Exception:

            probability_text = (
                f"{probability}%"
            )

        # ========================================================
        # CONFIRMATIONS
        # ========================================================

        confirmation_lines = []

        if isinstance(
            confirmations,
            (list, tuple),
        ):

            for confirmation in confirmations:

                confirmation = str(
                    confirmation
                ).strip()

                if not confirmation:
                    continue

                # Если движок уже добавил emoji,
                # не добавляем второй.
                if confirmation.startswith("✅"):

                    confirmation_lines.append(
                        confirmation
                    )

                else:

                    confirmation_lines.append(
                        f"✅ {confirmation}"
                    )

        elif confirmations:

            confirmation = str(
                confirmations
            ).strip()

            if confirmation:

                # Поддержка старого формата,
                # когда подтверждения могли приходить
                # одной строкой через запятую.
                if "," in confirmation:

                    for item in confirmation.split(","):

                        item = item.strip()

                        if not item:
                            continue

                        if item.startswith("✅"):

                            confirmation_lines.append(
                                item
                            )

                        else:

                            confirmation_lines.append(
                                f"✅ {item}"
                            )

                else:

                    if confirmation.startswith("✅"):

                        confirmation_lines.append(
                            confirmation
                        )

                    else:

                        confirmation_lines.append(
                            f"✅ {confirmation}"
                        )

        # ========================================================
        # MESSAGE
        # ========================================================

        lines = [
            f"{emoji} {direction_text}",
            "",
            f"💱 {pair}",
            "",
            f"⏰ ВХОД: {entry_text}",
            f"🎯 ЭКСПИРАЦИЯ: {expiry_text}",
            "",
            f"📊 QUALITY: {quality_text}/100",
            f"📈 ШАНС: {probability_text}",
        ]

        if confirmation_lines:

            lines.extend(
                [
                    "",
                    *confirmation_lines,
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
        """
        Основной автоматический цикл.

        Сигналы проверяются каждые 5 минут:

        00:00
        00:05
        00:10
        00:15
        ...

        Если сильного сигнала нет,
        ничего пользователям не отправляется.
        """

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

                # ------------------------------------------------
                # АВТОМАТИЧЕСКИЙ СКАН
                # ------------------------------------------------

                await self.scan_once()

                # ------------------------------------------------
                # Защита от мгновенного повторного запуска.
                # ------------------------------------------------

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
        """
        Останавливает автоматический цикл.
        """

        self.running = False
