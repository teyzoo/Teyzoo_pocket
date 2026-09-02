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

TIMEZONE = ZoneInfo("Europe/Moscow")

PORT = int(os.getenv("PORT", "10000"))
HOST = "0.0.0.0"

DATABASE_PATH = os.getenv("DATABASE_PATH", "signal_bot.db")

CANDLE_INTERVAL = os.getenv("CANDLE_INTERVAL", "5min")
CANDLE_LIMIT = int(os.getenv("CANDLE_LIMIT", "200"))

MIN_QUALITY = float(os.getenv("MIN_QUALITY", "85"))
AUTO_SCAN_SECONDS = int(os.getenv("AUTO_SCAN_SECONDS", "60"))

DEFAULT_PAIRS = [
    "EUR/USD",
    "GBP/USD",
    "USD/JPY",
    "USD/CHF",
    "EUR/GBP",
]

pairs_env = os.getenv("PAIRS", "").strip()

if pairs_env:
    PAIRS = [
        pair.strip()
        for pair in pairs_env.split(",")
        if pair.strip()
    ]
else:
    PAIRS = DEFAULT_PAIRS


def validate_config() -> list[str]:
    errors = []

    if not BOT_TOKEN:
        errors.append("BOT_TOKEN не задан")

    if ADMIN_ID <= 0:
        errors.append("ADMIN_ID не задан или некорректный")

    if not TWELVE_DATA_API_KEY:
        errors.append("TWELVE_DATA_API_KEY не задан")

    if not PAIRS:
        errors.append("Список PAIRS пуст")

    return errors
