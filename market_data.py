"""
Independent Binance / KuCoin market data provider.

Rules:
- Binance and KuCoin are completely independent.
- No history is copied between exchanges.
- Both sources can maintain their own candle history.
- Market analyzer chooses Binance when available, otherwise KuCoin.
"""

import logging
from typing import Dict, List, Optional, Tuple

import requests

from assets import TARGET_SYMBOLS, resolve_alias

log = logging.getLogger("smart_money_bot.market_data")


BINANCE_TICKER_ENDPOINTS = [
    "https://api1.binance.com/api/v3/ticker/24hr",
    "https://api2.binance.com/api/v3/ticker/24hr",
    "https://api3.binance.com/api/v3/ticker/24hr",
    "https://api.binance.com/api/v3/ticker/24hr",
]

BINANCE_KLINES_ENDPOINTS = [
    "https://api1.binance.com/api/v3/klines",
    "https://api2.binance.com/api/v3/klines",
    "https://api3.binance.com/api/v3/klines",
    "https://api.binance.com/api/v3/klines",
]

KUCOIN_TICKER_ENDPOINT = (
    "https://api.kucoin.com/api/v1/market/allTickers"
)

KUCOIN_KLINES_ENDPOINT = (
    "https://api.kucoin.com/api/v1/market/candles"
)


class MarketDataProvider:

    def __init__(
        self,
        session: requests.Session,
        timeout: int = 10,
    ):
        self.session = session
        self.timeout = timeout

    # =========================================================
    # TICKERS
    # =========================================================

    def fetch_binance(self) -> Dict[str, dict]:

        log.info(
            "BINANCE TICKER FETCH START"
        )

        for endpoint in BINANCE_TICKER_ENDPOINTS:

            try:

                response = self.session.get(
                    endpoint,
                    timeout=self.timeout,
                )

                if response.status_code != 200:

                    log.warning(
                        "BINANCE TICKER HTTP ERROR | status=%s endpoint=%s",
                        response.status_code,
                        endpoint,
                    )

                    continue

                raw = response.json()

                if not isinstance(raw, list):
                    continue

                result = {}

                for item in raw:

                    symbol = item.get(
                        "symbol"
                    )

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
                        "BINANCE TICKER OK | symbols=%s endpoint=%s",
                        len(result),
                        endpoint,
                    )

                    return result

            except requests.RequestException as e:

                log.warning(
                    "BINANCE TICKER REQUEST ERROR | endpoint=%s error=%s",
                    endpoint,
                    e,
                )

        log.error(
            "BINANCE TICKER FAILED | all endpoints unavailable"
        )

        return {}

    def fetch_kucoin(self) -> Dict[str, dict]:

        log.info(
            "KUCOIN TICKER FETCH START"
        )

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

            payload = response.json()

            tickers = (
                payload
                .get("data", {})
                .get("ticker", [])
            )

            result = {}

            for item in tickers:

                raw_symbol = item.get(
                    "symbol",
                    "",
                )

                if not raw_symbol.endswith(
                    "-USDT"
                ):
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
                            ) * 100
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

        except (
            requests.RequestException,
            ValueError,
        ) as e:

            log.error(
                "KUCOIN TICKER FAILED | error=%s",
                e,
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

        params = {
            "symbol": symbol,
            "interval": "5m",
            "limit": min(limit, 1000),
        }

        for endpoint in BINANCE_KLINES_ENDPOINTS:

            try:

                response = self.session.get(
                    endpoint,
                    params=params,
                    timeout=self.timeout,
                )

                if response.status_code != 200:

                    log.warning(
                        "BINANCE HISTORY HTTP ERROR | symbol=%s status=%s endpoint=%s",
                        symbol,
                        response.status_code,
                        endpoint,
                    )

                    continue

                raw = response.json()

                if not isinstance(raw, list):

                    return None

                log.info(
                    "BINANCE HISTORY OK | symbol=%s candles=%s/%s",
                    symbol,
                    len(raw),
                    limit,
                )

                return raw

            except (
                requests.RequestException,
                ValueError,
            ) as e:

                log.warning(
                    "BINANCE HISTORY ERROR | symbol=%s endpoint=%s error=%s",
                    symbol,
                    endpoint,
                    e,
                )

        return None

    def fetch_kucoin_klines(
        self,
        symbol: str,
        limit: int = 864,
    ) -> Optional[list]:

        # KuCoin uses SYMBOL-USDT.
        if "-" not in symbol:

            symbol = (
                symbol[:-4]
                + "-USDT"
                if symbol.endswith("USDT")
                else symbol
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
                    "KUCOIN HISTORY HTTP ERROR | symbol=%s status=%s",
                    symbol,
                    response.status_code,
                )

                return None

            payload = response.json()

            raw = payload.get(
                "data",
                [],
            )

            if not isinstance(raw, list):
                return None

            # KuCoin can return newest first.
            raw = list(reversed(raw))

            raw = raw[-limit:]

            log.info(
                "KUCOIN HISTORY OK | symbol=%s candles=%s/%s",
                symbol,
                len(raw),
                limit,
            )

            return raw

        except (
            requests.RequestException,
            ValueError,
        ) as e:

            log.warning(
                "KUCOIN HISTORY ERROR | symbol=%s error=%s",
                symbol,
                e,
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

    # =========================================================
    # BOTH SOURCES
    # =========================================================

    def fetch_all_sources(
        self,
    ) -> Tuple[
        Dict[str, dict],
        Dict[str, dict],
    ]:

        binance = self.fetch_binance()
        kucoin = self.fetch_kucoin()

        log.info(
            "MARKET SOURCES | binance_symbols=%s | kucoin_symbols=%s",
            len(binance),
            len(kucoin),
        )

        return binance, kucoin