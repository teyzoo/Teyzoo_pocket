from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp
import pandas as pd

from config import (
    ASSET_DISCOVERY_CACHE_SECONDS,
    ASSET_DISCOVERY_ENABLED,
    CANDLE_INTERVAL,
    CANDLE_LIMIT,
    FALLBACK_PAIRS,
    POCKET_OPTION_ASSETS_URL,
    TWELVE_DATA_API_KEY,
)


class MarketClient:
    """
    Клиент получения рыночных данных.

    Обычные Forex-пары:
        Twelve Data.

    Интервал из config.py может быть:
        1min
        5min
        15min
        30min
        1h
        60
        300
        и т.д.

    Важно:
        строка "5min" никогда не передаётся в int().
    """

    TWELVE_DATA_URL = (
        "https://api.twelvedata.com/time_series"
    )

    REQUEST_TIMEOUT = 15

    MIN_CANDLES = 80
    DEFAULT_LIMIT = 120

    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None

        self._asset_cache: list[str] = []
        self._asset_cache_time: float = 0.0

        self._request_lock = asyncio.Lock()

    # ============================================================
    # SESSION
    # ============================================================

    async def _get_session(self) -> aiohttp.ClientSession:
        if (
            self.session is None
            or self.session.closed
        ):
            timeout = aiohttp.ClientTimeout(
                total=self.REQUEST_TIMEOUT,
                connect=8,
                sock_read=10,
            )

            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": (
                        "Teyzoo-Pocket-Signal-Bot/1.0"
                    ),
                    "Accept": "application/json",
                },
            )

        return self.session

    async def close(self) -> None:
        if (
            self.session is not None
            and not self.session.closed
        ):
            await self.session.close()

        self.session = None

    # ============================================================
    # INTERVAL
    # ============================================================

    @staticmethod
    def interval_to_twelve_data(
        interval: Any,
    ) -> str:
        """
        Преобразование интервала в формат Twelve Data.

        Примеры:

            "5min" -> "5min"
            "5m"   -> "5min"
            300    -> "5min"
            "300"  -> "5min"
        """

        if isinstance(interval, str):
            value = interval.strip().lower()

            aliases = {
                "1m": "1min",
                "1min": "1min",
                "60": "1min",

                "5m": "5min",
                "5min": "5min",
                "300": "5min",

                "15m": "15min",
                "15min": "15min",
                "900": "15min",

                "30m": "30min",
                "30min": "30min",
                "1800": "30min",

                "45m": "45min",
                "45min": "45min",
                "2700": "45min",

                "1h": "1h",
                "60m": "1h",
                "3600": "1h",

                "2h": "2h",
                "7200": "2h",

                "4h": "4h",
                "14400": "4h",

                "8h": "8h",
                "28800": "8h",

                "1d": "1day",
                "1day": "1day",
            }

            if value in aliases:
                return aliases[value]

            if value.endswith("min"):
                number = value[:-3]

                if number.isdigit():
                    return f"{int(number)}min"

            if value.endswith("m"):
                number = value[:-1]

                if number.isdigit():
                    return f"{int(number)}min"

            if value.endswith("h"):
                number = value[:-1]

                if number.isdigit():
                    hours = int(number)

                    if hours == 1:
                        return "1h"

                    return f"{hours}h"

            if value.isdigit():
                seconds = int(value)

                seconds_map = {
                    60: "1min",
                    300: "5min",
                    900: "15min",
                    1800: "30min",
                    2700: "45min",
                    3600: "1h",
                    7200: "2h",
                    14400: "4h",
                    28800: "8h",
                }

                if seconds in seconds_map:
                    return seconds_map[seconds]

            return value

        if isinstance(interval, (int, float)):
            value = int(interval)

            seconds_map = {
                60: "1min",
                300: "5min",
                900: "15min",
                1800: "30min",
                2700: "45min",
                3600: "1h",
                7200: "2h",
                14400: "4h",
                28800: "8h",
            }

            if value in seconds_map:
                return seconds_map[value]

            if value in (
                1,
                5,
                15,
                30,
                45,
            ):
                return f"{value}min"

            return f"{value}min"

        return "5min"

    @staticmethod
    def interval_to_seconds(
        interval: Any,
    ) -> int:

        if isinstance(interval, (int, float)):
            value = int(interval)

            if value <= 45:
                return value * 60

            return value

        if isinstance(interval, str):
            value = interval.strip().lower()

            aliases = {
                "1m": 60,
                "1min": 60,
                "60": 60,

                "5m": 300,
                "5min": 300,
                "300": 300,

                "15m": 900,
                "15min": 900,
                "900": 900,

                "30m": 1800,
                "30min": 1800,
                "1800": 1800,

                "45m": 2700,
                "45min": 2700,
                "2700": 2700,

                "1h": 3600,
                "3600": 3600,

                "2h": 7200,
                "7200": 7200,

                "4h": 14400,
                "14400": 14400,

                "8h": 28800,
                "28800": 28800,

                "1d": 86400,
                "1day": 86400,
            }

            if value in aliases:
                return aliases[value]

            if value.endswith("min"):
                number = value[:-3]

                if number.isdigit():
                    return int(number) * 60

            if value.endswith("m"):
                number = value[:-1]

                if number.isdigit():
                    return int(number) * 60

            if value.endswith("h"):
                number = value[:-1]

                if number.isdigit():
                    return int(number) * 3600

            if value.isdigit():
                number = int(value)

                if number <= 45:
                    return number * 60

                return number

        return 300

    # ============================================================
    # PAIRS
    # ============================================================

    @staticmethod
    def clean_pair(
        pair: str | None,
    ) -> str:

        if not pair:
            return ""

        value = str(pair).strip().upper()

        value = value.replace("-", "/")
        value = value.replace("_", "/")

        if value.endswith("/OTC"):
            value = value[:-4] + "_OTC"

        if value.endswith(" OTC"):
            value = value[:-4] + "_OTC"

        if (
            value.endswith("OTC")
            and not value.endswith("_OTC")
        ):
            value = value[:-3] + "_OTC"

        return value

    @staticmethod
    def is_otc_pair(
        pair: str | None,
    ) -> bool:

        if not pair:
            return False

        value = str(pair).upper()

        return (
            "_OTC" in value
            or "/OTC" in value
            or " OTC" in value
        )

    @staticmethod
    def twelve_data_symbol(
        pair: str,
    ) -> str:

        value = str(pair).strip().upper()

        value = value.replace(
            "_OTC",
            "",
        )

        value = value.replace(
            "/OTC",
            "",
        )

        value = value.replace(
            " OTC",
            "",
        )

        value = value.replace(
            "-",
            "",
        )

        value = value.replace(
            "_",
            "",
        )

        value = value.replace(
            "/",
            "",
        )

        if len(value) == 6:
            return (
                f"{value[:3]}/"
                f"{value[3:]}"
            )

        return value

    @staticmethod
    def display_pair(
        pair: str,
    ) -> str:

        value = str(pair).upper()

        otc = MarketClient.is_otc_pair(
            value
        )

        value = value.replace(
            "_OTC",
            "",
        )

        value = value.replace(
            "/OTC",
            "",
        )

        value = value.replace(
            " OTC",
            "",
        )

        value = value.replace(
            "/",
            "",
        )

        value = value.replace(
            "-",
            "",
        )

        value = value.replace(
            "_",
            "",
        )

        if len(value) == 6:
            result = (
                f"{value[:3]}/"
                f"{value[3:]}"
            )
        else:
            result = value

        if otc:
            result += " OTC"

        return result

    # ============================================================
    # NUMBER
    # ============================================================

    @staticmethod
    def _safe_float(
        value: Any,
    ) -> float:

        try:
            return float(value)
        except (
            TypeError,
            ValueError,
        ):
            return 0.0

    # ============================================================
    # TWELVE DATA
    # ============================================================

    async def _request_twelve_data(
        self,
        pair: str,
        interval: str,
        limit: int,
    ) -> dict[str, Any] | None:

        if not TWELVE_DATA_API_KEY:
            print(
                "[MARKET] "
                "TWELVE_DATA_API_KEY не задан"
            )

            return None

        symbol = self.twelve_data_symbol(
            pair
        )

        params = {
            "symbol": symbol,
            "interval": interval,
            "outputsize": limit,
            "apikey": TWELVE_DATA_API_KEY,
            "timezone": "UTC",
            "format": "JSON",
        }

        print(
            f"[MARKET] "
            f"{self.display_pair(pair)}: "
            f"Twelve Data "
            f"interval={interval}, "
            f"limit={limit}"
        )

        session = await self._get_session()

        try:
            async with self._request_lock:
                async with session.get(
                    self.TWELVE_DATA_URL,
                    params=params,
                ) as response:

                    text = await response.text()

                    if response.status != 200:
                        print(
                            f"[MARKET] "
                            f"{self.display_pair(pair)}: "
                            f"HTTP {response.status}: "
                            f"{text[:300]}"
                        )

                        return None

                    try:
                        data = await response.json(
                            content_type=None
                        )
                    except Exception as exc:
                        print(
                            f"[MARKET] "
                            f"{self.display_pair(pair)}: "
                            f"JSON error: {exc}"
                        )

                        return None

                    if not isinstance(
                        data,
                        dict,
                    ):
                        return None

                    return data

        except asyncio.TimeoutError:
            print(
                f"[MARKET] "
                f"{self.display_pair(pair)}: "
                "timeout"
            )

            return None

        except aiohttp.ClientError as exc:
            print(
                f"[MARKET] "
                f"{self.display_pair(pair)}: "
                f"network error: {exc}"
            )

            return None

        except Exception as exc:
            print(
                f"[MARKET] "
                f"{self.display_pair(pair)}: "
                f"request error: {exc}"
            )

            return None

    async def _get_twelve_data_candles(
        self,
        pair: str,
        limit: int | None = None,
        interval: Any = None,
    ) -> pd.DataFrame | None:

        if not TWELVE_DATA_API_KEY:
            print(
                "[MARKET] "
                "TWELVE_DATA_API_KEY не задан"
            )

            return None

        if interval is None:
            interval = CANDLE_INTERVAL

        normalized_interval = (
            self.interval_to_twelve_data(
                interval
            )
        )

        try:
            requested_limit = int(
                limit
                if limit is not None
                else CANDLE_LIMIT
            )
        except (
            TypeError,
            ValueError,
        ):
            requested_limit = self.DEFAULT_LIMIT

        requested_limit = max(
            requested_limit,
            self.MIN_CANDLES + 20,
        )

        requested_limit = min(
            requested_limit,
            5000,
        )

        data = await self._request_twelve_data(
            pair=pair,
            interval=normalized_interval,
            limit=requested_limit,
        )

        if data is None:
            return None

        if data.get("status") == "error":
            print(
                f"[MARKET] "
                f"{self.display_pair(pair)}: "
                f"Twelve Data error: "
                f"{data.get('message', 'unknown error')}"
            )

            return None

        values = data.get(
            "values"
        )

        if not isinstance(
            values,
            list,
        ):
            print(
                f"[MARKET] "
                f"{self.display_pair(pair)}: "
                "поле values отсутствует"
            )

            return None

        if not values:
            print(
                f"[MARKET] "
                f"{self.display_pair(pair)}: "
                "свечи не получены"
            )

            return None

        rows: list[dict[str, Any]] = []

        for item in values:

            if not isinstance(
                item,
                dict,
            ):
                continue

            try:
                datetime_value = item[
                    "datetime"
                ]

                open_price = float(
                    item["open"]
                )

                high_price = float(
                    item["high"]
                )

                low_price = float(
                    item["low"]
                )

                close_price = float(
                    item["close"]
                )

                volume = self._safe_float(
                    item.get("volume")
                )

            except (
                KeyError,
                TypeError,
                ValueError,
            ):
                continue

            rows.append(
                {
                    "datetime": datetime_value,
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close_price,
                    "volume": volume,
                }
            )

        if not rows:
            print(
                f"[MARKET] "
                f"{self.display_pair(pair)}: "
                "OHLC не удалось распарсить"
            )

            return None

        df = pd.DataFrame(
            rows
        )

        df["datetime"] = pd.to_datetime(
            df["datetime"],
            utc=True,
            errors="coerce",
        )

        for column in (
            "open",
            "high",
            "low",
            "close",
            "volume",
        ):
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

        if df.empty:
            return None

        df = (
            df.sort_values(
                "datetime"
            )
            .drop_duplicates(
                subset=[
                    "datetime"
                ],
                keep="last",
            )
            .reset_index(
                drop=True
            )
        )

        if len(df) < self.MIN_CANDLES:
            print(
                f"[MARKET] "
                f"{self.display_pair(pair)}: "
                f"недостаточно свечей "
                f"{len(df)}/"
                f"{self.MIN_CANDLES}"
            )

            return None

        print(
            f"[MARKET] "
            f"{self.display_pair(pair)}: "
            f"получено {len(df)} свечей"
        )

        return df

    # ============================================================
    # PUBLIC GET CANDLES
    # ============================================================

    async def get_candles(
        self,
        pair: str,
        limit: int | None = None,
    ) -> pd.DataFrame | None:

        if not pair:
            return None

        pair = str(pair).strip()

        if self.is_otc_pair(
            pair
        ):
            return await self.get_otc_candles(
                pair,
                limit=limit,
            )

        return await self._get_twelve_data_candles(
            pair=pair,
            limit=limit,
        )

    # ============================================================
    # COMPATIBILITY METHODS
    # ============================================================

    async def fetch_candles(
        self,
        pair: str,
        limit: int | None = None,
    ) -> pd.DataFrame | None:

        return await self.get_candles(
            pair,
            limit=limit,
        )

    async def get_history(
        self,
        pair: str,
        interval: Any = None,
        limit: int | None = None,
    ) -> pd.DataFrame | None:

        if self.is_otc_pair(
            pair
        ):
            return await self.get_otc_candles(
                pair,
                limit=limit,
            )

        return await self._get_twelve_data_candles(
            pair=pair,
            limit=limit,
            interval=(
                interval
                if interval is not None
                else CANDLE_INTERVAL
            ),
        )

    async def get_data(
        self,
        pair: str,
        interval: Any = None,
        limit: int | None = None,
    ) -> pd.DataFrame | None:

        return await self.get_history(
            pair=pair,
            interval=interval,
            limit=limit,
        )

    # ============================================================
    # OTC
    # ============================================================

    async def get_otc_candles(
        self,
        pair: str,
        limit: int | None = None,
    ) -> pd.DataFrame | None:

        """
        OTC не заменяем обычным Forex.

        Если полноценной публичной исторической
        серии для OTC нет, возвращаем None.
        """

        print(
            f"[OTC] "
            f"{self.display_pair(pair)}: "
            "исторические публичные OTC-свечи "
            "недоступны"
        )

        return None

    # ============================================================
    # ASSET DISCOVERY
    # ============================================================

    async def discover_pocket_pairs(
        self,
    ) -> list[str]:

        if not ASSET_DISCOVERY_ENABLED:
            return []

        now = time.time()

        if (
            self._asset_cache
            and (
                now
                - self._asset_cache_time
                < ASSET_DISCOVERY_CACHE_SECONDS
            )
        ):
            return list(
                self._asset_cache
            )

        if not POCKET_OPTION_ASSETS_URL:
            return []

        session = await self._get_session()

        try:
            async with session.get(
                POCKET_OPTION_ASSETS_URL
            ) as response:

                if response.status != 200:
                    print(
                        "[PAIRS] discovery HTTP "
                        f"{response.status}"
                    )

                    return []

                data = await response.json(
                    content_type=None
                )

        except Exception as exc:
            print(
                "[PAIRS] discovery error: "
                f"{exc}"
            )

            return []

        if isinstance(
            data,
            dict,
        ):
            candidates = (
                data.get("assets")
                or data.get("pairs")
                or data.get("symbols")
                or data.get("data")
                or []
            )

        elif isinstance(
            data,
            list,
        ):
            candidates = data

        else:
            candidates = []

        result: list[str] = []

        for item in candidates:

            symbol = None

            if isinstance(
                item,
                str,
            ):
                symbol = item

            elif isinstance(
                item,
                dict,
            ):
                symbol = (
                    item.get("symbol")
                    or item.get("name")
                    or item.get("pair")
                    or item.get("asset")
                )

            if not symbol:
                continue

            symbol = str(
                symbol
            ).strip()

            if symbol:
                result.append(
                    symbol
                )

        result = self._normalize_pair_list(
            result
        )

        self._asset_cache = result
        self._asset_cache_time = now

        print(
            "[PAIRS] Динамически найдено: "
            f"{len(result)}"
        )

        return list(result)

    @staticmethod
    def _normalize_pair_list(
        pairs: list[str],
    ) -> list[str]:

        result: list[str] = []
        seen: set[str] = set()

        for pair in pairs:

            value = str(
                pair
            ).strip()

            if not value:
                continue

            key = (
                value.upper()
                .replace("/", "")
                .replace("_", "")
                .replace("-", "")
                .replace(" ", "")
            )

            if key in seen:
                continue

            seen.add(key)
            result.append(
                value
            )

        return result

    # ============================================================
    # AVAILABLE PAIRS
    # ============================================================

    async def get_available_pairs(
        self,
    ) -> list[str]:

        """
        Используем PAIRS из config.py.

        Не делаем отдельный запрос свечей
        для каждой пары только ради списка.
        """

        regular_pairs = (
            self._normalize_pair_list(
                list(FALLBACK_PAIRS)
            )
        )

        result = list(
            regular_pairs
        )

        try:
            from keyboards import OTC_PAIRS

            otc_pairs = (
                self._normalize_pair_list(
                    list(OTC_PAIRS)
                )
            )

            result.extend(
                otc_pairs
            )

            print(
                "[PAIRS] Обычных: "
                f"{len(regular_pairs)}"
            )

            print(
                "[PAIRS] OTC: "
                f"{len(otc_pairs)}"
            )

        except Exception:
            print(
                "[PAIRS] Обычных: "
                f"{len(regular_pairs)}"
            )

        return self._normalize_pair_list(
            result
        )

    # ============================================================
    # HEALTH
    # ============================================================

    async def health_check(
        self,
    ) -> bool:

        if not TWELVE_DATA_API_KEY:
            return False

        if not FALLBACK_PAIRS:
            return False

        try:
            candles = await self.get_candles(
                FALLBACK_PAIRS[0],
                limit=10,
            )

            return (
                candles is not None
                and not candles.empty
            )

        except Exception:
            return False


# ================================================================
# GLOBAL CLIENT
# ================================================================
#
# main.py делает:
#
#     from market import market_client
#
# Поэтому этот объект ОБЯЗАТЕЛЬНО должен существовать.
#

market_client = MarketClient()
