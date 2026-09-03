from __future__ import annotations

import os
from typing import Final


def get_env(
    name: str,
    default: str | None = None,
) -> str | None:
    value = os.getenv(name)

    if value is None:
        return default

    value = value.strip()

    return value if value else default


def get_required_env(name: str) -> str:
    value = get_env(name)

    if not value:
        raise RuntimeError(
            f"Environment variable {name} is required."
        )

    return value


def get_int_env(
    name: str,
    default: int,
) -> int:
    value = get_env(
        name,
        str(default),
    )

    try:
        return int(value or default)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Environment variable {name} must be an integer."
        ) from exc


def get_float_env(
    name: str,
    default: float,
) -> float:
    value = get_env(
        name,
        str(default),
    )

    try:
        return float(value or default)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Environment variable {name} must be a number."
        ) from exc


def get_bool_env(
    name: str,
    default: bool,
) -> bool:
    value = get_env(
        name,
        "true" if default else "false",
    )

    if value is None:
        return default

    normalized = value.lower()

    if normalized in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }:
        return True

    if normalized in {
        "0",
        "false",
        "no",
        "n",
        "off",
    }:
        return False

    raise RuntimeError(
        f"Environment variable {name} must be a boolean."
    )


# =========================================================
# TELEGRAM
# =========================================================

BOT_TOKEN: Final[str] = get_required_env(
    "BOT_TOKEN"
)


# =========================================================
# DATABASE
# =========================================================

DATABASE_URL: Final[str] = get_required_env(
    "DATABASE_URL"
)


# =========================================================
# OWNER / ADMINS
# =========================================================

OWNER_ID: Final[int] = get_int_env(
    "OWNER_ID",
    0,
)

ADMIN_IDS_RAW: Final[str] = (
    get_env(
        "ADMIN_IDS",
        "",
    )
    or ""
)


def get_admin_ids() -> set[int]:
    result: set[int] = set()

    if OWNER_ID > 0:
        result.add(OWNER_ID)

    for value in ADMIN_IDS_RAW.split(","):
        value = value.strip()

        if not value:
            continue

        try:
            result.add(int(value))
        except ValueError:
            continue

    return result


ADMIN_IDS: Final[set[int]] = get_admin_ids()


def is_admin(
    telegram_id: int,
) -> bool:
    return telegram_id in ADMIN_IDS


def is_owner(
    telegram_id: int,
) -> bool:
    return (
        OWNER_ID > 0
        and telegram_id == OWNER_ID
    )


# =========================================================
# FASTAPI / RENDER
# =========================================================

HOST: Final[str] = (
    get_env(
        "HOST",
        "0.0.0.0",
    )
    or "0.0.0.0"
)

PORT: Final[int] = get_int_env(
    "PORT",
    10000,
)


# =========================================================
# MARKET API
# =========================================================

MARKET_API_URL: Final[str] = get_required_env(
    "MARKET_API_URL"
)

MARKET_API_KEY: Final[str | None] = get_env(
    "MARKET_API_KEY"
)

MARKET_REQUEST_TIMEOUT: Final[int] = get_int_env(
    "MARKET_REQUEST_TIMEOUT",
    15,
)

MARKET_CANDLE_LIMIT: Final[int] = get_int_env(
    "MARKET_CANDLE_LIMIT",
    200,
)


# =========================================================
# SIGNAL SETTINGS
# =========================================================

# Минимальный Quality Score.
SIGNAL_MINIMUM_QUALITY: Final[float] = (
    get_float_env(
        "SIGNAL_MINIMUM_QUALITY",
        75.0,
    )
)

# Минимальная историческая вероятность.
SIGNAL_MINIMUM_PROBABILITY: Final[float] = (
    get_float_env(
        "SIGNAL_MINIMUM_PROBABILITY",
        70.0,
    )
)

# Не блокируем сигнал только из-за отсутствия
# накопленной исторической статистики.
SIGNAL_REQUIRE_HISTORICAL_PROBABILITY: Final[bool] = (
    get_bool_env(
        "SIGNAL_REQUIRE_HISTORICAL_PROBABILITY",
        False,
    )
)

# Предупреждение перед закрытием.
SIGNAL_WARNING_MINUTES: Final[int] = get_int_env(
    "SIGNAL_WARNING_MINUTES",
    2,
)

# Основное время экспирации.
SIGNAL_EXPIRY_MINUTES: Final[int] = get_int_env(
    "SIGNAL_EXPIRY_MINUTES",
    5,
)

