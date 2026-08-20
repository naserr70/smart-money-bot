"""
Minimal USD price lookups via CoinGecko.

This module intentionally does NOT reuse the shared HTTP session because
the main application's session has automatic retries for HTTP 429.

CoinGecko 429 responses are handled with:
- no automatic retry
- minimum request interval
- local cooldown
- cache
- batch token pricing
"""

import logging
import threading
import time
from typing import Dict, Optional

import requests


log = logging.getLogger(
    "smart_money_bot.price_feed"
)


COINGECKO_BASE = (
    "https://api.coingecko.com/api/v3"
)


NATIVE_COINGECKO_IDS = {
    "ETH": "ethereum",
    "BSC": "binancecoin",
    "TRON": "tron",
}


CHAIN_PLATFORM = {
    "ETH": "ethereum",
    "BSC": "binance-smart-chain",
    "TRON": "tron",
}


DEFAULT_TTL_SEC = 300
MIN_CALL_INTERVAL_SEC = 2.0
RATE_LIMIT_COOLDOWN_SEC = 90


class PriceFeed:

    def __init__(
        self,
        session: requests.Session = None,
        timeout: int = 8,
        ttl_sec: int = DEFAULT_TTL_SEC,
        api_key: str = "",
    ):

        # Deliberately use our own session.
        del session

        self.session = requests.Session()

        headers = {
            "User-Agent": "SmartMoneyBot/2.0",
            "Accept": "application/json",
        }

        if api_key:
            headers[
                "x-cg-demo-api-key"
            ] = api_key

        self.session.headers.update(
            headers
        )

        self.timeout = max(
            1,
            int(timeout),
        )

        self.ttl_sec = max(
            1,
            int(ttl_sec),
        )

        self._cache: Dict[
            str,
            tuple,
        ] = {}

        self._last_call_at = 0.0
        self._rate_limited_until = 0.0

        # PriceFeed may be called by multiple whale-tracker operations.
        # Protect throttle/cache/rate-limit state.
        self._lock = threading.RLock()

    # =========================================================
    # CACHE
    # =========================================================

    def _get_cached(
        self,
        key: str,
    ) -> Optional[float]:

        with self._lock:

            entry = self._cache.get(key)

            if not entry:
                return None

            price, fetched_at = entry

            if (
                time.time() - fetched_at
            ) < self.ttl_sec:

                return price

            self._cache.pop(
                key,
                None,
            )

            return None

    def _set_cached(
        self,
        key: str,
        price: float,
    ) -> None:

        if price <= 0:
            return

        with self._lock:
            self._cache[key] = (
                float(price),
                time.time(),
            )

    # =========================================================
    # THROTTLE
    # =========================================================

    def _throttle(self) -> None:

        with self._lock:

            elapsed = (
                time.time()
                - self._last_call_at
            )

            if elapsed < MIN_CALL_INTERVAL_SEC:
                time.sleep(
                    MIN_CALL_INTERVAL_SEC
                    - elapsed
                )

            self._last_call_at = (
                time.time()
            )

    # =========================================================
    # REQUEST
    # =========================================================

    def _request(
        self,
        url: str,
        params: dict,
    ) -> Optional[dict]:

        with self._lock:

            if (
                time.time()
                < self._rate_limited_until
            ):
                return None

        self._throttle()

        try:

            response = self.session.get(
                url,
                params=params,
                timeout=self.timeout,
            )

            if response.status_code == 429:

                with self._lock:
                    self._rate_limited_until = (
                        time.time()
                        + RATE_LIMIT_COOLDOWN_SEC
                    )

                log.warning(
                    "CoinGecko rate limit hit; "
                    "pausing price lookups for %ss.",
                    RATE_LIMIT_COOLDOWN_SEC,
                )

                return None

            if response.status_code != 200:

                log.warning(
                    "CoinGecko HTTP %s | "
                    "url=%s params=%s body=%s",
                    response.status_code,
                    url,
                    params,
                    response.text[:300],
                )

                return None

            try:
                data = response.json()
            except ValueError:

                log.warning(
                    "CoinGecko returned invalid JSON | "
                    "url=%s",
                    url,
                )

                return None

            if not isinstance(data, dict):
                return None

            return data

        except requests.RequestException as exc:

            log.warning(
                "CoinGecko request failed | error=%s",
                exc,
            )

            return None

    # =========================================================
    # NATIVE
    # =========================================================

    def get_native_price_usd(
        self,
        chain: str,
    ) -> Optional[float]:

        chain = (
            chain or ""
        ).upper()

        cg_id = NATIVE_COINGECKO_IDS.get(
            chain
        )

        if not cg_id:
            return None

        cache_key = (
            f"native:{chain}"
        )

        cached = self._get_cached(
            cache_key
        )

        if cached is not None:
            return cached

        data = self._request(
            f"{COINGECKO_BASE}/simple/price",
            {
                "ids": cg_id,
                "vs_currencies": "usd",
            },
        )

        if not data:
            return None

        try:
            price = float(
                data[cg_id]["usd"]
            )
        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            return None

        if price <= 0:
            return None

        self._set_cached(
            cache_key,
            price,
        )

        return price

    # =========================================================
    # TOKEN
    # =========================================================

    def get_token_price_usd(
        self,
        chain: str,
        contract_address: str,
    ) -> Optional[float]:

        chain = (
            chain or ""
        ).upper()

        platform = CHAIN_PLATFORM.get(
            chain
        )

        if (
            not platform
            or not contract_address
        ):
            return None

        contract_address = (
            contract_address.strip()
            .lower()
        )

        cache_key = (
            f"token:{chain}:"
            f"{contract_address}"
        )

        cached = self._get_cached(
            cache_key
        )

        if cached is not None:
            return cached

        data = self._request(
            (
                f"{COINGECKO_BASE}/simple/"
                f"token_price/{platform}"
            ),
            {
                "contract_addresses":
                    contract_address,
                "vs_currencies": "usd",
            },
        )

        if not data:
            return None

        try:
            price = float(
                data[
                    contract_address
                ]["usd"]
            )
        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            return None

        if price <= 0:
            return None

        self._set_cached(
            cache_key,
            price,
        )

        return price

    # =========================================================
    # BATCH TOKEN PRICING
    # =========================================================

    def get_token_prices_usd_batch(
        self,
        chain: str,
        contract_addresses: list,
    ) -> Dict[str, float]:

        chain = (
            chain or ""
        ).upper()

        platform = CHAIN_PLATFORM.get(
            chain
        )

        if not platform:
            return {}

        result: Dict[
            str,
            float,
        ] = {}

        to_fetch = []
        seen = set()

        for address in (
            contract_addresses or []
        ):

            address = (
                address or ""
            ).strip().lower()

            if not address:
                continue

            if address in seen:
                continue

            seen.add(address)

            cache_key = (
                f"token:{chain}:{address}"
            )

            cached = self._get_cached(
                cache_key
            )

            if cached is not None:
                result[address] = cached
            else:
                to_fetch.append(address)

        if not to_fetch:
            return result

        # Keep requests reasonably small.
        for start in range(
            0,
            len(to_fetch),
            50,
        ):

            chunk = to_fetch[
                start:start + 50
            ]

            data = self._request(
                (
                    f"{COINGECKO_BASE}/simple/"
                    f"token_price/{platform}"
                ),
                {
                    "contract_addresses":
                        ",".join(chunk),
                    "vs_currencies": "usd",
                },
            )

            if not data:
                continue

            for address, info in data.items():

                if not isinstance(
                    info,
                    dict,
                ):
                    continue

                try:
                    price = float(
                        info["usd"]
                    )
                except (
                    KeyError,
                    TypeError,
                    ValueError,
                ):
                    continue

                if price <= 0:
                    continue

                address = (
                    address.lower()
                )

                self._set_cached(
                    f"token:{chain}:{address}",
                    price,
                )

                result[address] = price

        return result