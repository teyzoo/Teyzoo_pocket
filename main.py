import asyncio
import logging
from datetime import datetime
from typing import Dict

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from fastapi import FastAPI

from config import ADMIN_ID, BOT_TOKEN


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)


bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    ),
)

dp = Dispatcher()

app = FastAPI(
    title="Pocket Signal Bot",
)


# ============================================================
# USERS
# ============================================================

# Пока храним в памяти.
# Базу подключим следующим этапом.
users: Dict[int, str] = {}


# ============================================================
# KEYBOARDS
# ============================================================

def admin_request_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить",
                    callback_data=f"approve:{user_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"reject:{user_id}",
                ),
            ]
        ]
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔎 Запросить сигнал",
                    callback_data="request_signal",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🤖 Авто-сигналы",
                    callback_data="auto_signals",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Статистика",
                    callback_data="statistics",
                )
            ],
        ]
    )


# ============================================================
# START
# ============================================================

@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    user_id = message.from_user.id

    status = users.get(user_id)

    if status == "approved":
        await message.answer(
            "🤖 <b>Pocket Signal Bot</b>\n\n"
            "Добро пожаловать обратно.\n\n"
            "Выберите действие:",
            reply_markup=main_menu_keyboard(),
        )
        return

    if status == "pending":
        await message.answer(
            "⏳ <b>Заявка уже отправлена.</b>\n\n"
            "Ожидайте решения администратора."
        )
        return

    if status == "rejected":
        await message.answer(
            "❌ <b>Ваша заявка была отклонена.</b>"
        )
        return

    users[user_id] = "pending"

    user = message.from_user

    username = (
        f"@{user.username}"
        if user.username
        else "отсутствует"
    )

    full_name = user.full_name or "Не указано"

    admin_text = (
        "🔔 <b>НОВАЯ ЗАЯВКА</b>\n\n"
        f"👤 Имя: {full_name}\n"
        f"🔗 Username: {username}\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"🕐 Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    try:
        await bot.send_message(
            ADMIN_ID,
            admin_text,
            reply_markup=admin_request_keyboard(user_id),
        )
    except Exception:
        logger.exception(
            "Не удалось отправить заявку администратору."
        )

    await message.answer(
        "🔒 <b>Доступ закрыт</b>\n\n"
        "Ваша заявка отправлена администратору.\n\n"
        "После одобрения вы получите доступ к боту."
    )


# ============================================================
# APPROVE
# ============================================================

@dp.callback_query(F.data.startswith("approve:"))
async def approve_handler(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "⛔ У вас нет доступа.",
            show_alert=True,
        )
        return

    try:
        user_id = int(
            callback.data.split(":", 1)[1]
        )
    except (ValueError, AttributeError):
        await callback.answer(
            "Ошибка заявки.",
            show_alert=True,
        )
        return

    users[user_id] = "approved"

    try:
        await bot.send_message(
            user_id,
            "✅ <b>Ваша заявка одобрена!</b>\n\n"
            "Теперь вам доступен бот.",
            reply_markup=main_menu_keyboard(),
        )
    except Exception:
        logger.exception(
            "Не удалось уведомить пользователя %s.",
            user_id,
        )

    await callback.message.edit_reply_markup(
        reply_markup=None
    )

    await callback.message.answer(
        f"✅ Пользователь <code>{user_id}</code> одобрен."
    )

    await callback.answer("Пользователь одобрен.")


# ============================================================
# REJECT
# ============================================================

@dp.callback_query(F.data.startswith("reject:"))
async def reject_handler(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "⛔ У вас нет доступа.",
            show_alert=True,
        )
        return

    try:
        user_id = int(
            callback.data.split(":", 1)[1]
        )
    except (ValueError, AttributeError):
        await callback.answer(
            "Ошибка заявки.",
            show_alert=True,
        )
        return

    users[user_id] = "rejected"

    try:
        await bot.send_message(
            user_id,
            "❌ <b>Ваша заявка отклонена.</b>",
        )
    except Exception:
        logger.exception(
            "Не удалось уведомить пользователя %s.",
            user_id,
        )

    await callback.message.edit_reply_markup(
        reply_markup=None
    )

    await callback.message.answer(
        f"❌ Пользователь <code>{user_id}</code> отклонён."
    )

    await callback.answer("Заявка отклонена.")


# ============================================================
# BUTTON PROTECTION
# ============================================================

@dp.callback_query()
async def protected_callbacks(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id

    if users.get(user_id) != "approved":
        await callback.answer(
            "🔒 Доступ ещё не одобрен.",
            show_alert=True,
        )
        return

    await callback.answer(
        "⏳ Функция будет подключена следующим этапом."
    )


# ============================================================
# FASTAPI
# ============================================================

@app.get("/")
async def root() -> dict:
    return {
        "status": "ok",
        "service": "pocket-signal-bot",
    }


@app.get("/health")
async def health() -> dict:
    return {
        "status": "healthy",
    }


# ============================================================
# BOT
# ============================================================

async def run_bot() -> None:
    logger.info("Telegram bot starting...")

    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
    )


async def main() -> None:
    await run_bot()


if __name__ == "__main__":
    asyncio.run(main())