# Разрешённый диапазон экспирации:
# от 1 до 20 минут.
SIGNAL_MIN_EXPIRY_MINUTES: Final[int] = get_int_env(
    "SIGNAL_MIN_EXPIRY_MINUTES",
    1,
)

SIGNAL_MAX_EXPIRY_MINUTES: Final[int] = get_int_env(
    "SIGNAL_MAX_EXPIRY_MINUTES",
    20,
)


# =========================================================
# SCANNER
# =========================================================

# Автоматический анализ каждую минуту.
SIGNAL_SCAN_INTERVAL: Final[int] = get_int_env(
    "SIGNAL_SCAN_INTERVAL",
    60,
)

SIGNAL_CANDLE_LIMIT: Final[int] = get_int_env(
    "SIGNAL_CANDLE_LIMIT",
    MARKET_CANDLE_LIMIT,
)

# Сколько Forex-пар анализировать за цикл.
SIGNAL_PAIRS_PER_CYCLE: Final[int] = get_int_env(
    "SIGNAL_PAIRS_PER_CYCLE",
    4,
)

# Защита от повторной отправки одного сигнала.
SIGNAL_COOLDOWN: Final[int] = get_int_env(
    "SIGNAL_COOLDOWN",
    300,
)


# =========================================================
# SCHEDULER
# =========================================================

SIGNAL_ANALYSIS_INTERVAL: Final[int] = get_int_env(
    "SIGNAL_ANALYSIS_INTERVAL",
    20,
)

SIGNAL_WARNING_INTERVAL: Final[int] = get_int_env(
    "SIGNAL_WARNING_INTERVAL",
    15,
)

SIGNAL_RESULT_INTERVAL: Final[int] = get_int_env(
    "SIGNAL_RESULT_INTERVAL",
    30,
)


# =========================================================
# TIMEFRAMES
# =========================================================

TIMEFRAMES: Final[tuple[str, ...]] = (
    "1m",
    "5m",
    "15m",
)


# =========================================================
# MARKET PAIRS
# =========================================================

DEFAULT_PAIRS: Final[list[str]] = [
    "EUR/USD",
    "GBP/USD",
    "USD/JPY",
    "USD/CHF",
    "AUD/USD",
    "USD/CAD",
    "NZD/USD",
    "EUR/GBP",
    "EUR/JPY",
    "GBP/JPY",
]


def get_market_symbols() -> list[str]:
    raw = get_env(
        "MARKET_SYMBOLS",
        "",
    )

    if not raw:
        return list(DEFAULT_PAIRS)

    result: list[str] = []

    for item in raw.split(","):
        item = item.strip()

        if item:
            result.append(item)

    return result or list(DEFAULT_PAIRS)


# =========================================================
# APPLICATION
# =========================================================

APPLICATION_MAX_LENGTH: Final[int] = get_int_env(
    "APPLICATION_MAX_LENGTH",
    4000,
)


# =========================================================
# LOGGING
# =========================================================

LOG_LEVEL: Final[str] = (
    get_env(
        "LOG_LEVEL",
        "INFO",
    )
    or "INFO"
)


# =========================================================
# IMPORTANT:
# signal_scanner.py сейчас читает значения напрямую
# через os.getenv().
#
# Поэтому устанавливаем безопасные значения по умолчанию
# в окружение, чтобы старый scanner тоже получил новые
# настройки, даже если переменные Render не заданы.
# =========================================================

os.environ.setdefault(
    "SIGNAL_MINIMUM_QUALITY",
    str(SIGNAL_MINIMUM_QUALITY),
)

os.environ.setdefault(
    "SIGNAL_SCAN_INTERVAL",
    str(SIGNAL_SCAN_INTERVAL),
)

os.environ.setdefault(
    "SIGNAL_CANDLE_LIMIT",
    str(SIGNAL_CANDLE_LIMIT),
)

os.environ.setdefault(
    "SIGNAL_PAIRS_PER_CYCLE",
    str(SIGNAL_PAIRS_PER_CYCLE),
)

os.environ.setdefault(
    "SIGNAL_COOLDOWN",
    str(SIGNAL_COOLDOWN),
)

os.environ.setdefault(
    "SIGNAL_TIMEFRAMES",
    ",".join(TIMEFRAMES),
)


# =========================================================
# VALIDATION
# =========================================================

