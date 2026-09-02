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
    signal_type_keyboard,
    regular_pair_selection_keyboard,
    otc_pair_selection_keyboard,
    all_pair_selection_keyboard,
    OTC_PAIRS,
    format_pair,
)

from scheduler import SignalScheduler
from market import market_client


# ============================================================
# CONFIG VALIDATION
# ============================================================

errors = validate_config()

if errors:
    print("[CONFIG] Ошибки конфигурации:")

    for error in errors:
        print(f" - {error}")


# ============================================================
# BOT
# ============================================================

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# ============================================================
# SCHEDULER
# ============================================================

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

    # --------------------------------------------------------
    # ADMIN
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # EXISTING USER
    # --------------------------------------------------------

    user = db.get_user(user_id)

    if user:

        status = user["status"]

        # APPROVED
        if status == "APPROVED":

            await message.answer(
                "✅ Доступ разрешён.\n\n"
                "Выберите действие:",
                reply_markup=main_keyboard(),
            )

            return

        # PENDING
        if status == "PENDING":

            await message.answer(
                "⏳ Ваша заявка ещё рассматривается.\n\n"
                "Ожидайте одобрения администратора.",
                reply_markup=pending_keyboard(),
            )

            return

        # REJECTED
        if status == "REJECTED":

            await message.answer(
                "❌ В доступе отказано."
            )

            return

        # BLOCKED
        if status == "BLOCKED":

            await message.answer(
                "🚫 Ваш доступ заблокирован."
            )

            return

    # --------------------------------------------------------
    # NEW USER
    # --------------------------------------------------------

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
# CHECK ACCESS
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

    # APPROVED
    if status == "APPROVED":

        await callback.answer()

        await callback.message.edit_text(
            "✅ Доступ уже одобрен.\n\n"
            "Выберите действие:",
            reply_markup=main_keyboard(),
        )

        return

    # PENDING
    if status == "PENDING":

        await callback.answer(
            "⏳ Заявка ещё рассматривается.",
            show_alert=True,
        )

        return

    # BLOCKED
    if status == "BLOCKED":

        await callback.answer(
            "🚫 Доступ заблокирован.",
            show_alert=True,
        )

        return

    # REJECTED
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

    try:

        user_id = int(
            callback.data.split(
                ":",
                1,
            )[1]
        )

    except (ValueError, IndexError):

        await callback.answer(
            "Некорректный ID пользователя.",
            show_alert=True,
        )

        return

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

    try:

        user_id = int(
            callback.data.split(
                ":",
                1,
            )[1]
        )

    except (ValueError, IndexError):

        await callback.answer(
            "Некорректный ID пользователя.",
            show_alert=True,
        )

        return

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
            text="❌ Ваша заявка на доступ отклонена.",
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
# GET SIGNAL
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

    # Сразу закрываем callback.
    await callback.answer()

    await callback.message.edit_text(
        "🎯 Получение сигнала\n\n"
        "Выбери тип рынка:\n\n"
        "💱 Обычные пары — обычный Forex\n"
        "🟣 OTC — OTC-пары\n"
        "🔀 Все пары — обычные + OTC",
        reply_markup=signal_type_keyboard(),
    )


# ============================================================
# SIGNAL TYPE: REGULAR
# ============================================================

