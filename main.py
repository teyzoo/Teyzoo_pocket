from __future__ import annotations

import asyncio
import contextlib

from contextlib import asynccontextmanager

from fastapi import FastAPI

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
    expiry_selection_keyboard,
    OTC_PAIRS,
    format_pair,
)

from scheduler import SignalScheduler


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
# USER SIGNAL SELECTION STATE
# ============================================================

# После выбора пары пользователь выбирает длительность.
#
# {
#     user_id: {
#         "pair": "EUR/USD",
#         "pair_value": "EUR/USD",
#         "selected_name": "EUR/USD"
#     }
# }
#
# Для any / any_regular / any_otc:
# pair == None

pending_signal_selections: dict[int, dict] = {}


# ============================================================
# HELPERS
# ============================================================

def is_approved(user_id: int) -> bool:
    user = db.get_user(user_id)

    return bool(
        user
        and user["status"] == "APPROVED"
    )


def save_pending_selection(
    user_id: int,
    pair_value: str | None,
    selected_name: str,
) -> None:
    pending_signal_selections[user_id] = {
        "pair": pair_value,
        "pair_value": pair_value,
        "selected_name": selected_name,
    }


def get_pending_selection(
    user_id: int,
) -> dict | None:
    return pending_signal_selections.get(user_id)


def clear_pending_selection(
    user_id: int,
) -> None:
    pending_signal_selections.pop(
        user_id,
        None,
    )


async def safe_edit_text(
    callback: CallbackQuery,
    text: str,
    reply_markup=None,
) -> None:
    try:
        await callback.message.edit_text(
            text,
            reply_markup=reply_markup,
        )
    except Exception as exc:
        print(f"[BOT] Ошибка edit_text: {exc}")

        with contextlib.suppress(Exception):
            await callback.message.answer(
                text,
                reply_markup=reply_markup,
            )


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
                "Выбери действие:",
                reply_markup=main_keyboard(),
            )
            return

        # PENDING
        if status == "PENDING":
            await message.answer(
                "⏳ Твоя заявка ещё рассматривается.\n\n"
                "Ожидай одобрения администратора.",
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
                "🚫 Твой доступ заблокирован."
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
                f"🔗 Username: "
                f"@{username if username else 'нет'}\n"
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

        await safe_edit_text(
            callback,
            "✅ Доступ уже одобрен.\n\n"
            "Выбери действие:",
            main_keyboard(),
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
            "[ADMIN] Ошибка уведомления "
            f"пользователя: {exc}"
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
            text="❌ Твоя заявка на доступ отклонена.",
        )

    except Exception as exc:
        print(
            "[ADMIN] Ошибка уведомления "
            f"пользователя: {exc}"
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

    clear_pending_selection(
        callback.from_user.id
    )

    await callback.answer()

    await safe_edit_text(
        callback,
        "🎯 Получение сигнала\n\n"
        "Выбери тип рынка:\n\n"
        "💱 Обычные пары — обычный Forex\n"
        "🟣 OTC — OTC-пары\n"
        "🔀 Все пары — обычные + OTC",
        signal_type_keyboard(),
    )


# ============================================================
# SIGNAL TYPE: REGULAR
# ============================================================

@dp.callback_query(
    F.data == "signal_type:regular"
)
async def signal_type_regular_callback(
    callback: CallbackQuery,
):
    if not is_approved(callback.from_user.id):
        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True,
        )
        return

    clear_pending_selection(
        callback.from_user.id
    )

    await callback.answer()

    await safe_edit_text(
        callback,
        "💱 Обычные валютные пары\n\n"
        f"📈 Минимальный шанс: "
        f"{MIN_PROBABILITY:.0f}%\n\n"
        "Выбери пару:",
        regular_pair_selection_keyboard(),
    )


# ============================================================
# SIGNAL TYPE: OTC
# ============================================================

@dp.callback_query(
    F.data == "signal_type:otc"
)
async def signal_type_otc_callback(
    callback: CallbackQuery,
):
    if not is_approved(callback.from_user.id):
        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True,
        )
        return

    clear_pending_selection(
        callback.from_user.id
    )

    await callback.answer()

    await safe_edit_text(
        callback,
        "🟣 OTC валютные пары\n\n"
        f"📈 Минимальный шанс: "
        f"{MIN_PROBABILITY:.0f}%\n\n"
        "Выбери OTC-пару:",
        otc_pair_selection_keyboard(),
    )


