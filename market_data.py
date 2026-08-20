"""
CEX market data providers.

Ticker data:
    Used for current price / 24h price change.

Kline data:
    Used for the real 5-minute candle history.

The analyzer does NOT use the current/open candle for signals.
Only closed 5m candles are used.
"""

import logging
from typing import Dict, List, Optional, Tuple

import requests

from assets import TARGET_SYMBOLS, resolve_alias

log = logging.getLogger(
    "smart_money_bot.market_data"
)


BINANCE_ENDPOINTS = [
    "https://api1.binance.com/api/v3/ticker/24hr",
    "https://api2.binance.com/api/v3/ticker/24hr",
    "https://api3.binance.com/api/v3/ticker/24hr",
    "https://api.binance.com/api/v3/ticker/24hr",
]

BINANCE_KLINES_ENDPOINT = (
    "https://api.binance.com/api/v3/klines"
)

KUCOIN_ENDPOINT = (
    "https://api.kucoin.com/api/v1/market/allTickers"
)

KUCOIN_KLINES_ENDPOINT = (
    "https://api.kucoin.com/api/v1/market/candles"
)


class MarketDataProvider:

    def __init__(
        self,
        session: requests.Session,
        timeout: int = 8,
    ):
        self.session = session
        self.timeout = timeout

    # =========================================================
    # Ticker
    # =========================================================

    def fetch(
        self,
    ) -> Tuple[Dict[str, dict], str]:

        data = self._fetch_binance()

        if data:
            return data, "binance"

        data = self._fetch_kucoin()

        if data:
            return data, "kucoin"

        return {}, "none"

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
                    continue

                raw = res.json()

            except (
                requests.RequestException,
                ValueError,
            ) as e:

                log.warning(
                    "بایننس %s خطا داد: %s",
                    url,
                    e,
                )

                continue

            filtered = {}

            if not isinstance(
                raw,
                list,
            ):
                continue

            for item in raw:

                sym = item.get(
                    "symbol"
                )

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
                            item[
                                "priceChangePercent"
                            ]
                        ),
                    }

                except (
                    KeyError,
                    TypeError,
                    ValueError,
                ):
                    continue

            if filtered:
                return filtered

        return {}

    # =========================================================
    # Binance closed candles
    # =========================================================

    def fetch_recent_5m_candles(
        self,
        binance_symbol: str,
        limit: int = 864,
    ) -> Optional[List[list]]:

        """
        Fetch recent Binance 5m candles.

        We deliberately request the full 72h history only during
        bootstrap/recovery.

        Normal operation should only request a very small number
        of candles.

        Binance kline format:

        [
            openTime,
            open,
            high,
            low,
            close,
            volume,
            closeTime,
            quoteAssetVolume,
            numberOfTrades,
            takerBuyBase,
            takerBuyQuote,
            ignore
        ]
        """

        try:

            res = self.session.get(
                BINANCE_KLINES_ENDPOINT,
                params={
                    "symbol": binance_symbol,
                    "interval": "5m",
                    "limit": min(
                        int(limit),
                        1000,
                    ),
                },
                timeout=self.timeout,
            )

            if res.status_code != 200:
                return None

            raw = res.json()

            if not isinstance(
                raw,
                list,
            ):
                return None

            return raw

        except (
            requests.RequestException,
            ValueError,
            TypeError,
        ):

            return None

    def fetch_latest_5m_candles(
        self,
        binance_symbol: str,
    ) -> Optional[List[list]]:

        """
        Small normal-operation request.

        limit=2 gives us:
            previous closed candle
            current/open candle

        The analyzer/store decides whether a candle is closed.
        """

        return self.fetch_recent_5m_candles(
            binance_symbol,
            limit=2,
        )

    # =========================================================
    # KuCoin ticker
    # =========================================================

    def _fetch_kucoin(
        self,
    ) -> Dict[str, dict]:

        try:

            res = self.session.get(
                KUCOIN_ENDPOINT,
                timeout=self.timeout + 2,
            )

            if res.status_code != 200:
                return {}

            payload = res.json()

        except (
            requests.RequestException,
            ValueError,
        ) as e:

            log.warning(
                "کوکوین خطا داد: %s",
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

            last_price = ticker.get(
                "last"
            )

            vol_value = ticker.get(
                "volValue"
            )

            change_rate = ticker.get(
                "changeRate"
            )

            if (
                last_price is None
                or vol_value is None
                or change_rate is None
            ):
                continue

            try:

                result[
                    resolve_alias(normalized)
                ] = {
                    "lastPrice": float(
                        last_price
                    ),
                    "quoteVolume": float(
                        vol_value
                    ),
                    "priceChangePercent": (
                        float(change_rate)
                        * 100
                    ),
                }

            except (
                TypeError,
                ValueError,
            ):
                continue

        return result

    # =========================================================
    # KuCoin candles
    # =========================================================

    def fetch_kucoin_5m_candles(
        self,
        symbol: str,
        limit: int = 864,
    ) -> Optional[List[list]]:

        """
        KuCoin fallback candle fetch.

        KuCoin's REST Kline API returns:
            [time, open, close, high, low, volume, amount]

        Note:
        Binance remains the primary candle source because the
        project's canonical symbols are currently Binance based.
        """

        kucoin_symbol = (
            symbol[:-4]
            + "-USDT"
            if symbol.endswith("USDT")
            else symbol
        )

        try:

            res = self.session.get(
                KUCOIN_KLINES_ENDPOINT,
                params={
                    "symbol": kucoin_symbol,
                    "type": "5min",
                },
                timeout=self.timeout,
            )

            if res.status_code != 200:
                return None

            payload = res.json()

            data = payload.get(
                "data"
            )

            if not isinstance(
                data,
                list,
            ):
                return None

            # KuCoin returns newest first.
            data = list(
                reversed(data)
            )

            return data[-limit:]

        except (
            requests.RequestException,
            ValueError,
            TypeError,
        ):

            return None