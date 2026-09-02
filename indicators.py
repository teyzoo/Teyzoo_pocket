from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Расчёт технических индикаторов для SignalEngine.

    Вход:
        DataFrame с колонками:
        datetime, open, high, low, close, volume

    Выход:
        Тот же DataFrame + технические индикаторы.
    """

    df = df.copy()

    # ---------------------------------------------------------
    # Проверка и подготовка данных
    # ---------------------------------------------------------

    required = [
        "open",
        "high",
        "low",
        "close",
    ]

    for column in required:
        if column not in df.columns:
            raise ValueError(
                f"В свечах отсутствует колонка: {column}"
            )

        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    df = df.dropna(
        subset=required
    ).copy()

    if len(df) < 50:
        return df

    close = df["close"]
    high = df["high"]
    low = df["low"]

    # ---------------------------------------------------------
    # EMA
    # ---------------------------------------------------------

    df["ema_9"] = close.ewm(
        span=9,
        adjust=False,
    ).mean()

    df["ema_21"] = close.ewm(
        span=21,
        adjust=False,
    ).mean()

    df["ema_50"] = close.ewm(
        span=50,
        adjust=False,
    ).mean()

    # ---------------------------------------------------------
    # RSI 14
    # ---------------------------------------------------------

    delta = close.diff()

    gain = delta.clip(
        lower=0
    )

    loss = -delta.clip(
        upper=0
    )

    avg_gain = gain.ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14,
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14,
    ).mean()

    rs = avg_gain / avg_loss.replace(
        0,
        np.nan,
    )

    df["rsi"] = 100 - (
        100 / (1 + rs)
    )

    # ---------------------------------------------------------
    # MACD
    # ---------------------------------------------------------

    ema_12 = close.ewm(
        span=12,
        adjust=False,
    ).mean()

    ema_26 = close.ewm(
        span=26,
        adjust=False,
    ).mean()

    df["macd"] = ema_12 - ema_26

    df["macd_signal"] = df["macd"].ewm(
        span=9,
        adjust=False,
    ).mean()

    df["macd_histogram"] = (
        df["macd"] - df["macd_signal"]
    )

    # ---------------------------------------------------------
    # Bollinger Bands
    # ---------------------------------------------------------

    bb_middle = close.rolling(
        window=20,
        min_periods=20,
    ).mean()

    bb_std = close.rolling(
        window=20,
        min_periods=20,
    ).std()

    df["bb_middle"] = bb_middle

    df["bb_upper"] = (
        bb_middle + 2 * bb_std
    )

    df["bb_lower"] = (
        bb_middle - 2 * bb_std
    )

    # ---------------------------------------------------------
    # Stochastic
    # ---------------------------------------------------------

    lowest_low = low.rolling(
        window=14,
        min_periods=14,
    ).min()

    highest_high = high.rolling(
        window=14,
        min_periods=14,
    ).max()

    denominator = (
        highest_high - lowest_low
    ).replace(
        0,
        np.nan,
    )

    df["stoch_k"] = (
        (close - lowest_low)
        / denominator
        * 100
    )

    df["stoch_d"] = df["stoch_k"].rolling(
        window=3,
        min_periods=3,
    ).mean()

    # ---------------------------------------------------------
    # ATR 14
    # ---------------------------------------------------------

    previous_close = close.shift(1)

    true_range_1 = high - low

    true_range_2 = (
        high - previous_close
    ).abs()

    true_range_3 = (
        low - previous_close
    ).abs()

    true_range = pd.concat(
        [
            true_range_1,
            true_range_2,
            true_range_3,
        ],
        axis=1,
    ).max(
        axis=1
    )

    df["atr"] = true_range.rolling(
        window=14,
        min_periods=14,
    ).mean()

    # ---------------------------------------------------------
    # Свеча
    # ---------------------------------------------------------

    df["candle_body"] = (
        df["close"] - df["open"]
    ).abs()

    df["candle_range"] = (
        df["high"] - df["low"]
    )

    df["body_ratio"] = np.where(
        df["candle_range"] > 0,
        df["candle_body"]
        / df["candle_range"],
        0.0,
    )

    # ---------------------------------------------------------
    # Support / Resistance
    # ---------------------------------------------------------

    df["support"] = low.rolling(
        window=20,
        min_periods=20,
    ).min()

    df["resistance"] = high.rolling(
        window=20,
        min_periods=20,
    ).max()

    # ---------------------------------------------------------
    # Наклон EMA9
    # ---------------------------------------------------------

    df["ema9_slope"] = (
        df["ema_9"] - df["ema_9"].shift(3)
    )

    # ---------------------------------------------------------
    # Дополнительные показатели
    # ---------------------------------------------------------

    df["price_change"] = (
        close.pct_change()
    )

    df["volatility"] = (
        df["price_change"]
        .rolling(
            window=20,
            min_periods=20,
        )
        .std()
    )

    # ---------------------------------------------------------
    # Очистка
    # ---------------------------------------------------------

    df = df.replace(
        [np.inf, -np.inf],
        np.nan,
    )

    return df
