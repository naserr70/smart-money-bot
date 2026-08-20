"""
CEX market data providers.

IMPORTANT ARCHITECTURE:

Binance and KuCoin are completely independent sources.

Both are fetched every market cycle:

    Binance -> Binance ticker + Binance candles
    KuCoin  -> KuCoin ticker  + KuCoin candles

The analyzer chooses the active source:

    Binance available -> Binance
    Binance unavailable -> KuCoin

Historical data is NEVER copied between exchanges.
"""

import logging
import time
from typing import Dict, List, Optional, Tuple

import requests

from assets import TARGET_SYMBOLS, resolve_alias
from candle_store import Candle, CandleStore

log = logging.getLogger("smart_money_bot.market_data")


BINANCE_ENDPOINTS = [
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
    "https://api.kucoin.com/api/ua/v1/market/kline"
)


class MarketDataProvider:
    def __init__(
        self,
        session: requests.Session,
        timeout: int = 8,
        candle_store: Optional[CandleStore] = None,
        history_limit: int = 864,
    ):
        self.session = session
        self.timeout = timeout

        self.candle_store = candle_store
        self.history_limit = history_limit

        self._bootstrap_attempted = {
            "binance": set(),
            "kucoin": set(),
        }

        self._last_history_attempt = {
            "binance": {},
            "kucoin": {},
        }

    # =========================================================
    # PUBLIC
    # =========================================================

    def fetch(
        self,
    ) -> Tuple[
        Dict[str, dict],
        str,
    ]:
        """
        Fetch both exchanges.

        Return value:

            active_ticker_data,
            active_source

        Side effect:

            Both exchanges' candle histories are updated.
        """

        log.info("MARKET FETCH START")

        binance_data = self._fetch_binance()
        kucoin_data = self._fetch_kucoin()

        log.info(
            "MARKET SOURCES | binance_symbols=%d | kucoin_symbols=%d",
            len(binance_data),
            len(kucoin_data),
        )

        # History is maintained independently for BOTH sources.
        if self.candle_store:

            self._maintain_history(
                source="binance",
                ticker_data=binance_data,
            )

            self._maintain_history(
                source="kucoin",
                ticker_data=kucoin_data,
            )

        # Binance is primary.
        if binance_data:
            log.info(
                "ACTIVE MARKET SOURCE | Binance | symbols=%d",
                len(binance_data),
            )

            return binance_data, "binance"

        # KuCoin is fallback.
        if kucoin_data:
            log.warning(
                "ACTIVE MARKET SOURCE | KuCoin FALLBACK | "
                "Binance unavailable"
            )

            return kucoin_data, "kucoin"

        log.error(
            "MARKET DATA FAILURE | Binance and KuCoin unavailable"
        )

        return {}, "none"

    # =========================================================
    # BINANCE
    # =========================================================

    def _fetch_binance(
        self,
    ) -> Dict[str, dict]:

        for url in BINANCE_ENDPOINTS:

            try:
                res = self.session.get(
                    url,
                    timeout=self.timeout,
                )

                if res.status_code != 200:
                    log.warning(
                        "BINANCE TICKER HTTP ERROR | "
                        "status=%s endpoint=%s",
                        res.status_code,
                        url,
                    )
                    continue

                raw = res.json()

                if not isinstance(raw, list):
                    log.warning(
                        "BINANCE TICKER INVALID RESPONSE"
                    )
                    continue

            except (
                requests.RequestException,
                ValueError,
            ) as e:

                log.warning(
                    "BINANCE TICKER REQUEST ERROR | error=%s",
                    e,
                )
                continue

            filtered = {}

            for item in raw:

                sym = item.get("symbol")

                if sym not in TARGET_SYMBOLS:
                    continue

                try:
                    filtered[
                        resolve_alias(sym)
                    ] = {
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
                    log.debug(
                        "BINANCE INVALID TICKER | symbol=%s",
                        sym,
                    )
                    continue

            if filtered:

                log.info(
                    "BINANCE TICKER OK | symbols=%d",
                    len(filtered),
                )

                return filtered

        log.error(
            "BINANCE TICKER FAILED | all endpoints unavailable"
        )

        return {}

    # =========================================================
    # KUCOIN
    # =========================================================

    def _fetch_kucoin(
        self,
    ) -> Dict[str, dict]:

        try:
            res = self.session.get(
                KUCOIN_TICKER_ENDPOINT,
                timeout=self.timeout,
            )

            if res.status_code != 200:

                log.warning(
                    "KUCOIN TICKER HTTP ERROR | status=%s",
                    res.status_code,
                )

                return {}

            payload = res.json()

        except (
            requests.RequestException,
            ValueError,
        ) as e:

            log.warning(
                "KUCOIN TICKER REQUEST ERROR | error=%s",
                e,
            )

            return {}

        tickers = (
            payload
            .get("data", {})
            .get("ticker", [])
        )

        result = {}

        for ticker in tickers:

            raw_symbol = ticker.get(
                "symbol",
                "",
            )

            if not raw_symbol.endswith("-USDT"):
                continue

            normalized = raw_symbol.replace(
                "-",
                "",
            )

            if normalized not in TARGET_SYMBOLS:
                continue

            try:

                last_price = float(
                    ticker["last"]
                )

                quote_volume = float(
                    ticker["volValue"]
                )

                change_rate = float(
                    ticker["changeRate"]
                )

                result[
                    resolve_alias(normalized)
                ] = {
                    "lastPrice": last_price,
                    "quoteVolume": quote_volume,
                    "priceChangePercent": (
                        change_rate * 100
                    ),
                }

            except (
                KeyError,
                TypeError,
                ValueError,
            ):
                log.debug(
                    "KUCOIN INVALID TICKER | symbol=%s",
                    raw_symbol,
                )

        if result:

            log.info(
                "KUCOIN TICKER OK | symbols=%d",
                len(result),
            )

        else:

            log.error(
                "KUCOIN TICKER FAILED | no target symbols"
            )

        return result

    # =========================================================
    # HISTORY MAINTENANCE
    # =========================================================

    def _maintain_history(
        self,
        source: str,
        ticker_data: Dict[str, dict],
    ) -> None:

        if not self.candle_store:
            return

        symbols = list(ticker_data.keys())

        log.info(
            "HISTORY MAINTENANCE | source=%s symbols=%d",
            source,
            len(symbols),
        )

        for symbol in symbols:

            try:

                # Load local history first.
                self.candle_store.load(
                    source,
                    symbol,
                )

                count = self.candle_store.count(
                    source,
                    symbol,
                )

                # -------------------------------------------------
                # BOOTSTRAP
                # -------------------------------------------------

                if count < self.history_limit:

                    if symbol not in self._bootstrap_attempted[
                        source
                    ]:

                        self._bootstrap_attempted[
                            source
                        ].add(symbol)

                        log.info(
                            "HISTORY BOOTSTRAP | "
                            "source=%s symbol=%s current=%d/%d",
                            source,
                            symbol,
                            count,
                            self.history_limit,
                        )

                        candles = (
                            self.fetch_recent_5m_candles(
                                source,
                                symbol,
                                limit=self.history_limit,
                            )
                        )

                        if candles:

                            self.candle_store.seed(
                                source,
                                symbol,
                                candles,
                            )

                            self.candle_store.save(
                                source,
                                symbol,
                            )

                            log.info(
                                "HISTORY BOOTSTRAP OK | "
                                "source=%s symbol=%s candles=%d/%d",
                                source,
                                symbol,
                                self.candle_store.count(
                                    source,
                                    symbol,
                                ),
                                self.history_limit,
                            )

                        else:

                            log.warning(
                                "HISTORY BOOTSTRAP FAILED | "
                                "source=%s symbol=%s",
                                source,
                                symbol,
                            )

                    # We do not repeatedly hammer REST on every cycle.
                    continue

                # -------------------------------------------------
                # NORMAL UPDATE
                # -------------------------------------------------

                candles = (
                    self.fetch_recent_5m_candles(
                        source,
                        symbol,
                        limit=2,
                    )
                )

                if not candles:
                    log.warning(
                        "LIVE CANDLE UPDATE FAILED | "
                        "source=%s symbol=%s",
                        source,
                        symbol,
                    )
                    continue

                # The API gives the most recent candle(s).
                for candle in candles:
                    self.candle_store.update(
                        source,
                        symbol,
                        candle,
                    )

            except Exception as e:

                log.exception(
                    "HISTORY MAINTENANCE ERROR | "
                    "source=%s symbol=%s error=%s",
                    source,
                    symbol,
                    e,
                )

    # =========================================================
    # CANDLES
    # =========================================================

    def fetch_recent_5m_candles(
        self,
        source: str,
        symbol: str,
        limit: int = 864,
    ) -> Optional[List[Candle]]:

        source = source.lower()

        if source == "binance":

            return self._fetch_binance_candles(
                symbol,
                limit,
            )

        if source == "kucoin":

            return self._fetch_kucoin_candles(
                symbol,
                limit,
            )

        raise ValueError(
            f"Unsupported source: {source}"
        )

    # ---------------------------------------------------------
    # Binance candles
    # ---------------------------------------------------------

    def _fetch_binance_candles(
        self,
        symbol: str,
        limit: int,
    ) -> Optional[List[Candle]]:

        for endpoint in BINANCE_KLINES_ENDPOINTS:

            try:

                response = self.session.get(
                    endpoint,
                    params={
                        "symbol": symbol,
                        "interval": "5m",
                        "limit": min(
                            int(limit),
                            1000,
                        ),
                    },
                    timeout=self.timeout,
                )

                if response.status_code != 200:

                    log.warning(
                        "BINANCE KLINE HTTP ERROR | "
                        "symbol=%s status=%s",
                        symbol,
                        response.status_code,
                    )

                    continue

                raw = response.json()

                if not isinstance(raw, list):
                    continue

                candles = []

                now_ms = int(
                    time.time() * 1000
                )

                for item in raw:

                    try:

                        candle = Candle.from_binance(
                            item
                        )

                        # NEVER put an open candle into closed history.
                        if candle.close_time >= now_ms:
                            continue

                        candles.append(candle)

                    except (
                        TypeError,
                        ValueError,
                        IndexError,
                    ):
                        continue

                if candles:

                    return candles

            except (
                requests.RequestException,
                ValueError,
            ) as e:

                log.warning(
                    "BINANCE KLINE ERROR | "
                    "symbol=%s error=%s",
                    symbol,
                    e,
                )

        return None

    # ---------------------------------------------------------
    # KuCoin candles
    # ---------------------------------------------------------

    def _fetch_kucoin_candles(
        self,
        symbol: str,
        limit: int,
    ) -> Optional[List[Candle]]:

        kucoin_symbol = (
            f"{symbol}-USDT"
        )

        now_sec = int(
            time.time()
        )

        start_sec = (
            now_sec
            - (limit * 300)
            - 600
        )

        try:

            response = self.session.get(
                KUCOIN_KLINES_ENDPOINT,
                params={
                    "tradeType": "SPOT",
                    "symbol": kucoin_symbol,
                    "interval": "5min",
                    "startAt": start_sec,
                    "endAt": now_sec,
                },
                timeout=self.timeout,
            )

            if response.status_code != 200:

                log.warning(
                    "KUCOIN KLINE HTTP ERROR | "
                    "symbol=%s status=%s",
                    symbol,
                    response.status_code,
                )

                return None

            payload = response.json()

            if payload.get("code") != "200000":
                log.warning(
                    "KUCOIN KLINE API ERROR | "
                    "symbol=%s response=%s",
                    symbol,
                    payload,
                )
                return None

            raw = payload.get(
                "data",
                [],
            )

            candles = []

            now_ms = int(
                time.time() * 1000
            )

            for item in raw:

                try:

                    candle = Candle.from_kucoin(
                        item
                    )

                    if candle.close_time >= now_ms:
                        continue

                    candles.append(candle)

                except (
                    TypeError,
                    ValueError,
                    IndexError,
                ):
                    continue

            candles.sort(
                key=lambda c: c.open_time
            )

            return candles[-min(limit, 864):]

        except (
            requests.RequestException,
            ValueError,
        ) as e:

            log.warning(
                "KUCOIN KLINE ERROR | "
                "symbol=%s error=%s",
                symbol,
                e,
            )

            return None