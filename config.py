import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


load_dotenv()


# ============================================================
# BASIC CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except (TypeError, ValueError):
    ADMIN_ID = 0


TWELVE_DATA_API_KEY = os.getenv(
    "TWELVE_DATA_API_KEY",
    "",
).strip()


PORT = int(
    os.getenv(
        "PORT",
        "10000",
    )
)


DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    "signal_bot.db",
).strip()


# ============================================================
# MARKET CONFIG
# ============================================================

CANDLE_INTERVAL = os.getenv(
    "CANDLE_INTERVAL",
    "5min",
).strip()


CANDLE_LIMIT = int(
    os.getenv(
        "CANDLE_LIMIT",
        "200",
    )
)


# ============================================================
# SIGNAL FILTERS
# ============================================================

MIN_QUALITY = float(
    os.getenv(
        "MIN_QUALITY",
        "85",
    )
)


MIN_PROBABILITY = float(
    os.getenv(
        "MIN_PROBABILITY",
        "75",
    )
)


AUTO_SCAN_SECONDS = int(
    os.getenv(
        "AUTO_SCAN_SECONDS",
        "60",
    )
)


# ============================================================
# TIMEZONE
# ============================================================

TIMEZONE = ZoneInfo(
    "Europe/Moscow"
)


# ============================================================
# PAIR DISCOVERY
# ============================================================

# PAIRS теперь являются FALLBACK.
#
# Основной список будет получаться динамически
# из актуальных доступных активов Pocket Option.
#
# Важно:
# Twelve Data используется как источник свечей.
# Поэтому из найденных Pocket Option активов будут
# отбираться те, которые реально можно получить
# через Twelve Data.

FALLBACK_PAIRS = [
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
    "AUD/JPY",
    "EUR/CHF",
    "EUR/AUD",
    "EUR/CAD",
    "EUR/NZD",
    "GBP/AUD",
    "GBP/CAD",
    "GBP/CHF",
    "GBP/NZD",
    "AUD/CAD",
    "AUD/CHF",
    "AUD/NZD",
    "CAD/CHF",
    "CAD/JPY",
    "CHF/JPY",
    "NZD/JPY",
]


# ------------------------------------------------------------
# Optional manual override
# ------------------------------------------------------------

# Если PAIRS задан в Render Environment:
#
# PAIRS=EUR/USD,GBP/USD,USD/JPY
#
# тогда используется именно этот список.
#
# Если PAIRS пустой — используется динамическое
# обнаружение доступных пар.

PAIRS_ENV = os.getenv(
    "PAIRS",
    "",
).strip()


if PAIRS_ENV:
    PAIRS = [
        pair.strip().upper()
        for pair in PAIRS_ENV.split(",")
        if pair.strip()
    ]
else:
    PAIRS = FALLBACK_PAIRS.copy()


# ============================================================
# POCKET OPTION ASSET DISCOVERY
# ============================================================

POCKET_OPTION_ASSETS_URL = os.getenv(
    "POCKET_OPTION_ASSETS_URL",
    "https://pocketoption.com/en/assets-current/",
).strip()


ASSET_DISCOVERY_ENABLED = (
    os.getenv(
        "ASSET_DISCOVERY_ENABLED",
        "true",
    ).strip().lower()
    in {
        "1",
        "true",
        "yes",
        "on",
    }
)


ASSET_DISCOVERY_CACHE_SECONDS = int(
    os.getenv(
        "ASSET_DISCOVERY_CACHE_SECONDS",
        "120",
    )
)


# ============================================================
# VALIDATION
# ============================================================

def validate_config() -> None:
    errors = []

    if not BOT_TOKEN:
        errors.append(
            "BOT_TOKEN не задан."
        )

    if not ADMIN_ID:
        errors.append(
            "ADMIN_ID не задан или некорректен."
        )

    if not TWELVE_DATA_API_KEY:
        errors.append(
            "TWELVE_DATA_API_KEY не задан."
        )

    if MIN_QUALITY < 0 or MIN_QUALITY > 100:
        errors.append(
            "MIN_QUALITY должен быть от 0 до 100."
        )

    if MIN_PROBABILITY < 0 or MIN_PROBABILITY > 100:
        errors.append(
            "MIN_PROBABILITY должен быть от 0 до 100."
        )

    if CANDLE_LIMIT < 80:
        errors.append(
            "CANDLE_LIMIT должен быть не меньше 80."
        )

    if not PAIRS:
        errors.append(
            "Не задан ни один fallback PAIRS."
        )

    if errors:
        raise RuntimeError(
            "\n".join(errors)
        )