@dp.callback_query(F.data == "signal_type:regular")
async def signal_type_regular_callback(
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

    await callback.answer()

    await callback.message.edit_text(
        "💱 Обычные валютные пары\n\n"
        f"📈 Минимальный шанс: "
        f"{MIN_PROBABILITY:.0f}%\n\n"
        "Выбери пару:",
        reply_markup=regular_pair_selection_keyboard(),
    )


# ============================================================
# SIGNAL TYPE: OTC
# ============================================================

@dp.callback_query(F.data == "signal_type:otc")
async def signal_type_otc_callback(
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

    await callback.answer()

    await callback.message.edit_text(
        "🟣 OTC валютные пары\n\n"
        f"📈 Минимальный шанс: "
        f"{MIN_PROBABILITY:.0f}%\n\n"
        "Выбери OTC-пару:",
        reply_markup=otc_pair_selection_keyboard(),
    )


# ============================================================
# SIGNAL TYPE: ALL
# ============================================================

@dp.callback_query(F.data == "signal_type:all")
async def signal_type_all_callback(
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

    await callback.answer()

    await callback.message.edit_text(
        "🔀 Все доступные пары\n\n"
        "💱 Обычные + 🟣 OTC\n\n"
        f"📈 Минимальный шанс: "
        f"{MIN_PROBABILITY:.0f}%\n\n"
        "Выбери пару или автоматический поиск:",
        reply_markup=all_pair_selection_keyboard(),
    )


# ============================================================
# SIGNAL TYPE: BACK
# ============================================================

@dp.callback_query(F.data == "signal_type:back")
async def signal_type_back_callback(
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

    await callback.answer()

    await callback.message.edit_text(
        "🎯 Получение сигнала\n\n"
        "Выбери тип рынка:\n\n"
        "💱 Обычные пары\n"
        "🟣 OTC пары\n"
        "🔀 Все пары",
        reply_markup=signal_type_keyboard(),
    )


# ============================================================
# SIGNAL TYPE: CANCEL
# ============================================================

@dp.callback_query(F.data == "signal_type:cancel")
async def signal_type_cancel_callback(
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

    await callback.answer()

    await callback.message.edit_text(
        "❌ Получение сигнала отменено.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# PAIR SELECTION
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

    # --------------------------------------------------------
    # CANCEL
    # --------------------------------------------------------

    if pair_value == "cancel":

        await callback.answer()

        await callback.message.edit_text(
            "❌ Получение сигнала отменено.",
            reply_markup=main_keyboard(),
        )

        return

    # --------------------------------------------------------
    # ANY PAIR
    # --------------------------------------------------------

    if pair_value == "any":

        selected_pair = None
        selected_name = "Любая пара"

    # --------------------------------------------------------
    # ANY REGULAR
    # --------------------------------------------------------

    elif pair_value == "any_regular":

        selected_pair = None
        selected_name = "Любая обычная пара"

    # --------------------------------------------------------
    # ANY OTC
    # --------------------------------------------------------

    elif pair_value == "any_otc":

        selected_pair = None
        selected_name = "Любая OTC пара"

    # --------------------------------------------------------
    # SPECIFIC PAIR
    # --------------------------------------------------------

    else:

        selected_pair = pair_value

        # Обычная пара
        is_regular = (
            selected_pair in PAIRS
        )

        # OTC
        is_otc = (
            selected_pair in OTC_PAIRS
        )

        if not is_regular and not is_otc:

            await callback.answer(
                "❌ Неизвестная пара.",
                show_alert=True,
            )

            return

        selected_name = format_pair(
            selected_pair
        )

    # --------------------------------------------------------
    # CALLBACK ANSWER
    # --------------------------------------------------------

    await callback.answer(
        "🔎 Начинаю анализ..."
    )

    # --------------------------------------------------------
    # SHOW ANALYSIS STATUS
    # --------------------------------------------------------

    await callback.message.edit_text(
        "🔎 Анализирую...\n\n"
        f"💱 {selected_name}\n"
        f"📈 Минимальный шанс: "
        f"{MIN_PROBABILITY:.0f}%\n\n"
        "⏳ Проверяю рынок..."
    )

    # --------------------------------------------------------
    # MARKET ANALYSIS
    # --------------------------------------------------------

    try:

        # ----------------------------------------------------
        # SPECIFIC PAIR
        # ----------------------------------------------------

        if selected_pair is not None:

            signal = await scheduler.get_manual_signal(
                pair=selected_pair
            )

        # ----------------------------------------------------
        # ANY REGULAR
        # ----------------------------------------------------

        elif pair_value == "any_regular":

            signal = None

            best_signal = None

            for pair in PAIRS:

                try:

                    candidate = (
                        await scheduler.get_manual_signal(
                            pair=pair
                        )
                    )

                    if candidate is None:
                        continue

                    if best_signal is None:

                        best_signal = candidate

                    elif (
                        candidate.quality
                        > best_signal.quality
                    ):

                        best_signal = candidate

                except Exception as exc:

                    print(
                        f"[MANUAL] Ошибка "
                        f"{pair}: "
                        f"{type(exc).__name__}: "
                        f"{exc}"
                    )

            signal = best_signal

        # ----------------------------------------------------
        # ANY OTC
        # ----------------------------------------------------

        elif pair_value == "any_otc":

            signal = None

            best_signal = None

            for pair in OTC_PAIRS:

                try:

                    candidate = (
                        await scheduler.get_manual_signal(
                            pair=pair
                        )
                    )

                    if candidate is None:
                        continue

                    if best_signal is None:

                        best_signal = candidate

                    elif (
                        candidate.quality
                        > best_signal.quality
                    ):

                        best_signal = candidate

                except Exception as exc:

                    print(
                        f"[MANUAL OTC] Ошибка "
                        f"{pair}: "
                        f"{type(exc).__name__}: "
                        f"{exc}"
                    )

            signal = best_signal

        # ----------------------------------------------------
        # ANY ALL
        # ----------------------------------------------------

        else:

            signal = await scheduler.get_manual_signal(
                pair=None
            )

    except Exception as exc:

        print(
            "[MANUAL] Ошибка получения сигнала:"
        )

        print(
            f"   {type(exc).__name__}: {exc}"
        )

        await callback.message.edit_text(
            "⚠️ Не удалось получить сигнал.\n\n"
            f"💱 {selected_name}\n\n"
            "Произошла ошибка при анализе рынка.",
            reply_markup=main_keyboard(),
        )

        return

    # --------------------------------------------------------
    # NO SIGNAL
    # --------------------------------------------------------

    if signal is None:

        text = (
            "⚪ Сильного сигнала сейчас нет.\n\n"
            f"💱 {selected_name}\n"
            f"📈 Минимальный шанс: "
            f"{MIN_PROBABILITY:.0f}%\n\n"
            "Я не буду выдавать слабый сигнал "
            "только ради того, чтобы что-то показать."
        )

        await callback.message.edit_text(
            text,
            reply_markup=main_keyboard(),
        )

        return

    # --------------------------------------------------------
    # SIGNAL FOUND
    # --------------------------------------------------------

    text = scheduler.format_signal(
        signal
    )

    await callback.message.edit_text(
        text,
        reply_markup=main_keyboard(),
    )


# ============================================================
# HISTORY
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

    await callback.answer()

    signals = db.get_recent_signals(
        limit=10
    )

    if not signals:

        await callback.message.edit_text(
            "📊 История пока пустая.",
            reply_markup=main_keyboard(),
        )

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

        result = (
            signal["result"]
            or "—"
        )

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

        user_id = int(
            user["user_id"]
        )

        username = (
            user["username"]
            or "нет"
        )

        first_name = (
            user["first_name"]
            or "нет"
        )

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
        "🆔 Твой Telegram ID:\n\n"
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

    elif user and user["status"] == "BLOCKED":

        await message.answer(
            "🚫 Ваш доступ заблокирован."
        )

    else:

        await message.answer(
            "Используй /start"
        )


# ============================================================
# FASTAPI LIFESPAN
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

    # Передаём bot в scheduler.
    try:

        scheduler.set_bot(bot)

    except Exception:

        pass

    # --------------------------------------------------------
    # START SCHEDULER
    # --------------------------------------------------------

    scheduler_task = asyncio.create_task(
        scheduler.run()
    )

    # --------------------------------------------------------
    # START POLLING
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # STOP SCHEDULER
    # --------------------------------------------------------

    if scheduler_task:

        scheduler_task.cancel()

        with contextlib.suppress(
            asyncio.CancelledError
        ):

            await scheduler_task

    # --------------------------------------------------------
    # STOP POLLING
    # --------------------------------------------------------

    if polling_task:

        polling_task.cancel()

        with contextlib.suppress(
            asyncio.CancelledError
        ):

            await polling_task

    # --------------------------------------------------------
    # CLOSE MARKET
    # --------------------------------------------------------

    with contextlib.suppress(
        Exception
    ):

        await market_client.close()

    # --------------------------------------------------------
    # CLOSE BOT
    # --------------------------------------------------------

    with contextlib.suppress(
        Exception
    ):

        await bot.session.close()


# ============================================================
# FASTAPI APP
# ============================================================

app = FastAPI(
    lifespan=lifespan
)


# ============================================================
# ROOT
# ============================================================

@app.get("/")
async def root():

    return {
        "status": "ok",
        "service": "Pocket Signal Bot",
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():

    return {
        "status": "healthy",
    }
