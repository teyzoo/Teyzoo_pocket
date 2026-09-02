from __future__ import annotations

import asyncio
import inspect
import traceback
from datetime import datetime, timedelta
from typing import Any

from zoneinfo import ZoneInfo

from config import (
    PAIRS,
    MIN_PROBABILITY,
    MIN_QUALITY,
    TIMEZONE,
)

from database import db
from market import market_client
from signal_engine import SignalEngine


# ============================================================
# CONFIG
# ============================================================

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# Автоматический анализ каждые 5 минут.
SIGNAL_INTERVAL_MINUTES = 5

# Минимальное количество свечей для анализа.
MIN_CANDLES_REQUIRED = 80

# Сколько причин максимум показываем в сообщениях.
MAX_REASONS = 8

# Небольшая пауза между проверками пар.
PAIR_ANALYSIS_DELAY = 0.15

# Если API временно не отвечает — не спамим запросами.
ERROR_RETRY_DELAY = 15

# Таймаут одной операции анализа.
ANALYSIS_TIMEOUT = 30


# ============================================================
# HELPERS
# ============================================================

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default

        if isinstance(value, str):
            value = value.replace("%", "").replace(",", ".").strip()

        result = float(value)

        if result != result:
            return default

        return result

    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _get_attr_or_dict(obj: Any, name: str, default: Any = None) -> Any:
    """
    Универсальное получение значения:
    - из объекта;
    - из dataclass;
    - из словаря.
    """

    if obj is None:
        return default

    if isinstance(obj, dict):
        return obj.get(name, default)

    return getattr(obj, name, default)


def _set_attr_or_dict(obj: Any, name: str, value: Any) -> None:
    """
    Универсальная установка значения.
    """

    if obj is None:
        return

    if isinstance(obj, dict):
        obj[name] = value
        return

    try:
        setattr(obj, name, value)
    except Exception:
        pass


def _normalize_direction(direction: Any) -> str:
    if direction is None:
        return ""

    value = str(direction).strip().upper()

    mapping = {
        "BUY": "CALL",
        "UP": "CALL",
        "CALL": "CALL",
        "LONG": "CALL",

        "SELL": "PUT",
        "DOWN": "PUT",
        "PUT": "PUT",
        "SHORT": "PUT",
    }

    return mapping.get(value, value)


def _normalize_pair(pair: Any) -> str:
    if pair is None:
        return ""

    return str(pair).strip().upper()


def _now() -> datetime:
    return datetime.now(MOSCOW_TZ)


def _next_5_minute_boundary(
    current: datetime | None = None,
) -> datetime:
    """
    Возвращает ближайшее следующее время:
    00, 05, 10, 15, 20...
    """

    current = current or _now()

    current = current.replace(second=0, microsecond=0)

    minutes_to_add = (
        SIGNAL_INTERVAL_MINUTES
        - (current.minute % SIGNAL_INTERVAL_MINUTES)
    )

    if minutes_to_add == 0:
        minutes_to_add = SIGNAL_INTERVAL_MINUTES

    return current + timedelta(minutes=minutes_to_add)


# ============================================================
# SCHEDULER
# ============================================================

