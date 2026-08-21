"""
Multi-chain exchange-wallet flow tracker — an independent whale signal.

Approach: poll publicly-labelled exchange hot-wallet addresses (see
config.DEFAULT_EXCHANGE_WALLETS / EXCHANGE_WALLETS_JSON) for their most
recent transfers. A large transfer INTO one of these wallets is flagged as
an exchange INFLOW (possible incoming sell pressure); a large transfer OUT
is flagged as an exchange OUTFLOW (coins leaving to cold storage / private
wallets). This mirrors the same public-data technique used by commercial
whale-tracking services — no private or authenticated wallet access is
required, only transactions that are already public on-chain.

Chain coverage (this is the honest, current state — not every chain a
whale could possibly use):
  * ETH / BSC (EVM chains): native coin + ANY ERC-20 token transfer, via
    Etherscan's unified V2 API.
  * TRON: TRC-20 token transfers (this is what matters most in practice —
    the overwhelming majority of USDT whale/exchange flow happens as
    USDT-TRC20 on Tron, not as USDT-ERC20 on Ethereum), via TronGrid.
    Native TRX transfers are NOT tracked yet: TronGrid's raw transaction
    endpoint encodes addresses/amounts inside a hex "raw_data" blob that
    needs extra decoding logic; rather than guess at that encoding, it's
    left out until it can be implemented and tested properly.
  * Bitcoin, Solana, and other non-EVM/non-Tron chains are NOT covered.
    Each would need its own adapter (different explorer API, different
    address/tx format) — the CHAIN_TYPE table below is exactly the seam
    where a new one gets added; ping me when you have verified exchange
    wallet addresses for a specific chain and I'll wire it in.

Limitations (documented honestly rather than hidden):
  * Only covers wallets you list — it is only as complete as your watch-list.
  * USD pricing comes from CoinGecko's free endpoint and can occasionally be
    stale/missing for obscure tokens; such transfers are skipped rather than
    reported with a wrong value (except TRC20-USDT, which is hardcoded to
    $1 if CoinGecko is unreachable, since that peg is the entire point of
    the token).
  * Etherscan/TronGrid API endpoints and parameters can change over time —
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
# param, and a single ETHERSCAN_API_KEY works across all of them.
ETHERSCAN_V2_BASE = "https://api.etherscan.io/v2/api"
EVM_CHAIN_ID = {"ETH": 1, "BSC": 56}

TRONGRID_BASE = "https://api.trongrid.io"

# Which adapter handles which chain key (as used in EXCHANGE_WALLETS_JSON).
CHAIN_TYPE = {"ETH": "evm", "BSC": "evm", "TRON": "tron"}


class ExchangeFlowTracker:
    def __init__(self, settings: Settings, state: BotState, session: requests.Session):
        self.settings = settings
        self.state = state
        self.session = session
        self.price_feed = PriceFeed(session, timeout=settings.http_timeout_sec, api_key=settings.coingecko_api_key)
        self._evm_keys = {
            "ETH": settings.etherscan_api_key,
            "BSC": settings.bscscan_api_key or settings.etherscan_api_key,
        }

    def is_enabled(self) -> bool:
        for chain, wallets in self.settings.exchange_wallets.items():
            if not wallets:
                continue
            chain_type = CHAIN_TYPE.get(chain)
            if chain_type == "evm" and self._evm_keys.get(chain):
                return True
            if chain_type == "tron":
                return True  # TronGrid works keyless too, just more rate-limited
        return False

    def scan(self) -> List[ExchangeFlowSignal]:
        signals: List[ExchangeFlowSignal] = []
        for chain, wallets in self.settings.exchange_wallets.items():
            if not wallets:
                continue
            chain_type = CHAIN_TYPE.get(chain)
            if chain_type == "evm":
                api_key = self._evm_keys.get(chain)
                if not api_key:
                    continue
                signals.extend(self._scan_evm_chain(chain, api_key, wallets))
            elif chain_type == "tron":
                for address, label in wallets.items():
                    signals.extend(self._scan_tron_trc20(address, label))
            else:
                log.warning(f"زنجیره‌ی ناشناخته در EXCHANGE_WALLETS_JSON نادیده گرفته شد: {chain}")
        return signals

    def _direction_for(self, address: str, to_addr: str) -> SignalDirection:
        return SignalDirection.INFLOW if to_addr.lower() == address.lower() else SignalDirection.OUTFLOW

    # ==================== EVM (Etherscan V2) ====================

    def _evm_api_get(self, chain: str, params: dict) -> list:
        chain_id = EVM_CHAIN_ID.get(chain)
        if chain_id is None:
            return []
        params = {**params, "chainid": chain_id}
        try:
            res = self.session.get(ETHERSCAN_V2_BASE, params=params, timeout=self.settings.http_timeout_sec)
            payload = res.json()
            if str(payload.get("message", "")).upper() == "NOTOK":
                # Distinguish "bad key / bad request" from "no transactions" —
                # a silently-broken key should never look like quiet markets.
                log.error(f"Etherscan {chain} خطای NOTOK داد: {payload.get('result')}")
                return []
            result = payload.get("result")
            return result if isinstance(result, list) else []
        except (requests.RequestException, ValueError) as e:
            log.warning(f"{chain} API خطا داد: {e}")
            return []

    def _scan_evm_chain(self, chain: str, api_key: str, wallets: Dict[str, str]) -> List[ExchangeFlowSignal]:
        signals: List[ExchangeFlowSignal] = []
        native_symbol = "ETH" if chain == "ETH" else "BNB"
        native_price = self.price_feed.get_native_price_usd(chain)

        # Two-phase token scan: first collect every candidate transfer across
        # ALL wallets on this chain, then batch-price the distinct contracts
        # in as few CoinGecko calls as possible (instead of one call per
        # transfer, which is how the previous version worked and is what was
        # making CoinGecko rate-limit under a larger watch-list).
        token_candidates = []  # list of (address, label, tx)
        for address, label in wallets.items():
            if native_price is not None:
                signals.extend(self._scan_evm_native(chain, api_key, address, label, native_symbol, native_price))
            txs = self._evm_api_get(chain, {
                "module": "account", "action": "tokentx", "address": address,
                "sort": "desc", "offset": 30, "page": 1, "apikey": api_key,
            })
            for tx in txs:
                token_candidates.append((address, label, tx))

        contracts = {tx.get("contractAddress", "").lower() for _, _, tx in token_candidates if tx.get("contractAddress")}
        prices = self.price_feed.get_token_prices_usd_batch(chain, list(contracts))

        for address, label, tx in token_candidates:
            tx_hash = tx.get("hash", "")
            contract = tx.get("contractAddress", "").lower()
            if not tx_hash or not self.state.is_new_tx(f"{tx_hash}:{contract}"):
                continue
            try:
                decimals = int(tx.get("tokenDecimal", 18))
                value_native = float(tx.get("value", 0)) / (10 ** decimals)
            except (TypeError, ValueError):
                continue
            if value_native <= 0:
                continue

            price = prices.get(contract)
            if price is None:
                continue  # don't report a value we can't verify

            amount_usd = value_native * price
            if amount_usd < self.settings.whale_min_usd:
                continue

            token_symbol = tx.get("tokenSymbol", "?")
            direction = self._direction_for(address, tx.get("to", ""))
            cooldown_key = f"{chain}:{address}:{token_symbol}:{direction.value}"
            if self.state.is_in_cooldown(cooldown_key, self.settings.whale_cooldown_sec):
                continue

            signals.append(ExchangeFlowSignal(
                chain=chain, token_symbol=token_symbol, exchange_name=label,
                amount_usd=amount_usd, amount_native=value_native,
                tx_hash=tx_hash, direction=direction,
            ))
            self.state.mark_alerted(cooldown_key)

        return signals

    def _scan_evm_native(self, chain: str, api_key: str, address: str, label: str,
                          native_symbol: str, native_price: float) -> List[ExchangeFlowSignal]:
        txs = self._evm_api_get(chain, {
            "module": "account", "action": "txlist", "address": address,
            "sort": "desc", "offset": 20, "page": 1, "apikey": api_key,
        })
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
            amount_usd = value_native * native_price
            if amount_usd < self.settings.whale_min_usd:
                continue

            direction = self._direction_for(address, tx.get("to", ""))
            cooldown_key = f"{chain}:{address}:{native_symbol}:{direction.value}"
            if self.state.is_in_cooldown(cooldown_key, self.settings.whale_cooldown_sec):
                continue

            out.append(ExchangeFlowSignal(
                chain=chain, token_symbol=native_symbol, exchange_name=label,
                amount_usd=amount_usd, amount_native=value_native,
                tx_hash=tx_hash, direction=direction,
            ))
            self.state.mark_alerted(cooldown_key)
        return out

    # ==================== TRON (TronGrid) ====================

    def _tron_headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.settings.tron_api_key:
            headers["TRON-PRO-API-KEY"] = self.settings.tron_api_key
        return headers

    def _scan_tron_trc20(self, address: str, label: str) -> List[ExchangeFlowSignal]:
        url = f"{TRONGRID_BASE}/v1/accounts/{address}/transactions/trc20"
        try:
            res = self.session.get(
                url, params={"limit": 30, "only_confirmed": "true"},
                headers=self._tron_headers(), timeout=self.settings.http_timeout_sec,
            )
            payload = res.json()
            if payload.get("success") is False:
                log.error(f"TronGrid خطا داد: {payload.get('error')}")
                return []
            txs = payload.get("data", [])
        except (requests.RequestException, ValueError) as e:
            log.warning(f"TRON API خطا داد: {e}")
            return []

        out: List[ExchangeFlowSignal] = []
        for tx in txs:
            tx_id = tx.get("transaction_id", "")
            token_info = tx.get("token_info", {}) or {}
            contract = token_info.get("address", "")
            if not tx_id or not self.state.is_new_tx(f"{tx_id}:{contract}"):
                continue
            try:
                decimals = int(token_info.get("decimals", 6))
                value_native = float(tx.get("value", 0)) / (10 ** decimals)
            except (TypeError, ValueError):
                continue
            if value_native <= 0:
                continue

            token_symbol = token_info.get("symbol", "?")
            price = self.price_feed.get_token_price_usd("TRON", contract)
            if price is None:
                if token_symbol.upper() == "USDT":
                    price = 1.0  # the entire point of the token is that this holds
                else:
                    continue

            amount_usd = value_native * price
            if amount_usd < self.settings.whale_min_usd:
                continue

            to_addr = tx.get("to", "")
            direction = SignalDirection.INFLOW if to_addr == address else SignalDirection.OUTFLOW
            cooldown_key = f"TRON:{address}:{token_symbol}:{direction.value}"
            if self.state.is_in_cooldown(cooldown_key, self.settings.whale_cooldown_sec):
                continue

            out.append(ExchangeFlowSignal(
                chain="TRON", token_symbol=token_symbol, exchange_name=label,
                amount_usd=amount_usd, amount_native=value_native,
                tx_hash=tx_id, direction=direction,
            ))
            self.state.mark_alerted(cooldown_key)
        return out
        