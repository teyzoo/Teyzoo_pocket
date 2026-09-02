from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

from config import MIN_PROBABILITY, MIN_QUALITY
from indicators import calculate_indicators


@dataclass
class Signal:
    pair: str
    direction: str

    quality: int
    probability: float

    entry_time: datetime
    expiry_time: datetime
    analysis_time: datetime

    confirmations: list[str]
    reasons: list[str]


class SignalEngine:

    def __init__(
        self,
        min_quality: Optional[int] = None,
        min_probability: Optional[float] = None,
    ):
        self.min_quality = (
            MIN_QUALITY
            if min_quality is None
            else min_quality
        )

        self.min_probability = (
            MIN_PROBABILITY
            if min_probability is None
            else min_probability
        )

    @staticmethod
    def _next_5_minute(dt: datetime) -> datetime:
        dt = dt.astimezone(timezone.utc)

        minute = dt.minute
        next_minute = ((minute // 5) + 1) * 5

        if next_minute >= 60:
            result = dt.replace(
                minute=0,
                second=0,
                microsecond=0,
            ) + timedelta(hours=1)
        else:
            result = dt.replace(
                minute=next_minute,
                second=0,
                microsecond=0,
            )

        return result

    @staticmethod
    def _clamp(value: float, minimum: float, maximum: float) -> float:
        return max(minimum, min(maximum, value))

    def _calculate_probability(
        self,
        quality: int,
        score_difference: float,
        confirmations_count: int,
    ) -> float:
        """
        Расчётный показатель вероятности.

        ВАЖНО:
        Это пока НЕ гарантированная вероятность выигрыша.
        До накопления собственной статистики это calibrated-style
        оценка на основе качества сигнала и количества подтверждений.
        """

        # Базовая оценка от качества.
        if quality >= 98:
            probability = 91.0
        elif quality >= 95:
            probability = 87.0
        elif quality >= 92:
            probability = 84.0
        elif quality >= 90:
            probability = 82.0
        elif quality >= 88:
            probability = 79.0
        elif quality >= 85:
            probability = 76.0
        elif quality >= 82:
            probability = 73.0
        elif quality >= 80:
            probability = 70.0
        else:
            probability = 65.0

        # Сильное преимущество направления повышает оценку.
        if score_difference >= 35:
            probability += 3.0
        elif score_difference >= 30:
            probability += 2.0
        elif score_difference >= 25:
            probability += 1.0

        # Дополнительные подтверждения.
        if confirmations_count >= 6:
            probability += 2.0
        elif confirmations_count >= 5:
            probability += 1.0

        return round(
            self._clamp(probability, 50.0, 95.0),
            1,
        )

    def analyze(
        self,
        pair: str,
        candles: pd.DataFrame,
    ) -> Optional[Signal]:

        if candles is None or len(candles) < 80:
            return None

        try:
            df = calculate_indicators(candles.copy())
        except Exception:
            return None

        if df is None or len(df) < 80:
            return None

        # Последняя свеча может ещё формироваться.
        df = df.iloc[:-1].copy()

        if len(df) < 70:
            return None

        row = df.iloc[-1]

        required_columns = [
            "ema_9",
            "ema_21",
            "ema_50",
            "rsi",
            "macd",
            "macd_signal",
            "bb_upper",
            "bb_lower",
            "stoch_k",
            "stoch_d",
            "atr",
            "body_ratio",
            "support",
            "resistance",
            "ema9_slope",
        ]

        for column in required_columns:
            if column not in df.columns:
                return None

            if pd.isna(row[column]):
                return None

        close = float(row["close"])

        ema9 = float(row["ema_9"])
        ema21 = float(row["ema_21"])
        ema50 = float(row["ema_50"])

        rsi = float(row["rsi"])

        macd = float(row["macd"])
        macd_signal = float(row["macd_signal"])

        bb_upper = float(row["bb_upper"])
        bb_lower = float(row["bb_lower"])

        stoch_k = float(row["stoch_k"])
        stoch_d = float(row["stoch_d"])

        atr = float(row["atr"])

        body_ratio = float(row["body_ratio"])

        support = float(row["support"])
        resistance = float(row["resistance"])

        ema9_slope = float(row["ema9_slope"])

        candle_open = float(row["open"])
        candle_high = float(row["high"])
        candle_low = float(row["low"])

        call_score = 0.0
        put_score = 0.0

        call_confirmations: list[str] = []
        put_confirmations: list[str] = []

        call_reasons: list[str] = []
        put_reasons: list[str] = []

        # ---------------------------------------------------------
        # TREND EMA — максимум 20
        # ---------------------------------------------------------

        if ema9 > ema21 > ema50:
            call_score += 20
            call_confirmations.append("EMA тренд вверх")
            call_reasons.append("EMA9 > EMA21 > EMA50")

        elif ema9 < ema21 < ema50:
            put_score += 20
            put_confirmations.append("EMA тренд вниз")
            put_reasons.append("EMA9 < EMA21 < EMA50")

        else:
            if ema9 > ema21:
                call_score += 10
                call_confirmations.append("EMA9 выше EMA21")

            if ema9 < ema21:
                put_score += 10
                put_confirmations.append("EMA9 ниже EMA21")

        # ---------------------------------------------------------
        # EMA MOMENTUM — максимум 10
        # ---------------------------------------------------------

        if ema9_slope > 0:
            call_score += 10
            call_confirmations.append("Импульс вверх")

        elif ema9_slope < 0:
            put_score += 10
            put_confirmations.append("Импульс вниз")

        # ---------------------------------------------------------
        # MACD — максимум 15
        # ---------------------------------------------------------

        if macd > macd_signal:
            call_score += 15
            call_confirmations.append("MACD подтверждает CALL")

        elif macd < macd_signal:
            put_score += 15
            put_confirmations.append("MACD подтверждает PUT")

        # ---------------------------------------------------------
        # RSI — максимум 10
        # ---------------------------------------------------------

        if 52 <= rsi <= 68:
            call_score += 10
            call_confirmations.append("RSI подтверждает рост")

        elif 32 <= rsi <= 48:
            put_score += 10
            put_confirmations.append("RSI подтверждает падение")

        # ---------------------------------------------------------
        # BOLLINGER — максимум 10
        # ---------------------------------------------------------

        if close > (bb_upper + bb_lower) / 2:
            call_score += 10
            call_confirmations.append("Цена выше средней BB")

        elif close < (bb_upper + bb_lower) / 2:
            put_score += 10
            put_confirmations.append("Цена ниже средней BB")

        # ---------------------------------------------------------
        # STOCHASTIC — максимум 10
        # ---------------------------------------------------------

        if stoch_k > stoch_d and stoch_k < 80:
            call_score += 10
            call_confirmations.append("Stochastic вверх")

        elif stoch_k < stoch_d and stoch_k > 20:
            put_score += 10
            put_confirmations.append("Stochastic вниз")

        # ---------------------------------------------------------
        # СВЕЧА — максимум 10
        # ---------------------------------------------------------

        candle_range = candle_high - candle_low

        if candle_range > 0:
            bullish = candle_open < close
            bearish = candle_open > close

            if bullish and body_ratio >= 0.55:
                call_score += 10
                call_confirmations.append("Сильная бычья свеча")

            elif bearish and body_ratio >= 0.55:
                put_score += 10
                put_confirmations.append("Сильная медвежья свеча")

        # ---------------------------------------------------------
        # SUPPORT / RESISTANCE — максимум 10
        # ---------------------------------------------------------

        support_distance = abs(close - support)
        resistance_distance = abs(resistance - close)

        if support_distance < resistance_distance:
            call_score += 10
            call_confirmations.append("Поддержка ближе")

        elif resistance_distance < support_distance:
            put_score += 10
            put_confirmations.append("Сопротивление ближе")

        # ---------------------------------------------------------
        # VOLATILITY — максимум 5
        # ---------------------------------------------------------

        if close > 0 and atr / close >= 0.0002:
            if call_score >= put_score:
                call_score += 5
                call_confirmations.append("Нормальная волатильность")
            else:
                put_score += 5
                put_confirmations.append("Нормальная волатильность")

        # ---------------------------------------------------------
        # РЕЗУЛЬТАТ
        # ---------------------------------------------------------

        if call_score > put_score:
            direction = "CALL"
            quality = int(round(call_score))
            score_difference = call_score - put_score

            confirmations = call_confirmations
            reasons = call_reasons

        else:
            direction = "PUT"
            quality = int(round(put_score))
            score_difference = put_score - call_score

            confirmations = put_confirmations
            reasons = put_reasons

        quality = max(0, min(100, quality))

        # Направление должно иметь заметное преимущество.
        if score_difference < 20:
            return None

        # Технический минимальный score.
        if quality < self.min_quality:
            return None

        probability = self._calculate_probability(
            quality=quality,
            score_difference=score_difference,
            confirmations_count=len(confirmations),
        )

        # Главный новый фильтр.
        if probability < self.min_probability:
            return None

        now = datetime.now(timezone.utc)

        entry_time = self._next_5_minute(now)
        expiry_time = entry_time + timedelta(minutes=5)

        # Добавляем полезную информацию.
        reasons = reasons[:8]

        return Signal(
            pair=pair,
            direction=direction,
            quality=quality,
            probability=probability,
            entry_time=entry_time,
            expiry_time=expiry_time,
            analysis_time=now,
            confirmations=confirmations[:8],
            reasons=reasons,
        )
