from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from zoneinfo import ZoneInfo

from config import (
    PAIRS,
    MIN_PROBABILITY,
    MIN_QUALITY,
)

from database import db
from market import market_client
from signal_engine import (
    SignalEngine,
    Signal,
)


# ================================================================
# SETTINGS
# ================================================================

SIGNAL_INTERVAL_MINUTES = 5

MOSCOW_TZ = ZoneInfo(
    "Europe/Moscow"
)

MIN_CANDLES = 80

MARKET_LIMIT = 120

MAX_REASONS = 8


# ================================================================
# SCHEDULER
# ================================================================

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
            f"[SCHEDULER] MIN_QUALITY = "
            f"{MIN_QUALITY}"
        )

        print(
            f"[SCHEDULER] MIN_PROBABILITY = "
            f"{MIN_PROBABILITY}%"
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

    def now(
        self,
    ) -> datetime:

        return datetime.now(
            MOSCOW_TZ
        )

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
    # AVAILABLE PAIRS
    # ============================================================

    async def get_available_pairs(
        self,
    ) -> list[str]:

        """
        Получаем список из market.py.

        Если список недоступен —
        используем PAIRS из config.py.
        """

        try:

            method = getattr(
                market_client,
                "get_available_pairs",
                None,
            )

            if method is not None:

                result = method()

                if asyncio.iscoroutine(
                    result
                ):
                    result = await result

                if result:

                    cleaned = []

                    for pair in result:

                        if not pair:
                            continue

                        pair = str(
                            pair
                        ).strip().upper()

                        if pair not in cleaned:
                            cleaned.append(
                                pair
                            )

                    if cleaned:

                        print(
                            "[PAIRS] "
                            f"Доступно: "
                            f"{len(cleaned)}"
                        )

                        return cleaned

        except Exception as exc:

            print(
                "[PAIRS] Ошибка получения: "
                f"{exc}"
            )

        fallback = []

        for pair in PAIRS:

            pair = str(
                pair
            ).strip().upper()

            if not pair:
                continue

            if (
                "OTC"
                in pair
            ):
                continue

            if pair not in fallback:
                fallback.append(
                    pair
                )

        print(
            "[PAIRS] Использую PAIRS: "
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
        ВАЖНО:

        Здесь больше нет цепочки:

            get_candles
            fetch_candles
            get_history
            get_data

        Мы вызываем только основной метод.

        Поэтому одна пара =
        один нормальный запрос.
        """

        try:

            result = (
                await market_client.get_candles(
                    pair,
                    limit=MARKET_LIMIT,
                )
            )

            if result is not None:
                return result

        except Exception as exc:

            print(
                f"[MARKET] {pair}: "
                f"{exc}"
            )

        print(
            f"[MARKET] {pair}: "
            "свечи не получены"
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
    # ENGINE
    # ============================================================

    async def analyze_pair(
        self,
        pair: str,
    ) -> Signal | None:

        pair = str(
            pair
        ).strip().upper()

        print(
            f"[ANALYZE] {pair}"
        )

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

            if candle_count < MIN_CANDLES:

                print(
                    f"[ANALYZE] {pair}: "
                    f"мало свечей: "
                    f"{candle_count}/"
                    f"{MIN_CANDLES}"
                )

                return None

            signal = None

            # ----------------------------------------------------
            # Основной интерфейс текущего SignalEngine.
            #
            # Сначала:
            #
            #     analyze(candles)
            #
            # Потом поддерживаем старые варианты.
            # ----------------------------------------------------

            method = getattr(
                self.engine,
                "analyze",
                None,
            )

            if method is not None:

                try:

                    signal = method(
                        candles
                    )

                    if asyncio.iscoroutine(
                        signal
                    ):
                        signal = (
                            await signal
                        )

                except TypeError:

                    try:

                        signal = method(
                            pair,
                            candles,
                        )

                        if asyncio.iscoroutine(
                            signal
                        ):
                            signal = (
                                await signal
                            )

                    except TypeError:

                        try:

                            signal = method(
                                candles,
                                pair,
                            )

                            if asyncio.iscoroutine(
                                signal
                            ):
                                signal = (
                                    await signal
                                )

                        except Exception:
                            signal = None

                    except Exception as exc:

                        print(
                            f"[ENGINE] "
                            f"{pair}: "
                            f"{exc}"
                        )

                        return None

                except Exception as exc:

                    print(
                        f"[ENGINE] "
                        f"{pair}: "
                        f"{exc}"
                    )

                    return None

            # ----------------------------------------------------
            # Старые имена методов.
            # ----------------------------------------------------

            if signal is None:

                for method_name in (
                    "generate_signal",
                    "get_signal",
                ):

                    old_method = getattr(
                        self.engine,
                        method_name,
                        None,
                    )

                    if old_method is None:
                        continue

                    try:

                        signal = old_method(
                            pair,
                            candles,
                        )

                        if asyncio.iscoroutine(
                            signal
                        ):
                            signal = (
                                await signal
                            )

                        if signal is not None:
                            break

                    except TypeError:

                        try:

                            signal = old_method(
                                candles,
                                pair,
                            )

                            if asyncio.iscoroutine(
                                signal
                            ):
                                signal = (
                                    await signal
                                )

                            if signal is not None:
                                break

                        except Exception:
                            continue

                    except Exception:
                        continue

            if signal is None:

                print(
                    f"[ANALYZE] {pair}: "
                    "сигнал не сформирован"
                )

                return None

            # ----------------------------------------------------
            # Pair
            # ----------------------------------------------------

            try:
                signal.pair = pair
            except Exception:
                pass

            # ----------------------------------------------------
            # QUALITY
            # ----------------------------------------------------

            quality = self.get_value(
                signal,
                "quality",
                0,
            )

            try:
                quality = float(
                    quality
                )
            except (
                TypeError,
                ValueError,
            ):
                quality = 0.0

            # ----------------------------------------------------
            # PROBABILITY
            # ----------------------------------------------------

            probability = self.get_value(
                signal,
                "probability",
                0,
            )

            try:
                probability = float(
                    probability
                )
            except (
                TypeError,
                ValueError,
            ):
                probability = 0.0

            print(
                f"[ANALYZE] {pair}: "
                f"Q={quality:.1f} "
                f"P={probability:.1f}%"
            )

            # ----------------------------------------------------
            # QUALITY FILTER
            # ----------------------------------------------------

            if quality < float(
                MIN_QUALITY
            ):

                print(
                    f"[REJECT] {pair}: "
                    f"quality "
                    f"{quality:.1f} < "
                    f"{MIN_QUALITY}"
                )

                return None

            # ----------------------------------------------------
            # PROBABILITY FILTER
            # ----------------------------------------------------

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

            # ----------------------------------------------------
            # DIRECTION
            # ----------------------------------------------------

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

            # ----------------------------------------------------
            # TIME
            # ----------------------------------------------------

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

            if entry_time is None:

                entry_time = (
                    self.next_analysis_time(
                        5
                    )
                )

                try:
                    signal.entry_time = (
                        entry_time
                    )
                except Exception:
                    pass

            if expiry_time is None:

                expiry_time = (
                    entry_time
                    + timedelta(
                        minutes=5
                    )
                )

                try:
                    signal.expiry_time = (
                        expiry_time
                    )
                except Exception:
                    pass

            return signal

        except Exception as exc:

            print(
                f"[ANALYZE] {pair}: "
                f"ошибка {exc}"
            )

            return None

    # ============================================================
    # CHOOSE BEST
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
            except (
                TypeError,
                ValueError,
            ):
                quality = 0.0

            try:
                probability = float(
                    probability
                )
            except (
                TypeError,
                ValueError,
            ):
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
    # MANUAL SIGNAL
    # ============================================================

    async def get_manual_signal(
        self,
        pair: str | None = None,
    ) -> Signal | None:

        async with self.scan_lock:

            # ----------------------------------------------------
            # Конкретная пара
            # ----------------------------------------------------

            if pair:

                pair = str(
                    pair
                ).strip().upper()

                print(
                    f"[MANUAL] "
                    f"Проверяю {pair}"
                )

                return await self.analyze_pair(
                    pair
                )

            # ----------------------------------------------------
            # Все пары
            # ----------------------------------------------------

            print(
                "[MANUAL] "
                "Проверяю все доступные пары"
            )

            pairs = (
                await self.get_available_pairs()
            )

            if not pairs:
                return None

            signals = []

            for current_pair in pairs:

                try:

                    signal = (
                        await self.analyze_pair(
                            current_pair
                        )
                    )

                    if signal is not None:
                        signals.append(
                            signal
                        )

                except Exception as exc:

                    print(
                        f"[MANUAL] "
                        f"{current_pair}: "
                        f"{exc}"
                    )

            best = self.choose_best(
                signals
            )

            if best is None:

                print(
                    "[MANUAL] "
                    "Сильный сигнал не найден"
                )

                return None

            print(
                "[MANUAL] "
                "Лучший сигнал: "
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
            print(
                "=" * 60
            )

            print(
                "[SCAN] "
                f"{self.now().strftime('%d.%m.%Y %H:%M:%S')} "
                "МСК"
            )

            print(
                "=" * 60
            )

            pairs = (
                await self.get_available_pairs()
            )

            if not pairs:

                print(
                    "[SCAN] "
                    "Нет доступных пар"
                )

                return None

            print(
                "[SCAN] Проверяю "
                f"{len(pairs)} пар"
            )

            signals = []

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
                        f"{pair}: "
                        f"{exc}"
                    )

            best = self.choose_best(
                signals
            )

            if best is None:

                print(
                    "[SCAN] "
                    "Сильного сигнала не найдено"
                )

                return None

            print(
                "[SCAN] "
                "============================="
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
                "[SCAN] "
                "============================="
            )

            await self.save_signal(
                best
            )

            await self.send_to_users(
                best
            )

            return best

    # ============================================================
    # RUN
    # ============================================================

    async def run(
        self,
    ):

        """
        Основной цикл.

        main.py запускает:

            asyncio.create_task(
                scheduler.run()
            )
        """

        if self.running:
            print(
                "[SCHEDULER] "
                "Уже запущен"
            )

            return

        self.running = True

        print(
            "[SCHEDULER] "
            "Автоматический режим запущен"
        )

        try:

            while self.running:

                try:

                    next_time = (
                        self.next_analysis_time(
                            SIGNAL_INTERVAL_MINUTES
                        )
                    )

                    print(
                        "[SCHEDULER] "
                        "Следующий анализ: "
                        f"{next_time.strftime('%H:%M:%S')} "
                        "МСК"
                    )

                    now = self.now()

                    delay = (
                        next_time - now
                    ).total_seconds()

                    if delay > 0:

                        await asyncio.sleep(
                            delay
                        )

                    if not self.running:
                        break

                    await self.scan_once()

                except asyncio.CancelledError:

                    raise

                except Exception as exc:

                    print(
                        "[SCHEDULER] "
                        f"Ошибка цикла: "
                        f"{exc}"
                    )

                    await asyncio.sleep(
                        5
                    )

        except asyncio.CancelledError:

            print(
                "[SCHEDULER] "
                "Цикл отменён"
            )

            raise

        finally:

            self.running = False

            print(
                "[SCHEDULER] "
                "Автоматический режим остановлен"
            )

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
            (list, tuple),
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
                    "[DB] Ошибка сохранения: "
                    f"{exc}"
                )

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

            users = (
                db.get_approved_users()
            )

        except Exception as exc:

            print(
                "[SEND] Ошибка БД: "
                f"{exc}"
            )

            return

        if not users:

            print(
                "[SEND] "
                "Нет одобренных пользователей"
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
                    f"[SEND] "
                    f"Ошибка {user}: "
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

        reasons = self.get_value(
            signal,
            "reasons",
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
            direction_text = (
                "CALL ↑"
            )

        else:

            emoji = "🔴"
            direction_text = (
                "PUT ↓"
            )

        # --------------------------------------------------------
        # PAIR
        # --------------------------------------------------------

        pair_text = str(
            pair
        ).upper()

        if (
            "_OTC"
            in pair_text
        ):

            pair_text = (
                pair_text
                .replace(
                    "_OTC",
                    "",
                )
            )

            pair_text = (
                pair_text
                .replace(
                    "/",
                    "",
                )
            )

            if len(pair_text) == 6:

                pair_text = (
                    f"{pair_text[:3]}/"
                    f"{pair_text[3:]}"
                )

            pair_text += " OTC"

        else:

            pair_text = (
                pair_text
                .replace(
                    "/",
                    "",
                )
            )

            if len(pair_text) == 6:

                pair_text = (
                    f"{pair_text[:3]}/"
                    f"{pair_text[3:]}"
                )

        # --------------------------------------------------------
        # ENTRY
        # --------------------------------------------------------

        if isinstance(
            entry_time,
            datetime,
        ):

            entry_text = (
                entry_time
                .astimezone(
                    MOSCOW_TZ
                )
                .strftime(
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

        # --------------------------------------------------------
        # EXPIRY
        # --------------------------------------------------------

        if isinstance(
            expiry_time,
            datetime,
        ):

            expiry_text = (
                expiry_time
                .astimezone(
                    MOSCOW_TZ
                )
                .strftime(
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

        confirmation_lines = []

        if isinstance(
            confirmations,
            (list, tuple),
        ):

            for item in confirmations:

                text = str(
                    item
                ).strip()

                if not text:
                    continue

                if not text.startswith(
                    (
                        "✅",
                        "❌",
                        "⚠️",
                    )
                ):

                    text = (
                        f"✅ {text}"
                    )

                confirmation_lines.append(
                    text
                )

        elif confirmations:

            text = str(
                confirmations
            ).strip()

            if text:
                confirmation_lines.append(
                    text
                )

        # --------------------------------------------------------
        # REASONS
        # --------------------------------------------------------

        if isinstance(
            reasons,
            (list, tuple),
        ):

            for item in reasons:

                if (
                    len(
                        confirmation_lines
                    )
                    >= MAX_REASONS
                ):
                    break

                text = str(
                    item
                ).strip()

                if not text:
                    continue

                if not text.startswith(
                    (
                        "✅",
                        "❌",
                        "⚠️",
                    )
                ):

                    text = (
                        f"✅ {text}"
                    )

                if text not in (
                    confirmation_lines
                ):

                    confirmation_lines.append(
                        text
                    )

        # --------------------------------------------------------
        # MESSAGE
        # --------------------------------------------------------

        lines = [
            f"{emoji} {direction_text}",
            "",
            f"💱 {pair_text}",
            "",
            f"⏰ ВХОД: {entry_text}",
            f"🎯 ЭКСПИРАЦИЯ: {expiry_text}",
            "",
            f"📊 QUALITY: "
            f"{quality_text}/100",
            f"📈 ШАНС: "
            f"{probability_text}",
        ]

        lines.extend(
            confirmation_lines[
                :MAX_REASONS
            ]
        )

        return "\n".join(
            lines
        )
