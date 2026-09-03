from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta
from typing import Any, Optional

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
    Планировщик и менеджер сигналов.

    Основные задачи:

    1. Автоматический анализ.
    2. Ручное получение сигнала.
    3. Выбор лучшей пары.
    4. Защита от превышения лимита Twelve Data.
    5. Сохранение найденных сигналов.
    6. Отправка сигналов пользователям.

    ВАЖНО:

    Бесплатный Twelve Data имеет ограничение по API credits.
    Поэтому нельзя бездумно запрашивать все 26 пар одновременно.

    По умолчанию один полный проход использует максимум
    MAX_PAIRS_PER_SCAN пар.
    """

    # =========================================================
    # API LIMIT
    # =========================================================

    # Для бесплатного лимита 8 кредитов/минуту оставляем запас.
    #
    # 6 запросов за проход позволяют оставить место
    # для других запросов API и ручного запуска.
    MAX_PAIRS_PER_SCAN = 6

    # Минимальная пауза между API-запросами.
    #
    # Это не отменяет кредитный лимит, но предотвращает
    # мгновенный шквал запросов.
    REQUEST_DELAY_SECONDS = 2.0

    # После получения 429 не продолжаем долбить API.
    RATE_LIMIT_COOLDOWN_SECONDS = 65

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

        self._last_scan_at: Optional[
            datetime
        ] = None

        self._last_signals: dict[
            str,
            Signal
        ] = {}

        print(
            "[SCHEDULER] Initialized | "
            f"pairs={len(PAIRS)} | "
            f"max_pairs_per_scan={self.MAX_PAIRS_PER_SCAN} | "
            f"min_quality={float(MIN_QUALITY):.1f} | "
            f"min_probability={float(MIN_PROBABILITY):.1f}% | "
            "interval=5m"
        )

    # =========================================================
    # TIME
    # =========================================================

    def _tz(self):
        try:
            if hasattr(TIMEZONE, "utcoffset"):
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
            ((dt.minute // 5) + 1) * 5
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
    # PAIRS
    # =========================================================

    def get_available_pairs(self) -> list[str]:
        """
        Возвращает список доступных пар.

        Сначала используем PAIRS из config.py.
        Если список пуст — пробуем market_client.
        """

        result: list[str] = []

        try:
            for pair in PAIRS:
                pair = str(pair).strip()

                if pair and pair not in result:
                    result.append(pair)

        except Exception as exc:
            print(
                "[SCHEDULER] Ошибка чтения PAIRS: "
                f"{exc}"
            )

        if result:
            return result

        # -----------------------------------------------------
        # Fallback
        # -----------------------------------------------------

        try:
            method = getattr(
                market_client,
                "get_available_pairs",
                None,
            )

            if method is not None:
                pairs = method()

                if inspect.isawaitable(pairs):
                    # Этот метод синхронно здесь вызвать нельзя.
                    # Поэтому просто не используем результат.
                    return []

                if pairs:
                    for pair in pairs:
                        pair = str(pair).strip()

                        if (
                            pair
                            and pair not in result
                        ):
                            result.append(pair)

        except Exception as exc:
            print(
                "[SCHEDULER] "
                f"Ошибка fallback pair list: {exc}"
            )

        return result

    # =========================================================
    # PAIR SELECTION
    # =========================================================

    def _select_pairs_for_scan(
        self,
        requested_pair: Optional[str] = None,
    ) -> list[str]:
        """
        Выбирает пары для конкретного анализа.

        Если пользователь запросил конкретную пару —
        анализируем только её.

        Если пользователь запросил "любую пару" —
        берём максимум MAX_PAIRS_PER_SCAN.
        """

        pairs = self.get_available_pairs()

        if requested_pair:
            requested_pair = (
                str(requested_pair)
                .strip()
            )

            if (
                requested_pair.lower()
                in {
                    "any",
                    "all",
                    "любая",
                    "любая пара",
                }
            ):
                requested_pair = None

        if requested_pair:
            # Не блокируем ручной запрос конкретной пары,
            # даже если она не находится в PAIRS.
            return [requested_pair]

        if not pairs:
            return []

        # -----------------------------------------------------
        # Важно:
        #
        # Здесь не отправляем запросы для всех 26 пар.
        # Это защищает Twelve Data от 429.
        # -----------------------------------------------------

        return pairs[
            :self.MAX_PAIRS_PER_SCAN
        ]

    # =========================================================
    # RATE LIMIT
    # =========================================================

    def _rate_limit_active(self) -> bool:
        if self._rate_limited_until is None:
            return False

        now = self._now()

        if now >= self._rate_limited_until:
            self._rate_limited_until = None
            return False

        return True

    def _activate_rate_limit(self) -> None:
        self._rate_limited_until = (
            self._now()
            + timedelta(
                seconds=self.RATE_LIMIT_COOLDOWN_SECONDS
            )
        )

        print(
            "[SCHEDULER] Twelve Data rate limit "
            "detected. Анализ временно остановлен "
            f"на {self.RATE_LIMIT_COOLDOWN_SECONDS}s."
        )

    # =========================================================
    # MARKET DATA
    # =========================================================

    async def get_candles(
        self,
        pair: str,
    ):
        """
        Получение свечей через market_client.
        """

        try:
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

            result = method(pair)

            if inspect.isawaitable(result):
                result = await result

            return result

        except Exception as exc:
            text = str(exc)

            print(
                f"[MARKET] {pair}: "
                f"get_candles error: {exc}"
            )

            if (
                "429" in text
                or "rate limit" in text.lower()
                or "credits" in text.lower()
            ):
                self._activate_rate_limit()

            return None

    # =========================================================
    # ANALYZE ONE PAIR
    # =========================================================

    async def analyze_pair(
        self,
        pair: str,
    ) -> Optional[Signal]:

        if self._rate_limit_active():
            print(
                f"[ANALYSIS] {pair}: "
                "SKIP | rate limit cooldown"
            )
            return None

        pair = str(pair).strip()

        if not pair:
            return None

        # -----------------------------------------------------
        # Small delay between API requests
        # -----------------------------------------------------

        if (
            self._last_scan_at is not None
        ):
            elapsed = (
                self._now()
                - self._last_scan_at
            ).total_seconds()

            if (
                elapsed
                < self.REQUEST_DELAY_SECONDS
            ):
                await asyncio.sleep(
                    self.REQUEST_DELAY_SECONDS
                    - elapsed
                )

        self._last_scan_at = self._now()

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
            candles_count = len(candles)
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
        # Engine
        # -----------------------------------------------------

        try:
            signal = self.engine.analyze(
                pair,
                candles,
            )

        except TypeError as exc:
            print(
                f"[ANALYSIS] {pair}: "
                f"ENGINE TYPE ERROR: {exc}"
            )
            return None

        except Exception as exc:
            print(
                f"[ANALYSIS] {pair}: "
                f"ENGINE ERROR: "
                f"{type(exc).__name__}: {exc}"
            )
            return None

        if signal is None:
            print(
                f"[ANALYSIS] {pair}: "
                "REJECT | engine returned None"
            )
            return None

        print(
            f"[ANALYSIS] {pair}: "
            f"SUCCESS | "
            f"{signal.direction} | "
            f"Q={signal.quality:.1f} | "
            f"P={signal.probability:.1f}%"
        )

        self._last_signals[
            pair
        ] = signal

        return signal

    # =========================================================
    # FIND BEST SIGNAL
    # =========================================================

    @staticmethod
    def _signal_score(
        signal: Signal,
    ) -> tuple[float, float, float]:
        """
        Сортировка лучших сигналов.

        Сначала вероятность,
        затем качество,
        затем количество подтверждений.
        """

        return (
            float(signal.probability),
            float(signal.quality),
            float(
                len(signal.confirmations)
            ),
        )

    async def _find_best_signal(
        self,
        requested_pair: Optional[str] = None,
    ) -> tuple[
        Optional[Signal],
        list[str],
    ]:

        pairs = self._select_pairs_for_scan(
            requested_pair
        )

        print(
            "=" * 70
        )

        print(
            "[SCAN] START"
        )

        print(
            f"[SCAN] Проверяем {len(pairs)} пар:"
        )

        if pairs:
            print(
                ", ".join(pairs)
            )

        print(
            "=" * 70
        )

        if not pairs:
            print(
                "[SCAN] Нет доступных пар"
            )
            return None, []

        signals: list[Signal] = []
        diagnostics: list[str] = []

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
                f"[SCAN] {index}/{len(pairs)} "
                f"→ {pair}"
            )

            signal = await self.analyze_pair(
                pair
            )

            if signal is None:
                diagnostics.append(
                    f"{pair}: NO_SIGNAL"
                )
            else:
                signals.append(signal)

            # -------------------------------------------------
            # Если это не последняя пара —
            # выдерживаем паузу.
            # -------------------------------------------------

            if index < len(pairs):
                await asyncio.sleep(
                    self.REQUEST_DELAY_SECONDS
                )

        # -----------------------------------------------------
        # No signals
        # -----------------------------------------------------

        if not signals:
            print(
                "=" * 70
            )
            print(
                "[SCAN] NO STRONG SIGNAL"
            )
            print(
                "=" * 70
            )

            return None, diagnostics

        # -----------------------------------------------------
        # Best signal
        # -----------------------------------------------------

        signals.sort(
            key=self._signal_score,
            reverse=True,
        )

        best = signals[0]

        print(
            "=" * 70
        )

        print(
            "[SCAN] BEST SIGNAL"
        )

        print(
            f"Pair: {best.pair}"
        )

        print(
            f"Direction: {best.direction}"
        )

        print(
            f"Quality: {best.quality:.1f}"
        )

        print(
            f"Probability: "
            f"{best.probability:.1f}%"
        )

        print(
            "=" * 70
        )

        return best, diagnostics

    # =========================================================
    # MANUAL SIGNAL
    # =========================================================

    async def get_manual_signal(
        self,
        pair: Optional[str] = None,
    ) -> Optional[Signal]:

        async with self._scan_lock:

            print(
                "=" * 70
            )

            print(
                "[MANUAL SIGNAL] START"
            )

            print(
                f"[MANUAL SIGNAL] "
                f"{self._now().strftime('%Y-%m-%d %H:%M:%S')}"
            )

            # -------------------------------------------------
            # Rate limit
            # -------------------------------------------------

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

            # -------------------------------------------------
            # Requested pair
            # -------------------------------------------------

            if pair:
                print(
                    f"[MANUAL SIGNAL] "
                    f"Запрошена пара: {pair}"
                )

            signal, _ = (
                await self._find_best_signal(
                    requested_pair=pair
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
                    f"FOUND: {signal.pair} "
                    f"{signal.direction} "
                    f"Q={signal.quality:.1f} "
                    f"P={signal.probability:.1f}%"
                )

            return signal

    # =========================================================
    # SCAN ONCE
    # =========================================================

    async def scan_once(
        self,
    ) -> Optional[Signal]:

        async with self._scan_lock:

            if self._rate_limit_active():
                print(
                    "[SCHEDULER] scan_once skipped | "
                    "rate limit cooldown"
                )
                return None

            signal, _ = (
                await self._find_best_signal()
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

        # -----------------------------------------------------
        # Возможные API базы
        # -----------------------------------------------------

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
                    f"{signal.pair}"
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

        entry = signal.entry_time.strftime(
            "%H:%M:%S"
        )

        expiry = signal.expiry_time.strftime(
            "%H:%M:%S"
        )

        confirmations = (
            signal.confirmations[:8]
        )

        if confirmations:
            confirmations_text = "\n".join(
                f"• {item}"
                for item in confirmations
            )
        else:
            confirmations_text = (
                "• Подтверждения не указаны"
            )

        return (
            "🎯 <b>ТОРГОВЫЙ СИГНАЛ</b>\n"
            "\n"
            f"💱 Пара: <b>{signal.pair}</b>\n"
            f"📈 Направление: "
            f"<b>{direction_text}</b>\n"
            "\n"
            f"⭐ Качество: "
            f"<b>{signal.quality:.1f}/100</b>\n"
            f"🎯 Расчётная вероятность: "
            f"<b>{signal.probability:.1f}%</b>\n"
            "\n"
            f"🟢 Вход: <b>{entry}</b>\n"
            f"🔴 Закрытие: <b>{expiry}</b>\n"
            "\n"
            "🔎 <b>Подтверждения:</b>\n"
            f"{confirmations_text}\n"
            "\n"
            "⚠️ Вероятность является расчётной "
            "оценкой модели, а не гарантией результата."
        )

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

        # -----------------------------------------------------
        # Получаем пользователей через возможные методы DB
        # -----------------------------------------------------

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
                    f"{method_name} error: {exc}"
                )

        if not users:
            print(
                "[SCHEDULER] "
                "Нет активных пользователей"
            )
            return

        text = self.format_signal(
            signal
        )

        sent = 0
        failed = 0

        for user in users:

            # -------------------------------------------------
            # Поддержка разных форматов DB
            # -------------------------------------------------

            user_id = None

            if isinstance(
                user,
                int,
            ):
                user_id = user

            elif isinstance(
                user,
                str,
            ):
                try:
                    user_id = int(user)
                except ValueError:
                    user_id = None

            elif isinstance(
                user,
                dict,
            ):
                for key in (
                    "telegram_id",
                    "user_id",
                    "id",
                ):
                    if key in user:
                        user_id = user[key]
                        break

            else:
                for key in (
                    "telegram_id",
                    "user_id",
                    "id",
                ):
                    if hasattr(
                        user,
                        key,
                    ):
                        user_id = getattr(
                            user,
                            key,
                        )
                        break

            if user_id is None:
                continue

            try:

                await self.bot.send_message(
                    chat_id=int(user_id),
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
                    f"{next_run.strftime('%H:%M:%S')} МСК "
                    f"| через {seconds:.1f}s"
                )

                if seconds > 0:
                    await asyncio.sleep(
                        seconds
                    )

                # -------------------------------------------------
                # После сна проверяем, что scheduler всё ещё
                # работает.
                # -------------------------------------------------

                if not self.running:
                    break

                # -------------------------------------------------
                # Если Twelve Data временно заблокирован,
                # ждём cooldown.
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
                # Запуск анализа
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

                # -------------------------------------------------
                # Небольшая защита от повторного запуска
                # в ту же минуту.
                # -------------------------------------------------

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

    def stop(self) -> None:

        self.running = False

        print(
            "[SCHEDULER] "
            "Stop requested"
        )
