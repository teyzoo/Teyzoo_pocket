from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from database import (
    get_pending_signals,
    update_signal_result,
)
from market import (
    Candle,
    MarketClient,
    MarketDataError,
    MarketRateLimitError,
)
from models import Direction


logger = logging.getLogger(
    "signal_result_checker"
)


# =========================================================
# RESULT
# =========================================================


@dataclass(slots=True)
class ResultCheck:
    signal_id: int
    status: str
    exit_price: float | None
    reason: str


# =========================================================
# CHECKER
# =========================================================


class SignalResultChecker:
    """
    Проверяет завершившиеся PENDING-сигналы.

    Основное правило:

        UP:
            exit > entry -> WON
            exit < entry -> LOST
            equal         -> DRAW

        DOWN:
            exit < entry -> WON
            exit > entry -> LOST
            equal         -> DRAW

    Время закрытия:

        1–20 минут.

    Если close_time хранится как полноценный ISO
    timestamp — используется он.

    Если старый формат close_time не содержит даты,
    используется:

        created_at + SIGNAL_EXPIRY_MINUTES

    Это позволяет не закрывать сигнал раньше времени.
    """

    def __init__(
        self,
        market: MarketClient,
        timeframe: str = "1m",
        candle_limit: int = 50,
        expiry_minutes: int = 5,
    ) -> None:
        self.market = market

        self.timeframe = timeframe

        self.candle_limit = max(
            20,
            int(candle_limit),
        )

        self.expiry_minutes = max(
            1,
            min(
                20,
                int(expiry_minutes),
            ),
        )

    # =====================================================
    # DATETIME
    # =====================================================

    @staticmethod
    def _ensure_utc(
        value: datetime,
    ) -> datetime:
        if value.tzinfo is None:
            return value.replace(
                tzinfo=timezone.utc
            )

        return value.astimezone(
            timezone.utc
        )

    @classmethod
    def _parse_expiry(
        cls,
        value,
    ) -> datetime | None:
        """
        Поддерживает ISO timestamp.

        Примеры:

            2026-09-03T12:30:00+00:00
            2026-09-03T12:30:00Z
        """

        if value is None:
            return None

        text = str(value).strip()

        if not text:
            return None

        try:
            parsed = datetime.fromisoformat(
                text.replace(
                    "Z",
                    "+00:00",
                )
            )

        except ValueError:
            return None

        return cls._ensure_utc(
            parsed
        )

    @classmethod
    def _resolve_expiry(
        cls,
        signal,
        fallback_minutes: int,
    ) -> datetime | None:
        """
        Определяет реальное время закрытия сигнала.

        Приоритет:

        1. signal.close_time как ISO timestamp.
        2. signal.expiry_time / expiration_time,
           если такие поля есть.
        3. signal.created_at + expiry_minutes.
        """

        # -------------------------------------------------
        # CLOSE TIME
        # -------------------------------------------------

        close_time = getattr(
            signal,
            "close_time",
            None,
        )

        parsed = cls._parse_expiry(
            close_time
        )

        if parsed is not None:
            return parsed

        # -------------------------------------------------
        # OPTIONAL EXPIRY FIELDS
        # -------------------------------------------------

        for field_name in (
            "expiry_time",
            "expiration_time",
            "expires_at",
        ):
            value = getattr(
                signal,
                field_name,
                None,
            )

            parsed = cls._parse_expiry(
                value
            )

            if parsed is not None:
                return parsed

        # -------------------------------------------------
        # CREATED AT + EXPIRY
        # -------------------------------------------------

        created_at = getattr(
            signal,
            "created_at",
            None,
        )

        if isinstance(
            created_at,
            datetime,
        ):
            created_at = cls._ensure_utc(
                created_at
            )

            minutes = max(
                1,
                min(
                    20,
                    int(fallback_minutes),
                ),
            )

            return (
                created_at
                + timedelta(
                    minutes=minutes
                )
            )

        return None

    # =====================================================
    # MARKET
    # =====================================================

    async def _get_last_candle(
        self,
        symbol: str,
    ) -> Candle:
        candles = await self.market.get_candles(
            symbol=symbol,
            timeframe=self.timeframe,
            limit=self.candle_limit,
        )

        if not candles:
            raise MarketDataError(
                f"No candles for {symbol}."
            )

        return candles[-1]

    # =====================================================
    # STATUS
    # =====================================================

    @staticmethod
    def _calculate_status(
        direction: str,
        entry_price: float,
        exit_price: float,
    ) -> tuple[str, str]:
        normalized = str(
            direction
        ).upper()

        # -------------------------------------------------
        # DRAW
        # -------------------------------------------------

        if exit_price == entry_price:
            return (
                "DRAW",
                "Цена закрытия равна цене входа.",
            )

        # -------------------------------------------------
        # UP
        # -------------------------------------------------

        if normalized == Direction.UP.value:
            if exit_price > entry_price:
                return (
                    "WON",
                    "Цена после сигнала выросла.",
                )

            return (
                "LOST",
                "Цена после сигнала снизилась.",
            )

        # -------------------------------------------------
        # DOWN
        # -------------------------------------------------

        if normalized == Direction.DOWN.value:
            if exit_price < entry_price:
                return (
                    "WON",
                    "Цена после сигнала снизилась.",
                )

            return (
                "LOST",
                "Цена после сигнала выросла.",
            )

        # -------------------------------------------------
        # UNKNOWN
        # -------------------------------------------------

        return (
            "CANCELLED",
            f"Неизвестное направление: {direction}.",
        )

    # =====================================================
    # CHECK SIGNAL
    # =====================================================

    async def check_signal(
        self,
        signal,
    ) -> ResultCheck | None:
        """
        Проверяет один сигнал.

        Пока время закрытия не наступило,
        сигнал остаётся PENDING.
        """

        if signal.status != "PENDING":
            return None

        # -------------------------------------------------
        # ENTRY PRICE
        # -------------------------------------------------

        if signal.entry_price is None:
            reason = (
                "У сигнала отсутствует "
                "entry_price."
            )

            await update_signal_result(
                signal_id=signal.id,
                status="CANCELLED",
                exit_price=None,
                reason=reason,
            )

            return ResultCheck(
                signal_id=signal.id,
                status="CANCELLED",
                exit_price=None,
                reason=reason,
            )

        # -------------------------------------------------
        # EXPIRY
        # -------------------------------------------------

        expiry = self._resolve_expiry(
            signal=signal,
            fallback_minutes=self.expiry_minutes,
        )

        now = datetime.now(
            timezone.utc
        )

        # Если время закрытия известно,
        # НЕЛЬЗЯ закрывать сигнал раньше него.
        if expiry is not None and now < expiry:
            return None

        # -------------------------------------------------
        # MARKET PRICE
        # -------------------------------------------------

        candle = await self._get_last_candle(
            signal.symbol
        )

        exit_price = float(
            candle.close
        )

        # -------------------------------------------------
        # RESULT
        # -------------------------------------------------

        status, reason = (
            self._calculate_status(
                direction=signal.direction,
                entry_price=float(
                    signal.entry_price
                ),
                exit_price=exit_price,
            )
        )

        # -------------------------------------------------
        # DATABASE
        # -------------------------------------------------

        updated = await update_signal_result(
            signal_id=signal.id,
            status=status,
            exit_price=exit_price,
            reason=reason,
        )

        if not updated:
            logger.warning(
                "Signal #%s disappeared "
                "during result update.",
                signal.id,
            )

            return None

        return ResultCheck(
            signal_id=signal.id,
            status=status,
            exit_price=exit_price,
            reason=reason,
        )

    # =====================================================
    # CHECK ALL
    # =====================================================

    async def check_once(
        self,
    ) -> list[ResultCheck]:
        pending = await get_pending_signals()

        if not pending:
            return []

        results: list[ResultCheck] = []

        for signal in pending:
            try:
                result = await self.check_signal(
                    signal
                )

                if result is not None:
                    results.append(result)

            except MarketRateLimitError:
                logger.warning(
                    "Market rate limit while "
                    "checking signal #%s.",
                    signal.id,
                )

                break

            except MarketDataError as exc:
                logger.warning(
                    "Market error for signal #%s: %s",
                    signal.id,
                    exc,
                )

            except Exception:
                logger.exception(
                    "Failed to check signal #%s.",
                    signal.id,
                )

        return results


__all__ = [
    "ResultCheck",
    "SignalResultChecker",
]
