from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import PAIRS


def main_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="🎯 Получить сигнал",
            callback_data="request_signal",
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="📊 История",
            callback_data="history",
        )
    )

    return builder.as_markup()


def pending_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="🔄 Проверить доступ",
            callback_data="check_access",
        )
    )

    return builder.as_markup()


def pair_selection_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for pair in PAIRS:
        builder.row(
            InlineKeyboardButton(
                text=f"💱 {pair}",
                callback_data=f"pair:{pair}",
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="🔀 Любая пара",
            callback_data="pair:any",
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="pair:cancel",
        )
    )

    return builder.as_markup()


def admin_request_keyboard(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="✅ Одобрить",
            callback_data=f"approve:{user_id}",
        ),
        InlineKeyboardButton(
            text="❌ Отклонить",
            callback_data=f"reject:{user_id}",
        ),
    )

    return builder.as_markup()
