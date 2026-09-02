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

from market import (
    market_client,
    OTC_PAIRS,
)

from signal_engine import (
    SignalEngine,
    Signal,
)


# ============================================================
# SETTINGS
# ============================================================

SIGNAL_INTERVAL_MINUTES = 5

ENABLE_OTC = True

MIN_CANDLES_FOR_ANALYSIS = 50


# ============================================================
# TIMEZONE
# ============================================================

try:
    from zoneinfo import ZoneInfo

    MOSCOW_TZ = ZoneInfo(
        "Europe/Moscow"
    )

except Exception:
    MOSCOW_TZ = None


class SignalScheduler:

    def __init__(
        self,
        bot=None,
    ):

        self.bot = bot

        self.engine = SignalEngine()

        self.running = False

        self.scan_lock = asyncio.Lock()

        print(
            "[SCHEDULER] Инициализирован"
        )

        print(
            "[SCHEDULER] MIN_QUALITY = "
            f"{MIN_QUALITY}"
        )

        print(
            "[SCHEDULER] MIN_PROBABILITY = "
            f"{MIN_PROBABILITY}%"
        )

        print(
            "[SCHEDULER] OTC = "
            f"{'ON' if ENABLE_OTC else 'OFF'}"
        )

    # ============================================================
    # BOT
    # ============================================================

    def set_bot(
        self,
        bot,
    ):
        self.bot = bot

    # ============================================================
    # TIME
    # ============================================================

    def now(self) -> datetime:

        if MOSCOW_TZ is not None:
            return datetime.now(
                MOSCOW_TZ
            )

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

        remainder = (
            current.minute
            % interval
        )

        if remainder == 0:

            return (
                current
                + timedelta(
                    minutes=interval
                )
            )

        return (
            current
            + timedelta(
                minutes=(
                    interval
                    - remainder
                )
            )
        )

    # ============================================================
    # PAIR HELPERS
    # ============================================================

    @staticmethod
    def is_otc_pair(
        pair: str,
    ) -> bool:

        if not pair:
            return False

        value = (
            str(pair)
            .strip()
            .lower()
        )

        return (
            value.endswith("_otc")
            or value.endswith("-otc")
            or value.endswith(" otc")
            or value.endswith("otc")
        )

    @staticmethod
    def normalize_pair(
        pair: str,
    ) -> str:

        if not pair:
            return ""

        return str(
            pair
        ).strip()

    @staticmethod
    def display_pair(
        pair: str,
    ) -> str:

        if not pair:
            return "UNKNOWN"

        raw = (
            str(pair)
            .strip()
            .upper()
        )

        otc = (
            SignalScheduler.is_otc_pair(
                raw
            )
        )

        cleaned = raw

        for suffix in (
            "_OTC",
            "-OTC",
            " OTC",
            "OTC",
        ):

            if cleaned.endswith(
                suffix
            ):

                cleaned = cleaned[
                    :-len(suffix)
                ]

                break

        cleaned = cleaned.strip()

        if (
            len(cleaned) == 6
            and cleaned.isalpha()
        ):

            result = (
                cleaned[:3]
                + "/"
                + cleaned[3:]
            )

        else:

            result = cleaned

        if otc:
            result += " OTC"

        return result

    # ============================================================
    # AVAILABLE PAIRS
    # ============================================================

    async def get_available_pairs(
        self,
    ) -> list[str]:

        result: list[str] = []

        # --------------------------------------------------------
        # MARKET CLIENT
        # --------------------------------------------------------

        try:

            method = getattr(
                market_client,
                "get_available_pairs",
                None,
            )

            if callable(method):

                pairs = method()

                if asyncio.iscoroutine(
                    pairs
                ):
                    pairs = await pairs

                if pairs:

                    for pair in pairs:

                        normalized = (
                            self.normalize_pair(
                                pair
                            )
                        )

                        if not normalized:
                            continue

                        if (
                            self.is_otc_pair(
                                normalized
                            )
                            and not ENABLE_OTC
                        ):
                            continue

                        if normalized not in result:
                            result.append(
                                normalized
                            )

        except Exception as exc:

            print(
                "[PAIRS] Ошибка: "
                f"{exc}"
            )

        # --------------------------------------------------------
        # NORMAL FALLBACK
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
                continue

            if normalized not in result:
                result.append(
                    normalized
                )

        # --------------------------------------------------------
        # OTC FALLBACK
        # --------------------------------------------------------

        if ENABLE_OTC:

            for pair in OTC_PAIRS:

                normalized = (
                    self.normalize_pair(
                        pair
                    )
                )

                if not normalized:
                    continue

                if not self.is_otc_pair(
                    normalized
                ):
                    continue

                if normalized not in result:
                    result.append(
                        normalized
                    )

        # --------------------------------------------------------
        # FINAL
        # --------------------------------------------------------

        regular_count = sum(
            1
            for pair in result
            if not self.is_otc_pair(
                pair
            )
        )

        otc_count = sum(
            1
            for pair in result
            if self.is_otc_pair(
                pair
            )
        )

        print(
            "[PAIRS] Всего: "
            f"{len(result)}"
        )

        print(
            "[PAIRS] Обычных: "
            f"{regular_count}"
        )

        print(
            "[PAIRS] OTC: "
            f"{otc_count}"
        )

        return result

    # ============================================================
    # TYPE FILTERS
    # ============================================================

    async def get_regular_pairs(
        self,
    ) -> list[str]:

        pairs = (
            await self.get_available_pairs()
        )

        return [
            pair
            for pair in pairs
            if not self.is_otc_pair(
                pair
            )
        ]

    async def get_otc_pairs(
        self,
    ) -> list[str]:

        if not ENABLE_OTC:
            return []

        pairs = (
            await self.get_available_pairs()
        )

        return [
            pair
            for pair in pairs
            if self.is_otc_pair(
                pair
            )
        ]

    # ============================================================
    # CANDLES
    # ============================================================

    async def get_candles(
        self,
        pair: str,
    ):

        if not pair:
            return None

        pair = self.normalize_pair(
            pair
        )

        # --------------------------------------------------------
        # Main method
        # --------------------------------------------------------

        method = getattr(
            market_client,
            "get_candles",
            None,
        )

        if callable(method):

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

            except Exception as exc:

                print(
                    f"[MARKET] "
                    f"{self.display_pair(pair)}: "
                    f"{exc}"
                )

        # --------------------------------------------------------
        # Compatibility
        # --------------------------------------------------------

        for method_name in (
            "fetch_candles",
            "get_history",
            "get_data",
        ):

            method = getattr(
                market_client,
                method_name,
                None,
            )

            if not callable(method):
                continue

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

            except Exception as exc:

                print(
                    f"[MARKET] "
                    f"{self.display_pair(pair)} "
                    f"{method_name}: "
                    f"{exc}"
                )

        return None

    # ============================================================
    # ANALYZE
    # ============================================================

    async def analyze_pair(
        self,
        pair: str,
    ) -> Signal | None:

        pair = self.normalize_pair(
            pair
        )

        display = self.display_pair(
            pair
        )

        try:

            print(
                "[ANALYZE] "
                f"{display}"
            )

            candles = await self.get_candles(
                pair
            )

            if candles is None:

                print(
                    f"[REJECT] {display}: "
                    "нет свечей"
                )

                return None

            try:
                candle_count = len(
                    candles
                )
            except Exception:
                candle_count = 0

            if (
                candle_count
                < MIN_CANDLES_FOR_ANALYSIS
            ):

                print(
                    f"[REJECT] {display}: "
                    f"мало свечей "
                    f"{candle_count}/"
                    f"{MIN_CANDLES_FOR_ANALYSIS}"
                )

                return None

            signal = None

            # ====================================================
            # ENGINE
            # ====================================================

            method = getattr(
                self.engine,
                "analyze",
                None,
            )

            if callable(method):

                # ------------------------------------------------
                # Current engine format:
                #
                # analyze(candles, pair)
                # ------------------------------------------------

                try:

                    signal = method(
                        candles,
                        pair,
                    )

                    if asyncio.iscoroutine(
                        signal
                    ):
                        signal = await signal

                except TypeError:

                    signal = None

                except Exception as exc:

                    print(
                        f"[ENGINE] {display}: "
                        f"{exc}"
                    )

                    return None

                # ------------------------------------------------
                # Compatibility:
                # analyze(candles)
                # ------------------------------------------------

                if signal is None:

                    try:

                        signal = method(
                            candles
                        )

                        if asyncio.iscoroutine(
                            signal
                        ):
                            signal = await signal

                    except TypeError:

                        signal = None

                    except Exception as exc:

                        print(
                            f"[ENGINE] "
                            f"{display}: "
                            f"{exc}"
                        )

                        return None

            # ====================================================
            # OTHER ENGINE METHODS
            # ====================================================

            if signal is None:

                for method_name in (
                    "generate_signal",
                    "get_signal",
                ):

                    method = getattr(
                        self.engine,
                        method_name,
                        None,
                    )

                    if not callable(method):
                        continue

                    try:

                        signal = method(
                            candles,
                            pair,
                        )

                        if asyncio.iscoroutine(
                            signal
                        ):
                            signal = await signal

                    except TypeError:

                        try:

                            signal = method(
                                candles
                            )

                            if asyncio.iscoroutine(
                                signal
                            ):
                                signal = await signal

                        except Exception:
                            signal = None

                    except Exception as exc:

                        print(
                            f"[ENGINE] "
                            f"{display}: "
                            f"{exc}"
                        )

                        signal = None

                    if signal is not None:
                        break

            if signal is None:

                print(
                    f"[REJECT] {display}: "
                    "SignalEngine "
                    "не сформировал сигнал"
                )

                return None

            # ====================================================
            # QUALITY
            # ====================================================

            quality = self.get_value(
                signal,
                "quality",
                0,
            )

            try:

                quality = float(
                    quality
                )

            except Exception:

                quality = 0.0

            # ====================================================
            # PROBABILITY
            # ====================================================

            probability = self.get_value(
                signal,
                "probability",
                0,
            )

            try:

                probability = float(
                    probability
                )

            except Exception:

                probability = 0.0

            # ====================================================
            # DIRECTION
            # ====================================================

            direction = self.get_value(
                signal,
                "direction",
                "",
            )

            direction = str(
                direction
            ).upper()

            print(
                "[ANALYZE] "
                f"{display}: "
                f"Q={quality:.1f} "
                f"P={probability:.1f}% "
                f"D={direction}"
            )

            # ====================================================
            # QUALITY FILTER
            # ====================================================

            if quality < float(
                MIN_QUALITY
            ):

                print(
                    f"[REJECT] {display}: "
                    f"QUALITY "
                    f"{quality:.1f} < "
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
                    f"[REJECT] {display}: "
                    f"PROBABILITY "
                    f"{probability:.1f}% < "
                    f"{MIN_PROBABILITY}%"
                )

                return None

            # ====================================================
            # DIRECTION FILTER
            # ====================================================

            if direction not in {
                "CALL",
                "PUT",
                "UP",
                "DOWN",
            }:

                print(
                    f"[REJECT] {display}: "
                    "неверное направление "
                    f"{direction}"
                )

                return None

            return signal

        except Exception as exc:

            print(
                f"[ANALYZE] {display}: "
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
                quality = 0.0

            try:
                probability = float(
                    probability
                )
            except Exception:
                probability = 0.0

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
    # SCAN
    # ============================================================

    async def scan_pairs(
        self,
        pairs: list[str],
    ) -> Signal | None:

        if not pairs:
            return None

        signals: list[Signal] = []

        for pair in pairs:

            try:

                signal = (
                    await self.analyze_pair(
                        pair
                    )
                )

                if signal is not None:
                    signals.append(
                        signal
                    )

            except Exception as exc:

                print(
                    f"[SCAN] "
                    f"{self.display_pair(pair)}: "
                    f"{exc}"
                )

        return self.choose_best(
            signals
        )

    # ============================================================
    # MANUAL SIGNAL
    # ============================================================

    async def get_manual_signal(
        self,
        pair: str | None = None,
    ) -> Signal | None:

        async with self.scan_lock:

            # ----------------------------------------------------
            # SPECIFIC PAIR
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
            # ALL
            # ----------------------------------------------------

            print(
                "[MANUAL] "
                "Проверяю обычные + OTC"
            )

            pairs = (
                await self.get_available_pairs()
            )

            if not pairs:

                return None

            return await self.scan_pairs(
                pairs
            )

    # ============================================================
    # SIGNAL BY TYPE
    # ============================================================

    async def get_manual_signal_by_type(
        self,
        signal_type: str,
    ) -> Signal | None:

        async with self.scan_lock:

            signal_type = str(
                signal_type
            ).strip().lower()

            if signal_type == "regular":

                pairs = (
                    await self.get_regular_pairs()
                )

            elif signal_type == "otc":

                pairs = (
                    await self.get_otc_pairs()
                )

            else:

                pairs = (
                    await self.get_available_pairs()
                )

            if not pairs:
                return None

            return await self.scan_pairs(
                pairs
            )

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

            now = self.now()

            print("")
            print("=" * 60)

            print(
                "[SCAN] "
                f"{now.strftime('%d.%m.%Y %H:%M:%S')} МСК"
            )

            print("=" * 60)

            pairs = (
                await self.get_available_pairs()
            )

            if not pairs:

                print(
                    "[SCAN] "
                    "Нет доступных пар"
                )

                return None

            regular_count = sum(
                1
                for pair in pairs
                if not self.is_otc_pair(
                    pair
                )
            )

            otc_count = sum(
                1
                for pair in pairs
                if self.is_otc_pair(
                    pair
                )
            )

            print(
                "[SCAN] Обычных: "
                f"{regular_count}"
            )

            print(
                "[SCAN] OTC: "
                f"{otc_count}"
            )

            best = await self.scan_pairs(
                pairs
            )

            if best is None:

                print(
                    "[SCAN] "
                    "Сильного сигнала "
                    "не найдено"
                )

                return None

            print(
                "[SCAN] НАЙДЕН СИГНАЛ"
            )

            print(
                self.format_signal(
                    best
                )
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

        if not callable(method):
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
                (
                    list,
                    tuple,
                ),
            ):

                data["confirmations"] = (
                    ", ".join(
                        map(
                            str,
                            data["confirmations"],
                        )
                    )
                )

            if isinstance(
                data["reasons"],
                (
                    list,
                    tuple,
                ),
            ):

                data["reasons"] = (
                    ", ".join(
                        map(
                            str,
                            data["reasons"],
                        )
                    )
                )

            for key in (
                "entry_time",
                "expiry_time",
            ):

                value = data[key]

                if isinstance(
                    value,
                    datetime,
                ):

                    data[key] = (
                        value.isoformat()
                    )

            try:

                result = method(
                    **data
                )

                if asyncio.iscoroutine(
                    result
                ):
                    await result

            except TypeError:

                result = method(
                    signal
                )

                if asyncio.iscoroutine(
                    result
                ):
                    await result

        except Exception as exc:

            print(
                "[DB] Ошибка сохранения: "
                f"{exc}"
            )

    # ============================================================
    # SEND
    # ============================================================

    async def send_to_users(
        self,
        signal: Signal,
    ):

        if self.bot is None:
            return

        try:

            users = db.get_approved_users()

            if asyncio.iscoroutine(
                users
            ):
                users = await users

        except Exception as exc:

            print(
                "[SEND] Ошибка БД: "
                f"{exc}"
            )

            return

        if not users:
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
                    f"[SEND] Ошибка: "
                    f"{exc}"
                )

        print(
            "[SEND] Отправлено: "
            f"{sent}/{len(users)}"
        )

    # ============================================================
    # FORMAT
    # ============================================================

    def format_signal(
        self,
        signal: Signal,
    ) -> str:

        pair = self.display_pair(
            self.get_value(
                signal,
                "pair",
                "UNKNOWN",
            )
        )

        direction = str(
            self.get_value(
                signal,
                "direction",
                "",
            )
        ).upper()

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

        # --------------------------------------------------------
        # DIRECTION
        # --------------------------------------------------------

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

        def format_time(
            value,
        ) -> str:

            if isinstance(
                value,
                datetime,
            ):

                if value.tzinfo is None:

                    if MOSCOW_TZ is not None:
                        value = value.replace(
                            tzinfo=MOSCOW_TZ
                        )

                elif MOSCOW_TZ is not None:

                    value = value.astimezone(
                        MOSCOW_TZ
                    )

                return (
                    value.strftime(
                        "%H:%M"
                    )
                    + " МСК"
                )

            if value:
                return str(value)

            return "—"

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

        confirmation_lines = []

        if isinstance(
            confirmations,
            (
                list,
                tuple,
            ),
        ):

            for item in confirmations:

                item = str(
                    item
                ).strip()

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

        elif confirmations:

            text = str(
                confirmations
            ).strip()

            for item in text.split(
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

        # --------------------------------------------------------
        # MESSAGE
        # --------------------------------------------------------

        lines = [
            f"{emoji} {direction_text}",
            "",
            f"💱 {pair}",
            "",
            f"⏰ ВХОД: "
            f"{format_time(entry_time)}",
            f"🎯 ЭКСПИРАЦИЯ: "
            f"{format_time(expiry_time)}",
            "",
            f"📊 QUALITY: "
            f"{quality_text}/100",
            f"📈 ШАНС: "
            f"{probability_text}",
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
    # RUN
    # ============================================================

    async def run(
        self,
        bot=None,
    ):

        if bot is not None:
            self.bot = bot

        self.running = True

        print(
            "[SCHEDULER] "
            "Автоматический режим запущен"
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
                    "[SCHEDULER] "
                    "Следующий анализ: "
                    f"{next_time.strftime('%H:%M:%S')} МСК"
                )

                await asyncio.sleep(
                    wait_seconds
                )

                if not self.running:
                    break

                await self.scan_once()

                await asyncio.sleep(
                    2
                )

            except asyncio.CancelledError:

                break

            except Exception as exc:

                print(
                    "[SCHEDULER] Ошибка: "
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
