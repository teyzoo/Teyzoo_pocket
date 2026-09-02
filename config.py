import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


load_dotenv()


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()

TIMEZONE = ZoneInfo("Europe/Moscow")

WEB_HOST = "0.0.0.0"
WEB_PORT = int(os.getenv("PORT", "10000"))


if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден. "
        "Добавь BOT_TOKEN в Environment Variables на Render."
    )


if not ADMIN_ID_RAW:
    raise RuntimeError(
        "ADMIN_ID не найден. "
        "Добавь ADMIN_ID в Environment Variables на Render."
    )


try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError as exc:
    raise RuntimeError(
        "ADMIN_ID должен быть числом."
    ) from exc
