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
# should) extend/replace this list via the EXCHANGE_WALLETS_JSON env var —
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
        # Left empty deliberately — I could not verify a specific exchange's
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

    # --- Access control (password gate + per-user time-limited access) ---
    bot_access_password: str = field(default_factory=lambda: _env_str("BOT_ACCESS_PASSWORD"))
    admin_chat_id: str = field(default_factory=lambda: _env_str("ADMIN_CHAT_ID"))
    developer_name: str = field(default_factory=lambda: _env_str("DEVELOPER_NAME", "ناصر رومی‌پور"))
    default_access_duration_days: float = field(default_factory=lambda: _env_float("DEFAULT_ACCESS_DURATION_DAYS", 30))
    auth_state_file_path: str = field(default_factory=lambda: _env_str("AUTH_STATE_FILE_PATH", "authorized_users.json"))
    github_gist_id: str = field(default_factory=lambda: _env_str("GITHUB_GIST_ID"))
    github_gist_token: str = field(default_factory=lambda: _env_str("GITHUB_GIST_TOKEN"))
    telegram_webhook_secret: str = field(default_factory=lambda: _env_str("TELEGRAM_WEBHOOK_SECRET"))

    # --- CEX ticker-based smart-money detection ---
    min_inflow_usd_5m: float = field(default_factory=lambda: _env_float("MIN_INFLOW_USD_5M", 50_000))
    volume_spike_ratio: float = field(default_factory=lambda: _env_float("VOLUME_SPIKE_RATIO", 2.5))
    price_pump_min: float = field(default_factory=lambda: _env_float("PRICE_PUMP_MIN", 1.0))
    price_pump_max: float = field(default_factory=lambda: _env_float("PRICE_PUMP_MAX", 8.0))
    alert_cooldown_sec: int = field(default_factory=lambda: _env_int("ALERT_COOLDOWN_SEC", 1800))
    scan_interval_sec: int = field(default_factory=lambda: _env_int("SCAN_INTERVAL_SEC", 300))
    # 36 candles = 3 hours of 5-min data. 12 (1 hour) was too thin — the
    # standard error of a 12-sample mean is noticeably larger, so the
    # baseline itself could jump around from 1-2 unusual candles. 36 cuts
    # that noise by ~40% while still being responsive enough to track real
    # shifts in a coin's typical volume within a few hours. Still a purely
    # trailing window though — it doesn't know "this hour is normally
    # quieter" the way a same-hour-across-days average would; that's a
    # separate, bigger feature if you want full time-of-day awareness.
    history_window: int = field(default_factory=lambda: _env_int("HISTORY_WINDOW", 36))
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
    coingecko_api_key: str = field(default_factory=lambda: _env_str("COINGECKO_API_KEY"))
    exchange_wallets: Dict[str, Dict[str, str]] = field(
        default_factory=lambda: _load_exchange_wallets()
    )

    # --- Persistence (survive restarts) ---
    state_file_path: str = field(default_factory=lambda: _env_str("STATE_FILE_PATH", "bot_state.json"))
    state_save_interval_sec: int = field(default_factory=lambda: _env_int("STATE_SAVE_INTERVAL_SEC", 60))

    # --- HTTP ---
    http_timeout_sec: int = field(default_factory=lambda: _env_int("HTTP_TIMEOUT_SEC", 10))
    http_max_retries: int = field(default_factory=lambda: _env_int("HTTP_MAX_RETRIES", 3))

    @property
    def admin_chat_id_resolved(self) -> str:
        """The admin identity: explicit ADMIN_CHAT_ID if set, otherwise falls
        back to CHAT_ID (keeps single-user setups simple — no extra env var
        needed unless you want the admin to be a different chat than the
        original default recipient)."""
        return self.admin_chat_id or self.chat_id

    def validate(self) -> List[str]:
        """Return a list of human-readable configuration problems (non-fatal warnings included)."""
        problems = []
        if not self.bot_token:
            problems.append("BOT_TOKEN تنظیم نشده است — ارسال پیام غیرممکن خواهد بود.")
        if not self.chat_id:
            problems.append("CHAT_ID تنظیم نشده است — ارسال پیام غیرممکن خواهد بود.")
        if not self.bot_access_password:
            problems.append(
                "BOT_ACCESS_PASSWORD تنظیم نشده — هر کسی که chat_id ربات را پیدا کند می‌تواند بدون رمز درخواست دسترسی بدهد."
            )
        if not self.telegram_webhook_secret:
            problems.append(
                "TELEGRAM_WEBHOOK_SECRET تنظیم نشده — وبهوک بدون این مقدار قابل جعل است؛ یک رشته‌ی تصادفی تنظیم کنید."
            )
        if not (self.github_gist_id and self.github_gist_token):
            problems.append(
                "GITHUB_GIST_ID/GITHUB_GIST_TOKEN تنظیم نشده — لیست کاربران مجاز فقط روی دیسک موقتی نگه داشته می‌شود "
                "و با هر ری‌استارت سرویس روی Render (حتی خودکار) از بین می‌رود."
            )
        if self.price_pump_min >= self.price_pump_max:
            problems.append("PRICE_PUMP_MIN باید کوچکتر از PRICE_PUMP_MAX باشد.")
        if self.scan_interval_sec <= 0:
            problems.append("SCAN_INTERVAL_SEC باید مثبت باشد.")
        if not self.etherscan_api_key:
            problems.append(
                "ETHERSCAN_API_KEY تنظیم نشده — ماژول ردیابی ولت/صرافی (Exchange Flow) غیرفعال می‌ماند."
            )
        if not self.coingecko_api_key:
            problems.append(
                "COINGECKO_API_KEY تنظیم نشده — روی IPهای مشترک (مثل Render) قیمت‌گذاری آن‌چین ممکن است "
                "دائماً rate-limit بخورد. یک کلید Demo رایگان از coingecko.com/en/developers/dashboard بگیرید."
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
