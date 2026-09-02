import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()

PORT = int(os.getenv("PORT", "10000"))

DATABASE_PATH = os.getenv("DATABASE_PATH", "signal_bot.db")

CANDLE_INTERVAL = os.getenv("CANDLE_INTERVAL", "5min")
CANDLE_LIMIT = int(os.getenv("CANDLE_LIMIT", "200"))

# Минимальное качество технического сигнала
MIN_QUALITY = int(os.getenv("MIN_QUALITY", "85"))

# Минимальный расчётный исторический шанс
MIN_PROBABILITY = float(os.getenv("MIN_PROBABILITY", "75"))

AUTO_SCAN_SECONDS = int(os.getenv("AUTO_SCAN_SECONDS", "60"))

TIMEZONE = ZoneInfo("Europe/Moscow")

DEFAULT_PAIRS = [
    "EUR/USD",
    "GBP/USD",
    "USD/JPY",
    "USD/CHF",
    "EUR/GBP",
]

PAIRS_ENV = os.getenv("PAIRS", "").strip()

if PAIRS_ENV:
    PAIRS = [
        pair.strip()
        for pair in PAIRS_ENV.split(",")
        if pair.strip()
    ]
else:
    PAIRS = DEFAULT_PAIRS


def validate_config() -> list[str]:
    errors = []

    if not BOT_TOKEN:
        errors.append("BOT_TOKEN не задан")

    if not ADMIN_ID:
        errors.append("ADMIN_ID не задан")

    if not TWELVE_DATA_API_KEY:
        errors.append("TWELVE_DATA_API_KEY не задан")

    if not PAIRS:
        errors.append("Список PAIRS пуст")

    if MIN_PROBABILITY < 0 or MIN_PROBABILITY > 100:
        errors.append("MIN_PROBABILITY должен быть от 0 до 100")

    if MIN_QUALITY < 0 or MIN_QUALITY > 100:
        errors.append("MIN_QUALITY должен быть от 0 до 100")

    return errors
