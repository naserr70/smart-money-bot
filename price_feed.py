"""
Minimal USD price lookups via the free CoinGecko API, used only to convert
on-chain transfer amounts into USD for the exchange-flow whale signal.

IMPORTANT: this module intentionally does NOT reuse the shared HTTP session
that has an urllib3 Retry adapter mounted for status 429 (rate-limited).
Retrying a 429 automatically just hammers CoinGecko's free tier harder and
digs the hole deeper ("too many 429 error responses" after retries were
exhausted). Instead: a plain session with no retries, a longer cache TTL so
far fewer calls are needed in the first place, and a short local cooldown
that's entered as soon as a 429 is seen so we back off completely for a
while instead of retrying immediately.
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
    "TRON": "tron",
}

# Platform slug used by CoinGecko's token_price-by-contract endpoint.
CHAIN_PLATFORM = {
    "ETH": "ethereum",
    "BSC": "binance-smart-chain",
    "TRON": "tron",
}

# CoinGecko's free/demo tier is roughly ~30 calls/min per IP, shared across
# every app on the same host (e.g. all Render free-tier instances can share
# egress IPs). These defaults are deliberately conservative.
DEFAULT_TTL_SEC = 300          # cache each price for 5 minutes
MIN_CALL_INTERVAL_SEC = 2.0    # never call CoinGecko more than ~30/min
RATE_LIMIT_COOLDOWN_SEC = 90   # after a 429, stop calling entirely for a while


class PriceFeed:
    def __init__(self, session: requests.Session = None, timeout: int = 8, ttl_sec: int = DEFAULT_TTL_SEC):
        # Deliberately NOT the shared retry-happy session â see module docstring.
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "SmartMoneyBot/2.0"})
        self.timeout = timeout
        self.ttl_sec = ttl_sec
        self._cache: Dict[str, tuple] = {}  # key -> (price, fetched_at)
        self._last_call_at: float = 0.0
        self._rate_limited_until: float = 0.0

    def _get_cached(self, key: str) -> Optional[float]:
        entry = self._cache.get(key)
        if entry and (time.time() - entry[1]) < self.ttl_sec:
            return entry[0]
        return None

    def _set_cached(self, key: str, price: float) -> None:
        self._cache[key] = (price, time.time())

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_call_at
        if elapsed < MIN_CALL_INTERVAL_SEC:
            time.sleep(MIN_CALL_INTERVAL_SEC - elapsed)

    def _request(self, url: str, params: dict) -> Optional[dict]:
        now = time.time()
        if now < self._rate_limited_until:
            return None  # still cooling down from a previous 429, don't even try

        self._throttle()
        self._last_call_at = time.time()
        try:
            res = self.session.get(url, params=params, timeout=self.timeout)
            if res.status_code == 429:
                self._rate_limited_until = time.time() + RATE_LIMIT_COOLDOWN_SEC
                log.warning(
                    f"CoinGecko rate limit hit; pausing price lookups for {RATE_LIMIT_COOLDOWN_SEC}s."
                )
                return None
            if res.status_code != 200:
                log.warning(f"CoinGecko HTTP {res.status_code} for {url}")
                return None
            return res.json()
        except (requests.RequestException, ValueError) as e:
            log.warning(f"CoinGecko request failed: {e}")
            return None

    def get_native_price_usd(self, chain: str) -> Optional[float]:
        cg_id = NATIVE_COINGECKO_IDS.get(chain)
        if not cg_id:
            return None
        cache_key = f"native:{chain}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        data = self._request(
            f"{COINGECKO_BASE}/simple/price",
            {"ids": cg_id, "vs_currencies": "usd"},
        )
        if not data:
            return None
        price = data.get(cg_id, {}).get("usd")
        if price:
            self._set_cached(cache_key, float(price))
            return float(price)
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

        data = self._request(
            f"{COINGECKO_BASE}/simple/token_price/{platform}",
            {"contract_addresses": contract_address, "vs_currencies": "usd"},
        )
        if not data:
            return None
        price = data.get(contract_address, {}).get("usd")
        if price:
            self._set_cached(cache_key, float(price))
            return float(price)
        return None

    def get_token_prices_usd_batch(self, chain: str, contract_addresses: list) -> Dict[str, float]:
        """Price several contracts on the same chain in as few CoinGecko
        calls as possible: CoinGecko's token_price endpoint accepts a
        comma-separated contract_addresses list in a single request, so
        pricing e.g. 15 distinct tokens seen across a wallet watch-list costs
        1 call instead of 15 â this is what keeps the exchange-flow tracker
        from tripping CoinGecko's free-tier rate limit as the watch-list
        grows. Returns {contract_address_lowercase: price_usd} for whatever
        could be resolved (missing/uncached-and-unreachable ones are simply
        absent from the result, exactly like get_token_price_usd's None)."""
        platform = CHAIN_PLATFORM.get(chain)
        if not platform:
            return {}

        result: Dict[str, float] = {}
        to_fetch = []
        seen = set()
        for addr in contract_addresses:
            addr = (addr or "").lower()
            if not addr or addr in seen:
                continue
            seen.add(addr)
            cached = self._get_cached(f"token:{chain}:{addr}")
            if cached is not None:
                result[addr] = cached
            else:
                to_fetch.append(addr)

        if not to_fetch:
            return result

        # CoinGecko's URL length limits mean very large batches should still
        # be chunked; 50 contracts per call is comfortably safe.
        for i in range(0, len(to_fetch), 50):
            chunk = to_fetch[i:i + 50]
            data = self._request(
                f"{COINGECKO_BASE}/simple/token_price/{platform}",
                {"contract_addresses": ",".join(chunk), "vs_currencies": "usd"},
            )
            if not data:
                continue
            for addr, info in data.items():
                price = info.get("usd")
                if price:
                    self._set_cached(f"token:{chain}:{addr.lower()}", float(price))
                    result[addr.lower()] = float(price)

        return result
