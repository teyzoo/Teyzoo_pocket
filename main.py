from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from html import escape
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from fastapi import FastAPI

from config import (
    ADMIN_IDS,
    BOT_TOKEN,
    MIN_SIGNAL_QUALITY,
    PAIRS,
)
from database import (
    get_approved_users,
    get_user,
    register_user,
)
from keyboards import (
    admin_request_keyboard,
    expiry_selection_keyboard,
    main_keyboard,
    pending_keyboard,
    pair_selection_keyboard,
    signal_type_keyboard,
)
from market import MarketClient
from scheduler import SignalScheduler
from signal_scanner import SignalScanner, TradingSignal


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger("main")


# =========================================================
# BOT
# =========================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is not configured."
    )


bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML,
    ),
)

dp = Dispatcher()


# =========================================================
# MARKET + SCHEDULER
# =========================================================

market = MarketClient()

scheduler = SignalScheduler(
    bot=bot,
    market=market,
)


# =========================================================
# STATE
# =========================================================

pending_signal_selections: dict[
    int,
    dict[str, Any],
] = {}


# =========================================================
# ADMIN HELPERS
# =========================================================

def is_admin(
    user_id: int,
) -> bool:
    return user_id in ADMIN_IDS


def get_primary_admin_id() -> int:
    if not ADMIN_IDS:
        raise RuntimeError(
            "ADMIN_IDS должен содержать хотя бы один ID администратора."
        )

    return ADMIN_IDS[0]


# =========================================================
# GENERAL HELPERS
# =========================================================

def _user_id(
    message: Message,
) -> int:
    if message.from_user is None:
        raise RuntimeError(
            "Telegram user is unavailable."
        )

    return message.from_user.id


def _callback_user_id(
    callback: CallbackQuery,
) -> int:
    if callback.from_user is None:
        raise RuntimeError(
            "Telegram user is unavailable."
        )

    return callback.from_user.id


def _format_pair(
    pair: str | None,
) -> str:
    if not pair:
        return "Любая пара"

    value = str(pair).strip()

    if value.lower().endswith("_otc"):
        value = value[:-4]
        otc = True
    else:
        otc = False

    value = (
        value.replace("/", "")
        .replace("_", "")
        .replace("-", "")
    )

    if len(value) == 6:
        result = (
            f"{value[:3]}/{value[3:]}"
        )
    else:
        result = value

    if otc:
        result += " OTC"

    return result


def _format_expiry(
    minutes: int | None,
) -> str:
    if minutes is None:
        return "любого времени"

    return f"{minutes} мин."


def _get_signal_quality(
    signal: TradingSignal,
) -> float:
    try:
        return float(
            getattr(
                signal,
                "quality_score",
                0.0,
            )
        )

    except (
        TypeError,
        ValueError,
    ):
        return 0.0


def _get_signal_expiry(
    signal: TradingSignal,
) -> datetime | None:

    for attribute in (
        "expiry_time",
        "expiration_time",
        "expires_at",
        "close_time",
    ):
        value = getattr(
            signal,
            attribute,
            None,
        )

        if isinstance(
            value,
            datetime,
        ):
            return value

        if isinstance(
            value,
            str,
        ):
            try:
                return datetime.fromisoformat(
                    value.replace(
                        "Z",
                        "+00:00",
                    )
                )

            except ValueError:
                continue

    return None


def _signal_to_text(
    signal: TradingSignal,
) -> str:

    try:
        text = SignalScanner.format_signal(
            signal
        )

    except Exception:
        logger.exception(
            "Failed to format signal."
        )

        text = (
            "📊 <b>Сигнал</b>\n\n"
            f"💱 Пара: "
            f"<b>{escape(str(signal.symbol))}</b>\n"
            f"📈 Направление: "
            f"<b>{escape(str(signal.direction))}</b>\n"
            f"⭐ Качество: "
            f"<b>{_get_signal_quality(signal):.1f}%</b>"
        )

    return text


# =========================================================
# START MESSAGE
# =========================================================

def start_text(
    approved: bool,
) -> str:

    if approved:
        return (
            "👋 <b>Добро пожаловать!</b>\n\n"
            "🤖 Бот готов искать сильные торговые сигналы.\n\n"
            f"📈 Минимальное качество: "
            f"<b>{MIN_SIGNAL_QUALITY:.0f}%</b>\n\n"
            "Нажми <b>🎯 Получить сигнал</b>, "
            "чтобы начать."
        )

    return (
        "👋 <b>Добро пожаловать!</b>\n\n"
        "⏳ Твой доступ пока ожидает подтверждения "
        "администратора.\n\n"
        "После одобрения ты сможешь получать сигналы."
    )