# ============================================================
# SIGNAL TYPE: ALL
# ============================================================

@dp.callback_query(
    F.data == "signal_type:all"
)
async def signal_type_all_callback(
    callback: CallbackQuery,
):
    if not is_approved(callback.from_user.id):
        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True,
        )
        return

    clear_pending_selection(
        callback.from_user.id
    )

    await callback.answer()

    await safe_edit_text(
        callback,
        "🔀 Все доступные пары\n\n"
        "💱 Обычные + 🟣 OTC\n\n"
        f"📈 Минимальный шанс: "
        f"{MIN_PROBABILITY:.0f}%\n\n"
        "Выбери пару или автоматический поиск:",
        all_pair_selection_keyboard(),
    )


# ============================================================
# SIGNAL TYPE: BACK
# ============================================================

@dp.callback_query(
    F.data == "signal_type:back"
)
async def signal_type_back_callback(
    callback: CallbackQuery,
):
    if not is_approved(callback.from_user.id):
        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True,
        )
        return

    clear_pending_selection(
        callback.from_user.id
    )

    await callback.answer()

    await safe_edit_text(
        callback,
        "🎯 Получение сигнала\n\n"
        "Выбери тип рынка:\n\n"
        "💱 Обычные пары\n"
        "🟣 OTC пары\n"
        "🔀 Все пары",
        signal_type_keyboard(),
    )


# ============================================================
# SIGNAL TYPE: CANCEL
# ============================================================

@dp.callback_query(
    F.data == "signal_type:cancel"
)
async def signal_type_cancel_callback(
    callback: CallbackQuery,
):
    if not is_approved(callback.from_user.id):
        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True,
        )
        return

    clear_pending_selection(
        callback.from_user.id
    )

    await callback.answer()

    await safe_edit_text(
        callback,
        "❌ Получение сигнала отменено.",
        main_keyboard(),
    )


# ============================================================
# PAIR SELECTION
# ============================================================

@dp.callback_query(F.data.startswith("pair:"))
async def pair_callback(
    callback: CallbackQuery,
):
    user_id = callback.from_user.id

    if not is_approved(user_id):
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
        clear_pending_selection(user_id)

        await callback.answer()

        await safe_edit_text(
            callback,
            "❌ Получение сигнала отменено.",
            main_keyboard(),
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

        is_regular = (
            selected_pair in PAIRS
        )

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

    save_pending_selection(
        user_id=user_id,
        pair_value=selected_pair,
        selected_name=selected_name,
    )

    await callback.answer()

    await safe_edit_text(
        callback,
        "⏱ Выбери время сделки\n\n"
        f"💱 Пара: {selected_name}\n\n"
        "Можно выбрать от 1 до 20 минут.\n"
        "⚡ «Любое время» — бот сам выберет "
        "лучший вариант.",
        expiry_selection_keyboard(),
    )


# ============================================================
# EXPIRY: BACK
# ============================================================

@dp.callback_query(
    F.data == "expiry:back"
)
async def expiry_back_callback(
    callback: CallbackQuery,
):
    user_id = callback.from_user.id

    if not is_approved(user_id):
        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True,
        )
        return

    clear_pending_selection(user_id)

    await callback.answer()

    await safe_edit_text(
        callback,
        "🎯 Получение сигнала\n\n"
        "Выбери тип рынка:",
        signal_type_keyboard(),
    )


# ============================================================
# EXPIRY: CANCEL
# ============================================================

