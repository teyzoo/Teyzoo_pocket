from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from zoneinfo import ZoneInfo

from config import (
    MIN_PROBABILITY,
    MIN_QUALITY,
    PAIRS,
    TIMEZONE,
)

from market import MarketClient
from signal_engine import Signal, SignalEngine


class SignalScheduler:
    """
    Планировщик автоматических и ручных сигналов.

    Основные возможности:
    - автоматический анализ;
    - ручной сигнал по конкретной паре;
    - обычные пары;
    - OTC-пары;
    - все пары;
    - фильтр Quality;
    - фильтр Chance;
    - отправка активным пользователям;
    - сохранение сигнала;
    - время МСК.
    """

    ANALYSIS_INTERVAL_MINUTES = 5

    # Сколько времени до входа оставляем.
    ENTRY_OFFSET_MINUTES = 0

    # Срок сделки.
    EXPIRY_MINUTES = 5

    # Минимум свечей.
    MIN_CANDLES = 80

    # Максимальное количество причин в сообщении.
    MAX_REASONS = 8

    def __init__(
        self,
        market_client: MarketClient | None = None,
        signal_engine: SignalEngine | None = None,
        bot: Any = None,
        db: Any = None,
    ) -> None:

        self.market_client = (
            market_client
            if market_client is not None
            else MarketClient()
        )

        self.engine = (
            signal_engine
            if signal_engine is not None
            else SignalEngine()
        )

        self.bot = bot
        self.db = db

        self.running = False
        self.task: asyncio.Task | None = None

        self.timezone = ZoneInfo(
            TIMEZONE
        )

        self.min_quality = float(
            MIN_QUALITY
        )

        self.min_probability = float(
            MIN_PROBABILITY
        )

        print(
            "[SCHEDULER] Инициализирован"
        )

        print(
            f"[SCHEDULER] MIN_QUALITY = "
            f"{self.min_quality}"
        )

        print(
            f"[SCHEDULER] MIN_PROBABILITY = "
            f"{self.min_probability}%"
        )

    # ============================================================
    # TIME
    # ============================================================

    def now_moscow(self) -> datetime:
        return datetime.now(
            self.timezone
        )

    def next_mark(
        self,
        minutes: int | None = None,
    ) -> datetime:

        interval = (
            minutes
            if minutes is not None
            else self.ANALYSIS_INTERVAL_MINUTES
        )

        now = self.now_moscow()

        next_minute = (
            (now.minute // interval) + 1
        ) * interval

        result = now.replace(
            second=0,
            microsecond=0,
        )

        if next_minute >= 60:
            result = (
                result
                + timedelta(hours=1)
            ).replace(
                minute=0
            )
        else:
            result = result.replace(
                minute=next_minute
            )

        return result

    def make_entry_time(self) -> datetime:
        """
        Время входа округляется к ближайшей
        будущей пятиминутной отметке.
        """

        return self.next_mark(5)

    def make_expiry_time(
        self,
        entry_time: datetime,
    ) -> datetime:

        return (
            entry_time
            + timedelta(
                minutes=self.EXPIRY_MINUTES
            )
        )

    # ============================================================
    # SIGNAL NORMALIZATION
    # ============================================================

    def _normalize_signal(
        self,
        signal: Any,
        pair: str,
    ) -> Signal | None:

        if signal is None:
            return None

        if not isinstance(signal, Signal):
            return signal

        try:
            if (
                not getattr(
                    signal,
                    "pair",
                    None,
                )
                or getattr(
                    signal,
                    "pair",
                    "",
                )
                in (
                    "",
                    "UNKNOWN",
                    "unknown",
                    None,
                )
            ):
                signal.pair = pair

        except Exception:
            pass

        return signal

    # ============================================================
    # ENGINE CALL
    # ============================================================

    async def _run_engine(
        self,
        candles,
        pair: str,
    ) -> Signal | None:

        if candles is None:
            return None

        if len(candles) < self.MIN_CANDLES:
            print(
                f"[REJECT] {pair}: "
                f"нет достаточного количества свечей "
                f"{len(candles)}/{self.MIN_CANDLES}"
            )

            return None

        analyze_method = getattr(
            self.engine,
            "analyze",
            None,
        )

        if analyze_method is None:
            print(
                "[ENGINE] Метод analyze не найден"
            )

            return None

        # --------------------------------------------------------
        # Сначала используем актуальный интерфейс:
        # analyze(candles)
        # --------------------------------------------------------

        try:
            result = analyze_method(
                candles
            )

            if inspect.isawaitable(result):
                result = await result

            return self._normalize_signal(
                result,
                pair,
            )

        except TypeError as first_error:
            """
            Совместимость со старой версией
            SignalEngine, где мог использоваться
            analyze(candles, pair).
            """

            try:
                result = analyze_method(
                    candles,
                    pair,
                )

                if inspect.isawaitable(result):
                    result = await result

                return self._normalize_signal(
                    result,
                    pair,
                )

            except Exception as second_error:
                print(
                    f"[ENGINE] {pair}: "
                    f"{second_error}"
                )

                return None

        except Exception as exc:
            print(
                f"[ENGINE] {pair}: "
                f"{exc}"
            )

            return None

    # ============================================================
    # QUALITY
    # ============================================================

    @staticmethod
    def _get_quality(
        signal: Any,
    ) -> float:

        try:
            return float(
                getattr(
                    signal,
                    "quality",
                    0,
                )
            )
        except (
            TypeError,
            ValueError,
        ):
            return 0.0

    @staticmethod
    def _get_probability(
        signal: Any,
    ) -> float:

        try:
            return float(
                getattr(
                    signal,
                    "probability",
                    0,
                )
            )
        except (
            TypeError,
            ValueError,
        ):
            return 0.0

    def is_acceptable(
        self,
        signal: Signal | None,
    ) -> bool:

        if signal is None:
            return False

        quality = self._get_quality(
            signal
        )

        probability = self._get_probability(
            signal
        )

        if quality < self.min_quality:
            print(
                f"[REJECT] "
                f"{getattr(signal, 'pair', '?')}: "
                f"Quality {quality:.1f} "
                f"< {self.min_quality:.1f}"
            )

            return False

        if probability < self.min_probability:
            print(
                f"[REJECT] "
                f"{getattr(signal, 'pair', '?')}: "
                f"Chance {probability:.1f}% "
                f"< {self.min_probability:.1f}%"
            )

            return False

        return True

    # ============================================================
    # ANALYZE ONE PAIR
    # ============================================================

    async def analyze_pair(
        self,
        pair: str,
    ) -> Signal | None:

        if not pair:
            return None

        pair = str(pair).strip()

        print(
            f"[ANALYZE] {pair}"
        )

        try:
            candles = await self.market_client.get_candles(
                pair,
                limit=max(
                    self.MIN_CANDLES + 20,
                    120,
                ),
            )

        except Exception as exc:
            print(
                f"[MARKET] {pair}: "
                f"{exc}"
            )

            return None

        if candles is None:
            print(
                f"[REJECT] {pair}: "
                f"нет свечей"
            )

            return None

        if len(candles) < self.MIN_CANDLES:
            print(
                f"[REJECT] {pair}: "
                f"недостаточно свечей "
                f"{len(candles)}/"
                f"{self.MIN_CANDLES}"
            )

            return None

        signal = await self._run_engine(
            candles,
            pair,
        )

        if signal is None:
            print(
                f"[REJECT] {pair}: "
                f"движок не дал сигнал"
            )

            return None

        # Всегда устанавливаем правильную пару.
        try:
            signal.pair = pair
        except Exception:
            pass

        quality = self._get_quality(
            signal
        )

        probability = self._get_probability(
            signal
        )

        print(
            f"[RESULT] {pair}: "
            f"quality={quality:.1f}, "
            f"chance={probability:.1f}%"
        )

        if not self.is_acceptable(
            signal
        ):
            return None

        # --------------------------------------------------------
        # Устанавливаем вход / экспирацию,
        # если движок их не создал.
        # --------------------------------------------------------

        entry_time = getattr(
            signal,
            "entry_time",
            None,
        )

        expiry_time = getattr(
            signal,
            "expiry_time",
            None,
        )

        if entry_time is None:
            entry_time = (
                self.make_entry_time()
            )

            try:
                signal.entry_time = entry_time
            except Exception:
                pass

        if expiry_time is None:
            expiry_time = (
                self.make_expiry_time(
                    entry_time
                )
            )

            try:
                signal.expiry_time = expiry_time
            except Exception:
                pass

        return signal

    # ============================================================
    # SCAN PAIRS
    # ============================================================

    async def scan_pairs(
        self,
        pairs: list[str],
    ) -> Signal | None:

        if not pairs:
            return None

        best_signal: Signal | None = None

        for pair in pairs:

            try:
                signal = await self.analyze_pair(
                    pair
                )

            except Exception as exc:
                print(
                    f"[SCAN] {pair}: "
                    f"{exc}"
                )

                continue

            if signal is None:
                continue

            if best_signal is None:
                best_signal = signal
                continue

            current_quality = (
                self._get_quality(signal)
            )

            current_probability = (
                self._get_probability(signal)
            )

            best_quality = (
                self._get_quality(
                    best_signal
                )
            )

            best_probability = (
                self._get_probability(
                    best_signal
                )
            )

            # Сначала Quality.
            # При равенстве — Chance.
            if (
                current_quality > best_quality
                or (
                    current_quality
                    == best_quality
                    and current_probability
                    > best_probability
                )
            ):
                best_signal = signal

        return best_signal

    # ============================================================
    # PAIR LISTS
    # ============================================================

    def get_regular_pairs(self) -> list[str]:

        result = []

        for pair in PAIRS:
            value = str(pair).strip()

            if not value:
                continue

            if self._is_otc(pair):
                continue

            if value not in result:
                result.append(value)

        return result

    async def get_otc_pairs(self) -> list[str]:

        try:
            from keyboards import OTC_PAIRS

            return list(OTC_PAIRS)

        except Exception as exc:
            print(
                f"[PAIRS] OTC error: {exc}"
            )

            return []

    async def get_all_pairs(self) -> list[str]:

        regular = self.get_regular_pairs()
        otc = await self.get_otc_pairs()

        result = []

        for pair in (
            regular + otc
        ):
            if pair not in result:
                result.append(pair)

        return result

    @staticmethod
    def _is_otc(
        pair: str,
    ) -> bool:

        value = str(pair).upper()

        return (
            "_OTC" in value
            or "/OTC" in value
            or " OTC" in value
        )

    # ============================================================
    # MANUAL SIGNAL
    # ============================================================

    async def get_manual_signal(
        self,
        pair: str | None = None,
    ) -> Signal | None:

        # --------------------------------------------------------
        # Конкретная пара.
        # --------------------------------------------------------

        if pair:
            print(
                f"[MANUAL] Проверяю {pair}"
            )

            return await self.analyze_pair(
                pair
            )

        # --------------------------------------------------------
        # Все доступные пары.
        # --------------------------------------------------------

        print(
            "[MANUAL] Проверяю все доступные "
            "пары, включая OTC"
        )

        pairs = await self.get_all_pairs()

        return await self.scan_pairs(
            pairs
        )

    async def get_manual_signal_regular(
        self,
        pair: str | None = None,
    ) -> Signal | None:

        if pair:
            if self._is_otc(pair):
                return None

            return await self.analyze_pair(
                pair
            )

        pairs = self.get_regular_pairs()

        return await self.scan_pairs(
            pairs
        )

    async def get_manual_signal_otc(
        self,
        pair: str | None = None,
    ) -> Signal | None:

        otc_pairs = await self.get_otc_pairs()

        if pair:
            if not self._is_otc(pair):
                return None

            return await self.analyze_pair(
                pair
            )

        return await self.scan_pairs(
            otc_pairs
        )

    # ============================================================
    # SIGNAL TEXT
    # ============================================================

    @staticmethod
    def _format_time(
        value: Any,
    ) -> str:

        if value is None:
            return "--:--"

        try:
            if value.tzinfo is None:
                value = value.replace(
                    tzinfo=ZoneInfo(
                        TIMEZONE
                    )
                )

            value = value.astimezone(
                ZoneInfo(TIMEZONE)
            )

            return value.strftime(
                "%H:%M"
            )

        except Exception:
            return "--:--"

    @staticmethod
    def _direction_text(
        direction: Any,
    ) -> tuple[str, str]:

        value = str(
            direction or ""
        ).upper()

        if value in (
            "PUT",
            "DOWN",
            "SELL",
            "🔴",
        ):
            return (
                "🔴 PUT",
                "↓",
            )

        if value in (
            "CALL",
            "UP",
            "BUY",
            "🟢",
        ):
            return (
                "🟢 CALL",
                "↑",
            )

        return (
            value or "SIGNAL",
            "",
        )

    @staticmethod
    def _format_pair(
        pair: Any,
    ) -> str:

        value = str(
            pair or ""
        ).upper()

        otc = (
            "_OTC" in value
            or "/OTC" in value
            or " OTC" in value
        )

        value = value.replace(
            "_OTC",
            "",
        )

        value = value.replace(
            "/OTC",
            "",
        )

        value = value.replace(
            " OTC",
            "",
        )

        value = value.replace(
            "/",
            "",
        )

        value = value.replace(
            "-",
            "",
        )

        value = value.replace(
            "_",
            "",
        )

        if len(value) == 6:
            value = (
                f"{value[:3]}/"
                f"{value[3:]}"
            )

        if otc:
            value += " OTC"

        return value

    def format_signal(
        self,
        signal: Signal,
    ) -> str:

        direction, arrow = (
            self._direction_text(
                getattr(
                    signal,
                    "direction",
                    "",
                )
            )
        )

        pair = self._format_pair(
            getattr(
                signal,
                "pair",
                "",
            )
        )

        quality = self._get_quality(
            signal
        )

        probability = self._get_probability(
            signal
        )

        entry_time = self._format_time(
            getattr(
                signal,
                "entry_time",
                None,
            )
        )

        expiry_time = self._format_time(
            getattr(
                signal,
                "expiry_time",
                None,
            )
        )

        confirmations = list(
            getattr(
                signal,
                "confirmations",
                [],
            )
            or []
        )

        reasons = list(
            getattr(
                signal,
                "reasons",
                [],
            )
            or []
        )

        lines = []

        lines.append(
            f"{direction} {arrow}".strip()
        )

        lines.append("")
        lines.append(
            f"💱 {pair}"
        )

        lines.append("")
        lines.append(
            f"⏰ ВХОД: {entry_time} МСК"
        )

        lines.append(
            f"🎯 ЭКСПИРАЦИЯ: "
            f"{expiry_time} МСК"
        )

        lines.append("")
        lines.append(
            f"📊 QUALITY: "
            f"{quality:.0f}/100"
        )

        lines.append(
            f"📈 ШАНС: "
            f"{probability:.0f}%"
        )

        used = 0

        for item in confirmations:
            if used >= self.MAX_REASONS:
                break

            text = str(item).strip()

            if not text:
                continue

            if not text.startswith(
                ("✅", "❌", "⚠️")
            ):
                text = f"✅ {text}"

            lines.append(text)

            used += 1

        if used < self.MAX_REASONS:
            for item in reasons:
                if used >= self.MAX_REASONS:
                    break

                text = str(item).strip()

                if not text:
                    continue

                if not text.startswith(
                    ("✅", "❌", "⚠️")
                ):
                    text = f"✅ {text}"

                if text in lines:
                    continue

                lines.append(text)

                used += 1

        return "\n".join(lines)

    # ============================================================
    # DATABASE
    # ============================================================

    async def save_signal(
        self,
        signal: Signal,
    ) -> Any:

        if self.db is None:
            return None

        methods = [
            "save_signal",
            "create_signal",
            "add_signal",
        ]

        for method_name in methods:

            method = getattr(
                self.db,
                method_name,
                None,
            )

            if method is None:
                continue

            try:
                result = method(
                    signal
                )

                if inspect.isawaitable(
                    result
                ):
                    result = await result

                return result

            except TypeError:

                try:
                    result = method(
                        pair=getattr(
                            signal,
                            "pair",
                            None,
                        ),
                        direction=getattr(
                            signal,
                            "direction",
                            None,
                        ),
                        quality=self._get_quality(
                            signal
                        ),
                        probability=(
                            self._get_probability(
                                signal
                            )
                        ),
                        entry_time=getattr(
                            signal,
                            "entry_time",
                            None,
                        ),
                        expiry_time=getattr(
                            signal,
                            "expiry_time",
                            None,
                        ),
                    )

                    if inspect.isawaitable(
                        result
                    ):
                        result = await result

                    return result

                except Exception as exc:
                    print(
                        f"[DB] {method_name}: "
                        f"{exc}"
                    )

            except Exception as exc:
                print(
                    f"[DB] {method_name}: "
                    f"{exc}"
                )

        return None

    # ============================================================
    # USERS
    # ============================================================

    async def get_active_users(
        self,
    ) -> list[Any]:

        if self.db is None:
            return []

        methods = [
            "get_active_users",
            "get_approved_users",
            "active_users",
        ]

        for method_name in methods:

            method = getattr(
                self.db,
                method_name,
                None,
            )

            if method is None:
                continue

            try:
                result = method()

                if inspect.isawaitable(
                    result
                ):
                    result = await result

                if result is None:
                    return []

                return list(result)

            except Exception as exc:
                print(
                    f"[DB] users "
                    f"{method_name}: {exc}"
                )

        return []

    @staticmethod
    def _extract_user_id(
        user: Any,
    ) -> int | None:

        if isinstance(
            user,
            int,
        ):
            return user

        if isinstance(
            user,
            str,
        ):
            try:
                return int(user)
            except ValueError:
                return None

        for attribute in (
            "telegram_id",
            "user_id",
            "id",
        ):
            value = getattr(
                user,
                attribute,
                None,
            )

            if value is None:
                continue

            try:
                return int(value)
            except (
                TypeError,
                ValueError,
            ):
                continue

        if isinstance(
            user,
            dict,
        ):
            for key in (
                "telegram_id",
                "user_id",
                "id",
            ):
                value = user.get(key)

                if value is None:
                    continue

                try:
                    return int(value)
                except (
                    TypeError,
                    ValueError,
                ):
                    continue

        return None

    # ============================================================
    # SEND
    # ============================================================

    async def send_signal(
        self,
        signal: Signal,
    ) -> int:

        if self.bot is None:
            return 0

        text = self.format_signal(
            signal
        )

        users = await self.get_active_users()

        sent = 0

        for user in users:

            user_id = (
                self._extract_user_id(
                    user
                )
            )

            if user_id is None:
                continue

            try:
                await self.bot.send_message(
                    chat_id=user_id,
                    text=text,
                )

                sent += 1

            except Exception as exc:
                print(
                    f"[SEND] {user_id}: "
                    f"{exc}"
                )

        return sent

    # ============================================================
    # AUTOMATIC ANALYSIS
    # ============================================================

    async def run_once(
        self,
    ) -> Signal | None:

        print(
            "[SCHEDULER] Запуск автоматического "
            "анализа"
        )

        pairs = await self.get_all_pairs()

        if not pairs:
            print(
                "[SCHEDULER] Нет доступных пар"
            )

            return None

        signal = await self.scan_pairs(
            pairs
        )

        if signal is None:
            print(
                "[SCHEDULER] Сильный сигнал "
                "не найден"
            )

            return None

        print(
            f"[SCHEDULER] Найден сигнал: "
            f"{getattr(signal, 'pair', '?')} "
            f"{getattr(signal, 'direction', '?')} "
            f"quality="
            f"{self._get_quality(signal):.1f} "
            f"chance="
            f"{self._get_probability(signal):.1f}%"
        )

        await self.save_signal(
            signal
        )

        sent = await self.send_signal(
            signal
        )

        print(
            f"[SCHEDULER] Отправлено: "
            f"{sent}"
        )

        return signal

    # ============================================================
    # LOOP
    # ============================================================

    async def _loop(
        self,
    ) -> None:

        print(
            "[SCHEDULER] Автоматический "
            "режим запущен"
        )

        while self.running:

            try:
                next_time = (
                    self.next_mark(
                        self.ANALYSIS_INTERVAL_MINUTES
                    )
                )

                print(
                    "[SCHEDULER] Следующий "
                    f"анализ: "
                    f"{next_time.strftime('%H:%M:%S')} "
                    f"МСК"
                )

                now = self.now_moscow()

                sleep_seconds = (
                    next_time - now
                ).total_seconds()

                if sleep_seconds > 0:
                    await asyncio.sleep(
                        sleep_seconds
                    )

                if not self.running:
                    break

                await self.run_once()

            except asyncio.CancelledError:
                break

            except Exception as exc:
                print(
                    f"[SCHEDULER] Ошибка цикла: "
                    f"{exc}"
                )

                await asyncio.sleep(
                    5
                )

        print(
            "[SCHEDULER] Автоматический "
            "режим остановлен"
        )

    # ============================================================
    # START / STOP
    # ============================================================

    def start(self) -> None:

        if self.running:
            print(
                "[SCHEDULER] Уже запущен"
            )

            return

        self.running = True

        self.task = asyncio.create_task(
            self._loop()
        )

    async def stop(self) -> None:

        self.running = False

        if self.task is not None:

            if not self.task.done():
                self.task.cancel()

                try:
                    await self.task

                except asyncio.CancelledError:
                    pass

            self.task = None

        try:
            await self.market_client.close()

        except Exception:
            pass

        print(
            "[SCHEDULER] Остановлен"
        )

    # ============================================================
    # COMPATIBILITY
    # ============================================================

    async def automatic_loop(
        self,
    ) -> None:

        if not self.running:
            self.running = True

        await self._loop()

    async def process_signal(
        self,
        signal: Signal,
    ) -> int:

        await self.save_signal(
            signal
        )

        return await self.send_signal(
            signal
        )
