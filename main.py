import asyncio
import logging
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    Message,
)
from fastapi import FastAPI

from config import (
    ADMIN_ID,
    BOT_TOKEN,
    validate_config,
)
from database import db
from keyboards import (
    admin_request_keyboard,
    main_keyboard,
    pending_keyboard,
)
from market import market_client
from scheduler import SignalScheduler


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger(__name__)

errors = validate_config()

if errors:
    for error in errors:
        logger.error(error)


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

signal_scheduler = SignalScheduler(bot)


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def get_user_status(user_id: int) -> str | None:
    user = db.get_user(user_id)

    if not user:
        return None

    return user["status"]


async def notify_admin_new_user(
    user_id: int,
    username: str | None,
    first_name: str | None,
):
    if not ADMIN_ID:
        return

    username_text = (
        f"@{username}"
        if username
        else "нет username"
    )

    text = (
        "🔔 <b>Новая заявка на доступ</b>\n\n"
        f"👤 Имя: <b>{first_name or '—'}</b>\n"
        f"🔗 Username: <b>{username_text}</b>\n"
        f"🆔 ID: <code>{user_id}</code>\n\n"
        "Выберите действие:"
    )

    try:
        await bot.send_message(
            ADMIN_ID,
            text,
            parse_mode="HTML",
            reply_markup=admin_request_keyboard(
                user_id
            ),
        )
    except Exception:
        logger.exception(
            "Не удалось уведомить администратора"
        )