@dp.callback_query(
    F.data == "expiry:cancel"
)
async def expiry_cancel_callback(
    callback: CallbackQuery,
):
    user_id = callback.from_user.id

    if not is_approved(user_id):
        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True,
        )
        return

    clear_pending_selection(user_id)

    await callback.answer()

    await safe_edit_text(
        callback,
        "❌ Получение сигнала отменено.",
        main_keyboard(),
    )


# ============================================================
# EXPIRY SELECTION
# ============================================================

@dp.callback_query(
    F.data.startswith("expiry:")
)
async def expiry_callback(
    callback: CallbackQuery,
):
    user_id = callback.from_user.id

    if not is_approved(user_id):
        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True,
        )
        return

    value = callback.data.split(
        ":",
        1,
    )[1]

    # --------------------------------------------------------
    # VALIDATE EXPIRY
    # --------------------------------------------------------

    if value == "any":
        expiry_minutes: int | None = None

    else:
        try:
            expiry_minutes = int(value)
        except ValueError:
            await callback.answer(
                "❌ Некорректное время.",
                show_alert=True,
            )
            return

        if not 1 <= expiry_minutes <= 20:
            await callback.answer(
                "❌ Время должно быть от 1 до 20 минут.",
                show_alert=True,
            )
            return

    # --------------------------------------------------------
    # GET SAVED PAIR
    # --------------------------------------------------------

    selection = get_pending_selection(user_id)

    if not selection:
        await callback.answer(
            "❌ Выбор пары устарел. "
            "Начни получение сигнала заново.",
            show_alert=True,
        )
        return

    pair = selection.get("pair")
    selected_name = selection.get(
        "selected_name",
        "Любая пара",
    )

    await callback.answer()

    await safe_edit_text(
        callback,
        "🔎 Анализирую рынок...\n\n"
        f"💱 {selected_name}\n"
        f"⏱ Время: "
        f"{'Любое' if expiry_minutes is None else str(expiry_minutes) + ' мин.'}\n\n"
        f"📈 Минимальный шанс: "
        f"{MIN_PROBABILITY:.0f}%\n\n"
        "⏳ Подбираю только сильный сигнал...",
    )

    signal = None

    # --------------------------------------------------------
    # SPECIFIC PAIR
    # --------------------------------------------------------

    if pair:
        try:
            signal = await scheduler.get_manual_signal(
                pair=pair,
                expiry_minutes=expiry_minutes,
            )

        except TypeError:
            # Совместимость со старой версией scheduler.
            # Не даём боту упасть из-за старой сигнатуры.
            try:
                signal = await scheduler.get_manual_signal(
                    pair=pair,
                )
            except Exception as exc:
                print(
                    f"[SIGNAL] Ошибка: {exc}"
                )

        except Exception as exc:
            print(
                f"[SIGNAL] Ошибка анализа "
                f"{pair}: {exc}"
            )

    # --------------------------------------------------------
    # ANY REGULAR / ANY OTC
    # --------------------------------------------------------

    elif selected_name in (
        "Любая обычная пара",
        "Любая OTC пара",
    ):
        candidates = []

        if selected_name == "Любая обычная пара":
            pairs_to_check = PAIRS
        else:
            pairs_to_check = OTC_PAIRS

        for candidate_pair in pairs_to_check:
            try:
                candidate_signal = (
                    await scheduler.get_manual_signal(
                        pair=candidate_pair,
                        expiry_minutes=expiry_minutes,
                    )
                )

                if candidate_signal:
                    candidates.append(
                        candidate_signal
                    )

            except TypeError:
                try:
                    candidate_signal = (
                        await scheduler.get_manual_signal(
                            pair=candidate_pair,
                        )
                    )

                    if candidate_signal:
                        candidates.append(
                            candidate_signal
                        )

                except Exception as exc:
                    print(
                        f"[SIGNAL] Ошибка "
                        f"{candidate_pair}: {exc}"
                    )

            except Exception as exc:
                print(
                    f"[SIGNAL] Ошибка "
                    f"{candidate_pair}: {exc}"
                )

        if candidates:
            signal = max(
                candidates,
                key=lambda item: (
                    float(
                        getattr(
                            item,
                            "probability",
                            0,
                        )
                    ),
                    float(
                        getattr(
                            item,
                            "quality",
                            0,
                        )
                    ),
                    len(
                        getattr(
                            item,
                            "confirmations",
                            [],
                        )
                        or []
                    ),
                ),
            )

    # --------------------------------------------------------
    # ANY PAIR
    # --------------------------------------------------------

    else:
        try:
            signal = await scheduler.get_manual_signal(
                pair=None,
                expiry_minutes=expiry_minutes,
            )

        except TypeError:
            try:
                signal = await scheduler.get_manual_signal(
                    pair=None,
                )
            except Exception as exc:
                print(
                    f"[SIGNAL] Ошибка: {exc}"
                )

        except Exception as exc:
            print(
                f"[SIGNAL] Ошибка поиска "
                f"лучшей пары: {exc}"
            )

    # --------------------------------------------------------
    # CLEAR STATE
    # --------------------------------------------------------

    clear_pending_selection(user_id)

    # --------------------------------------------------------
    # NO SIGNAL
    # --------------------------------------------------------

    if signal is None:
        duration_text = (
            "любого времени"
            if expiry_minutes is None
            else f"{expiry_minutes} мин."
        )

        await safe_edit_text(
            callback,
            "⚪ Сильного сигнала сейчас нет.\n\n"
            f"💱 {selected_name}\n"
            f"⏱ Время: {duration_text}\n\n"
            f"📈 Требование: "
            f"от {MIN_PROBABILITY:.0f}%\n\n"
            "Я не буду выдавать слабый сигнал "
            "только ради того, чтобы что-то показать.",
            main_keyboard(),
        )

        return

    # --------------------------------------------------------
    # FORMAT SIGNAL
    # --------------------------------------------------------

    try:
        text = scheduler.format_signal(
            signal
        )

    except Exception as exc:
        print(
            f"[SIGNAL] Ошибка форматирования: {exc}"
        )

        pair_text = format_pair(
            getattr(
                signal,
                "pair",
                "UNKNOWN",
            )
        )

        direction = getattr(
            signal,
            "direction",
            "UNKNOWN",
        )

        probability = float(
            getattr(
                signal,
                "probability",
                0,
            )
        )

        quality = float(
            getattr(
                signal,
                "quality",
                0,
            )
        )

        entry_time = getattr(
            signal,
            "entry_time",
            None,
        )

        expiry_time = getattr(
            signal,
            "expiry_time",
            None,
        )

        text = (
            "🚨 СИГНАЛ\n\n"
            f"💱 Пара: {pair_text}\n"
            f"📊 Направление: {direction}\n"
            f"📈 Шанс: {probability:.1f}%\n"
            f"⭐ Quality Score: {quality:.1f}\n"
            f"🟢 Вход: {entry_time}\n"
            f"🔴 Закрытие: {expiry_time}"
        )

    await safe_edit_text(
        callback,
        text,
        main_keyboard(),
    )


