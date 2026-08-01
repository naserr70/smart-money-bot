"""
On-chain exchange-wallet flow tracker — an independent whale signal.

Approach: poll publicly-labelled exchange hot-wallet addresses (see
config.DEFAULT_EXCHANGE_WALLETS / EXCHANGE_WALLETS_JSON) via the free
Etherscan / BscScan "account" API for their most recent native-coin and
ERC-20 transfers. A large transfer INTO one of these wallets is flagged as
an exchange INFLOW (possible incoming sell pressure); a large transfer OUT
is flagged as an exchange OUTFLOW (coins leaving to cold storage / private
wallets). This mirrors the same public-data technique used by commercial
whale-tracking services — no private or authenticated wallet access is
required, only transactions that are already public on-chain.

Limitations (documented honestly rather than hidden):
  * Requires a free ETHERSCAN_API_KEY (https://etherscan.io/apis) to do
    anything at all; BSCSCAN_API_KEY optionally adds BNB Chain coverage.
  * Only covers wallets you list — it is only as complete as your watch-list.
  * USD pricing comes from CoinGecko's free endpoint and can occasionally be
    stale/missing for obscure tokens; such transfers are skipped rather than
    reported with a wrong value.
  * Etherscan/BscScan API endpoints and parameters can change over time —
    verify against their current docs if this stops working.
"""
import logging
from typing import Dict, List

import requests

from config import Settings
from price_feed import PriceFeed
from signals import ExchangeFlowSignal, SignalDirection
from state import BotState

log = logging.getLogger("smart_money_bot.exchange_flow")

# Etherscan's legacy v1 API (separate api.etherscan.io / api.bscscan.com hosts
# with per-chain keys) was fully deprecated on 2025-08-15. Etherscan API V2 is
# a single unified endpoint for 50+ EVM chains, selected via a `chainid` query
# param, and — per Etherscan's own docs — a single ETHERSCAN_API_KEY works
# across all of them (BSCSCAN_API_KEY is kept only as an optional override in
# case you're on a plan/setup that still needs a distinct key for BNB Chain).
ETHERSCAN_V2_BASE = "https://api.etherscan.io/v2/api"
CHAIN_ID = {"ETH": 1, "BSC": 56}


class ExchangeFlowTracker:
    def __init__(self, settings: Settings, state: BotState, session: requests.Session):
        self.settings = settings
        self.state = state
        self.session = session
        self.price_feed = PriceFeed(session, timeout=settings.http_timeout_sec)
        # Prefer a chain-specific key if the user supplied one, otherwise fall
        # back to the single unified Etherscan key.
        self._api_keys = {
            "ETH": settings.etherscan_api_key,
            "BSC": settings.bscscan_api_key or settings.etherscan_api_key,
        }

    def is_enabled(self) -> bool:
        return any(
            self._api_keys.get(chain) and addrs
            for chain, addrs in self.settings.exchange_wallets.items()
        )

    def scan(self) -> List[ExchangeFlowSignal]:
        signals: List[ExchangeFlowSignal] = []
        for chain, wallets in self.settings.exchange_wallets.items():
            api_key = self._api_keys.get(chain)
            if not api_key or not wallets:
                continue
            for address, label in wallets.items():
                signals.extend(self._scan_native(chain, api_key, address, label))
                signals.extend(self._scan_tokens(chain, api_key, address, label))
        return signals

    # ---------------- internal ----------------

    def _api_get(self, chain: str, params: dict) -> list:
        chain_id = CHAIN_ID.get(chain)
        if chain_id is None:
            return []
        params = {**params, "chainid": chain_id}
        try:
            res = self.session.get(ETHERSCAN_V2_BASE, params=params, timeout=self.settings.http_timeout_sec)
            payload = res.json()
            if str(payload.get("status")) == "0" and "deprecated" in str(payload.get("result", "")).lower():
                log.error("Etherscan API نسخه قدیمی شناسایی شد؛ لطفاً پیکربندی را بررسی کنید.")
                return []
            result = payload.get("result")
            return result if isinstance(result, list) else []
        except (requests.RequestException, ValueError) as e:
            log.warning(f"{chain} API خطا داد: {e}")
            return []

    def _direction_for(self, address: str, to_addr: str) -> SignalDirection:
        return SignalDirection.INFLOW if to_addr.lower() == address.lower() else SignalDirection.OUTFLOW

    def _scan_native(self, chain: str, api_key: str, address: str, label: str) -> List[ExchangeFlowSignal]:
        params = {
            "module": "account",
            "action": "txlist",
            "address": address,
            "sort": "desc",
            "offset": 20,
            "page": 1,
            "apikey": api_key,
        }
        txs = self._api_get(chain, params)
        native_symbol = "ETH" if chain == "ETH" else "BNB"
        price = self.price_feed.get_native_price_usd(chain)
        if price is None:
            return []

        out: List[ExchangeFlowSignal] = []
        for tx in txs:
            tx_hash = tx.get("hash", "")
            if not tx_hash or not self.state.is_new_tx(tx_hash):
                continue
            try:
                value_native = float(tx.get("value", 0)) / 1e18
            except (TypeError, ValueError):
                continue
            if value_native <= 0:
                continue
            amount_usd = value_native * price
            if amount_usd < self.settings.whale_min_usd:
                continue

            direction = self._direction_for(address, tx.get("to", ""))
            cooldown_key = f"{chain}:{address}:{native_symbol}:{direction.value}"
            if self.state.is_in_cooldown(cooldown_key, self.settings.whale_cooldown_sec):
                continue

            out.append(
                ExchangeFlowSignal(
                    chain=chain,
                    token_symbol=native_symbol,
                    exchange_name=label,
                    amount_usd=amount_usd,
                    amount_native=value_native,
                    tx_hash=tx_hash,
                    direction=direction,
                )
            )
            self.state.mark_alerted(cooldown_key)
        return out

    def _scan_tokens(self, chain: str, api_key: str, address: str, label: str) -> List[ExchangeFlowSignal]:
        params = {
            "module": "account",
            "action": "tokentx",
            "address": address,
            "sort": "desc",
            "offset": 30,
            "page": 1,
            "apikey": api_key,
        }
        txs = self._api_get(chain, params)

        out: List[ExchangeFlowSignal] = []
        for tx in txs:
            tx_hash = tx.get("hash", "")
            if not tx_hash or not self.state.is_new_tx(f"{tx_hash}:{tx.get('contractAddress','')}"):
                continue
            try:
                decimals = int(tx.get("tokenDecimal", 18))
                value_native = float(tx.get("value", 0)) / (10 ** decimals)
            except (TypeError, ValueError):
                continue
            if value_native <= 0:
                continue

            contract = tx.get("contractAddress", "")
            token_symbol = tx.get("tokenSymbol", "?")
            price = self.price_feed.get_token_price_usd(chain, contract)
            if price is None:
                continue  # don't report a value we can't verify

            amount_usd = value_native * price
            if amount_usd < self.settings.whale_min_usd:
                continue

            direction = self._direction_for(address, tx.get("to", ""))
            cooldown_key = f"{chain}:{address}:{token_symbol}:{direction.value}"
            if self.state.is_in_cooldown(cooldown_key, self.settings.whale_cooldown_sec):
                continue

            out.append(
                ExchangeFlowSignal(
                    chain=chain,
                    token_symbol=token_symbol,
                    exchange_name=label,
                    amount_usd=amount_usd,
                    amount_native=value_native,
                    tx_hash=tx_hash,
                    direction=direction,
                )
            )
            self.state.mark_alerted(cooldown_key)
        return out
