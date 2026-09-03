from __future__ import annotations

import logging
from dataclasses import dataclass

from config import (
    SIGNAL_MINIMUM_QUALITY,
)
from market import Candle
from models import Direction
from signal_engine import signal_engine


logger = logging.getLogger(
    "quality_filter"
)


@dataclass(slots=True)
class TimeframeAnalysis:
    timeframe: str
    direction: Direction | None
    score: float
    reasons: list[str]


@dataclass(slots=True)
class QualityResult:
    accepted: bool
    direction: Direction | None

    quality_score: float

    confirmations: int
    total_checks: int

    reasons: list[str]
    rejected_reasons: list[str]

    timeframe_results: list[
        TimeframeAnalysis
    ]


TIMEFRAME_WEIGHTS: dict[str, float] = {
    "1m": 0.20,
    "3m": 0.25,
    "5m": 0.35,
    "15m": 0.45,
    "30m": 0.50,
    "1h": 0.60,
    "4h": 0.70,
}


def analyze_timeframe(
    timeframe: str,
    candles: list[Candle],
) -> TimeframeAnalysis:
    if not candles:
        raise ValueError(
            f"No candles for timeframe {timeframe}."
        )

    if len(candles) < 20:
        raise ValueError(
            f"Too few candles for {timeframe}: "
            f"{len(candles)}."
        )

    result = signal_engine.analyze(
        candles
    )

    score = max(
        0.0,
        min(
            100.0,
            float(result.score),
        ),
    )

    reasons = list(
        result.reasons or []
    )

    return TimeframeAnalysis(
        timeframe=str(timeframe),
        direction=result.direction,
        score=score,
        reasons=reasons,
    )


