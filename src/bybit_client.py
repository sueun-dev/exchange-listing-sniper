"""Bybit instrument availability lookup."""

from __future__ import annotations

import logging
import time
import urllib.parse

import httpx

logger = logging.getLogger(__name__)

BYBIT_INSTRUMENTS_ENDPOINT = "https://api.bybit.com/v5/market/instruments-info"
BYBIT_SERVER_TIME_ENDPOINT = "https://api.bybit.com/v5/market/time"
REQUEST_TIMEOUT = 10
DEFAULT_CACHE_TTL = 300


class BybitClient:
    """Check whether a ticker is available on Bybit spot or perp."""

    def __init__(self, timeout: int = REQUEST_TIMEOUT, cache_ttl: int = DEFAULT_CACHE_TTL):
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._symbol_sets: dict[str, set[str]] = {}
        self._instrument_cache: dict[str, dict[str, dict]] = {}
        self._cache_loaded_at = 0.0
        self._http = httpx.Client(
            timeout=self.timeout,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )

    def lookup_ticker(self, ticker: str, refresh: bool = True) -> dict:
        if refresh:
            self.refresh_market_cache()
        return self.lookup_ticker_cached(ticker)

    def lookup_ticker_cached(self, ticker: str) -> dict:
        symbol = f"{ticker.upper()}USDT"
        spot = symbol in self._symbol_sets.get("spot", set())
        perp = symbol in self._symbol_sets.get("linear", set())
        return {
            "symbol": symbol,
            "spot": spot,
            "perp": perp,
            "any": spot or perp,
            "cache_ready": self.is_cache_ready(),
            "cache_age_ms": self.cache_age_ms(),
        }

    def server_time_ms(self) -> float | None:
        """Bybit server time in ms, or None on failure (for runtime clock checks)."""
        try:
            response = self._http.get(BYBIT_SERVER_TIME_ENDPOINT)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # pragma: no cover - network safeguard
            logger.debug("Bybit 서버 시각 조회 실패: %s", exc)
            return None
        result = payload.get("result") if isinstance(payload, dict) else None
        if isinstance(result, dict):
            nano = result.get("timeNano")
            if nano is not None:
                try:
                    return int(str(nano)) / 1_000_000.0
                except (TypeError, ValueError):
                    pass
            second = result.get("timeSecond")
            if second is not None:
                try:
                    return int(str(second)) * 1000.0
                except (TypeError, ValueError):
                    pass
        top = payload.get("time") if isinstance(payload, dict) else None
        try:
            return float(top) if top is not None else None
        except (TypeError, ValueError):
            return None

    def has_symbol(self, category: str, symbol: str) -> bool:
        self.refresh_market_cache()
        return symbol in self._symbol_sets.get(category, set())

    def has_symbol_cached(self, category: str, symbol: str) -> bool:
        return symbol in self._symbol_sets.get(category, set())

    def is_cache_ready(self) -> bool:
        return bool(self._symbol_sets)

    def cache_age_ms(self) -> float:
        if not self._cache_loaded_at:
            return -1.0
        return max(0.0, (time.time() - self._cache_loaded_at) * 1000.0)

    def refresh_market_cache(self, force: bool = False):
        now = time.time()
        if (
            not force
            and self._symbol_sets
            and now - self._cache_loaded_at < self.cache_ttl
        ):
            return

        symbol_sets: dict[str, set[str]] = {}
        instrument_cache: dict[str, dict[str, dict]] = {}
        for category in ("spot", "linear"):
            instruments = self._fetch_all_instruments(category)
            symbol_sets[category] = {
                item.get("symbol", "")
                for item in instruments
                if item.get("symbol")
            }
            instrument_cache[category] = {
                item["symbol"]: item
                for item in instruments
                if item.get("symbol")
            }

        if symbol_sets:
            self._symbol_sets = symbol_sets
            self._instrument_cache = instrument_cache
            self._cache_loaded_at = now
            logger.info(
                "Bybit 심볼 캐시 갱신 완료: spot=%d, linear=%d",
                len(symbol_sets.get("spot", set())),
                len(symbol_sets.get("linear", set())),
            )

    def _fetch_json(self, url: str) -> dict | None:
        try:
            response = self._http.get(url)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.warning("Bybit 조회 실패 [%s]: %s", url, exc)
            return None

    def _fetch_all_instruments(self, category: str) -> list[dict]:
        items: list[dict] = []
        cursor = ""

        while True:
            params = {
                "category": category,
                "limit": 1000,
            }
            if cursor:
                params["cursor"] = cursor

            url = f"{BYBIT_INSTRUMENTS_ENDPOINT}?{urllib.parse.urlencode(params)}"
            body = self._fetch_json(url)
            if body is None or body.get("retCode") != 0:
                break

            result = body.get("result", {})
            items.extend(result.get("list", []))
            cursor = result.get("nextPageCursor", "")
            if not cursor:
                break

        return items

    def close(self):
        self._http.close()
