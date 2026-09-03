from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

import pandas as pd

from signal_engine import Signal, SignalEngine


logger = logging.getLogger(__name__)


MIN_EXPIRY_MINUTES = 1
MAX_EXPIRY_MINUTES = 20

# Минимум исторических сделок для доверия к результату.
MIN_TRADES_FOR_SELECTION = 10

# Сколько последних исторических точек использовать.
DEFAULT_TEST_POINTS = 100

# Минимальный исторический WINRATE для выбора
# экспирации в режиме "Любое время".
MIN_HISTORICAL_WINRATE = 50.0


@dataclass
class ExpiryStats:
    expiry_minutes: int

    total: int = 0
    wins: int = 0
    losses: int = 0

    quality_sum: float = 0.0
    probability_sum: float = 0.0

    @property
    def winrate(self) -> float:
        if self.total <= 0:
            return 0.0

        return (
            self.wins
            / self.total
            * 100.0
        )

    @property
    def average_quality(self) -> float:
        if self.total <= 0:
            return 0.0

        return (
            self.quality_sum
            / self.total
        )

    @property
    def average_probability(self) -> float:
        if self.total <= 0:
            return 0.0

        return (
            self.probability_sum
            / self.total
        )


class ExpiryBacktester:
    """
    Исторический backtest экспираций 1..20 минут.

    Важный принцип:

        Нельзя смотреть в будущее при формировании сигнала.

    Для каждой исторической точки:

        1. Берём только свечи ДО этой точки.
        2. Строим индикаторы.
        3. Получаем сигнал.
        4. Определяем направление.
        5. Берём будущую цену через N минут.
        6. Сравниваем результат.

    Поэтому backtest не использует будущие свечи
    для принятия решения.
    """

    def __init__(
        self,
        engine: Optional[SignalEngine] = None,
        min_trades: int = MIN_TRADES_FOR_SELECTION,
    ) -> None:

        self.engine = (
            engine
            if engine is not None
            else SignalEngine()
        )

        self.min_trades = max(
            1,
            int(min_trades),
        )

    # ============================================================
    # DATETIME
    # ============================================================

    @staticmethod
    def _prepare_datetime(
        candles: pd.DataFrame,
    ) -> pd.DataFrame:

        df = candles.copy()

        if "datetime" in df.columns:

            dt = pd.to_datetime(
                df["datetime"],
                utc=True,
                errors="coerce",
            )

        elif isinstance(
            df.index,
            pd.DatetimeIndex,
        ):

            dt = pd.to_datetime(
                df.index,
                utc=True,
                errors="coerce",
            )

            df = df.reset_index(
                drop=True
            )

        else:

            return df

        df["datetime"] = dt

        df = (
            df.dropna(
                subset=["datetime"]
            )
            .sort_values(
                "datetime"
            )
            .drop_duplicates(
                subset=["datetime"],
                keep="last",
            )
            .reset_index(
                drop=True
            )
        )

        return df

    # ============================================================
    # PRICE
    # ============================================================

    @staticmethod
    def _price_column(
        candles: pd.DataFrame,
    ) -> Optional[str]:

        if "close" in candles.columns:
            return "close"

        return None

    # ============================================================
    # FUTURE INDEX
    # ============================================================

    @staticmethod
    def _find_future_index(
        candles: pd.DataFrame,
        entry_index: int,
        expiry_minutes: int,
    ) -> Optional[int]:
        """
        Находим свечу, которая соответствует моменту
        entry + expiry.

        Предпочтительно используем timestamp,
        а не просто +N строк, чтобы backtest
        корректнее переживал пропуски.
        """

        if (
            "datetime"
            not in candles.columns
        ):
            future_index = (
                entry_index
                + expiry_minutes
            )

            if (
                future_index
                >= len(candles)
            ):
                return None

            return future_index

        entry_time = candles.iloc[
            entry_index
        ]["datetime"]

        target_time = (
            entry_time
            + timedelta(
                minutes=expiry_minutes
            )
        )

        future_times = candles[
            "datetime"
        ]

        matches = future_times[
            future_times >= target_time
        ]

        if matches.empty:
            return None

        first_position = matches.index[0]

        return int(
            first_position
        )

    # ============================================================
    # RESULT
    # ============================================================

    @staticmethod
    def _is_win(
        direction: str,
        entry_price: float,
        exit_price: float,
    ) -> Optional[bool]:
        """
        Для бинарного backtest:

            CALL:
                exit > entry

            PUT:
                exit < entry

        Если цена ровно одинаковая,
        результат считаем неопределённым.
        """

        if (
            entry_price
            == exit_price
        ):
            return None

        direction = str(
            direction
        ).upper()

        if direction == "CALL":

            return (
                exit_price
                > entry_price
            )

        if direction == "PUT":

            return (
                exit_price
                < entry_price
            )

        return None

    # ============================================================
    # SINGLE EXPIRY
    # ============================================================

    def backtest_expiry(
        self,
        pair: str,
        candles: pd.DataFrame,
        expiry_minutes: int,
        test_points: int = DEFAULT_TEST_POINTS,
    ) -> ExpiryStats:

        expiry_minutes = max(
            MIN_EXPIRY_MINUTES,
            min(
                MAX_EXPIRY_MINUTES,
                int(expiry_minutes),
            ),
        )

        stats = ExpiryStats(
            expiry_minutes=expiry_minutes
        )

        df = self._prepare_datetime(
            candles
        )

        price_column = (
            self._price_column(df)
        )

        if price_column is None:
            return stats

        if len(df) < (
            self.engine.MIN_CANDLES
            + expiry_minutes
            + 5
        ):
            return stats

        # --------------------------------------------------------
        # Последние test_points исторических точек.
        #
        # ВАЖНО:
        # Последняя точка должна иметь достаточно
        # будущих свечей для проверки результата.
        # --------------------------------------------------------

        first_index = (
            self.engine.MIN_CANDLES
        )

        last_index = (
            len(df)
            - expiry_minutes
            - 1
        )

        if last_index <= first_index:
            return stats

        available_points = (
            last_index
            - first_index
            + 1
        )

        points = min(
            int(test_points),
            available_points,
        )

        start_index = (
            last_index
            - points
            + 1
        )

        for current_index in range(
            start_index,
            last_index + 1,
        ):

            # ----------------------------------------------------
            # Только история до текущей точки.
            # ----------------------------------------------------

            historical_df = df.iloc[
                : current_index + 1
            ].copy()

            try:

                signal = self.engine.analyze(
                    pair=pair,
                    candles=historical_df,
                    expiry_minutes=expiry_minutes,
                )

            except Exception as exc:

                logger.debug(
                    "[BACKTEST] "
                    "%s %sm analysis error: %s",
                    pair,
                    expiry_minutes,
                    exc,
                )

                continue

            if signal is None:
                continue

            # ----------------------------------------------------
            # Цена входа.
            #
            # Для backtest используем close текущей
            # исторической свечи.
            # ----------------------------------------------------

            try:

                entry_price = float(
                    df.iloc[
                        current_index
                    ][price_column]
                )

            except (
                TypeError,
                ValueError,
            ):

                continue

            # ----------------------------------------------------
            # Будущая цена.
            # ----------------------------------------------------

            future_index = (
                self._find_future_index(
                    candles=df,
                    entry_index=current_index,
                    expiry_minutes=expiry_minutes,
                )
            )

            if future_index is None:
                continue

            try:

                exit_price = float(
                    df.iloc[
                        future_index
                    ][price_column]
                )

            except (
                TypeError,
                ValueError,
            ):

                continue

            result = self._is_win(
                direction=signal.direction,
                entry_price=entry_price,
                exit_price=exit_price,
            )

            if result is None:
                continue

            stats.total += 1

            if result:
                stats.wins += 1
            else:
                stats.losses += 1

            stats.quality_sum += float(
                signal.quality
            )

            stats.probability_sum += float(
                signal.probability
            )

        return stats

    # ============================================================
    # ALL EXPIRIES
    # ============================================================

    def backtest_all(
        self,
        pair: str,
        candles: pd.DataFrame,
        test_points: int = DEFAULT_TEST_POINTS,
    ) -> list[ExpiryStats]:

        results: list[
            ExpiryStats
        ] = []

        for expiry in range(
            MIN_EXPIRY_MINUTES,
            MAX_EXPIRY_MINUTES + 1,
        ):

            stats = self.backtest_expiry(
                pair=pair,
                candles=candles,
                expiry_minutes=expiry,
                test_points=test_points,
            )

            results.append(
                stats
            )

        return results

    # ============================================================
    # BEST EXPIRY
    # ============================================================

    def choose_best_expiry(
        self,
        pair: str,
        candles: pd.DataFrame,
        test_points: int = DEFAULT_TEST_POINTS,
    ) -> tuple[
        Optional[int],
        list[ExpiryStats],
    ]:

        results = self.backtest_all(
            pair=pair,
            candles=candles,
            test_points=test_points,
        )

        eligible = [
            item
            for item in results
            if (
                item.total
                >= self.min_trades
                and item.winrate
                >= MIN_HISTORICAL_WINRATE
            )
        ]

        if not eligible:

            return (
                None,
                results,
            )

        # --------------------------------------------------------
        # Основной критерий:
        #
        # 1. WINRATE
        # 2. количество сделок
        # 3. среднее качество
        # 4. средняя расчётная вероятность
        #
        # Таким образом 100% на 10 сделках
        # лучше 100% на 1 сделке только если
        # обе прошли минимальный порог.
        # --------------------------------------------------------

        eligible.sort(
            key=lambda item: (
                item.winrate,
                item.total,
                item.average_quality,
                item.average_probability,
            ),
            reverse=True,
        )

        return (
            eligible[0].expiry_minutes,
            results,
        )

    # ============================================================
    # REPORT
    # ============================================================

    @staticmethod
    def format_report(
        results: list[ExpiryStats],
    ) -> str:

        lines = [
            "📊 <b>BACKTEST 1–20 МИНУТ</b>",
            "",
        ]

        for item in results:

            if item.total <= 0:

                lines.append(
                    f"⏱ {item.expiry_minutes:02d} мин"
                    " — нет данных"
                )

                continue

            lines.append(
                f"⏱ {item.expiry_minutes:02d} мин"
                f" — "
                f"<b>{item.winrate:.1f}%</b>"
                f" | "
                f"W {item.wins}"
                f" / "
                f"L {item.losses}"
                f" | "
                f"N {item.total}"
            )

        return "\n".join(
            lines
        )


__all__ = [
    "ExpiryStats",
    "ExpiryBacktester",
]