class QualityFilter:

    def __init__(
        self,
        minimum_quality: float = SIGNAL_MINIMUM_QUALITY,
        minimum_confirmations: int = 2,
        minimum_timeframe_score: float = 55.0,
    ) -> None:
        self.minimum_quality = max(
            0.0,
            min(
                100.0,
                float(minimum_quality),
            ),
        )

        self.minimum_confirmations = max(
            1,
            int(minimum_confirmations),
        )

        self.minimum_timeframe_score = max(
            0.0,
            min(
                100.0,
                float(minimum_timeframe_score),
            ),
        )

    @staticmethod
    def _timeframe_weight(
        timeframe: str,
    ) -> float:
        return TIMEFRAME_WEIGHTS.get(
            str(timeframe).lower().strip(),
            0.30,
        )

    def _weighted_score(
        self,
        selected: list[TimeframeAnalysis],
    ) -> float:
        if not selected:
            return 0.0

        weighted = 0.0
        total_weight = 0.0

        for item in selected:
            weight = self._timeframe_weight(
                item.timeframe
            )

            weighted += (
                item.score * weight
            )

            total_weight += weight

        if total_weight <= 0:
            return 0.0

        return weighted / total_weight

    @staticmethod
    def _agreement_bonus(
        confirmations: int,
        total_valid: int,
    ) -> float:
        if total_valid <= 0:
            return 0.0

        ratio = (
            confirmations
            / total_valid
        )

        if ratio >= 1.0:
            return 15.0

        if ratio >= 0.80:
            return 9.0

        if ratio >= 0.66:
            return 5.0

        if ratio >= 0.60:
            return 2.0

        return 0.0

    @staticmethod
    def _strong_tf_bonus(
        selected: list[TimeframeAnalysis],
    ) -> float:
        strong_80 = sum(
            item.score >= 80.0
            for item in selected
        )

        strong_70 = sum(
            item.score >= 70.0
            for item in selected
        )

        if strong_80 >= 3:
            return 12.0

        if strong_80 >= 2:
            return 8.0

        if strong_80 >= 1:
            return 4.0

        if strong_70 >= 2:
            return 3.0

        return 0.0

    @staticmethod
    def _conflict_penalty(
        selected: list[TimeframeAnalysis],
        valid: list[TimeframeAnalysis],
    ) -> float:
        selected_ids = {
            id(item)
            for item in selected
        }

        conflicting = [
            item
            for item in valid
            if id(item)
            not in selected_ids
        ]

        if not conflicting:
            return 0.0

        average = (
            sum(
                item.score
                for item in conflicting
            )
            / len(conflicting)
        )

        return min(
            15.0,
            average * 0.15,
        )

    def _calculate_quality(
        self,
        selected: list[TimeframeAnalysis],
        valid: list[TimeframeAnalysis],
        confirmations: int,
    ) -> float:
        if not selected or not valid:
            return 0.0

        weighted = self._weighted_score(
            selected
        )

        agreement = self._agreement_bonus(
            confirmations,
            len(valid),
        )

        strong = self._strong_tf_bonus(
            selected
        )

        conflict = self._conflict_penalty(
            selected,
            valid,
        )

        confirmation_bonus = 0.0

        if confirmations >= 3:
            confirmation_bonus = 8.0
        elif confirmations >= 2:
            confirmation_bonus = 3.0

        return max(
            0.0,
            min(
                100.0,
                weighted
                + agreement
                + strong
                + confirmation_bonus
                - conflict,
            ),
        )

    def evaluate(
        self,
        analyses: list[TimeframeAnalysis],
    ) -> QualityResult:
        if not analyses:
            return QualityResult(
                accepted=False,
                direction=None,
                quality_score=0.0,
                confirmations=0,
                total_checks=0,
                reasons=[],
                rejected_reasons=[
                    "Нет данных для анализа."
                ],
                timeframe_results=[],
            )

        valid = [
            item
            for item in analyses
            if (
                item.direction is not None
                and item.score
                >= self.minimum_timeframe_score
            )
        ]

        if not valid:
            return QualityResult(
                accepted=False,
                direction=None,
                quality_score=0.0,
                confirmations=0,
                total_checks=len(analyses),
                reasons=[],
                rejected_reasons=[
                    "Нет достаточно сильных таймфреймов."
                ],
                timeframe_results=analyses,
            )

        up_count = sum(
            item.direction == Direction.UP
            for item in valid
        )

        down_count = sum(
            item.direction == Direction.DOWN
            for item in valid
        )

        if up_count == down_count:
            return QualityResult(
                accepted=False,
                direction=None,
                quality_score=0.0,
                confirmations=0,
                total_checks=len(valid),
                reasons=[],
                rejected_reasons=[
                    "Таймфреймы разделились по направлению."
                ],
                timeframe_results=analyses,
            )

        if up_count > down_count:
            direction = Direction.UP
            confirmations = up_count
        else:
            direction = Direction.DOWN
            confirmations = down_count

        selected = [
            item
            for item in valid
            if item.direction == direction
        ]

        agreement_ratio = (
            confirmations
            / len(valid)
        )

        rejected: list[str] = []

        if confirmations < self.minimum_confirmations:
            rejected.append(
                f"Недостаточно подтверждений: "
                f"{confirmations}/"
                f"{self.minimum_confirmations}."
            )

        if agreement_ratio < 0.60:
            rejected.append(
                f"Слабое согласие таймфреймов: "
                f"{agreement_ratio * 100:.1f}%."
            )

        quality_score = (
            self._calculate_quality(
                selected=selected,
                valid=valid,
                confirmations=confirmations,
            )
        )

        if quality_score < self.minimum_quality:
            rejected.append(
                f"Quality score ниже порога: "
                f"{quality_score:.1f}% < "
                f"{self.minimum_quality:.1f}%."
            )

        reasons: list[str] = []

        for item in selected:
            reasons.extend(
                item.reasons
            )

        reasons = list(
            dict.fromkeys(
                reasons
            )
        )

        reasons.append(
            f"Подтверждение TF: "
            f"{confirmations}/{len(valid)}"
        )

        reasons.append(
            f"Согласованность TF: "
            f"{agreement_ratio * 100:.1f}%"
        )

        reasons.append(
            f"Итоговый Quality score: "
            f"{quality_score:.1f}%"
        )

        return QualityResult(
            accepted=not rejected,
            direction=(
                direction
                if not rejected
                else None
            ),
            quality_score=quality_score,
            confirmations=confirmations,
            total_checks=len(valid),
            reasons=reasons,
            rejected_reasons=rejected,
            timeframe_results=analyses,
        )


# =========================================================
# GLOBAL FILTER
# =========================================================

# ВАЖНО:
#
# Раньше здесь было жёстко:
#
#     minimum_quality=85.0
#
# Теперь используется конфигурация.
#
# По умолчанию:
#
#     75%
#
# При этом Render Environment Variable
# SIGNAL_MINIMUM_QUALITY имеет приоритет.

quality_filter = QualityFilter(
    minimum_quality=SIGNAL_MINIMUM_QUALITY,
    minimum_confirmations=2,
    minimum_timeframe_score=55.0,
)


__all__ = [
    "TimeframeAnalysis",
    "QualityResult",
    "QualityFilter",
    "analyze_timeframe",
    "quality_filter",
]