# ============================================================
# HISTORY
# ============================================================

@dp.callback_query(F.data == "history")
async def history_callback(
    callback: CallbackQuery,
):
    user_id = callback.from_user.id

    if not is_approved(user_id):
        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True,
        )
        return

    await callback.answer()

    try:
        signals = db.get_recent_signals(
            limit=10
        )
    except Exception as exc:
        print(
            f"[HISTORY] Ошибка: {exc}"
        )
        signals = []

    if not signals:
        await safe_edit_text(
            callback,
            "📊 История сигналов\n\n"
            "Пока сигналов нет.",
            main_keyboard(),
        )
        return

    lines = [
        "📊 История последних сигналов",
        "",
    ]

    for signal in signals:
        pair = signal.get(
            "pair",
            "UNKNOWN",
        )

        direction = signal.get(
            "direction",
            "UNKNOWN",
        )

        quality = signal.get(
            "quality",
            0,
        )

        probability = signal.get(
            "probability",
            None,
        )

        result = signal.get(
            "result",
            None,
        )

        expiry_minutes = signal.get(
            "expiry_minutes",
            None,
        )

        if result == "WIN":
            result_icon = "✅"
        elif result == "LOSS":
            result_icon = "❌"
        elif result == "DRAW":
            result_icon = "➖"
        else:
            result_icon = "⏳"

        probability_text = ""

        if probability is not None:
            probability_text = (
                f" | {float(probability):.0f}%"
            )

        expiry_text = ""

        if expiry_minutes:
            expiry_text = (
                f" | {expiry_minutes}м"
            )

        lines.append(
            f"{result_icon} "
            f"{format_pair(pair)} "
            f"{direction} "
            f"{float(quality):.0f}%"
            f"{probability_text}"
            f"{expiry_text}"
        )

    lines.append("")
    lines.append(
        "⚠️ История показывает фактический "
        "результат прошлых сигналов."
    )

    await safe_edit_text(
        callback,
        "\n".join(lines),
        main_keyboard(),
    )