@dp.message(CommandStart())
async def start_handler(message: Message):
    if not message.from_user:
        return

    user_id = message.from_user.id

    if is_admin(user_id):
        await message.answer(
            "👑 <b>Админ-панель</b>\n\n"
            "Ты имеешь полный доступ к сигналам.",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    existing = db.get_user(user_id)

    if existing:
        status = existing["status"]

        if status == "APPROVED":
            await message.answer(
                "✅ <b>Доступ подтверждён.</b>\n\n"
                "Можно получать сигналы.",
                parse_mode="HTML",
                reply_markup=main_keyboard(),
            )
            return

        if status == "REJECTED":
            await message.answer(
                "❌ <b>Доступ отклонён.</b>\n\n"
                "Обратитесь к администратору.",
                parse_mode="HTML",
            )
            return

        await message.answer(
            "⏳ <b>Заявка уже отправлена.</b>\n\n"
            "Ожидайте решения администратора.",
            parse_mode="HTML",
            reply_markup=pending_keyboard(),
        )
        return

    user = db.create_or_update_user(
        user_id=user_id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )

    await message.answer(
        "⏳ <b>Заявка отправлена.</b>\n\n"
        "Доступ к сигналам пока закрыт.\n"
        "Администратор должен одобрить твою заявку.",
        parse_mode="HTML",
        reply_markup=pending_keyboard(),
    )

    await notify_admin_new_user(
        user_id=user_id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )


@dp.callback_query(
    F.data == "check_access"
)
async def check_access_handler(
    callback: CallbackQuery,
):
    user_id = callback.from_user.id

    if is_admin(user_id):
        await callback.message.edit_text(
            "👑 Администратор имеет доступ."
        )
        await callback.answer()
        return

    status = get_user_status(user_id)

    if status == "APPROVED":
        await callback.message.edit_text(
            "✅ <b>Доступ подтверждён.</b>\n\n"
            "Теперь можно получать сигналы.",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
    elif status == "REJECTED":
        await callback.message.edit_text(
            "❌ <b>Доступ отклонён.</b>",
            parse_mode="HTML",
        )
    else:
        await callback.answer(
            "Заявка ещё не рассмотрена.",
            show_alert=True,
        )
        return

    await callback.answer()


@dp.callback_query(
    F.data.startswith("approve:")
)
async def approve_handler(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )
        return

    try:
        user_id = int(
            callback.data.split(":", 1)[1]
        )
    except (
        ValueError,
        AttributeError,
    ):
        await callback.answer(
            "Некорректный ID.",
            show_alert=True,
        )
        return

    db.set_status(
        user_id,
        "APPROVED",
    )

    await callback.message.edit_text(
        callback.message.text +
        "\n\n✅ <b>ОДОБРЕНО</b>",
        parse_mode="HTML",
    )

    try:
        await bot.send_message(
            user_id,
            "🎉 <b>Доступ одобрен!</b>\n\n"
            "Теперь ты можешь получать сигналы.",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
    except Exception:
        logger.exception(
            "Не удалось уведомить пользователя"
        )

    await callback.answer(
        "Пользователь одобрен."
    )


@dp.callback_query(
    F.data.startswith("reject:")
)
async def reject_handler(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )
        return

    try:
        user_id = int(
            callback.data.split(":", 1)[1]
        )
    except (
        ValueError,
        AttributeError,
    ):
        await callback.answer(
            "Некорректный ID.",
            show_alert=True,
        )
        return

    db.set_status(
        user_id,
        "REJECTED",
    )

    await callback.message.edit_text(
        callback.message.text +
        "\n\n❌ <b>ОТКЛОНЕНО</b>",
        parse_mode="HTML",
    )

    try:
        await bot.send_message(
            user_id,
            "❌ <b>Доступ отклонён.</b>\n\n"
            "Обратитесь к администратору.",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception(
            "Не удалось уведомить пользователя"
        )

    await callback.answer(
        "Пользователь отклонён."
    )


@dp.callback_query(
    F.data == "request_signal"
)
async def request_signal_handler(
    callback: CallbackQuery,
):
    user_id = callback.from_user.id

    if (
        not is_admin(user_id)
        and get_user_status(user_id) != "APPROVED"
    ):
        await callback.answer(
            "Сначала получите доступ.",
            show_alert=True,
        )
        return

    await callback.answer(
        "🔎 Анализирую рынок..."
    )

    try:
        signal = await signal_scheduler.find_best_signal()

        if signal is None:
            await callback.message.answer(
                "⚪ <b>Сильного сигнала сейчас нет.</b>\n\n"
                "Я не буду выдавать слабый сигнал "
                "только ради того, чтобы что-то показать.",
                parse_mode="HTML",
            )
            return

        await signal_scheduler.save_signal(
            signal
        )

        await callback.message.answer(
            signal_scheduler.format_signal(
                signal
            ),
            parse_mode="HTML",
        )

    except Exception:
        logger.exception(
            "Ошибка ручного запроса сигнала"
        )

        await callback.message.answer(
            "⚠️ Не удалось получить рыночные данные.\n"
            "Попробуйте ещё раз позже."
        )


@dp.callback_query(
    F.data == "history"
)
async def history_handler(
    callback: CallbackQuery,
):
    user_id = callback.from_user.id

    if (
        not is_admin(user_id)
        and get_user_status(user_id) != "APPROVED"
    ):
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )
        return

    signals = db.get_recent_signals(10)

    if not signals:
        await callback.message.answer(
            "📊 История пока пустая."
        )
        await callback.answer()
        return

    lines = [
        "📊 <b>Последние сигналы</b>\n"
    ]

    for signal in signals:
        direction = signal["direction"]

        emoji = (
            "🟢"
            if direction == "CALL"
            else "🔴"
        )

        lines.append(
            f"{emoji} {signal['pair']} "
            f"<b>{direction}</b> "
            f"Quality {signal['quality']:.0f}"
        )

    await callback.message.answer(
        "\n".join(lines),
        parse_mode="HTML",
    )

    await callback.answer()


@dp.message(Command("users"))
async def users_handler(message: Message):
    if not message.from_user:
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    pending = db.get_pending_users()

    if not pending:
        await message.answer(
            "📭 Новых заявок нет."
        )
        return

    for user in pending:
        username = (
            f"@{user['username']}"
            if user["username"]
            else "нет username"
        )

        text = (
            "👤 <b>Заявка</b>\n\n"
            f"Имя: {user['first_name'] or '—'}\n"
            f"Username: {username}\n"
            f"ID: <code>{user['user_id']}</code>"
        )

        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=admin_request_keyboard(
                user["user_id"]
            ),
        )


@dp.message(Command("id"))
async def id_handler(message: Message):
    if not message.from_user:
        return

    await message.answer(
        f"🆔 Ваш Telegram ID:\n"
        f"<code>{message.from_user.id}</code>",
        parse_mode="HTML",
    )


@dp.message()
async def fallback_handler(message: Message):
    if not message.from_user:
        return

    user_id = message.from_user.id

    if (
        is_admin(user_id)
        or get_user_status(user_id) == "APPROVED"
    ):
        await message.answer(
            "Выберите действие:",
            reply_markup=main_keyboard(),
        )
    else:
        await message.answer(
            "⏳ Доступ ещё не одобрен.",
            reply_markup=pending_keyboard(),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Запуск Pocket Signal Bot..."
    )

    if errors:
        logger.error(
            "Конфигурация содержит ошибки: %s",
            errors,
        )

    bot_task = asyncio.create_task(
        dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    )

    scheduler_task = asyncio.create_task(
        signal_scheduler.run()
    )

    try:
        yield

    finally:
        logger.info(
            "Остановка приложения..."
        )

        scheduler_task.cancel()
        bot_task.cancel()

        await asyncio.gather(
            scheduler_task,
            bot_task,
            return_exceptions=True,
        )

        await market_client.close()
        await bot.session.close()


app = FastAPI(
    title="Pocket Signal Bot",
    lifespan=lifespan,
)


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "Pocket Signal Bot",
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "bot": "running",
    }
