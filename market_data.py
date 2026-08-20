"""
CEX ticker data providers. Binance is tried first (several anycast endpoints,
for redundancy), then KuCoin as a fallback if Binance is unreachable
(e.g. geo-blocked). Each provider normalizes its response into:

    {SYMBOL: {"lastPrice": float, "quoteVolume": float, "priceChangePercent": float}}

using Nobitex-canonical symbols (aliases already resolved).
"""
import logging
from typing import Dict, List, Optional, Tuple

import requests

from assets import TARGET_SYMBOLS, resolve_alias

log = logging.getLogger("smart_money_bot.market_data")

BINANCE_ENDPOINTS = [
    "https://api1.binance.com/api/v3/ticker/24hr",
    "https://api2.binance.com/api/v3/ticker/24hr",
    "https://api3.binance.com/api/v3/ticker/24hr",
    "https://api.binance.com/api/v3/ticker/24hr",
]
BINANCE_KLINES_ENDPOINT = "https://api.binance.com/api/v3/klines"
KUCOIN_ENDPOINT = "https://api.kucoin.com/api/v1/market/allTickers"


class MarketDataProvider:
    def __init__(self, session: requests.Session, timeout: int = 8):
        self.session = session
        self.timeout = timeout

    def fetch(self) -> Tuple[Dict[str, dict], str]:
        data = self._fetch_binance()
        if data:
            return data, "binance"
        data = self._fetch_kucoin()
        if data:
            return data, "kucoin"
        return {}, "none"

    def _fetch_binance(self) -> Dict[str, dict]:
        for url in BINANCE_ENDPOINTS:
            try:
                res = self.session.get(url, timeout=self.timeout)
                if res.status_code != 200:
                    continue
                raw = res.json()
            except (requests.RequestException, ValueError) as e:
                log.warning(f"بایننس {url} خطا داد: {e}")
                continue

            filtered: Dict[str, dict] = {}
            for item in raw:
                sym = item.get("symbol")
                if sym not in TARGET_SYMBOLS:
                    continue
                try:
                    filtered[resolve_alias(sym)] = {
                        "lastPrice": float(item["lastPrice"]),
                        "quoteVolume": float(item["quoteVolume"]),
                        "priceChangePercent": float(item["priceChangePercent"]),
                    }
                except (KeyError, TypeError, ValueError):
                    continue
            if filtered:
                return filtered
        return {}

    def fetch_recent_5m_volumes(self, binance_symbol: str, limit: int = 36) -> Optional[List[float]]:
        """Real, per-candle 5-minute quote-volume history from Binance's
        klines endpoint — used to seed an accurate volume baseline
        immediately, instead of the old 24h-rolling-delta proxy (which is
        contaminated by whatever happened at this exact time-of-day
        yesterday) or the naive 24h/288 uniform-distribution guess used
        during the first few cycles after a symbol is first seen.
        Returns None on any failure so the caller can fall back gracefully;
        never raises."""
        try:
            res = self.session.get(
                BINANCE_KLINES_ENDPOINT,
                params={"symbol": binance_symbol, "interval": "5m", "limit": limit},
                timeout=self.timeout,
            )
            if res.status_code != 200:
                return None
            raw = res.json()
            if not isinstance(raw, list):
                return None
            # kline format: [openTime, open, high, low, close, volume, closeTime,
            #                 quoteAssetVolume, numTrades, takerBuyBase, takerBuyQuote, ignore]
            volumes = [float(candle[7]) for candle in raw]
            return volumes if volumes else None
        except (requests.RequestException, ValueError, IndexError, TypeError):
            return None

    def _fetch_kucoin(self) -> Dict[str, dict]:
        try:
            res = self.session.get(KUCOIN_ENDPOINT, timeout=self.timeout + 2)
            if res.status_code != 200:
                return {}
            payload = res.json()
        except (requests.RequestException, ValueError) as e:
            log.warning(f"کوکوین خطا داد: {e}")
            return {}

        tickers = payload.get("data", {}).get("ticker", [])
        result: Dict[str, dict] = {}
        for t in tickers:
            raw_symbol = t.get("symbol", "")
            if not raw_symbol.endswith("-USDT"):
                continue
            normalized = raw_symbol.replace("-", "")
            if normalized not in TARGET_SYMBOLS:
                continue

            last_price = t.get("last")
            vol_value = t.get("volValue")
            change_rate = t.get("changeRate")
            if last_price is None or vol_value is None or change_rate is None:
                continue

            try:
                result[resolve_alias(normalized)] = {
                    "lastPrice": float(last_price),
                    "quoteVolume": float(vol_value),
                    "priceChangePercent": float(change_rate) * 100,
                }
            except (TypeError, ValueError):
                continue
        return result