def validate_config() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is not configured."
        )

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not configured."
        )

    if not MARKET_API_URL:
        raise RuntimeError(
            "MARKET_API_URL is not configured."
        )

    if OWNER_ID <= 0:
        raise RuntimeError(
            "OWNER_ID must be configured."
        )

    if not 1 <= PORT <= 65535:
        raise RuntimeError(
            "PORT must be between 1 and 65535."
        )

    if MARKET_REQUEST_TIMEOUT <= 0:
        raise RuntimeError(
            "MARKET_REQUEST_TIMEOUT must be greater than 0."
        )

    if not 20 <= MARKET_CANDLE_LIMIT <= 5000:
        raise RuntimeError(
            "MARKET_CANDLE_LIMIT must be between 20 and 5000."
        )

    if not 0 <= SIGNAL_MINIMUM_QUALITY <= 100:
        raise RuntimeError(
            "SIGNAL_MINIMUM_QUALITY must be between 0 and 100."
        )

    if not 0 <= SIGNAL_MINIMUM_PROBABILITY <= 100:
        raise RuntimeError(
            "SIGNAL_MINIMUM_PROBABILITY must be between 0 and 100."
        )

    if SIGNAL_WARNING_MINUTES < 0:
        raise RuntimeError(
            "SIGNAL_WARNING_MINUTES cannot be negative."
        )

    if not (
        SIGNAL_MIN_EXPIRY_MINUTES
        <= SIGNAL_EXPIRY_MINUTES
        <= SIGNAL_MAX_EXPIRY_MINUTES
    ):
        raise RuntimeError(
            "SIGNAL_EXPIRY_MINUTES must be within "
            "SIGNAL_MIN_EXPIRY_MINUTES and "
            "SIGNAL_MAX_EXPIRY_MINUTES."
        )

    if SIGNAL_MIN_EXPIRY_MINUTES < 1:
        raise RuntimeError(
            "SIGNAL_MIN_EXPIRY_MINUTES must be >= 1."
        )

    if SIGNAL_MAX_EXPIRY_MINUTES > 20:
        raise RuntimeError(
            "SIGNAL_MAX_EXPIRY_MINUTES must be <= 20."
        )

    if SIGNAL_SCAN_INTERVAL <= 0:
        raise RuntimeError(
            "SIGNAL_SCAN_INTERVAL must be greater than 0."
        )

    if SIGNAL_PAIRS_PER_CYCLE <= 0:
        raise RuntimeError(
            "SIGNAL_PAIRS_PER_CYCLE must be greater than 0."
        )

    if SIGNAL_COOLDOWN < 0:
        raise RuntimeError(
            "SIGNAL_COOLDOWN cannot be negative."
        )

    if not TIMEFRAMES:
        raise RuntimeError(
            "TIMEFRAMES cannot be empty."
        )

    if not DEFAULT_PAIRS:
        raise RuntimeError(
            "DEFAULT_PAIRS cannot be empty."
        )


validate_config()


__all__ = [
    "BOT_TOKEN",
    "DATABASE_URL",
    "OWNER_ID",
    "ADMIN_IDS",
    "is_admin",
    "is_owner",
    "HOST",
    "PORT",
    "MARKET_API_URL",
    "MARKET_API_KEY",
    "MARKET_REQUEST_TIMEOUT",
    "MARKET_CANDLE_LIMIT",
    "SIGNAL_MINIMUM_QUALITY",
    "SIGNAL_MINIMUM_PROBABILITY",
    "SIGNAL_REQUIRE_HISTORICAL_PROBABILITY",
    "SIGNAL_WARNING_MINUTES",
    "SIGNAL_EXPIRY_MINUTES",
    "SIGNAL_MIN_EXPIRY_MINUTES",
    "SIGNAL_MAX_EXPIRY_MINUTES",
    "SIGNAL_SCAN_INTERVAL",
    "SIGNAL_CANDLE_LIMIT",
    "SIGNAL_PAIRS_PER_CYCLE",
    "SIGNAL_COOLDOWN",
    "SIGNAL_ANALYSIS_INTERVAL",
    "SIGNAL_WARNING_INTERVAL",
    "SIGNAL_RESULT_INTERVAL",
    "TIMEFRAMES",
    "DEFAULT_PAIRS",
    "get_market_symbols",
    "APPLICATION_MAX_LENGTH",
    "LOG_LEVEL",
    "get_env",
    "get_required_env",
    "get_int_env",
    "get_float_env",
    "get_bool_env",
    "validate_config",
]
