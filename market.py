from __future__ import annotations
import asyncio
import re
import time
from typing import Optional
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
TWELVE_DATA_URL = (
    "https://api.twelvedata.com/time_series"
)
class MarketClient:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self._asset_cache: list[str] = []
        self._asset_cache_time: float = 0
        self._asset_lock = asyncio.Lock()
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
        if (
            self.session is not None
            and not self.session.closed
        ):
            await self.session.close()
        self.session = None
    # ============================================================
    # NORMALIZE PAIR
    # ============================================================
    @staticmethod
    def _normalize_pair(
        value: str,
    ) -> Optional[str]:
        """
        Приводит различные варианты обозначения
        валютной пары к EUR/USD.
        Поддерживает:
            EUR/USD
            EURUSD
            EUR-USD
            EUR_USD
            EUR\\USD
        """
        if not value:
            return None
        value = value.upper().strip()
        # Убираем OTC.
        # OTC отдельно не используем с Twelve Data.
        value = re.sub(
            r"\s+OTC$",
            "",
            value,
        )
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
        # EURUSD -> EUR/USD
        compact = value.replace(
            "/",
            "",
        )
        if len(compact) == 6:
            if (
                compact[:3].isalpha()
                and compact[3:].isalpha()
            ):
                return (
                    f"{compact[:3]}/"
                    f"{compact[3:]}"
                )
        # Уже EUR/USD
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
    # ============================================================
    # CHECK FOREX PAIR
    # ============================================================
    @staticmethod
    def _is_forex_pair(
        pair: str,
    ) -> bool:
        normalized = (
            MarketClient._normalize_pair(pair)
        )
        if not normalized:
            return False
        base, quote = normalized.split("/")
        # Простая защита от акций/индексов/крипты.
        return (
            len(base) == 3
            and len(quote) == 3
            and base.isalpha()
            and quote.isalpha()
        )
    # ============================================================
    # POCKET OPTION CURRENT ASSETS
    # ============================================================
    async def discover_pocket_pairs(
        self,
    ) -> list[str]:
        """
        Получает текущие доступные активы
        с публичной страницы Pocket Option.
        Мы намеренно берём только обычные FX-пары.
        OTC НЕ добавляем сюда, потому что Twelve Data
        не является источником котировок Pocket Option OTC.
        """
        if not ASSET_DISCOVERY_ENABLED:
            print(
                "ℹ️ Asset discovery отключён."
            )
            return FALLBACK_PAIRS.copy()
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
                        "(compatible; "
                        "PocketSignalBot/1.0)"
                    ),
                    "Accept": (
                        "text/html,"
                        "application/xhtml+xml"
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
                    html = await response.text()
            except Exception as exc:
                print(
                    "⚠️ Не удалось получить список "
                    "Pocket Option:"
                )
                print(
                    f"   {type(exc).__name__}: {exc}"
                )
                return self._fallback_assets()
            pairs = self._parse_pocket_assets(
                html
            )
            if not pairs:
                print(
                    "⚠️ Не удалось извлечь пары "
                    "из Pocket Option."
                )
                return self._fallback_assets()
            self._asset_cache = pairs
            self._asset_cache_time = (
                time.monotonic()
            )
            print(
                f"✅ Найдено доступных FX-пар: "
                f"{len(pairs)}"
            )
            print(
                "📋 "
                + ", ".join(pairs)
            )
            return pairs.copy()
    # ============================================================
    # PARSE POCKET OPTION
    # ============================================================
    def _parse_pocket_assets(
        self,
        html: str,
    ) -> list[str]:
        found: list[str] = []
        # Ищем стандартные пары вида EUR/USD.
        patterns = [
            r"\b([A-Z]{3}/[A-Z]{3})\b",
            r"\b([A-Z]{3}[A-Z]{3})\b",
        ]
        for pattern in patterns:
            matches = re.findall(
                pattern,
                html.upper(),
            )
            for match in matches:
                pair = self._normalize_pair(
                    match
                )
                if not pair:
                    continue
                if not self._is_forex_pair(
                    pair
                ):
                    continue
                # Не допускаем одинаковую валюту.
                base, quote = pair.split("/")
                if base == quote:
                    continue
                if pair not in found:
                    found.append(pair)
        # --------------------------------------------------------
        # OTC не считаем обычным рынком.
        # Если рядом с парой есть OTC, сама обычная пара
        # всё равно может присутствовать в HTML.
        #
        # Поэтому здесь только нормализация.
        # --------------------------------------------------------
        return found
    # ============================================================
    # FALLBACK
    # ============================================================
    def _fallback_assets(self) -> list[str]:
        print(
            "🔄 Используем fallback список FX-пар."
        )
        pairs = []
        for pair in FALLBACK_PAIRS:
            normalized = self._normalize_pair(
                pair
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
    # AVAILABLE PAIRS
    # ============================================================
    async def get_available_pairs(
        self,
    ) -> list[str]:
        """
        Возвращает пары, которые сейчас заявлены
        как доступные Pocket Option.
        Дополнительно проверяет, что Twelve Data
        способен получить по ним свечи.
        """
        pocket_pairs = (
            await self.discover_pocket_pairs()
        )
        if not pocket_pairs:
            return self._fallback_assets()
        print("")
        print(
            f"🔎 Проверяем {len(pocket_pairs)} "
            "потенциальных FX-пар..."
        )
        # Проверяем пары небольшими группами,
        # чтобы не создавать огромную нагрузку.
        available: list[str] = []
        semaphore = asyncio.Semaphore(5)
        async def check(pair: str):
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
        results = await asyncio.gather(
            *[
                check(pair)
                for pair in pocket_pairs
            ],
            return_exceptions=True,
        )
        for result in results:
            if (
                isinstance(result, str)
                and result not in available
            ):
                available.append(result)
        # --------------------------------------------------------
        # Если Twelve Data не смог получить ничего,
        # возвращаем Pocket список, чтобы диагностика
        # показала проблему с источником данных.
        # --------------------------------------------------------
        if not available:
            print(
                "⚠️ Не удалось получить свечи "
                "ни по одной обнаруженной паре."
            )
            print(
                "⚠️ Возвращаем список Pocket Option "
                "для дальнейшей диагностики."
            )
            return pocket_pairs
        print("")
        print(
            f"✅ Реально анализируемых пар: "
            f"{len(available)}"
        )
        print(
            "📋 "
            + ", ".join(available)
        )
        return available
    # ============================================================
    # TWELVE DATA CANDLES
    # ============================================================
    async def get_candles(
        self,
        pair: str,
        log_errors: bool = True,
    ) -> Optional[pd.DataFrame]:
        pair = self._normalize_pair(
            pair
        )
        if not pair:
            if log_errors:
                print(
                    "❌ Invalid pair"
                )
            return None
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
        # --------------------------------------------------------
        # API ERROR
        # --------------------------------------------------------
        if not isinstance(data, dict):
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
        # --------------------------------------------------------
        # DATAFRAME
        # --------------------------------------------------------
        try:
            df = pd.DataFrame(values)
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
                        + ", ".join(missing)
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
            ).reset_index(
                drop=True
            )
            if df.empty:
                if log_errors:
                    print(
                        f"❌ {pair}: "
                        "после очистки DataFrame пуст"
                    )
                return None
            return df
        except Exception as exc:
            if log_errors:
                print(
                    f"❌ {pair}: "
                    f"ошибка обработки свечей: "
                    f"{type(exc).__name__}: "
                    f"{exc}"
                )
            return None
# ================================================================
# GLOBAL CLIENT
# ================================================================
market_client = MarketClient()
