import asyncio
from typing import Optional

import aiohttp
import pandas as pd

from config import (
    MIN_CANDLES,
    TIMEFRAME,
    TWELVE_DATA_API_KEY,
)


TWELVE_DATA_URL = (
    "https://api.twelvedata.com/time_series"
)


class MarketClient:
    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(
            total=20
        )


    async def get_candles(
        self,
        pair: str,
        interval: str = TIMEFRAME,
        outputsize: int = 200,
    ) -> Optional[pd.DataFrame]:

        params = {
            "symbol": pair,
            "interval": interval,
            "outputsize": outputsize,
            "apikey": TWELVE_DATA_API_KEY,
            "format": "JSON",
        }

        try:
            async with aiohttp.ClientSession(
                timeout=self.timeout
            ) as session:

                async with session.get(
                    TWELVE_DATA_URL,
                    params=params,
                ) as response:

                    if response.status != 200:
                        return None

                    data = await response.json()

        except (
            asyncio.TimeoutError,
            aiohttp.ClientError,
            ValueError,
        ):
            return None

        if not isinstance(data, dict):
            return None

        if "values" not in data:
            return None

        values = data["values"]

        if not isinstance(values, list):
            return None

        if len(values) < MIN_CANDLES:
            return None

        try:
            dataframe = pd.DataFrame(values)

            dataframe["datetime"] = pd.to_datetime(
                dataframe["datetime"],
                utc=True,
            )

            for column in (
                "open",
                "high",
                "low",
                "close",
            ):
                dataframe[column] = pd.to_numeric(
                    dataframe[column],
                    errors="coerce",
                )

            dataframe = dataframe.dropna(
                subset=[
                    "open",
                    "high",
                    "low",
                    "close",
                ]
            )

            dataframe = dataframe.sort_values(
                "datetime"
            )

            dataframe = dataframe.reset_index(
                drop=True
            )

            if len(dataframe) < MIN_CANDLES:
                return None

            return dataframe

        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            return None
