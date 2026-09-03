from __future__ import annotations

import logging

from aiogram import Bot

from database import get_signal
from signal_notifications import send_to_users


logger = logging.getLogger(
    "signal_result_notifications"
)


def _format_price(value) -> str:
    if value is None:
        return "—"

    try:
        number = float(value)

        if number >= 100:
            return f"{number:.3f}"

        if number >= 1:
            return f"{number:.5f}"

        return f"{number:.6f}"

    except (TypeError, ValueError):
        return str(value)


def _get_score(signal) -> float | None:
    """
    Совместимость со старыми и новыми моделями.
    """

    for field in (
        "score",
        "quality_score",
        "probability",
    ):
        value = getattr(
            signal,
            field,
            None,
        )

        if value is None:
            continue

        try:
            return float(value)
        except (TypeError, ValueError):
            continue

    return None


def _get_direction_text(signal) -> str:
    direction = str(
        getattr(
            signal,
            "direction",
            "",
        )
    ).upper()

    if direction in {
        "UP",
        "CALL",
    }:
        return "🟢 CALL / UP"

    if direction in {
        "DOWN",
        "PUT",
    }:
        return "🔴 PUT / DOWN"

    return direction or "—"


def _get_close_time(signal) -> str | None:
    for field in (
        "close_time",
        "expiry_time",
        "expiration_time",
        "expires_at",
    ):
        value = getattr(
            signal,
            field,
            None,
        )

        if value is None:
            continue

        text = str(value).strip()

        if not text:
            continue

        # ISO timestamp -> оставляем только дату/время
        if "T" in text:
            try:
                from datetime import datetime

                parsed = datetime.fromisoformat(
                    text.replace(
                        "Z",
                        "+00:00",
                    )
                )

                return parsed.strftime(
                    "%H:%M"
                ) + " МСК"

            except ValueError:
                pass

        return text

    return None


async def notify_signal_result(
    bot: Bot,
    signal_id: int,
) -> None:
    """
    Отправляет пользователям результат завершившегося сигнала.

    WON   -> WIN
    LOST  -> LOSS
    DRAW  -> DRAW
    CANCELLED -> отдельное уведомление

    Если результат неизвестен, ничего не отправляем.
    """

    try:
        signal = await get_signal(
            signal_id
        )

    except Exception:
        logger.exception(
            "Failed to load signal #%s.",
            signal_id,
        )
        return

    if signal is None:
        logger.warning(
            "Signal #%s not found.",
            signal_id,
        )
        return

    status = str(
        getattr(
            signal,
            "status",
            "",
        )
    ).upper()

    symbol = getattr(
        signal,
        "symbol",
        "—",
    )

    direction = _get_direction_text(
        signal
    )

    entry_price = _format_price(
        getattr(
            signal,
            "entry_price",
            None,
        )
    )

    exit_price = _format_price(
        getattr(
            signal,
            "exit_price",
            None,
        )
    )

    score = _get_score(
        signal
    )

    close_time = _get_close_time(
        signal
    )

    if score is not None:
        score_text = (
            f"{score:.1f}%"
        )
    else:
        score_text = "—"

    close_text = ""

    if close_time:
        close_text = (
            f"\n⏰ Закрытие: {close_time}\n"
        )

    # =====================================================
    # WIN
    # =====================================================

    if status == "WON":
        text = (
            "🟢 <b>СИГНАЛ ЗАКРЫТ — WIN</b>\n\n"
            f"💱 Пара: <b>{symbol}</b>\n"
            f"📈 Направление: <b>{direction}</b>\n"
            f"{close_text}\n"
            f"💰 Вход: <b>{entry_price}</b>\n"
            f"💰 Выход: <b>{exit_price}</b>\n\n"
            f"🎯 Quality Score: <b>{score_text}</b>\n\n"
            "✅ Сделка закрылась в нужном направлении."
        )

    # =====================================================
    # LOSS
    # =====================================================

    elif status == "LOST":
        text = (
            "🔴 <b>СИГНАЛ ЗАКРЫТ — LOSS</b>\n\n"
            f"💱 Пара: <b>{symbol}</b>\n"
            f"📉 Направление: <b>{direction}</b>\n"
            f"{close_text}\n"
            f"💰 Вход: <b>{entry_price}</b>\n"
            f"💰 Выход: <b>{exit_price}</b>\n\n"
            f"🎯 Quality Score: <b>{score_text}</b>\n\n"
            "❌ Цена пошла против направления сигнала."
        )

    # =====================================================
    # DRAW
    # =====================================================

    elif status == "DRAW":
        text = (
            "⚪ <b>СИГНАЛ ЗАКРЫТ — DRAW</b>\n\n"
            f"💱 Пара: <b>{symbol}</b>\n"
            f"📊 Направление: <b>{direction}</b>\n"
            f"{close_text}\n"
            f"💰 Вход: <b>{entry_price}</b>\n"
            f"💰 Выход: <b>{exit_price}</b>\n\n"
            f"🎯 Quality Score: <b>{score_text}</b>\n\n"
            "➖ Цена практически не изменилась."
        )

    # =====================================================
    # CANCELLED
    # =====================================================

    elif status == "CANCELLED":
        reason = getattr(
            signal,
            "result_reason",
            None,
        )

        if not reason:
            reason = getattr(
                signal,
                "reason",
                None,
            )

        reason_text = (
            str(reason)
            if reason
            else "Причина отмены не указана."
        )

        text = (
            "⚪ <b>СИГНАЛ ОТМЕНЁН</b>\n\n"
            f"💱 Пара: <b>{symbol}</b>\n"
            f"📊 Направление: <b>{direction}</b>\n\n"
            f"ℹ️ {reason_text}"
        )

    else:
        logger.debug(
            "Signal #%s has unsupported result status: %s",
            signal_id,
            status,
        )
        return

    try:
        await send_to_users(
            bot,
            text,
        )

        logger.info(
            "Result notification sent for signal #%s: %s",
            signal_id,
            status,
        )

    except Exception:
        logger.exception(
            "Failed to send result notification "
            "for signal #%s.",
            signal_id,
        )


__all__ = [
    "notify_signal_result",
]
