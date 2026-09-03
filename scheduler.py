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

try:
    MOSCOW_TZ = ZoneInfo(TIMEZONE)
except Exception:
    MOSCOW_TZ = ZoneInfo("Europe/Moscow")

SIGNAL_INTERVAL_MINUTES = 5
MIN_CANDLES_REQUIRED = 80

PAIR_ANALYSIS_DELAY = 0.15
ANALYSIS_TIMEOUT = 30
ERROR_RETRY_DELAY = 15

MAX_REASONS = 8
MAX_CONFIRMATIONS = 8


# ============================================================
# HELPERS
# ============================================================

def _safe_float(
    value: Any,
    default: float = 0.0,
) -> float:
    try:
        if value is None:
            return default

        if isinstance(value, str):
            value = (
                value
                .replace("%", "")
                .replace(",", ".")
                .strip()
            )

        result = float(value)

        if result != result:
            return default

        return result

    except (
        TypeError,
        ValueError,
    ):
        return default


def _normalize_pair(
    pair: Any,
) -> str:
    if pair is None:
        return ""

    return str(pair).strip().upper()


def _normalize_direction(
    direction: Any,
) -> str:

    if direction is None:
        return ""

    value = (
        str(direction)
        .strip()
        .upper()
    )

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

    return mapping.get(
        value,
        value,
    )


def _get_value(
    obj: Any,
    name: str,
    default: Any = None,
) -> Any:

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


def _set_value(
    obj: Any,
    name: str,
    value: Any,
) -> None:

    if obj is None:
        return

    if isinstance(
        obj,
        dict,
    ):
        obj[name] = value
        return

    try:
        setattr(
            obj,
            name,
            value,
        )
    except Exception:
        pass


def _now() -> datetime:
    return datetime.now(
        MOSCOW_TZ
    )


