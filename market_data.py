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
DEFAULT_418_COOLDOWN_SEC = 120  # official minimum ban is 2 minutes
MAX_COOLDOWN_SEC = 3 * 24 * 3600  # 3 days (official max ban)


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

        # Unix timestamp until which Binance must not be called.
        self._binance_cooldown_until: float = 0.0
        self._binance_cooldown_reason: str = ""

    # =========================================================
    # BINANCE BAN / RATE-LIMIT COOLDOWN
    # =========================================================

    def binance_cooldown_remaining(self) -> float:
        """Seconds left on Binance cooldown (0 if clear)."""
        remaining = self._binance_cooldown_until - time.time()
        return max(0.0, remaining)

    def binance_is_cooling(self) -> bool:
        return self.binance_cooldown_remaining() > 0

    def _set_binance_cooldown(
        self,
        seconds: float,
        reason: str,
    ) -> None:
        seconds = max(1.0, min(float(seconds), MAX_COOLDOWN_SEC))
        until = time.time() + seconds

        # Never shorten an existing longer cooldown.
        if until <= self._binance_cooldown_until:
            return

        self._binance_cooldown_until = until
        self._binance_cooldown_reason = reason

        log.warning(
            "BINANCE COOLDOWN SET | reason=%s | "
            "seconds=%.0f | until_in=%.0fs",
            reason,
            seconds,
            seconds,
        )

    def _parse_retry_after_seconds(
        self,
        response: requests.Response,
    ) -> Optional[float]:
        """
        Binance may send:
          - Retry-After header: seconds to wait
          - body.data.retryAfter: unix ms timestamp when ban lifts
          - body.msg containing "until <ms>"
        """

        # 1) Header (preferred)
        header = response.headers.get("Retry-After")
        if header:
            try:
                value = float(header.strip())
                # Header is usually seconds; if huge, treat as epoch ms.
                if value > 1_000_000_000_000:  # ms timestamp
                    return max(0.0, (value / 1000.0) - time.time())
                if value > 1_000_000_000:  # sec timestamp
                    return max(0.0, value - time.time())
                return max(0.0, value)
            except (TypeError, ValueError):
                pass

        # 2) JSON body
        try:
            payload = response.json()
        except ValueError:
            return None

        if not isinstance(payload, dict):
            return None

        # Nested data.retryAfter (ms)
        data = payload.get("data")
        if isinstance(data, dict):
            retry_ms = data.get("retryAfter")
            if retry_ms is not None:
                try:
                    ts = float(retry_ms)
                    if ts > 1_000_000_000_000:
                        return max(0.0, (ts / 1000.0) - time.time())
                    if ts > 1_000_000_000:
                        return max(0.0, ts - time.time())
                    return max(0.0, ts)
                except (TypeError, ValueError):
                    pass

        # Top-level retryAfter
        retry = payload.get("retryAfter")
        if retry is not None:
            try:
                ts = float(retry)
                if ts > 1_000_000_000_000:
                    return max(0.0, (ts / 1000.0) - time.time())
                if ts > 1_000_000_000:
                    return max(0.0, ts - time.time())
                return max(0.0, ts)
            except (TypeError, ValueError):
                pass

        return None

    def _handle_binance_limit_response(
        self,
        response: requests.Response,
        context: str,
    ) -> None:
        """
        Apply cooldown for 418 (ban) / 429 (rate limit).
        Other non-200 statuses do not set a long cooldown.
        """

        status = response.status_code

        if status == 418:
            seconds = self._parse_retry_after_seconds(response)
            if seconds is None or seconds < 1:
                seconds = float(DEFAULT_418_COOLDOWN_SEC)
            self._set_binance_cooldown(
                seconds,
                reason=f"418_IP_BAN:{context}",
            )
            return

        if status == 429:
            seconds = self._parse_retry_after_seconds(response)
            if seconds is None or seconds < 1:
                seconds = float(DEFAULT_429_COOLDOWN_SEC)
            self._set_binance_cooldown(
                seconds,
                reason=f"429_RATE_LIMIT:{context}",
            )
            return

        # Soft note for other errors — no long ban
        log.warning(
            "BINANCE HTTP ERROR | status=%s context=%s",
            status,
            context,
        )

    def _binance_guard(self, context: str) -> bool:
        """
        Return True if Binance may be called.
        Return False if still in cooldown (and log once-style message).
        """

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
            "BINANCE SKIPPED | cooldown active | "
            "remaining=%.0fs | reason=%s | context=%s",
            remaining,
            self._binance_cooldown_reason or "unknown",
            context,
        )
        return False

    # =========================================================
    # TICKERS
    # =========================================================

    def fetch_binance(self) -> Dict[str, dict]:

        if not self.binance_enabled:
            return {}

        if not self._binance_guard("ticker"):
            return {}

        log.info("BINANCE TICKER FETCH START")

        for endpoint in BINANCE_TICKER_ENDPOINTS:

            try:
                response = self.session.get(
                    endpoint,
                    timeout=self.timeout,
                )

                if response.status_code in (418, 429):
                    self._handle_binance_limit_response(
                        response,
                        context="ticker",
                    )
                    return {}

                if response.status_code != 200:
                    log.warning(
                        "BINANCE TICKER HTTP ERROR | "
                        "status=%s endpoint=%s",
                        response.status_code,
                        endpoint,
                    )
                    continue

                try:
                    raw = response.json()
                except ValueError:
                    log.warning(
                        "BINANCE TICKER INVALID JSON | "
                        "endpoint=%s",
                        endpoint,
                    )
                    continue

                if not isinstance(raw, list):
                    log.warning(
                        "BINANCE TICKER INVALID PAYLOAD | "
                        "endpoint=%s",
                        endpoint,
                    )
                    continue

                result: Dict[str, dict] = {}

                for item in raw:

                    if not isinstance(item, dict):
                        continue

                    symbol = item.get("symbol")

                    if symbol not in TARGET_SYMBOLS:
                        continue

                    try:
                        normalized = resolve_alias(
                            symbol
                        )

                        result[normalized] = {
                            "lastPrice": float(
                                item["lastPrice"]
                            ),
                            "quoteVolume": float(
                                item["quoteVolume"]
                            ),
                            "priceChangePercent": float(
                                item["priceChangePercent"]
                            ),
                        }

                    except (
                        KeyError,
                        TypeError,
                        ValueError,
                    ):
                        continue

                if result:
                    log.info(
                        "BINANCE TICKER OK | "
                        "symbols=%s endpoint=%s",
                        len(result),
                        endpoint,
                    )
                    return result

            except requests.RequestException as exc:
                log.warning(
                    "BINANCE TICKER REQUEST ERROR | "
                    "endpoint=%s error=%s",
                    endpoint,
                    exc,
                )

        log.error(
            "BINANCE TICKER FAILED | "
            "all endpoints unavailable"
        )

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
                log.warning(
                    "BYBIT TICKER HTTP ERROR | status=%s",
                    response.status_code,
                )
                return {}

            try:
                payload = response.json()
            except ValueError:
                log.warning("BYBIT TICKER INVALID JSON")
                return {}

            if not isinstance(payload, dict):
                return {}

            if int(payload.get("retCode", -1)) != 0:
                log.warning(
                    "BYBIT TICKER API ERROR | retCode=%s retMsg=%s",
                    payload.get("retCode"),
                    payload.get("retMsg"),
                )
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

                    pct = float(item.get("price24hPcnt", 0.0)) * 100.0

                    result[canonical] = {
                        "lastPrice": float(item["lastPrice"]),
                        "quoteVolume": float(
                            item.get("turnover24h")
                            or item.get("turnover24H")
                            or 0.0
                        ),
                        "priceChangePercent": pct,
                    }

                except (
                    KeyError,
                    TypeError,
                    ValueError,
                ):
                    continue

            log.info(
                "BYBIT TICKER OK | symbols=%s",
                len(result),
            )

            return result

        except requests.RequestException as exc:
            log.error(
                "BYBIT TICKER FAILED | error=%s",
                exc,
            )
            return {}

    def fetch_kucoin(self) -> Dict[str, dict]:

        if not self.kucoin_enabled:
            return {}

        log.info("KUCOIN TICKER FETCH START")

        try:
            response = self.session.get(
                KUCOIN_TICKER_ENDPOINT,
                timeout=self.timeout + 2,
            )

            if response.status_code != 200:
                log.warning(
                    "KUCOIN TICKER HTTP ERROR | status=%s",
                    response.status_code,
                )
                return {}

            try:
                payload = response.json()
            except ValueError:
                log.warning(
                    "KUCOIN TICKER INVALID JSON"
                )
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

                raw_symbol = str(
                    item.get("symbol", "")
                ).upper()

                if not raw_symbol.endswith("-USDT"):
                    continue

                normalized = raw_symbol.replace(
                    "-",
                    "",
                )

                if normalized not in TARGET_SYMBOLS:
                    continue

                try:
                    canonical = resolve_alias(
                        normalized
                    )

                    result[canonical] = {
                        "lastPrice": float(
                            item["last"]
                        ),
                        "quoteVolume": float(
                            item["volValue"]
                        ),
                        "priceChangePercent": (
                            float(
                                item["changeRate"]
                            ) * 100.0
                        ),
                    }

                except (
                    KeyError,
                    TypeError,
                    ValueError,
                ):
                    continue

            log.info(
                "KUCOIN TICKER OK | symbols=%s",
                len(result),
            )

            return result

        except requests.RequestException as exc:
            log.error(
                "KUCOIN TICKER FAILED | error=%s",
                exc,
            )
            return {}

    # =========================================================
    # KLINES
    # =========================================================

    def fetch_binance_klines(
        self,
        symbol: str,
        limit: int = 864,
    ) -> Optional[list]:

        if not self._binance_guard(f"klines:{symbol}"):
            return None

        limit = max(1, min(int(limit), 1000))

        params = {
            "symbol": symbol,
            "interval": "5m",
            "limit": limit,
        }

        for endpoint in BINANCE_KLINES_ENDPOINTS:

            try:
                response = self.session.get(
                    endpoint,
                    params=params,
                    timeout=self.timeout,
                )

                if response.status_code in (418, 429):
                    self._handle_binance_limit_response(
                        response,
                        context=f"klines:{symbol}",
                    )
                    return None

                if response.status_code != 200:
                    log.warning(
                        "BINANCE HISTORY HTTP ERROR | "
                        "symbol=%s status=%s endpoint=%s",
                        symbol,
                        response.status_code,
                        endpoint,
                    )
                    continue

                try:
                    raw = response.json()
                except ValueError:
                    log.warning(
                        "BINANCE HISTORY INVALID JSON | "
                        "symbol=%s",
                        symbol,
                    )
                    continue

                if not isinstance(raw, list):
                    return None

                log.info(
                    "BINANCE HISTORY OK | "
                    "symbol=%s candles=%s/%s",
                    symbol,
                    len(raw),
                    limit,
                )

                return raw

            except requests.RequestException as exc:
                log.warning(
                    "BINANCE HISTORY ERROR | "
                    "symbol=%s endpoint=%s error=%s",
                    symbol,
                    endpoint,
                    exc,
                )

        return None

    def fetch_bybit_klines(
        self,
        symbol: str,
        limit: int = 864,
    ) -> Optional[list]:

        limit = max(1, min(int(limit), 1000))

        params = {
            "category": "spot",
            "symbol": symbol,
            "interval": "5",
            "limit": limit,
        }

        try:
            response = self.session.get(
                BYBIT_KLINES_ENDPOINT,
                params=params,
                timeout=self.timeout + 2,
            )

            if response.status_code != 200:
                log.warning(
                    "BYBIT HISTORY HTTP ERROR | "
                    "symbol=%s status=%s",
                    symbol,
                    response.status_code,
                )
                return None

            try:
                payload = response.json()
            except ValueError:
                log.warning(
                    "BYBIT HISTORY INVALID JSON | symbol=%s",
                    symbol,
                )
                return None

            if not isinstance(payload, dict):
                return None

            if int(payload.get("retCode", -1)) != 0:
                log.warning(
                    "BYBIT HISTORY API ERROR | "
                    "symbol=%s retCode=%s retMsg=%s",
                    symbol,
                    payload.get("retCode"),
                    payload.get("retMsg"),
                )
                return None

            result_block = payload.get("result") or {}
            raw = result_block.get("list") or []

            if not isinstance(raw, list):
                return None

            raw = list(reversed(raw))
            raw = raw[-limit:]

            log.info(
                "BYBIT HISTORY OK | "
                "symbol=%s candles=%s/%s",
                symbol,
                len(raw),
                limit,
            )

            return raw

        except requests.RequestException as exc:
            log.warning(
                "BYBIT HISTORY ERROR | "
                "symbol=%s error=%s",
                symbol,
                exc,
            )
            return None

    def fetch_kucoin_klines(
        self,
        symbol: str,
        limit: int = 864,
    ) -> Optional[list]:

        limit = max(1, int(limit))

        if "-" not in symbol:
            if symbol.endswith("USDT"):
                symbol = (
                    symbol[:-4]
                    + "-USDT"
                )

        params = {
            "symbol": symbol,
            "type": "5min",
        }

        try:
            response = self.session.get(
                KUCOIN_KLINES_ENDPOINT,
                params=params,
                timeout=self.timeout + 2,
            )

            if response.status_code != 200:
                log.warning(
                    "KUCOIN HISTORY HTTP ERROR | "
                    "symbol=%s status=%s",
                    symbol,
                    response.status_code,
                )
                return None

            try:
                payload = response.json()
            except ValueError:
                log.warning(
                    "KUCOIN HISTORY INVALID JSON | "
                    "symbol=%s",
                    symbol,
                )
                return None

            if not isinstance(payload, dict):
                return None

            raw = payload.get("data", [])

            if not isinstance(raw, list):
                return None

            raw = list(reversed(raw))

            raw = raw[-limit:]

            log.info(
                "KUCOIN HISTORY OK | "
                "symbol=%s candles=%s/%s",
                symbol,
                len(raw),
                limit,
            )

            return raw

        except requests.RequestException as exc:
            log.warning(
                "KUCOIN HISTORY ERROR | "
                "symbol=%s error=%s",
                symbol,
                exc,
            )
            return None

    # =========================================================
    # NORMALIZED CANDLE OBJECTS
    # =========================================================

    def fetch_binance_candles(
        self,
        symbol: str,
        limit: int = 864,
    ):
        from candle_store import Candle

        raw = self.fetch_binance_klines(
            symbol,
            limit,
        )

        if not raw:
            return []

        candles = []

        for row in raw:
            try:
                candles.append(
                    Candle.from_binance(row)
                )
            except (
                IndexError,
                TypeError,
                ValueError,
            ):
                continue

        return candles

    def fetch_bybit_candles(
        self,
        symbol: str,
        limit: int = 864,
    ):
        from candle_store import Candle

        raw = self.fetch_bybit_klines(
            symbol,
            limit,
        )

        if not raw:
            return []

        candles = []

        for row in raw:
            try:
                candles.append(
                    Candle.from_bybit(row)
                )
            except (
                IndexError,
                TypeError,
                ValueError,
            ):
                continue

        return candles

    def fetch_kucoin_candles(
        self,
        symbol: str,
        limit: int = 864,
    ):
        from candle_store import Candle

        raw = self.fetch_kucoin_klines(
            symbol,
            limit,
        )

        if not raw:
            return []

        candles = []

        for row in raw:
            try:
                candles.append(
                    Candle.from_kucoin(row)
                )
            except (
                IndexError,
                TypeError,
                ValueError,
            ):
                continue

        return candles

    def fetch_candles(
        self,
        source: str,
        symbol: str,
        limit: int = 864,
    ):
        source = source.lower().strip()

        if source == "binance":
            return self.fetch_binance_candles(symbol, limit)
        if source == "bybit":
            return self.fetch_bybit_candles(symbol, limit)
        if source == "kucoin":
            return self.fetch_kucoin_candles(symbol, limit)

        raise ValueError(f"Unsupported market source: {source}")

    # =========================================================
    # ALL SOURCES
    # =========================================================

    def fetch_all_sources(
        self,
    ) -> Tuple[
        Dict[str, dict],
        Dict[str, dict],
        Dict[str, dict],
    ]:
        """
        Returns (binance, bybit, kucoin) tickers independently.

        Binance is skipped entirely while a cooldown is active.
        """

        binance = self.fetch_binance()
        bybit = self.fetch_bybit()
        kucoin = self.fetch_kucoin()

        log.info(
            "MARKET SOURCES | "
            "binance=%s | bybit=%s | kucoin=%s | "
            "binance_cooldown=%.0fs",
            len(binance),
            len(bybit),
            len(kucoin),
            self.binance_cooldown_remaining(),
        )

        return binance, bybit, kucoin
