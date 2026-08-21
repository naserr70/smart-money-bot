"""
Independent Binance / Bybit / KuCoin market data provider.

Rules
-----
- Each exchange is completely independent.
- No history is copied between exchanges.
- Each exchange maintains its own candle history.
- Analysis priority for the active cycle:
    Binance → Bybit → KuCoin

Binance ban handling
--------------------
HTTP 429 = rate limit (back off).
HTTP 418 = IP ban after repeated 429s (2 min … 3 days).
We honor Retry-After and stop all Binance calls until it expires
so the ban is not extended by continued traffic.
"""

import logging
import time
from typing import Dict, Optional, Tuple

import requests

from assets import TARGET_SYMBOLS, resolve_alias


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

KUCOIN_TICKER_ENDPOINT = (
    "https://api.kucoin.com/api/v1/market/allTickers"
)

KUCOIN_KLINES_ENDPOINT = (
    "https://api.kucoin.com/api/v1/market/candles"
)

# Fallback when Binance omits Retry-After
DEFAULT_429_COOLDOWN_SEC = 60
DEFAULT_418_COOLDOWN_SEC = 120
MAX_COOLDOWN_SEC = 3 * 24 * 3600


class MarketDataProvider:

    def __init__(
        self,
        session: requests.Session,
        timeout: int = 10,
        binance_enabled: bool = True,
        kucoin_enabled: bool = True,
    ):
        self.session = session
        self.timeout = max(1, int(timeout))
        self.binance_enabled = binance_enabled
        self.kucoin_enabled = kucoin_enabled
        self._binance_cooldown_until: float = 0.0
        self._binance_cooldown_reason: str = ""

    def binance_cooldown_remaining(self) -> float:
        remaining = self._binance_cooldown_until - time.time()
        return max(0.0, remaining)

    def binance_is_cooling(self) -> bool:
        return self.binance_cooldown_remaining() > 0

    def _set_binance_cooldown(self, seconds: float, reason: str) -> None:
        seconds = max(1.0, min(float(seconds), MAX_COOLDOWN_SEC))
        until = time.time() + seconds
        if until <= self._binance_cooldown_until:
            return
        self._binance_cooldown_until = until
        self._binance_cooldown_reason = reason
        log.warning(
            "BINANCE COOLDOWN SET | reason=%s | seconds=%.0f | until_in=%.0fs",
            reason,
            seconds,
            seconds,
        )

    def _parse_retry_after_seconds(self, response: requests.Response) -> Optional[float]:
        header = response.headers.get("Retry-After")
        if header:
            try:
                value = float(header.strip())
                if value > 1_000_000_000_000:
                    return max(0.0, (value / 1000.0) - time.time())
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

        data = payload.get("data")
        retry_values = []
        if isinstance(data, dict):
            retry_values.append(data.get("retryAfter"))
        retry_values.append(payload.get("retryAfter"))

        for retry in retry_values:
            if retry is None:
                continue
            try:
                ts = float(retry)
                if ts > 1_000_000_000_000:
                    return max(0.0, (ts / 1000.0) - time.time())
                if ts > 1_000_000_000:
                    return max(0.0, ts - time.time())
                return max(0.0, ts)
            except (TypeError, ValueError):
                continue
        return None

    def _handle_binance_limit_response(self, response: requests.Response, context: str) -> None:
        status = response.status_code
        if status == 418:
            seconds = self._parse_retry_after_seconds(response)
            if seconds is None or seconds < 1:
                seconds = float(DEFAULT_418_COOLDOWN_SEC)
            self._set_binance_cooldown(seconds, reason=f"418_IP_BAN:{context}")
            return
        if status == 429:
            seconds = self._parse_retry_after_seconds(response)
            if seconds is None or seconds < 1:
                seconds = float(DEFAULT_429_COOLDOWN_SEC)
            self._set_binance_cooldown(seconds, reason=f"429_RATE_LIMIT:{context}")
            return
        log.warning("BINANCE HTTP ERROR | status=%s context=%s", status, context)

    def _binance_guard(self, context: str) -> bool:
        remaining = self.binance_cooldown_remaining()
        if remaining <= 0:
            if self._binance_cooldown_reason:
                log.info(
                    "BINANCE COOLDOWN ENDED | previous_reason=%s",
                    self._binance_cooldown_reason,
                )
                self._binance_cooldown_reason = ""
            return True
        log.info(
            "BINANCE SKIPPED | cooldown active | remaining=%.0fs | reason=%s | context=%s",
            remaining,
            self._binance_cooldown_reason or "unknown",
            context,
        )
        return False

    def fetch_binance(self) -> Dict[str, dict]:
        if not self.binance_enabled or not self._binance_guard("ticker"):
            return {}
        log.info("BINANCE TICKER FETCH START")
        for endpoint in BINANCE_TICKER_ENDPOINTS:
            try:
                response = self.session.get(endpoint, timeout=self.timeout)
                if response.status_code in (418, 429):
                    self._handle_binance_limit_response(response, context="ticker")
                    return {}
                if response.status_code != 200:
                    log.warning(
                        "BINANCE TICKER HTTP ERROR | status=%s endpoint=%s",
                        response.status_code,
                        endpoint,
                    )
                    continue
                try:
                    raw = response.json()
                except ValueError:
                    log.warning("BINANCE TICKER INVALID JSON | endpoint=%s", endpoint)
                    continue
                if not isinstance(raw, list):
                    continue
                result: Dict[str, dict] = {}
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    symbol = item.get("symbol")
                    if symbol not in TARGET_SYMBOLS:
                        continue
                    try:
                        normalized = resolve_alias(symbol)
                        result[normalized] = {
                            "lastPrice": float(item["lastPrice"]),
                            "quoteVolume": float(item["quoteVolume"]),
                            "priceChangePercent": float(item["priceChangePercent"]),
                        }
                    except (KeyError, TypeError, ValueError):
                        continue
                if result:
                    log.info(
                        "BINANCE TICKER OK | symbols=%s endpoint=%s",
                        len(result),
                        endpoint,
                    )
                    return result
            except requests.RequestException as exc:
                log.warning(
                    "BINANCE TICKER REQUEST ERROR | endpoint=%s error=%s",
                    endpoint,
                    exc,
                )
        log.error("BINANCE TICKER FAILED | all endpoints unavailable")
        return {}

    def fetch_bybit(self) -> Dict[str, dict]:
        log.info("BYBIT TICKER FETCH START")
        try:
            response = self.session.get(
                BYBIT_TICKER_ENDPOINT,
                params={"category": "spot"},
                timeout=self.timeout + 2,
            )
            if response.status_code != 200:
                log.warning("BYBIT TICKER HTTP ERROR | status=%s", response.status_code)
                return {}
            try:
                payload = response.json()
            except ValueError:
                return {}
            if not isinstance(payload, dict) or int(payload.get("retCode", -1)) != 0:
                return {}
            result_block = payload.get("result") or {}
            tickers = result_block.get("list") or []
            if not isinstance(tickers, list):
                return {}
            result: Dict[str, dict] = {}
            for item in tickers:
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
            log.info("BYBIT TICKER OK | symbols=%s", len(result))
            return result
        except requests.RequestException as exc:
            log.error("BYBIT TICKER FAILED | error=%s", exc)
            return {}

    def fetch_kucoin(self) -> Dict[str, dict]:
        if not self.kucoin_enabled:
            return {}
        log.info("KUCOIN TICKER FETCH START")
        try:
            response = self.session.get(KUCOIN_TICKER_ENDPOINT, timeout=self.timeout + 2)
            if response.status_code != 200:
                log.warning("KUCOIN TICKER HTTP ERROR | status=%s", response.status_code)
                return {}
            try:
                payload = response.json()
            except ValueError:
                return {}
            if not isinstance(payload, dict):
                return {}
            data = payload.get("data", {})
            if not isinstance(data, dict):
                return {}
            tickers = data.get("ticker", [])
            if not isinstance(tickers, list):
                return {}
            result: Dict[str, dict] = {}
            for item in tickers:
                if not isinstance(item, dict):
                    continue
                raw_symbol = str(item.get("symbol", "")).upper()
                if not raw_symbol.endswith("-USDT"):
                    continue
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
            log.info("KUCOIN TICKER OK | symbols=%s", len(result))
            return result
        except requests.RequestException as exc:
            log.error("KUCOIN TICKER FAILED | error=%s", exc)
            return {}

    # =========================================================
    # KLINES
    # =========================================================

    def fetch_binance_klines(self, symbol: str, limit: int = 864) -> Optional[list]:
        if not self._binance_guard(f"klines:{symbol}"):
            return None
        limit = max(1, min(int(limit), 1000))
        params = {"symbol": symbol, "interval": "5m", "limit": limit}
        for endpoint in BINANCE_KLINES_ENDPOINTS:
            try:
                response = self.session.get(endpoint, params=params, timeout=self.timeout)
                if response.status_code in (418, 429):
                    self._handle_binance_limit_response(response, context=f"klines:{symbol}")
                    return None
                if response.status_code != 200:
                    log.warning(
                        "BINANCE HISTORY HTTP ERROR | symbol=%s status=%s endpoint=%s",
                        symbol,
                        response.status_code,
                        endpoint,
                    )
                    continue
                try:
                    raw = response.json()
                except ValueError:
                    continue
                if not isinstance(raw, list):
                    return None
                log.info(
                    "BINANCE HISTORY OK | symbol=%s candles=%s/%s",
                    symbol,
                    len(raw),
                    limit,
                )
                return raw
            except requests.RequestException as exc:
                log.warning(
                    "BINANCE HISTORY ERROR | symbol=%s endpoint=%s error=%s",
                    symbol,
                    endpoint,
                    exc,
                )
        return None

    def fetch_bybit_klines(self, symbol: str, limit: int = 864) -> Optional[list]:
        limit = max(1, min(int(limit), 1000))
        params = {
            "category": "spot",
            "symbol": symbol,
            "interval": "5",
            "limit": limit,
        }
        try:
            response = self.session.get(BYBIT_KLINES_ENDPOINT, params=params, timeout=self.timeout + 2)
            if response.status_code != 200:
                log.warning("BYBIT HISTORY HTTP ERROR | symbol=%s status=%s", symbol, response.status_code)
                return None
            try:
                payload = response.json()
            except ValueError:
                return None
            if not isinstance(payload, dict) or int(payload.get("retCode", -1)) != 0:
                return None
            result_block = payload.get("result") or {}
            raw = result_block.get("list") or []
            if not isinstance(raw, list):
                return None
            raw = list(reversed(raw))[-limit:]
            log.info("BYBIT HISTORY OK | symbol=%s candles=%s/%s", symbol, len(raw), limit)
            return raw
        except requests.RequestException as exc:
            log.warning("BYBIT HISTORY ERROR | symbol=%s error=%s", symbol, exc)
            return None

    def fetch_kucoin_klines(self, symbol: str, limit: int = 864) -> Optional[list]:
        limit = max(1, int(limit))
        if "-" not in symbol and symbol.endswith("USDT"):
            symbol = symbol[:-4] + "-USDT"
        now_sec = int(time.time())
        params = {
            "symbol": symbol,
            "type": "5min",
            "startAt": now_sec - (limit + 1) * 300,
            "endAt": now_sec,
        }
        try:
            response = self.session.get(KUCOIN_KLINES_ENDPOINT, params=params, timeout=self.timeout + 2)
            if response.status_code != 200:
                log.warning("KUCOIN HISTORY HTTP ERROR | symbol=%s status=%s", symbol, response.status_code)
                return None
            try:
                payload = response.json()
            except ValueError:
                return None
            if not isinstance(payload, dict):
                return None
            raw = payload.get("data", [])
            if not isinstance(raw, list):
                return None
            raw = list(reversed(raw))[-limit:]
            log.info("KUCOIN HISTORY OK | symbol=%s candles=%s/%s", symbol, len(raw), limit)
            return raw
        except requests.RequestException as exc:
            log.warning("KUCOIN HISTORY ERROR | symbol=%s error=%s", symbol, exc)
            return None

    # =========================================================
    # NORMALIZED CANDLE OBJECTS
    # =========================================================

    def fetch_binance_candles(self, symbol: str, limit: int = 864):
        from candle_store import Candle
        raw = self.fetch_binance_klines(symbol, limit)
        if not raw:
            return []
        candles = []
        for row in raw:
            try:
                candles.append(Candle.from_binance(row))
            except (IndexError, TypeError, ValueError):
                continue
        return candles

    def fetch_bybit_candles(self, symbol: str, limit: int = 864):
        from candle_store import Candle
        raw = self.fetch_bybit_klines(symbol, limit)
        if not raw:
            return []
        candles = []
        for row in raw:
            try:
                candles.append(Candle.from_bybit(row))
            except (IndexError, TypeError, ValueError):
                continue
        return candles

    def fetch_kucoin_candles(self, symbol: str, limit: int = 864):
        from candle_store import Candle
        raw = self.fetch_kucoin_klines(symbol, limit)
        if not raw:
            return []
        candles = []
        for row in raw:
            try:
                candles.append(Candle.from_kucoin(row))
            except (IndexError, TypeError, ValueError):
                continue
        return candles

    def fetch_candles(self, source: str, symbol: str, limit: int = 864):
        """Fetch candles with a strict closed/open split.

        Bootstrap requests ask for one extra candle and remove the currently
        open candle before returning. Live requests keep the current candle so
        CandleStore can detect its close and roll it into the 864-candle window.
        """
        source = source.lower().strip()
        limit = max(1, int(limit))

        # Bootstrap/history requests are large. We need 864 CLOSED candles,
        # not 863 closed + the currently-open candle returned by exchanges.
        closed_only = limit >= 100
        request_limit = min(limit + 1, 1000) if closed_only else limit

        if source == "binance":
            candles = self.fetch_binance_candles(symbol, request_limit)
        elif source == "bybit":
            candles = self.fetch_bybit_candles(symbol, request_limit)
        elif source == "kucoin":
            candles = self.fetch_kucoin_candles(symbol, request_limit)
        else:
            raise ValueError(f"Unsupported market source: {source}")

        if not closed_only:
            return candles

        now_ms = int(time.time() * 1000)
        closed = [c for c in candles if c.close_time < now_ms]
        closed.sort(key=lambda c: c.open_time)
        return closed[-limit:]

    # =========================================================
    # ALL SOURCES
    # =========================================================

    def fetch_all_sources(self) -> Tuple[Dict[str, dict], Dict[str, dict], Dict[str, dict]]:
        binance = self.fetch_binance()
        bybit = self.fetch_bybit()
        kucoin = self.fetch_kucoin()
        log.info(
            "MARKET SOURCES | binance=%s | bybit=%s | kucoin=%s | binance_cooldown=%.0fs",
            len(binance),
            len(bybit),
            len(kucoin),
            self.binance_cooldown_remaining(),
        )
        return binance, bybit, kucoin
