from __future__ import annotations
import asyncio
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
# ============================================================
# Pocket Option API
# ============================================================
try:
    from BinaryOptionsToolsV2.pocketoption import PocketOptionAsync
except ImportError:
    PocketOptionAsync = None
TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"
REQUEST_TIMEOUT = 20
POCKET_CONNECT_TIMEOUT = 30
# Кэш списка активов.
ASSET_CACHE_SECONDS = max(
    60,
    int(ASSET_DISCOVERY_CACHE_SECONDS or 300),
)
# Сколько раз пробуем получить OTC.
OTC_RETRIES = 2
class MarketClient:
    """
    Унифицированный источник рыночных данных.
    Обычные Forex:
        Twelve Data
    Pocket Option OTC:
        BinaryOptionsToolsV2 / PocketOptionAsync
    OTC требует POCKET_OPTION_SSID.
    SSID никогда не хранится в коде.
    """
    def __init__(self) -> None:
        self.session: Optional[aiohttp.ClientSession] = None
        self._asset_cache: list[str] = []
        self._asset_cache_time: float = 0.0
        self._pocket_client: Any = None
        self._pocket_lock = asyncio.Lock()
        self._pocket_ready = False
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
        """
        Полностью закрывает источники данных.
        """
        await self._close_pocket_client()
        if (
            self.session is not None
            and not self.session.closed
        ):
            await self.session.close()
        self.session = None
    # ============================================================
    # SYMBOL HELPERS
    # ============================================================
    @staticmethod
    def is_otc(value: str) -> bool:
        """
        Проверяет, является ли актив OTC.
        Поддерживает:
            EUR/USD OTC
            EURUSD OTC
            EURUSD_otc
            EURUSD-OTC
        """
        if not value:
            return False
        value = str(value).strip().lower()
        return (
            value.endswith("_otc")
            or value.endswith(" otc")
            or value.endswith("-otc")
            or value.endswith("otc")
        )
    @staticmethod
    def _clean_asset_name(
        value: str,
    ) -> str:
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
        EUR/USD -> EUR/USD
        EURUSD  -> EUR/USD
        OTC здесь намеренно исключён.
        """
        if not value:
            return None
        value = str(value).strip().upper()
        if "OTC" in value:
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
        Преобразует пользовательское имя OTC
        в Pocket Option symbol.
        EUR/USD OTC -> EURUSD_otc
        EURUSD OTC  -> EURUSD_otc
        Gold OTC    -> Gold_otc
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
        # Forex.
        compact = re.sub(
            r"[^A-Za-z]",
            "",
            clean,
        ).upper()
        if len(compact) == 6:
            return f"{compact}_otc"
        # Другие OTC активы.
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
        """
        Техническое имя -> имя для Telegram.
        """
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
    @staticmethod
    def _is_forex_pair(
        value: str,
    ) -> bool:
        return (
            MarketClient
            ._normalize_forex_pair(value)
            is not None
        )
    # ============================================================
    # ASSET DISCOVERY
    # ============================================================
    async def discover_pocket_assets(
        self,
    ) -> list[str]:
        """
        Получает список активов с публичной
        страницы Pocket Option.
        Важно:
        найденный OTC asset ещё не означает,
        что у нас есть доступ к его свечам.
        Свечи OTC берутся отдельно через
        PocketOptionAsync.
        """
        if not ASSET_DISCOVERY_ENABLED:
            return list(FALLBACK_PAIRS)
        now = time.monotonic()
        if (
            self._asset_cache
            and (
                now - self._asset_cache_time
                < ASSET_CACHE_SECONDS
            )
        ):
            return list(self._asset_cache)
        try:
            session = await self._get_session()
            async with session.get(
                POCKET_OPTION_ASSETS_URL,
            ) as response:
                if response.status != 200:
                    return list(
                        FALLBACK_PAIRS
                    )
                html = await response.text(
                    errors="ignore"
                )
            assets = self._parse_pocket_assets(
                html
            )
            if assets:
                self._asset_cache = assets
                self._asset_cache_time = now
                return list(assets)
        except Exception:
            pass
        return list(FALLBACK_PAIRS)
    async def discover_pocket_pairs(
        self,
    ) -> list[str]:
        """
        Совместимость со старым кодом.
        Возвращает обычные Forex.
        """
        assets = (
            await self.discover_pocket_assets()
        )
        pairs: list[str] = []
        for asset in assets:
            normalized = (
                self._normalize_forex_pair(
                    asset
                )
            )
            if (
                normalized
                and normalized not in pairs
            ):
                pairs.append(normalized)
        if not pairs:
            return list(FALLBACK_PAIRS)
        return pairs
    def _parse_pocket_assets(
        self,
        html: str,
    ) -> list[str]:
        """
        Извлекает названия активов.
        Парсер намеренно консервативный.
        """
        if not html:
            return []
        found: list[str] = []
        # --------------------------------------------
        # Forex OTC
        # --------------------------------------------
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
                    if isinstance(
                        match,
                        tuple,
                    )
                    else match
                )
                value = (
                    self._clean_asset_name(
                        value
                    )
                )
                if not value:
                    continue
                # Обычный Forex.
                normal = (
                    self._normalize_forex_pair(
                        value
                    )
                )
                if normal:
                    found.append(normal)
                    continue
                # OTC.
                otc = (
                    self.normalize_otc_symbol(
                        f"{value} OTC"
                    )
                )
                if otc:
                    found.append(otc)
        # --------------------------------------------
        # Обычные Forex
        # --------------------------------------------
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
            normalized = (
                self._normalize_forex_pair(
                    value
                )
            )
            if normalized:
                found.append(normalized)
        # --------------------------------------------
        # Unique
        # --------------------------------------------
        result: list[str] = []
        for asset in found:
            if asset not in result:
                result.append(asset)
        return result
    def _fallback_assets(
        self,
    ) -> list[str]:
        return list(FALLBACK_PAIRS)
    # ============================================================
    # AVAILABLE PAIRS
    # ============================================================
    async def get_available_pairs(
        self,
    ) -> list[str]:
        """
        Возвращает активы, которые реально
        могут использоваться анализатором.
        Forex:
            проверяем Twelve Data.
        OTC:
            добавляем только если SSID
            и OTC-клиент доступны.
        """
        discovered = (
            await self.discover_pocket_assets()
        )
        result: list[str] = []
        for asset in discovered:
            # -------------------------------
            # OTC
            # -------------------------------
            if self.is_otc(asset):
                if not self._pocket_ssid_available():
                    continue
                if PocketOptionAsync is None:
                    continue
                if asset not in result:
                    result.append(asset)
                continue
            # -------------------------------
            # Normal Forex
            # -------------------------------
            pair = (
                self._normalize_forex_pair(
                    asset
                )
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
            except Exception:
                continue
        if not result:
            return list(FALLBACK_PAIRS)
        return result
    # ============================================================
    # POCKET OPTION AUTH
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
        """
        Создаёт PocketOptionAsync.
        SSID берётся только из environment.
        """
        if (
            self._pocket_ready
            and self._pocket_client is not None
        ):
            return True
        if not self._pocket_ssid_available():
            return False
        if PocketOptionAsync is None:
            return False
        async with self._pocket_lock:
            if (
                self._pocket_ready
                and self._pocket_client is not None
            ):
                return True
            client = None
            try:
                client = PocketOptionAsync(
                    ssid=POCKET_OPTION_SSID
                )
                # В актуальных версиях соединение
                # может устанавливаться автоматически.
                # Если есть явный connect(),
                # вызываем его.
                connect_method = getattr(
                    client,
                    "connect",
                    None,
                )
                if callable(connect_method):
                    result = connect_method()
                    if asyncio.iscoroutine(
                        result
                    ):
                        await asyncio.wait_for(
                            result,
                            timeout=POCKET_CONNECT_TIMEOUT,
                        )
                # Небольшая задержка для WebSocket.
                await asyncio.sleep(
                    1.0
                )
                self._pocket_client = client
                self._pocket_ready = True
                return True
            except Exception:
                try:
                    if client is not None:
                        close_method = getattr(
                            client,
                            "disconnect",
                            None,
                        )
                        if callable(
                            close_method
                        ):
                            result = close_method()
                            if asyncio.iscoroutine(
                                result
                            ):
                                await result
                except Exception:
                    pass
                self._pocket_client = None
                self._pocket_ready = False
                return False
    async def _close_pocket_client(
        self,
    ) -> None:
        async with self._pocket_lock:
            client = self._pocket_client
            self._pocket_client = None
            self._pocket_ready = False
            if client is None:
                return
            for method_name in (
                "disconnect",
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
                    if asyncio.iscoroutine(
                        result
                    ):
                        await result
                    break
                except Exception:
                    continue
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
        Получает реальные OTC-свечи Pocket Option.
        Требуется:
            POCKET_OPTION_SSID
        """
        otc_symbol = (
            self.normalize_otc_symbol(
                symbol
            )
        )
        if not otc_symbol:
            return None
        if not await self._ensure_pocket_client():
            return None
        client = self._pocket_client
        if client is None:
            return None
        period = int(interval)
        # BinaryOptionsToolsV2 лучше работает
        # с get_candles_live, потому что он умеет
        # исторический backfill + текущие данные.
        get_live = getattr(
            client,
            "get_candles_live",
            None,
        )
        get_candles = getattr(
            client,
            "get_candles",
            None,
        )
        history_method = getattr(
            client,
            "history",
            None,
        )
        for attempt in range(
            OTC_RETRIES
        ):
            try:
                # =================================================
                # METHOD 1: get_candles_live
                # =================================================
                if callable(
                    get_live
                ):
                    rows = []
                    stream = get_live(
                        otc_symbol,
                        period=period,
                        hours=max(
                            1.0,
                            (
                                max(
                                    limit,
                                    100,
                                )
                                * period
                                / 3600
                            )
                            + 0.5,
                        ),
                        max_rows=max(
                            limit,
                            100,
                        ),
                    )
                    # async generator
                    async for item in stream:
                        # Формат:
                        # (closed_candles, forming)
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
                                list,
                            ):
                                rows.extend(
                                    closed
                                )
                        if forming:
                            if isinstance(
                                forming,
                                dict,
                            ):
                                rows.append(
                                    forming
                                )
                        # Нам не нужно держать
                        # бесконечный stream.
                        if len(rows) >= max(
                            limit,
                            100,
                        ):
                            break
                    df = (
                        self._pocket_candles_to_dataframe(
                            rows
                        )
                    )
                    if (
                        df is not None
                        and not df.empty
                    ):
                        if len(df) > limit:
                            df = df.tail(
                                limit
                            ).copy()
                        if len(df) >= 10:
                            return df
                # =================================================
                # METHOD 2: get_candles
                # =================================================
                if callable(
                    get_candles
                ):
                    result = None
                    try:
                        result = await get_candles(
                            otc_symbol,
                            period,
                            0,
                        )
                    except TypeError:
                        try:
                            result = await get_candles(
                                otc_symbol,
                                period=period,
                            )
                        except TypeError:
                            result = await get_candles(
                                otc_symbol,
                                period,
                            )
                    df = (
                        self._pocket_candles_to_dataframe(
                            result
                        )
                    )
                    if (
                        df is not None
                        and not df.empty
                    ):
                        if len(df) > limit:
                            df = df.tail(
                                limit
                            ).copy()
                        if len(df) >= 10:
                            return df
                # =================================================
                # METHOD 3: history
                # =================================================
                if callable(
                    history_method
                ):
                    result = None
                    try:
                        result = await history_method(
                            otc_symbol,
                            period=period,
                        )
                    except TypeError:
                        try:
                            result = await history_method(
                                otc_symbol,
                                period,
                            )
                        except TypeError:
                            result = await history_method(
                                otc_symbol
                            )
                    df = (
                        self._pocket_candles_to_dataframe(
                            result
                        )
                    )
                    if (
                        df is not None
                        and not df.empty
                    ):
                        if len(df) > limit:
                            df = df.tail(
                                limit
                            ).copy()
                        if len(df) >= 10:
                            return df
                raise RuntimeError(
                    "No OTC candles returned"
                )
            except Exception:
                if (
                    attempt + 1
                    < OTC_RETRIES
                ):
                    await asyncio.sleep(
                        1.5
                    )
                    # При ошибке соединения
                    # сбрасываем клиент.
                    self._pocket_ready = False
                    continue
                return None
        return None
    # ============================================================
    # MAIN CANDLES METHOD
    # ============================================================
    async def get_candles(
        self,
        pair: str,
        interval: int = CANDLE_INTERVAL,
        limit: int = CANDLE_LIMIT,
    ) -> Optional[pd.DataFrame]:
        """
        Главная функция.
        OTC -> Pocket Option
        Normal -> Twelve Data
        """
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
            session = (
                await self._get_session()
            )
            async with session.get(
                TWELVE_DATA_URL,
                params=params,
            ) as response:
                if response.status != 200:
                    return None
                payload = (
                    await response.json(
                        content_type=None
                    )
                )
        except Exception:
            return None
        if not isinstance(
            payload,
            dict,
        ):
            return None
        values = payload.get(
            "values"
        )
        if not isinstance(
            values,
            list,
        ):
            return None
        return (
            self._candles_to_dataframe(
                values
            )
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
    # TWELVE DATA DATAFRAME
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
        df["timestamp"] = (
            pd.to_datetime(
                df["timestamp"],
                utc=True,
                errors="coerce",
            )
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
        df = df.reset_index(
            drop=True
        )
        return df
    # ============================================================
    # POCKET OPTION DATAFRAME
    # ============================================================
    @staticmethod
    def _pocket_candles_to_dataframe(
        candles: Any,
    ) -> Optional[pd.DataFrame]:
        if candles is None:
            return None
        # --------------------------------------------------------
        # DataFrame
        # --------------------------------------------------------
        if isinstance(
            candles,
            pd.DataFrame,
        ):
            df = candles.copy()
        # --------------------------------------------------------
        # Dict
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
                if key in candles:
                    value = candles[
                        key
                    ]
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
        # List
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
        # Single object
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
        # Lowercase columns
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
        # Timestamp
        # --------------------------------------------------------
        raw_timestamp = df[
            "timestamp"
        ]
        numeric_timestamp = (
            pd.to_numeric(
                raw_timestamp,
                errors="coerce",
            )
        )
        if numeric_timestamp.notna().any():
            valid = (
                numeric_timestamp
                .dropna()
            )
            median_value = float(
                valid.median()
            )
            if (
                median_value
                > 100_000_000_000
            ):
                df["timestamp"] = (
                    pd.to_datetime(
                        numeric_timestamp,
                        unit="ms",
                        utc=True,
                        errors="coerce",
                    )
                )
            else:
                df["timestamp"] = (
                    pd.to_datetime(
                        numeric_timestamp,
                        unit="s",
                        utc=True,
                        errors="coerce",
                    )
                )
        else:
            df["timestamp"] = (
                pd.to_datetime(
                    raw_timestamp,
                    utc=True,
                    errors="coerce",
                )
            )
        # --------------------------------------------------------
        # Cleanup
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
        df = df.reset_index(
            drop=True
        )
        return df
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