# =========================================================
# /START
# =========================================================

@dp.message(
    CommandStart()
)
async def start_handler(
    message: Message,
) -> None:

    user_id = _user_id(
        message
    )

    try:
        user = await get_user(
            user_id
        )

    except Exception:
        logger.exception(
            "Failed to get user %s.",
            user_id,
        )

        user = None

    if user is None:

        try:
            await register_user(
                telegram_id=user_id,
                username=(
                    message.from_user.username
                    if message.from_user
                    else None
                ),
                first_name=(
                    message.from_user.first_name
                    if message.from_user
                    else None
                ),
            )

        except TypeError:
            try:
                await register_user(
                    user_id=user_id,
                    username=(
                        message.from_user.username
                        if message.from_user
                        else None
                    ),
                )

            except Exception:
                logger.exception(
                    "Failed to register user %s.",
                    user_id,
                )

        except Exception:
            logger.exception(
                "Failed to register user %s.",
                user_id,
            )

        # -------------------------------------------------
        # Notify primary admin about new user.
        # -------------------------------------------------

        try:

            admin_id = get_primary_admin_id()

            username = (
                message.from_user.username
                if message.from_user
                and message.from_user.username
                else "нет"
            )

            name = (
                message.from_user.full_name
                if message.from_user
                else "неизвестно"
            )

            text = (
                "👤 <b>Новый пользователь</b>\n\n"
                f"🆔 ID: <code>{user_id}</code>\n"
                f"👤 Имя: "
                f"<b>{escape(name)}</b>\n"
                f"🔗 Username: "
                f"<b>@{escape(username)}</b>\n\n"
                "Выбери действие:"
            )

            await bot.send_message(
                chat_id=admin_id,
                text=text,
                reply_markup=admin_request_keyboard(
                    user_id
                ),
            )

        except Exception:
            logger.exception(
                "Failed to notify admin about user %s.",
                user_id,
            )

        await message.answer(
            start_text(False),
            reply_markup=pending_keyboard(),
        )

        return

    # -----------------------------------------------------
    # Determine status.
    # -----------------------------------------------------

    status = None

    if isinstance(
        user,
        dict,
    ):
        status = user.get(
            "status"
        )

    else:
        status = getattr(
            user,
            "status",
            None,
        )

    approved = (
        str(status).upper()
        == "APPROVED"
    )

    if is_admin(user_id):
        approved = True

    if approved:

        await message.answer(
            start_text(True),
            reply_markup=main_keyboard(),
        )

    else:

        await message.answer(
            start_text(False),
            reply_markup=pending_keyboard(),
        )


# =========================================================
# MAIN MENU
# =========================================================

@dp.callback_query(
    F.data == "request_signal"
)
async def request_signal_callback(
    callback: CallbackQuery,
) -> None:

    user_id = _callback_user_id(
        callback
    )

    try:
        user = await get_user(
            user_id
        )

    except Exception:
        logger.exception(
            "Failed to get user %s.",
            user_id,
        )

        await callback.answer(
            "Ошибка проверки доступа.",
            show_alert=True,
        )

        return

    status = None

    if isinstance(
        user,
        dict,
    ):
        status = user.get(
            "status"
        )

    else:
        status = getattr(
            user,
            "status",
            None,
        )

    approved = (
        str(status).upper()
        == "APPROVED"
    )

    if is_admin(user_id):
        approved = True

    if not approved:

        await callback.answer(
            "Доступ ещё не одобрен.",
            show_alert=True,
        )

        return

    pending_signal_selections[
        user_id
    ] = {}

    await callback.message.edit_text(
        "🎯 <b>Получение сигнала</b>\n\n"
        "Выбери тип пары:",
        reply_markup=signal_type_keyboard(),
    )

    await callback.answer()


# =========================================================
# CHECK ACCESS
# =========================================================

@dp.callback_query(
    F.data == "check_access"
)
async def check_access_callback(
    callback: CallbackQuery,
) -> None:

    user_id = _callback_user_id(
        callback
    )

    try:
        user = await get_user(
            user_id
        )

    except Exception:
        await callback.answer(
            "Не удалось проверить доступ.",
            show_alert=True,
        )
        return

    status = None

    if isinstance(
        user,
        dict,
    ):
        status = user.get(
            "status"
        )

    else:
        status = getattr(
            user,
            "status",
            None,
        )

    if (
        str(status).upper()
        == "APPROVED"
        or is_admin(user_id)
    ):

        await callback.message.edit_text(
            start_text(True),
            reply_markup=main_keyboard(),
        )

        await callback.answer(
            "Доступ подтверждён!"
        )

    else:

        await callback.answer(
            "Доступ пока не одобрен.",
            show_alert=True,
        )


