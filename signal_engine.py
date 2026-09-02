from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from config import MIN_PROBABILITY, MIN_QUALITY, TIMEZONE
from indicators import calculate_indicators


# ============================================================
# TIMEZONE
# ============================================================

try:
    MOSCOW_TZ = ZoneInfo(TIMEZONE)
except Exception:
    MOSCOW_TZ = ZoneInfo("Europe/Moscow")


# ============================================================
# SIGNAL DATACLASS
# ============================================================

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


# ============================================================
# SIGNAL ENGINE
# ============================================================

class SignalEngine:
    """
    Анализатор торговых сигналов.

    ВАЖНО:
    probability здесь является РАСЧЁТНОЙ ОЦЕНКОЙ
    силы сигнала, а не гарантированной вероятностью
    выигрыша.

    Реальный WINRATE необходимо калибровать
    по фактической истории WIN/LOSS.
    """

    # --------------------------------------------------------
    # INIT
    # --------------------------------------------------------

    def __init__(
        self,
        min_quality: Optional[float] = None,
        min_probability: Optional[float] = None,
    ):
        self.min_quality = (
            float(min_quality)
            if min_quality is not None
            else float(MIN_QUALITY)
        )

        self.min_probability = (
            float(min_probability)
            if min_probability is not None
            else float(MIN_PROBABILITY)
        )

    # --------------------------------------------------------
    # TIME
    # --------------------------------------------------------

    @staticmethod
    def _now_moscow() -> datetime:
        return datetime.now(MOSCOW_TZ)

    @staticmethod
    def _next_5_minute(
        current_time: Optional[datetime] = None,
    ) -> datetime:
        """
        Возвращает ближайшую следующую пятиминутную отметку
        именно в часовом поясе Москвы.

        Например:

        14:01 -> 14:05
        14:04 -> 14:05
        14:05 -> 14:10
        14:59 -> 15:00
        """

        if current_time is None:
            current_time = SignalEngine._now_moscow()

        if current_time.tzinfo is None:
            current_time = current_time.replace(
                tzinfo=timezone.utc
            )

        current_time = current_time.astimezone(MOSCOW_TZ)

        next_minute = (
            (current_time.minute // 5) + 1
        ) * 5

        if next_minute >= 60:
            return (
                current_time + timedelta(hours=1)
            ).replace(
                minute=0,
                second=0,
                microsecond=0,
            )

        return current_time.replace(
            minute=next_minute,
            second=0,
            microsecond=0,
        )

    # --------------------------------------------------------
    # HELPERS
    # --------------------------------------------------------

    @staticmethod
    def _clamp(
        value: float,
        minimum: float,
        maximum: float,
    ) -> float:
        return max(
            minimum,
            min(maximum, value),
        )

    @staticmethod
    def _safe_float(value) -> Optional[float]:
        try:
            result = float(value)

            if pd.isna(result):
                return None

            return result

        except (TypeError, ValueError):
            return None

    @staticmethod
    def _last_row(df: pd.DataFrame):
        if df is None or df.empty:
            return None

        return df.iloc[-1]

    @staticmethod
    def _append_unique(
        items: list[str],
        value: str,
    ) -> None:
        if value and value not in items:
            items.append(value)

    # --------------------------------------------------------
    # PROBABILITY
    # --------------------------------------------------------

    def _calculate_probability(
        self,
        quality: float,
        score_difference: float,
        confirmations_count: int,
    ) -> float:
        """
        Расчётная confidence-оценка.

        Это НЕ реальная статистическая вероятность.

        Чем выше:
        - качество;
        - разница CALL/PUT;
        - количество подтверждений;

        тем выше confidence.
        """

        # Базовая оценка от качества.
        #
        # 85 quality -> около 75%
        # 90 quality -> около 78%
        # 95 quality -> около 82%
        # 100 quality -> около 85%
        probability = 50.0 + (
            quality * 0.35
        )

        # Дополнительная уверенность,
        # когда одно направление сильно доминирует.
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

        # Количество независимых подтверждений.
        if confirmations_count >= 7:
            probability += 3.0
        elif confirmations_count >= 6:
            probability += 2.0
        elif confirmations_count >= 5:
            probability += 1.0

        return round(
            self._clamp(
                probability,
                50.0,
                95.0,
            ),
            1,
        )

    # --------------------------------------------------------
    # INDICATOR VALIDATION
    # --------------------------------------------------------

    @staticmethod
    def _required_columns() -> list[str]:
        return [
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

    # --------------------------------------------------------
    # MAIN ANALYSIS
    # --------------------------------------------------------

    def analyze(
        self,
        pair: str,
        candles: pd.DataFrame,
    ) -> Optional[Signal]:

        pair = str(pair).strip()

        analysis_time = self._now_moscow()

        print("")
        print("=" * 70)
        print(f"🔎 ANALYSIS: {pair}")
        print("=" * 70)

        # ----------------------------------------------------
        # CANDLES
        # ----------------------------------------------------

        if candles is None:
            print(
                f"❌ {pair}: candles=None"
            )
            print(
                f"❌ REJECTED: {pair} | "
                f"Причина: нет данных"
            )
            return None

        if not isinstance(
            candles,
            pd.DataFrame,
        ):
            print(
                f"❌ {pair}: неправильный тип данных: "
                f"{type(candles).__name__}"
            )
            print(
                f"❌ REJECTED: {pair} | "
                f"Причина: неправильный DataFrame"
            )
            return None

        if candles.empty:
            print(
                f"❌ {pair}: DataFrame пустой"
            )
            print(
                f"❌ REJECTED: {pair} | "
                f"Причина: нет свечей"
            )
            return None

        print(
            f"📥 Candles received: {len(candles)}"
        )

        # Нам нужно достаточно истории
        # для расчёта индикаторов.
        if len(candles) < 80:
            print(
                f"❌ REJECTED: {pair} | "
                f"Недостаточно свечей: "
                f"{len(candles)}/80"
            )
            return None

        # ----------------------------------------------------
        # INDICATORS
        # ----------------------------------------------------

        try:
            df = calculate_indicators(
                candles.copy()
            )

        except Exception as exc:
            print(
                f"❌ {pair}: ошибка "
                f"calculate_indicators: "
                f"{type(exc).__name__}: {exc}"
            )

            print(
                f"❌ REJECTED: {pair} | "
                f"Ошибка расчёта индикаторов"
            )

            return None

        if df is None or df.empty:
            print(
                f"❌ REJECTED: {pair} | "
                f"Индикаторы вернули пустой DataFrame"
            )
            return None

        # Если последняя свеча формирующаяся,
        # не используем её.
        if len(df) > 1:
            df = df.iloc[:-1].copy()

        if len(df) < 70:
            print(
                f"❌ REJECTED: {pair} | "
                f"Недостаточно готовых свечей "
                f"после фильтрации: "
                f"{len(df)}/70"
            )
            return None

        # ----------------------------------------------------
        # REQUIRED COLUMNS
        # ----------------------------------------------------

        required_columns = (
            self._required_columns()
        )

        missing = [
            column
            for column in required_columns
            if column not in df.columns
        ]

        if missing:
            print(
                f"❌ REJECTED: {pair} | "
                f"Отсутствуют индикаторы: "
                f"{', '.join(missing)}"
            )
            return None

        # ----------------------------------------------------
        # LAST ROW
        # ----------------------------------------------------

        row = self._last_row(df)

        if row is None:
            print(
                f"❌ REJECTED: {pair} | "
                f"Последняя свеча отсутствует"
            )
            return None

        # ----------------------------------------------------
        # VALUES
        # ----------------------------------------------------

        close = self._safe_float(
            row.get("close")
        )

        ema_fast = self._safe_float(
            row.get("ema_fast")
        )

        ema_slow = self._safe_float(
            row.get("ema_slow")
        )

        ema_50 = self._safe_float(
            row.get("ema_50")
        )

        rsi = self._safe_float(
            row.get("rsi")
        )

        macd = self._safe_float(
            row.get("macd")
        )

        macd_signal = self._safe_float(
            row.get("macd_signal")
        )

        macd_hist = self._safe_float(
            row.get("macd_hist")
        )

        bb_upper = self._safe_float(
            row.get("bb_upper")
        )

        bb_lower = self._safe_float(
            row.get("bb_lower")
        )

        stoch_k = self._safe_float(
            row.get("stoch_k")
        )

        stoch_d = self._safe_float(
            row.get("stoch_d")
        )

        atr = self._safe_float(
            row.get("atr")
        )

        body_ratio = self._safe_float(
            row.get("body_ratio")
        )

        support = self._safe_float(
            row.get("support")
        )

        resistance = self._safe_float(
            row.get("resistance")
        )

        ema_fast_slope = self._safe_float(
            row.get("ema_fast_slope")
        )

        volatility = self._safe_float(
            row.get("volatility")
        )

        values = {
            "close": close,
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "ema_50": ema_50,
            "rsi": rsi,
            "macd": macd,
            "macd_signal": macd_signal,
            "macd_hist": macd_hist,
            "bb_upper": bb_upper,
            "bb_lower": bb_lower,
            "stoch_k": stoch_k,
            "stoch_d": stoch_d,
            "atr": atr,
            "body_ratio": body_ratio,
            "support": support,
            "resistance": resistance,
            "ema_fast_slope": ema_fast_slope,
            "volatility": volatility,
        }

        invalid_values = [
            name
            for name, value in values.items()
            if value is None
        ]

        if invalid_values:
            print(
                f"❌ REJECTED: {pair} | "
                f"Некорректные значения: "
                f"{', '.join(invalid_values)}"
            )
            return None

        # ----------------------------------------------------
        # DEBUG VALUES
        # ----------------------------------------------------

        print(
            f"💰 CLOSE: {close}"
        )

        print(
            f"📈 EMA FAST: {ema_fast:.8f}"
        )

        print(
            f"📈 EMA SLOW: {ema_slow:.8f}"
        )

        print(
            f"📈 EMA 50: {ema_50:.8f}"
        )

        print(
            f"📊 RSI: {rsi:.2f}"
        )

        print(
            f"📊 MACD: {macd:.8f}"
        )

        print(
            f"📊 MACD SIGNAL: {macd_signal:.8f}"
        )

        print(
            f"📊 MACD HIST: {macd_hist:.8f}"
        )

        print(
            f"📊 STOCH K: {stoch_k:.2f}"
        )

        print(
            f"📊 STOCH D: {stoch_d:.2f}"
        )

        print(
            f"📊 BODY RATIO: {body_ratio:.2f}"
        )

        print(
            f"📊 VOLATILITY: {volatility:.6f}"
        )

        # ----------------------------------------------------
        # SCORES
        # ----------------------------------------------------

        call_score = 0.0
        put_score = 0.0

        call_confirmations: list[str] = []
        put_confirmations: list[str] = []

        call_reasons: list[str] = []
        put_reasons: list[str] = []

        # ====================================================
        # 1. EMA TREND — 20 POINTS
        # ====================================================

        if (
            close > ema_fast
            and ema_fast > ema_slow
            and ema_slow > ema_50
        ):
            call_score += 20

            self._append_unique(
                call_confirmations,
                "EMA тренд вверх",
            )

            self._append_unique(
                call_reasons,
                "Цена выше EMA и EMA выстроены вверх",
            )

        elif (
            close < ema_fast
            and ema_fast < ema_slow
            and ema_slow < ema_50
        ):
            put_score += 20

            self._append_unique(
                put_confirmations,
                "EMA тренд вниз",
            )

            self._append_unique(
                put_reasons,
                "Цена ниже EMA и EMA выстроены вниз",
            )

        elif (
            close > ema_fast
            and ema_fast >= ema_slow
        ):
            call_score += 13

            self._append_unique(
                call_confirmations,
                "EMA тренд вверх",
            )

            self._append_unique(
                call_reasons,
                "Краткосрочный EMA-тренд вверх",
            )

        elif (
            close < ema_fast
            and ema_fast <= ema_slow
        ):
            put_score += 13

            self._append_unique(
                put_confirmations,
                "EMA тренд вниз",
            )

            self._append_unique(
                put_reasons,
                "Краткосрочный EMA-тренд вниз",
            )

        elif close > ema_fast:
            call_score += 8

            self._append_unique(
                call_confirmations,
                "Цена выше EMA",
            )

        elif close < ema_fast:
            put_score += 8

            self._append_unique(
                put_confirmations,
                "Цена ниже EMA",
            )

        # ====================================================
        # 2. EMA SLOPE / MOMENTUM — 10 POINTS
        # ====================================================

        if ema_fast_slope > 0:
            call_score += 10

            self._append_unique(
                call_confirmations,
                "Импульс вверх",
            )

            self._append_unique(
                call_reasons,
                "EMA fast направлена вверх",
            )

        elif ema_fast_slope < 0:
            put_score += 10

            self._append_unique(
                put_confirmations,
                "Импульс вниз",
            )

            self._append_unique(
                put_reasons,
                "EMA fast направлена вниз",
            )

        # ====================================================
        # 3. MACD — 15 POINTS
        # ====================================================

        if (
            macd > macd_signal
            and macd_hist > 0
        ):
            call_score += 15

            self._append_unique(
                call_confirmations,
                "MACD подтверждает CALL",
            )

            self._append_unique(
                call_reasons,
                "MACD и histogram выше нуля",
            )

        elif (
            macd < macd_signal
            and macd_hist < 0
        ):
            put_score += 15

            self._append_unique(
                put_confirmations,
                "MACD подтверждает PUT",
            )

            self._append_unique(
                put_reasons,
                "MACD и histogram ниже нуля",
            )

        elif macd > macd_signal:
            call_score += 8

            self._append_unique(
                call_confirmations,
                "MACD подтверждает CALL",
            )

        elif macd < macd_signal:
            put_score += 8

            self._append_unique(
                put_confirmations,
                "MACD подтверждает PUT",
            )

        # ====================================================
        # 4. RSI — 10 POINTS
        # ====================================================

        # CALL:
        # нормальная бычья зона.
        if 52 <= rsi <= 68:
            call_score += 10

            self._append_unique(
                call_confirmations,
                "RSI подтверждает рост",
            )

            self._append_unique(
                call_reasons,
                "RSI находится в бычьей зоне",
            )

        elif 50 < rsi < 52:
            call_score += 5

            self._append_unique(
                call_confirmations,
                "RSI поддерживает CALL",
            )

        # PUT:
        # нормальная медвежья зона.
        elif 32 <= rsi <= 48:
            put_score += 10

            self._append_unique(
                put_confirmations,
                "RSI подтверждает падение",
            )

            self._append_unique(
                put_reasons,
                "RSI находится в медвежьей зоне",
            )

        elif 48 < rsi < 50:
            put_score += 5

            self._append_unique(
                put_confirmations,
                "RSI поддерживает PUT",
            )

        # Экстремальные зоны сами по себе
        # не заставляют нас открывать сделку.
        elif rsi < 30:
            self._append_unique(
                call_reasons,
                "RSI в зоне сильной перепроданности",
            )

        elif rsi > 70:
            self._append_unique(
                put_reasons,
                "RSI в зоне сильной перекупленности",
            )

        # ====================================================
        # 5. BOLLINGER BANDS — 10 POINTS
        # ====================================================

        bb_middle = (
            bb_upper + bb_lower
        ) / 2.0

        bb_range = (
            bb_upper - bb_lower
        )

        # Защита от деления на ноль.
        if bb_range <= 0:
            bb_position = 0.5
        else:
            bb_position = (
                close - bb_lower
            ) / bb_range

        # CALL:
        # цена в нижней половине BB.
        if close <= bb_lower:
            call_score += 10

            self._append_unique(
                call_confirmations,
                "Цена возле нижней BB",
            )

            self._append_unique(
                call_reasons,
                "Цена находится возле нижней полосы Bollinger",
            )

        elif close < bb_middle:
            call_score += 5

            self._append_unique(
                call_confirmations,
                "Цена ниже средней BB",
            )

        # PUT:
        # цена в верхней половине BB.
        elif close >= bb_upper:
            put_score += 10

            self._append_unique(
                put_confirmations,
                "Цена возле верхней BB",
            )

            self._append_unique(
                put_reasons,
                "Цена находится возле верхней полосы Bollinger",
            )

        elif close > bb_middle:
            put_score += 5

            self._append_unique(
                put_confirmations,
                "Цена выше средней BB",
            )

        # ====================================================
        # 6. STOCHASTIC — 10 POINTS
        # ====================================================

        if (
            stoch_k > stoch_d
            and stoch_k < 80
        ):
            call_score += 10

            self._append_unique(
                call_confirmations,
                "Stochastic вверх",
            )

            self._append_unique(
                call_reasons,
                "Stochastic поддерживает рост",
            )

        elif (
            stoch_k < stoch_d
            and stoch_k > 20
        ):
            put_score += 10

            self._append_unique(
                put_confirmations,
                "Stochastic вниз",
            )

            self._append_unique(
                put_reasons,
                "Stochastic поддерживает падение",
            )

        elif stoch_k < 20:
            call_score += 5

            self._append_unique(
                call_confirmations,
                "Stochastic разворачивается вверх",
            )

        elif stoch_k > 80:
            put_score += 5

            self._append_unique(
                put_confirmations,
                "Stochastic разворачивается вниз",
            )

        # ====================================================
        # 7. CANDLE — 10 POINTS
        # ====================================================

        # Ограничиваем body_ratio,
        # чтобы странные данные не ломали score.
        safe_body_ratio = self._clamp(
            body_ratio,
            0.0,
            1.0,
        )

        # Определяем направление последней
        # готовой свечи через OHLC, если возможно.
        candle_open = self._safe_float(
            row.get("open")
        )

        candle_close = self._safe_float(
            row.get("close")
        )

        if (
            candle_open is not None
            and candle_close is not None
        ):

            if (
                candle_close > candle_open
                and safe_body_ratio >= 0.55
            ):
                call_score += 10

                self._append_unique(
                    call_confirmations,
                    "Сильная бычья свеча",
                )

                self._append_unique(
                    call_reasons,
                    "Последняя свеча имеет сильное бычье тело",
                )

            elif (
                candle_close < candle_open
                and safe_body_ratio >= 0.55
            ):
                put_score += 10

                self._append_unique(
                    put_confirmations,
                    "Сильная медвежья свеча",
                )

                self._append_unique(
                    put_reasons,
                    "Последняя свеча имеет сильное медвежье тело",
                )

            elif (
                candle_close > candle_open
                and safe_body_ratio >= 0.30
            ):
                call_score += 5

                self._append_unique(
                    call_confirmations,
                    "Бычья свеча",
                )

            elif (
                candle_close < candle_open
                and safe_body_ratio >= 0.30
            ):
                put_score += 5

                self._append_unique(
                    put_confirmations,
                    "Медвежья свеча",
                )

        # ====================================================
        # 8. SUPPORT / RESISTANCE — 10 POINTS
        # ====================================================

        sr_range = (
            resistance - support
        )

        if sr_range > 0:
            position = (
                close - support
            ) / sr_range

            position = self._clamp(
                position,
                0.0,
                1.0,
            )

            # Цена ближе к поддержке.
            if position <= 0.25:
                call_score += 10

                self._append_unique(
                    call_confirmations,
                    "Цена возле поддержки",
                )

                self._append_unique(
                    call_reasons,
                    "Цена находится в нижней части локального диапазона",
                )

            elif position <= 0.40:
                call_score += 5

                self._append_unique(
                    call_confirmations,
                    "Цена ближе к поддержке",
                )

            # Цена ближе к сопротивлению.
            elif position >= 0.75:
                put_score += 10

                self._append_unique(
                    put_confirmations,
                    "Цена возле сопротивления",
                )

                self._append_unique(
                    put_reasons,
                    "Цена находится в верхней части локального диапазона",
                )

            elif position >= 0.60:
                put_score += 5

                self._append_unique(
                    put_confirmations,
                    "Цена ближе к сопротивлению",
                )

        # ====================================================
        # 9. VOLATILITY — 5 POINTS
        # ====================================================

        volatility_score = 0.0

        if atr > 0 and volatility > 0:

            # Слишком маленькая волатильность:
            # рынок может быть практически мёртвым.
            if volatility < 0.0005:
                volatility_score = 0.0

            # Нормальная волатильность.
            elif volatility < 0.03:
                volatility_score = 5.0

            # Высокая, но ещё допустимая.
            elif volatility < 0.08:
                volatility_score = 4.0

            # Очень высокая волатильность.
            else:
                volatility_score = 2.0

        if volatility_score > 0:

            # Волатильность является качеством рынка,
            # поэтому добавляем её к обоим направлениям.
            call_score += volatility_score
            put_score += volatility_score

            self._append_unique(
                call_confirmations,
                "Нормальная волатильность",
            )

            self._append_unique(
                put_confirmations,
                "Нормальная волатильность",
            )

        # ====================================================
        # CLAMP
        # ====================================================

        call_score = self._clamp(
            call_score,
            0.0,
            100.0,
        )

        put_score = self._clamp(
            put_score,
            0.0,
            100.0,
        )

        # ====================================================
        # PRINT SCORE
        # ====================================================

        print("")
        print(
            f"🟢 CALL SCORE: {call_score:.1f}"
        )

        print(
            f"🔴 PUT SCORE: {put_score:.1f}"
        )

        score_difference = abs(
            call_score - put_score
        )

        print(
            f"📏 SCORE DIFFERENCE: "
            f"{score_difference:.1f}"
        )

        print(
            f"🟢 CALL CONFIRMATIONS: "
            f"{len(call_confirmations)}"
        )

        print(
            f"🔴 PUT CONFIRMATIONS: "
            f"{len(put_confirmations)}"
        )

        # ====================================================
        # DETERMINE DIRECTION
        # ====================================================

        if call_score > put_score:
            direction = "CALL"
            quality = call_score
            confirmations = call_confirmations
            reasons = call_reasons

        elif put_score > call_score:
            direction = "PUT"
            quality = put_score
            confirmations = put_confirmations
            reasons = put_reasons

        else:
            print(
                f"❌ REJECTED: {pair} | "
                f"CALL и PUT имеют одинаковый score"
            )
            return None

        # ====================================================
        # DIFFERENCE FILTER
        # ====================================================

        # Старое значение 20 было слишком жёстким.
        #
        # Теперь направление должно хотя бы заметно
        # превосходить противоположное.
        MIN_SCORE_DIFFERENCE = 12.0

        if score_difference < MIN_SCORE_DIFFERENCE:

            print(
                f"❌ REJECTED: {pair} | "
                f"Слишком маленькое преимущество "
                f"направления: "
                f"{score_difference:.1f}/"
                f"{MIN_SCORE_DIFFERENCE:.1f}"
            )

            return None

        # ====================================================
        # QUALITY
        # ====================================================

        print(
            f"🏆 QUALITY: {quality:.1f}/100"
        )

        if quality < self.min_quality:

            print(
                f"❌ REJECTED: {pair} | "
                f"Quality {quality:.1f} < "
                f"{self.min_quality:.1f}"
            )

            return None

        # ====================================================
        # CONFIRMATION BONUS
        # ====================================================

        # Не учитываем общую волатильность
        # как направление.
        directional_confirmations = [
            item
            for item in confirmations
            if "волатильность" not in item.lower()
        ]

        confirmations_count = len(
            directional_confirmations
        )

        # ====================================================
        # PROBABILITY / CONFIDENCE
        # ====================================================

        probability = (
            self._calculate_probability(
                quality=quality,
                score_difference=score_difference,
                confirmations_count=confirmations_count,
            )
        )

        print(
            f"📈 ESTIMATED CONFIDENCE: "
            f"{probability:.1f}%"
        )

        # ====================================================
        # PROBABILITY FILTER
        # ====================================================

        if probability < self.min_probability:

            print(
                f"❌ REJECTED: {pair} | "
                f"Confidence {probability:.1f}% < "
                f"{self.min_probability:.1f}%"
            )

            return None

        # ====================================================
        # ENTRY / EXPIRY
        # ====================================================

        entry_time = self._next_5_minute(
            analysis_time
        )

        expiry_time = (
            entry_time
            + timedelta(minutes=5)
        )

        # ====================================================
        # FINAL REASONS
        # ====================================================

        if not reasons:
            reasons = [
                "Достаточное количество подтверждений"
            ]

        # Ограничиваем количество причин,
        # чтобы Telegram-сообщение не было огромным.
        reasons = reasons[:8]

        confirmations = (
            confirmations[:8]
        )

        # ====================================================
        # FINAL LOG
        # ====================================================

        print("")
        print(
            "✅ SIGNAL ACCEPTED"
        )

        print(
            f"💱 PAIR: {pair}"
        )

        print(
            f"📌 DIRECTION: {direction}"
        )

        print(
            f"🏆 QUALITY: {quality:.1f}/100"
        )

        print(
            f"📈 CONFIDENCE: {probability:.1f}%"
        )

        print(
            f"⏰ ENTRY: "
            f"{entry_time.strftime('%H:%M')} МСК"
        )

        print(
            f"🎯 EXPIRY: "
            f"{expiry_time.strftime('%H:%M')} МСК"
        )

        print(
            f"✅ CONFIRMATIONS: "
            f"{len(confirmations)}"
        )

        print("=" * 70)
        print("")

        # ====================================================
        # RETURN
        # ====================================================

        return Signal(
            pair=pair,
            direction=direction,
            quality=round(
                quality,
                1,
            ),
            probability=round(
                probability,
                1,
            ),
            entry_time=entry_time,
            expiry_time=expiry_time,
            analysis_time=analysis_time,
            confirmations=confirmations,
            reasons=reasons,
        )


# ============================================================
# OPTIONAL DIRECT TEST
# ============================================================

if __name__ == "__main__":
    print(
        "SignalEngine loaded successfully."
    )

    print(
        f"TIMEZONE: {MOSCOW_TZ}"
    )

    print(
        f"MIN_QUALITY: {MIN_QUALITY}"
    )

    print(
        f"MIN_PROBABILITY: {MIN_PROBABILITY}%"
    )

    now = datetime.now(
        MOSCOW_TZ
    )

    next_entry = (
        SignalEngine._next_5_minute(
            now
        )
    )

    print(
        f"Current Moscow time: "
        f"{now.strftime('%Y-%m-%d %H:%M:%S')}"
    )

    print(
        f"Next entry: "
        f"{next_entry.strftime('%Y-%m-%d %H:%M:%S')}"
    )

    print(
        f"Expiry: "
        f"{(next_entry + timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')}"
    )
