import numpy as np
import pandas as pd


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    data = df.copy()

    close = data["close"]
    high = data["high"]
    low = data["low"]

    data["ema_9"] = close.ewm(
        span=9,
        adjust=False,
    ).mean()

    data["ema_21"] = close.ewm(
        span=21,
        adjust=False,
    ).mean()

    data["ema_50"] = close.ewm(
        span=50,
        adjust=False,
    ).mean()

    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

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

    rs = avg_gain / avg_loss.replace(0, np.nan)

    data["rsi"] = 100 - (
        100 / (1 + rs)
    )

    ema_12 = close.ewm(
        span=12,
        adjust=False,
    ).mean()

    ema_26 = close.ewm(
        span=26,
        adjust=False,
    ).mean()

    data["macd"] = ema_12 - ema_26

    data["macd_signal"] = data["macd"].ewm(
        span=9,
        adjust=False,
    ).mean()

    data["macd_hist"] = (
        data["macd"] -
        data["macd_signal"]
    )

    data["bb_middle"] = close.rolling(
        20
    ).mean()

    bb_std = close.rolling(20).std()

    data["bb_upper"] = (
        data["bb_middle"] +
        2 * bb_std
    )

    data["bb_lower"] = (
        data["bb_middle"] -
        2 * bb_std
    )

    lowest_low = low.rolling(14).min()
    highest_high = high.rolling(14).max()

    denominator = (
        highest_high - lowest_low
    ).replace(0, np.nan)

    data["stoch_k"] = (
        (close - lowest_low) /
        denominator
    ) * 100

    data["stoch_d"] = data["stoch_k"].rolling(
        3
    ).mean()

    previous_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - previous_close).abs()
    tr3 = (low - previous_close).abs()

    true_range = pd.concat(
        [tr1, tr2, tr3],
        axis=1,
    ).max(axis=1)

    data["atr"] = true_range.rolling(
        14
    ).mean()

    data["candle_body"] = (
        close - data["open"]
    ).abs()

    data["candle_range"] = (
        high - low
    )

    data["body_ratio"] = (
        data["candle_body"] /
        data["candle_range"].replace(0, np.nan)
    )

    data["support"] = low.rolling(
        20
    ).min()

    data["resistance"] = high.rolling(
        20
    ).max()

    data["ema_9_slope"] = (
        data["ema_9"] -
        data["ema_9"].shift(3)
    )

    return data.replace(
        [np.inf, -np.inf],
        np.nan,
    )
