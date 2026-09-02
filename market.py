from __future__ import annotations

import time
from typing import Any, Optional

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


# ============================================================
# CONSTANTS
# ============================================================

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"

# Публичный источник рыночных котировок без авторизации.
BIQUOTE_URL = "https://biquote.io/api/{symbol}"

REQUEST_TIMEOUT = 20

ASSET_CACHE_SECONDS = max(
    60,
    int(ASSET_DISCOVERY_CACHE_SECONDS or 300),
)

MIN_CANDLES = max(
    80,
    int(CANDLE_LIMIT or 100),
)


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


class MarketClient:
    """
    Единый источник рыночных данных.

    Обычные Forex:
        Twelve Data

    OTC:
        публичный источник BiQuote.

    Никакой авторизации Pocket Option
    для получения данных не используется.
    """

    def __init__(self) -> None:
        self.session: Optional[aiohttp.ClientSession] = None

        self._asset_cache: list[str] = []
        self._asset_cache_time: float = 0.0

    # ============================================================
    # HTTP SESSION
    # ============================================================

    async def _get_session(self) -> aiohttp.ClientSession:
        if (
            self.session is None
            or self.session.closed
        ):
            timeout = aiohttp.ClientTimeout(
                total=REQUEST_TIMEOUT
            )

            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 "
                        "(compatible; TeyzooSignalBot/1.0)"
                    )
                },
            )

        return self.session

    async def close(self) -> None:
        if (
            self.session is not None
            and not self.session.closed
        ):
            try:
                await self.session.close()
            except Exception:
                pass

        self.session = None

    # ============================================================
    # SYMBOL HELPERS
    # ============================================================

    @staticmethod
    def is_otc(value: str) -> bool:
        if not value:
            return False

        value = str(value).strip().lower()

        return (
            value.endswith("_otc")
            or value.endswith("-otc")
            or value.endswith(" otc")
            or value.endswith("otc")
        )

    @staticmethod
    def _clean_asset_name(
        value: str,
    ) -> str:
        if not value:
            return ""

        value = str(value).strip()

        while "  " in value:
            value = value.replace(
                "  ",
                " ",
            )

        return value

    @staticmethod
    def _normalize_forex_pair(
        value: str,
    ) -> Optional[str]:
        """
        EURUSD -> EUR/USD
        EUR/USD -> EUR/USD
        """

        if not value:
            return None

        value = str(
            value
        ).strip().upper()

        if MarketClient.is_otc(value):
            return None

        value = value.replace(
            " ",
            "",
        )

        value = value.replace(
            "-",
            "/",
        )

        value = value.replace(
            "_",
            "/",
        )

        if "/" in value:
            parts = value.split("/")

            if (
                len(parts) == 2
                and len(parts[0]) == 3
                and len(parts[1]) == 3
                and parts[0].isalpha()
                and parts[1].isalpha()
            ):
                return (
                    f"{parts[0]}/"
                    f"{parts[1]}"
                )

        if (
            len(value) == 6
            and value.isalpha()
        ):
            return (
                f"{value[:3]}/"
                f"{value[3:]}"
            )

        return None

    @staticmethod
    def normalize_otc_symbol(
        value: str,
    ) -> Optional[str]:
        """
        EUR/USD OTC -> EURUSD_otc
        EURUSD OTC  -> EURUSD_otc
        EURUSD_otc  -> EURUSD_otc
        """

        if not value:
            return None

        raw = str(
            value
        ).strip()

        if not MarketClient.is_otc(raw):
            return None

        clean = raw

        for suffix in (
            "_otc",
            "-otc",
            " otc",
            "otc",
            "_OTC",
            "-OTC",
            " OTC",
            "OTC",
        ):
            if clean.lower().endswith(
                suffix.lower()
            ):
                clean = clean[
                    : -len(suffix)
                ]
                break

        compact = "".join(
            character
            for character in clean
            if character.isalpha()
        ).upper()

        if len(compact) == 6:
            return f"{compact}_otc"

        if compact:
            return f"{compact}_otc"

        return None

    @staticmethod
    def display_asset_name(
        value: str,
    ) -> str:

        if not value:
            return value

        raw = str(
            value
        ).strip()

        if MarketClient.is_otc(raw):

            otc = MarketClient.normalize_otc_symbol(
                raw
            )

            if otc:
                base = otc[:-4]

                if (
                    len(base) == 6
                    and base.isalpha()
                ):
                    return (
                        f"{base[:3]}/"
                        f"{base[3:]} OTC"
                    )

                return f"{base} OTC"

        normal = MarketClient._normalize_forex_pair(
            raw
        )

        if normal:
            return normal

        return raw

    # ============================================================
    # ASSET DISCOVERY
    # ============================================================

    async def discover_pocket_assets(
        self,
    ) -> list[str]:

        now = time.monotonic()

        if (
            self._asset_cache
            and (
                now - self._asset_cache_time
                < ASSET_CACHE_SECONDS
            )
        ):
            return list(
                self._asset_cache
            )

        if not ASSET_DISCOVERY_ENABLED:
            return self._fallback_assets()

        assets: list[str] = []

        try:
            session = await self._get_session()

            async with session.get(
                POCKET_OPTION_ASSETS_URL
            ) as response:

                if response.status == 200:

                    html = await response.text(
                        errors="ignore"
                    )

                    assets = self._parse_pocket_assets(
                        html
                    )

        except Exception as exc:

            print(
                "[MARKET] Discovery error: "
                f"{exc}"
            )

        fallback = self._fallback_assets()

        result: list[str] = []

        for asset in (
            assets + fallback
        ):

            if not asset:
                continue

            if asset not in result:
                result.append(
                    asset
                )

        self._asset_cache = result
        self._asset_cache_time = now

        return list(result)

    async def discover_pocket_pairs(
        self,
    ) -> list[str]:

        assets = await self.discover_pocket_assets()

        pairs: list[str] = []

        for asset in assets:

            if self.is_otc(asset):
                continue

            pair = self._normalize_forex_pair(
                asset
            )

            if pair and pair not in pairs:
                pairs.append(pair)

        if not pairs:
            return list(
                FALLBACK_PAIRS
            )

        return pairs

    def _fallback_assets(
        self,
    ) -> list[str]:

        result: list[str] = []

        for pair in FALLBACK_PAIRS:

            normalized = (
                self._normalize_forex_pair(
                    pair
                )
            )

            if (
                normalized
                and normalized not in result
            ):
                result.append(
                    normalized
                )

        for pair in OTC_PAIRS:

            normalized = (
                self.normalize_otc_symbol(
                    pair
                )
            )

            if (
                normalized
                and normalized not in result
            ):
                result.append(
                    normalized
                )

        return result

    def _parse_pocket_assets(
        self,
        html: str,
    ) -> list[str]:

        if not html:
            return []

        found: list[str] = []

        # --------------------------------------------------------
        # OTC
        # --------------------------------------------------------

        otc_patterns = [
            r"\b([A-Z]{3}/[A-Z]{3})\s*OTC\b",
            r"\b([A-Z]{6})[_\-\s]OTC\b",
        ]

        for pattern in otc_patterns:

            try:
                matches = __import__(
                    "re"
                ).findall(
                    pattern,
                    html,
                    flags=__import__(
                        "re"
                    ).IGNORECASE,
                )
            except Exception:
                matches = []

            for value in matches:

                value = self._clean_asset_name(
                    value
                )

                otc = self.normalize_otc_symbol(
                    f"{value} OTC"
                )

                if otc:
                    found.append(
                        otc
                    )

        # --------------------------------------------------------
        # NORMAL
        # --------------------------------------------------------

        try:

            matches = __import__(
                "re"
            ).findall(
                r"\b([A-Z]{3}/[A-Z]{3})\b",
                html,
                flags=__import__(
                    "re"
                ).IGNORECASE,
            )

        except Exception:

            matches = []

        for value in matches:

            normalized = (
                self._normalize_forex_pair(
                    value
                )
            )

            if normalized:
                found.append(
                    normalized
                )

        result: list[str] = []

        for asset in found:

            if asset not in result:
                result.append(
                    asset
                )

        return result

    # ============================================================
    # AVAILABLE PAIRS
    # ============================================================

    async def get_available_pairs(
        self,
    ) -> list[str]:

        discovered = (
            await self.discover_pocket_assets()
        )

        result: list[str] = []

        # --------------------------------------------------------
        # NORMAL FOREX
        # --------------------------------------------------------

        for asset in discovered:

            if self.is_otc(asset):
                continue

            pair = self._normalize_forex_pair(
                asset
            )

            if not pair:
                continue

            try:

                candles = (
                    await self._get_twelve_data_candles(
                        pair,
                        limit=5,
                        interval=CANDLE_INTERVAL,
                    )
                )

                if (
                    candles is not None
                    and not candles.empty
                ):

                    if pair not in result:
                        result.append(
                            pair
                        )

            except Exception:
                continue

        # --------------------------------------------------------
        # FALLBACK NORMAL
        # --------------------------------------------------------

        if not any(
            not self.is_otc(pair)
            for pair in result
        ):
            for pair in FALLBACK_PAIRS:

                normalized = (
                    self._normalize_forex_pair(
                        pair
                    )
                )

                if (
                    normalized
                    and normalized not in result
                ):
                    result.append(
                        normalized
                    )

        # --------------------------------------------------------
        # OTC
        # --------------------------------------------------------

        for pair in OTC_PAIRS:

            normalized = (
                self.normalize_otc_symbol(
                    pair
                )
            )

            if (
                normalized
                and normalized not in result
            ):
                result.append(
                    normalized
                )

        regular_count = sum(
            1
            for pair in result
            if not self.is_otc(pair)
        )

        otc_count = sum(
            1
            for pair in result
            if self.is_otc(pair)
        )

        print(
            "[MARKET] Доступные активы: "
            f"{len(result)}"
        )

        print(
            "[MARKET] Обычные: "
            f"{regular_count}"
        )

        print(
            "[MARKET] OTC: "
            f"{otc_count}"
        )

        return result

    # ============================================================
    # MAIN CANDLES
    # ============================================================

    async def get_candles(
        self,
        pair: str,
        interval: int = CANDLE_INTERVAL,
        limit: int = CANDLE_LIMIT,
    ) -> Optional[pd.DataFrame]:

        if not pair:
            return None

        if self.is_otc(pair):

            return await self.get_otc_candles(
                pair,
                interval=interval,
                limit=limit,
            )

        normalized = (
            self._normalize_forex_pair(
                pair
            )
        )

        if not normalized:
            return None

        return await self._get_twelve_data_candles(
            normalized,
            limit=limit,
            interval=interval,
        )

    # ============================================================
    # OTC CANDLES
    # ============================================================

    async def get_otc_candles(
        self,
        symbol: str,
        interval: int = CANDLE_INTERVAL,
        limit: int = CANDLE_LIMIT,
    ) -> Optional[pd.DataFrame]:

        otc_symbol = (
            self.normalize_otc_symbol(
                symbol
            )
        )

        if not otc_symbol:
            return None

        # --------------------------------------------------------
        # BiQuote expects normal symbol:
        #
        # EURUSD
        #
        # not EURUSD_otc.
        # --------------------------------------------------------

        base_symbol = otc_symbol[:-4]

        if len(base_symbol) != 6:
            print(
                f"[OTC] Неподдерживаемый "
                f"символ: {symbol}"
            )

            return None

        print(
            f"[OTC] {otc_symbol}: "
            "получение публичных данных"
        )

        # --------------------------------------------------------
        # First: try enough historical points.
        # --------------------------------------------------------

        df = await self._get_biquote_history(
            base_symbol,
            interval=interval,
            limit=limit,
        )

        if (
            df is not None
            and len(df) >= 50
        ):

            print(
                f"[OTC] {otc_symbol}: "
                f"получено {len(df)} свечей"
            )

            return df.tail(
                limit
            ).reset_index(
                drop=True
            )

        # --------------------------------------------------------
        # Second: build candles from live ticks.
        # --------------------------------------------------------

        df = await self._get_biquote_ticks(
            base_symbol,
            interval=interval,
            limit=limit,
        )

        if (
            df is not None
            and len(df) >= 50
        ):

            print(
                f"[OTC] {otc_symbol}: "
                f"из live-данных получено "
                f"{len(df)} свечей"
            )

            return df.tail(
                limit
            ).reset_index(
                drop=True
            )

        print(
            f"[OTC] {otc_symbol}: "
            "недостаточно данных"
        )

        return None

    # ============================================================
    # BIQUOTE HISTORY
    # ============================================================

    async def _get_biquote_history(
        self,
        symbol: str,
        interval: int,
        limit: int,
    ) -> Optional[pd.DataFrame]:

        url = BIQUOTE_URL.format(
            symbol=symbol
        )

        try:

            session = await self._get_session()

            async with session.get(
                url
            ) as response:

                if response.status != 200:
                    return None

                payload = await response.json(
                    content_type=None
                )

        except Exception as exc:

            print(
                f"[OTC] BiQuote history "
                f"{symbol}: {exc}"
            )

            return None

        # --------------------------------------------------------
        # Different possible response formats.
        # --------------------------------------------------------

        rows: Any = None

        if isinstance(
            payload,
            list,
        ):
            rows = payload

        elif isinstance(
            payload,
            dict,
        ):

            for key in (
                "data",
                "history",
                "candles",
                "values",
                "prices",
                "ticks",
                "result",
            ):

                value = payload.get(
                    key
                )

                if isinstance(
                    value,
                    list,
                ):
                    rows = value
                    break

        if rows is None:
            return None

        # --------------------------------------------------------
        # If API returns OHLC directly.
        # --------------------------------------------------------

        df = self._candles_to_dataframe(
            rows
        )

        if (
            df is not None
            and len(df) >= 10
        ):
            return df

        # --------------------------------------------------------
        # If API returns ticks.
        # --------------------------------------------------------

        tick_df = self._ticks_to_dataframe(
            rows
        )

        if tick_df is None:
            return None

        return self._ticks_to_candles(
            tick_df,
            interval=interval,
        )

    # ============================================================
    # BIQUOTE TICKS
    # ============================================================

    async def _get_biquote_ticks(
        self,
        symbol: str,
        interval: int,
        limit: int,
    ) -> Optional[pd.DataFrame]:

        url = BIQUOTE_URL.format(
            symbol=symbol
        )

        try:

            session = await self._get_session()

            async with session.get(
                url
            ) as response:

                if response.status != 200:
                    return None

                payload = await response.json(
                    content_type=None
                )

        except Exception:
            return None

        rows: Any = None

        if isinstance(
            payload,
            list,
        ):
            rows = payload

        elif isinstance(
            payload,
            dict,
        ):

            for key in (
                "ticks",
                "prices",
                "data",
                "history",
                "values",
                "result",
            ):

                value = payload.get(
                    key
                )

                if isinstance(
                    value,
                    list,
                ):
                    rows = value
                    break

        if rows is None:
            return None

        ticks = self._ticks_to_dataframe(
            rows
        )

        if ticks is None:
            return None

        return self._ticks_to_candles(
            ticks,
            interval=interval,
        )

    # ============================================================
    # TICKS -> DATAFRAME
    # ============================================================

    @staticmethod
    def _ticks_to_dataframe(
        rows: Any,
    ) -> Optional[pd.DataFrame]:

        if not rows:
            return None

        if isinstance(
            rows,
            dict,
        ):
            rows = [rows]

        try:

            df = pd.DataFrame(
                rows
            )

        except Exception:
            return None

        if df.empty:
            return None

        df.columns = [
            str(column)
            .strip()
            .lower()
            for column in df.columns
        ]

        timestamp_column = None

        for name in (
            "timestamp",
            "time",
            "datetime",
            "date",
            "ts",
        ):

            if name in df.columns:
                timestamp_column = name
                break

        price_column = None

        for name in (
            "price",
            "last",
            "close",
            "value",
            "rate",
        ):

            if name in df.columns:
                price_column = name
                break

        if (
            timestamp_column is None
            or price_column is None
        ):
            return None

        df["timestamp"] = (
            pd.to_numeric(
                df[timestamp_column],
                errors="coerce",
            )
        )

        numeric_time = (
            df["timestamp"]
            .dropna()
        )

        if numeric_time.empty:
            return None

        median_time = float(
            numeric_time.median()
        )

        if median_time > 100_000_000_000:

            df["timestamp"] = pd.to_datetime(
                df["timestamp"],
                unit="ms",
                utc=True,
                errors="coerce",
            )

        else:

            df["timestamp"] = pd.to_datetime(
                df["timestamp"],
                unit="s",
                utc=True,
                errors="coerce",
            )

        df["price"] = pd.to_numeric(
            df[price_column],
            errors="coerce",
        )

        df = df.dropna(
            subset=[
                "timestamp",
                "price",
            ]
        )

        if df.empty:
            return None

        return (
            df[
                [
                    "timestamp",
                    "price",
                ]
            ]
            .sort_values("timestamp")
            .drop_duplicates(
                "timestamp",
                keep="last",
            )
            .reset_index(
                drop=True
            )
        )

    # ============================================================
    # TICKS -> CANDLES
    # ============================================================

    @staticmethod
    def _ticks_to_candles(
        ticks: pd.DataFrame,
        interval: int,
    ) -> Optional[pd.DataFrame]:

        if ticks is None:
            return None

        if ticks.empty:
            return None

        seconds = max(
            1,
            int(interval),
        )

        frame = ticks.copy()

        frame = frame.set_index(
            "timestamp"
        )

        candles = (
            frame["price"]
            .resample(
                f"{seconds}s",
                label="left",
                closed="left",
            )
            .ohlc()
        )

        candles = candles.dropna()

        if candles.empty:
            return None

        candles = candles.reset_index()

        return candles[
            [
                "timestamp",
                "open",
                "high",
                "low",
                "close",
            ]
        ]

    # ============================================================
    # TWELVE DATA
    # ============================================================

    async def _get_twelve_data_candles(
        self,
        pair: str,
        limit: int = CANDLE_LIMIT,
        interval: int = CANDLE_INTERVAL,
    ) -> Optional[pd.DataFrame]:

        if not TWELVE_DATA_API_KEY:
            print(
                "[MARKET] "
                "TWELVE_DATA_API_KEY отсутствует"
            )
            return None

        normalized = (
            self._normalize_forex_pair(
                pair
            )
        )

        if not normalized:
            return None

        interval_name = (
            self._twelve_interval(
                interval
            )
        )

        params = {
            "symbol": normalized,
            "interval": interval_name,
            "outputsize": max(
                1,
                int(limit),
            ),
            "apikey": TWELVE_DATA_API_KEY,
            "format": "JSON",
        }

        try:

            session = await self._get_session()

            async with session.get(
                TWELVE_DATA_URL,
                params=params,
            ) as response:

                if response.status != 200:
                    return None

                payload = await response.json(
                    content_type=None
                )

        except Exception as exc:

            print(
                f"[MARKET] Twelve Data "
                f"{normalized}: {exc}"
            )

            return None

        if not isinstance(
            payload,
            dict,
        ):
            return None

        if payload.get(
            "status"
        ) == "error":
            return None

        values = payload.get(
            "values"
        )

        if not isinstance(
            values,
            list,
        ):
            return None

        return self._candles_to_dataframe(
            values
        )

    # ============================================================
    # INTERVAL
    # ============================================================

    @staticmethod
    def _twelve_interval(
        seconds: int,
    ) -> str:

        seconds = int(
            seconds
        )

        mapping = {
            60: "1min",
            300: "5min",
            900: "15min",
            1800: "30min",
            3600: "1h",
        }

        if seconds in mapping:
            return mapping[seconds]

        if (
            seconds > 60
            and seconds % 60 == 0
        ):
            return (
                f"{seconds // 60}min"
            )

        return "1min"

    # ============================================================
    # CANDLES DATAFRAME
    # ============================================================

    @staticmethod
    def _candles_to_dataframe(
        candles: Any,
    ) -> Optional[pd.DataFrame]:

        if candles is None:
            return None

        try:

            df = pd.DataFrame(
                candles
            )

        except Exception:
            return None

        if df.empty:
            return None

        df.columns = [
            str(column)
            .strip()
            .lower()
            for column in df.columns
        ]

        aliases = {
            "datetime": "timestamp",
            "date": "timestamp",
            "time": "timestamp",
            "ts": "timestamp",
            "open_price": "open",
            "high_price": "high",
            "low_price": "low",
            "close_price": "close",
        }

        df = df.rename(
            columns=aliases
        )

        required = [
            "open",
            "high",
            "low",
            "close",
        ]

        for column in required:

            if column not in df.columns:
                return None

            df[column] = pd.to_numeric(
                df[column],
                errors="coerce",
            )

        if "timestamp" not in df.columns:
            return None

        raw_timestamp = df[
            "timestamp"
        ]

        numeric_timestamp = (
            pd.to_numeric(
                raw_timestamp,
                errors="coerce",
            )
        )

        valid = (
            numeric_timestamp
            .dropna()
        )

        if not valid.empty:

            median_value = float(
                valid.median()
            )

            if median_value > 100_000_000_000:

                df["timestamp"] = pd.to_datetime(
                    numeric_timestamp,
                    unit="ms",
                    utc=True,
                    errors="coerce",
                )

            else:

                df["timestamp"] = pd.to_datetime(
                    numeric_timestamp,
                    unit="s",
                    utc=True,
                    errors="coerce",
                )

        else:

            df["timestamp"] = pd.to_datetime(
                raw_timestamp,
                utc=True,
                errors="coerce",
            )

        df = df.dropna(
            subset=[
                "timestamp",
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
                "timestamp"
            )
            .drop_duplicates(
                "timestamp",
                keep="last",
            )
            .reset_index(
                drop=True
            )
        )

        return df[
            [
                "timestamp",
                "open",
                "high",
                "low",
                "close",
            ]
        ]

    # ============================================================
    # LEGACY COMPATIBILITY
    # ============================================================

    async def fetch_candles(
        self,
        pair: str,
        interval: int = CANDLE_INTERVAL,
        limit: int = CANDLE_LIMIT,
    ):

        return await self.get_candles(
            pair,
            interval=interval,
            limit=limit,
        )

    async def get_history(
        self,
        pair: str,
        interval: int = CANDLE_INTERVAL,
        limit: int = CANDLE_LIMIT,
    ):

        return await self.get_candles(
            pair,
            interval=interval,
            limit=limit,
        )

    async def get_data(
        self,
        pair: str,
        interval: int = CANDLE_INTERVAL,
        limit: int = CANDLE_LIMIT,
    ):

        return await self.get_candles(
            pair,
            interval=interval,
            limit=limit,
        )


# ============================================================
# GLOBAL CLIENT
# ============================================================

market_client = MarketClient()
