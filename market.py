import asyncio
import logging
from typing import Optional

import aiohttp
import pandas as pd

from config import (
    TWELVE_DATA_API_KEY,
    CANDLE_INTERVAL,
    CANDLE_LIMIT,
)

logger = logging.getLogger(__name__)

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"


class MarketClient:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=20)

            self.session = aiohttp.ClientSession(
                timeout=timeout
            )

        return self.session

    async def get_candles(
        self,
        symbol: str,
    ) -> Optional[pd.DataFrame]:

        session = await self._get_session()

        params = {
            "symbol": symbol,
            "interval": CANDLE_INTERVAL,
            "outputsize": CANDLE_LIMIT,
            "apikey": TWELVE_DATA_API_KEY,
            "format": "JSON",
        }

        try:
            async with session.get(
                TWELVE_DATA_URL,
                params=params,
            ) as response:

                data = await response.json(
                    content_type=None
                )

                if response.status != 200:
                    logger.warning(
                        "Market API HTTP %s: %s",
                        response.status,
                        data,
                    )
                    return None

                if "status" in data and data["status"] == "error":
                    logger.warning(
                        "Market API error for %s: %s",
                        symbol,
                        data.get("message"),
                    )
                    return None

                values = data.get("values")

                if not values:
                    logger.warning(
                        "Нет свечей для %s",
                        symbol,
                    )
                    return None

                df = pd.DataFrame(values)

                required = {
                    "datetime",
                    "open",
                    "high",
                    "low",
                    "close",
                }

                if not required.issubset(df.columns):
                    logger.warning(
                        "Неполный ответ API для %s",
                        symbol,
                    )
                    return None

                df["datetime"] = pd.to_datetime(
                    df["datetime"],
                    utc=True,
                    errors="coerce",
                )

                for column in [
                    "open",
                    "high",
                    "low",
                    "close",
                ]:
                    df[column] = pd.to_numeric(
                        df[column],
                        errors="coerce",
                    )

                df = df.dropna(
                    subset=[
                        "datetime",
                        "open",
                        "high",
                        "low",
                        "close",
                    ]
                )

                df = df.sort_values(
                    "datetime"
                ).reset_index(drop=True)

                if len(df) < 80:
                    logger.warning(
                        "Недостаточно свечей %s: %s",
                        symbol,
                        len(df),
                    )
                    return None

                return df

        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            ValueError,
        ) as exc:

            logger.warning(
                "Ошибка получения рынка %s: %s",
                symbol,
                exc,
            )

            return None

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()


market_client = MarketClient()
