from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Расчёт технических индикаторов для SignalEngine.

    Вход:
        DataFrame:
        datetime, open, high, low, close, volume

    Выход:
        Исходный DataFrame + все необходимые индикаторы.

    ВАЖНО:
        Сохраняются старые названия индикаторов:
            ema_9
            ema_21
            ema_50
            macd_histogram
            ema9_slope

        И добавляются совместимые названия:
            ema_fast
            ema_slow
            macd_hist
            ema_fast_slope

    Это позволяет SignalEngine работать без изменения
    существующей логики проекта.
    """

    df = df.copy()

    # =========================================================
    # 1. Проверка входных данных
    # =========================================================

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

    # Volume может отсутствовать у некоторых источников.
    # Для расчёта текущих индикаторов он не обязателен.
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(
            df["volume"],
            errors="coerce",
        )

    df = df.dropna(
        subset=required
    ).copy()

    if len(df) < 50:
        return df

    # =========================================================
    # 2. Базовые серии
    # =========================================================

    close = df["close"]
    high = df["high"]
    low = df["low"]
    open_price = df["open"]

    # =========================================================
    # 3. EMA
    # =========================================================

    # Быстрая EMA = 9
    df["ema_9"] = close.ewm(
        span=9,
        adjust=False,
    ).mean()

    # Медленная EMA = 21
    df["ema_21"] = close.ewm(
        span=21,
        adjust=False,
    ).mean()

    # Долгосрочная EMA = 50
    df["ema_50"] = close.ewm(
        span=50,
        adjust=False,
    ).mean()

    # ---------------------------------------------------------
    # Совместимые имена для SignalEngine
    # ---------------------------------------------------------

    df["ema_fast"] = df["ema_9"]
    df["ema_slow"] = df["ema_21"]

    # =========================================================
    # 4. RSI 14
    # =========================================================

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

    # Не заменяем нулевой loss на NaN навсегда:
    # если цена росла без единого снижения, RSI должен
    # корректно приближаться к 100.
    rs = avg_gain / avg_loss.replace(
        0,
        np.nan,
    )

    rsi = 100 - (
        100 / (1 + rs)
    )

    # Обработка крайних случаев:
    # постоянный рост -> RSI 100
    # постоянное падение -> RSI 0
    rsi = rsi.where(
        ~((avg_loss == 0) & (avg_gain > 0)),
        100.0,
    )

    rsi = rsi.where(
        ~((avg_gain == 0) & (avg_loss > 0)),
        0.0,
    )

    df["rsi"] = rsi

    # =========================================================
    # 5. MACD
    # =========================================================

    ema_12 = close.ewm(
        span=12,
        adjust=False,
    ).mean()

    ema_26 = close.ewm(
        span=26,
        adjust=False,
    ).mean()

    df["macd"] = (
        ema_12 - ema_26
    )

    df["macd_signal"] = df["macd"].ewm(
        span=9,
        adjust=False,
    ).mean()

    # Старое имя сохраняем
    df["macd_histogram"] = (
        df["macd"] - df["macd_signal"]
    )

    # Новое имя, которое требует SignalEngine
    df["macd_hist"] = df["macd_histogram"]

    # =========================================================
    # 6. Bollinger Bands
    # =========================================================

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

    # =========================================================
    # 7. Stochastic
    # =========================================================

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

    # Ограничиваем диапазон из-за возможных
    # небольших ошибок плавающей точки.
    df["stoch_k"] = df["stoch_k"].clip(
        lower=0,
        upper=100,
    )

    df["stoch_d"] = df["stoch_d"].clip(
        lower=0,
        upper=100,
    )

    # =========================================================
    # 8. ATR 14
    # =========================================================

    previous_close = close.shift(1)

    true_range_1 = (
        high - low
    )

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

    # =========================================================
    # 9. Параметры свечи
    # =========================================================

    df["candle_body"] = (
        close - open_price
    ).abs()

    df["candle_range"] = (
        high - low
    )

    df["body_ratio"] = np.where(
        df["candle_range"] > 0,
        df["candle_body"]
        / df["candle_range"],
        0.0,
    )

    df["body_ratio"] = pd.to_numeric(
        df["body_ratio"],
        errors="coerce",
    )

    df["body_ratio"] = df["body_ratio"].clip(
        lower=0,
        upper=1,
    )

    # =========================================================
    # 10. Support / Resistance
    # =========================================================

    df["support"] = low.rolling(
        window=20,
        min_periods=20,
    ).min()

    df["resistance"] = high.rolling(
        window=20,
        min_periods=20,
    ).max()

    # =========================================================
    # 11. Наклон быстрой EMA
    # =========================================================

    df["ema9_slope"] = (
        df["ema_9"]
        - df["ema_9"].shift(3)
    )

    # Совместимое имя для SignalEngine
    df["ema_fast_slope"] = (
        df["ema_fast"]
        - df["ema_fast"].shift(3)
    )

    # =========================================================
    # 12. Изменение цены
    # =========================================================

    df["price_change"] = (
        close.pct_change()
    )

    # =========================================================
    # 13. Волатильность
    # =========================================================

    df["volatility"] = (
        df["price_change"]
        .rolling(
            window=20,
            min_periods=20,
        )
        .std()
    )

    # =========================================================
    # 14. Дополнительные полезные данные
    # =========================================================

    # Направление свечи.
    df["candle_direction"] = np.where(
        close > open_price,
        1,
        np.where(
            close < open_price,
            -1,
            0,
        ),
    )

    # Размер свечи относительно цены.
    df["range_ratio"] = np.where(
        close != 0,
        df["candle_range"] / close,
        0.0,
    )

    # Расстояние цены от поддержки.
    df["support_distance"] = np.where(
        close != 0,
        (close - df["support"]) / close,
        np.nan,
    )

    # Расстояние цены от сопротивления.
    df["resistance_distance"] = np.where(
        close != 0,
        (df["resistance"] - close) / close,
        np.nan,
    )

    # =========================================================
    # 15. Очистка inf
    # =========================================================

    df = df.replace(
        [np.inf, -np.inf],
        np.nan,
    )

    # =========================================================
    # 16. Контроль обязательных индикаторов
    # =========================================================

    engine_required = [
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

    # Создаём отсутствующие колонки только в случае
    # непредвидённой проблемы, чтобы SignalEngine никогда
    # не получал KeyError.
    for column in engine_required:
        if column not in df.columns:
            df[column] = np.nan

    return df