# =========================================================
# SIGNAL TYPE
# =========================================================

@dp.callback_query(
    F.data.startswith("signal_type:")
)
async def signal_type_callback(
    callback: CallbackQuery,
) -> None:

    user_id = _callback_user_id(
        callback
    )

    value = callback.data.split(
        ":",
        1,
    )[1]

    pending_signal_selections[
        user_id
    ] = {
        "signal_type": value,
    }

    if value == "regular":

        text = (
            "💱 <b>Обычные пары</b>\n\n"
            "Выбери пару:"
        )

    elif value == "otc":

        text = (
            "🟣 <b>OTC пары</b>\n\n"
            "Выбери пару:"
        )

    else:

        text = (
            "🔀 <b>Все пары</b>\n\n"
            "Выбери пару:"
        )

    # -----------------------------------------------------
    # Existing keyboard function is preserved.
    # -----------------------------------------------------

    try:

        from keyboards import (
            regular_pair_keyboard,
            otc_pair_keyboard,
            all_pair_keyboard,
        )

        if value == "regular":

            keyboard = regular_pair_keyboard()

        elif value == "otc":

            keyboard = otc_pair_keyboard()

        else:

            keyboard = all_pair_keyboard()

    except ImportError:

        keyboard = pair_selection_keyboard()

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
    )

    await callback.answer()


# =========================================================
# PAIR SELECTION
# =========================================================

@dp.callback_query(
    F.data.startswith("pair:")
)
async def pair_callback(
    callback: CallbackQuery,
) -> None:

    user_id = _callback_user_id(
        callback
    )

    pair_value = callback.data.split(
        ":",
        1,
    )[1]

    selection = pending_signal_selections.get(
        user_id,
        {},
    )

    signal_type = selection.get(
        "signal_type",
        "all",
    )

    # -----------------------------------------------------
    # Any pair variants.
    # -----------------------------------------------------

    if pair_value == "any":

        selection["pair"] = None
        selection["pair_mode"] = "any"

    elif pair_value == "any_regular":

        selection["pair"] = None
        selection["pair_mode"] = "regular"

    elif pair_value == "any_otc":

        selection["pair"] = None
        selection["pair_mode"] = "otc"

    else:

        selection["pair"] = pair_value
        selection["pair_mode"] = signal_type

    pending_signal_selections[
        user_id
    ] = selection

    await callback.message.edit_text(
        (
            "⏱ <b>Выбери время действия сигнала</b>\n\n"
            "Можно выбрать от <b>1 до 20 минут</b> "
            "или <b>⚡ Любое время</b>."
        ),
        reply_markup=expiry_selection_keyboard(),
    )

    await callback.answer()


# =========================================================
# EXPIRY SELECTION
# =========================================================

