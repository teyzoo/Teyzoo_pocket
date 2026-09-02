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

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"


class MarketClient:
    """
    Единый клиент рыночных данных.

    Обычные Forex:
        EUR/USD
        GBP/USD
        USD/JPY

        -> Twelve Data

    OTC Pocket Option:
        EUR/USD OTC
        GBP/USD OTC
        Gold OTC
        Bitcoin OTC
        и т.д.

        -> Pocket Option API/WebSocket client,
           если POCKET_OPTION_SSID задан.

    ВАЖНО:
    OTC никогда не преобразуется в обычный EUR/USD.
    """

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

        self._asset_cache: list[str] = []
        self._asset_cache_time: float = 0
        self._asset_lock = asyncio.Lock()

        self._pocket_client: Any = None
        self._pocket_lock = asyncio.Lock()
        self._pocket_ready = False

    # ============================================================
    # SESSION
    # ============================================================

    async def _get_session(self) -> aiohttp.ClientSession:
        if (
            self.session is None
            or self.session.closed
        ):
            timeout = aiohttp.ClientTimeout(
                total=30
            )

            self.session = aiohttp.ClientSession(
                timeout=timeout
            )

        return self.session

    async def close(self) -> None:
        """
        Полностью закрывает HTTP и Pocket Option
        подключения.
        """

        if (
            self.session is not None
            and not self.session.closed
        ):
            await self.session.close()

        self.session = None

        await self._close_pocket_client()

    # ============================================================
    # PAIR / ASSET HELPERS
    # ============================================================

    @staticmethod
    def is_otc(value: str) -> bool:
        """
        Определяет, является ли актив OTC.

        Поддерживает:

            EUR/USD OTC
            EURUSD OTC
            EURUSD_otc
            EURUSD-OTC
        """

        if not value:
            return False

        value = str(value).strip().upper()

        return bool(
            re.search(
                r"(?:\s|_|-)?OTC$",
                value,
            )
        )

    @staticmethod
    def _clean_asset_name(value: str) -> str:
        """
        Чистит название актива, сохраняя OTC.
        """

        if not value:
            return ""

        value = (
            str(value)
            .replace("&AMP;", "&")
            .strip()
        )

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
        Нормализует ТОЛЬКО обычную Forex-пару.

        OTC здесь специально НЕ удаляется.

        EUR/USD -> EUR/USD
        EURUSD  -> EUR/USD
        EUR-USD -> EUR/USD
        EUR_USD -> EUR/USD
        """

        if not value:
            return None

        value = str(value).upper().strip()

        if MarketClient.is_otc(value):
            return None

        value = value.replace(
            "-",
            "/",
        )

        value = value.replace(
            "_",
            "/",
        )

        value = value.replace(
            "\\",
            "/",
        )

        compact = value.replace(
            "/",
            "",
        )

        if (
            len(compact) == 6
            and compact.isalpha()
        ):
            return (
                f"{compact[:3]}/"
                f"{compact[3:]}"
            )

        match = re.fullmatch(
            r"([A-Z]{3})/([A-Z]{3})",
            value,
        )

        if match:
            return (
                f"{match.group(1)}/"
                f"{match.group(2)}"
            )

        return None

    @staticmethod
    def normalize_otc_symbol(
        value: str,
    ) -> Optional[str]:
        """
        Преобразует отображаемое имя Pocket Option
        в символ API.

        EUR/USD OTC
            ->
        EURUSD_otc

        EURUSD OTC
            ->
        EURUSD_otc
        """

        if not value:
            return None

        raw = str(value).strip()

        if not MarketClient.is_otc(raw):
            return None

        raw = re.sub(
            r"(?:\s|_|-)OTC$",
            "",
            raw,
            flags=re.IGNORECASE,
        )

        raw = raw.upper().strip()

        # Валютная пара.
        compact = re.sub(
            r"[^A-Z]",
            "",
            raw,
        )

        if (
            len(compact) == 6
            and compact.isalpha()
        ):
            return f"{compact}_otc"

        # Другие OTC активы.
        #
        # Например:
        # Gold OTC -> Gold_otc
        # Bitcoin OTC -> Bitcoin_otc
        #
        # Для таких активов сохраняем название.
        clean = re.sub(
            r"[^A-Za-z0-9]+",
            "_",
            raw,
        ).strip("_")

        if not clean:
            return None

        return f"{clean}_otc"

    @staticmethod
    def display_asset_name(
        value: str,
    ) -> str:
        """
        Приводит API symbol обратно к понятному
        названию для Telegram.

        EURUSD_otc -> EUR/USD OTC
        """

        if not value:
            return ""

        raw = str(value).strip()

        if raw.lower().endswith("_otc"):
            base = raw[:-4]

            compact = re.sub(
                r"[^A-Za-z]",
                "",
                base,
            )

            if (
                len(compact) == 6
                and compact.isalpha()
            ):
                return (
                    f"{compact[:3].upper()}/"
                    f"{compact[3:].upper()} OTC"
                )

            return f"{base} OTC"

        normalized = (
            MarketClient._normalize_forex_pair(
                raw
            )
        )

        if normalized:
            return normalized

        return raw

    # ============================================================
    # FOREX CHECK
    # ============================================================

    @staticmethod
    def _is_forex_pair(
        pair: str,
    ) -> bool:
        normalized = (
            MarketClient._normalize_forex_pair(
                pair
            )
        )

        if not normalized:
            return False

        base, quote = normalized.split("/")

        return (
            len(base) == 3
            and len(quote) == 3
            and base.isalpha()
            and quote.isalpha()
        )

    # ============================================================
    # POCKET OPTION ASSET DISCOVERY
    # ============================================================

    async def discover_pocket_assets(
        self,
    ) -> list[str]:
        """
        Получает актуальные активы Pocket Option.

        Возвращает:

            EUR/USD
            GBP/USD
            EUR/USD OTC
            Gold OTC
            Bitcoin OTC
            ...

        OTC НЕ удаляется.
        """

        if not ASSET_DISCOVERY_ENABLED:
            print(
                "ℹ️ Asset discovery отключён."
            )

            return self._fallback_assets()

        now = time.monotonic()

        if (
            self._asset_cache
            and (
                now - self._asset_cache_time
                < ASSET_DISCOVERY_CACHE_SECONDS
            )
        ):
            return self._asset_cache.copy()

        async with self._asset_lock:
            now = time.monotonic()

            if (
                self._asset_cache
                and (
                    now - self._asset_cache_time
                    < ASSET_DISCOVERY_CACHE_SECONDS
                )
            ):
                return self._asset_cache.copy()

            print("")
            print(
                "🌐 Получение актуального списка "
                "активов Pocket Option..."
            )

            try:
                session = await self._get_session()

                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 "
                        "(iPhone; CPU iPhone OS 17_0 "
                        "like Mac OS X) "
                        "AppleWebKit/605.1.15 "
                        "Version/17.0 Mobile/15E148 "
                        "Safari/604.1"
                    ),
                    "Accept": (
                        "text/html,"
                        "application/xhtml+xml,"
                        "application/xml;q=0.9,"
                        "*/*;q=0.8"
                    ),
                    "Accept-Language": (
                        "en-US,en;q=0.9"
                    ),
                }

                async with session.get(
                    POCKET_OPTION_ASSETS_URL,
                    headers=headers,
                ) as response:

                    if response.status != 200:
                        print(
                            "⚠️ Pocket Option assets "
                            f"HTTP {response.status}"
                        )

                        return self._fallback_assets()

                    html = await response.text(
                        errors="ignore"
                    )

            except Exception as exc:
                print(
                    "⚠️ Не удалось получить список "
                    "Pocket Option:"
                )

                print(
                    f"   {type(exc).__name__}: {exc}"
                )

                return self._fallback_assets()

            assets = self._parse_pocket_assets(
                html
            )

            if not assets:
                print(
                    "⚠️ Не удалось извлечь активы "
                    "из Pocket Option."
                )

                return self._fallback_assets()

            self._asset_cache = assets
            self._asset_cache_time = (
                time.monotonic()
            )

            normal_count = sum(
                1
                for asset in assets
                if not self.is_otc(asset)
            )

            otc_count = sum(
                1
                for asset in assets
                if self.is_otc(asset)
            )

            print(
                "✅ Pocket Option assets:"
            )

            print(
                f"   Обычные: {normal_count}"
            )

            print(
                f"   OTC: {otc_count}"
            )

            print(
                f"   Всего: {len(assets)}"
            )

            return assets.copy()

    async def discover_pocket_pairs(
        self,
    ) -> list[str]:
        """
        Совместимость со старым scheduler.py.

        Возвращает только обычные Forex-пары.
        Для всех активов используется
        discover_pocket_assets().
        """

        assets = (
            await self.discover_pocket_assets()
        )

        result: list[str] = []

        for asset in assets:
            if self.is_otc(asset):
                continue

            normalized = (
                self._normalize_forex_pair(
                    asset
                )
            )

            if (
                normalized
                and normalized not in result
            ):
                result.append(normalized)

        if not result:
            return self._fallback_assets()

        return result

    # ============================================================
    # PARSE POCKET OPTION PAGE
    # ============================================================

    def _parse_pocket_assets(
        self,
        html: str,
    ) -> list[str]:
        """
        Извлекает обычные и OTC активы.

        Сначала ищет валютные OTC:
            EUR/USD OTC

        Затем остальные OTC:
            Gold OTC
            Bitcoin OTC
            Tesla OTC

        И затем обычные Forex.
        """

        if not html:
            return []

        text = html.upper()

        found: list[str] = []

        def add_asset(
            asset: str,
        ) -> None:
            asset = self._clean_asset_name(
                asset
            )

            if not asset:
                return

            if asset not in found:
                found.append(asset)

        # --------------------------------------------------------
        # 1. Валютные OTC.
        # --------------------------------------------------------

        otc_forex_patterns = [
            r"\b([A-Z]{3}\s*/\s*[A-Z]{3}\s+OTC)\b",
            r"\b([A-Z]{3}\s*[A-Z]{3}\s+OTC)\b",
            r"\b([A-Z]{3}\s*[-_]\s*[A-Z]{3}\s+OTC)\b",
        ]

        for pattern in otc_forex_patterns:
            matches = re.findall(
                pattern,
                text,
            )

            for match in matches:
                normalized = (
                    MarketClient._normalize_otc_display(
                        match
                    )
                )

                if normalized:
                    add_asset(normalized)

        # --------------------------------------------------------
        # 2. Обычные валютные пары.
        # --------------------------------------------------------

        normal_patterns = [
            r"\b([A-Z]{3}\s*/\s*[A-Z]{3})\b",
            r"\b([A-Z]{3}[A-Z]{3})\b",
        ]

        for pattern in normal_patterns:
            matches = re.findall(
                pattern,
                text,
            )

            for match in matches:
                normalized = (
                    self._normalize_forex_pair(
                        match
                    )
                )

                if not normalized:
                    continue

                base, quote = normalized.split("/")

                if base == quote:
                    continue

                add_asset(normalized)

        # --------------------------------------------------------
        # 3. Другие OTC активы.
        #
        # Берём названия непосредственно перед OTC.
        #
        # Примеры:
        # Gold OTC
        # Bitcoin OTC
        # Tesla OTC
        # Microsoft OTC
        # --------------------------------------------------------

        other_otc_patterns = [
            r"\b([A-Z][A-Z0-9&.\- ]{1,50}?)\s+OTC\b",
        ]

        for pattern in other_otc_patterns:
            matches = re.findall(
                pattern,
                text,
            )

            for match in matches:
                candidate = (
                    self._clean_asset_name(
                        match
                    )
                )

                if not candidate:
                    continue

                candidate = candidate.strip()

                # Не добавляем мусор.
                if len(candidate) < 2:
                    continue

                # Если это валютная пара —
                # она уже обработана.
                if re.fullmatch(
                    r"[A-Z]{3}\s*/\s*[A-Z]{3}",
                    candidate,
                ):
                    continue

                if re.fullmatch(
                    r"[A-Z]{6}",
                    candidate,
                ):
                    continue

                # HTML/служебный мусор.
                forbidden = {
                    "ASSET IS AVAILABLE",
                    "AVAILABLE",
                    "TRADING",
                    "CURRENT",
                    "OPTION",
                    "PAYOUT",
                    "CURRENCY",
                    "COMMODITIES",
                    "STOCKS",
                    "CRYPTOCURRENCIES",
                    "INDICES",
                }

                if candidate in forbidden:
                    continue

                add_asset(
                    f"{candidate} OTC"
                )

        # --------------------------------------------------------
        # 4. Финальная очистка.
        # --------------------------------------------------------

        cleaned: list[str] = []

        for asset in found:
            asset = re.sub(
                r"\s+",
                " ",
                asset,
            ).strip()

            if not asset:
                continue

            if asset not in cleaned:
                cleaned.append(asset)

        return cleaned

    @staticmethod
    def _normalize_otc_display(
        value: str,
    ) -> Optional[str]:
        """
        EURUSD OTC / EUR/USD OTC
        ->
        EUR/USD OTC
        """

        if not value:
            return None

        raw = str(value).upper().strip()

        raw = re.sub(
            r"\s+",
            " ",
            raw,
        )

        raw = re.sub(
            r"\s*OTC$",
            "",
            raw,
        ).strip()

        compact = re.sub(
            r"[^A-Z]",
            "",
            raw,
        )

        if (
            len(compact) == 6
            and compact.isalpha()
        ):
            if compact[:3] == compact[3:]:
                return None

            return (
                f"{compact[:3]}/"
                f"{compact[3:]} OTC"
            )

        return None

    # ============================================================
    # FALLBACK
    # ============================================================

    def _fallback_assets(self) -> list[str]:
        """
        Fallback для обычных Forex.

        OTC намеренно здесь не выдумываем:
        без актуального Pocket Option списка
        нельзя гарантировать доступность OTC.
        """

        print(
            "🔄 Используем fallback список "
            "обычных FX-пар."
        )

        pairs: list[str] = []

        for pair in FALLBACK_PAIRS:
            normalized = (
                self._normalize_forex_pair(
                    pair
                )
            )

            if (
                normalized
                and self._is_forex_pair(
                    normalized
                )
                and normalized not in pairs
            ):
                pairs.append(normalized)

        return pairs

    # ============================================================
    # AVAILABLE PAIRS / ASSETS
    # ============================================================

    async def get_available_pairs(
        self,
    ) -> list[str]:
        """
        Возвращает обычные + OTC активы.

        ВАЖНО:
        Для обычных Forex дополнительно проверяем
        Twelve Data.

        Для OTC проверка Twelve Data НЕ используется.
        """

        assets = (
            await self.discover_pocket_assets()
        )

        if not assets:
            return self._fallback_assets()

        normal_assets: list[str] = []
        otc_assets: list[str] = []

        for asset in assets:
            if self.is_otc(asset):
                if asset not in otc_assets:
                    otc_assets.append(asset)

                continue

            normalized = (
                self._normalize_forex_pair(
                    asset
                )
            )

            if (
                normalized
                and normalized not in normal_assets
            ):
                normal_assets.append(normalized)

        print("")
        print(
            f"🔎 Обычных FX-кандидатов: "
            f"{len(normal_assets)}"
        )

        print(
            f"🔎 OTC-кандидатов: "
            f"{len(otc_assets)}"
        )

        # --------------------------------------------------------
        # Проверяем обычные пары через Twelve Data.
        # --------------------------------------------------------

        available_normal: list[str] = []

        semaphore = asyncio.Semaphore(5)

        async def check_normal(
            pair: str,
        ):
            async with semaphore:
                candles = await self.get_candles(
                    pair,
                    log_errors=False,
                )

                if (
                    candles is not None
                    and len(candles) >= 80
                ):
                    return pair

                return None

        if normal_assets:
            results = await asyncio.gather(
                *[
                    check_normal(pair)
                    for pair in normal_assets
                ],
                return_exceptions=True,
            )

            for result in results:
                if (
                    isinstance(result, str)
                    and result not in available_normal
                ):
                    available_normal.append(
                        result
                    )

        # --------------------------------------------------------
        # OTC НЕ проверяем Twelve Data.
        #
        # Если SSID есть — scheduler сможет
        # запросить реальные OTC свечи.
        # --------------------------------------------------------

        result: list[str] = []

        for pair in available_normal:
            if pair not in result:
                result.append(pair)

        for asset in otc_assets:
            if asset not in result:
                result.append(asset)

        print("")
        print(
            "📊 Доступные активы для анализатора:"
        )

        print(
            f"   Обычные FX: "
            f"{len(available_normal)}"
        )

        print(
            f"   OTC: "
            f"{len(otc_assets)}"
        )

        print(
            f"   Всего: "
            f"{len(result)}"
        )

        if otc_assets:
            print("")
            print(
                "🟣 OTC:"
            )
            print(
                "   "
                + ", ".join(
                    otc_assets
                )
            )

        return result

    # ============================================================
    # OTC CLIENT
    # ============================================================

    async def _ensure_pocket_client(
        self,
    ) -> bool:
        """
        Создаёт Pocket Option client.

        Используется только если установлен:
            POCKET_OPTION_SSID

        Поддерживает библиотеку pocketoptionapi.
        """

        if self._pocket_ready:
            return True

        if not POCKET_OPTION_SSID:
            print(
                "⚠️ POCKET_OPTION_SSID не задан."
            )

            print(
                "⚠️ OTC-свечи Pocket Option "
                "пока недоступны."
            )

            return False

        async with self._pocket_lock:

            if self._pocket_ready:
                return True

            try:
                from pocketoptionapi import (
                    PocketOption,
                )
            except ImportError:
                print(
                    "❌ Не установлен "
                    "pocketoptionapi."
                )

                print(
                    "❌ Невозможно получить "
                    "реальные OTC-свечи."
                )

                return False

            try:
                print("")
                print(
                    "🔌 Подключение к "
                    "Pocket Option..."
                )

                client = PocketOption(
                    POCKET_OPTION_SSID
                )

                # Библиотека синхронная.
                # Подключение выносим из event loop.
                connected = await asyncio.to_thread(
                    client.connect
                )

                # Некоторые версии возвращают:
                # (True, None)
                # Некоторые — True.
                ok = False

                if isinstance(
                    connected,
                    tuple,
                ):
                    ok = bool(
                        connected[0]
                    )
                else:
                    ok = bool(
                        connected
                    )

                if not ok:
                    print(
                        "❌ Pocket Option "
                        "не подключён."
                    )

                    return False

                # Ждём WebSocket/time sync.
                for _ in range(100):

                    try:
                        check_connect = (
                            await asyncio.to_thread(
                                client.check_connect
                            )
                        )
                    except Exception:
                        check_connect = True

                    if not check_connect:
                        await asyncio.sleep(
                            0.1
                        )
                        continue

                    try:
                        synced = (
                            await asyncio.to_thread(
                                client.is_time_synced
                            )
                        )
                    except Exception:
                        synced = True

                    if synced:
                        break

                    await asyncio.sleep(
                        0.1
                    )

                self._pocket_client = client
                self._pocket_ready = True

                print(
                    "✅ Pocket Option "
                    "подключён."
                )

                return True

            except Exception as exc:
                print(
                    "❌ Ошибка подключения "
                    "Pocket Option:"
                )

                print(
                    f"   {type(exc).__name__}: "
                    f"{exc}"
                )

                self._pocket_client = None
                self._pocket_ready = False

                return False

    async def _close_pocket_client(
        self,
    ) -> None:
        """
        Безопасно закрывает Pocket Option client.
        """

        client = self._pocket_client

        self._pocket_client = None
        self._pocket_ready = False

        if client is None:
            return

        close_methods = [
            "close",
            "disconnect",
            "shutdown",
        ]

        for method_name in close_methods:
            method = getattr(
                client,
                method_name,
                None,
            )

            if method is None:
                continue

            try:
                await asyncio.to_thread(
                    method
                )
            except Exception:
                pass

            break

    # ============================================================
    # CANDLES ROUTER
    # ============================================================

    async def get_candles(
        self,
        pair: str,
        log_errors: bool = True,
    ) -> Optional[pd.DataFrame]:
        """
        Главный метод получения свечей.

        Обычный:
            EUR/USD
                -> Twelve Data

        OTC:
            EUR/USD OTC
                -> Pocket Option
        """

        if not pair:
            return None

        if self.is_otc(pair):
            return await self.get_otc_candles(
                pair,
                log_errors=log_errors,
            )

        normalized = (
            self._normalize_forex_pair(
                pair
            )
        )

        if not normalized:
            if log_errors:
                print(
                    f"❌ Invalid asset: {pair}"
                )

            return None

        return await self._get_twelve_data_candles(
            normalized,
            log_errors=log_errors,
        )

    # ============================================================
    # TWELVE DATA
    # ============================================================

    async def _get_twelve_data_candles(
        self,
        pair: str,
        log_errors: bool = True,
    ) -> Optional[pd.DataFrame]:

        if not TWELVE_DATA_API_KEY:
            if log_errors:
                print(
                    "❌ TWELVE_DATA_API_KEY "
                    "не задан."
                )

            return None

        try:
            session = await self._get_session()

            params = {
                "symbol": pair,
                "interval": CANDLE_INTERVAL,
                "outputsize": CANDLE_LIMIT,
                "apikey": TWELVE_DATA_API_KEY,
                "format": "JSON",
            }

            async with session.get(
                TWELVE_DATA_URL,
                params=params,
            ) as response:

                if response.status != 200:
                    if log_errors:
                        print(
                            f"❌ Twelve Data "
                            f"{pair}: HTTP "
                            f"{response.status}"
                        )

                    return None

                data = await response.json(
                    content_type=None
                )

        except asyncio.TimeoutError:

            if log_errors:
                print(
                    f"❌ Twelve Data "
                    f"{pair}: timeout"
                )

            return None

        except Exception as exc:

            if log_errors:
                print(
                    f"❌ Twelve Data "
                    f"{pair}: "
                    f"{type(exc).__name__}: "
                    f"{exc}"
                )

            return None

        if not isinstance(
            data,
            dict,
        ):
            if log_errors:
                print(
                    f"❌ {pair}: "
                    "неверный ответ API"
                )

            return None

        if data.get("status") == "error":

            if log_errors:
                print(
                    f"❌ Twelve Data "
                    f"{pair}: "
                    f"{data.get('message', 'API error')}"
                )

            return None

        values = data.get(
            "values"
        )

        if not values:

            if log_errors:
                print(
                    f"❌ {pair}: "
                    "API не вернул свечи"
                )

            return None

        return self._candles_to_dataframe(
            values,
            pair,
            log_errors,
        )

    # ============================================================
    # OTC CANDLES
    # ============================================================

    async def get_otc_candles(
        self,
        pair: str,
        log_errors: bool = True,
    ) -> Optional[pd.DataFrame]:
        """
        Получает реальные OTC-свечи через
        Pocket Option.

        Пример:

            EUR/USD OTC
                ->
            EURUSD_otc
        """

        symbol = self.normalize_otc_symbol(
            pair
        )

        if not symbol:
            if log_errors:
                print(
                    f"❌ Неверный OTC asset: "
                    f"{pair}"
                )

            return None

        ready = (
            await self._ensure_pocket_client()
        )

        if not ready:
            if log_errors:
                print(
                    f"⚠️ OTC {pair}: "
                    "Pocket Option client "
                    "не готов."
                )

            return None

        client = self._pocket_client

        try:
            period = (
                self._interval_to_seconds(
                    CANDLE_INTERVAL
                )
            )

            # Подписываемся на поток.
            subscribe = getattr(
                client,
                "subscribe",
                None,
            )

            if subscribe is not None:
                try:
                    await asyncio.to_thread(
                        subscribe,
                        symbol,
                        period=period,
                    )
                except TypeError:
                    await asyncio.to_thread(
                        subscribe,
                        symbol,
                        period,
                    )

            # История.
            get_history = getattr(
                client,
                "get_historical_candles",
                None,
            )

            if get_history is None:
                if log_errors:
                    print(
                        "❌ Pocket Option client "
                        "не имеет "
                        "get_historical_candles()."
                    )

                return None

            try:
                candles = await asyncio.to_thread(
                    get_history,
                    symbol,
                    period=period,
                    offset=45000,
                    count_request=1,
                )

            except TypeError:

                candles = await asyncio.to_thread(
                    get_history,
                    symbol,
                    period,
                    45000,
                    1,
                )

            if not candles:
                if log_errors:
                    print(
                        f"❌ OTC {pair}: "
                        "Pocket Option не вернул "
                        "свечи."
                    )

                return None

            df = self._pocket_candles_to_dataframe(
                candles,
                pair,
                log_errors,
            )

            if (
                df is not None
                and len(df) > CANDLE_LIMIT
            ):
                df = df.tail(
                    CANDLE_LIMIT
                ).reset_index(
                    drop=True
                )

            return df

        except Exception as exc:

            if log_errors:
                print(
                    f"❌ OTC {pair}: "
                    f"{type(exc).__name__}: "
                    f"{exc}"
                )

            return None

    # ============================================================
    # POCKET CANDLES -> DATAFRAME
    # ============================================================

    def _pocket_candles_to_dataframe(
        self,
        candles: Any,
        pair: str,
        log_errors: bool = True,
    ) -> Optional[pd.DataFrame]:
        """
        Нормализует различные форматы свечей
        pocketoptionapi.
        """

        try:
            rows: list[dict[str, Any]] = []

            if isinstance(
                candles,
                pd.DataFrame,
            ):
                df = candles.copy()

            else:
                if isinstance(
                    candles,
                    dict,
                ):
                    if "data" in candles:
                        candles = candles[
                            "data"
                        ]

                    elif "candles" in candles:
                        candles = candles[
                            "candles"
                        ]

                for candle in candles:

                    if isinstance(
                        candle,
                        dict,
                    ):
                        timestamp = (
                            candle.get(
                                "timestamp",
                                candle.get(
                                    "time",
                                    candle.get(
                                        "from"
                                    ),
                                ),
                            )
                        )

                        open_price = (
                            candle.get(
                                "open"
                            )
                        )

                        high_price = (
                            candle.get(
                                "high"
                            )
                        )

                        low_price = (
                            candle.get(
                                "low"
                            )
                        )

                        close_price = (
                            candle.get(
                                "close"
                            )
                        )

                    else:
                        timestamp = getattr(
                            candle,
                            "timestamp",
                            getattr(
                                candle,
                                "time",
                                getattr(
                                    candle,
                                    "from",
                                    None,
                                ),
                            ),
                        )

                        open_price = getattr(
                            candle,
                            "open",
                            None,
                        )

                        high_price = getattr(
                            candle,
                            "high",
                            None,
                        )

                        low_price = getattr(
                            candle,
                            "low",
                            None,
                        )

                        close_price = getattr(
                            candle,
                            "close",
                            None,
                        )

                    if (
                        timestamp is None
                        or open_price is None
                        or high_price is None
                        or low_price is None
                        or close_price is None
                    ):
                        continue

                    rows.append(
                        {
                            "datetime": timestamp,
                            "open": open_price,
                            "high": high_price,
                            "low": low_price,
                            "close": close_price,
                        }
                    )

                if not rows:
                    if log_errors:
                        print(
                            f"❌ OTC {pair}: "
                            "не удалось распознать "
                            "формат свечей."
                        )

                    return None

                df = pd.DataFrame(
                    rows
                )

            if df.empty:
                return None

            # ----------------------------------------------------
            # Возможные имена времени.
            # ----------------------------------------------------

            if "datetime" not in df.columns:

                for candidate in (
                    "timestamp",
                    "time",
                    "from",
                    "date",
                ):
                    if (
                        candidate
                        in df.columns
                    ):
                        df = df.rename(
                            columns={
                                candidate:
                                    "datetime"
                            }
                        )

                        break

            # ----------------------------------------------------
            # Иногда библиотека отдаёт Unix timestamp.
            # ----------------------------------------------------

            if "datetime" not in df.columns:
                if log_errors:
                    print(
                        f"❌ OTC {pair}: "
                        "нет времени свечи."
                    )

                return None

            numeric_columns = [
                "open",
                "high",
                "low",
                "close",
                "volume",
            ]

            for column in numeric_columns:

                if column in df.columns:
                    df[column] = pd.to_numeric(
                        df[column],
                        errors="coerce",
                    )

            timestamp_series = df[
                "datetime"
            ]

            # Если timestamp числовой —
            # определяем секунды/миллисекунды.
            if pd.api.types.is_numeric_dtype(
                timestamp_series
            ):
                maximum = (
                    pd.to_numeric(
                        timestamp_series,
                        errors="coerce",
                    ).max()
                )

                unit = (
                    "ms"
                    if maximum > 10_000_000_000
                    else "s"
                )

                df["datetime"] = (
                    pd.to_datetime(
                        timestamp_series,
                        unit=unit,
                        utc=True,
                        errors="coerce",
                    )
                )

            else:
                df["datetime"] = (
                    pd.to_datetime(
                        timestamp_series,
                        utc=True,
                        errors="coerce",
                    )
                )

            required = [
                "open",
                "high",
                "low",
                "close",
            ]

            missing = [
                column
                for column in required
                if column not in df.columns
            ]

            if missing:
                if log_errors:
                    print(
                        f"❌ OTC {pair}: "
                        "отсутствуют поля "
                        + ", ".join(
                            missing
                        )
                    )

                return None

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
            ).drop_duplicates(
                subset=["datetime"],
                keep="last",
            ).reset_index(
                drop=True
            )

            if df.empty:
                return None

            return df

        except Exception as exc:

            if log_errors:
                print(
                    f"❌ OTC {pair}: "
                    "ошибка обработки свечей: "
                    f"{type(exc).__name__}: "
                    f"{exc}"
                )

            return None

    # ============================================================
    # GENERIC CANDLES -> DATAFRAME
    # ============================================================

    def _candles_to_dataframe(
        self,
        values: Any,
        pair: str,
        log_errors: bool = True,
    ) -> Optional[pd.DataFrame]:

        try:
            df = pd.DataFrame(
                values
            )

            if "datetime" not in df.columns:

                if log_errors:
                    print(
                        f"❌ {pair}: "
                        "нет datetime"
                    )

                return None

            df["datetime"] = pd.to_datetime(
                df["datetime"],
                utc=True,
                errors="coerce",
            )

            numeric_columns = [
                "open",
                "high",
                "low",
                "close",
                "volume",
            ]

            for column in numeric_columns:

                if column in df.columns:
                    df[column] = pd.to_numeric(
                        df[column],
                        errors="coerce",
                    )

            required = [
                "open",
                "high",
                "low",
                "close",
            ]

            missing = [
                column
                for column in required
                if column not in df.columns
            ]

            if missing:

                if log_errors:
                    print(
                        f"❌ {pair}: "
                        "отсутствуют поля "
                        + ", ".join(
                            missing
                        )
                    )

                return None

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
            ).drop_duplicates(
                subset=["datetime"],
                keep="last",
            ).reset_index(
                drop=True
            )

            if df.empty:

                if log_errors:
                    print(
                        f"❌ {pair}: "
                        "после очистки DataFrame "
                        "пуст."
                    )

                return None

            return df

        except Exception as exc:

            if log_errors:
                print(
                    f"❌ {pair}: "
                    "ошибка обработки DataFrame: "
                    f"{type(exc).__name__}: "
                    f"{exc}"
                )

            return None

    # ============================================================
    # INTERVAL
    # ============================================================

    @staticmethod
    def _interval_to_seconds(
        interval: str,
    ) -> int:
        """
        Преобразует:

            1min
            5min
            15min
            1h

        в секунды.
        """

        if not interval:
            return 60

        value = str(
            interval
        ).strip().lower()

        match = re.fullmatch(
            r"(\d+)\s*(s|sec|secs|m|min|mins|h|hour|hours|d|day|days)",
            value,
        )

        if not match:
            return 60

        amount = int(
            match.group(1)
        )

        unit = match.group(2)

        if unit in {
            "s",
            "sec",
            "secs",
        }:
            return amount

        if unit in {
            "m",
            "min",
            "mins",
        }:
            return amount * 60

        if unit in {
            "h",
            "hour",
            "hours",
        }:
            return amount * 3600

        if unit in {
            "d",
            "day",
            "days",
        }:
            return amount * 86400

        return 60


# ================================================================
# GLOBAL CLIENT
# ================================================================

market_client = MarketClient()
