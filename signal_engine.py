from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional

import pandas as pd

from config import (
    ENTRY_STEP_MINUTES,
    EXPIRY_MINUTES,
    MIN_CANDLES,
    MIN_QUALITY,
    TIMEZONE,
)
from indicators import calculate_indicators


@dataclass
class Signal:
    pair: str
    direction: str
    entry_time: datetime
    expiry_time: datetime
    quality: int
    confirmations: Dict[str, str]
    reasons: list[str]


class SignalEngine:

    def analyze(
        self,
        pair: str,
        dataframe: pd.DataFrame,
    ) -> Optional[Signal]:

        if dataframe is None:
            return None

        if len(dataframe) < MIN_CANDLES:
            return None

        try:
            df = calculate_indicators(
                dataframe
            )
        except Exception:
            return None

        if len(df) < 60:
            return None

        current = df.iloc[-1]
        previous = df.iloc[-2]

        required_columns = [
            "ema_fast",
            "ema_slow",
            "ema_trend",
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
        ]

        for column in required_columns:
            value = current[column]

            if pd.isna(value):
                return None


        # ====================================================
        # SCORES
        # ====================================================

        call_score = 0
        put_score = 0

        confirmations = {}

        reasons = []


        # ====================================================
        # TREND / EMA
        # ====================================================

        if (
            current["ema_fast"]
            > current["ema_slow"]
            > current["ema_trend"]
        ):
            call_score += 20
            confirmations["Тренд"] = "CALL"
            reasons.append(
                "EMA показывают восходящий тренд."
            )

        elif (
            current["ema_fast"]
            < current["ema_slow"]
            < current["ema_trend"]
        ):
            put_score += 20
            confirmations["Тренд"] = "PUT"
            reasons.append(
                "EMA показывают нисходящий тренд."
            )


        # ====================================================
        # RSI
        # ====================================================

        rsi = float(
            current["rsi"]
        )

        if 50 <= rsi <= 68:
            call_score += 10
            confirmations["RSI"] = "CALL"

        elif 32 <= rsi < 50:
            put_score += 10
            confirmations["RSI"] = "PUT"


        # ====================================================
        # MACD
        # ====================================================

        macd = float(
            current["macd"]
        )

        macd_signal = float(
            current["macd_signal"]
        )

        previous_macd = float(
            previous["macd"]
        )

        previous_macd_signal = float(
            previous["macd_signal"]
        )

        if (
            macd > macd_signal
            and previous_macd
            <= previous_macd_signal
        ):
            call_score += 15
            confirmations["MACD"] = "CALL"
            reasons.append(
                "MACD дал бычье пересечение."
            )

        elif (
            macd < macd_signal
            and previous_macd
            >= previous_macd_signal
        ):
            put_score += 15
            confirmations["MACD"] = "PUT"
            reasons.append(
                "MACD дал медвежье пересечение."
            )

        elif macd > macd_signal:
            call_score += 8
            confirmations["MACD"] = "CALL"

        elif macd < macd_signal:
            put_score += 8
            confirmations["MACD"] = "PUT"


        # ====================================================
        # BOLLINGER
        # ====================================================

        close = float(
            current["close"]
        )

        bb_upper = float(
            current["bb_upper"]
        )

        bb_lower = float(
            current["bb_lower"]
        )

        if close < bb_lower:
            call_score += 10
            confirmations["Bollinger"] = "CALL"

        elif close > bb_upper:
            put_score += 10
            confirmations["Bollinger"] = "PUT"


        # ====================================================
        # STOCHASTIC
        # ====================================================

        stoch_k = float(
            current["stoch_k"]
        )

        stoch_d = float(
            current["stoch_d"]
        )

        if (
            stoch_k > stoch_d
            and stoch_k < 80
        ):
            call_score += 10
            confirmations["Stochastic"] = "CALL"

        elif (
            stoch_k < stoch_d
            and stoch_k > 20
        ):
            put_score += 10
            confirmations["Stochastic"] = "PUT"


        # ====================================================
        # CANDLE
        # ====================================================

        candle_body = float(
            current["body"]
        )

        body_ratio = float(
            current["body_ratio"]
        )

        if body_ratio >= 0.55:

            if candle_body > 0:
                call_score += 10
                confirmations["Свеча"] = "CALL"

            elif candle_body < 0:
                put_score += 10
                confirmations["Свеча"] = "PUT"


        # ====================================================
        # SUPPORT / RESISTANCE
        # ====================================================

        support = float(
            current["support"]
        )

        resistance = float(
            current["resistance"]
        )

        range_size = (
            resistance - support
        )

        if range_size > 0:

            distance_from_support = (
                close - support
            )

            distance_from_resistance = (
                resistance - close
            )

            support_zone = (
                distance_from_support
                / range_size
                < 0.20
            )

            resistance_zone = (
                distance_from_resistance
                / range_size
                < 0.20
            )

            if support_zone:
                call_score += 10
                confirmations[
                    "Уровень"
                ] = "CALL"

            elif resistance_zone:
                put_score += 10
                confirmations[
                    "Уровень"
                ] = "PUT"


        # ====================================================
        # VOLATILITY FILTER
        # ====================================================

        atr = float(
            current["atr"]
        )

        if atr <= 0:
            return None

        candle_range = float(
            current["range"]
        )

        # Отбрасываем аномально огромную свечу.
        if candle_range > atr * 3:
            return None


        # ====================================================
        # CHOOSE DIRECTION
        # ====================================================

        if call_score == put_score:
            return None

        if call_score > put_score:
            direction = "CALL"
            quality = call_score

        else:
            direction = "PUT"
            quality = put_score


        # ====================================================
        # QUALITY FILTER
        # ====================================================

        if quality < MIN_QUALITY:
            return None


        # ====================================================
        # TIME
        # ====================================================

        now = datetime.now(
            TIMEZONE
        )

        entry_time = self._next_entry_time(
            now
        )

        expiry_time = (
            entry_time
            + timedelta(
                minutes=EXPIRY_MINUTES
            )
        )


        return Signal(
            pair=pair,
            direction=direction,
            entry_time=entry_time,
            expiry_time=expiry_time,
            quality=int(quality),
            confirmations=confirmations,
            reasons=reasons,
        )


    @staticmethod
    def _next_entry_time(
        now: datetime,
    ) -> datetime:

        minute = now.minute

        step = ENTRY_STEP_MINUTES

        next_minute = (
            (
                minute // step
            ) + 1
        ) * step

        result = now.replace(
            second=0,
            microsecond=0,
        )

        if next_minute >= 60:
            result = result.replace(
                minute=0
            )

            result += timedelta(
                hours=1
            )

        else:
            result = result.replace(
                minute=next_minute
            )

        return result