@dp.callback_query(
    F.data.startswith("expiry:")
)
async def expiry_callback(
    callback: CallbackQuery,
) -> None:

    user_id = _callback_user_id(
        callback
    )

    value = callback.data.split(
        ":",
        1,
    )[1]

    if value == "back":

        pending_signal_selections.pop(
            user_id,
            None,
        )

        await callback.message.edit_text(
            "🎯 <b>Получение сигнала</b>\n\n"
            "Выбери тип пары:",
            reply_markup=signal_type_keyboard(),
        )

        await callback.answer()
        return

    if value == "cancel":

        pending_signal_selections.pop(
            user_id,
            None,
        )

        await callback.message.edit_text(
            "Главное меню:",
            reply_markup=main_keyboard(),
        )

        await callback.answer()
        return

    # -----------------------------------------------------
    # ANY EXPIRY
    # -----------------------------------------------------

    if value == "any":

        expiry_minutes: int | None = None

    else:

        try:
            expiry_minutes = int(
                value
            )

        except ValueError:

            await callback.answer(
                "Некорректное время.",
                show_alert=True,
            )

            return

        if not (
            1
            <= expiry_minutes
            <= 20
        ):

            await callback.answer(
                "Время должно быть от 1 до 20 минут.",
                show_alert=True,
            )

            return

    selection = pending_signal_selections.get(
        user_id,
        {},
    )

    pair = selection.get(
        "pair"
    )

    pair_mode = selection.get(
        "pair_mode",
        selection.get(
            "signal_type",
            "all",
        ),
    )

    # -----------------------------------------------------
    # OTC protection.
    #
    # Twelve Data public historical API does not provide
    # the OTC candles used by our scanner.
    # We therefore do NOT fake an OTC signal.
    # -----------------------------------------------------

    if (
        pair_mode == "otc"
        or (
            pair is not None
            and str(pair).lower().endswith(
                "_otc"
            )
        )
    ):

        pending_signal_selections.pop(
            user_id,
            None,
        )

        await callback.message.edit_text(
            (
                "🟣 <b>OTC сигнал сейчас недоступен</b>\n\n"
                "Источник рыночных данных не предоставляет "
                "достоверные исторические OTC-свечи.\n\n"
                "Я не буду подменять OTC обычной парой "
                "или придумывать сигнал."
            ),
            reply_markup=main_keyboard(),
        )

        await callback.answer()

        return

    # -----------------------------------------------------
    # WAIT MESSAGE
    # -----------------------------------------------------

    pair_text = _format_pair(
        pair
    )

    expiry_text = _format_expiry(
        expiry_minutes
    )

    await callback.message.edit_text(
        (
            "🔎 <b>Ищу сильный сигнал...</b>\n\n"
            f"💱 Пара: <b>{escape(pair_text)}</b>\n"
            f"⏱ Время: <b>{escape(expiry_text)}</b>\n"
            f"📈 Минимум: "
            f"<b>{MIN_SIGNAL_QUALITY:.0f}%</b>\n\n"
            "Анализирую рынок."
        )
    )

    await callback.answer()

    # -----------------------------------------------------
    # GET SIGNAL
    # -----------------------------------------------------

    try:

        signal = await scheduler.get_manual_signal(
            pair=pair,
            expiry_minutes=expiry_minutes,
        )

    except Exception as exc:

        logger.exception(
            (
                "Manual signal generation failed | "
                "user=%s | pair=%s | expiry=%s"
            ),
            user_id,
            pair,
            expiry_minutes,
        )

        await callback.message.edit_text(
            (
                "❌ <b>Не удалось получить сигнал.</b>\n\n"
                "Произошла ошибка при анализе рынка.\n\n"
                f"<code>{escape(str(exc))}</code>"
            ),
            reply_markup=main_keyboard(),
        )

        return

    pending_signal_selections.pop(
        user_id,
        None,
    )

    # -----------------------------------------------------
    # NO SIGNAL
    # -----------------------------------------------------

    if signal is None:

        await callback.message.edit_text(
            (
                "⚪ <b>Сильного сигнала сейчас нет.</b>\n\n"
                f"💱 {escape(pair_text)}\n"
                f"⏱ Время: {escape(expiry_text)}\n\n"
                f"📈 Требование: "
                f"<b>от {MIN_SIGNAL_QUALITY:.0f}%</b>\n\n"
                "Я не буду выдавать слабый сигнал "
                "только ради того, чтобы что-то показать."
            ),
            reply_markup=main_keyboard(),
        )

        return

    # -----------------------------------------------------
    # QUALITY CHECK
    # -----------------------------------------------------

    quality = _get_signal_quality(
        signal
    )

    if quality < MIN_SIGNAL_QUALITY:

        await callback.message.edit_text(
            (
                "⚪ <b>Сильного сигнала сейчас нет.</b>\n\n"
                f"Фактическое качество: "
                f"<b>{quality:.1f}%</b>\n"
                f"Требуется: "
                f"<b>{MIN_SIGNAL_QUALITY:.0f}%+</b>\n\n"
                "Слабый сигнал не показываю."
            ),
            reply_markup=main_keyboard(),
        )

        return

    # -----------------------------------------------------
    # SIGNAL FOUND
    # -----------------------------------------------------

    text = _signal_to_text(
        signal
    )

    expiry_dt = _get_signal_expiry(
        signal
    )

    if expiry_dt is not None:

        try:

            expiry_text_actual = (
                expiry_dt.astimezone().strftime(
                    "%H:%M"
                )
            )

        except Exception:

            expiry_text_actual = (
                expiry_dt.strftime(
                    "%H:%M"
                )
            )

        if "Закрытие" not in text:

            text += (
                "\n"
                f"⏱ Закрытие: "
                f"<b>{expiry_text_actual}</b>"
            )

    await callback.message.edit_text(
        text,
        reply_markup=main_keyboard(),
    )


