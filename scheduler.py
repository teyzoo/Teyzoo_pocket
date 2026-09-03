from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from config import MIN_PROBABILITY, MIN_QUALITY, TIMEZONE
from indicators import calculate_indicators


# =========================================================
# SIGNAL
# =========================================================

@dataclass
class Signal:
    pair: str
    direction: str
    quality: float
    probability: float
    entry_time: datetime
    expiry_time: datetime
    analysis_time: datetime
    confirmations: list[str]
    reasons: list[str]

    @property
    def expiry_minutes(self) -> int:
        """Длительность сделки в минутах."""
        seconds = (
            self.expiry_time - self.entry_time
        ).total_seconds()

        return max(
            1,
            int(round(seconds / 60)),
        )


# =========================================================
# SIGNAL ENGINE
# =========================================================

class SignalEngine:
    """
    Основной движок анализа торговых сигналов.

    Вход:
        OHLCV свечи.

    Выход:
        Signal либо None.

    Поддерживает:
        expiry_minutes = 1..20

    Старый вызов:

        engine.analyze(pair, candles)

    продолжает работать и использует:
        expiry_minutes=5

    ВАЖНО:
        Расчётная probability — внутренняя оценка модели,
        а НЕ гарантированная вероятность выигрыша.
    """

    # ---------------------------------------------------------
    # REQUIRED INDICATORS
    # ---------------------------------------------------------

    REQUIRED_INDICATORS = [
        "close",
        "ema_fast",
        "ema_slow",
        "ema_50",
        "rsi",
        "macd",
        "macd_signal",
        "macd_hist",
        "bb_upper",
        "bb_lower",
        "stoch_k",
        "stoch_d",
        "atr",
        "body_ratio",
        "support",
        "resistance",
        "ema_fast_slope",
        "volatility",
    ]

    MIN_CANDLES = 80

    MIN_EXPIRY_MINUTES = 1
    MAX_EXPIRY_MINUTES = 20
    DEFAULT_EXPIRY_MINUTES = 5

    # =========================================================
    # CONSTRUCTOR
    # =========================================================

    def __init__(
        self,
        min_quality: Optional[float] = None,
        min_probability: Optional[float] = None,
    ) -> None:

        self.min_quality = (
            float(MIN_QUALITY)
            if min_quality is None
            else float(min_quality)
        )

        self.min_probability = (
            float(MIN_PROBABILITY)
            if min_probability is None
            else float(min_probability)
        )

        print(
            "[ENGINE] Initialized | "
            f"min_quality={self.min_quality:.1f} | "
            f"min_probability="
            f"{self.min_probability:.1f}% | "
            f"expiry="
            f"{self.MIN_EXPIRY_MINUTES}-"
            f"{self.MAX_EXPIRY_MINUTES}m"
        )

    # =========================================================
    # TIMEZONE
    # =========================================================

    @staticmethod
    def _timezone():
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
            self._timezone()
        )

    # =========================================================
    # EXPIRY NORMALIZATION
    # =========================================================

    def normalize_expiry_minutes(
        self,
        expiry_minutes: Optional[int] = None,
    ) -> int:
        """
        Нормализует длительность сделки.

        Допустимый диапазон:
            1..20 минут

        None:
            5 минут.
        """

        if expiry_minutes is None:
            return self.DEFAULT_EXPIRY_MINUTES

        try:
            value = int(
                expiry_minutes
            )

        except (
            TypeError,
            ValueError,
        ):
            print(
                "[ENGINE] Invalid expiry_minutes="
                f"{expiry_minutes!r}; "
                f"using default "
                f"{self.DEFAULT_EXPIRY_MINUTES}m"
            )

            return self.DEFAULT_EXPIRY_MINUTES

        value = max(
            self.MIN_EXPIRY_MINUTES,
            min(
                self.MAX_EXPIRY_MINUTES,
                value,
            ),
        )

        return value

    # =========================================================
    # ENTRY TIME
    # =========================================================

    def _next_5_minute(
        self,
        dt: Optional[datetime] = None,
    ) -> datetime:
        """
        Возвращает ближайшую следующую
        5-минутную отметку.

        Это сохраняет совместимость с текущей
        системой анализа 5m свечей.
        """

        if dt is None:
            dt = self._now()

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=self._timezone()
            )

        dt = dt.astimezone(
            self._timezone()
        )

        minute = (
            (dt.minute // 5) + 1
        ) * 5

        if minute >= 60:

            return (
                dt.replace(
                    minute=0,
                    second=0,
                    microsecond=0,
                )
                + timedelta(hours=1)
            )

        return dt.replace(
            minute=minute,
            second=0,
            microsecond=0,
        )

    # =========================================================
    # CUSTOM ENTRY
    # =========================================================

    def _get_entry_time(
        self,
        analysis_time: datetime,
    ) -> datetime:
        """
        Вход остаётся привязанным к следующей
        5-минутной свечной отметке.

        Это важно, поскольку текущий market layer
        использует 5m данные.
        """

        return self._next_5_minute(
            analysis_time
        )

    # =========================================================
    # EXPIRY TIME
    # =========================================================

    def _get_expiry_time(
        self,
        entry_time: datetime,
        expiry_minutes: int,
    ) -> datetime:

        expiry_minutes = (
            self.normalize_expiry_minutes(
                expiry_minutes
            )
        )

        return (
            entry_time
            + timedelta(
                minutes=expiry_minutes
            )
        )

    # =========================================================
    # HELPERS
    # =========================================================

    @staticmethod
    def _safe_float(
        value,
    ) -> Optional[float]:

        try:
            result = float(value)

            if not math.isfinite(
                result
            ):
                return None

            return result

        except (
            TypeError,
            ValueError,
        ):
            return None

    @staticmethod
    def _ensure_numeric(
        df: pd.DataFrame,
        columns: list[str],
    ) -> pd.DataFrame:

        result = df.copy()

        for column in columns:

            if column in result.columns:

                result[column] = pd.to_numeric(
                    result[column],
                    errors="coerce",
                )

        return result

    # =========================================================
    # INDICATOR NORMALIZATION
    # =========================================================

    def _normalize_indicators(
        self,
        df: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        Нормализует старые и новые названия индикаторов.

        Поддерживаются:

            ema_9
                -> ema_fast

            ema_21
                -> ema_slow

            macd_histogram
                -> macd_hist

            ema9_slope
                -> ema_fast_slope
        """

        df = df.copy()

        # -----------------------------------------------------
        # EMA
        # -----------------------------------------------------

        if (
            "ema_fast" not in df.columns
            and "ema_9" in df.columns
        ):
            df["ema_fast"] = df[
                "ema_9"
            ]

        if (
            "ema_slow" not in df.columns
            and "ema_21" in df.columns
        ):
            df["ema_slow"] = df[
                "ema_21"
            ]

        # -----------------------------------------------------
        # MACD HISTOGRAM
        # -----------------------------------------------------

        if (
            "macd_hist" not in df.columns
            and "macd_histogram" in df.columns
        ):
            df["macd_hist"] = df[
                "macd_histogram"
            ]

        # -----------------------------------------------------
        # EMA SLOPE
        # -----------------------------------------------------

        if (
            "ema_fast_slope" not in df.columns
            and "ema9_slope" in df.columns
        ):
            df["ema_fast_slope"] = df[
                "ema9_slope"
            ]

        # -----------------------------------------------------
        # EMA FALLBACK
        # -----------------------------------------------------

        if "close" in df.columns:

            close = pd.to_numeric(
                df["close"],
                errors="coerce",
            )

            if (
                "ema_fast"
                not in df.columns
            ):
                df["ema_fast"] = (
                    close.ewm(
                        span=9,
                        adjust=False,
                    ).mean()
                )

            if (
                "ema_slow"
                not in df.columns
            ):
                df["ema_slow"] = (
                    close.ewm(
                        span=21,
                        adjust=False,
                    ).mean()
                )

            if (
                "ema_50"
                not in df.columns
            ):
                df["ema_50"] = (
                    close.ewm(
                        span=50,
                        adjust=False,
                    ).mean()
                )

        # -----------------------------------------------------
        # MACD FALLBACK
        # -----------------------------------------------------

        if "close" in df.columns:

            close = pd.to_numeric(
                df["close"],
                errors="coerce",
            )

            if "macd" not in df.columns:

                ema_12 = (
                    close.ewm(
                        span=12,
                        adjust=False,
                    ).mean()
                )

                ema_26 = (
                    close.ewm(
                        span=26,
                        adjust=False,
                    ).mean()
                )

                df["macd"] = (
                    ema_12 - ema_26
                )

            if (
                "macd_signal"
                not in df.columns
            ):
                df["macd_signal"] = (
                    df["macd"].ewm(
                        span=9,
                        adjust=False,
                    ).mean()
                )

            if (
                "macd_hist"
                not in df.columns
            ):
                df["macd_hist"] = (
                    df["macd"]
                    - df["macd_signal"]
                )

        # -----------------------------------------------------
        # EMA SLOPE FALLBACK
        # -----------------------------------------------------

        if (
            "ema_fast" in df.columns
            and "ema_fast_slope"
            not in df.columns
        ):
            df["ema_fast_slope"] = (
                df["ema_fast"]
                - df["ema_fast"].shift(3)
            )

        return df

    # =========================================================
    # PROBABILITY
    # =========================================================

    def _calculate_probability(
        self,
        quality: float,
        score_difference: float,
        confirmations: int,
    ) -> float:
        """
        Внутренняя оценка вероятности.

        НЕ является гарантией результата.
        """

        probability = (
            50.0
            + quality * 0.35
        )

        if score_difference >= 35:
            probability += 5.0

        elif score_difference >= 30:
            probability += 4.0

        elif score_difference >= 25:
            probability += 3.0

        elif score_difference >= 20:
            probability += 2.0

        elif score_difference >= 15:
            probability += 1.0

        if confirmations >= 7:
            probability += 3.0

        elif confirmations >= 6:
            probability += 2.0

        elif confirmations >= 5:
            probability += 1.0

        return float(
            max(
                50.0,
                min(
                    95.0,
                    probability,
                ),
            )
        )

    # =========================================================
    # ANALYZE
    # =========================================================

    def analyze(
        self,
        pair: str,
        candles: pd.DataFrame,
        expiry_minutes: Optional[int] = None,
    ) -> Optional[Signal]:

        print("=" * 70)
        print(
            f"🔎 ANALYSIS: {pair}"
        )
        print("=" * 70)

        # -----------------------------------------------------
        # EXPIRY
        # -----------------------------------------------------

        expiry_minutes = (
            self.normalize_expiry_minutes(
                expiry_minutes
            )
        )

        print(
            f"⏱️ Expiry requested: "
            f"{expiry_minutes}m"
        )

        # -----------------------------------------------------
        # DATAFRAME
        # -----------------------------------------------------

        if candles is None:

            print(
                f"❌ REJECTED: {pair} | "
                "candles=None"
            )

            return None

        if not isinstance(
            candles,
            pd.DataFrame,
        ):

            print(
                f"❌ REJECTED: {pair} | "
                "invalid candles type: "
                f"{type(candles).__name__}"
            )

            return None

        print(
            f"📥 Candles received: "
            f"{len(candles)}"
        )

        if candles.empty:

            print(
                f"❌ REJECTED: {pair} | "
                "candles empty"
            )

            return None

        if len(candles) < self.MIN_CANDLES:

            print(
                f"❌ REJECTED: {pair} | "
                f"недостаточно свечей: "
                f"{len(candles)} < "
                f"{self.MIN_CANDLES}"
            )

            return None

        # -----------------------------------------------------
        # OHLC
        # -----------------------------------------------------

        required_ohlc = [
            "open",
            "high",
            "low",
            "close",
        ]

        missing_ohlc = [
            column
            for column in required_ohlc
            if column not in candles.columns
        ]

        if missing_ohlc:

            print(
                f"❌ REJECTED: {pair} | "
                "Отсутствуют OHLC: "
                f"{', '.join(missing_ohlc)}"
            )

            return None

        # -----------------------------------------------------
        # INDICATORS
        # -----------------------------------------------------

        try:

            df = calculate_indicators(
                candles.copy()
            )

        except Exception as exc:

            print(
                f"❌ INDICATOR ERROR: "
                f"{pair} | "
                f"{type(exc).__name__}: "
                f"{exc}"
            )

            return None

        if df is None:

            print(
                f"❌ REJECTED: {pair} | "
                "calculate_indicators "
                "returned None"
            )

            return None

        if not isinstance(
            df,
            pd.DataFrame,
        ):

            print(
                f"❌ REJECTED: {pair} | "
                "calculate_indicators "
                "returned "
                f"{type(df).__name__}"
            )

            return None

        print(
            f"📊 Indicators result: "
            f"{len(df)} rows"
        )

        print(
            "📋 Indicator columns: "
            f"{', '.join(map(str, df.columns))}"
        )

        # -----------------------------------------------------
        # NORMALIZE
        # -----------------------------------------------------

        df = self._normalize_indicators(
            df
        )

        print(
            "📋 Normalized columns: "
            f"{', '.join(map(str, df.columns))}"
        )

        # -----------------------------------------------------
        # REQUIRED INDICATORS
        # -----------------------------------------------------

        missing = [
            column
            for column in self.REQUIRED_INDICATORS
            if column not in df.columns
        ]

        if missing:

            print(
                f"❌ REJECTED: {pair} | "
                "Отсутствуют индикаторы: "
                f"{', '.join(missing)}"
            )

            return None

        # -----------------------------------------------------
        # NUMERIC
        # -----------------------------------------------------

        df = self._ensure_numeric(
            df,
            self.REQUIRED_INDICATORS,
        )

        # -----------------------------------------------------
        # REMOVE FORMING CANDLE
        # -----------------------------------------------------

        if len(df) > 1:

            df = df.iloc[:-1].copy()

        if len(df) < 70:

            print(
                f"❌ REJECTED: {pair} | "
                "после подготовки осталось "
                f"{len(df)} свечей"
            )

            return None

        # -----------------------------------------------------
        # LAST CLOSED CANDLE
        # -----------------------------------------------------

        row = df.iloc[-1]

        # -----------------------------------------------------
        # VALID VALUES
        # -----------------------------------------------------

        invalid_values: list[str] = []

        for column in self.REQUIRED_INDICATORS:

            value = self._safe_float(
                row.get(column)
            )

            if value is None:

                invalid_values.append(
                    column
                )

        if invalid_values:

            print(
                f"❌ REJECTED: {pair} | "
                "Некорректные/NaN значения: "
                f"{', '.join(invalid_values)}"
            )

            return None

        # =====================================================
        # VALUES
        # =====================================================

        close = self._safe_float(
            row["close"]
        )

        ema_fast = self._safe_float(
            row["ema_fast"]
        )

        ema_slow = self._safe_float(
            row["ema_slow"]
        )

        ema_50 = self._safe_float(
            row["ema_50"]
        )

        rsi = self._safe_float(
            row["rsi"]
        )

        macd = self._safe_float(
            row["macd"]
        )

        macd_signal = self._safe_float(
            row["macd_signal"]
        )

        macd_hist = self._safe_float(
            row["macd_hist"]
        )

        bb_upper = self._safe_float(
            row["bb_upper"]
        )

        bb_lower = self._safe_float(
            row["bb_lower"]
        )

        stoch_k = self._safe_float(
            row["stoch_k"]
        )

        stoch_d = self._safe_float(
            row["stoch_d"]
        )

        atr = self._safe_float(
            row["atr"]
        )

        body_ratio = self._safe_float(
            row["body_ratio"]
        )

        support = self._safe_float(
            row["support"]
        )

        resistance = self._safe_float(
            row["resistance"]
        )

        ema_fast_slope = self._safe_float(
            row["ema_fast_slope"]
        )

        volatility = self._safe_float(
            row["volatility"]
        )

        # =====================================================
        # SCORE
        # =====================================================

        call_score = 0.0
        put_score = 0.0

        call_reasons: list[str] = []
        put_reasons: list[str] = []

        # =====================================================
        # 1. EMA TREND — 20
        # =====================================================

        if (
            close > ema_fast
            and ema_fast > ema_slow
            and ema_slow > ema_50
        ):

            call_score += 20

            call_reasons.append(
                "EMA bullish trend"
            )

        elif (
            close < ema_fast
            and ema_fast < ema_slow
            and ema_slow < ema_50
        ):

            put_score += 20

            put_reasons.append(
                "EMA bearish trend"
            )

        elif close > ema_fast:

            call_score += 10

            call_reasons.append(
                "Цена выше EMA fast"
            )

        elif close < ema_fast:

            put_score += 10

            put_reasons.append(
                "Цена ниже EMA fast"
            )

        # =====================================================
        # 2. EMA SLOPE — 10
        # =====================================================

        if ema_fast_slope > 0:

            call_score += 10

            call_reasons.append(
                "EMA fast растёт"
            )

        elif ema_fast_slope < 0:

            put_score += 10

            put_reasons.append(
                "EMA fast снижается"
            )

        # =====================================================
        # 3. MACD — 15
        # =====================================================

        if (
            macd > macd_signal
            and macd_hist > 0
        ):

            call_score += 15

            call_reasons.append(
                "MACD bullish"
            )

        elif (
            macd < macd_signal
            and macd_hist < 0
        ):

            put_score += 15

            put_reasons.append(
                "MACD bearish"
            )

        elif macd_hist > 0:

            call_score += 7

            call_reasons.append(
                "MACD histogram positive"
            )

        elif macd_hist < 0:

            put_score += 7

            put_reasons.append(
                "MACD histogram negative"
            )

        # =====================================================
        # 4. RSI — 10
        # =====================================================

        if 50 <= rsi <= 68:

            call_score += 10

            call_reasons.append(
                "RSI подтверждает CALL"
            )

        elif 32 <= rsi < 50:

            put_score += 10

            put_reasons.append(
                "RSI подтверждает PUT"
            )

        elif rsi < 30:

            call_score += 6

            call_reasons.append(
                "RSI oversold"
            )

        elif rsi > 70:

            put_score += 6

            put_reasons.append(
                "RSI overbought"
            )

        # =====================================================
        # 5. BOLLINGER BANDS — 10
        # =====================================================

        bb_range = (
            bb_upper
            - bb_lower
        )

        if bb_range > 0:

            bb_position = (
                close
                - bb_lower
            ) / bb_range

            if bb_position <= 0.30:

                call_score += 10

                call_reasons.append(
                    "Цена у нижней "
                    "Bollinger Band"
                )

            elif bb_position >= 0.70:

                put_score += 10

                put_reasons.append(
                    "Цена у верхней "
                    "Bollinger Band"
                )

            elif bb_position < 0.50:

                call_score += 4

                call_reasons.append(
                    "Цена ниже середины "
                    "Bollinger"
                )

            else:

                put_score += 4

                put_reasons.append(
                    "Цена выше середины "
                    "Bollinger"
                )

        # =====================================================
        # 6. STOCHASTIC — 10
        # =====================================================

        if (
            stoch_k > stoch_d
            and stoch_k < 80
        ):

            call_score += 10

            call_reasons.append(
                "Stochastic bullish"
            )

        elif (
            stoch_k < stoch_d
            and stoch_k > 20
        ):

            put_score += 10

            put_reasons.append(
                "Stochastic bearish"
            )

        elif stoch_k < 20:

            call_score += 6

            call_reasons.append(
                "Stochastic oversold"
            )

        elif stoch_k > 80:

            put_score += 6

            put_reasons.append(
                "Stochastic overbought"
            )

        # =====================================================
        # 7. CANDLE — 10
        # =====================================================

        candle_open = self._safe_float(
            row.get("open")
        )

        candle_high = self._safe_float(
            row.get("high")
        )

        candle_low = self._safe_float(
            row.get("low")
        )

        if (
            candle_open is not None
            and close is not None
            and candle_high is not None
            and candle_low is not None
        ):

            candle_range = (
                candle_high
                - candle_low
            )

            if candle_range > 0:

                upper_wick = (
                    candle_high
                    - max(
                        candle_open,
                        close,
                    )
                )

                lower_wick = (
                    min(
                        candle_open,
                        close,
                    )
                    - candle_low
                )

                if (
                    close > candle_open
                    and body_ratio >= 0.55
                ):

                    call_score += 10

                    call_reasons.append(
                        "Сильная бычья свеча"
                    )

                elif (
                    close < candle_open
                    and body_ratio >= 0.55
                ):

                    put_score += 10

                    put_reasons.append(
                        "Сильная медвежья свеча"
                    )

                elif (
                    lower_wick
                    > candle_range * 0.45
                    and close > candle_open
                ):

                    call_score += 6

                    call_reasons.append(
                        "Бычий rejection"
                    )

                elif (
                    upper_wick
                    > candle_range * 0.45
                    and close < candle_open
                ):

                    put_score += 6

                    put_reasons.append(
                        "Медвежий rejection"
                    )

        # =====================================================
        # 8. SUPPORT / RESISTANCE — 10
        # =====================================================

        sr_range = (
            resistance
            - support
        )

        if sr_range > 0:

            support_distance = (
                close
                - support
            ) / sr_range

            resistance_distance = (
                resistance
                - close
            ) / sr_range

            if support_distance <= 0.20:

                call_score += 10

                call_reasons.append(
                    "Цена рядом с поддержкой"
                )

            elif resistance_distance <= 0.20:

                put_score += 10

                put_reasons.append(
                    "Цена рядом с сопротивлением"
                )

        # =====================================================
        # 9. VOLATILITY — 5
        # =====================================================

        if volatility is not None:

            if volatility > 0:

                call_score += 2.5
                put_score += 2.5

                call_reasons.append(
                    "Есть рыночная волатильность"
                )

                put_reasons.append(
                    "Есть рыночная волатильность"
                )

        # =====================================================
        # FINAL SCORE
        # =====================================================

        call_score = float(
            min(
                100.0,
                max(
                    0.0,
                    call_score,
                ),
            )
        )

        put_score = float(
            min(
                100.0,
                max(
                    0.0,
                    put_score,
                ),
            )
        )

        if call_score >= put_score:

            direction = "CALL"
            quality = call_score
            losing_score = put_score
            reasons = call_reasons

        else:

            direction = "PUT"
            quality = put_score
            losing_score = call_score
            reasons = put_reasons

        score_difference = (
            quality
            - losing_score
        )

        confirmations = len(
            reasons
        )

        print(
            f"📈 {pair} | "
            f"CALL={call_score:.1f} | "
            f"PUT={put_score:.1f} | "
            f"BEST={direction} | "
            f"QUALITY={quality:.1f} | "
            f"DIFF={score_difference:.1f}"
        )

        # =====================================================
        # SCORE DIFFERENCE FILTER
        # =====================================================

        if score_difference < 12:

            print(
                f"❌ REJECTED: {pair} | "
                "Разница сигналов слишком мала: "
                f"{score_difference:.1f} < 12"
            )

            return None

        # =====================================================
        # QUALITY FILTER
        # =====================================================

        if quality < self.min_quality:

            print(
                f"❌ REJECTED: {pair} | "
                f"Quality {quality:.1f} < "
                f"{self.min_quality:.1f}"
            )

            return None

        # =====================================================
        # PROBABILITY
        # =====================================================

        probability = (
            self._calculate_probability(
                quality=quality,
                score_difference=score_difference,
                confirmations=confirmations,
            )
        )

        print(
            f"🎯 {pair} | "
            f"probability="
            f"{probability:.1f}%"
        )

        # =====================================================
        # PROBABILITY FILTER
        # =====================================================

        if probability < self.min_probability:

            print(
                f"❌ REJECTED: {pair} | "
                f"Probability "
                f"{probability:.1f}% < "
                f"{self.min_probability:.1f}%"
            )

            return None

        # =====================================================
        # TIME
        # =====================================================

        analysis_time = self._now()

        entry_time = (
            self._get_entry_time(
                analysis_time
            )
        )

        expiry_time = (
            self._get_expiry_time(
                entry_time,
                expiry_minutes,
            )
        )

        # =====================================================
        # FINAL SIGNAL
        # =====================================================

        print("=" * 70)

        print(
            f"✅ SIGNAL FOUND: {pair}"
        )

        print(
            f"📊 Direction: "
            f"{direction}"
        )

        print(
            f"⭐ Quality: "
            f"{quality:.1f}"
        )

        print(
            f"🎯 Probability: "
            f"{probability:.1f}%"
        )

        print(
            f"⏱️ Duration: "
            f"{expiry_minutes}m"
        )

        print(
            f"⏰ Entry: "
            f"{entry_time.strftime('%H:%M:%S')}"
        )

        print(
            f"⏰ Expiry: "
            f"{expiry_time.strftime('%H:%M:%S')}"
        )

        print(
            f"🔍 Confirmations: "
            f"{confirmations}"
        )

        print("=" * 70)

        return Signal(
            pair=pair,
            direction=direction,
            quality=quality,
            probability=probability,
            entry_time=entry_time,
            expiry_time=expiry_time,
            analysis_time=analysis_time,
            confirmations=list(
                reasons
            ),
            reasons=list(
                reasons
            ),
        )

    # =========================================================
    # ANALYZE WITH ANY EXPIRY
    # =========================================================

    def analyze_with_expiry(
        self,
        pair: str,
        candles: pd.DataFrame,
        expiry_minutes: int,
    ) -> Optional[Signal]:
        """
        Явный метод для нового интерфейса.

        Пример:

            engine.analyze_with_expiry(
                "EUR/USD",
                candles,
                10,
            )

        Аналогичен:

            engine.analyze(
                "EUR/USD",
                candles,
                expiry_minutes=10,
            )
        """

        return self.analyze(
            pair=pair,
            candles=candles,
            expiry_minutes=expiry_minutes,
        )

    # =========================================================
    # ANALYZE ALL EXPIRIES
    # =========================================================

    def analyze_all_expiries(
        self,
        pair: str,
        candles: pd.DataFrame,
    ) -> dict[int, Optional[Signal]]:
        """
        Анализирует текущий рынок для всех длительностей
        от 1 до 20 минут.

        ВАЖНО:

        Индикаторы рассчитываются один раз.
        Для каждой длительности создаётся независимый
        Signal с соответствующим expiry_time.

        Если направление рынка одинаковое, это не означает,
        что все 20 сигналов гарантированно одинаково успешны.
        """

        result: dict[
            int,
            Optional[Signal],
        ] = {}

        for minutes in range(
            self.MIN_EXPIRY_MINUTES,
            self.MAX_EXPIRY_MINUTES + 1,
        ):

            signal = self.analyze(
                pair=pair,
                candles=candles,
                expiry_minutes=minutes,
            )

            result[minutes] = signal

        return result

    # =========================================================
    # BEST EXPIRY
    # =========================================================

    @staticmethod
    def _expiry_score(
        signal: Optional[Signal],
    ) -> float:

        if signal is None:
            return -1.0

        return (
            float(signal.probability)
            * 0.65
            +
            float(signal.quality)
            * 0.35
        )

    def choose_best_expiry(
        self,
        pair: str,
        candles: pd.DataFrame,
        preferred_minutes: Optional[int] = None,
    ) -> Optional[Signal]:
        """
        Выбирает лучший вариант длительности.

        Если preferred_minutes указан —
        сначала проверяется именно он.

        Если preferred_minutes=None —
        проверяются 1..20 минут.

        Если ни один вариант не проходит фильтры —
        возвращается None.
        """

        if preferred_minutes is not None:

            preferred_minutes = (
                self.normalize_expiry_minutes(
                    preferred_minutes
                )
            )

            return self.analyze(
                pair=pair,
                candles=candles,
                expiry_minutes=preferred_minutes,
            )

        best_signal: Optional[
            Signal
        ] = None

        best_score = -1.0

        for minutes in range(
            self.MIN_EXPIRY_MINUTES,
            self.MAX_EXPIRY_MINUTES + 1,
        ):

            signal = self.analyze(
                pair=pair,
                candles=candles,
                expiry_minutes=minutes,
            )

            score = (
                self._expiry_score(
                    signal
                )
            )

            if (
                signal is not None
                and score > best_score
            ):

                best_signal = signal
                best_score = score

        return best_signal


# =========================================================
# BACKWARD COMPATIBILITY
# =========================================================

__all__ = [
    "Signal",
    "SignalEngine",
]
