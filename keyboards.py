from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎯 Получить сигнал",
                    callback_data="request_signal",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 История",
                    callback_data="history",
                )
            ],
        ]
    )


def pending_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Проверить доступ",
                    callback_data="check_access",
                )
            ]
        ]
    )


def admin_request_keyboard(
    user_id: int,
) -> InlineKeyboardMarkup:

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
