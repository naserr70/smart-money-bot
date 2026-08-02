"""
Centralized, validated configuration for the Smart Money Bot.
All values are read from environment variables with sane defaults, so
nothing here needs to be edited by hand for normal deployment.
"""
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List


def _env_str(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    return _env_str(key, str(default)).lower() in ("1", "true", "yes", "on")


# Verified, publicly-labelled exchange hot-wallet addresses (Etherscan public
# tags). These are public blockchain addresses, not credentials. You can (and
# should) extend/replace this list via the EXCHANGE_WALLETS_JSON env var â
# always double-check any address on etherscan.io/bscscan.com before trusting it.
DEFAULT_EXCHANGE_WALLETS: Dict[str, Dict[str, str]] = {
    "ETH": {
        "0x28c6c06298d514db089934071355e5743bf21d60": "Binance 14",
        "0xf977814e90da44bfa03b6295a0616a897441acec": "Binance Hot Wallet 20",
    },
    "BSC": {
        # Populate with verified BscScan-labelled addresses if you have a
        # BSCSCAN_API_KEY configured.
    },
    "TRON": {
        # Populate with verified Tronscan-labelled addresses (tronscan.org).
        # Left empty deliberately â I could not verify a specific exchange's
        # TRC20 hot-wallet address confidently enough to hardcode it here;
        # guessing one would repeat exactly the SYS/SYN mistake this project
        # already learned from. Add your own via EXCHANGE_WALLETS_JSON, e.g.:
        #   {"TRON": {"T...verified_address...": "Some Exchange"}}
    },
}


@dataclass(frozen=True)
class Settings:
    # --- Telegram ---
    bot_token: str = field(default_factory=lambda: _env_str("BOT_TOKEN"))
    chat_id: str = field(default_factory=lambda: _env_str("CHAT_ID"))

    # --- CEX ticker-based smart-money detection ---
    min_inflow_usd_5m: float = field(default_factory=lambda: _env_float("MIN_INFLOW_USD_5M", 50_000))
    volume_spike_ratio: float = field(default_factory=lambda: _env_float("VOLUME_SPIKE_RATIO", 2.5))
    price_pump_min: float = field(default_factory=lambda: _env_float("PRICE_PUMP_MIN", 1.0))
    price_pump_max: float = field(default_factory=lambda: _env_float("PRICE_PUMP_MAX", 8.0))
    alert_cooldown_sec: int = field(default_factory=lambda: _env_int("ALERT_COOLDOWN_SEC", 1800))
    scan_interval_sec: int = field(default_factory=lambda: _env_int("SCAN_INTERVAL_SEC", 300))
    history_window: int = field(default_factory=lambda: _env_int("HISTORY_WINDOW", 12))
    pump_zscore_enabled: bool = field(default_factory=lambda: _env_bool("PUMP_ZSCORE_ENABLED", True))
    pump_zscore_threshold: float = field(default_factory=lambda: _env_float("PUMP_ZSCORE_THRESHOLD", 3.0))

    # --- Status / housekeeping ---
    send_status_report: bool = field(default_factory=lambda: _env_bool("SEND_STATUS_REPORT", True))
    auto_delete_delay_sec: int = field(default_factory=lambda: _env_int("AUTO_DELETE_DELAY_SEC", 300))

    # --- On-chain exchange-wallet flow tracking (independent whale signal) ---
    etherscan_api_key: str = field(default_factory=lambda: _env_str("ETHERSCAN_API_KEY"))
    bscscan_api_key: str = field(default_factory=lambda: _env_str("BSCSCAN_API_KEY"))
    tron_api_key: str = field(default_factory=lambda: _env_str("TRON_API_KEY"))
    whale_min_usd: float = field(default_factory=lambda: _env_float("WHALE_MIN_USD", 500_000))
    whale_scan_interval_sec: int = field(default_factory=lambda: _env_int("WHALE_SCAN_INTERVAL_SEC", 120))
    whale_cooldown_sec: int = field(default_factory=lambda: _env_int("WHALE_COOLDOWN_SEC", 900))
    exchange_wallets: Dict[str, Dict[str, str]] = field(
        default_factory=lambda: _load_exchange_wallets()
    )

    # --- Persistence (survive restarts) ---
    state_file_path: str = field(default_factory=lambda: _env_str("STATE_FILE_PATH", "bot_state.json"))
    state_save_interval_sec: int = field(default_factory=lambda: _env_int("STATE_SAVE_INTERVAL_SEC", 60))

    # --- HTTP ---
    http_timeout_sec: int = field(default_factory=lambda: _env_int("HTTP_TIMEOUT_SEC", 10))
    http_max_retries: int = field(default_factory=lambda: _env_int("HTTP_MAX_RETRIES", 3))

    def validate(self) -> List[str]:
        """Return a list of human-readable configuration problems (non-fatal warnings included)."""
        problems = []
        if not self.bot_token:
            problems.append("BOT_TOKEN ØªÙØ¸ÛÙ ÙØ´Ø¯Ù Ø§Ø³Øª â Ø§Ø±Ø³Ø§Ù Ù¾ÛØ§Ù ØºÛØ±ÙÙÚ©Ù Ø®ÙØ§ÙØ¯ Ø¨ÙØ¯.")
        if not self.chat_id:
            problems.append("CHAT_ID ØªÙØ¸ÛÙ ÙØ´Ø¯Ù Ø§Ø³Øª â Ø§Ø±Ø³Ø§Ù Ù¾ÛØ§Ù ØºÛØ±ÙÙÚ©Ù Ø®ÙØ§ÙØ¯ Ø¨ÙØ¯.")
        if self.price_pump_min >= self.price_pump_max:
            problems.append("PRICE_PUMP_MIN Ø¨Ø§ÛØ¯ Ú©ÙÚÚ©ØªØ± Ø§Ø² PRICE_PUMP_MAX Ø¨Ø§Ø´Ø¯.")
        if self.scan_interval_sec <= 0:
            problems.append("SCAN_INTERVAL_SEC Ø¨Ø§ÛØ¯ ÙØ«Ø¨Øª Ø¨Ø§Ø´Ø¯.")
        if not self.etherscan_api_key:
            problems.append(
                "ETHERSCAN_API_KEY ØªÙØ¸ÛÙ ÙØ´Ø¯Ù â ÙØ§ÚÙÙ Ø±Ø¯ÛØ§Ø¨Û ÙÙØª/ØµØ±Ø§ÙÛ (Exchange Flow) ØºÛØ±ÙØ¹Ø§Ù ÙÛâÙØ§ÙØ¯."
            )
        return problems


def _load_exchange_wallets() -> Dict[str, Dict[str, str]]:
    """Allow overriding/extending the wallet watch-list via EXCHANGE_WALLETS_JSON,
    formatted as {"ETH": {"0xabc...": "Some Exchange"}, "BSC": {...}}.
    """
    raw = os.environ.get("EXCHANGE_WALLETS_JSON", "").strip()
    if not raw:
        return DEFAULT_EXCHANGE_WALLETS
    try:
        parsed = json.loads(raw)
        merged = {chain: dict(addrs) for chain, addrs in DEFAULT_EXCHANGE_WALLETS.items()}
        for chain, addrs in parsed.items():
            merged.setdefault(chain, {}).update(addrs)
        return merged
    except (json.JSONDecodeError, AttributeError):
        return DEFAULT_EXCHANGE_WALLETS


settings = Settings()
