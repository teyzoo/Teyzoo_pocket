import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    Message,
)
from fastapi import FastAPI

from config import (
    ADMIN_ID,
    BOT_TOKEN,
)
from database import Database
from keyboards import (
    admin_request,
    main_menu,
)
from market import MarketClient
from scheduler import SignalScheduler
from signal_engine import SignalEngine


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger(
    "pocket_signal_bot"
)


# ============================================================
# BOT
# ============================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    ),
)

dp = Dispatcher()

app = FastAPI(
    title="Pocket Signal Bot"
)


# ============================================================
# SERVICES
# ============================================================

database = Database()

market = MarketClient()

engine = SignalEngine()

scheduler = SignalScheduler(
    bot=bot,
    database=database,
    market=market,
    engine=engine,
)


scheduler_task = None


# ============================================================
# ACCESS HELPERS
# ============================================================

async def require_approved(
    callback: CallbackQuery,
) -> bool:

    user_id = callback.from_user.id

    status = database.get_status(
        user_id
    )

    if status != "approved":

        await callback.answer(
            "🔒 Сначала дождитесь одобрения.",
            show_alert=True,
        )

        return False

    return True


# ============================================================
# START
# ============================================================

@dp.message(CommandStart())
async def start_handler(
    message: Message,
) -> None:

    user = message.from_user

    if user is None:
        return

    user_id = user.id

    existing = database.get_user(
        user_id
    )

    # --------------------------------------------------------
    # APPROVED
    # --------------------------------------------------------

    if existing is not None:

        status = existing["status"]

        if status == "approved":

            await message.answer(
                "🤖 <b>Pocket Signal Bot</b>\n\n"
                "Доступ подтверждён.\n\n"
                "Выберите действие:",
                reply_markup=main_menu(),
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
                "❌ <b>Ваша заявка отклонена.</b>"
            )

            return

        if status == "blocked":

            await message.answer(
                "🚫 <b>Ваш доступ заблокирован.</b>"
            )

            return


    # --------------------------------------------------------
    # NEW USER
    # --------------------------------------------------------

    username = (
        f"@{user.username}"
        if user.username
        else "отсутствует"
    )

    full_name = (
        user.full_name
        or "Не указано"
    )

    database.create_user(
        user_id=user_id,
        username=username,
        full_name=full_name,
    )

    await message.answer(
        "🔒 <b>Доступ закрыт</b>\n\n"
        "Для использования бота необходимо "
        "отправить заявку.\n\n"
        "После проверки администратор "
        "решит, предоставить ли доступ."
    )

    admin_message = (
        "🔔 <b>НОВАЯ ЗАЯВКА</b>\n\n"
        f"👤 Имя: {full_name}\n"
        f"🔗 Username: {username}\n"
        f"🆔 ID: <code>{user_id}</code>"
    )

    try:

        await bot.send_message(
            ADMIN_ID,
            admin_message,
            reply_markup=admin_request(
                user_id
            ),
        )

    except Exception:

        logger.exception(
            "Не удалось отправить заявку админу."
        )


# ============================================================
# APPROVE
# ============================================================

@dp.callback_query(
    F.data.startswith("approve:")
)
async def approve_handler(
    callback: CallbackQuery,
) -> None:

    if callback.from_user.id != ADMIN_ID:

        await callback.answer(
            "⛔ Нет доступа.",
            show_alert=True,
        )

        return

    try:

        user_id = int(
            callback.data.split(
                ":",
                1
            )[1]
        )

    except (
        ValueError,
        AttributeError,
    ):

        await callback.answer(
            "Некорректная заявка.",
            show_alert=True,
        )

        return

    database.update_user_status(
        user_id,
        "approved",
    )

    try:

        await bot.send_message(
            user_id,
            "✅ <b>Заявка одобрена!</b>\n\n"
            "Теперь тебе доступен бот.",
            reply_markup=main_menu(),
        )

    except Exception:

        logger.exception(
            "Не удалось уведомить пользователя."
        )

    try:

        await callback.message.edit_reply_markup(
            reply_markup=None
        )

    except Exception:

        pass

    await callback.message.answer(
        f"✅ Пользователь "
        f"<code>{user_id}</code> одобрен."
    )

    await callback.answer(
        "Пользователь одобрен."
    )


# ============================================================
# REJECT
# ============================================================

@dp.callback_query(
    F.data.startswith("reject:")
)
async def reject_handler(
    callback: CallbackQuery,
) -> None:

    if callback.from_user.id != ADMIN_ID:

        await callback.answer(
            "⛔ Нет доступа.",
            show_alert=True,
        )

        return

    try:

        user_id = int(
            callback.data.split(
                ":",
                1
            )[1]
        )

    except (
        ValueError,
        AttributeError,
    ):

        await callback.answer(
            "Некорректная заявка.",
            show_alert=True,
        )

        return

    database.update_user_status(
        user_id,
        "rejected",
    )

    try:

        await bot.send_message(
            user_id,
            "❌ <b>Заявка отклонена.</b>"
        )

    except Exception:

        logger.exception(
            "Не удалось уведомить пользователя."
        )

    try:

        await callback.message.edit_reply_markup(
            reply_markup=None
        )

    except Exception:

        pass

    await callback.message.answer(
        f"❌ Пользователь "
        f"<code>{user_id}</code> отклонён."
    )

    await callback.answer(
        "Заявка отклонена."
    )


# ============================================================
# MANUAL SIGNAL
# ============================================================

