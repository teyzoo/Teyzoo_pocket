from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

from config import MIN_QUALITY
from indicators import add_indicators


@dataclass
class Signal:
    pair: str
    direction: str
    quality: float
    entry_time: datetime
    expiry_time: datetime
    analysis_time: datetime
    confirmations: list[str]
    reasons: list[str]


class SignalEngine:
    def analyze(
        self,
        pair: str,
        candles: pd.DataFrame,
    ) -> Signal | None:

        if candles is None or len(candles) < 80:
            return None

        df = add_indicators(candles)

        # Последняя свеча может быть ещё формирующейся.
        # Анализируем только полностью закрытые свечи.
        if len(df) > 1:
            df = df.iloc[:-1].copy()

        df = df.dropna(
            subset=[
                "ema_9",
                "ema_21",
                "ema_50",
                "rsi",
                "macd",
                "macd_signal",
                "macd_hist",
                "bb_middle",
                "bb_upper",
                "bb_lower",
                "stoch_k",
                "stoch_d",
                "atr",
                "body_ratio",
                "support",
                "resistance",
            ]
        )

        if len(df) < 50:
            return None

        current = df.iloc[-1]
        previous = df.iloc[-2]

        close = float(current["close"])

        call_score = 0.0
        put_score = 0.0

        call_confirmations = []
        put_confirmations = []

        # -------------------------------------------------
        # 1. Основной тренд — 20 баллов
        # -------------------------------------------------

        bullish_trend = (
            current["ema_9"] >
            current["ema_21"] >
            current["ema_50"]
        )

        bearish_trend = (
            current["ema_9"] <
            current["ema_21"] <
            current["ema_50"]
        )

        if bullish_trend:
            call_score += 20
            call_confirmations.append(
                "EMA 9/21/50: бычье выравнивание"
            )

        if bearish_trend:
            put_score += 20
            put_confirmations.append(
                "EMA 9/21/50: медвежье выравнивание"
            )

        # -------------------------------------------------
        # 2. Momentum EMA — 10 баллов
        # -------------------------------------------------

        ema_rising = (
            current["ema_9_slope"] > 0
        )

        ema_falling = (
            current["ema_9_slope"] < 0
        )

        if (
            close > current["ema_9"]
            and ema_rising
        ):
            call_score += 10
            call_confirmations.append(
                "Цена выше EMA9 + восходящий momentum"
            )

        if (
            close < current["ema_9"]
            and ema_falling
        ):
            put_score += 10
            put_confirmations.append(
                "Цена ниже EMA9 + нисходящий momentum"
            )

        # -------------------------------------------------
        # 3. MACD — 15 баллов
        # -------------------------------------------------

        macd_bullish = (
            current["macd"] >
            current["macd_signal"]
            and current["macd_hist"] > 0
        )

        macd_bearish = (
            current["macd"] <
            current["macd_signal"]
            and current["macd_hist"] < 0
        )

        if macd_bullish:
            call_score += 15
            call_confirmations.append(
                "MACD подтверждает рост"
            )

        if macd_bearish:
            put_score += 15
            put_confirmations.append(
                "MACD подтверждает падение"
            )

        # -------------------------------------------------
        # 4. RSI — 10 баллов
        # -------------------------------------------------

        rsi = float(current["rsi"])

        if 52 <= rsi <= 68:
            call_score += 10
            call_confirmations.append(
                f"RSI в бычьей зоне: {rsi:.1f}"
            )

        elif 32 <= rsi <= 48:
            put_score += 10
            put_confirmations.append(
                f"RSI в медвежьей зоне: {rsi:.1f}"
            )

        # -------------------------------------------------
        # 5. Bollinger — 10 баллов
        # -------------------------------------------------

        bb_middle = float(current["bb_middle"])
        bb_upper = float(current["bb_upper"])
        bb_lower = float(current["bb_lower"])

        if (
            close > bb_middle
            and close < bb_upper
        ):
            call_score += 10
            call_confirmations.append(
                "Цена в верхней половине Bollinger"
            )

        if (
            close < bb_middle
            and close > bb_lower
        ):
            put_score += 10
            put_confirmations.append(
                "Цена в нижней половине Bollinger"
            )

        # -------------------------------------------------
        # 6. Stochastic — 10 баллов
        # -------------------------------------------------

        stoch_k = float(current["stoch_k"])
        stoch_d = float(current["stoch_d"])

        if (
            stoch_k > stoch_d
            and 35 <= stoch_k <= 80
        ):
            call_score += 10
            call_confirmations.append(
                f"Stochastic подтверждает CALL: {stoch_k:.1f}"
            )

        if (
            stoch_k < stoch_d
            and 20 <= stoch_k <= 65
        ):
            put_score += 10
            put_confirmations.append(
                f"Stochastic подтверждает PUT: {stoch_k:.1f}"
            )

        # -------------------------------------------------
        # 7. Свечное подтверждение — 10 баллов
        # -------------------------------------------------

        body_ratio = float(
            current["body_ratio"]
        )

        candle_bullish = (
            current["close"] >
            current["open"]
        )

        candle_bearish = (
            current["close"] <
            current["open"]
        )

        if (
            candle_bullish
            and body_ratio >= 0.55
        ):
            call_score += 10
            call_confirmations.append(
                f"Сильная бычья свеча: body {body_ratio:.0%}"
            )

        if (
            candle_bearish
            and body_ratio >= 0.55
        ):
            put_score += 10
            put_confirmations.append(
                f"Сильная медвежья свеча: body {body_ratio:.0%}"
            )

        # -------------------------------------------------
        # 8. Support / Resistance — 10 баллов
        # -------------------------------------------------

        support = float(current["support"])
        resistance = float(current["resistance"])

        total_range = resistance - support

        if total_range > 0:
            support_distance = (
                close - support
            ) / total_range

            resistance_distance = (
                resistance - close
            ) / total_range

            if support_distance <= 0.25:
                call_score += 10
                call_confirmations.append(
                    "Цена находится рядом с поддержкой"
                )

            if resistance_distance <= 0.25:
                put_score += 10
                put_confirmations.append(
                    "Цена находится рядом с сопротивлением"
                )

        # -------------------------------------------------
        # 9. Волатильность / качество рынка — 5 баллов
        # -------------------------------------------------

        atr = float(current["atr"])

        atr_median = float(
            df["atr"].tail(30).median()
        )

        if atr_median > 0:
            atr_ratio = atr / atr_median

            if 0.60 <= atr_ratio <= 1.80:
                call_score += 5
                put_score += 5

        # -------------------------------------------------
        # Выбор направления
        # -------------------------------------------------

        if call_score > put_score:
            direction = "CALL"
            quality = call_score
            confirmations = call_confirmations
        elif put_score > call_score:
            direction = "PUT"
            quality = put_score
            confirmations = put_confirmations
        else:
            return None

        score_difference = abs(
            call_score - put_score
        )

        # Если направления почти равны — пропускаем.
        if score_difference < 20:
            return None

        # Минимальное качество.
        if quality < MIN_QUALITY:
            return None

        # -------------------------------------------------
        # Время входа
        # -------------------------------------------------

        now = datetime.now(timezone.utc)

        minutes_to_add = (
            5 - (now.minute % 5)
        ) % 5

        if minutes_to_add == 0:
            minutes_to_add = 5

        entry_time = (
            now.replace(
                second=0,
                microsecond=0,
            )
            + timedelta(
                minutes=minutes_to_add
            )
        )

        expiry_time = (
            entry_time +
            timedelta(minutes=5)
        )

        reasons = [
            f"CALL score: {call_score:.0f}",
            f"PUT score: {put_score:.0f}",
            f"Разница: {score_difference:.0f}",
            f"RSI: {rsi:.1f}",
            f"ATR: {atr:.6f}",
        ]

        return Signal(
            pair=pair,
            direction=direction,
            quality=round(
                min(100, quality),
                1,
            ),
            entry_time=entry_time,
            expiry_time=expiry_time,
            analysis_time=now,
            confirmations=confirmations,
            reasons=reasons,
        )
