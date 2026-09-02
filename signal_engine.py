from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from config import MIN_PROBABILITY, MIN_QUALITY, TIMEZONE
from indicators import calculate_indicators


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


class SignalEngine:
    """
    Анализатор торговых сигналов.

    ВАЖНО:
    probability здесь является расчётной оценкой силы сигнала,
    а не гарантированной вероятностью выигрыша.
    Реальная калибровка вероятности должна выполняться
    по накопленной истории WIN/LOSS.
    """

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

    # ============================================================
    # TIME
    # ============================================================

    @staticmethod
    def _next_5_minute(
        current_time: Optional[datetime] = None,
    ) -> datetime:
        """
        Возвращает ближайшее будущее 5-минутное время.

        Если сейчас 21:07 -> 21:10
        Если сейчас 21:10:05 -> 21:15
        """

        if current_time is None:
            current_time = datetime.now(timezone.utc)

        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        current_time = current_time.astimezone(timezone.utc)

        minute = (current_time.minute // 5 + 1) * 5

        if minute >= 60:
            entry_time = (
                current_time
                + timedelta(hours=1)
            ).replace(
                minute=0,
                second=0,
                microsecond=0,
            )
        else:
            entry_time = current_time.replace(
                minute=minute,
                second=0,
                microsecond=0,
            )

        return entry_time

    # ============================================================
    # HELPERS
    # ============================================================

    @staticmethod
    def _clamp(
        value: float,
        minimum: float,
        maximum: float,
    ) -> float:
        return max(minimum, min(maximum, value))

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

    # ============================================================
    # PROBABILITY
    # ============================================================

    def _calculate_probability(
        self,
        quality: float,
        score_difference: float,
        confirmations_count: int,
    ) -> float:
        """
        Расчётная оценка вероятности.

        Это НЕ фактический WINRATE.
        """

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

        if score_difference >= 35:
            probability += 3.0
        elif score_difference >= 30:
            probability += 2.0
        elif score_difference >= 25:
            probability += 1.0

        if confirmations_count >= 6:
            probability += 2.0
        elif confirmations_count >= 5:
            probability += 1.0

        return round(
            self._clamp(probability, 50.0, 95.0),
            1,
        )

    # ============================================================
    # ANALYSIS
    # ============================================================

    def analyze(
        self,
        pair: str,
        candles: pd.DataFrame,
    ) -> Optional[Signal]:
        """
        Анализирует одну валютную пару.

        Все причины отказа выводятся в Render logs.
        """

        pair = str(pair).strip()

        analysis_time = datetime.now(timezone.utc)

        print("")
        print("=" * 70)
        print(f"🔎 ANALYSIS: {pair}")
        print("=" * 70)

        # --------------------------------------------------------
        # BASIC DATA CHECK
        # --------------------------------------------------------

        if candles is None:
            print(f"❌ {pair}: candles=None")
            print(f"❌ REJECTED: {pair} | Причина: нет данных")
            return None

        if not isinstance(candles, pd.DataFrame):
            print(
                f"❌ {pair}: неправильный тип данных: "
                f"{type(candles).__name__}"
            )
            print(f"❌ REJECTED: {pair} | Причина: неправильный DataFrame")
            return None

        if candles.empty:
            print(f"❌ {pair}: DataFrame пустой")
            print(f"❌ REJECTED: {pair} | Причина: нет свечей")
            return None

        print(f"📥 Candles received: {len(candles)}")

        if len(candles) < 80:
            print(
                f"❌ REJECTED: {pair} | "
                f"Недостаточно свечей: {len(candles)}/80"
            )
            return None

        # --------------------------------------------------------
        # INDICATORS
        # --------------------------------------------------------

        try:
            df = calculate_indicators(candles.copy())
        except Exception as exc:
            print(
                f"❌ {pair}: ошибка calculate_indicators: "
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

        # --------------------------------------------------------
        # REMOVE FORMING CANDLE
        # --------------------------------------------------------

        if len(df) > 1:
            df = df.iloc[:-1].copy()

        if len(df) < 70:
            print(
                f"❌ REJECTED: {pair} | "
                f"Недостаточно готовых свечей после фильтрации: "
                f"{len(df)}/70"
            )
            return None

        # --------------------------------------------------------
        # REQUIRED COLUMNS
        # --------------------------------------------------------

        required_columns = [
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

        missing = [
            column
            for column in required_columns
            if column not in df.columns
        ]

        if missing:
            print(
                f"❌ REJECTED: {pair} | "
                f"Отсутствуют индикаторы: {', '.join(missing)}"
            )
            return None

        row = self._last_row(df)

        if row is None:
            print(
                f"❌ REJECTED: {pair} | "
                f"Последняя свеча отсутствует"
            )
            return None

        # --------------------------------------------------------
        # CURRENT VALUES
        # --------------------------------------------------------

        close = self._safe_float(row.get("close"))
        ema_fast = self._safe_float(row.get("ema_fast"))
        ema_slow = self._safe_float(row.get("ema_slow"))
        ema_50 = self._safe_float(row.get("ema_50"))

        rsi = self._safe_float(row.get("rsi"))

        macd = self._safe_float(row.get("macd"))
        macd_signal = self._safe_float(row.get("macd_signal"))
        macd_hist = self._safe_float(row.get("macd_hist"))

        bb_upper = self._safe_float(row.get("bb_upper"))
        bb_lower = self._safe_float(row.get("bb_lower"))

        stoch_k = self._safe_float(row.get("stoch_k"))
        stoch_d = self._safe_float(row.get("stoch_d"))

        atr = self._safe_float(row.get("atr"))

        body_ratio = self._safe_float(row.get("body_ratio"))

        support = self._safe_float(row.get("support"))
        resistance = self._safe_float(row.get("resistance"))

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

        # --------------------------------------------------------
        # SCORES
        # --------------------------------------------------------

        call_score = 0.0
        put_score = 0.0

        call_confirmations: list[str] = []
        put_confirmations: list[str] = []

        call_reasons: list[str] = []
        put_reasons: list[str] = []

        # --------------------------------------------------------
        # 1. EMA TREND — 20
        # --------------------------------------------------------

        if close > ema_fast > ema_slow > ema_50:
            call_score += 20
            call_confirmations.append("EMA тренд вверх")
            call_reasons.append(
                "Цена выше EMA9 > EMA21 > EMA50"
            )

        elif close < ema_fast < ema_slow < ema_50:
            put_score += 20
            put_confirmations.append("EMA тренд вниз")
            put_reasons.append(
                "Цена ниже EMA9 < EMA21 < EMA50"
            )

        else:
            if close > ema_fast:
                call_score += 7
                call_confirmations.append("Цена выше EMA9")

            if close < ema_fast:
                put_score += 7
                put_confirmations.append("Цена ниже EMA9")

        # --------------------------------------------------------
        # 2. EMA MOMENTUM — 10
        # --------------------------------------------------------

        if ema_fast_slope > 0:
            call_score += 10
            call_confirmations.append("EMA momentum вверх")
            call_reasons.append("EMA9 имеет положительный наклон")

        elif ema_fast_slope < 0:
            put_score += 10
            put_confirmations.append("EMA momentum вниз")
            put_reasons.append("EMA9 имеет отрицательный наклон")

        # --------------------------------------------------------
        # 3. MACD — 15
        # --------------------------------------------------------

        if macd > macd_signal and macd_hist > 0:
            call_score += 15
            call_confirmations.append("MACD bullish")
            call_reasons.append(
                "MACD выше сигнальной линии"
            )

        elif macd < macd_signal and macd_hist < 0:
            put_score += 15
            put_confirmations.append("MACD bearish")
            put_reasons.append(
                "MACD ниже сигнальной линии"
            )

        # --------------------------------------------------------
        # 4. RSI — 10
        # --------------------------------------------------------

        if 50 <= rsi <= 68:
            call_score += 10
            call_confirmations.append("RSI bullish zone")
            call_reasons.append(
                f"RSI={rsi:.1f}"
            )

        elif 32 <= rsi <= 50:
            put_score += 10
            put_confirmations.append("RSI bearish zone")
            put_reasons.append(
                f"RSI={rsi:.1f}"
            )

        elif rsi < 30:
            call_score += 5
            call_confirmations.append("RSI oversold")
            call_reasons.append(
                f"RSI перепродан: {rsi:.1f}"
            )

        elif rsi > 70:
            put_score += 5
            put_confirmations.append("RSI overbought")
            put_reasons.append(
                f"RSI перекуплен: {rsi:.1f}"
            )

        # --------------------------------------------------------
        # 5. BOLLINGER — 10
        # --------------------------------------------------------

        if close < bb_lower:
            call_score += 10
            call_confirmations.append("Bollinger oversold")
            call_reasons.append(
                "Цена ниже нижней полосы Bollinger"
            )

        elif close > bb_upper:
            put_score += 10
            put_confirmations.append("Bollinger overbought")
            put_reasons.append(
                "Цена выше верхней полосы Bollinger"
            )
        else:
            middle = (bb_upper + bb_lower) / 2

            if close > middle:
                call_score += 5
                call_confirmations.append(
                    "Цена выше средней Bollinger"
                )

            elif close < middle:
                put_score += 5
                put_confirmations.append(
                    "Цена ниже средней Bollinger"
                )

        # --------------------------------------------------------
        # 6. STOCHASTIC — 10
        # --------------------------------------------------------

        if stoch_k > stoch_d and stoch_k < 80:
            call_score += 10
            call_confirmations.append("Stochastic bullish")
            call_reasons.append(
                f"Stochastic K={stoch_k:.1f}"
            )

        elif stoch_k < stoch_d and stoch_k > 20:
            put_score += 10
            put_confirmations.append("Stochastic bearish")
            put_reasons.append(
                f"Stochastic K={stoch_k:.1f}"
            )

        elif stoch_k < 20:
            call_score += 5
            call_confirmations.append(
                "Stochastic oversold"
            )

        elif stoch_k > 80:
            put_score += 5
            put_confirmations.append(
                "Stochastic overbought"
            )

        # --------------------------------------------------------
        # 7. CANDLE — 10
        # --------------------------------------------------------

        if len(df) >= 2:
            previous = df.iloc[-2]

            prev_close = self._safe_float(
                previous.get("close")
            )

            prev_open = self._safe_float(
                previous.get("open")
            )

            current_open = self._safe_float(
                row.get("open")
            )

            current_close = close

            if (
                current_open is not None
                and current_close is not None
                and current_close > current_open
                and body_ratio >= 0.5
            ):
                call_score += 10
                call_confirmations.append(
                    "Сильная bullish свеча"
                )
                call_reasons.append(
                    "Тело текущей свечи подтверждает CALL"
                )

            elif (
                current_open is not None
                and current_close is not None
                and current_close < current_open
                and body_ratio >= 0.5
            ):
                put_score += 10
                put_confirmations.append(
                    "Сильная bearish свеча"
                )
                put_reasons.append(
                    "Тело текущей свечи подтверждает PUT"
                )

            if (
                prev_close is not None
                and prev_open is not None
                and current_close is not None
            ):
                if (
                    current_close > prev_close
                    and current_close > current_open
                ):
                    call_score += 0

                elif (
                    current_close < prev_close
                    and current_close < current_open
                ):
                    put_score += 0

        # --------------------------------------------------------
        # 8. SUPPORT / RESISTANCE — 10
        # --------------------------------------------------------

        range_size = resistance - support

        if range_size > 0:
            position = (
                (close - support) / range_size
            )

            if position <= 0.25:
                call_score += 10
                call_confirmations.append(
                    "Цена возле поддержки"
                )
                call_reasons.append(
                    "Цена находится в нижней части диапазона"
                )

            elif position >= 0.75:
                put_score += 10
                put_confirmations.append(
                    "Цена возле сопротивления"
                )
                put_reasons.append(
                    "Цена находится в верхней части диапазона"
                )
            else:
                # В середине диапазона не даём бонус.
                pass

        # --------------------------------------------------------
        # 9. VOLATILITY — 5
        # --------------------------------------------------------

        if atr > 0 and volatility > 0:
            if volatility < 0.03:
                call_score += 2
                put_score += 2

                call_confirmations.append(
                    "Стабильная волатильность"
                )
                put_confirmations.append(
                    "Стабильная волатильность"
                )

            else:
                call_score += 3
                put_score += 3

                call_confirmations.append(
                    "Достаточная волатильность"
                )
                put_confirmations.append(
                    "Достаточная волатильность"
                )

        # --------------------------------------------------------
        # SCORE OUTPUT
        # --------------------------------------------------------

        call_score = round(
            self._clamp(call_score, 0, 100),
            1,
        )

        put_score = round(
            self._clamp(put_score, 0, 100),
            1,
        )

        print("")
        print(f"📊 {pair}")
        print(f"   CALL score: {call_score}/100")
        print(f"   PUT  score: {put_score}/100")

        if call_score >= put_score:
            direction = "CALL"
            score = call_score
            confirmations = call_confirmations
            reasons = call_reasons
            opposite_score = put_score
        else:
            direction = "PUT"
            score = put_score
            confirmations = put_confirmations
            reasons = put_reasons
            opposite_score = call_score

        score_difference = abs(
            call_score - put_score
        )

        print(f"   🧭 Direction: {direction}")
        print(
            f"   📐 Difference: "
            f"{score_difference:.1f}"
        )

        # --------------------------------------------------------
        # QUALITY
        # --------------------------------------------------------

        quality = score

        print(
            f"   📊 Quality: "
            f"{quality:.1f}/100"
        )

        # --------------------------------------------------------
        # CONFIRMATIONS
        # --------------------------------------------------------

        confirmations_count = len(
            confirmations
        )

        print(
            f"   ✅ Confirmations: "
            f"{confirmations_count}"
        )

        if confirmations:
            for confirmation in confirmations:
                print(
                    f"      • {confirmation}"
                )
        else:
            print("      • Нет подтверждений")

        # --------------------------------------------------------
        # SCORE DIFFERENCE FILTER
        # --------------------------------------------------------

        if score_difference < 20:
            print(
                f"❌ REJECTED: {pair} | "
                f"Слишком маленькая разница "
                f"между направлениями: "
                f"{score_difference:.1f} < 20"
            )

            reasons.append(
                f"Разница направлений слишком мала: "
                f"{score_difference:.1f}/20"
            )

            return None

        # --------------------------------------------------------
        # QUALITY FILTER
        # --------------------------------------------------------

        if quality < self.min_quality:
            print(
                f"❌ REJECTED: {pair} | "
                f"Quality {quality:.1f} < "
                f"{self.min_quality:.1f}"
            )

            reasons.append(
                f"Quality ниже минимума: "
                f"{quality:.1f} < "
                f"{self.min_quality:.1f}"
            )

            return None

        # --------------------------------------------------------
        # PROBABILITY
        # --------------------------------------------------------

        probability = self._calculate_probability(
            quality=quality,
            score_difference=score_difference,
            confirmations_count=confirmations_count,
        )

        print(
            f"   📈 Probability estimate: "
            f"{probability:.1f}%"
        )

        # --------------------------------------------------------
        # PROBABILITY FILTER
        # --------------------------------------------------------

        if probability < self.min_probability:
            print(
                f"❌ REJECTED: {pair} | "
                f"Probability {probability:.1f}% < "
                f"{self.min_probability:.1f}%"
            )

            reasons.append(
                f"Расчётный шанс ниже минимума: "
                f"{probability:.1f}% < "
                f"{self.min_probability:.1f}%"
            )

            return None

        # --------------------------------------------------------
        # ENTRY / EXPIRY
        # --------------------------------------------------------

        entry_time = self._next_5_minute(
            analysis_time
        )

        expiry_time = (
            entry_time + timedelta(minutes=5)
        )

        # --------------------------------------------------------
        # SUCCESS
        # --------------------------------------------------------

        print("")
        print(
            f"🟢 ACCEPTED: {pair}"
        )
        print(
            f"   Direction: {direction}"
        )
        print(
            f"   Quality: {quality:.1f}/100"
        )
        print(
            f"   Probability: {probability:.1f}%"
        )
        print(
            f"   Entry: {entry_time}"
        )
        print(
            f"   Expiry: {expiry_time}"
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
            confirmations=confirmations,
            reasons=reasons,
        )
