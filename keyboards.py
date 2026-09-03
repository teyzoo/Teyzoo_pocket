from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import PAIRS


# ============================================================
# OTC PAIRS
# ============================================================

OTC_PAIRS = [
    "EURUSD_otc",
    "GBPUSD_otc",
    "USDJPY_otc",
    "USDCHF_otc",
    "AUDUSD_otc",
    "USDCAD_otc",
    "NZDUSD_otc",
    "EURGBP_otc",
    "EURJPY_otc",
    "GBPJPY_otc",
    "AUDCAD_otc",
    "AUDCHF_otc",
    "AUDJPY_otc",
    "CADCHF_otc",
    "CADJPY_otc",
    "CHFJPY_otc",
    "EURAUD_otc",
    "EURCAD_otc",
    "EURCHF_otc",
    "EURNZD_otc",
    "GBPAUD_otc",
    "GBPCAD_otc",
    "GBPCHF_otc",
    "GBPNZD_otc",
    "NZDCAD_otc",
    "NZDCHF_otc",
    "NZDJPY_otc",
]


# ============================================================
# EXPIRY SETTINGS
# ============================================================

MIN_EXPIRY_MINUTES = 1
MAX_EXPIRY_MINUTES = 20


# ============================================================
# HELPERS
# ============================================================

def format_pair(pair: str) -> str:
    """
    Преобразует:

        EURUSD      -> EUR/USD
        EURUSD_otc  -> EUR/USD OTC
    """

    value = str(pair).strip()

    if value.lower().endswith("_otc"):
        base = value[:-4].upper()

        if len(base) == 6:
            return f"{base[:3]}/{base[3:]} OTC"

        return f"{base} OTC"

    value = value.upper()

    if len(value) == 6 and "/" not in value:
        return f"{value[:3]}/{value[3:]}"

    return value


# ============================================================
# MAIN KEYBOARD
# ============================================================

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


# ============================================================
# PENDING KEYBOARD
# ============================================================

def pending_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="🔄 Проверить доступ",
            callback_data="check_access",
        )
    )

    return builder.as_markup()


# ============================================================
# SIGNAL TYPE SELECTION
# ============================================================

def signal_type_keyboard() -> InlineKeyboardMarkup:
    """
    Выбор типа рынка:

    - обычные пары;
    - OTC;
    - все пары.
    """

    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="💱 Обычные пары",
            callback_data="signal_type:regular",
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🟣 OTC пары",
            callback_data="signal_type:otc",
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔀 Все пары",
            callback_data="signal_type:all",
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="signal_type:cancel",
        )
    )

    return builder.as_markup()


# ============================================================
# REGULAR PAIRS
# ============================================================

def regular_pair_selection_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for pair in PAIRS:
        builder.row(
            InlineKeyboardButton(
                text=f"💱 {format_pair(pair)}",
                callback_data=f"pair:{pair}",
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="🔀 Любая обычная пара",
            callback_data="pair:any_regular",
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data="signal_type:back",
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="pair:cancel",
        )
    )

    return builder.as_markup()


# ============================================================
# OTC PAIRS
# ============================================================

def otc_pair_selection_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for pair in OTC_PAIRS:
        builder.row(
            InlineKeyboardButton(
                text=f"🟣 {format_pair(pair)}",
                callback_data=f"pair:{pair}",
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="🔀 Любая OTC пара",
            callback_data="pair:any_otc",
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data="signal_type:back",
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="pair:cancel",
        )
    )

    return builder.as_markup()


# ============================================================
# ALL PAIRS
# ============================================================

def all_pair_selection_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for pair in PAIRS:
        builder.row(
            InlineKeyboardButton(
                text=f"💱 {format_pair(pair)}",
                callback_data=f"pair:{pair}",
            )
        )

    for pair in OTC_PAIRS:
        builder.row(
            InlineKeyboardButton(
                text=f"🟣 {format_pair(pair)}",
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
            text="⬅️ Назад",
            callback_data="signal_type:back",
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="pair:cancel",
        )
    )

    return builder.as_markup()


# ============================================================
# EXPIRY / TRADE DURATION KEYBOARD
# ============================================================

def expiry_selection_keyboard() -> InlineKeyboardMarkup:
    """
    Выбор времени сделки от 1 до 20 минут.

    callback:
        expiry:1
        expiry:2
        ...
        expiry:20
        expiry:any
    """

    builder = InlineKeyboardBuilder()

    for start in range(
        MIN_EXPIRY_MINUTES,
        MAX_EXPIRY_MINUTES + 1,
        4,
    ):
        row = []

        for minutes in range(start, min(start + 4, MAX_EXPIRY_MINUTES + 1)):
            row.append(
                InlineKeyboardButton(
                    text=f"{minutes} мин",
                    callback_data=f"expiry:{minutes}",
                )
            )

        builder.row(*row)

    builder.row(
        InlineKeyboardButton(
            text="⚡ Любое время",
            callback_data="expiry:any",
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад к выбору пары",
            callback_data="expiry:back",
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="expiry:cancel",
        )
    )

    return builder.as_markup()


# ============================================================
# OLD COMPATIBILITY KEYBOARD
# ============================================================

def pair_selection_keyboard() -> InlineKeyboardMarkup:
    """
    Старая функция оставлена для совместимости.
    """

    return signal_type_keyboard()


# ============================================================
# ADMIN REQUEST KEYBOARD
# ============================================================

def admin_request_keyboard(
    user_id: int,
) -> InlineKeyboardMarkup:

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
