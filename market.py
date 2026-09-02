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
try:
    # Не импортируем библиотеку при запуске файла.
    # Она понадобится только если реально используется OTC.
    from pocketoptionapi import PocketOption
except ImportError:
    PocketOption = None
TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"
REQUEST_TIMEOUT = 15
POCKET_CONNECT_TIMEOUT = 20
# Большой offset нужен Pocket Option для стабильного получения истории.
POCKET_HISTORY_OFFSET = 45000
# Максимальное количество попыток получить свечи.
MAX_OTC_RETRIES = 2
class MarketClient:
    """
    Источник рыночных данных.
    Обычные Forex:
        Twelve Data
    Pocket Option OTC:
        Pocket Option WebSocket API, если задан POCKET_OPTION_SSID.
    ВАЖНО:
        Без SSID Pocket Option OTC свечи невозможно достоверно получить
        из публичного API Pocket Option. Поэтому OTC в таком случае
        возвращает None, а не подменяется обычным Forex.
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
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
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
        Закрывает HTTP и Pocket Option соединения.
        """
        await self._close_pocket_client()
        if self.session is not None and not self.session.closed:
            await self.session.close()
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
            or value.endswith(" otc")
            or value.endswith("-otc")
            or value.endswith("otc")
        )
    @staticmethod
    def _clean_asset_name(value: str) -> str:
        if not value:
            return ""
        value = str(value).strip()
        value = re.sub(r"\s+", " ", value)
        return value
    @staticmethod
    def _normalize_forex_pair(value: str) -> Optional[str]:
        """
        EUR/USD -> EUR/USD
        EURUSD  -> EUR/USD
        OTC намеренно не проходит через этот метод.
        """
        if not value:
            return None
        value = str(value).strip().upper()
        if "OTC" in value:
            return None
        value = value.replace(" ", "")
        value = value.replace("-", "/")
        value = value.replace("_", "/")
        match = re.fullmatch(r"([A-Z]{3})/?([A-Z]{3})", value)
        if not match:
            return None
        return f"{match.group(1)}/{match.group(2)}"
    @staticmethod
    def normalize_otc_symbol(value: str) -> Optional[str]:
        """
        Приводит название OTC к Pocket Option symbol.
        EUR/USD OTC -> EURUSD_otc
        EURUSD OTC  -> EURUSD_otc
        Gold OTC    -> Gold_otc
        """
        if not value:
            return None
        raw = str(value).strip()
        if not MarketClient.is_otc(raw):
            return None
        # Убираем различные варианты OTC.
        clean = re.sub(
            r"[\s_\-]*otc$",
            "",
            raw,
            flags=re.IGNORECASE,
        )
        clean = clean.strip()
        # Forex:
        compact = re.sub(r"[^A-Za-z]", "", clean).upper()
        if len(compact) == 6:
            return f"{compact}_otc"
        # Остальные активы.
        normalized = re.sub(r"\s+", "", clean)
        if not normalized:
            return None
        return f"{normalized}_otc"
    @staticmethod
    def display_asset_name(value: str) -> str:
        """
        Преобразует техническое имя в понятное пользователю.
        """
        if not value:
            return value
        value = str(value).strip()
        if value.lower().endswith("_otc"):
            base = value[:-4]
            if len(base) == 6 and base.isalpha():
                base = base.upper()
                return f"{base[:3]}/{base[3:]} OTC"
            return f"{base} OTC"
        return value
    @staticmethod
    def _is_forex_pair(value: str) -> bool:
        return MarketClient._normalize_forex_pair(value) is not None
    # ============================================================
    # ASSET DISCOVERY
    # ============================================================
    async def discover_pocket_assets(self) -> list[str]:
        """
        Получает список активов с публичной страницы Pocket Option.
        Это только discovery.
        Получение свечей OTC всё равно требует авторизованного
        Pocket Option WebSocket.
        """
        if not ASSET_DISCOVERY_ENABLED:
            return list(FALLBACK_PAIRS)
        now = time.monotonic()
        if (
            self._asset_cache
            and now - self._asset_cache_time
            < ASSET_DISCOVERY_CACHE_SECONDS
        ):
            return list(self._asset_cache)
        try:
            session = await self._get_session()
            async with session.get(
                POCKET_OPTION_ASSETS_URL
            ) as response:
                if response.status != 200:
                    return list(FALLBACK_PAIRS)
                html = await response.text(
                    errors="ignore"
                )
            assets = self._parse_pocket_assets(html)
            if assets:
                self._asset_cache = assets
                self._asset_cache_time = now
                return list(assets)
        except Exception:
            pass
        return list(FALLBACK_PAIRS)
    async def discover_pocket_pairs(self) -> list[str]:
        """
        Совместимость со старым кодом.
        Возвращает только обычные Forex-пары.
        """
        assets = await self.discover_pocket_assets()
        pairs: list[str] = []
        for asset in assets:
            normalized = self._normalize_forex_pair(asset)
            if normalized and normalized not in pairs:
                pairs.append(normalized)
        if not pairs:
            return list(FALLBACK_PAIRS)
        return pairs
    def _parse_pocket_assets(self, html: str) -> list[str]:
        """
        Эвристический парсер названий активов.
        Мы не используем найденные OTC-имена как доказательство
        наличия свечей. Они только добавляются в список активов.
        """
        found: list[str] = []
        if not html:
            return found
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
                value = self._clean_asset_name(value)
                if not value:
                    continue
                if self._is_forex_pair(value):
                    normalized = self._normalize_forex_pair(value)
                    if normalized:
                        found.append(normalized)
                    continue
                otc = self.normalize_otc_symbol(
                    f"{value} OTC"
                )
                if otc:
                    found.append(otc)
        # Также ищем обычные Forex.
        normal_pattern = r"\b([A-Z]{3}/[A-Z]{3})\b"
        try:
            normal_matches = re.findall(
                normal_pattern,
                html,
                flags=re.IGNORECASE,
            )
        except Exception:
            normal_matches = []
        for value in normal_matches:
            normalized = self._normalize_forex_pair(value)
            if normalized:
                found.append(normalized)
        # Уникализация.
        result: list[str] = []
        for asset in found:
            if asset not in result:
                result.append(asset)
        return result
    def _fallback_assets(self) -> list[str]:
        return list(FALLBACK_PAIRS)
    async def get_available_pairs(self) -> list[str]:
        """
        Возвращает активы, которые реально можно анализировать.
        Обычные Forex:
            проверяются через Twelve Data.
        OTC:
            добавляются ТОЛЬКО если доступен Pocket Option SSID.
        Это важно:
            если SSID отсутствует, бот не будет выбирать OTC,
            после чего получать None и ломать анализ.
        """
        discovered = await self.discover_pocket_assets()
        result: list[str] = []
        for asset in discovered:
            if self.is_otc(asset):
                # OTC нельзя считать доступным только потому,
                # что название найдено на странице.
                if self._pocket_ssid_available():
                    if asset not in result:
                        result.append(asset)
                continue
            pair = self._normalize_forex_pair(asset)
            if not pair:
                continue
            # Проверяем, что Twelve Data реально может дать свечи.
            try:
                candles = await self._get_twelve_data_candles(
                    pair,
                    limit=5,
                )
                if candles is not None and not candles.empty:
                    if pair not in result:
                        result.append(pair)
            except Exception:
                continue
        if not result:
            return list(FALLBACK_PAIRS)
        return result
    # ============================================================
    # POCKET OPTION
    # ============================================================
    @staticmethod
    def _pocket_ssid_available() -> bool:
        return bool(
            isinstance(POCKET_OPTION_SSID, str)
            and POCKET_OPTION_SSID.strip()
        )
    async def _ensure_pocket_client(self) -> bool:
        """
        Подключает Pocket Option только когда требуется OTC.
        ВАЖНО:
            SSID берётся только из environment variable.
            В коде/репозитории он не хранится.
        """
        if self._pocket_ready and self._pocket_client is not None:
            return True
        if not self._pocket_ssid_available():
            return False
        if PocketOption is None:
            return False
        async with self._pocket_lock:
            if (
                self._pocket_ready
                and self._pocket_client is not None
            ):
                return True
            try:
                client = PocketOption(
                    POCKET_OPTION_SSID
                )
                # connect() у этой библиотеки синхронный.
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        client.connect
                    ),
                    timeout=POCKET_CONNECT_TIMEOUT,
                )
                # Разные версии библиотеки возвращают:
                #   bool
                #   tuple(bool, error)
                #   None
                #
                # Поэтому проверяем мягко.
                if isinstance(result, tuple):
                    connected = bool(result[0])
                elif isinstance(result, bool):
                    connected = result
                else:
                    connected = True
                if not connected:
                    try:
                        client.close()
                    except Exception:
                        pass
                    return False
                # Ждём готовности WebSocket.
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        connected_now = bool(
                            client.check_connect()
                        )
                    except Exception:
                        connected_now = True
                    try:
                        synced = bool(
                            client.is_time_synced()
                        )
                    except Exception:
                        synced = True
                    if connected_now and synced:
                        break
                    await asyncio.sleep(0.25)
                self._pocket_client = client
                self._pocket_ready = True
                return True
            except Exception:
                self._pocket_client = None
                self._pocket_ready = False
                return False
    async def _close_pocket_client(self) -> None:
        async with self._pocket_lock:
            client = self._pocket_client
            self._pocket_client = None
            self._pocket_ready = False
            if client is None:
                return
            try:
                if hasattr(client, "close"):
                    await asyncio.to_thread(
                        client.close
                    )
                elif hasattr(client, "disconnect"):
                    await asyncio.to_thread(
                        client.disconnect
                    )
            except Exception:
                pass
    async def get_otc_candles(
        self,
        symbol: str,
        interval: int = CANDLE_INTERVAL,
        limit: int = CANDLE_LIMIT,
    ) -> Optional[pd.DataFrame]:
        """
        Получает настоящие Pocket Option OTC свечи.
        Требуется POCKET_OPTION_SSID.
        Пример:
            EURUSD_otc
        """
        otc_symbol = self.normalize_otc_symbol(symbol)
        if not otc_symbol:
            return None
        if not await self._ensure_pocket_client():
            return None
        client = self._pocket_client
        if client is None:
            return None
        period = int(interval)
        for attempt in range(MAX_OTC_RETRIES):
            try:
                # Подписка помогает Pocket Option подготовить
                # поток/историю конкретного актива.
                try:
                    await asyncio.to_thread(
                        client.subscribe,
                        otc_symbol,
                        period=period,
                    )
                except TypeError:
                    try:
                        await asyncio.to_thread(
                            client.subscribe,
                            otc_symbol,
                            period,
                        )
                    except Exception:
                        pass
                except Exception:
                    pass
                await asyncio.sleep(0.5)
                candles = None
                # Основной метод старой stable_api.
                if hasattr(
                    client,
                    "get_historical_candles",
                ):
                    try:
                        candles = await asyncio.to_thread(
                            client.get_historical_candles,
                            otc_symbol,
                            period=period,
                            offset=POCKET_HISTORY_OFFSET,
                            count_request=1,
                        )
                    except TypeError:
                        candles = await asyncio.to_thread(
                            client.get_historical_candles,
                            otc_symbol,
                            period,
                            POCKET_HISTORY_OFFSET,
                            1,
                        )
                # Некоторые версии имеют get_candles.
                elif hasattr(client, "get_candles"):
                    try:
                        candles = await asyncio.to_thread(
                            client.get_candles,
                            otc_symbol,
                            period,
                            POCKET_HISTORY_OFFSET,
                        )
                    except TypeError:
                        candles = await asyncio.to_thread(
                            client.get_candles,
                            otc_symbol,
                            period,
                            limit,
                        )
                df = self._pocket_candles_to_dataframe(
                    candles
                )
                if df is None or df.empty:
                    raise RuntimeError(
                        "Pocket Option returned no OTC candles"
                    )
                # Оставляем достаточно данных для индикаторов.
                if limit > 0 and len(df) > limit:
                    df = df.tail(limit).copy()
                if len(df) < 10:
                    raise RuntimeError(
                        "Not enough OTC candles"
                    )
                return df
            except Exception:
                if attempt + 1 < MAX_OTC_RETRIES:
                    await asyncio.sleep(1.0)
                    continue
                # Возможно, соединение умерло.
                self._pocket_ready = False
                return None
        return None
    # ============================================================
    # PUBLIC CANDLES API
    # ============================================================
    async def get_candles(
        self,
        pair: str,
        interval: int = CANDLE_INTERVAL,
        limit: int = CANDLE_LIMIT,
    ) -> Optional[pd.DataFrame]:
        """
        Главная функция получения свечей.
        OTC:
            Pocket Option
        Обычные пары:
            Twelve Data
        """
        if not pair:
            return None
        if self.is_otc(pair):
            return await self.get_otc_candles(
                pair,
                interval=interval,
                limit=limit,
            )
        normalized = self._normalize_forex_pair(pair)
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
        """
        Twelve Data источник для обычного Forex.
        """
        if not TWELVE_DATA_API_KEY:
            return None
        normalized = self._normalize_forex_pair(pair)
        if not normalized:
            return None
        # Twelve Data использует формат EUR/USD.
        symbol = normalized
        interval_name = self._twelve_interval(interval)
        params = {
            "symbol": symbol,
            "interval": interval_name,
            "outputsize": max(1, int(limit)),
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
        if not isinstance(payload, dict):
            return None
        if "values" not in payload:
            return None
        values = payload.get("values")
        if not isinstance(values, list):
            return None
        return self._candles_to_dataframe(
            values
        )
    @staticmethod
    def _twelve_interval(seconds: int) -> str:
        seconds = int(seconds)
        mapping = {
            1: "1min",
            5: "5min",
            15: "15min",
            30: "30min",
            60: "1h",
            300: "5h",
        }
        # Для твоего бота основное значение — 60 секунд.
        # Twelve Data использует 1min для минутных свечей.
        if seconds == 60:
            return "1min"
        if seconds in mapping:
            return mapping[seconds]
        # Безопасный fallback.
        if seconds % 60 == 0:
            return f"{seconds // 60}min"
        return "1min"
    # ============================================================
    # DATAFRAME NORMALIZATION
    # ============================================================
    @staticmethod
    def _candles_to_dataframe(
        candles: Any,
    ) -> Optional[pd.DataFrame]:
        """
        Нормализация Twelve Data.
        """
        if candles is None:
            return None
        try:
            df = pd.DataFrame(candles)
        except Exception:
            return None
        if df.empty:
            return None
        # Twelve Data:
        # datetime, open, high, low, close, volume
        rename_map = {
            "datetime": "timestamp",
            "date": "timestamp",
            "time": "timestamp",
        }
        df = df.rename(
            columns=rename_map
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
        try:
            df["timestamp"] = pd.to_datetime(
                df["timestamp"],
                utc=True,
                errors="coerce",
            )
        except Exception:
            return None
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
            subset=["timestamp"],
            keep="last",
        )
        df = df.reset_index(
            drop=True
        )
        return df
    @staticmethod
    def _pocket_candles_to_dataframe(
        candles: Any,
    ) -> Optional[pd.DataFrame]:
        """
        Нормализует разные форматы свечей Pocket Option.
        """
        if candles is None:
            return None
        # DataFrame уже готов.
        if isinstance(candles, pd.DataFrame):
            df = candles.copy()
        elif isinstance(candles, dict):
            # Иногда API возвращает:
            # {"candles": [...]}
            for key in (
                "candles",
                "data",
                "history",
                "values",
            ):
                if key in candles:
                    nested = candles[key]
                    if isinstance(
                        nested,
                        (list, tuple),
                    ):
                        candles = nested
                        break
            if isinstance(candles, dict):
                try:
                    df = pd.DataFrame(
                        [candles]
                    )
                except Exception:
                    return None
            else:
                try:
                    df = pd.DataFrame(
                        candles
                    )
                except Exception:
                    return None
        elif isinstance(
            candles,
            (list, tuple),
        ):
            try:
                rows: list[dict[str, Any]] = []
                for item in candles:
                    if isinstance(item, dict):
                        rows.append(item)
                    elif hasattr(
                        item,
                        "__dict__",
                    ):
                        rows.append(
                            dict(item.__dict__)
                        )
                    else:
                        rows.append(
                            item
                        )
                df = pd.DataFrame(rows)
            except Exception:
                return None
        else:
            # Один объект Candle.
            if hasattr(
                candles,
                "__dict__",
            ):
                try:
                    df = pd.DataFrame(
                        [dict(candles.__dict__)]
                    )
                except Exception:
                    return None
            else:
                return None
        if df.empty:
            return None
        # Унификация названий.
        normalized_columns = {}
        for column in df.columns:
            clean = str(column).strip().lower()
            normalized_columns[column] = clean
        df = df.rename(
            columns=normalized_columns
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
        required = [
            "open",
            "high",
            "low",
            "close",
        ]
        # Иногда timestamp находится под "id".
        if "timestamp" not in df.columns:
            for candidate in (
                "timestamp",
                "time",
                "ts",
                "date",
            ):
                if candidate in df.columns:
                    df["timestamp"] = df[
                        candidate
                    ]
                    break
        for column in required:
            if column not in df.columns:
                return None
            df[column] = pd.to_numeric(
                df[column],
                errors="coerce",
            )
        if "timestamp" not in df.columns:
            return None
        # Timestamp может быть:
        # Unix seconds
        # Unix milliseconds
        # datetime string
        raw_timestamp = df[
            "timestamp"
        ]
        numeric_timestamp = pd.to_numeric(
            raw_timestamp,
            errors="coerce",
        )
        if (
            numeric_timestamp.notna().any()
        ):
            median_value = float(
                numeric_timestamp.dropna().median()
            )
            if median_value > 100_000_000_000:
                # milliseconds
                df["timestamp"] = pd.to_datetime(
                    numeric_timestamp,
                    unit="ms",
                    utc=True,
                    errors="coerce",
                )
            else:
                # seconds
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
        df = df.sort_values(
            "timestamp"
        )
        df = df.drop_duplicates(
            subset=["timestamp"],
            keep="last",
        )
        df = df.reset_index(
            drop=True
        )
        return df
    # ============================================================
    # TIMEFRAME
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