class SignalScheduler:
    """
    Автоматический и ручной поиск торговых сигналов.

    ВАЖНО:
    main.py создаёт объект именно так:

        scheduler = SignalScheduler(bot)

    Поэтому сигнатуру конструктора не меняем.
    """

    def __init__(self, bot=None):
        self.bot = bot

        self.engine = SignalEngine()

        self.running = False

        # Защита от одновременного запуска двух анализов.
        self.scan_lock = asyncio.Lock()

        # Статистика последнего анализа.
        self.last_scan_time: datetime | None = None
        self.last_signal = None

        self.last_scan_results: list[dict[str, Any]] = []

        print(
            "[SCHEDULER] Initialized | "
            f"pairs={len(PAIRS)} | "
            f"min_quality={MIN_QUALITY} | "
            f"min_probability={MIN_PROBABILITY}% | "
            f"interval={SIGNAL_INTERVAL_MINUTES}m"
        )

    # ========================================================
    # BOT
    # ========================================================

    def set_bot(self, bot) -> None:
        self.bot = bot

    # ========================================================
    # TIME
    # ========================================================

    @staticmethod
    def now() -> datetime:
        return _now()

    @staticmethod
    def next_analysis_time() -> datetime:
        return _next_5_minute_boundary()

    # ========================================================
    # PAIRS
    # ========================================================

    async def get_available_pairs(self) -> list[str]:
        """
        Получаем пары.

        Основной источник — PAIRS из config.py.

        Не делаем отдельный сетевой запрос для каждой пары:
        это могло приводить к долгому старту анализа и лишнему
        расходу API-запросов.
        """

        result: list[str] = []

        configured_pairs = PAIRS or []

        for pair in configured_pairs:
            normalized = _normalize_pair(pair)

            if not normalized:
                continue

            # Не допускаем дубли.
            if normalized not in result:
                result.append(normalized)

        # Если config по какой-то причине пустой,
        # пробуем получить пары от market client.
        if not result:
            try:
                method = getattr(
                    market_client,
                    "get_available_pairs",
                    None,
                )

                if method is not None:
                    available = method()

                    if inspect.isawaitable(available):
                        available = await available

                    if available:
                        for pair in available:
                            normalized = _normalize_pair(pair)

                            if normalized and normalized not in result:
                                result.append(normalized)

            except Exception as exc:
                print(
                    f"[SCHEDULER] get_available_pairs fallback error: "
                    f"{type(exc).__name__}: {exc}"
                )

        return result

    # ========================================================
    # CANDLES
    # ========================================================

    async def get_candles(self, pair: str):
        """
        Единственный путь получения свечей.

        Не используем старые get_history/get_data/fetch_candles,
        потому что они могут по-разному обрабатывать CANDLE_INTERVAL
        вроде '5min'.
        """

        pair = _normalize_pair(pair)

        if not pair:
            return None

        method = getattr(market_client, "get_candles", None)

        if method is None:
            print(
                "[MARKET] ERROR: MarketClient.get_candles() "
                "does not exist"
            )
            return None

        try:
            candles = method(pair)

            if inspect.isawaitable(candles):
                candles = await candles

            if candles is None:
                print(
                    f"[MARKET] {pair}: candles=None"
                )
                return None

            try:
                length = len(candles)
            except Exception:
                length = 0

            if length == 0:
                print(
                    f"[MARKET] {pair}: 0 candles"
                )
                return None

            print(
                f"[MARKET] {pair}: received {length} candles"
            )

            return candles

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            print(
                f"[MARKET] {pair}: get_candles ERROR: "
                f"{type(exc).__name__}: {exc}"
            )
            return None

    # ========================================================
    # ANALYZE ONE PAIR
    # ========================================================

    async def analyze_pair(
        self,
        pair: str,
    ) -> tuple[Any | None, dict[str, Any]]:
        """
        Полностью анализирует одну пару.

        Возвращает:
            (signal, diagnostic)

        diagnostic нужен для логирования причины отказа.
        """

        pair = _normalize_pair(pair)

        diagnostic: dict[str, Any] = {
            "pair": pair,
            "status": "ERROR",
            "candles": 0,
            "quality": 0.0,
            "probability": 0.0,
            "direction": "",
            "reason": "",
        }

        if not pair:
            diagnostic["reason"] = "empty pair"
            return None, diagnostic

        # ----------------------------------------------------
        # 1. Получаем свечи
        # ----------------------------------------------------

        candles = await self.get_candles(pair)

        if candles is None:
            diagnostic["status"] = "NO_DATA"
            diagnostic["reason"] = "candles unavailable"

            print(
                f"[ANALYSIS] {pair}: REJECT | "
                "candles unavailable"
            )

            return None, diagnostic

        try:
            candle_count = len(candles)
        except Exception:
            candle_count = 0

        diagnostic["candles"] = candle_count

        if candle_count < MIN_CANDLES_REQUIRED:
            diagnostic["status"] = "NOT_ENOUGH_CANDLES"
            diagnostic["reason"] = (
                f"only {candle_count} candles, "
                f"minimum {MIN_CANDLES_REQUIRED}"
            )

            print(
                f"[ANALYSIS] {pair}: REJECT | "
                f"not enough candles: "
                f"{candle_count}/{MIN_CANDLES_REQUIRED}"
            )

            return None, diagnostic

        # ----------------------------------------------------
        # 2. SignalEngine
        # ----------------------------------------------------

        try:
            analyze_method = getattr(
                self.engine,
                "analyze",
                None,
            )

            if analyze_method is None:
                diagnostic["reason"] = (
                    "SignalEngine.analyze() does not exist"
                )

                print(
                    f"[ANALYSIS] {pair}: ERROR | "
                    f"{diagnostic['reason']}"
                )

                return None, diagnostic

            # В текущем SignalEngine analyze принимает DataFrame
            # свечей, а не pair + candles.
            result = analyze_method(candles)

            if inspect.isawaitable(result):
                result = await result

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            diagnostic["status"] = "ENGINE_ERROR"
            diagnostic["reason"] = (
                f"{type(exc).__name__}: {exc}"
            )

            print(
                f"[ANALYSIS] {pair}: ENGINE ERROR | "
                f"{type(exc).__name__}: {exc}"
            )

            traceback.print_exc()

            return None, diagnostic

        # ----------------------------------------------------
        # 3. Проверяем результат
        # ----------------------------------------------------

        if result is None:
            diagnostic["status"] = "NO_SIGNAL"
            diagnostic["reason"] = "engine returned None"

            print(
                f"[ANALYSIS] {pair}: REJECT | "
                "engine returned None"
            )

            return None, diagnostic

        # Иногда engine может вернуть список/кортеж.
        # Если там единственный результат — используем его.
        if isinstance(result, (list, tuple)):
            if not result:
                diagnostic["status"] = "NO_SIGNAL"
                diagnostic["reason"] = "engine returned empty result"

                print(
                    f"[ANALYSIS] {pair}: REJECT | "
                    "empty engine result"
                )

                return None, diagnostic

            result = result[0]

        # ----------------------------------------------------
        # 4. Pair
        # ----------------------------------------------------

        signal_pair = _normalize_pair(
            _get_attr_or_dict(result, "pair", "")
        )

        if not signal_pair:
            _set_attr_or_dict(
                result,
                "pair",
                pair,
            )
            signal_pair = pair

        # ----------------------------------------------------
        # 5. Direction
        # ----------------------------------------------------

        raw_direction = _get_attr_or_dict(
            result,
            "direction",
            "",
        )

        direction = _normalize_direction(raw_direction)

        diagnostic["direction"] = direction

        if direction not in {"CALL", "PUT"}:
            diagnostic["status"] = "INVALID_DIRECTION"
            diagnostic["reason"] = (
                f"invalid direction: {raw_direction}"
            )

            print(
                f"[ANALYSIS] {pair}: REJECT | "
                f"invalid direction={raw_direction}"
            )

            return None, diagnostic

        # Нормализуем направление и в самом сигнале.
        _set_attr_or_dict(
            result,
            "direction",
            direction,
        )

        # ----------------------------------------------------
        # 6. Quality
        # ----------------------------------------------------

        quality = _safe_float(
            _get_attr_or_dict(
                result,
                "quality",
                0,
            )
        )

        diagnostic["quality"] = quality

        # ----------------------------------------------------
        # 7. Probability
        # ----------------------------------------------------

        probability = _safe_float(
            _get_attr_or_dict(
                result,
                "probability",
                0,
            )
        )

        diagnostic["probability"] = probability

        # ----------------------------------------------------
        # 8. Quality filter
        # ----------------------------------------------------

        if quality < float(MIN_QUALITY):
            diagnostic["status"] = "LOW_QUALITY"
            diagnostic["reason"] = (
                f"quality {quality:.1f} < {MIN_QUALITY}"
            )

            print(
                f"[ANALYSIS] {pair}: REJECT | "
                f"Quality={quality:.1f} < {MIN_QUALITY} | "
                f"Probability={probability:.1f}%"
            )

            return None, diagnostic

        # ----------------------------------------------------
        # 9. Probability filter
        # ----------------------------------------------------

        if probability < float(MIN_PROBABILITY):
            diagnostic["status"] = "LOW_PROBABILITY"
            diagnostic["reason"] = (
                f"probability {probability:.1f}% "
                f"< {MIN_PROBABILITY}%"
            )

            print(
                f"[ANALYSIS] {pair}: REJECT | "
                f"Quality={quality:.1f} | "
                f"Probability={probability:.1f}% "
                f"< {MIN_PROBABILITY}%"
            )

            return None, diagnostic

        # ----------------------------------------------------
        # 10. ACCEPT
        # ----------------------------------------------------

        diagnostic["status"] = "ACCEPTED"
        diagnostic["reason"] = "passed all filters"

        print(
            f"[ANALYSIS] {pair}: ACCEPT | "
            f"{direction} | "
            f"Quality={quality:.1f} | "
            f"Probability={probability:.1f}%"
        )

        return result, diagnostic

    # ========================================================
    # BEST SIGNAL
    # ========================================================

    @staticmethod
    def _signal_score(signal: Any) -> tuple[float, float]:
        quality = _safe_float(
            _get_attr_or_dict(signal, "quality", 0)
        )

        probability = _safe_float(
            _get_attr_or_dict(signal, "probability", 0)
        )

        return quality, probability

    def choose_best(
        self,
        signals: list[Any],
    ) -> Any | None:

        if not signals:
            return None

        valid_signals = [
            signal
            for signal in signals
            if signal is not None
        ]

        if not valid_signals:
            return None

        # Сначала качество.
        # При равном качестве — вероятность.
        valid_signals.sort(
            key=self._signal_score,
            reverse=True,
        )

        return valid_signals[0]

    # ========================================================
    # MANUAL SIGNAL
    # ========================================================

    async def get_manual_signal(
        self,
        pair: str | None = None,
    ):
        """
        Используется кнопкой ручного получения сигнала.

        Если pair передана:
            анализируем только её.

        Если pair не передана:
            анализируем все пары и выбираем лучшую.
        """

        if self.scan_lock.locked():
            print(
                "[SCHEDULER] Manual request: "
                "another scan is already running"
            )

        async with self.scan_lock:
            started = _now()

            print(
                "\n"
                "==================================================\n"
                "[MANUAL SIGNAL] START\n"
                f"[MANUAL SIGNAL] Time: "
                f"{started.strftime('%Y-%m-%d %H:%M:%S')}\n"
                "=================================================="
            )

            if pair:
                pairs = [_normalize_pair(pair)]
            else:
                pairs = await self.get_available_pairs()

            if not pairs:
                print(
                    "[MANUAL SIGNAL] No pairs available"
                )
                return None

            print(
                f"[MANUAL SIGNAL] Checking {len(pairs)} pair(s): "
                f"{', '.join(pairs)}"
            )

            signals: list[Any] = []
            diagnostics: list[dict[str, Any]] = []

            for current_pair in pairs:

                try:
                    signal, diagnostic = await asyncio.wait_for(
                        self.analyze_pair(current_pair),
                        timeout=ANALYSIS_TIMEOUT,
                    )

                except asyncio.TimeoutError:
                    diagnostic = {
                        "pair": current_pair,
                        "status": "TIMEOUT",
                        "candles": 0,
                        "quality": 0.0,
                        "probability": 0.0,
                        "direction": "",
                        "reason": (
                            f"analysis timeout "
                            f"{ANALYSIS_TIMEOUT}s"
                        ),
                    }

                    signal = None

                    print(
                        f"[ANALYSIS] {current_pair}: "
                        f"TIMEOUT after {ANALYSIS_TIMEOUT}s"
                    )

                except asyncio.CancelledError:
                    raise

                except Exception as exc:
                    diagnostic = {
                        "pair": current_pair,
                        "status": "ERROR",
                        "candles": 0,
                        "quality": 0.0,
                        "probability": 0.0,
                        "direction": "",
                        "reason": (
                            f"{type(exc).__name__}: {exc}"
                        ),
                    }

                    signal = None

                    print(
                        f"[ANALYSIS] {current_pair}: "
                        f"UNEXPECTED ERROR: "
                        f"{type(exc).__name__}: {exc}"
                    )

                    traceback.print_exc()

                diagnostics.append(diagnostic)

                if signal is not None:
                    signals.append(signal)

                # Маленькая пауза между API-запросами.
                if len(pairs) > 1:
                    await asyncio.sleep(PAIR_ANALYSIS_DELAY)

            self.last_scan_time = _now()
            self.last_scan_results = diagnostics

            best_signal = self.choose_best(signals)

            if best_signal is None:
                print(
                    "\n"
                    "[MANUAL SIGNAL] NO STRONG SIGNAL\n"
                )

                self._print_scan_summary(diagnostics)

                return None

            self.last_signal = best_signal

            pair_name = _normalize_pair(
                _get_attr_or_dict(
                    best_signal,
                    "pair",
                    "",
                )
            )

            direction = _normalize_direction(
                _get_attr_or_dict(
                    best_signal,
                    "direction",
                    "",
                )
            )

            quality = _safe_float(
                _get_attr_or_dict(
                    best_signal,
                    "quality",
                    0,
                )
            )

            probability = _safe_float(
                _get_attr_or_dict(
                    best_signal,
                    "probability",
                    0,
                )
            )

            print(
                "\n"
                "==================================================\n"
                "[MANUAL SIGNAL] BEST SIGNAL\n"
                f"Pair: {pair_name}\n"
                f"Direction: {direction}\n"
                f"Quality: {quality:.1f}\n"
                f"Probability: {probability:.1f}%\n"
                "==================================================\n"
            )

            return best_signal

    # ========================================================
    # SCAN ONCE
    # ========================================================

    async def scan_once(self):
        """
        Один автоматический цикл.

        Анализирует все пары, выбирает лучший сигнал,
        сохраняет его в БД и отправляет пользователям.
        """

        if self.scan_lock.locked():
            print(
                "[SCHEDULER] scan_once skipped: "
                "another scan is running"
            )
            return None

        async with self.scan_lock:
            started = _now()

            print(
                "\n"
                "==================================================\n"
                "[AUTO SCAN] START\n"
                f"[AUTO SCAN] Time: "
                f"{started.strftime('%Y-%m-%d %H:%M:%S')}\n"
                "=================================================="
            )

            pairs = await self.get_available_pairs()

            if not pairs:
                print(
                    "[AUTO SCAN] No pairs available"
                )
                return None

            print(
                f"[AUTO SCAN] Checking {len(pairs)} pairs: "
                f"{', '.join(pairs)}"
            )

            signals: list[Any] = []
            diagnostics: list[dict[str, Any]] = []

            for pair in pairs:

                try:
                    signal, diagnostic = await asyncio.wait_for(
                        self.analyze_pair(pair),
                        timeout=ANALYSIS_TIMEOUT,
                    )

                except asyncio.TimeoutError:
                    signal = None

                    diagnostic = {
                        "pair": pair,
                        "status": "TIMEOUT",
                        "candles": 0,
                        "quality": 0.0,
                        "probability": 0.0,
                        "direction": "",
                        "reason": (
                            f"analysis timeout "
                            f"{ANALYSIS_TIMEOUT}s"
                        ),
                    }

                    print(
                        f"[AUTO SCAN] {pair}: "
                        f"TIMEOUT"
                    )

                except asyncio.CancelledError:
                    raise

                except Exception as exc:
                    signal = None

                    diagnostic = {
                        "pair": pair,
                        "status": "ERROR",
                        "candles": 0,
                        "quality": 0.0,
                        "probability": 0.0,
                        "direction": "",
                        "reason": (
                            f"{type(exc).__name__}: {exc}"
                        ),
                    }

                    print(
                        f"[AUTO SCAN] {pair}: ERROR | "
                        f"{type(exc).__name__}: {exc}"
                    )

                    traceback.print_exc()

                diagnostics.append(diagnostic)

                if signal is not None:
                    signals.append(signal)

                await asyncio.sleep(PAIR_ANALYSIS_DELAY)

            self.last_scan_time = _now()
            self.last_scan_results = diagnostics

            best_signal = self.choose_best(signals)

            # ------------------------------------------------
            # NO SIGNAL
            # ------------------------------------------------

            if best_signal is None:
                print(
                    "\n"
                    "[AUTO SCAN] Strong signal not found.\n"
                    f"[AUTO SCAN] Required Quality >= {MIN_QUALITY}\n"
                    f"[AUTO SCAN] Required Probability >= "
                    f"{MIN_PROBABILITY}%\n"
                )

                self._print_scan_summary(diagnostics)

                return None

            # ------------------------------------------------
            # BEST SIGNAL
            # ------------------------------------------------

            self.last_signal = best_signal

            pair_name = _normalize_pair(
                _get_attr_or_dict(
                    best_signal,
                    "pair",
                    "",
                )
            )

            direction = _normalize_direction(
                _get_attr_or_dict(
                    best_signal,
                    "direction",
                    "",
                )
            )

            quality = _safe_float(
                _get_attr_or_dict(
                    best_signal,
                    "quality",
                    0,
                )
            )

            probability = _safe_float(
                _get_attr_or_dict(
                    best_signal,
                    "probability",
                    0,
                )
            )

            print(
                "\n"
                "==================================================\n"
                "[AUTO SCAN] SIGNAL FOUND\n"
                f"Pair: {pair_name}\n"
                f"Direction: {direction}\n"
                f"Quality: {quality:.1f}\n"
                f"Probability: {probability:.1f}%\n"
                "=================================================="
            )

            # ------------------------------------------------
            # SAVE
            # ------------------------------------------------

            try:
                await self.save_signal(best_signal)

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                print(
                    f"[DB] save signal ERROR: "
                    f"{type(exc).__name__}: {exc}"
                )
                traceback.print_exc()

            # ------------------------------------------------
            # SEND
            # ------------------------------------------------

            try:
                await self.send_to_users(best_signal)

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                print(
                    f"[TELEGRAM] send signal ERROR: "
                    f"{type(exc).__name__}: {exc}"
                )
                traceback.print_exc()

            return best_signal

    # ========================================================
    # DB
    # ========================================================

    async def save_signal(self, signal: Any):
        """
        Сохраняет сигнал в существующую database.py.

        Поддерживает несколько вариантов сигнатуры,
        чтобы не ломать текущую БД.
        """

        if db is None:
            print(
                "[DB] Database object is None"
            )
            return

        method = getattr(
            db,
            "save_signal",
            None,
        )

        if method is None:
            print(
                "[DB] save_signal() not found"
            )
            return

        pair = _normalize_pair(
            _get_attr_or_dict(
                signal,
                "pair",
                "",
            )
        )

        direction = _normalize_direction(
            _get_attr_or_dict(
                signal,
                "direction",
                "",
            )
        )

        quality = _safe_float(
            _get_attr_or_dict(
                signal,
                "quality",
                0,
            )
        )

        probability = _safe_float(
            _get_attr_or_dict(
                signal,
                "probability",
                0,
            )
        )

        entry_time = _get_attr_or_dict(
            signal,
            "entry_time",
            None,
        )

        expiry_time = _get_attr_or_dict(
            signal,
            "expiry_time",
            None,
        )

        analysis_time = _get_attr_or_dict(
            signal,
            "analysis_time",
            None,
        )

        confirmations = _get_attr_or_dict(
            signal,
            "confirmations",
            None,
        )

        reasons = _get_attr_or_dict(
            signal,
            "reasons",
            None,
        )

        # Первый вариант — именованные аргументы.
        try:
            result = method(
                pair=pair,
                direction=direction,
                quality=quality,
                probability=probability,
                entry_time=entry_time,
                expiry_time=expiry_time,
                analysis_time=analysis_time,
                confirmations=confirmations,
                reasons=reasons,
            )

            if inspect.isawaitable(result):
                await result

            print(
                f"[DB] Signal saved: "
                f"{pair} {direction}"
            )

            return

        except TypeError:
            pass

        except Exception:
            raise

        # Второй вариант — передать сам объект.
        try:
            result = method(signal)

            if inspect.isawaitable(result):
                await result

            print(
                f"[DB] Signal saved: "
                f"{pair} {direction}"
            )

        except Exception as exc:
            print(
                f"[DB] save_signal fallback ERROR: "
                f"{type(exc).__name__}: {exc}"
            )
            raise

    # ========================================================
    # SEND TO USERS
    # ========================================================

    async def send_to_users(self, signal: Any):
        """
        Отправляет сигнал активным/одобренным пользователям.

        Использует существующий database.py.
        """

        if self.bot is None:
            print(
                "[TELEGRAM] Bot is not configured"
            )
            return

        if db is None:
            print(
                "[TELEGRAM] DB is not configured"
            )
            return

        # ----------------------------------------------------
        # Получаем пользователей
        # ----------------------------------------------------

        users = None

        possible_methods = (
            "get_approved_users",
            "get_active_users",
            "get_users",
        )

        for method_name in possible_methods:
            method = getattr(
                db,
                method_name,
                None,
            )

            if method is None:
                continue

            try:
                result = method()

                if inspect.isawaitable(result):
                    result = await result

                if result is not None:
                    users = result
                    break

            except TypeError:
                continue

            except Exception as exc:
                print(
                    f"[DB] {method_name} ERROR: "
                    f"{type(exc).__name__}: {exc}"
                )

        if not users:
            print(
                "[TELEGRAM] No users to notify"
            )
            return

        text = self.format_signal(signal)

        sent = 0
        failed = 0

        for user in users:

            user_id = None

            if isinstance(user, int):
                user_id = user

            elif isinstance(user, str):
                try:
                    user_id = int(user)
                except ValueError:
                    user_id = None

            elif isinstance(user, dict):
                user_id = (
                    user.get("telegram_id")
                    or user.get("user_id")
                    or user.get("id")
                )

            else:
                user_id = (
                    getattr(user, "telegram_id", None)
                    or getattr(user, "user_id", None)
                    or getattr(user, "id", None)
                )

            if not user_id:
                continue

            try:
                await self.bot.send_message(
                    chat_id=int(user_id),
                    text=text,
                )

                sent += 1

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                failed += 1

                print(
                    f"[TELEGRAM] Failed to send "
                    f"to {user_id}: "
                    f"{type(exc).__name__}: {exc}"
                )

        print(
            f"[TELEGRAM] Signal notification complete | "
            f"sent={sent} failed={failed}"
        )

    # ========================================================
    # FORMAT
    # ========================================================

    def format_signal(self, signal: Any) -> str:
        pair = _normalize_pair(
            _get_attr_or_dict(
                signal,
                "pair",
                "UNKNOWN",
            )
        )

        direction = _normalize_direction(
            _get_attr_or_dict(
                signal,
                "direction",
                "",
            )
        )

        quality = _safe_float(
            _get_attr_or_dict(
                signal,
                "quality",
                0,
            )
        )

        probability = _safe_float(
            _get_attr_or_dict(
                signal,
                "probability",
                0,
            )
        )

        entry_time = _get_attr_or_dict(
            signal,
            "entry_time",
            None,
        )

        expiry_time = _get_attr_or_dict(
            signal,
            "expiry_time",
            None,
        )

        confirmations = _get_attr_or_dict(
            signal,
            "confirmations",
            None,
        )

        reasons = _get_attr_or_dict(
            signal,
            "reasons",
            None,
        )

        if direction == "CALL":
            direction_text = "🟢 CALL ↑"
        elif direction == "PUT":
            direction_text = "🔴 PUT ↓"
        else:
            direction_text = f"⚪ {direction or 'UNKNOWN'}"

        lines = [
            "🚨 СИЛЬНЫЙ СИГНАЛ",
            "",
            f"{direction_text}",
            "",
            f"💱 Пара: {pair}",
            f"📊 Качество: {quality:.1f}/100",
            f"🎯 Историческая вероятность: {probability:.1f}%",
        ]

        if entry_time is not None:
            lines.append(
                f"🕐 Вход: {self._format_datetime(entry_time)}"
            )

        if expiry_time is not None:
            lines.append(
                f"⏱ Закрытие: {self._format_datetime(expiry_time)}"
            )

        if confirmations:
            if isinstance(confirmations, (list, tuple, set)):
                confirmation_text = ", ".join(
                    str(item)
                    for item in confirmations
                    if item
                )
            else:
                confirmation_text = str(confirmations)

            if confirmation_text:
                lines.append(
                    f"✅ Подтверждения: {confirmation_text}"
                )

        if reasons:
            if isinstance(reasons, (list, tuple, set)):
                clean_reasons = [
                    str(item)
                    for item in reasons
                    if item
                ]

                clean_reasons = clean_reasons[:MAX_REASONS]

                if clean_reasons:
                    lines.append("")
                    lines.append("📌 Причины:")

                    for reason in clean_reasons:
                        lines.append(
                            f"• {reason}"
                        )

            elif isinstance(reasons, str) and reasons.strip():
                lines.append("")
                lines.append(
                    f"📌 {reasons.strip()}"
                )

        return "\n".join(lines)

    # ========================================================
    # DATETIME FORMAT
    # ========================================================

    @staticmethod
    def _format_datetime(value: Any) -> str:
        if value is None:
            return ""

        if isinstance(value, datetime):
            dt = value

            if dt.tzinfo is None:
                dt = dt.replace(
                    tzinfo=MOSCOW_TZ
                )

            dt = dt.astimezone(MOSCOW_TZ)

            return dt.strftime(
                "%H:%M:%S"
            )

        return str(value)

    # ========================================================
    # DIAGNOSTICS
    # ========================================================

    @staticmethod
    def _print_scan_summary(
        diagnostics: list[dict[str, Any]],
    ) -> None:

        if not diagnostics:
            print(
                "[SCHEDULER] No diagnostics"
            )
            return

        print(
            "\n"
            "[SCHEDULER] ===============================\n"
            "[SCHEDULER] SCAN SUMMARY"
        )

        for item in diagnostics:
            pair = item.get(
                "pair",
                "?",
            )

            status = item.get(
                "status",
                "?",
            )

            quality = _safe_float(
                item.get(
                    "quality",
                    0,
                )
            )

            probability = _safe_float(
                item.get(
                    "probability",
                    0,
                )
            )

            reason = item.get(
                "reason",
                "",
            )

            print(
                f"[SCHEDULER] {pair}: "
                f"{status} | "
                f"Q={quality:.1f} | "
                f"P={probability:.1f}% | "
                f"{reason}"
            )

        print(
            "[SCHEDULER] ===============================\n"
        )

    # ========================================================
    # RUN
    # ========================================================

    async def run(self):
        """
        Основной бесконечный цикл.

        Анализ запускается на границах:
        00, 05, 10, 15, 20...
        по московскому времени.
        """

        if self.running:
            print(
                "[SCHEDULER] run() already active"
            )
            return

        self.running = True

        print(
            "[SCHEDULER] Automatic signal scheduler started"
        )

        try:
            while self.running:

                now = _now()
                next_run = _next_5_minute_boundary(now)

                wait_seconds = (
                    next_run - now
                ).total_seconds()

                if wait_seconds < 0:
                    wait_seconds = 0

                print(
                    f"[SCHEDULER] Next scan: "
                    f"{next_run.strftime('%H:%M:%S')} "
                    f"(in {wait_seconds:.1f}s)"
                )

                try:
                    await asyncio.sleep(
                        wait_seconds
                    )

                except asyncio.CancelledError:
                    raise

                if not self.running:
                    break

                try:
                    await self.scan_once()

                except asyncio.CancelledError:
                    raise

                except Exception as exc:
                    print(
                        "[SCHEDULER] scan error: "
                        f"{type(exc).__name__}: {exc}"
                    )

                    traceback.print_exc()

                    # Небольшая пауза, чтобы ошибка API
                    # не превратилась в бесконечный спам.
                    await asyncio.sleep(
                        ERROR_RETRY_DELAY
                    )

        except asyncio.CancelledError:
            print(
                "[SCHEDULER] Scheduler cancelled"
            )

            self.running = False

            raise

        except Exception as exc:
            print(
                "[SCHEDULER] FATAL ERROR: "
                f"{type(exc).__name__}: {exc}"
            )

            traceback.print_exc()

        finally:
            self.running = False

            print(
                "[SCHEDULER] Automatic scheduler stopped"
            )

    # ========================================================
    # STOP
    # ========================================================

    def stop(self):
        self.running = False

        print(
            "[SCHEDULER] Stop requested"
        )


# ============================================================
# IMPORTANT
# ============================================================
#
# Здесь НЕ создаём:
#
# scheduler = SignalScheduler(...)
#
# Потому что main.py сам создаёт:
#
# scheduler = SignalScheduler(bot)
#
# ============================================================
