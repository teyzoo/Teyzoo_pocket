import numpy as np
import pandas as pd


def calculate_indicators(
    dataframe: pd.DataFrame,
) -> pd.DataFrame:

    df = dataframe.copy()

    close = df["close"]
    high = df["high"]
    low = df["low"]

    # ========================================================
    # EMA
    # ========================================================

    df["ema_fast"] = close.ewm(
        span=9,
        adjust=False,
    ).mean()

    df["ema_slow"] = close.ewm(
        span=21,
        adjust=False,
    ).mean()

    df["ema_trend"] = close.ewm(
        span=50,
        adjust=False,
    ).mean()


    # ========================================================
    # RSI
    # ========================================================

    delta = close.diff()

    gain = delta.clip(
        lower=0
    )

    loss = -delta.clip(
        upper=0
    )

    average_gain = gain.ewm(
        alpha=1 / 14,
        adjust=False,
    ).mean()

    average_loss = loss.ewm(
        alpha=1 / 14,
        adjust=False,
    ).mean()

    rs = average_gain / average_loss.replace(
        0,
        np.nan,
    )

    df["rsi"] = (
        100
        - (
            100
            / (1 + rs)
        )
    )


    # ========================================================
    # MACD
    # ========================================================

    ema12 = close.ewm(
        span=12,
        adjust=False,
    ).mean()

    ema26 = close.ewm(
        span=26,
        adjust=False,
    ).mean()

    df["macd"] = ema12 - ema26

    df["macd_signal"] = df["macd"].ewm(
        span=9,
        adjust=False,
    ).mean()

    df["macd_hist"] = (
        df["macd"]
        - df["macd_signal"]
    )


    # ========================================================
    # BOLLINGER BANDS
    # ========================================================

    middle = close.rolling(
        window=20
    ).mean()

    std = close.rolling(
        window=20
    ).std()

    df["bb_middle"] = middle
    df["bb_upper"] = middle + 2 * std
    df["bb_lower"] = middle - 2 * std


    # ========================================================
    # STOCHASTIC
    # ========================================================

    lowest_low = low.rolling(
        window=14
    ).min()

    highest_high = high.rolling(
        window=14
    ).max()

    denominator = (
        highest_high
        - lowest_low
    ).replace(
        0,
        np.nan,
    )

    df["stoch_k"] = (
        (
            close
            - lowest_low
        )
        / denominator
        * 100
    )

    df["stoch_d"] = df[
        "stoch_k"
    ].rolling(
        window=3
    ).mean()


    # ========================================================
    # ATR
    # ========================================================

    previous_close = close.shift(1)

    tr1 = high - low

    tr2 = (
        high
        - previous_close
    ).abs()

    tr3 = (
        low
        - previous_close
    ).abs()

    true_range = pd.concat(
        [
            tr1,
            tr2,
            tr3,
        ],
        axis=1,
    ).max(
        axis=1
    )

    df["atr"] = true_range.rolling(
        window=14
    ).mean()


    # ========================================================
    # CANDLE
    # ========================================================

    df["body"] = (
        df["close"]
        - df["open"]
    )

    df["range"] = (
        df["high"]
        - df["low"]
    )

    df["body_ratio"] = (
        df["body"].abs()
        / df["range"].replace(
            0,
            np.nan,
        )
    )


    # ========================================================
    # SUPPORT / RESISTANCE
    # ========================================================

    df["support"] = low.rolling(
        window=20
    ).min()

    df["resistance"] = high.rolling(
        window=20
    ).max()


    return df