# =========================================================
# CANCEL
# =========================================================

@dp.callback_query(
    F.data == "cancel"
)
async def cancel_callback(
    callback: CallbackQuery,
) -> None:

    user_id = _callback_user_id(
        callback
    )

    pending_signal_selections.pop(
        user_id,
        None,
    )

    await callback.message.edit_text(
        "Главное меню:",
        reply_markup=main_keyboard(),
    )

    await callback.answer()


# =========================================================
# HISTORY
# =========================================================

@dp.callback_query(
    F.data == "history"
)
async def history_callback(
    callback: CallbackQuery,
) -> None:

    user_id = _callback_user_id(
        callback
    )

    try:

        from database import get_recent_signals

        signals = await get_recent_signals(
            user_id=user_id,
            limit=10,
        )

    except Exception:

        logger.exception(
            "Failed to load signal history for %s.",
            user_id,
        )

        signals = []

    if not signals:

        text = (
            "📊 <b>История сигналов</b>\n\n"
            "История пока пустая."
        )

    else:

        lines = [
            "📊 <b>История сигналов</b>",
            "",
        ]

        for item in signals:

            if isinstance(
                item,
                dict,
            ):

                symbol = item.get(
                    "symbol",
                    "—",
                )

                direction = item.get(
                    "direction",
                    "—",
                )

                quality = item.get(
                    "quality_score",
                    item.get(
                        "quality",
                        "—",
                    ),
                )

                result = item.get(
                    "result",
                    item.get(
                        "status",
                        "—",
                    ),
                )

            else:

                symbol = getattr(
                    item,
                    "symbol",
                    "—",
                )

                direction = getattr(
                    item,
                    "direction",
                    "—",
                )

                quality = getattr(
                    item,
                    "quality_score",
                    "—",
                )

                result = getattr(
                    item,
                    "result",
                    getattr(
                        item,
                        "status",
                        "—",
                    ),
                )

            lines.append(
                (
                    f"💱 <b>{escape(str(symbol))}</b> | "
                    f"{escape(str(direction))}\n"
                    f"⭐ {escape(str(quality))} | "
                    f"📌 {escape(str(result))}"
                )
            )

        text = "\n\n".join(
            lines
        )

    await callback.message.edit_text(
        text,
        reply_markup=main_keyboard(),
    )

    await callback.answer()


# =========================================================
# ADMIN APPROVE / REJECT
# =========================================================

@dp.callback_query(
    F.data.startswith("admin_approve:")
)
async def admin_approve_callback(
    callback: CallbackQuery,
) -> None:

    admin_id = _callback_user_id(
        callback
    )

    if not is_admin(admin_id):

        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

    try:

        target_user_id = int(
            callback.data.split(
                ":",
                1,
            )[1]
        )

    except (
        ValueError,
        IndexError,
    ):

        await callback.answer(
            "Некорректный ID пользователя.",
            show_alert=True,
        )

        return

    try:

        from database import approve_user

        await approve_user(
            target_user_id
        )

    except Exception:

        logger.exception(
            "Failed to approve user %s.",
            target_user_id,
        )

        await callback.answer(
            "Ошибка одобрения пользователя.",
            show_alert=True,
        )

        return

    try:

        await bot.send_message(
            chat_id=target_user_id,
            text=(
                "✅ <b>Доступ одобрен!</b>\n\n"
                "Теперь ты можешь получать торговые сигналы."
            ),
            reply_markup=main_keyboard(),
        )

    except Exception:

        logger.exception(
            "Failed to notify approved user %s.",
            target_user_id,
        )

    await callback.message.edit_text(
        (
            "✅ <b>Пользователь одобрен.</b>\n\n"
            f"ID: <code>{target_user_id}</code>"
        )
    )

    await callback.answer(
        "Пользователь одобрен."
    )


