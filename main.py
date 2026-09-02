import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import (
    DefaultBotProperties,
)
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    Message,
)
from fastapi import FastAPI

from config import (
    ADMIN_ID,
    WEB_HOST,
    WEB_PORT,
)
from database import Database
from keyboards import (
    admin_request,
    main_menu,
)
from market import MarketClient
from scheduler import SignalScheduler
from signal_engine import SignalEngine


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger(
    "pocket_signal_bot"
)


# ============================================================
# OBJECTS
# ============================================================

bot = Bot(
    token=None,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    ),
)

# Token будет установлен ниже.
# Это сделано отдельно, чтобы избежать
# случайного дублирования Bot-объекта.
