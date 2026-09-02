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


# Включать OTC-пары.
ENABLE_OTC = True


# Московское время.
try:
    from zoneinfo import ZoneInfo

    MOSCOW_TZ = ZoneInfo("Europe/Moscow")
except Exception:
    MOSCOW_TZ = None


# ============================================================
# FALLBACK OTC PAIRS
# ============================================================
#
# Эти пары используются только если market.py не смог
# вернуть динамический список активов.
#
# Для запроса в market.py сохраняется формат без "/" —
# это наиболее распространённый формат Pocket Option:
#
# EURUSD_otc
# GBPUSD_otc
# USDJPY_otc
#
# Если market.py возвращает собственный список OTC,
# используются именно его названия.
# ============================================================

OTC_PAIRS = [
    "EURUSD_otc",
    "GBPUSD_otc",
    "USDJPY_otc",
    "USDCHF_otc",
    "AUDUSD_otc",
    "USDCAD_otc",
    "NZDUSD_otc",
    "EURGBP_otc",
    "EURJPY_otc",
    "GBPJPY_otc",
    "AUDCAD_otc",
    "AUDCHF_otc",
    "AUDJPY_otc",
    "CADCHF_otc",
    "CADJPY_otc",
    "CHFJPY_otc",
    "EURAUD_otc",
    "EURCAD_otc",
    "EURCHF_otc",
    "EURNZD_otc",
    "GBPAUD_otc",
    "GBPCAD_otc",
    "GBPCHF_otc",
    "GBPNZD_otc",
    "NZDCAD_otc",
    "NZDCHF_otc",
    "NZDJPY_otc",
]