# ============================================================
# FALLBACK TEXT HANDLER
# ============================================================

@dp.message()
async def fallback_message_handler(
    message: Message,
):
    user_id = message.from_user.id

    if user_id == ADMIN_ID:
        await message.answer(
            "👑 Панель администратора",
            reply_markup=main_keyboard(),
        )
        return

    user = db.get_user(user_id)

    if not user:
        await message.answer(
            "Для начала нажми /start."
        )
        return

    if user["status"] != "APPROVED":
        await message.answer(
            "⏳ Твой доступ ещё не одобрен.",
            reply_markup=pending_keyboard(),
        )
        return

    await message.answer(
        "Выбери действие:",
        reply_markup=main_keyboard(),
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
        "[STARTUP] Запуск Pocket Signal Bot..."
    )

    # --------------------------------------------------------
    # SCHEDULER
    # --------------------------------------------------------

    try:
        scheduler_task = asyncio.create_task(
            scheduler.run()
        )

        print(
            "[SCHEDULER] Automatic scheduler started"
        )

    except Exception as exc:
        print(
            f"[SCHEDULER] Ошибка запуска: {exc}"
        )

    # --------------------------------------------------------
    # TELEGRAM POLLING
    # --------------------------------------------------------

    try:
        polling_task = asyncio.create_task(
            dp.start_polling(
                bot
            )
        )

        print(
            "[BOT] Telegram polling started"
        )

    except Exception as exc:
        print(
            f"[BOT] Ошибка запуска polling: {exc}"
        )

    try:
        yield

    finally:
        print(
            "[SHUTDOWN] Остановка приложения..."
        )

        # ----------------------------------------------------
        # STOP POLLING
        # ----------------------------------------------------

        if polling_task:
            polling_task.cancel()

            with contextlib.suppress(
                asyncio.CancelledError,
                Exception,
            ):
                await polling_task

        # ----------------------------------------------------
        # STOP SCHEDULER
        # ----------------------------------------------------

        if scheduler_task:
            scheduler_task.cancel()

            with contextlib.suppress(
                asyncio.CancelledError,
                Exception,
            ):
                await scheduler_task

        with contextlib.suppress(Exception):
            await scheduler.stop()

        # ----------------------------------------------------
        # CLOSE BOT
        # ----------------------------------------------------

        with contextlib.suppress(Exception):
            await bot.session.close()

        print(
            "[SHUTDOWN] Приложение остановлено."
        )


app = FastAPI(
    title="Pocket Signal Bot",
    version="1.0.0",
    lifespan=lifespan,
)


# ============================================================
# HEALTH CHECK
# ============================================================

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
        "service": "Pocket Signal Bot",
    }


# ============================================================
# RUN DIRECTLY
# ============================================================

if __name__ == "__main__":
    import uvicorn

    import os

    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
    )
