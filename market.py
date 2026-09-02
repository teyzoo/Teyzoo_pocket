from __future__ import annotations

import asyncio
import inspect
import re
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

try:
    from config import POCKET_OPTION_SSID
except ImportError:
    POCKET_OPTION_SSID = ""

try:
    from BinaryOptionsToolsV2.pocketoption import PocketOptionAsync
except ImportError:
    PocketOptionAsync = None


# ============================================================
# CONSTANTS
# ============================================================

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"

REQUEST_TIMEOUT = 20
POCKET_CONNECT_TIMEOUT = 30

ASSET_CACHE_SECONDS = max(
    60,
    int(ASSET_DISCOVERY_CACHE_SECONDS or 300),
)

OTC_RETRIES = 2

MIN_CANDLES = max(
    80,
    int(CANDLE_LIMIT or 100),
)


# ============================================================
# OTC FALLBACK
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
    Унифицированный источник рыночных данных.

    Обычный Forex:
        Twelve Data

    OTC:
        PocketOptionAsync / BinaryOptionsToolsV2

    OTC требует POCKET_OPTION_SSID
    в environment variables Render.

    SSID не хранится в коде.
    """

    def __init__(self) -> None:
        self.session: Optional[aiohttp.ClientSession] = None

        self._asset_cache: list[str] = []
        self._asset_cache_time: float = 0.0

        self._pocket_client: Any = None
        self._pocket_lock = asyncio.Lock()
        self._pocket_ready = False

    # ============================================================
    # HTTP
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
                        "(compatible; TeyzooSignalBot/2.0)"
                    )
                },
            )

        return self.session

    async def close(self) -> None:
        await self._close_pocket_client()

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
    def _clean_asset_name(value: str) -> str:
        if not value:
            return ""

        value = str(value).strip()

        value = re.sub(
            r"\s+",
            " ",
            value,
        )

        return value

    @staticmethod
    def _normalize_forex_pair(
        value: str,
    ) -> Optional[str]:
        """
        EURUSD -> EUR/USD
        EUR/USD -> EUR/USD

        OTC здесь не принимается.
        """

        if not value:
            return None

        value = str(value).strip().upper()

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

        match = re.fullmatch(
            r"([A-Z]{3})/?([A-Z]{3})",
            value,
        )

        if not match:
            return None

        return (
            f"{match.group(1)}/"
            f"{match.group(2)}"
        )

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

        raw = str(value).strip()

        if not MarketClient.is_otc(raw):
            return None

        clean = re.sub(
            r"[\s_\-]*otc$",
            "",
            raw,
            flags=re.IGNORECASE,
        )

        clean = clean.strip()

        compact = re.sub(
            r"[^A-Za-z]",
            "",
            clean,
        ).upper()

        if len(compact) == 6:
            return f"{compact}_otc"

        normalized = re.sub(
            r"\s+",
            "",
            clean,
        )

        if not normalized:
            return None

        return f"{normalized}_otc"

    @staticmethod
    def display_asset_name(
        value: str,
    ) -> str:
        if not value:
            return value

        value = str(value).strip()

        if value.lower().endswith("_otc"):
            base = value[:-4]

            if (
                len(base) == 6
                and base.isalpha()
            ):
                base = base.upper()

                return (
                    f"{base[:3]}/"
                    f"{base[3:]} OTC"
                )

            return f"{base} OTC"

        return value

    # ============================================================
    # ASSET DISCOVERY
    # ============================================================

    async def discover_pocket_assets(
        self,
    ) -> list[str]:
        """
        Получает активы с Pocket Option.

        ВАЖНО:
        OTC fallback добавляется отдельно.
        Поэтому отсутствие OTC в HTML больше
        не ломает OTC режим.
        """

        if not ASSET_DISCOVERY_ENABLED:
            return self._build_discovery_fallback()

        now = time.monotonic()

        if (
            self._asset_cache
            and (
                now - self._asset_cache_time
                < ASSET_CACHE_SECONDS
            )
        ):
            return list(self._asset_cache)

        assets: list[str] = []

        try:
            session = await self._get_session()

            async with session.get(
                POCKET_OPTION_ASSETS_URL,
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
                "[MARKET] Asset discovery error: "
                f"{exc}"
            )

        fallback = self._build_discovery_fallback()

        result: list[str] = []

        for asset in assets + fallback:
            if not asset:
                continue

            if asset not in result:
                result.append(asset)

        if result:
            self._asset_cache = result
            self._asset_cache_time = now

        return result

    def _build_discovery_fallback(self) -> list[str]:
        result: list[str] = []

        for pair in FALLBACK_PAIRS:
            if not pair:
                continue

            normalized = self._normalize_forex_pair(
                pair
            )

            if normalized and normalized not in result:
                result.append(normalized)

        for pair in OTC_PAIRS:
            normalized = self.normalize_otc_symbol(
                pair
            )

            if normalized and normalized not in result:
                result.append(normalized)

        return result

    async def discover_pocket_pairs(
        self,
    ) -> list[str]:
        """
        Старый совместимый метод.

        Возвращает только обычные Forex.
        """

        assets = await self.discover_pocket_assets()

        pairs: list[str] = []

        for asset in assets:
            normalized = self._normalize_forex_pair(
                asset
            )

            if normalized and normalized not in pairs:
                pairs.append(normalized)

        if not pairs:
            return list(FALLBACK_PAIRS)

        return pairs

    def _parse_pocket_assets(
        self,
        html: str,
    ) -> list[str]:
        if not html:
            return []

        found: list[str] = []

        patterns = [
            r"\b([A-Z]{3}/[A-Z]{3})\s*OTC\b",
            r"\b([A-Z]{6})[_\-\s]OTC\b",
            r"\b([A-Za-z][A-Za-z0-9 .&_-]{1,30})\s+OTC\b",
        ]

        for pattern in patterns:
            try:
                matches = re.findall(
                    pattern,
                    html,
                    flags=re.IGNORECASE,
                )
            except Exception:
                matches = []

            for match in matches:
                value = (
                    match[0]
                    if isinstance(match, tuple)
                    else match
                )

                value = self._clean_asset_name(
                    value
                )

                if not value:
                    continue

                normal = self._normalize_forex_pair(
                    value
                )

                if normal:
                    found.append(normal)
                    continue

                otc = self.normalize_otc_symbol(
                    f"{value} OTC"
                )

                if otc:
                    found.append(otc)

        normal_pattern = (
            r"\b([A-Z]{3}/[A-Z]{3})\b"
        )

        try:
            matches = re.findall(
                normal_pattern,
                html,
                flags=re.IGNORECASE,
            )
        except Exception:
            matches = []

        for value in matches:
            normalized = self._normalize_forex_pair(
                value
            )

            if normalized:
                found.append(normalized)

        result: list[str] = []

        for asset in found:
            if asset not in result:
                result.append(asset)

        return result

    # ============================================================
    # AVAILABLE PAIRS
    # ============================================================

    async def get_available_pairs(
        self,
    ) -> list[str]:
        """
        Возвращает список активов для анализа.

        Обычные пары проверяются через Twelve Data.

        OTC НЕ зависит от discovery страницы.
        Если SSID + PocketOptionAsync доступны,
        OTC добавляются из фиксированного списка.
        """

        discovered = await self.discover_pocket_assets()

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
                        result.append(pair)

            except Exception as exc:
                print(
                    f"[MARKET] Forex availability "
                    f"{pair}: {exc}"
                )

        # --------------------------------------------------------
        # OTC
        # --------------------------------------------------------

        if (
            self._pocket_ssid_available()
            and PocketOptionAsync is not None
        ):
            for otc in OTC_PAIRS:

                normalized = self.normalize_otc_symbol(
                    otc
                )

                if not normalized:
                    continue

                if normalized not in result:
                    result.append(normalized)

        otc_count = sum(
            1
            for pair in result
            if self.is_otc(pair)
        )

        regular_count = (
            len(result) - otc_count
        )

        print(
            "[MARKET] Доступные активы: "
            f"{len(result)}"
        )

        print(
            "[MARKET] Обычных: "
            f"{regular_count}"
        )

        print(
            "[MARKET] OTC: "
            f"{otc_count}"
        )

        if otc_count == 0:
            if not self._pocket_ssid_available():
                print(
                    "[MARKET] OTC отключены: "
                    "POCKET_OPTION_SSID не задан"
                )
            elif PocketOptionAsync is None:
                print(
                    "[MARKET] OTC отключены: "
                    "BinaryOptionsToolsV2 "
                    "не установлен"
                )

        return result

    # ============================================================
    # POCKET AUTH
    # ============================================================

    @staticmethod
    def _pocket_ssid_available() -> bool:
        return bool(
            isinstance(
                POCKET_OPTION_SSID,
                str,
            )
            and POCKET_OPTION_SSID.strip()
        )

    async def _ensure_pocket_client(
        self,
    ) -> bool:

        if (
            self._pocket_ready
            and self._pocket_client is not None
        ):
            return True

        if not self._pocket_ssid_available():
            print(
                "[POCKET] SSID отсутствует"
            )
            return False

        if PocketOptionAsync is None:
            print(
                "[POCKET] BinaryOptionsToolsV2 "
                "не установлен"
            )
            return False

        async with self._pocket_lock:

            if (
                self._pocket_ready
                and self._pocket_client is not None
            ):
                return True

            client = None

            try:
                # ------------------------------------------------
                # Создание клиента.
                # Поддерживаем разные версии библиотеки.
                # ------------------------------------------------

                client = PocketOptionAsync(
                    ssid=POCKET_OPTION_SSID
                )

                if inspect.isawaitable(client):
                    client = await asyncio.wait_for(
                        client,
                        timeout=POCKET_CONNECT_TIMEOUT,
                    )

                # ------------------------------------------------
                # Явный connect(), если присутствует.
                # ------------------------------------------------

                connect_method = getattr(
                    client,
                    "connect",
                    None,
                )

                if callable(connect_method):

                    result = connect_method()

                    if inspect.isawaitable(result):
                        await asyncio.wait_for(
                            result,
                            timeout=POCKET_CONNECT_TIMEOUT,
                        )

                # ------------------------------------------------
                # Некоторые версии клиента используют
                # собственную инициализацию.
                # ------------------------------------------------

                await asyncio.sleep(2.0)

                self._pocket_client = client
                self._pocket_ready = True

                print(
                    "[POCKET] OTC клиент подключён"
                )

                return True

            except Exception as exc:

                print(
                    "[POCKET] Ошибка подключения: "
                    f"{exc}"
                )

                await self._safe_close_client(
                    client
                )

                self._pocket_client = None
                self._pocket_ready = False

                return False

    async def _safe_close_client(
        self,
        client: Any,
    ) -> None:

        if client is None:
            return

        for method_name in (
            "disconnect",
            "shutdown",
            "close",
        ):

            try:
                method = getattr(
                    client,
                    method_name,
                    None,
                )

                if not callable(method):
                    continue

                result = method()

                if inspect.isawaitable(result):
                    await result

                return

            except Exception:
                continue

    async def _close_pocket_client(
        self,
    ) -> None:

        async with self._pocket_lock:

            client = self._pocket_client

            self._pocket_client = None
            self._pocket_ready = False

            if client is None:
                return

            await self._safe_close_client(
                client
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
        """
        Получает OTC-свечи через Pocket Option.

        Основной способ:
            get_candles()

        Резерв:
            get_candles_live()
        """

        otc_symbol = self.normalize_otc_symbol(
            symbol
        )

        if not otc_symbol:
            print(
                f"[OTC] Некорректный символ: {symbol}"
            )
            return None

        if not await self._ensure_pocket_client():
            return None

        client = self._pocket_client

        if client is None:
            return None

        period = int(interval)
        required = max(
            int(limit),
            MIN_CANDLES,
        )

        # ========================================================
        # RETRIES
        # ========================================================

        for attempt in range(1, OTC_RETRIES + 1):

            try:

                print(
                    f"[OTC] {otc_symbol}: "
                    f"получение свечей "
                    f"(попытка {attempt}/{OTC_RETRIES})"
                )

                # =================================================
                # METHOD 1
                # get_candles
                # =================================================

                get_candles = getattr(
                    client,
                    "get_candles",
                    None,
                )

                if callable(get_candles):

                    result = None

                    # В документации:
                    # get_candles(asset, period, offset)
                    #
                    # offset = количество секунд истории.
                    #

                    offset = max(
                        period,
                        required * period,
                    )

                    try:
                        result = get_candles(
                            otc_symbol,
                            period,
                            offset,
                        )

                        if inspect.isawaitable(result):
                            result = await asyncio.wait_for(
                                result,
                                timeout=REQUEST_TIMEOUT,
                            )

                    except TypeError:

                        try:
                            result = get_candles(
                                otc_symbol,
                                period=period,
                                offset=offset,
                            )

                            if inspect.isawaitable(result):
                                result = await asyncio.wait_for(
                                    result,
                                    timeout=REQUEST_TIMEOUT,
                                )

                        except TypeError:

                            result = get_candles(
                                otc_symbol,
                                period,
                            )

                            if inspect.isawaitable(result):
                                result = await asyncio.wait_for(
                                    result,
                                    timeout=REQUEST_TIMEOUT,
                                )

                    df = self._pocket_candles_to_dataframe(
                        result
                    )

                    if (
                        df is not None
                        and len(df) >= 50
                    ):
                        if len(df) > limit:
                            df = df.tail(
                                limit
                            ).copy()

                        print(
                            f"[OTC] {otc_symbol}: "
                            f"получено {len(df)} свечей"
                        )

                        return df

                # =================================================
                # METHOD 2
                # candles()
                # =================================================

                candles_method = getattr(
                    client,
                    "candles",
                    None,
                )

                if callable(candles_method):

                    result = candles_method(
                        otc_symbol,
                        period,
                    )

                    if inspect.isawaitable(result):
                        result = await asyncio.wait_for(
                            result,
                            timeout=REQUEST_TIMEOUT,
                        )

                    df = self._pocket_candles_to_dataframe(
                        result
                    )

                    if (
                        df is not None
                        and len(df) >= 50
                    ):
                        if len(df) > limit:
                            df = df.tail(
                                limit
                            ).copy()

                        print(
                            f"[OTC] {otc_symbol}: "
                            f"получено {len(df)} свечей"
                        )

                        return df

                # =================================================
                # METHOD 3
                # get_candles_live
                # =================================================

                get_live = getattr(
                    client,
                    "get_candles_live",
                    None,
                )

                if callable(get_live):

                    rows: list[Any] = []

                    hours = max(
                        2.0,
                        (
                            required
                            * period
                            / 3600
                        ) + 0.5,
                    )

                    stream = get_live(
                        otc_symbol,
                        period=period,
                        hours=hours,
                        max_rows=required,
                    )

                    if inspect.isawaitable(stream):
                        stream = await stream

                    async def read_stream():
                        async for item in stream:

                            closed = None
                            forming = None

                            if isinstance(
                                item,
                                tuple,
                            ):
                                if len(item) >= 1:
                                    closed = item[0]

                                if len(item) >= 2:
                                    forming = item[1]

                            elif isinstance(
                                item,
                                list,
                            ):
                                closed = item

                            elif isinstance(
                                item,
                                dict,
                            ):
                                closed = [item]

                            if closed:

                                if isinstance(
                                    closed,
                                    (list, tuple),
                                ):
                                    rows.extend(
                                        closed
                                    )

                            if forming and isinstance(
                                forming,
                                dict,
                            ):
                                rows.append(
                                    forming
                                )

                            if len(rows) >= required:
                                break

                    try:
                        await asyncio.wait_for(
                            read_stream(),
                            timeout=REQUEST_TIMEOUT,
                        )
                    finally:
                        # Не оставляем бесконечный
                        # async generator висеть.
                        try:
                            aclose = getattr(
                                stream,
                                "aclose",
                                None,
                            )

                            if callable(aclose):
                                result = aclose()

                                if inspect.isawaitable(
                                    result
                                ):
                                    await result

                        except Exception:
                            pass

                    df = self._pocket_candles_to_dataframe(
                        rows
                    )

                    if (
                        df is not None
                        and len(df) >= 50
                    ):
                        if len(df) > limit:
                            df = df.tail(
                                limit
                            ).copy()

                        print(
                            f"[OTC] {otc_symbol}: "
                            f"live получено "
                            f"{len(df)} свечей"
                        )

                        return df

                raise RuntimeError(
                    "Pocket Option не вернул "
                    "достаточное количество свечей"
                )

            except asyncio.TimeoutError:

                print(
                    f"[OTC] {otc_symbol}: "
                    "таймаут получения свечей"
                )

            except Exception as exc:

                print(
                    f"[OTC] {otc_symbol}: "
                    f"ошибка: {exc}"
                )

            if attempt < OTC_RETRIES:

                await asyncio.sleep(1.5)

                # При проблеме соединения
                # полностью пересоздаём клиент.
                await self._close_pocket_client()

                if not await self._ensure_pocket_client():
                    return None

                client = self._pocket_client

        print(
            f"[OTC] {otc_symbol}: "
            "свечи получить не удалось"
        )

        return None

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

        normalized = self._normalize_forex_pair(
            pair
        )

        if not normalized:
            return None

        return await self._get_twelve_data_candles(
            normalized,
            limit=limit,
            interval=interval,
        )

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
            return None

        normalized = self._normalize_forex_pair(
            pair
        )

        if not normalized:
            return None

        interval_name = self._twelve_interval(
            interval
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

        except Exception:
            return None

        if not isinstance(
            payload,
            dict,
        ):
            return None

        if payload.get("status") == "error":
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

    @staticmethod
    def _twelve_interval(
        seconds: int,
    ) -> str:

        seconds = int(seconds)

        if seconds == 60:
            return "1min"

        if seconds == 300:
            return "5min"

        if seconds == 900:
            return "15min"

        if seconds == 1800:
            return "30min"

        if seconds == 3600:
            return "1h"

        if (
            seconds > 60
            and seconds % 60 == 0
        ):
            return (
                f"{seconds // 60}min"
            )

        return "1min"

    # ============================================================
    # TWELVE DATAFRAME
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

        df = df.rename(
            columns={
                "datetime": "timestamp",
                "date": "timestamp",
                "time": "timestamp",
            }
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

        df["timestamp"] = pd.to_datetime(
            df["timestamp"],
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

        df = df.sort_values(
            "timestamp"
        )

        df = df.drop_duplicates(
            subset=[
                "timestamp"
            ],
            keep="last",
        )

        return df.reset_index(
            drop=True
        )

    # ============================================================
    # POCKET DATAFRAME
    # ============================================================

    @staticmethod
    def _pocket_candles_to_dataframe(
        candles: Any,
    ) -> Optional[pd.DataFrame]:

        if candles is None:
            return None

        # --------------------------------------------------------
        # DATAFRAME
        # --------------------------------------------------------

        if isinstance(
            candles,
            pd.DataFrame,
        ):
            df = candles.copy()

        # --------------------------------------------------------
        # DICT
        # --------------------------------------------------------

        elif isinstance(
            candles,
            dict,
        ):

            nested = None

            for key in (
                "candles",
                "data",
                "history",
                "values",
                "result",
            ):

                if key not in candles:
                    continue

                value = candles[key]

                if isinstance(
                    value,
                    (
                        list,
                        tuple,
                    ),
                ):
                    nested = value
                    break

            if nested is not None:

                try:
                    df = pd.DataFrame(
                        nested
                    )
                except Exception:
                    return None

            else:

                try:
                    df = pd.DataFrame(
                        [candles]
                    )
                except Exception:
                    return None

        # --------------------------------------------------------
        # LIST
        # --------------------------------------------------------

        elif isinstance(
            candles,
            (
                list,
                tuple,
            ),
        ):

            rows = []

            for item in candles:

                if isinstance(
                    item,
                    dict,
                ):
                    rows.append(item)

                elif hasattr(
                    item,
                    "__dict__",
                ):

                    try:
                        rows.append(
                            dict(
                                item.__dict__
                            )
                        )
                    except Exception:
                        continue

                else:
                    rows.append(item)

            try:
                df = pd.DataFrame(
                    rows
                )
            except Exception:
                return None

        # --------------------------------------------------------
        # OBJECT
        # --------------------------------------------------------

        elif hasattr(
            candles,
            "__dict__",
        ):

            try:
                df = pd.DataFrame(
                    [
                        dict(
                            candles.__dict__
                        )
                    ]
                )
            except Exception:
                return None

        else:
            return None

        if df.empty:
            return None

        # --------------------------------------------------------
        # LOWERCASE
        # --------------------------------------------------------

        df = df.rename(
            columns={
                column: str(
                    column
                ).strip().lower()
                for column in df.columns
            }
        )

        aliases = {
            "time": "timestamp",
            "ts": "timestamp",
            "at": "timestamp",
            "datetime": "timestamp",
            "date": "timestamp",
            "open_price": "open",
            "openprice": "open",
            "high_price": "high",
            "highprice": "high",
            "low_price": "low",
            "lowprice": "low",
            "close_price": "close",
            "closeprice": "close",
        }

        df = df.rename(
            columns=aliases
        )

        # --------------------------------------------------------
        # OHLC
        # --------------------------------------------------------

        for column in (
            "open",
            "high",
            "low",
            "close",
        ):

            if column not in df.columns:
                return None

            df[column] = pd.to_numeric(
                df[column],
                errors="coerce",
            )

        if "timestamp" not in df.columns:
            return None

        # --------------------------------------------------------
        # TIMESTAMP
        # --------------------------------------------------------

        raw_timestamp = df[
            "timestamp"
        ]

        numeric_timestamp = pd.to_numeric(
            raw_timestamp,
            errors="coerce",
        )

        if numeric_timestamp.notna().any():

            valid = numeric_timestamp.dropna()

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

        # --------------------------------------------------------
        # CLEANUP
        # --------------------------------------------------------

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

        df = df.sort_values(
            "timestamp"
        )

        df = df.drop_duplicates(
            subset=[
                "timestamp"
            ],
            keep="last",
        )

        return df.reset_index(
            drop=True
        )

    # ============================================================
    # INTERVAL
    # ============================================================

    @staticmethod
    def _interval_to_seconds(
        interval: Any,
    ) -> int:

        if isinstance(
            interval,
            int,
        ):
            return interval

        if isinstance(
            interval,
            float,
        ):
            return int(interval)

        value = str(
            interval
        ).strip().lower()

        mapping = {
            "1s": 1,
            "5s": 5,
            "15s": 15,
            "30s": 30,
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
        }

        if value in mapping:
            return mapping[value]

        try:
            return int(value)
        except ValueError:
            return 60


# ================================================================
# GLOBAL CLIENT
# ================================================================

market_client = MarketClient()
