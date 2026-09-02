from __future__ import annotations

import asyncio
import contextlib

from fastapi import FastAPI
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

from config import (
    BOT_TOKEN,
    ADMIN_ID,
    validate_config,
    PAIRS,
    MIN_PROBABILITY,
)

from database import db

from keyboards import (
    main_keyboard,
    pending_keyboard,
    admin_request_keyboard,
    pair_selection_keyboard,
)

from scheduler import SignalScheduler
from market import market_client


errors = validate_config()

if errors:
    print("[CONFIG] Ошибки конфигурации:")
    for error in errors:
        print(f" - {error}")


bot = Bot(BOT_TOKEN)
dp = Dispatcher()

scheduler = SignalScheduler(bot)
scheduler_task: asyncio.Task | None = None
polling_task: asyncio.Task | None = None


# ============================================================
# START
# ============================================================

@dp.message(Command("start"))
async def start_handler(message: Message):

    user_id = message.from_user.id
    username = message.from_user.username or ""
    first_name = message.from_user.first_name or ""

    # Админ
    if user_id == ADMIN_ID:
        db.create_or_update_user(
            user_id=user_id,
            username=username,
            first_name=first_name,
            status="APPROVED",
        )

        await message.answer(
            "👑 Панель администратора\n\n"
            "Бот готов к работе.",
            reply_markup=main_keyboard(),
        )
        return

    user = db.get_user(user_id)

    # Пользователь уже существует
    if user:

        status = user["status"]

        if status == "APPROVED":
            await message.answer(
                "✅ Доступ разрешён.\n\n"
                "Выберите действие:",
                reply_markup=main_keyboard(),
            )
            return

        if status == "PENDING":
            await message.answer(
                "⏳ Ваша заявка ещё рассматривается.\n\n"
                "Ожидайте одобрения администратора.",
                reply_markup=pending_keyboard(),
            )
            return

        if status == "REJECTED":
            await message.answer(
                "❌ В доступе отказано."
            )
            return

    # Новый пользователь
    db.create_or_update_user(
        user_id=user_id,
        username=username,
        first_name=first_name,
        status="PENDING",
    )

    db.add_signal_request(user_id)

    await message.answer(
        "👋 Заявка отправлена администратору.\n\n"
        "После одобрения тебе станет доступен "
        "генератор сигналов."
    )

    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "🔔 Новая заявка на доступ\n\n"
                f"👤 Имя: {first_name}\n"
                f"🔗 Username: @{username if username else 'нет'}\n"
                f"🆔 ID: {user_id}"
            ),
            reply_markup=admin_request_keyboard(user_id),
        )
    except Exception as exc:
        print(
            f"[ADMIN] Ошибка уведомления: {exc}"
        )


# ============================================================
# ПРОВЕРКА ДОСТУПА
# ============================================================

@dp.callback_query(F.data == "check_access")
async def check_access_callback(
    callback: CallbackQuery,
):

    user_id = callback.from_user.id

    user = db.get_user(user_id)

    if not user:
        await callback.answer(
            "Заявка не найдена.",
            show_alert=True,
        )
        return

    status = user["status"]

    if status == "APPROVED":

        await callback.message.edit_text(
            "✅ Доступ уже одобрен.\n\n"
            "Выберите действие:",
            reply_markup=main_keyboard(),
        )

        await callback.answer()
        return

    if status == "PENDING":

        await callback.answer(
            "⏳ Заявка ещё рассматривается.",
            show_alert=True,
        )
        return

    await callback.answer(
        "❌ Доступ не предоставлен.",
        show_alert=True,
    )


# ============================================================
# ADMIN APPROVE
# ============================================================

@dp.callback_query(F.data.startswith("approve:"))
async def approve_callback(
    callback: CallbackQuery,
):

    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )
        return

    user_id = int(
        callback.data.split(":", 1)[1]
    )

    db.set_status(
        user_id=user_id,
        status="APPROVED",
    )

    await callback.answer(
        "Пользователь одобрен."
    )

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "🎉 Доступ одобрен!\n\n"
                "Теперь тебе доступны сигналы."
            ),
            reply_markup=main_keyboard(),
        )
    except Exception as exc:
        print(
            f"[ADMIN] Ошибка уведомления пользователя: {exc}"
        )

    with contextlib.suppress(Exception):
        await callback.message.edit_reply_markup(
            reply_markup=None
        )


# ============================================================
# ADMIN REJECT
# ============================================================

@dp.callback_query(F.data.startswith("reject:"))
async def reject_callback(
    callback: CallbackQuery,
):

    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )
        return

    user_id = int(
        callback.data.split(":", 1)[1]
    )

    db.set_status(
        user_id=user_id,
        status="REJECTED",
    )

    await callback.answer(
        "Пользователь отклонён."
    )

    try:
        await bot.send_message(
            chat_id=user_id,
            text="❌ Ваша заявка на доступ отклонена."
        )
    except Exception as exc:
        print(
            f"[ADMIN] Ошибка уведомления: {exc}"
        )

    with contextlib.suppress(Exception):
        await callback.message.edit_reply_markup(
            reply_markup=None
        )


# ============================================================
# ПОЛУЧИТЬ СИГНАЛ
# ============================================================

@dp.callback_query(F.data == "request_signal")
async def request_signal_callback(
    callback: CallbackQuery,
):

    user = db.get_user(
        callback.from_user.id
    )

    if not user or user["status"] != "APPROVED":
        await callback.answer(
            "❌ У тебя нет доступа.",
            show_alert=True,
        )
        return

    await callback.message.answer(
        "💱 Выбери валютную пару:\n\n"
        f"📈 Минимальный шанс: {MIN_PROBABILITY:.0f}%",
        reply_markup=pair_selection_keyboard(),
    )

    await callback.answer()