class SignalScheduler:
    """
    Планировщик сигналов.

    Поддерживает:

    - автоматический анализ;
    - ручной запрос сигнала;
    - анализ конкретной пары;
    - анализ любой доступной пары;
    - обычные пары;
    - OTC-пары;
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
            f"[SCHEDULER] MIN_QUALITY = "
            f"{MIN_QUALITY}"
        )

        print(
            f"[SCHEDULER] MIN_PROBABILITY = "
            f"{MIN_PROBABILITY}%"
        )

        print(
            f"[SCHEDULER] OTC = "
            f"{'ON' if ENABLE_OTC else 'OFF'}"
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
    # PAIR HELPERS
    # ============================================================

    @staticmethod
    def is_otc_pair(
        pair: str,
    ) -> bool:
        """
        Определяет, является ли актив OTC.

        Поддерживает варианты:

        EURUSD_otc
        EUR/USD_otc
        EUR/USD OTC
        EURUSD-OTC
        EURUSD OTC
        """

        if not pair:
            return False

        normalized = (
            str(pair)
            .strip()
            .upper()
            .replace("-", "_")
        )

        return (
            "_OTC" in normalized
            or " OTC" in normalized
            or normalized.endswith("OTC")
        )

    @staticmethod
    def normalize_pair(
        pair: str,
    ) -> str:
        """
        Нормализует имя пары, но НЕ меняет OTC
        в обычную пару.

        Важно:
        для market.py желательно передавать
        исходное имя актива.
        """

        if not pair:
            return ""

        pair = str(
            pair
        ).strip()

        return pair.upper()

    @staticmethod
    def display_pair(
        pair: str,
    ) -> str:
        """
        Красивое отображение пары в Telegram.

        Примеры:

        EURUSD      -> EUR/USD
        EURUSD_otc  -> EUR/USD OTC
        EUR/USD     -> EUR/USD
        EUR/USD OTC -> EUR/USD OTC
        """

        if not pair:
            return "UNKNOWN"

        raw = str(
            pair
        ).strip().upper()

        # --------------------------------------------------------
        # OTC
        # --------------------------------------------------------

        is_otc = SignalScheduler.is_otc_pair(
            raw
        )

        cleaned = (
            raw
            .replace("-OTC", "")
            .replace("_OTC", "")
            .replace(" OTC", "")
            .replace("OTC", "")
            .strip()
        )

        # --------------------------------------------------------
        # Уже есть /
        # --------------------------------------------------------

        if "/" in cleaned:

            result = cleaned

        # --------------------------------------------------------
        # EURUSD -> EUR/USD
        # --------------------------------------------------------

        elif len(cleaned) >= 6:

            result = (
                cleaned[:3]
                + "/"
                + cleaned[3:6]
            )

        else:

            result = cleaned

        if is_otc:
            result += " OTC"

        return result

    # ============================================================
    # AVAILABLE PAIRS
    # ============================================================

    async def get_available_pairs(
        self,
    ) -> list[str]:
        """
        Получает список доступных пар.

        ВАЖНО:

        OTC теперь НЕ исключаются.

        Если market.py возвращает OTC,
        они попадут в список.

        Если market.py не возвращает список,
        используются PAIRS + OTC_PAIRS.

        Исходное имя OTC сохраняется,
        чтобы market.py получил правильный символ.
        """

        dynamic_pairs: list[str] = []

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

                    for pair in result:

                        if not pair:
                            continue

                        normalized = (
                            self.normalize_pair(
                                pair
                            )
                        )

                        if not normalized:
                            continue

                        is_otc = (
                            self.is_otc_pair(
                                normalized
                            )
                        )

                        # ----------------------------------------
                        # OTC
                        # ----------------------------------------

                        if is_otc:

                            if not ENABLE_OTC:
                                continue

                            if normalized not in dynamic_pairs:

                                dynamic_pairs.append(
                                    normalized
                                )

                            continue

                        # ----------------------------------------
                        # ОБЫЧНЫЕ ПАРЫ
                        # ----------------------------------------

                        # Обычная forex-пара должна иметь
                        # либо slash, либо 6 символов.
                        if (
                            "/" not in normalized
                            and len(normalized) < 6
                        ):
                            continue

                        if normalized not in dynamic_pairs:

                            dynamic_pairs.append(
                                normalized
                            )

                    if dynamic_pairs:

                        otc_count = sum(
                            1
                            for pair in dynamic_pairs
                            if self.is_otc_pair(pair)
                        )

                        regular_count = (
                            len(dynamic_pairs)
                            - otc_count
                        )

                        print(
                            "[PAIRS] "
                            f"Динамически найдено: "
                            f"{len(dynamic_pairs)}"
                        )

                        print(
                            "[PAIRS] Обычных: "
                            f"{regular_count}"
                        )

                        print(
                            "[PAIRS] OTC: "
                            f"{otc_count}"
                        )

                        if otc_count > 0:

                            print(
                                "[PAIRS] OTC активы:"
                            )

                            for otc_pair in dynamic_pairs:

                                if self.is_otc_pair(
                                    otc_pair
                                ):

                                    print(
                                        "  - "
                                        f"{otc_pair}"
                                    )

                        return dynamic_pairs

        except Exception as exc:

            print(
                "[PAIRS] Ошибка динамического "
                f"получения: {exc}"
            )

        # ========================================================
        # FALLBACK
        # ========================================================

        fallback: list[str] = []

        # --------------------------------------------------------
        # Обычные пары из config.py
        # --------------------------------------------------------

        for pair in PAIRS:

            normalized = (
                self.normalize_pair(
                    pair
                )
            )

            if not normalized:
                continue

            if self.is_otc_pair(
                normalized
            ):

                if not ENABLE_OTC:
                    continue

            else:

                if (
                    "/" not in normalized
                    and len(normalized) < 6
                ):
                    continue

            if normalized not in fallback:

                fallback.append(
                    normalized
                )

        # --------------------------------------------------------
        # Добавляем OTC
        # --------------------------------------------------------

        if ENABLE_OTC:

            for pair in OTC_PAIRS:

                normalized = (
                    self.normalize_pair(
                        pair
                    )
                )

                if (
                    normalized
                    and normalized not in fallback
                ):

                    fallback.append(
                        normalized
                    )

        otc_count = sum(
            1
            for pair in fallback
            if self.is_otc_pair(pair)
        )

        regular_count = (
            len(fallback)
            - otc_count
        )

        print(
            "[PAIRS] Использую fallback:"
        )

        print(
            f"[PAIRS] Всего: {len(fallback)}"
        )

        print(
            f"[PAIRS] Обычных: {regular_count}"
        )

        print(
            f"[PAIRS] OTC: {otc_count}"
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

        OTC передаётся в market.py именно в том формате,
        в котором он пришёл из списка доступных активов.
        """

        pair = self.normalize_pair(
            pair
        )

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

                result = method(
                    pair
                )

                if asyncio.iscoroutine(
                    result
                ):
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

                    if asyncio.iscoroutine(
                        result
                    ):
                        result = await result

                    if result is not None:
                        return result

                except Exception as exc:

                    print(
                        f"[MARKET] {pair}: "
                        f"{method_name} "
                        f"symbol= ошибка: {exc}"
                    )

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

        pair = self.normalize_pair(
            pair
        )

        display_name = self.display_pair(
            pair
        )

        is_otc = self.is_otc_pair(
            pair
        )

        try:

            print(
                "[ANALYZE] "
                f"{display_name}"
                f"{' [OTC]' if is_otc else ''}"
            )

            candles = await self.get_candles(
                pair
            )

            if candles is None:

                print(
                    f"[ANALYZE] {display_name}: "
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
                    f"[ANALYZE] {display_name}: "
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

                    if asyncio.iscoroutine(
                        signal
                    ):
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
                        f"[ENGINE] {display_name}: "
                        f"{exc}"
                    )

                    return None

            if signal is None:

                print(
                    f"[ANALYZE] {display_name}: "
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
                f"[ANALYZE] {display_name}: "
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
                    f"[REJECT] {display_name}: "
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
                    f"[REJECT] {display_name}: "
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
                    f"[REJECT] {display_name}: "
                    f"неизвестное направление "
                    f"{direction}"
                )

                return None

            return signal

        except Exception as exc:

            print(
                f"[ANALYZE] {display_name}: "
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
            включая OTC.
        """

        async with self.scan_lock:

            # ----------------------------------------------------
            # КОНКРЕТНАЯ ПАРА
            # ----------------------------------------------------

            if pair:

                pair = self.normalize_pair(
                    pair
                )

                print(
                    "[MANUAL] Проверяю "
                    f"{self.display_pair(pair)}"
                )

                return await self.analyze_pair(
                    pair
                )

            # ----------------------------------------------------
            # ЛЮБАЯ ПАРА
            # ----------------------------------------------------

            print(
                "[MANUAL] Проверяю все "
                "доступные пары, включая OTC"
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
                f"{self.display_pair(self.get_value(best, 'pair', '?'))}"
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

        Анализируются все доступные пары,
        включая OTC.

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

            otc_count = sum(
                1
                for pair in pairs
                if self.is_otc_pair(pair)
            )

            regular_count = (
                len(pairs)
                - otc_count
            )

            print(
                "[SCAN] Проверяю "
                f"{len(pairs)} пар"
            )

            print(
                "[SCAN] Обычных: "
                f"{regular_count}"
            )

            print(
                "[SCAN] OTC: "
                f"{otc_count}"
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

        Автоматически отправляются только пользователям,
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
        """

        raw_pair = self.get_value(
            signal,
            "pair",
            "UNKNOWN",
        )

        pair = self.display_pair(
            raw_pair
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

            # Если datetime пришёл без timezone,
            # считаем его московским.
            if entry_time.tzinfo is None:

                entry_time = entry_time.replace(
                    tzinfo=MOSCOW_TZ
                    if MOSCOW_TZ is not None
                    else None
                )

            elif MOSCOW_TZ is not None:

                entry_time = entry_time.astimezone(
                    MOSCOW_TZ
                )

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

            if expiry_time.tzinfo is None:

                expiry_time = expiry_time.replace(
                    tzinfo=MOSCOW_TZ
                    if MOSCOW_TZ is not None
                    else None
                )

            elif MOSCOW_TZ is not None:

                expiry_time = expiry_time.astimezone(
                    MOSCOW_TZ
                )

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

                if confirmation.startswith(
                    "✅"
                ):

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

                if "," in confirmation:

                    for item in confirmation.split(
                        ","
                    ):

                        item = item.strip()

                        if not item:
                            continue

                        if item.startswith(
                            "✅"
                        ):

                            confirmation_lines.append(
                                item
                            )

                        else:

                            confirmation_lines.append(
                                f"✅ {item}"
                            )

                else:

                    if confirmation.startswith(
                        "✅"
                    ):

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

        Проверяются как обычные пары,
        так и OTC.

        Если сильного сигнала нет,
        пользователям ничего не отправляется.
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
