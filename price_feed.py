"""
Minimal USD price lookups via the free CoinGecko API, used only to convert
on-chain transfer amounts into USD for the exchange-flow whale signal.
Responses are cached briefly in memory to stay well under CoinGecko's
free-tier rate limits even when scanning many wallets/tokens per cycle.
"""
import logging
import time
from typing import Dict, Optional

import requests

log = logging.getLogger("smart_money_bot.price_feed")

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

NATIVE_COINGECKO_IDS = {
    "ETH": "ethereum",
    "BSC": "binancecoin",
}

# Platform slug used by CoinGecko's token_price-by-contract endpoint.
CHAIN_PLATFORM = {
    "ETH": "ethereum",
    "BSC": "binance-smart-chain",
}


class PriceFeed:
    def __init__(self, session: requests.Session, timeout: int = 8, ttl_sec: int = 60):
        self.session = session
        self.timeout = timeout
        self.ttl_sec = ttl_sec
        self._cache: Dict[str, tuple] = {}  # key -> (price, fetched_at)

    def _get_cached(self, key: str) -> Optional[float]:
        entry = self._cache.get(key)
        if entry and (time.time() - entry[1]) < self.ttl_sec:
            return entry[0]
        return None

    def _set_cached(self, key: str, price: float) -> None:
        self._cache[key] = (price, time.time())

    def get_native_price_usd(self, chain: str) -> Optional[float]:
        cg_id = NATIVE_COINGECKO_IDS.get(chain)
        if not cg_id:
            return None
        cache_key = f"native:{chain}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached
        try:
            res = self.session.get(
                f"{COINGECKO_BASE}/simple/price",
                params={"ids": cg_id, "vs_currencies": "usd"},
                timeout=self.timeout,
            )
            price = res.json().get(cg_id, {}).get("usd")
            if price:
                self._set_cached(cache_key, float(price))
                return float(price)
        except (requests.RequestException, ValueError) as e:
            log.warning(f"Ø¯Ø±ÛØ§ÙØª ÙÛÙØª {chain} ÙØ§ÙÙÙÙ Ø¨ÙØ¯: {e}")
        return None

    def get_token_price_usd(self, chain: str, contract_address: str) -> Optional[float]:
        platform = CHAIN_PLATFORM.get(chain)
        if not platform or not contract_address:
            return None
        contract_address = contract_address.lower()
        cache_key = f"token:{chain}:{contract_address}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached
        try:
            res = self.session.get(
                f"{COINGECKO_BASE}/simple/token_price/{platform}",
                params={"contract_addresses": contract_address, "vs_currencies": "usd"},
                timeout=self.timeout,
            )
            data = res.json().get(contract_address, {})
            price = data.get("usd")
            if price:
                self._set_cached(cache_key, float(price))
                return float(price)
        except (requests.RequestException, ValueError) as e:
            log.warning(f"Ø¯Ø±ÛØ§ÙØª ÙÛÙØª ØªÙÚ©Ù {contract_address} ÙØ§ÙÙÙÙ Ø¨ÙØ¯: {e}")
        return None
