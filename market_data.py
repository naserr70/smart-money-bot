"""Exchange market-data adapters with rate-limit protection.

All normalized candle methods return CLOSED 5-minute candles only.  The
adapter deliberately fetches one extra candle because exchange APIs commonly
include the currently-open candle in their latest-N response.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Iterable, Optional, Tuple

import requests

from assets import TARGET_SYMBOLS, resolve_alias
from candle_store import Candle

log = logging.getLogger("smart_money_bot.market_data")

BINANCE_TICKER_ENDPOINTS = (
    "https://api1.binance.com/api/v3/ticker/24hr",
    "https://api2.binance.com/api/v3/ticker/24hr",
    "https://api3.binance.com/api/v3/ticker/24hr",
    "https://api.binance.com/api/v3/ticker/24hr",
)
BINANCE_KLINES_ENDPOINTS = (
    "https://api1.binance.com/api/v3/klines",
    "https://api2.binance.com/api/v3/klines",
    "https://api3.binance.com/api/v3/klines",
    "https://api.binance.com/api/v3/klines",
)
BYBIT_TICKER_ENDPOINT = "https://api.bybit.com/v5/market/tickers"
BYBIT_KLINES_ENDPOINT = "https://api.bybit.com/v5/market/kline"
KUCOIN_TICKER_ENDPOINT = "https://api.kucoin.com/api/v1/market/allTickers"
KUCOIN_KLINES_ENDPOINT = "https://api.kucoin.com/api/v1/market/candles"

DEFAULT_429_COOLDOWN_SEC = 60
DEFAULT_418_COOLDOWN_SEC = 120
MAX_COOLDOWN_SEC = 3 * 24 * 3600
CANDLE_INTERVAL_MS = 5 * 60 * 1000


class MarketDataProvider:
    def __init__(self, session: requests.Session, timeout: int = 10):
        self.session = session
        self.timeout = max(1, int(timeout))
        self._binance_cooldown_until = 0.0
        self._binance_cooldown_reason = ""

    def binance_cooldown_remaining(self) -> float:
        return max(0.0, self._binance_cooldown_until - time.time())

    def binance_is_cooling(self) -> bool:
        return self.binance_cooldown_remaining() > 0

    def _set_binance_cooldown(self, seconds: float, reason: str) -> None:
        seconds = max(1.0, min(float(seconds), MAX_COOLDOWN_SEC))
        until = time.time() + seconds
        if until <= self._binance_cooldown_until:
            return
        self._binance_cooldown_until = until
        self._binance_cooldown_reason = reason
        log.warning("BINANCE COOLDOWN SET | reason=%s seconds=%.0f", reason, seconds)

    @staticmethod
    def _retry_after(response: requests.Response) -> Optional[float]:
        header = response.headers.get("Retry-After")
        if header:
            try:
                value = float(header)
                if value > 1_000_000_000_000:
                    return max(0.0, value / 1000.0 - time.time())
                if value > 1_000_000_000:
                    return max(0.0, value - time.time())
                return max(0.0, value)
            except (TypeError, ValueError):
                pass
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        value = payload.get("retryAfter")
        if isinstance(payload.get("data"), dict):
            value = payload["data"].get("retryAfter", value)
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        if value > 1_000_000_000_000:
            return max(0.0, value / 1000.0 - time.time())
        if value > 1_000_000_000:
            return max(0.0, value - time.time())
        return max(0.0, value)

    def _handle_binance_limit(self, response: requests.Response, context: str) -> None:
        if response.status_code == 418:
            self._set_binance_cooldown(self._retry_after(response) or DEFAULT_418_COOLDOWN_SEC, f"418:{context}")
        elif response.status_code == 429:
            self._set_binance_cooldown(self._retry_after(response) or DEFAULT_429_COOLDOWN_SEC, f"429:{context}")

    def _binance_guard(self, context: str) -> bool:
        remaining = self.binance_cooldown_remaining()
        if remaining <= 0:
            return True
        log.info("BINANCE SKIPPED | remaining=%.0fs reason=%s context=%s", remaining, self._binance_cooldown_reason, context)
        return False

    def fetch_binance(self) -> Dict[str, dict]:
        if not self._binance_guard("ticker"):
            return {}
        for endpoint in BINANCE_TICKER_ENDPOINTS:
            try:
                response = self.session.get(endpoint, timeout=self.timeout)
                if response.status_code in (418, 429):
                    self._handle_binance_limit(response, "ticker")
                    return {}
                if response.status_code != 200:
                    continue
                raw = response.json()
                if not isinstance(raw, list):
                    continue
                result = {}
                for item in raw:
                    if not isinstance(item, dict) or item.get("symbol") not in TARGET_SYMBOLS:
                        continue
                    try:
                        canonical = resolve_alias(item["symbol"])
                        result[canonical] = {
                            "lastPrice": float(item["lastPrice"]),
                            "quoteVolume": float(item["quoteVolume"]),
                            "priceChangePercent": float(item["priceChangePercent"]),
                        }
                    except (KeyError, TypeError, ValueError):
                        continue
                if result:
                    log.info("BINANCE TICKER OK | symbols=%s", len(result))
                    return result
            except (requests.RequestException, ValueError) as exc:
                log.warning("BINANCE TICKER ERROR | endpoint=%s error=%s", endpoint, exc)
        return {}

    def fetch_bybit(self) -> Dict[str, dict]:
        try:
            response = self.session.get(BYBIT_TICKER_ENDPOINT, params={"category": "spot"}, timeout=self.timeout + 2)
            if response.status_code != 200:
                return {}
            payload = response.json()
            if not isinstance(payload, dict) or int(payload.get("retCode", -1)) != 0:
                return {}
            items = ((payload.get("result") or {}).get("list") or [])
            result = {}
            for item in items:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).upper()
                if symbol not in TARGET_SYMBOLS:
                    continue
                try:
                    canonical = resolve_alias(symbol)
                    result[canonical] = {
                        "lastPrice": float(item["lastPrice"]),
                        "quoteVolume": float(item.get("turnover24h") or item.get("turnover24H") or 0.0),
                        "priceChangePercent": float(item.get("price24hPcnt", 0.0)) * 100.0,
                    }
                except (KeyError, TypeError, ValueError):
                    continue
            return result
        except (requests.RequestException, ValueError) as exc:
            log.warning("BYBIT TICKER ERROR | error=%s", exc)
            return {}

    def fetch_kucoin(self) -> Dict[str, dict]:
        try:
            response = self.session.get(KUCOIN_TICKER_ENDPOINT, timeout=self.timeout + 2)
            if response.status_code != 200:
                return {}
            payload = response.json()
            items = (payload.get("data") or {}).get("ticker", []) if isinstance(payload, dict) else []
            result = {}
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw_symbol = str(item.get("symbol", "")).upper()
                normalized = raw_symbol.replace("-", "")
                if normalized not in TARGET_SYMBOLS:
                    continue
                try:
                    canonical = resolve_alias(normalized)
                    result[canonical] = {
                        "lastPrice": float(item["last"]),
                        "quoteVolume": float(item["volValue"]),
                        "priceChangePercent": float(item["changeRate"]) * 100.0,
                    }
                except (KeyError, TypeError, ValueError):
                    continue
            return result
        except (requests.RequestException, ValueError) as exc:
            log.warning("KUCOIN TICKER ERROR | error=%s", exc)
            return {}

    @staticmethod
    def _closed_only(candles: Iterable[Candle], limit: int) -> list[Candle]:
        now_ms = int(time.time() * 1000)
        unique = {c.open_time: c for c in candles if c.close_time < now_ms}
        ordered = sorted(unique.values(), key=lambda c: c.open_time)
        return ordered[-max(1, int(limit)):]

    def fetch_binance_candles(self, symbol: str, limit: int = 864) -> list[Candle]:
        if not self._binance_guard(f"klines:{symbol}"):
            return []
        requested = min(1000, max(2, int(limit) + 1))
        params = {"symbol": symbol, "interval": "5m", "limit": requested}
        for endpoint in BINANCE_KLINES_ENDPOINTS:
            try:
                response = self.session.get(endpoint, params=params, timeout=self.timeout)
                if response.status_code in (418, 429):
                    self._handle_binance_limit(response, f"klines:{symbol}")
                    return []
                if response.status_code != 200:
                    continue
                raw = response.json()
                if not isinstance(raw, list):
                    continue
                parsed = []
                for row in raw:
                    try:
                        parsed.append(Candle.from_binance(row))
                    except (IndexError, TypeError, ValueError):
                        continue
                return self._closed_only(parsed, limit)
            except (requests.RequestException, ValueError) as exc:
                log.warning("BINANCE HISTORY ERROR | symbol=%s endpoint=%s error=%s", symbol, endpoint, exc)
        return []

    def fetch_bybit_candles(self, symbol: str, limit: int = 864) -> list[Candle]:
        requested = min(1000, max(2, int(limit) + 1))
        try:
            response = self.session.get(
                BYBIT_KLINES_ENDPOINT,
                params={"category": "spot", "symbol": symbol, "interval": "5", "limit": requested},
                timeout=self.timeout + 2,
            )
            if response.status_code != 200:
                return []
            payload = response.json()
            if not isinstance(payload, dict) or int(payload.get("retCode", -1)) != 0:
                return []
            raw = list(reversed(((payload.get("result") or {}).get("list") or [])))
            parsed = []
            for row in raw:
                try:
                    parsed.append(Candle.from_bybit(row))
                except (IndexError, TypeError, ValueError):
                    continue
            return self._closed_only(parsed, limit)
        except (requests.RequestException, ValueError) as exc:
            log.warning("BYBIT HISTORY ERROR | symbol=%s error=%s", symbol, exc)
            return []

    def fetch_kucoin_candles(self, symbol: str, limit: int = 864) -> list[Candle]:
        market = symbol if "-" in symbol else f"{symbol[:-4]}-USDT" if symbol.endswith("USDT") else symbol
        try:
            response = self.session.get(KUCOIN_KLINES_ENDPOINT, params={"symbol": market, "type": "5min"}, timeout=self.timeout + 2)
            if response.status_code != 200:
                return []
            payload = response.json()
            raw = list(reversed(payload.get("data", []))) if isinstance(payload, dict) else []
            parsed = []
            for row in raw:
                try:
                    parsed.append(Candle.from_kucoin(row))
                except (IndexError, TypeError, ValueError):
                    continue
            return self._closed_only(parsed, limit)
        except (requests.RequestException, ValueError) as exc:
            log.warning("KUCOIN HISTORY ERROR | symbol=%s error=%s", symbol, exc)
            return []

    def fetch_candles(self, source: str, symbol: str, limit: int = 864) -> list[Candle]:
        source = source.lower().strip()
        if source == "binance":
            return self.fetch_binance_candles(symbol, limit)
        if source == "bybit":
            return self.fetch_bybit_candles(symbol, limit)
        if source == "kucoin":
            return self.fetch_kucoin_candles(symbol, limit)
        raise ValueError(f"Unsupported market source: {source}")

    def fetch_all_sources(self, enabled_sources: Optional[Iterable[str]] = None) -> Tuple[Dict[str, dict], Dict[str, dict], Dict[str, dict]]:
        enabled = set(s.lower() for s in enabled_sources) if enabled_sources is not None else {"binance", "bybit", "kucoin"}
        binance = self.fetch_binance() if "binance" in enabled else {}
        bybit = self.fetch_bybit() if "bybit" in enabled else {}
        kucoin = self.fetch_kucoin() if "kucoin" in enabled else {}
        log.info("MARKET SOURCES | binance=%s bybit=%s kucoin=%s", len(binance), len(bybit), len(kucoin))
        return binance, bybit, kucoin
