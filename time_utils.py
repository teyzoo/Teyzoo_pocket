from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


MOSCOW = ZoneInfo("Europe/Moscow")


def now_moscow() -> datetime:
    return datetime.now(MOSCOW)


def ensure_moscow(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=MOSCOW)

    return value.astimezone(MOSCOW)


def next_20_minute_mark(
    now: datetime | None = None,
) -> datetime:
    if now is None:
        now = now_moscow()

    now = ensure_moscow(now)

    minute = now.minute

    next_block = ((minute // 20) + 1) * 20

    if next_block >= 60:
        return (
            now.replace(
                minute=0,
                second=0,
                microsecond=0,
            )
            + timedelta(hours=1)
        )

    return now.replace(
        minute=next_block,
        second=0,
        microsecond=0,
    )


def signal_warning_time(
    signal_time: datetime,
    warning_minutes: int = 2,
) -> datetime:
    return (
        ensure_moscow(signal_time)
        - timedelta(minutes=warning_minutes)
    )


def signal_expiry_time(
    signal_time: datetime,
    expiry_minutes: int,
) -> datetime:
    """
    Возвращает время закрытия сигнала.

    expiry_minutes:
        Разрешённый диапазон — 1–20 минут.
    """
    minutes = max(1, min(20, int(expiry_minutes)))

    return (
        ensure_moscow(signal_time)
        + timedelta(minutes=minutes)
    )


def clamp_expiry_minutes(
    expiry_minutes: int | float,
    minimum: int = 1,
    maximum: int = 20,
) -> int:
    """
    Безопасно ограничивает время сигнала диапазоном 1–20 минут.
    """
    minimum = max(1, int(minimum))
    maximum = min(20, max(minimum, int(maximum)))

    return max(
        minimum,
        min(maximum, int(expiry_minutes)),
    )


def format_moscow_time(
    value: datetime,
) -> str:
    value = ensure_moscow(value)

    return (
        value.strftime("%H:%M")
        + " МСК"
    )


def format_moscow_datetime(
    value: datetime,
) -> str:
    value = ensure_moscow(value)

    return value.strftime(
        "%d.%m.%Y %H:%M МСК"
    )


def parse_moscow_time(
    value: str,
    reference: datetime | None = None,
) -> datetime:
    if reference is None:
        reference = now_moscow()

    reference = ensure_moscow(reference)

    parsed = datetime.strptime(
        value.strip(),
        "%H:%M МСК",
    )

    return parsed.replace(
        year=reference.year,
        month=reference.month,
        day=reference.day,
        tzinfo=MOSCOW,
    )


__all__ = [
    "MOSCOW",
    "now_moscow",
    "ensure_moscow",
    "next_20_minute_mark",
    "signal_warning_time",
    "signal_expiry_time",
    "clamp_expiry_minutes",
    "format_moscow_time",
    "format_moscow_datetime",
    "parse_moscow_time",
]