def _next_analysis_time(
    current: datetime | None = None,
) -> datetime:

    current = current or _now()

    current = current.replace(
        second=0,
        microsecond=0,
    )

    next_minute = (
        (current.minute // SIGNAL_INTERVAL_MINUTES)
        + 1
    ) * SIGNAL_INTERVAL_MINUTES

    if next_minute >= 60:

        return (
            current
            + timedelta(hours=1)
        ).replace(
            minute=0,
            second=0,
            microsecond=0,
        )

    return current.replace(
        minute=next_minute,
        second=0,
        microsecond=0,
    )


# ============================================================
# SCHEDULER
# ============================================================

class SignalScheduler:

    def __init__(
        self,
        bot=None,
    ) -> None:

        self.bot = bot

        self.engine = SignalEngine()

        self.running = False

        self.scan_lock = asyncio.Lock()

        self.last_scan_time: datetime | None = None
        self.last_signal = None

        self.last_scan_results: list[
            dict[str, Any]
        ] = []

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

    def set_bot(
        self,
        bot,
    ) -> None:

        self.bot = bot

    # ========================================================
    # TIME
    # ========================================================

    @staticmethod
    def now() -> datetime:
        return _now()

    @staticmethod
    def next_analysis_time() -> datetime:
        return _next_analysis_time()

    # ========================================================
    # PAIRS
    # ========================================================

    async def get_available_pairs(
        self,
    ) -> list[str]:

        result: list[str] = []

        # Используем PAIRS из config.py.
        for pair in PAIRS:

            normalized = _normalize_pair(
                pair
            )

            if not normalized:
                continue

            if normalized not in result:
                result.append(
                    normalized
                )

        # Если PAIRS пустой — пробуем MarketClient.
        if not result:

            try:

                method = getattr(
                    market_client,
                    "get_available_pairs",
                    None,
                )

                if method is not None:

                    available = method()

                    if inspect.isawaitable(
                        available
                    ):
                        available = await available

                    if available:

                        for pair in available:

                            normalized = (
                                _normalize_pair(
                                    pair
                                )
                            )

                            if (
                                normalized
                                and normalized not in result
                            ):
                                result.append(
                                    normalized
                                )

            except Exception as exc:

                print(
                    "[SCHEDULER] "
                    "get_available_pairs error: "
                    f"{type(exc).__name__}: {exc}"
                )

        return result

    # ========================================================
    # CANDLES
    # ========================================================

    async def get_candles(
        self,
        pair: str,
    ):

        pair = _normalize_pair(
            pair
        )

        if not pair:
            return None

        method = getattr(
            market_client,
            "get_candles",
            None,
        )

        if method is None:

            print(
                "[MARKET] ERROR: "
                "market_client.get_candles() "
                "не найден"
            )

            return None

        try:

            candles = method(
                pair
            )

            if inspect.isawaitable(
                candles
            ):
                candles = await candles

            if candles is None:

                print(
                    f"[MARKET] {pair}: "
                    "candles=None"
                )

                return None

            try:
                count = len(candles)
            except Exception:
                count = 0

            if count == 0:

                print(
                    f"[MARKET] {pair}: "
                    "0 candles"
                )

                return None

            print(
                f"[MARKET] {pair}: "
                f"получено {count} свечей"
            )

            return candles

        except asyncio.CancelledError:
            raise

        except Exception as exc:

            print(
                f"[MARKET] {pair}: "
                f"get_candles ERROR: "
                f"{type(exc).__name__}: {exc}"
            )

            traceback.print_exc()

            return None

    # ========================================================
    # ANALYZE PAIR
    # ========================================================

    async def analyze_pair(
        self,
        pair: str,
    ) -> tuple[Any | None, dict[str, Any]]:

        pair = _normalize_pair(
            pair
        )

        diagnostic = {
            "pair": pair,
            "status": "ERROR",
            "candles": 0,
            "quality": 0.0,
            "probability": 0.0,
            "direction": "",
            "reason": "",
        }

        if not pair:

            diagnostic["reason"] = (
                "empty pair"
            )

            return (
                None,
                diagnostic,
            )

        # ----------------------------------------------------
        # GET CANDLES
        # ----------------------------------------------------

        candles = await self.get_candles(
            pair
        )

        if candles is None:

            diagnostic["status"] = (
                "NO_DATA"
            )

            diagnostic["reason"] = (
                "candles unavailable"
            )

            print(
                f"[ANALYSIS] {pair}: "
                "REJECT | candles unavailable"
            )

            return (
                None,
                diagnostic,
            )

        try:
            candle_count = len(
                candles
            )
        except Exception:
            candle_count = 0

        diagnostic["candles"] = (
            candle_count
        )

        if candle_count < MIN_CANDLES_REQUIRED:

            diagnostic["status"] = (
                "NOT_ENOUGH_CANDLES"
            )

            diagnostic["reason"] = (
                f"{candle_count} candles; "
                f"minimum={MIN_CANDLES_REQUIRED}"
            )

            print(
                f"[ANALYSIS] {pair}: "
                f"REJECT | "
                f"{candle_count}/"
                f"{MIN_CANDLES_REQUIRED} candles"
            )

            return (
                None,
                diagnostic,
            )

        # ----------------------------------------------------
        # SIGNAL ENGINE
        # ----------------------------------------------------

        try:

            analyze_method = getattr(
                self.engine,
                "analyze",
                None,
            )

            if analyze_method is None:

                diagnostic["status"] = (
                    "ENGINE_ERROR"
                )

                diagnostic["reason"] = (
                    "SignalEngine.analyze() not found"
                )

                print(
                    f"[ANALYSIS] {pair}: "
                    "ERROR | analyze() not found"
                )

                return (
                    None,
                    diagnostic,
                )

            # ==================================================
            # КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ
            # ==================================================
            #
            # Твой SignalEngine имеет:
            #
            # analyze(self, pair, candles)
            #
            # Поэтому передаём ОБА аргумента.
            #

            result = analyze_method(
                pair,
                candles,
            )

            if inspect.isawaitable(
                result
            ):
                result = await result

        except asyncio.CancelledError:
            raise

        except Exception as exc:

            diagnostic["status"] = (
                "ENGINE_ERROR"
            )

            diagnostic["reason"] = (
                f"{type(exc).__name__}: {exc}"
            )

            print(
                f"[ANALYSIS] {pair}: "
                f"ENGINE ERROR: "
                f"{type(exc).__name__}: {exc}"
            )

            traceback.print_exc()

            return (
                None,
                diagnostic,
            )

        # ----------------------------------------------------
        # NO SIGNAL
        # ----------------------------------------------------

        if result is None:

            diagnostic["status"] = (
                "NO_SIGNAL"
            )

            diagnostic["reason"] = (
                "SignalEngine returned None"
            )

            print(
                f"[ANALYSIS] {pair}: "
                "REJECT | engine returned None"
            )

            return (
                None,
                diagnostic,
            )

        # ----------------------------------------------------
        # SIGNAL VALUES
        # ----------------------------------------------------

        signal_pair = _normalize_pair(
            _get_value(
                result,
                "pair",
                pair,
            )
        )

        if not signal_pair:

            signal_pair = pair

            _set_value(
                result,
                "pair",
                pair,
            )

        raw_direction = _get_value(
            result,
            "direction",
            "",
        )

        direction = _normalize_direction(
            raw_direction
        )

        quality = _safe_float(
            _get_value(
                result,
                "quality",
                0,
            )
        )

        probability = _safe_float(
            _get_value(
                result,
                "probability",
                0,
            )
        )

        diagnostic["direction"] = (
            direction
        )

        diagnostic["quality"] = (
            quality
        )

        diagnostic["probability"] = (
            probability
        )

        # ----------------------------------------------------
        # DIRECTION
        # ----------------------------------------------------

        if direction not in {
            "CALL",
            "PUT",
        }:

            diagnostic["status"] = (
                "INVALID_DIRECTION"
            )

            diagnostic["reason"] = (
                f"direction={raw_direction}"
            )

            print(
                f"[ANALYSIS] {pair}: "
                f"REJECT | invalid direction="
                f"{raw_direction}"
            )

            return (
                None,
                diagnostic,
            )

        _set_value(
            result,
            "direction",
            direction,
        )

        # ----------------------------------------------------
        # QUALITY
        # ----------------------------------------------------

        if quality < float(
            MIN_QUALITY
        ):

            diagnostic["status"] = (
                "LOW_QUALITY"
            )

            diagnostic["reason"] = (
                f"Quality {quality:.1f} "
                f"< {MIN_QUALITY}"
            )

            print(
                f"[ANALYSIS] {pair}: "
                f"REJECT | "
                f"Quality={quality:.1f} "
                f"< {MIN_QUALITY} | "
                f"Probability={probability:.1f}%"
            )

            return (
                None,
                diagnostic,
            )

        # ----------------------------------------------------
        # PROBABILITY
        # ----------------------------------------------------

        if probability < float(
            MIN_PROBABILITY
        ):

            diagnostic["status"] = (
                "LOW_PROBABILITY"
            )

            diagnostic["reason"] = (
                f"Probability "
                f"{probability:.1f}% "
                f"< {MIN_PROBABILITY}%"
            )

            print(
                f"[ANALYSIS] {pair}: "
                f"REJECT | "
                f"Quality={quality:.1f} | "
                f"Probability={probability:.1f}% "
                f"< {MIN_PROBABILITY}%"
            )

            return (
                None,
                diagnostic,
            )

        # ----------------------------------------------------
        # ACCEPT
        # ----------------------------------------------------

        diagnostic["status"] = (
            "ACCEPTED"
        )

        diagnostic["reason"] = (
            "passed all filters"
        )

        print(
            f"[ANALYSIS] {pair}: "
            f"ACCEPT | "
            f"{direction} | "
            f"Quality={quality:.1f} | "
            f"Probability={probability:.1f}%"
        )

        return (
            result,
            diagnostic,
        )

    # ========================================================
    # CHOOSE BEST
    # ========================================================

    @staticmethod
    def _signal_score(
        signal: Any,
    ) -> tuple[float, float]:

        quality = _safe_float(
            _get_value(
                signal,
                "quality",
                0,
            )
        )

        probability = _safe_float(
            _get_value(
                signal,
                "probability",
                0,
            )
        )

        return (
            quality,
            probability,
        )

    def choose_best(
        self,
        signals: list[Any],
    ):

        if not signals:
            return None

        signals = [
            signal
            for signal in signals
            if signal is not None
        ]

        if not signals:
            return None

        signals.sort(
            key=self._signal_score,
            reverse=True,
        )

        return signals[0]

    # ========================================================
    # MANUAL SIGNAL
    # ========================================================

    async def get_manual_signal(
        self,
        pair: str | None = None,
    ):

        async with self.scan_lock:

            print("")
            print(
                "=" * 70
            )
            print(
                "[MANUAL SIGNAL] START"
            )
            print(
                f"[MANUAL SIGNAL] "
                f"{_now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            print(
                "=" * 70
            )

            if pair:

                pairs = [
                    _normalize_pair(
                        pair
                    )
                ]

            else:

                pairs = (
                    await self.get_available_pairs()
                )

            if not pairs:

                print(
                    "[MANUAL SIGNAL] "
                    "No pairs available"
                )

                return None

            print(
                f"[MANUAL SIGNAL] "
                f"Проверяю {len(pairs)} пар:"
            )

            print(
                ", ".join(pairs)
            )

            signals = []
            diagnostics = []

            for current_pair in pairs:

                try:

                    signal, diagnostic = (
                        await asyncio.wait_for(
                            self.analyze_pair(
                                current_pair
                            ),
                            timeout=ANALYSIS_TIMEOUT,
                        )
                    )

                except asyncio.TimeoutError:

                    signal = None

                    diagnostic = {
                        "pair": current_pair,
                        "status": "TIMEOUT",
                        "candles": 0,
                        "quality": 0.0,
                        "probability": 0.0,
                        "direction": "",
                        "reason": (
                            f"timeout "
                            f"{ANALYSIS_TIMEOUT}s"
                        ),
                    }

                    print(
                        f"[ANALYSIS] "
                        f"{current_pair}: "
                        "TIMEOUT"
                    )

                except asyncio.CancelledError:
                    raise

                except Exception as exc:

                    signal = None

                    diagnostic = {
                        "pair": current_pair,
                        "status": "ERROR",
                        "candles": 0,
                        "quality": 0.0,
                        "probability": 0.0,
                        "direction": "",
                        "reason": (
                            f"{type(exc).__name__}: "
                            f"{exc}"
                        ),
                    }

                    print(
                        f"[ANALYSIS] "
                        f"{current_pair}: "
                        f"ERROR: {exc}"
                    )

                    traceback.print_exc()

                diagnostics.append(
                    diagnostic
                )

                if signal is not None:
                    signals.append(
                        signal
                    )

                if len(pairs) > 1:
                    await asyncio.sleep(
                        PAIR_ANALYSIS_DELAY
                    )

            self.last_scan_time = _now()

            self.last_scan_results = (
                diagnostics
            )

            best_signal = (
                self.choose_best(
                    signals
                )
            )

            if best_signal is None:

                print("")
                print(
                    "[MANUAL SIGNAL] "
                    "NO STRONG SIGNAL"
                )

                self._print_scan_summary(
                    diagnostics
                )

                return None

            self.last_signal = (
                best_signal
            )

            print("")
            print(
                "=" * 70
            )
            print(
                "[MANUAL SIGNAL] "
                "BEST SIGNAL"
            )
            print(
                f"Pair: "
                f"{_get_value(best_signal, 'pair', '')}"
            )
            print(
                f"Direction: "
                f"{_get_value(best_signal, 'direction', '')}"
            )
            print(
                f"Quality: "
                f"{_safe_float(_get_value(best_signal, 'quality', 0)):.1f}"
            )
            print(
                f"Probability: "
                f"{_safe_float(_get_value(best_signal, 'probability', 0)):.1f}%"
            )
            print(
                "=" * 70
            )

            return best_signal

    # ========================================================
    # AUTO SCAN
    # ========================================================

    async def scan_once(
        self,
    ):

        if self.scan_lock.locked():

            print(
                "[SCHEDULER] "
                "scan_once skipped: "
                "another scan is running"
            )

            return None

        async with self.scan_lock:

            print("")
            print(
                "=" * 70
            )
            print(
                "[AUTO SCAN] START"
            )
            print(
                f"[AUTO SCAN] "
                f"{_now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            print(
                "=" * 70
            )

            pairs = (
                await self.get_available_pairs()
            )

            if not pairs:

                print(
                    "[AUTO SCAN] "
                    "No pairs available"
                )

                return None

            print(
                f"[AUTO SCAN] "
                f"Проверяю {len(pairs)} пар: "
                f"{', '.join(pairs)}"
            )

            signals = []
            diagnostics = []

            for pair in pairs:

                try:

                    signal, diagnostic = (
                        await asyncio.wait_for(
                            self.analyze_pair(
                                pair
                            ),
                            timeout=ANALYSIS_TIMEOUT,
                        )
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
                            f"timeout "
                            f"{ANALYSIS_TIMEOUT}s"
                        ),
                    }

                    print(
                        f"[AUTO SCAN] "
                        f"{pair}: TIMEOUT"
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
                            f"{type(exc).__name__}: "
                            f"{exc}"
                        ),
                    }

                    print(
                        f"[AUTO SCAN] "
                        f"{pair}: ERROR: {exc}"
                    )

                    traceback.print_exc()

                diagnostics.append(
                    diagnostic
                )

                if signal is not None:
                    signals.append(
                        signal
                    )

                await asyncio.sleep(
                    PAIR_ANALYSIS_DELAY
                )

            self.last_scan_time = _now()

            self.last_scan_results = (
                diagnostics
            )

            best_signal = (
                self.choose_best(
                    signals
                )
            )

            if best_signal is None:

                print("")
                print(
                    "[AUTO SCAN] "
                    "Сильного сигнала нет"
                )

                print(
                    f"[AUTO SCAN] "
                    f"Требуется Quality >= "
                    f"{MIN_QUALITY}"
                )

                print(
                    f"[AUTO SCAN] "
                    f"Требуется Probability >= "
                    f"{MIN_PROBABILITY}%"
                )

                self._print_scan_summary(
                    diagnostics
                )

                return None

            self.last_signal = (
                best_signal
            )

            print("")
            print(
                "=" * 70
            )
            print(
                "[AUTO SCAN] SIGNAL FOUND"
            )
            print(
                f"Pair: "
                f"{_get_value(best_signal, 'pair', '')}"
            )
            print(
                f"Direction: "
                f"{_get_value(best_signal, 'direction', '')}"
            )
            print(
                f"Quality: "
                f"{_safe_float(_get_value(best_signal, 'quality', 0)):.1f}"
            )
            print(
                f"Probability: "
                f"{_safe_float(_get_value(best_signal, 'probability', 0)):.1f}%"
            )
            print(
                "=" * 70
            )

            # ------------------------------------------------
            # SAVE
            # ------------------------------------------------

            try:

                await self.save_signal(
                    best_signal
                )

            except asyncio.CancelledError:
                raise

            except Exception as exc:

                print(
                    "[DB] "
                    f"save_signal ERROR: "
                    f"{type(exc).__name__}: {exc}"
                )

                traceback.print_exc()

            # ------------------------------------------------
            # SEND
            # ------------------------------------------------

            try:

                await self.send_to_users(
                    best_signal
                )

            except asyncio.CancelledError:
                raise

            except Exception as exc:

                print(
                    "[TELEGRAM] "
                    f"send_to_users ERROR: "
                    f"{type(exc).__name__}: {exc}"
                )

                traceback.print_exc()

            return best_signal

    # ========================================================
    # SAVE SIGNAL
    # ========================================================

    async def save_signal(
        self,
        signal: Any,
    ) -> None:

        if db is None:

            print(
                "[DB] db is None"
            )

            return

        method = getattr(
            db,
            "save_signal",
            None,
        )

        if method is None:

            print(
                "[DB] "
                "save_signal() not found"
            )

            return

        pair = _normalize_pair(
            _get_value(
                signal,
                "pair",
                "",
            )
        )

        direction = _normalize_direction(
            _get_value(
                signal,
                "direction",
                "",
            )
        )

        quality = _safe_float(
            _get_value(
                signal,
                "quality",
                0,
            )
        )

        probability = _safe_float(
            _get_value(
                signal,
                "probability",
                0,
            )
        )

        entry_time = _get_value(
            signal,
            "entry_time",
            None,
        )

        expiry_time = _get_value(
            signal,
            "expiry_time",
            None,
        )

        analysis_time = _get_value(
            signal,
            "analysis_time",
            None,
        )

        confirmations = _get_value(
            signal,
            "confirmations",
            None,
        )

        reasons = _get_value(
            signal,
            "reasons",
            None,
        )

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

            if inspect.isawaitable(
                result
            ):
                await result

            print(
                f"[DB] "
                f"Signal saved: "
                f"{pair} {direction}"
            )

            return

        except TypeError:
            pass

        result = method(
            signal
        )

        if inspect.isawaitable(
            result
        ):
            await result

        print(
            f"[DB] "
            f"Signal saved: "
            f"{pair} {direction}"
        )

    # ========================================================
    # SEND USERS
    # ========================================================

    async def send_to_users(
        self,
        signal: Any,
    ) -> None:

        if self.bot is None:

            print(
                "[TELEGRAM] "
                "bot is None"
            )

            return

        if db is None:

            print(
                "[TELEGRAM] "
                "db is None"
            )

            return

        users = None

        methods = (
            "get_approved_users",
            "get_active_users",
            "get_users",
        )

        for method_name in methods:

            method = getattr(
                db,
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

                if result is not None:

                    users = result

                    break

            except TypeError:
                continue

            except Exception as exc:

                print(
                    f"[DB] "
                    f"{method_name} ERROR: "
                    f"{type(exc).__name__}: {exc}"
                )

        if not users:

            print(
                "[TELEGRAM] "
                "No users to notify"
            )

            return

        text = self.format_signal(
            signal
        )

        sent = 0
        failed = 0

        for user in users:

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
                    user_id = int(
                        user
                    )
                except ValueError:
                    user_id = None

            elif isinstance(
                user,
                dict,
            ):

                user_id = (
                    user.get(
                        "telegram_id"
                    )
                    or user.get(
                        "user_id"
                    )
                    or user.get(
                        "id"
                    )
                )

            else:

                user_id = (
                    getattr(
                        user,
                        "telegram_id",
                        None,
                    )
                    or getattr(
                        user,
                        "user_id",
                        None,
                    )
                    or getattr(
                        user,
                        "id",
                        None,
                    )
                )

            if not user_id:
                continue

            try:

                await self.bot.send_message(
                    chat_id=int(
                        user_id
                    ),
                    text=text,
                )

                sent += 1

            except asyncio.CancelledError:
                raise

            except Exception as exc:

                failed += 1

                print(
                    f"[TELEGRAM] "
                    f"Failed to send "
                    f"{user_id}: "
                    f"{type(exc).__name__}: {exc}"
                )

        print(
            "[TELEGRAM] "
            f"complete | "
            f"sent={sent} | "
            f"failed={failed}"
        )

    # ========================================================
    # FORMAT SIGNAL
    # ========================================================

    def format_signal(
        self,
        signal: Any,
    ) -> str:

        pair = _normalize_pair(
            _get_value(
                signal,
                "pair",
                "UNKNOWN",
            )
        )

        direction = _normalize_direction(
            _get_value(
                signal,
                "direction",
                "",
            )
        )

        quality = _safe_float(
            _get_value(
                signal,
                "quality",
                0,
            )
        )

        probability = _safe_float(
            _get_value(
                signal,
                "probability",
                0,
            )
        )

        entry_time = _get_value(
            signal,
            "entry_time",
            None,
        )

        expiry_time = _get_value(
            signal,
            "expiry_time",
            None,
        )

        confirmations = _get_value(
            signal,
            "confirmations",
            None,
        )

        reasons = _get_value(
            signal,
            "reasons",
            None,
        )

        if direction == "CALL":
            direction_text = (
                "🟢 CALL ↑"
            )

        elif direction == "PUT":
            direction_text = (
                "🔴 PUT ↓"
            )

        else:
            direction_text = (
                f"⚪ {direction or 'UNKNOWN'}"
            )

        lines = [
            "🚨 СИЛЬНЫЙ СИГНАЛ",
            "",
            direction_text,
            "",
            f"💱 Пара: {pair}",
            f"📊 Качество: {quality:.1f}/100",
            (
                "🎯 Расчётная вероятность: "
                f"{probability:.1f}%"
            ),
        ]

        if entry_time is not None:

            lines.append(
                "🕐 Вход: "
                f"{self._format_datetime(entry_time)}"
            )

        if expiry_time is not None:

            lines.append(
                "⏱ Закрытие: "
                f"{self._format_datetime(expiry_time)}"
            )

        if confirmations:

            if isinstance(
                confirmations,
                (
                    list,
                    tuple,
                    set,
                ),
            ):

                confirmation_text = (
                    ", ".join(
                        str(item)
                        for item in confirmations
                        if item
                    )
                )

            else:

                confirmation_text = str(
                    confirmations
                )

            if confirmation_text:

                lines.append(
                    "✅ Подтверждения: "
                    f"{confirmation_text}"
                )

        if reasons:

            if isinstance(
                reasons,
                (
                    list,
                    tuple,
                    set,
                ),
            ):

                clean_reasons = [
                    str(item)
                    for item in reasons
                    if item
                ]

                clean_reasons = (
                    clean_reasons[
                        :MAX_REASONS
                    ]
                )

                if clean_reasons:

                    lines.append("")
                    lines.append(
                        "📌 Причины:"
                    )

                    for reason in clean_reasons:

                        lines.append(
                            f"• {reason}"
                        )

            elif isinstance(
                reasons,
                str,
            ):

                if reasons.strip():

                    lines.append("")
                    lines.append(
                        f"📌 {reasons.strip()}"
                    )

        return "\n".join(
            lines
        )

    # ========================================================
    # DATETIME
    # ========================================================

    @staticmethod
    def _format_datetime(
        value: Any,
    ) -> str:

        if value is None:
            return ""

        if isinstance(
            value,
            datetime,
        ):

            dt = value

            if dt.tzinfo is None:

                dt = dt.replace(
                    tzinfo=MOSCOW_TZ
                )

            dt = dt.astimezone(
                MOSCOW_TZ
            )

            return dt.strftime(
                "%H:%M:%S"
            )

        return str(
            value
        )

    # ========================================================
    # SUMMARY
    # ========================================================

    @staticmethod
    def _print_scan_summary(
        diagnostics: list[
            dict[str, Any]
        ],
    ) -> None:

        if not diagnostics:

            print(
                "[SCHEDULER] "
                "No diagnostics"
            )

            return

        print("")
        print(
            "[SCHEDULER] "
            "========== SCAN SUMMARY =========="
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

            direction = item.get(
                "direction",
                "",
            )

            reason = item.get(
                "reason",
                "",
            )

            candles = item.get(
                "candles",
                0,
            )

            print(
                f"[SCHEDULER] "
                f"{pair} | "
                f"{status} | "
                f"candles={candles} | "
                f"direction={direction or '-'} | "
                f"Q={quality:.1f} | "
                f"P={probability:.1f}% | "
                f"{reason}"
            )

        print(
            "[SCHEDULER] "
            "=================================="
        )
        print("")

    # ========================================================
    # RUN
    # ========================================================

    async def run(
        self,
    ) -> None:

        if self.running:

            print(
                "[SCHEDULER] "
                "run() already active"
            )

            return

        self.running = True

        print(
            "[SCHEDULER] "
            "Automatic scheduler started"
        )

        try:

            while self.running:

                now = _now()

                next_run = (
                    _next_analysis_time(
                        now
                    )
                )

                wait_seconds = (
                    next_run - now
                ).total_seconds()

                if wait_seconds < 0:
                    wait_seconds = 0

                print(
                    "[SCHEDULER] "
                    f"Следующий анализ: "
                    f"{next_run.strftime('%H:%M:%S')} "
                    f"МСК | "
                    f"через {wait_seconds:.1f}s"
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
                        "[SCHEDULER] "
                        f"scan error: "
                        f"{type(exc).__name__}: {exc}"
                    )

                    traceback.print_exc()

                    await asyncio.sleep(
                        ERROR_RETRY_DELAY
                    )

        except asyncio.CancelledError:

            print(
                "[SCHEDULER] "
                "Cancelled"
            )

            self.running = False

            raise

        except Exception as exc:

            print(
                "[SCHEDULER] "
                f"FATAL ERROR: "
                f"{type(exc).__name__}: {exc}"
            )

            traceback.print_exc()

        finally:

            self.running = False

            print(
                "[SCHEDULER] "
                "Stopped"
            )

    # ========================================================
    # STOP
    # ========================================================

    def stop(
        self,
    ) -> None:

        self.running = False

        print(
            "[SCHEDULER] "
            "Stop requested"
        )