@dp.callback_query(
    F.data == "request_signal"
)
async def request_signal_handler(
    callback: CallbackQuery,
) -> None:

    if not await require_approved(
        callback
    ):
        return

    user_id = callback.from_user.id

    database.save_signal_request(
        user_id
    )

    await callback.answer(
        "🔎 Анализирую рынок..."
    )

    status_message = await callback.message.answer(
        "🔎 <b>Анализирую рынок...</b>\n\n"
        "Проверяю валютные пары и "
        "ищу ближайшую качественную точку входа."
    )

    try:

        signal = (
            await scheduler.find_best_signal()
        )

        if signal is None:

            await status_message.edit_text(
                "⛔ <b>СИЛЬНОГО СИГНАЛА НЕТ</b>\n\n"
                "Сейчас рынок не прошёл "
                "минимальный фильтр качества.\n\n"
                "Попробуйте запросить сигнал позже."
            )

            return

        direction = (
            "🟢 CALL ↑"
            if signal.direction == "CALL"
            else "🔴 PUT ↓"
        )

        confirmations = []

        for name, value in (
            signal.confirmations.items()
        ):

            mark = (
                "🟢"
                if value == "CALL"
                else "🔴"
            )

            confirmations.append(
                f"{mark} {name}"
            )

        confirmation_text = "\n".join(
            confirmations
        )

        text = (
            f"<b>{direction}</b>\n\n"
            f"💱 <b>{signal.pair}</b>\n\n"
            f"⏰ <b>ВХОД:</b> "
            f"{signal.entry_time.strftime('%H:%M')} МСК\n"
            f"🎯 <b>ЭКСПИРАЦИЯ:</b> "
            f"{signal.expiry_time.strftime('%H:%M')} МСК\n\n"
            f"📊 <b>QUALITY:</b> "
            f"{signal.quality}/100\n\n"
            f"<b>Подтверждения:</b>\n"
            f"{confirmation_text}\n\n"
            f"⚠️ Это аналитический сигнал, "
            f"а не гарантия результата."
        )

        database.save_signal(
            pair=signal.pair,
            direction=signal.direction,
            entry_time=signal.entry_time.isoformat(),
            expiry_time=signal.expiry_time.isoformat(),
            quality=signal.quality,
            score_details=text,
        )

        await status_message.edit_text(
            text
        )

    except Exception:

        logger.exception(
            "Ошибка ручного анализа."
        )

        await status_message.edit_text(
            "⚠️ <b>Не удалось выполнить анализ.</b>\n\n"
            "Попробуйте ещё раз через некоторое время."
        )


# ============================================================
# AUTO SIGNALS BUTTON
# ============================================================

@dp.callback_query(
    F.data == "auto_signals"
)
async def auto_signals_handler(
    callback: CallbackQuery,
) -> None:

    if not await require_approved(
        callback
    ):
        return

    await callback.answer()

    await callback.message.answer(
        "🤖 <b>Автоматические сигналы</b>\n\n"
        "Бот самостоятельно анализирует рынок "
        "и отправляет сильные сигналы "
        "одобренным пользователям.\n\n"
        "⏰ Время указывается по МСК (UTC+3)."
    )


# ============================================================
# STATISTICS
# ============================================================

@dp.callback_query(
    F.data == "statistics"
)
async def statistics_handler(
    callback: CallbackQuery,
) -> None:

    if not await require_approved(
        callback
    ):
        return

    stats = (
        database.get_signal_stats()
    )

    await callback.answer()

    await callback.message.answer(
        "📊 <b>СТАТИСТИКА</b>\n\n"
        f"📨 Всего сигналов: "
        f"<b>{stats['total']}</b>\n"
        f"✅ WIN: "
        f"<b>{stats['wins']}</b>\n"
        f"❌ LOSS: "
        f"<b>{stats['losses']}</b>\n"
        f"📈 Завершено: "
        f"<b>{stats['finished']}</b>\n"
        f"🎯 WINRATE: "
        f"<b>{stats['winrate']:.2f}%</b>\n\n"
        "WINRATE считается только по "
        "фактически отмеченным результатам."
    )


# ============================================================
# BACK MENU
# ============================================================

@dp.callback_query(
    F.data == "back_menu"
)
async def back_menu_handler(
    callback: CallbackQuery,
) -> None:

    if not await require_approved(
        callback
    ):
        return

    await callback.message.answer(
        "Главное меню:",
        reply_markup=main_menu(),
    )

    await callback.answer()


# ============================================================
# UNKNOWN CALLBACK
# ============================================================

@dp.callback_query()
async def unknown_callback(
    callback: CallbackQuery,
) -> None:

    status = database.get_status(
        callback.from_user.id
    )

    if status != "approved":

        await callback.answer(
            "🔒 Доступ не одобрен.",
            show_alert=True,
        )

        return

    await callback.answer(
        "Функция пока недоступна."
    )


# ============================================================
# FASTAPI
# ============================================================

@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "pocket-signal-bot",
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
    }


# ============================================================
# STARTUP
# ============================================================

async def start_scheduler() -> None:

    global scheduler_task

    scheduler_task = asyncio.create_task(
        scheduler.run()
    )

    logger.info(
        "Signal scheduler task created."
    )


# ============================================================
# BOT
# ============================================================

async def run_bot() -> None:

    logger.info(
        "Starting Telegram bot..."
    )

    await start_scheduler()

    await dp.start_polling(
        bot,
        allowed_updates=(
            dp.resolve_used_update_types()
        ),
        handle_signals=True,
    )


# ============================================================
# MAIN
# ============================================================

async def main() -> None:

    try:

        await run_bot()

    finally:

        global scheduler_task

        if scheduler_task is not None:

            scheduler_task.cancel()

            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass

        database.close()

        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