@dp.callback_query(
    F.data.startswith("admin_reject:")
)
async def admin_reject_callback(
    callback: CallbackQuery,
) -> None:

    admin_id = _callback_user_id(
        callback
    )

    if not is_admin(admin_id):

        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

    try:

        target_user_id = int(
            callback.data.split(
                ":",
                1,
            )[1]
        )

    except (
        ValueError,
        IndexError,
    ):

        await callback.answer(
            "Некорректный ID пользователя.",
            show_alert=True,
        )

        return

    try:

        from database import reject_user

        await reject_user(
            target_user_id
        )

    except Exception:

        logger.exception(
            "Failed to reject user %s.",
            target_user_id,
        )

        await callback.answer(
            "Ошибка отклонения пользователя.",
            show_alert=True,
        )

        return

    try:

        await bot.send_message(
            chat_id=target_user_id,
            text=(
                "❌ <b>Доступ не одобрен.</b>\n\n"
                "Если это ошибка, обратись к администратору."
            ),
        )

    except Exception:

        logger.exception(
            "Failed to notify rejected user %s.",
            target_user_id,
        )

    await callback.message.edit_text(
        (
            "❌ <b>Пользователь отклонён.</b>\n\n"
            f"ID: <code>{target_user_id}</code>"
        )
    )

    await callback.answer(
        "Пользователь отклонён."
    )


# =========================================================
# ADMIN /USERS
# =========================================================

@dp.message(
    Command("users")
)
async def users_command(
    message: Message,
) -> None:

    user_id = _user_id(
        message
    )

    if not is_admin(user_id):

        await message.answer(
            "Нет доступа."
        )

        return

    try:

        users = await get_approved_users()

    except Exception:

        logger.exception(
            "Failed to load approved users."
        )

        await message.answer(
            "Не удалось загрузить пользователей."
        )

        return

    await message.answer(
        (
            "👥 <b>Одобренные пользователи</b>\n\n"
            f"Количество: <b>{len(users)}</b>"
        )
    )


# =========================================================
# UNKNOWN TEXT
# =========================================================

@dp.message()
async def unknown_message(
    message: Message,
) -> None:

    user_id = _user_id(
        message
    )

    try:

        user = await get_user(
            user_id
        )

    except Exception:

        user = None

    status = None

    if isinstance(
        user,
        dict,
    ):
        status = user.get(
            "status"
        )

    elif user is not None:
        status = getattr(
            user,
            "status",
            None,
        )

    approved = (
        str(status).upper()
        == "APPROVED"
        or is_admin(user_id)
    )

    if approved:

        await message.answer(
            "Выбери действие:",
            reply_markup=main_keyboard(),
        )

    else:

        await message.answer(
            "Доступ ещё не одобрен.",
            reply_markup=pending_keyboard(),
        )


# =========================================================
# FASTAPI LIFESPAN
# =========================================================

@asynccontextmanager
async def lifespan(
    app: FastAPI,
):
    logger.info(
        "Starting application..."
    )

    scheduler_task: asyncio.Task[Any] | None = None
    polling_task: asyncio.Task[Any] | None = None

    try:

        # -------------------------------------------------
        # START SCHEDULER
        # -------------------------------------------------

        await scheduler.start()

        logger.info(
            "Signal scheduler started."
        )

        # -------------------------------------------------
        # START TELEGRAM POLLING
        # -------------------------------------------------

        polling_task = asyncio.create_task(
            dp.start_polling(
                bot,
                handle_signals=False,
                allowed_updates=dp.resolve_used_update_types(),
            ),
            name="telegram_polling",
        )

        logger.info(
            "Telegram polling started."
        )

        yield

    finally:

        logger.info(
            "Stopping application..."
        )

        # -------------------------------------------------
        # STOP POLLING
        # -------------------------------------------------

        if polling_task is not None:

            try:

                await bot.session.close()

            except Exception:

                logger.exception(
                    "Failed to close bot session."
                )

        # -------------------------------------------------
        # STOP SCHEDULER
        # -------------------------------------------------

        try:

            await scheduler.stop()

        except Exception:

            logger.exception(
                "Failed to stop scheduler."
            )

        logger.info(
            "Application stopped."
        )


# =========================================================
# FASTAPI
# =========================================================

app = FastAPI(
    title="TEYZUS Signal Bot",
    version="1.0.0",
    lifespan=lifespan,
)


# =========================================================
# HEALTH
# =========================================================

@app.get("/")
async def root() -> dict[str, Any]:

    return {
        "status": "ok",
        "service": "TEYZUS Signal Bot",
        "scheduler_running": (
            scheduler.running
        ),
        "scheduler_started": (
            scheduler.started
        ),
    }


@app.get("/health")
async def health() -> dict[str, Any]:

    return {
        "status": "healthy",
        "scheduler": scheduler.get_stats(),
    }


@app.get("/stats")
async def stats() -> dict[str, Any]:

    return scheduler.get_stats()


# =========================================================
# DIRECT RUN
# =========================================================

if __name__ == "__main__":

    import uvicorn

    port = int(
        os.getenv(
            "PORT",
            "8000",
        )
    )

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
    )
