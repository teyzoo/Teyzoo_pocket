from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)


def main_menu() -> InlineKeyboardMarkup:
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


def admin_request(
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


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Главное меню",
                    callback_data="back_menu",
                )
            ]
        ]
    )
