from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta
from typing import Optional, Any

from aiogram import Bot

from config import (
    MIN_PROBABILITY,
    MIN_QUALITY,
    PAIRS,
    TIMEZONE,
)

from database import db
from market import market_client
from signal_engine import Signal, SignalEngine


class SignalScheduler:
    """
    Планировщик и менеджер торговых сигналов.

    Возможности:

    1. Автоматический анализ.
    2. Ручное получение сигнала.
    3. Выбор конкретной пары.
    4. Режим "любая пара".
    5. Выбор времени сделки 1-20 минут.
    6. Режим "любое время".
    7. Защита Twelve Data от лишних запросов.
    8. Сохранение сигналов в БД.
    9. Рассылка сигналов пользователям.
    10. Совместимость со старым API.

    ВАЖНО:

    Текущий market.py использует 5m свечи.
    Поэтому частота анализа остаётся привязанной к
    5-минутным свечным данным.

    Длительность сделки может быть от 1 до 20 минут.
    """

    # =========================================================
    # API LIMIT
    # =========================================================

    MAX_PAIRS_PER_SCAN = 6

    REQUEST_DELAY_SECONDS = 2.0

    RATE_LIMIT_COOLDOWN_SECONDS = 65

    # =========================================================
    # EXPIRY
    # =========================================================

    MIN_EXPIRY_MINUTES = 1
    MAX_EXPIRY_MINUTES = 20
    DEFAULT_EXPIRY_MINUTES = 5

    # Специальное значение для "Любое время"
    ANY_EXPIRY = "any"

    # =========================================================
    # CONSTRUCTOR
    # =========================================================

    def __init__(
        self,
        bot: Optional[Bot] = None,
    ) -> None:

        self.bot = bot

        self.engine = SignalEngine(
            min_quality=MIN_QUALITY,
            min_probability=MIN_PROBABILITY,
        )

        self.running = False

        self._scan_lock = asyncio.Lock()

        self._rate_limited_until: Optional[
            datetime
        ] = None

        self._last_request_at: Optional[
            datetime
        ] = None

        self._last_scan_at: Optional[
            datetime
        ] = None

        self._last_signals: dict[
            str,
            Signal,
        ] = {}

        # -----------------------------------------------------
        # Настройки автоматического режима
        # -----------------------------------------------------

        self.auto_expiry_minutes: Optional[
            int
        ] = self.DEFAULT_EXPIRY_MINUTES

        self.auto_any_expiry = False

        self.auto_pair: Optional[
            str
        ] = None

        print(
            "[SCHEDULER] Initialized | "
            f"pairs={len(PAIRS)} | "
            f"max_pairs_per_scan="
            f"{self.MAX_PAIRS_PER_SCAN} | "
            f"min_quality="
            f"{float(MIN_QUALITY):.1f} | "
            f"min_probability="
            f"{float(MIN_PROBABILITY):.1f}% | "
            f"expiry="
            f"{self.MIN_EXPIRY_MINUTES}-"
            f"{self.MAX_EXPIRY_MINUTES}m"
        )

    # =========================================================
    # TIMEZONE
    # =========================================================

    def _tz(self):
        try:

            if hasattr(
                TIMEZONE,
                "utcoffset",
            ):
                return TIMEZONE

            from zoneinfo import ZoneInfo

            return ZoneInfo(
                str(TIMEZONE)
            )

        except Exception:

            from zoneinfo import ZoneInfo

            return ZoneInfo(
                "Europe/Moscow"
            )

    def _now(self) -> datetime:

        return datetime.now(
            self._tz()
        )

    # =========================================================
    # NEXT 5 MINUTE
    # =========================================================

    def _next_5_minute(
        self,
        dt: Optional[datetime] = None,
    ) -> datetime:

        if dt is None:
            dt = self._now()

        if dt.tzinfo is None:

            dt = dt.replace(
                tzinfo=self._tz()
            )

        dt = dt.astimezone(
            self._tz()
        )

        next_minute = (
            ((dt.minute // 5) + 1)
            * 5
        )

        if next_minute >= 60:

            return (
                dt.replace(
                    minute=0,
                    second=0,
                    microsecond=0,
                )
                + timedelta(hours=1)
            )

        return dt.replace(
            minute=next_minute,
            second=0,
            microsecond=0,
        )

    # =========================================================
    # EXPIRY NORMALIZATION
    # =========================================================

    def normalize_expiry(
        self,
        expiry_minutes: Any = None,
    ) -> Optional[int]:
        """
        None / "any" / "любое время"
            -> None

        1..20
            -> соответствующее число.

        Неверное значение
            -> 5.
        """

        if expiry_minutes is None:

            return self.DEFAULT_EXPIRY_MINUTES

        if isinstance(
            expiry_minutes,
            str,
        ):

            value = (
                expiry_minutes
                .strip()
                .lower()
            )

            if value in {
                "any",
                "all",
                "auto",
                "any_time",
                "любое",
                "любое время",
                "любое_время",
            }:

                return None

            try:

                expiry_minutes = int(
                    value
                )

            except ValueError:

                return (
                    self.DEFAULT_EXPIRY_MINUTES
                )

        try:

            value = int(
                expiry_minutes
            )

        except (
            TypeError,
            ValueError,
        ):

            return (
                self.DEFAULT_EXPIRY_MINUTES
            )

        return max(
            self.MIN_EXPIRY_MINUTES,
            min(
                self.MAX_EXPIRY_MINUTES,
                value,
            ),
        )

    # =========================================================
    # CONFIGURE AUTOMATIC MODE
    # =========================================================

    def set_auto_expiry(
        self,
        expiry_minutes: Any,
    ) -> None:
        """
        Настройка времени автоматических сигналов.

        Примеры:

            set_auto_expiry(1)
            set_auto_expiry(5)
            set_auto_expiry(20)
            set_auto_expiry("any")
        """

        normalized = (
            self.normalize_expiry(
                expiry_minutes
            )
        )

        if normalized is None:

            self.auto_any_expiry = True

            self.auto_expiry_minutes = None

            print(
                "[SCHEDULER] "
                "Auto expiry = ANY "
                "(1-20m)"
            )

            return

        self.auto_any_expiry = False

        self.auto_expiry_minutes = (
            normalized
        )

        print(
            "[SCHEDULER] "
            f"Auto expiry = "
            f"{normalized}m"
        )

    def set_auto_pair(
        self,
        pair: Optional[str],
    ) -> None:
        """
        Настройка пары для автоматического режима.

        None:
            любая пара.
        """

        if pair is None:

            self.auto_pair = None

            print(
                "[SCHEDULER] "
                "Auto pair = ANY"
            )

            return

        value = str(
            pair
        ).strip()

        if not value:

            self.auto_pair = None

            return

        if value.lower() in {
            "any",
            "all",
            "любая",
            "любая пара",
        }:

            self.auto_pair = None

            print(
                "[SCHEDULER] "
                "Auto pair = ANY"
            )

            return

        self.auto_pair = value

        print(
            "[SCHEDULER] "
            f"Auto pair = {value}"
        )

    # =========================================================
    # GET AVAILABLE PAIRS
    # =========================================================

    def get_available_pairs(
        self,
    ) -> list[str]:

        result: list[str] = []

        try:

            for pair in PAIRS:

                pair = str(
                    pair
                ).strip()

                if (
                    pair
                    and pair not in result
                ):

                    result.append(
                        pair
                    )

        except Exception as exc:

            print(
                "[SCHEDULER] "
                f"Ошибка чтения PAIRS: "
                f"{exc}"
            )

        if result:

            return result

        # -----------------------------------------------------
        # FALLBACK
        # -----------------------------------------------------

        try:

            method = getattr(
                market_client,
                "get_available_pairs",
                None,
            )

            if method is None:

                return []

            pairs = method()

            if inspect.isawaitable(
                pairs
            ):

                return []

            if pairs:

                for pair in pairs:

                    pair = str(
                        pair
                    ).strip()

                    if (
                        pair
                        and pair not in result
                    ):

                        result.append(
                            pair
                        )

        except Exception as exc:

            print(
                "[SCHEDULER] "
                f"Ошибка fallback pair list: "
                f"{exc}"
            )

        return result

    # =========================================================
    # SELECT PAIRS
    # =========================================================

    def _select_pairs_for_scan(
        self,
        requested_pair: Optional[str] = None,
    ) -> list[str]:

        pairs = (
            self.get_available_pairs()
        )

        if requested_pair:

            requested_pair = (
                str(
                    requested_pair
                ).strip()
            )

            if requested_pair.lower() in {
                "any",
                "all",
                "любая",
                "любая пара",
            }:

                requested_pair = None

        # -----------------------------------------------------
        # Specific pair
        # -----------------------------------------------------

        if requested_pair:

            return [
                requested_pair
            ]

        # -----------------------------------------------------
        # Any pair
        # -----------------------------------------------------

        if not pairs:

            return []

        return pairs[
            :self.MAX_PAIRS_PER_SCAN
        ]

    # =========================================================
    # RATE LIMIT
    # =========================================================

    def _rate_limit_active(
        self,
    ) -> bool:

        if (
            self._rate_limited_until
            is None
        ):

            return False

        now = self._now()

        if (
            now
            >= self._rate_limited_until
        ):

            self._rate_limited_until = None

            return False

        return True

    def _activate_rate_limit(
        self,
    ) -> None:

        self._rate_limited_until = (
            self._now()
            + timedelta(
                seconds=(
                    self.RATE_LIMIT_COOLDOWN_SECONDS
                )
            )
        )

        print(
            "[SCHEDULER] "
            "Twelve Data rate limit "
            "detected. Анализ остановлен "
            f"на "
            f"{self.RATE_LIMIT_COOLDOWN_SECONDS}s."
        )

    # =========================================================
    # REQUEST DELAY
    # =========================================================

    async def _wait_request_delay(
        self,
    ) -> None:

        if (
            self._last_request_at
            is None
        ):

            return

        elapsed = (
            self._now()
            - self._last_request_at
        ).total_seconds()

        if (
            elapsed
            < self.REQUEST_DELAY_SECONDS
        ):

            await asyncio.sleep(
                self.REQUEST_DELAY_SECONDS
                - elapsed
            )

    # =========================================================
    # MARKET DATA
    # =========================================================

    async def get_candles(
        self,
        pair: str,
    ):

        try:

            await self._wait_request_delay()

            self._last_request_at = (
                self._now()
            )

            method = getattr(
                market_client,
                "get_candles",
                None,
            )

            if method is None:

                print(
                    f"[MARKET] {pair}: "
                    "get_candles отсутствует"
                )

                return None

            result = method(
                pair
            )

            if inspect.isawaitable(
                result
            ):

                result = await result

            return result

        except Exception as exc:

            text = str(
                exc
            )

            print(
                f"[MARKET] {pair}: "
                f"get_candles error: "
                f"{exc}"
            )

            if (
                "429" in text
                or "rate limit"
                in text.lower()
                or "credits"
                in text.lower()
            ):

                self._activate_rate_limit()

            return None

    # =========================================================
    # ANALYZE ONE PAIR
    # =========================================================

    async def analyze_pair(
        self,
        pair: str,
        expiry_minutes: Any = None,
    ) -> Optional[Signal]:

        if self._rate_limit_active():

            print(
                f"[ANALYSIS] {pair}: "
                "SKIP | rate limit cooldown"
            )

            return None

        pair = str(
            pair
        ).strip()

        if not pair:

            return None

        # -----------------------------------------------------
        # Normalize expiry
        # -----------------------------------------------------

        normalized_expiry = (
            self.normalize_expiry(
                expiry_minutes
            )
        )

        print(
            f"[ANALYSIS] {pair} | "
            f"requested_expiry="
            f"{'ANY' if normalized_expiry is None else str(normalized_expiry) + 'm'}"
        )

        # -----------------------------------------------------
        # Get candles
        # -----------------------------------------------------

        candles = await self.get_candles(
            pair
        )

        if candles is None:

            print(
                f"[ANALYSIS] {pair}: "
                "REJECT | candles unavailable"
            )

            return None

        try:

            candles_count = len(
                candles
            )

        except Exception:

            candles_count = 0

        if candles_count < 80:

            print(
                f"[ANALYSIS] {pair}: "
                f"REJECT | only "
                f"{candles_count} candles"
            )

            return None

        # -----------------------------------------------------
        # ANY EXPIRY
        # -----------------------------------------------------

        if normalized_expiry is None:

            try:

                signal = (
                    self.engine.choose_best_expiry(
                        pair=pair,
                        candles=candles,
                    )
                )

            except TypeError:

                # Совместимость, если движок
                # не поддерживает choose_best_expiry.

                signal = (
                    self.engine.analyze(
                        pair,
                        candles,
                    )
                )

        else:

            try:

                signal = (
                    self.engine.analyze(
                        pair,
                        candles,
                        expiry_minutes=(
                            normalized_expiry
                        ),
                    )
                )

            except TypeError:

                # Старый API.

                signal = (
                    self.engine.analyze(
                        pair,
                        candles,
                    )
                )

        if signal is None:

            print(
                f"[ANALYSIS] {pair}: "
                "REJECT | no strong signal"
            )

            return None

        print(
            f"[ANALYSIS] {pair}: "
            f"SUCCESS | "
            f"{signal.direction} | "
            f"Q={signal.quality:.1f} | "
            f"P={signal.probability:.1f}% | "
            f"EXPIRY={signal.expiry_minutes}m"
        )

        self._last_signals[
            pair
        ] = signal

        return signal

    # =========================================================
    # SIGNAL SCORE
    # =========================================================

    @staticmethod
    def _signal_score(
        signal: Signal,
    ) -> tuple[
        float,
        float,
        float,
        float,
    ]:

        return (
            float(
                signal.probability
            ),
            float(
                signal.quality
            ),
            float(
                len(
                    signal.confirmations
                )
            ),
            -float(
                signal.expiry_minutes
            ),
        )

    # =========================================================
    # FIND BEST SIGNAL
    # =========================================================

    async def _find_best_signal(
        self,
        requested_pair: Optional[str] = None,
        expiry_minutes: Any = None,
    ) -> tuple[
        Optional[Signal],
        list[str],
    ]:

        pairs = (
            self._select_pairs_for_scan(
                requested_pair
            )
        )

        print("=" * 70)
        print("[SCAN] START")

        expiry_text = (
            "ANY 1-20m"
            if self.normalize_expiry(
                expiry_minutes
            ) is None
            else f"{self.normalize_expiry(expiry_minutes)}m"
        )

        print(
            f"[SCAN] Expiry: "
            f"{expiry_text}"
        )

        print(
            f"[SCAN] Проверяем "
            f"{len(pairs)} пар:"
        )

        if pairs:

            print(
                ", ".join(
                    pairs
                )
            )

        print("=" * 70)

        if not pairs:

            print(
                "[SCAN] Нет доступных пар"
            )

            return None, []

        signals: list[
            Signal
        ] = []

        diagnostics: list[
            str
        ] = []

        for index, pair in enumerate(
            pairs,
            start=1,
        ):

            if self._rate_limit_active():

                diagnostics.append(
                    f"{pair}: RATE_LIMIT"
                )

                break

            print(
                f"[SCAN] "
                f"{index}/{len(pairs)} "
                f"→ {pair}"
            )

            signal = await self.analyze_pair(
                pair=pair,
                expiry_minutes=(
                    expiry_minutes
                ),
            )

            if signal is None:

                diagnostics.append(
                    f"{pair}: NO_SIGNAL"
                )

            else:

                signals.append(
                    signal
                )

            if index < len(pairs):

                await asyncio.sleep(
                    self.REQUEST_DELAY_SECONDS
                )

        # -----------------------------------------------------
        # No signal
        # -----------------------------------------------------

        if not signals:

            print("=" * 70)
            print(
                "[SCAN] NO STRONG SIGNAL"
            )
            print("=" * 70)

            return None, diagnostics

        # -----------------------------------------------------
        # Best
        # -----------------------------------------------------

        signals.sort(
            key=self._signal_score,
            reverse=True,
        )

        best = signals[0]

        print("=" * 70)
        print(
            "[SCAN] BEST SIGNAL"
        )

        print(
            f"Pair: "
            f"{best.pair}"
        )

        print(
            f"Direction: "
            f"{best.direction}"
        )

        print(
            f"Quality: "
            f"{best.quality:.1f}"
        )

        print(
            f"Probability: "
            f"{best.probability:.1f}%"
        )

        print(
            f"Expiry: "
            f"{best.expiry_minutes}m"
        )

        print(
            f"Entry: "
            f"{best.entry_time.strftime('%H:%M:%S')}"
        )

        print(
            f"Expiry time: "
            f"{best.expiry_time.strftime('%H:%M:%S')}"
        )

        print("=" * 70)

        return best, diagnostics

    # =========================================================
    # MANUAL SIGNAL
    # =========================================================

    async def get_manual_signal(
        self,
        pair: Optional[str] = None,
        expiry_minutes: Any = None,
    ) -> Optional[Signal]:

        async with self._scan_lock:

            print("=" * 70)
            print(
                "[MANUAL SIGNAL] START"
            )

            print(
                "[MANUAL SIGNAL] "
                f"{self._now().strftime('%Y-%m-%d %H:%M:%S')}"
            )

            if pair:

                print(
                    "[MANUAL SIGNAL] "
                    f"Пара: {pair}"
                )

            if expiry_minutes is not None:

                print(
                    "[MANUAL SIGNAL] "
                    f"Время: "
                    f"{expiry_minutes}"
                )

            if self._rate_limit_active():

                remaining = (
                    self._rate_limited_until
                    - self._now()
                ).total_seconds()

                print(
                    "[MANUAL SIGNAL] "
                    "RATE LIMIT ACTIVE | "
                    f"осталось примерно "
                    f"{max(0, remaining):.0f}s"
                )

                return None

            signal, _ = (
                await self._find_best_signal(
                    requested_pair=pair,
                    expiry_minutes=(
                        expiry_minutes
                    ),
                )
            )

            if signal is None:

                print(
                    "[MANUAL SIGNAL] "
                    "Сильного сигнала нет"
                )

            else:

                print(
                    "[MANUAL SIGNAL] "
                    f"FOUND: "
                    f"{signal.pair} "
                    f"{signal.direction} "
                    f"Q={signal.quality:.1f} "
                    f"P={signal.probability:.1f}% "
                    f"T={signal.expiry_minutes}m"
                )

            return signal

    # =========================================================
    # SCAN ONCE
    # =========================================================

    async def scan_once(
        self,
        pair: Optional[str] = None,
        expiry_minutes: Any = None,
    ) -> Optional[Signal]:

        async with self._scan_lock:

            if self._rate_limit_active():

                print(
                    "[SCHEDULER] "
                    "scan_once skipped | "
                    "rate limit cooldown"
                )

                return None

            # -------------------------------------------------
            # Если параметры не передали —
            # используем автоматические настройки.
            # -------------------------------------------------

            if pair is None:

                pair = self.auto_pair

            if expiry_minutes is None:

                if self.auto_any_expiry:

                    expiry_minutes = (
                        self.ANY_EXPIRY
                    )

                else:

                    expiry_minutes = (
                        self.auto_expiry_minutes
                        or self.DEFAULT_EXPIRY_MINUTES
                    )

            signal, _ = (
                await self._find_best_signal(
                    requested_pair=pair,
                    expiry_minutes=(
                        expiry_minutes
                    ),
                )
            )

            if signal is None:

                return None

            await self._save_signal(
                signal
            )

            await self._send_to_users(
                signal
            )

            return signal

    # =========================================================
    # DATABASE
    # =========================================================

    async def _save_signal(
        self,
        signal: Signal,
    ) -> None:

        if db is None:

            print(
                "[DATABASE] db unavailable | "
                "signal not saved"
            )

            return

        methods = [
            "save_signal",
            "add_signal",
            "create_signal",
        ]

        for method_name in methods:

            method = getattr(
                db,
                method_name,
                None,
            )

            if method is None:

                continue

            try:

                payload = {
                    "pair": signal.pair,
                    "direction": signal.direction,
                    "quality": signal.quality,
                    "probability": signal.probability,
                    "entry_time": signal.entry_time,
                    "expiry_time": signal.expiry_time,
                    "analysis_time": signal.analysis_time,
                    "confirmations": signal.confirmations,
                    "reasons": signal.reasons,
                }

                try:

                    result = method(
                        **payload
                    )

                except TypeError:

                    result = method(
                        signal
                    )

                if inspect.isawaitable(
                    result
                ):

                    await result

                print(
                    "[DATABASE] Signal saved | "
                    f"{signal.pair} | "
                    f"{signal.direction} | "
                    f"{signal.expiry_minutes}m"
                )

                return

            except Exception as exc:

                print(
                    f"[DATABASE] "
                    f"{method_name} error: "
                    f"{exc}"
                )

        print(
            "[DATABASE] "
            "Метод сохранения сигнала не найден"
        )

    # =========================================================
    # FORMAT SIGNAL
    # =========================================================

    @staticmethod
    def format_signal(
        signal: Signal,
    ) -> str:

        direction_text = {
            "CALL": "🟢 ВВЕРХ",
            "PUT": "🔴 ВНИЗ",
        }.get(
            signal.direction,
            signal.direction,
        )

        entry = (
            signal.entry_time.strftime(
                "%H:%M:%S"
            )
        )

        expiry = (
            signal.expiry_time.strftime(
                "%H:%M:%S"
            )
        )

        confirmations = (
            signal.confirmations[:8]
        )

        if confirmations:

            confirmations_text = (
                "\n".join(
                    f"• {item}"
                    for item in confirmations
                )
            )

        else:

            confirmations_text = (
                "• Подтверждения не указаны"
            )

        return (
            "🎯 <b>ТОРГОВЫЙ СИГНАЛ</b>\n"
            "\n"
            f"💱 Пара: "
            f"<b>{signal.pair}</b>\n"
            f"📈 Направление: "
            f"<b>{direction_text}</b>\n"
            "\n"
            f"⏱️ Время сделки: "
            f"<b>{signal.expiry_minutes} мин.</b>\n"
            "\n"
            f"⭐ Качество: "
            f"<b>{signal.quality:.1f}/100</b>\n"
            f"🎯 Расчётная вероятность: "
            f"<b>{signal.probability:.1f}%</b>\n"
            "\n"
            f"🟢 Вход: "
            f"<b>{entry}</b>\n"
            f"🔴 Закрытие: "
            f"<b>{expiry}</b>\n"
            "\n"
            "🔎 <b>Подтверждения:</b>\n"
            f"{confirmations_text}\n"
            "\n"
            "⚠️ Вероятность является расчётной "
            "оценкой модели, а не гарантией результата."
        )

    # =========================================================
    # GET USER ID
    # =========================================================

    @staticmethod
    def _extract_user_id(
        user: Any,
    ) -> Optional[int]:

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

                return int(
                    user
                )

            except ValueError:

                return None

        if isinstance(
            user,
            dict,
        ):

            for key in (
                "telegram_id",
                "user_id",
                "id",
            ):

                if key in user:

                    try:

                        return int(
                            user[key]
                        )

                    except (
                        TypeError,
                        ValueError,
                    ):

                        return None

            return None

        for key in (
            "telegram_id",
            "user_id",
            "id",
        ):

            if hasattr(
                user,
                key,
            ):

                try:

                    return int(
                        getattr(
                            user,
                            key,
                        )
                    )

                except (
                    TypeError,
                    ValueError,
                ):

                    return None

        return None

    # =========================================================
    # SEND USERS
    # =========================================================

    async def _send_to_users(
        self,
        signal: Signal,
    ) -> None:

        if self.bot is None:

            print(
                "[SCHEDULER] Bot unavailable | "
                "signal not sent"
            )

            return

        if db is None:

            print(
                "[SCHEDULER] DB unavailable | "
                "users not loaded"
            )

            return

        users = None

        methods = [
            "get_active_users",
            "get_users",
            "get_all_users",
        ]

        for method_name in methods:

            method = getattr(
                db,
                method_name,
                None,
            )

            if method is None:

                continue

            try:

                users = method()

                if inspect.isawaitable(
                    users
                ):

                    users = await users

                if users is not None:

                    break

            except Exception as exc:

                print(
                    f"[SCHEDULER] "
                    f"{method_name} error: "
                    f"{exc}"
                )

        if not users:

            print(
                "[SCHEDULER] "
                "Нет активных пользователей"
            )

            return

        text = (
            self.format_signal(
                signal
            )
        )

        sent = 0
        failed = 0

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
                    parse_mode="HTML",
                )

                sent += 1

            except Exception as exc:

                failed += 1

                print(
                    f"[SCHEDULER] "
                    f"Не удалось отправить "
                    f"{user_id}: {exc}"
                )

        print(
            "[SCHEDULER] "
            f"Signal sent | "
            f"success={sent} | "
            f"failed={failed}"
        )

    # =========================================================
    # AUTOMATIC LOOP
    # =========================================================

    async def run(
        self,
    ) -> None:

        if self.running:

            print(
                "[SCHEDULER] "
                "Already running"
            )

            return

        self.running = True

        print(
            "[SCHEDULER] "
            "Automatic scheduler started"
        )

        print(
            "[SCHEDULER] "
            f"Auto pair: "
            f"{self.auto_pair or 'ANY'}"
        )

        print(
            "[SCHEDULER] "
            f"Auto expiry: "
            f"{'ANY 1-20m' if self.auto_any_expiry else str(self.auto_expiry_minutes) + 'm'}"
        )

        try:

            while self.running:

                now = self._now()

                next_run = (
                    self._next_5_minute(
                        now
                    )
                )

                seconds = (
                    next_run - now
                ).total_seconds()

                print(
                    "[SCHEDULER] "
                    f"Следующий анализ: "
                    f"{next_run.strftime('%H:%M:%S')} "
                    f"| через "
                    f"{seconds:.1f}s"
                )

                if seconds > 0:

                    await asyncio.sleep(
                        seconds
                    )

                if not self.running:

                    break

                # -------------------------------------------------
                # RATE LIMIT
                # -------------------------------------------------

                if self._rate_limit_active():

                    remaining = (
                        self._rate_limited_until
                        - self._now()
                    ).total_seconds()

                    print(
                        "[SCHEDULER] "
                        "Пропуск цикла из-за "
                        f"rate limit "
                        f"({max(0, remaining):.0f}s)"
                    )

                    await asyncio.sleep(
                        min(
                            max(
                                remaining,
                                1,
                            ),
                            70,
                        )
                    )

                    continue

                # -------------------------------------------------
                # SCAN
                # -------------------------------------------------

                try:

                    await self.scan_once()

                except asyncio.CancelledError:

                    raise

                except Exception as exc:

                    print(
                        "[SCHEDULER] "
                        f"Ошибка цикла: "
                        f"{type(exc).__name__}: "
                        f"{exc}"
                    )

                await asyncio.sleep(
                    2
                )

        except asyncio.CancelledError:

            print(
                "[SCHEDULER] "
                "Automatic scheduler cancelled"
            )

            raise

        finally:

            self.running = False

            print(
                "[SCHEDULER] "
                "Automatic scheduler stopped"
            )

    # =========================================================
    # STOP
    # =========================================================

    def stop(
        self,
    ) -> None:

        self.running = False

        print(
            "[SCHEDULER] "
            "Stop requested"
        )


# =========================================================
# EXPORT
# =========================================================

__all__ = [
    "SignalScheduler",
]