# ============================================================
# ВЫБОР ПАРЫ
# ============================================================

@dp.callback_query(F.data.startswith("pair:"))
async def pair_callback(
    callback: CallbackQuery,
):

    user = db.get_user(
        callback.from_user.id
    )

    if not user or user["status"] != "APPROVED":
        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True,
        )
        return

    pair_value = callback.data.split(
        ":",
        1,
    )[1]

    if pair_value == "cancel":

        await callback.message.edit_text(
            "❌ Получение сигнала отменено."
        )

        await callback.answer()
        return

    if pair_value == "any":
        selected_pair = None
        selected_name = "Любая пара"
    else:
        selected_pair = pair_value

        if selected_pair not in PAIRS:
            await callback.answer(
                "❌ Неизвестная пара.",
                show_alert=True,
            )
            return

        selected_name = selected_pair

    await callback.message.edit_text(
        "🔎 Анализирую...\n\n"
        f"💱 {selected_name}\n"
        f"📈 Минимальный шанс: {MIN_PROBABILITY:.0f}%\n\n"
        "⏳ Проверяю рынок..."
    )

    signal = await scheduler.get_manual_signal(
        pair=selected_pair
    )

    if signal is None:

        if selected_pair is None:
            text = (
                "⚪ Сильного сигнала сейчас нет.\n\n"
                "🔀 Проверены все доступные пары.\n"
                f"📈 Минимальный шанс: {MIN_PROBABILITY:.0f}%\n\n"
                "Я не буду выдавать слабый сигнал "
                "только ради того, чтобы что-то показать."
            )
        else:
            text = (
                "⚪ Сильного сигнала сейчас нет.\n\n"
                f"💱 {selected_name}\n"
                f"📈 Минимальный шанс: {MIN_PROBABILITY:.0f}%\n\n"
                "Я не буду выдавать слабый сигнал "
                "только ради того, чтобы что-то показать."
            )

        await callback.message.edit_text(
            text,
            reply_markup=main_keyboard(),
        )

        await callback.answer()
        return

    text = scheduler.format_signal(signal)

    await callback.message.edit_text(
        text,
        reply_markup=main_keyboard(),
    )

    await callback.answer(
        "Сигнал найден."
    )


# ============================================================
# ИСТОРИЯ
# ============================================================

@dp.callback_query(F.data == "history")
async def history_callback(
    callback: CallbackQuery,
):

    user = db.get_user(
        callback.from_user.id
    )

    if not user or user["status"] != "APPROVED":
        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True,
        )
        return

    signals = db.get_recent_signals(
        limit=10
    )

    if not signals:
        await callback.message.edit_text(
            "📊 История пока пустая.",
            reply_markup=main_keyboard(),
        )
        await callback.answer()
        return

    lines = [
        "📊 ПОСЛЕДНИЕ СИГНАЛЫ\n"
    ]

    for signal in signals:

        direction = signal["direction"]

        emoji = (
            "🟢"
            if direction == "CALL"
            else "🔴"
        )

        result = signal["result"] or "—"

        lines.append(
            f"{emoji} {direction} | "
            f"{signal['pair']} | "
            f"Q:{signal['quality']} | "
            f"{result}"
        )

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=main_keyboard(),
    )

    await callback.answer()


# ============================================================
# /USERS
# ============================================================

@dp.message(Command("users"))
async def users_handler(
    message: Message,
):

    if message.from_user.id != ADMIN_ID:
        return

    users = db.get_pending_users()

    if not users:
        await message.answer(
            "📭 Новых заявок нет."
        )
        return

    for user in users:

        user_id = int(user["user_id"])

        username = user["username"] or "нет"
        first_name = user["first_name"] or "нет"

        await message.answer(
            "👤 Заявка\n\n"
            f"Имя: {first_name}\n"
            f"Username: @{username}\n"
            f"ID: {user_id}",
            reply_markup=admin_request_keyboard(
                user_id
            ),
        )


# ============================================================
# /ID
# ============================================================

@dp.message(Command("id"))
async def id_handler(
    message: Message,
):

    await message.answer(
        f"🆔 Твой Telegram ID:\n\n"
        f"{message.from_user.id}"
    )


# ============================================================
# FALLBACK
# ============================================================

@dp.message()
async def fallback_handler(
    message: Message,
):

    user = db.get_user(
        message.from_user.id
    )

    if user and user["status"] == "APPROVED":

        await message.answer(
            "Выбери действие:",
            reply_markup=main_keyboard(),
        )

    elif user and user["status"] == "PENDING":

        await message.answer(
            "⏳ Заявка ещё рассматривается.",
            reply_markup=pending_keyboard(),
        )

    else:

        await message.answer(
            "Используй /start"
        )


# ============================================================
# FASTAPI
# ============================================================

@asynccontextmanager
async def lifespan(
    app: FastAPI,
):

    global scheduler_task
    global polling_task

    print(
        "[APP] Запуск Pocket Signal Bot..."
    )

    scheduler_task = asyncio.create_task(
        scheduler.run()
    )

    polling_task = asyncio.create_task(
        dp.start_polling(
            bot,
            handle_signals=True,
        )
    )

    yield

    print(
        "[APP] Остановка..."
    )

    if scheduler_task:
        scheduler_task.cancel()

        with contextlib.suppress(
            asyncio.CancelledError
        ):
            await scheduler_task

    if polling_task:
        polling_task.cancel()

        with contextlib.suppress(
            asyncio.CancelledError
        ):
            await polling_task

    await market_client.close()
    await bot.session.close()


app = FastAPI(
    lifespan=lifespan
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
    }
