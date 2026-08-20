"""
Centralized, validated configuration for Smart Money Bot.

Architecture:
    - Binance and KuCoin are treated as completely independent data sources.
    - Each exchange maintains its own 5-minute candle history.
    - Each symbol keeps up to 864 closed candles = 72 hours.
    - GitHub is used as persistent storage for candle history.
    - Local disk is used as a fast cache.
    - Market signal volume baseline uses the latest 48 CLOSED candles.
    - Pump / dump statistical analysis can use the full 864-candle history.
    - Binance is preferred when available.
    - KuCoin is an independent fallback when Binance is unavailable.
    - Binance history is NEVER copied into KuCoin history and vice versa.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List


# ============================================================
# Environment helpers
# ============================================================

def _env_str(key: str, default: str = "") -> str:
    value = os.environ.get(key, default)
    if value is None:
        return default.strip()
    return str(value).strip()


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
    return _env_str(key, str(default)).lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# ============================================================
# Exchange wallet configuration
# ============================================================

DEFAULT_EXCHANGE_WALLETS: Dict[str, Dict[str, str]] = {
    "ETH": {
        "0x28c6c06298d514db089934071355e5743bf21d60":
            "Binance 14",

        "0xf977814e90da44bfa03b6295a0616a897441acec":
            "Binance Hot Wallet 20",
    },

    "BSC": {
        # Add only verified BscScan-labelled exchange wallets.
    },

    "TRON": {
        # Add only verified TronScan-labelled exchange wallets.
    },
}


def _load_exchange_wallets() -> Dict[str, Dict[str, str]]:
    """
    Load exchange wallets from EXCHANGE_WALLETS_JSON.

    Example:

    {
        "ETH": {
            "0x123...": "Binance"
        },
        "BSC": {
            "0x456...": "Binance"
        }
    }

    User-provided wallets extend the defaults.
    """

    raw = os.environ.get(
        "EXCHANGE_WALLETS_JSON",
        "",
    ).strip()

    if not raw:
        return {
            chain: dict(addresses)
            for chain, addresses
            in DEFAULT_EXCHANGE_WALLETS.items()
        }

    try:
        parsed = json.loads(raw)

        merged = {
            chain: dict(addresses)
            for chain, addresses
            in DEFAULT_EXCHANGE_WALLETS.items()
        }

        if not isinstance(parsed, dict):
            return merged

        for chain, addresses in parsed.items():

            if not isinstance(addresses, dict):
                continue

            merged.setdefault(chain, {}).update(addresses)

        return merged

    except (
        json.JSONDecodeError,
        TypeError,
        AttributeError,
    ):
        return {
            chain: dict(addresses)
            for chain, addresses
            in DEFAULT_EXCHANGE_WALLETS.items()
        }


# ============================================================
# Settings
# ============================================================

@dataclass(frozen=True)
class Settings:

    # ========================================================
    # Telegram
    # ========================================================

    bot_token: str = field(
        default_factory=lambda:
        _env_str("BOT_TOKEN")
    )

    chat_id: str = field(
        default_factory=lambda:
        _env_str("CHAT_ID")
    )

    # ========================================================
    # Access control
    # ========================================================

    bot_access_password: str = field(
        default_factory=lambda:
        _env_str("BOT_ACCESS_PASSWORD")
    )

    admin_chat_id: str = field(
        default_factory=lambda:
        _env_str("ADMIN_CHAT_ID")
    )

    developer_name: str = field(
        default_factory=lambda:
        _env_str(
            "DEVELOPER_NAME",
            "ناصر رومی‌پور",
        )
    )

    default_access_duration_days: float = field(
        default_factory=lambda:
        _env_float(
            "DEFAULT_ACCESS_DURATION_DAYS",
            30,
        )
    )

    auth_state_file_path: str = field(
        default_factory=lambda:
        _env_str(
            "AUTH_STATE_FILE_PATH",
            "authorized_users.json",
        )
    )

    github_gist_id: str = field(
        default_factory=lambda:
        _env_str("GITHUB_GIST_ID")
    )

    github_gist_token: str = field(
        default_factory=lambda:
        _env_str("GITHUB_GIST_TOKEN")
    )

    telegram_webhook_secret: str = field(
        default_factory=lambda:
        _env_str("TELEGRAM_WEBHOOK_SECRET")
    )

    # ========================================================
    # CEX market analysis
    # ========================================================

    #
    # IMPORTANT:
    #
    # MIN_INFLOW_USD_5M has deliberately been removed from
    # signal gating.
    #
    # A signal is NOT required to have a fixed $50K/$100K
    # amount anymore.
    #
    # Volume abnormality itself determines whether the market
    # activity is significant.
    #

    volume_spike_ratio: float = field(
        default_factory=lambda:
        _env_float(
            "VOLUME_SPIKE_RATIO",
            2.0,
        )
    )

    # --------------------------------------------------------
    # Price movement
    # --------------------------------------------------------

    price_pump_min: float = field(
        default_factory=lambda:
        _env_float(
            "PRICE_PUMP_MIN",
            1.0,
        )
    )

    price_pump_max: float = field(
        default_factory=lambda:
        _env_float(
            "PRICE_PUMP_MAX",
            8.0,
        )
    )

    # --------------------------------------------------------
    # Cooldown
    # --------------------------------------------------------

    alert_cooldown_sec: int = field(
        default_factory=lambda:
        _env_int(
            "ALERT_COOLDOWN_SEC",
            1800,
        )
    )

    # --------------------------------------------------------
    # Market scan
    # --------------------------------------------------------

    scan_interval_sec: int = field(
        default_factory=lambda:
        _env_int(
            "SCAN_INTERVAL_SEC",
            300,
        )
    )

    # ========================================================
    # Candle history
    # ========================================================

    #
    # 864 x 5 minutes = 72 hours.
    #

    candle_history_limit: int = field(
        default_factory=lambda:
        _env_int(
            "CANDLE_HISTORY_LIMIT",
            864,
        )
    )

    candle_interval: str = field(
        default_factory=lambda:
        _env_str(
            "CANDLE_INTERVAL",
            "5m",
        )
    )

    #
    # Number of CLOSED candles used for smart-money volume
    # comparison.
    #
    # 48 x 5m = 4 hours.
    #

    volume_baseline_candles: int = field(
        default_factory=lambda:
        _env_int(
            "VOLUME_BASELINE_CANDLES",
            48,
        )
    )

    #
    # Full historical window used for statistical pump/dump
    # analysis.
    #

    pump_history_candles: int = field(
        default_factory=lambda:
        _env_int(
            "PUMP_HISTORY_CANDLES",
            864,
        )
    )

    # ========================================================
    # Pump / dump statistical detector
    # ========================================================

    pump_zscore_enabled: bool = field(
        default_factory=lambda:
        _env_bool(
            "PUMP_ZSCORE_ENABLED",
            True,
        )
    )

    pump_zscore_threshold: float = field(
        default_factory=lambda:
        _env_float(
            "PUMP_ZSCORE_THRESHOLD",
            3.0,
        )
    )

    #
    # Minimum historical observations required before a
    # statistical calculation is considered valid.
    #

    pump_min_history_candles: int = field(
        default_factory=lambda:
        _env_int(
            "PUMP_MIN_HISTORY_CANDLES",
            48,
        )
    )

    # ========================================================
    # Exchange source selection
    # ========================================================

    #
    # Binance is preferred.
    #

    binance_enabled: bool = field(
        default_factory=lambda:
        _env_bool(
            "BINANCE_ENABLED",
            True,
        )
    )

    #
    # KuCoin remains available as an independent fallback.
    #

    kucoin_enabled: bool = field(
        default_factory=lambda:
        _env_bool(
            "KUCOIN_ENABLED",
            True,
        )
    )

    #
    # Never merge Binance and KuCoin candle histories.
    #
    # This is intentionally a configuration flag so the behavior
    # is explicit.
    #

    separate_exchange_history: bool = field(
        default_factory=lambda:
        _env_bool(
            "SEPARATE_EXCHANGE_HISTORY",
            True,
        )
    )

    #
    # Active market source:
    #
    # Binance -> preferred
    # KuCoin  -> fallback
    #

    preferred_market_source: str = field(
        default_factory=lambda:
        _env_str(
            "PREFERRED_MARKET_SOURCE",
            "binance",
        ).lower()
    )

    # ========================================================
    # Candle bootstrap
    # ========================================================

    #
    # Number of candles requested when a symbol has no local
    # history.
    #

    candle_bootstrap_limit: int = field(
        default_factory=lambda:
        _env_int(
            "CANDLE_BOOTSTRAP_LIMIT",
            864,
        )
    )

    #
    # When an exchange already has some history, request only
    # the missing section instead of downloading everything.
    #

    candle_incremental_bootstrap: bool = field(
        default_factory=lambda:
        _env_bool(
            "CANDLE_INCREMENTAL_BOOTSTRAP",
            True,
        )
    )

    # ========================================================
    # Local candle storage
    # ========================================================

    #
    # Fast local cache.
    #
    # Example:
    #
    # market_history/
    #   binance/
    #       BTCUSDT.json
    #       ETHUSDT.json
    #
    #   kucoin/
    #       BTCUSDT.json
    #       ETHUSDT.json
    #

    candle_store_path: str = field(
        default_factory=lambda:
        _env_str(
            "CANDLE_STORE_PATH",
            "market_history",
        )
    )

    #
    # Save local candle changes immediately after an update.
    #

    candle_local_autosave: bool = field(
        default_factory=lambda:
        _env_bool(
            "CANDLE_LOCAL_AUTOSAVE",
            True,
        )
    )

    # ========================================================
    # GitHub candle persistence
    # ========================================================

    #
    # GitHub repository used as persistent storage.
    #
    # Example:
    #
    # naserr70/smart-money-bot
    #

    github_repo: str = field(
        default_factory=lambda:
        _env_str(
            "GITHUB_REPO",
            "naserr70/smart-money-bot",
        )
    )

    #
    # GitHub Personal Access Token.
    #

    github_token: str = field(
        default_factory=lambda:
        _env_str("GITHUB_TOKEN")
    )

    #
    # Branch where candle files are stored.
    #

    github_branch: str = field(
        default_factory=lambda:
        _env_str(
            "GITHUB_BRANCH",
            "main",
        )
    )

    #
    # Directory inside GitHub repository.
    #

    github_candle_path: str = field(
        default_factory=lambda:
        _env_str(
            "GITHUB_CANDLE_PATH",
            "market_history",
        )
    )

    #
    # Backup interval.
    #
    # The bot can update local history every cycle but should not
    # continuously hit GitHub for every individual ticker.
    #
    # Default:
    #
    # 300 sec = 5 minutes
    #

    github_candle_sync_interval_sec: int = field(
        default_factory=lambda:
        _env_int(
            "GITHUB_CANDLE_SYNC_INTERVAL_SEC",
            300,
        )
    )

    #
    # Upload only changed files.
    #

    github_sync_dirty_only: bool = field(
        default_factory=lambda:
        _env_bool(
            "GITHUB_SYNC_DIRTY_ONLY",
            True,
        )
    )

    #
    # Use Git Trees API / Git Data API instead of creating a
    # separate commit for every single candle file.
    #
    # This is important because we may have hundreds of symbols.
    #

    github_use_git_trees_api: bool = field(
        default_factory=lambda:
        _env_bool(
            "GITHUB_USE_GIT_TREES_API",
            True,
        )
    )

    #
    # Maximum number of files included in one GitHub sync.
    #
    # 0 means unlimited.
    #

    github_max_files_per_sync: int = field(
        default_factory=lambda:
        _env_int(
            "GITHUB_MAX_FILES_PER_SYNC",
            0,
        )
    )

    #
    # GitHub request timeout.
    #

    github_http_timeout_sec: int = field(
        default_factory=lambda:
        _env_int(
            "GITHUB_HTTP_TIMEOUT_SEC",
            20,
        )
    )

    #
    # Retry GitHub synchronization after failure.
    #

    github_max_retries: int = field(
        default_factory=lambda:
        _env_int(
            "GITHUB_MAX_RETRIES",
            3,
        )
    )

    # ========================================================
    # Market history source policy
    # ========================================================

    #
    # When Binance is available:
    #
    #     Binance ticker + Binance candles
    #
    # When Binance is unavailable:
    #
    #     KuCoin ticker + KuCoin candles
    #
    # The histories NEVER mix.
    #

    use_source_specific_history: bool = field(
        default_factory=lambda:
        _env_bool(
            "USE_SOURCE_SPECIFIC_HISTORY",
            True,
        )
    )

    #
    # Always maintain both exchange histories whenever data
    # from both exchanges can be obtained.
    #

    maintain_both_exchange_histories: bool = field(
        default_factory=lambda:
        _env_bool(
            "MAINTAIN_BOTH_EXCHANGE_HISTORIES",
            True,
        )
    )

    #
    # If Binance ticker is unavailable but KuCoin is available,
    # use KuCoin for the active signal cycle.
    #

    allow_kucoin_fallback: bool = field(
        default_factory=lambda:
        _env_bool(
            "ALLOW_KUCOIN_FALLBACK",
            True,
        )
    )

    # ========================================================
    # Volume calculation
    # ========================================================

    #
    # Signal condition:
    #
    # current CLOSED 5m candle quote volume
    # ------------------------------------- >= ratio
    # average of previous 48 CLOSED candles
    #
    # No MIN_INFLOW_USD filter.
    #

    volume_signal_enabled: bool = field(
        default_factory=lambda:
        _env_bool(
            "VOLUME_SIGNAL_ENABLED",
            True,
        )
    )

    #
    # 2.0 means:
    #
    # current candle >= 2x average
    #

    volume_signal_multiplier: float = field(
        default_factory=lambda:
        _env_float(
            "VOLUME_SIGNAL_MULTIPLIER",
            2.0,
        )
    )

    #
    # Do NOT normalize a currently-open candle.
    #
    # Signals are generated after a 5m candle is closed.
    #

    signal_only_on_closed_candle: bool = field(
        default_factory=lambda:
        _env_bool(
            "SIGNAL_ONLY_ON_CLOSED_CANDLE",
            True,
        )
    )

    #
    # This prevents the current candle from contaminating its
    # own baseline.
    #

    exclude_current_candle_from_baseline: bool = field(
        default_factory=lambda:
        _env_bool(
            "EXCLUDE_CURRENT_CANDLE_FROM_BASELINE",
            True,
        )
    )

    # ========================================================
    # Price change calculation
    # ========================================================

    #
    # Price movement is calculated from candle OHLC data,
    # not from 24h ticker movement.
    #

    use_candle_price_change: bool = field(
        default_factory=lambda:
        _env_bool(
            "USE_CANDLE_PRICE_CHANGE",
            True,
        )
    )

    #
    # Closed-candle price movement used for static pump/dump.
    #

    static_price_signal_enabled: bool = field(
        default_factory=lambda:
        _env_bool(
            "STATIC_PRICE_SIGNAL_ENABLED",
            True,
        )
    )

    # ========================================================
    # On-chain whale flow
    # ========================================================

    etherscan_api_key: str = field(
        default_factory=lambda:
        _env_str("ETHERSCAN_API_KEY")
    )

    bscscan_api_key: str = field(
        default_factory=lambda:
        _env_str("BSCSCAN_API_KEY")
    )

    tron_api_key: str = field(
        default_factory=lambda:
        _env_str("TRON_API_KEY")
    )

    whale_min_usd: float = field(
        default_factory=lambda:
        _env_float(
            "WHALE_MIN_USD",
            500_000,
        )
    )

    whale_scan_interval_sec: int = field(
        default_factory=lambda:
        _env_int(
            "WHALE_SCAN_INTERVAL_SEC",
            120,
        )
    )

    whale_cooldown_sec: int = field(
        default_factory=lambda:
        _env_int(
            "WHALE_COOLDOWN_SEC",
            900,
        )
    )

    coingecko_api_key: str = field(
        default_factory=lambda:
        _env_str("COINGECKO_API_KEY")
    )

    exchange_wallets: Dict[str, Dict[str, str]] = field(
        default_factory=lambda:
        _load_exchange_wallets()
    )

    # ========================================================
    # General bot state persistence
    # ========================================================

    state_file_path: str = field(
        default_factory=lambda:
        _env_str(
            "STATE_FILE_PATH",
            "bot_state.json",
        )
    )

    state_save_interval_sec: int = field(
        default_factory=lambda:
        _env_int(
            "STATE_SAVE_INTERVAL_SEC",
            60,
        )
    )

    # ========================================================
    # HTTP
    # ========================================================

    http_timeout_sec: int = field(
        default_factory=lambda:
        _env_int(
            "HTTP_TIMEOUT_SEC",
            10,
        )
    )

    http_max_retries: int = field(
        default_factory=lambda:
        _env_int(
            "HTTP_MAX_RETRIES",
            3,
        )
    )

    # ========================================================
    # Status / logging
    # ========================================================

    send_status_report: bool = field(
        default_factory=lambda:
        _env_bool(
            "SEND_STATUS_REPORT",
            True,
        )
    )

    auto_delete_delay_sec: int = field(
        default_factory=lambda:
        _env_int(
            "AUTO_DELETE_DELAY_SEC",
            300,
        )
    )

    #
    # Detailed market analysis logging.
    #

    market_debug_logging: bool = field(
        default_factory=lambda:
        _env_bool(
            "MARKET_DEBUG_LOGGING",
            True,
        )
    )

    #
    # Log every reason why a signal was rejected.
    #

    signal_reason_logging: bool = field(
        default_factory=lambda:
        _env_bool(
            "SIGNAL_REASON_LOGGING",
            True,
        )
    )

    #
    # Log exchange source selection.
    #

    source_logging: bool = field(
        default_factory=lambda:
        _env_bool(
            "SOURCE_LOGGING",
            True,
        )
    )

    # ========================================================
    # Resolved admin
    # ========================================================

    @property
    def admin_chat_id_resolved(self) -> str:
        """
        Explicit ADMIN_CHAT_ID has priority.

        If it is not configured, CHAT_ID is used.
        """

        return self.admin_chat_id or self.chat_id

    # ========================================================
    # Validation
    # ========================================================

    def validate(self) -> List[str]:
        """
        Return configuration warnings/errors.

        Validation is intentionally non-fatal so the bot can start
        and expose the actual configuration problem through logs.
        """

        problems: List[str] = []

        # ----------------------------------------------------
        # Telegram
        # ----------------------------------------------------

        if not self.bot_token:
            problems.append(
                "BOT_TOKEN تنظیم نشده است — ارسال پیام غیرممکن خواهد بود."
            )

        if not self.chat_id:
            problems.append(
                "CHAT_ID تنظیم نشده است — ارسال پیام غیرممکن خواهد بود."
            )

        # ----------------------------------------------------
        # Authentication
        # ----------------------------------------------------

        if not self.bot_access_password:
            problems.append(
                "BOT_ACCESS_PASSWORD تنظیم نشده است."
            )

        if not self.telegram_webhook_secret:
            problems.append(
                "TELEGRAM_WEBHOOK_SECRET تنظیم نشده است."
            )

        # ----------------------------------------------------
        # GitHub authentication
        # ----------------------------------------------------

        if not self.github_token:
            problems.append(
                "GITHUB_TOKEN تنظیم نشده است — "
                "ذخیره تاریخچه کندل‌ها روی GitHub انجام نخواهد شد."
            )

        if not self.github_repo:
            problems.append(
                "GITHUB_REPO تنظیم نشده است."
            )

        # ----------------------------------------------------
        # Market source
        # ----------------------------------------------------

        if (
            not self.binance_enabled
            and not self.kucoin_enabled
        ):
            problems.append(
                "هر دو منبع Binance و KuCoin غیرفعال هستند."
            )

        if self.preferred_market_source not in (
            "binance",
            "kucoin",
        ):
            problems.append(
                "PREFERRED_MARKET_SOURCE باید binance یا kucoin باشد."
            )

        # ----------------------------------------------------
        # Candle history
        # ----------------------------------------------------

        if self.candle_history_limit <= 0:
            problems.append(
                "CANDLE_HISTORY_LIMIT باید مثبت باشد."
            )

        if self.candle_history_limit < 864:
            problems.append(
                "CANDLE_HISTORY_LIMIT کمتر از 864 است؛ "
                "تاریخچه کامل 72 ساعت ذخیره نخواهد شد."
            )

        if self.candle_interval != "5m":
            problems.append(
                "CANDLE_INTERVAL باید 5m باشد."
            )

        if (
            self.volume_baseline_candles <= 0
            or self.volume_baseline_candles
            >= self.candle_history_limit
        ):
            problems.append(
                "VOLUME_BASELINE_CANDLES باید مثبت و کمتر از "
                "CANDLE_HISTORY_LIMIT باشد."
            )

        if (
            self.pump_history_candles <= 0
            or self.pump_history_candles
            > self.candle_history_limit
        ):
            problems.append(
                "PUMP_HISTORY_CANDLES باید بین 1 و "
                "CANDLE_HISTORY_LIMIT باشد."
            )

        # ----------------------------------------------------
        # Volume
        # ----------------------------------------------------

        if self.volume_signal_multiplier < 1.0:
            problems.append(
                "VOLUME_SIGNAL_MULTIPLIER نباید کمتر از 1 باشد."
            )

        if self.volume_spike_ratio < 1.0:
            problems.append(
                "VOLUME_SPIKE_RATIO نباید کمتر از 1 باشد."
            )

        # ----------------------------------------------------
        # Price
        # ----------------------------------------------------

        if self.price_pump_min < 0:
            problems.append(
                "PRICE_PUMP_MIN نمی‌تواند منفی باشد."
            )

        if self.price_pump_min >= self.price_pump_max:
            problems.append(
                "PRICE_PUMP_MIN باید کوچکتر از PRICE_PUMP_MAX باشد."
            )

        # ----------------------------------------------------
        # Z-score
        # ----------------------------------------------------

        if self.pump_zscore_threshold <= 0:
            problems.append(
                "PUMP_ZSCORE_THRESHOLD باید مثبت باشد."
            )

        if (
            self.pump_min_history_candles
            > self.pump_history_candles
        ):
            problems.append(
                "PUMP_MIN_HISTORY_CANDLES نمی‌تواند بیشتر از "
                "PUMP_HISTORY_CANDLES باشد."
            )

        # ----------------------------------------------------
        # Intervals
        # ----------------------------------------------------

        if self.scan_interval_sec <= 0:
            problems.append(
                "SCAN_INTERVAL_SEC باید مثبت باشد."
            )

        if self.whale_scan_interval_sec <= 0:
            problems.append(
                "WHALE_SCAN_INTERVAL_SEC باید مثبت باشد."
            )

        if self.alert_cooldown_sec < 0:
            problems.append(
                "ALERT_COOLDOWN_SEC نمی‌تواند منفی باشد."
            )

        # ----------------------------------------------------
        # GitHub sync
        # ----------------------------------------------------

        if self.github_candle_sync_interval_sec <= 0:
            problems.append(
                "GITHUB_CANDLE_SYNC_INTERVAL_SEC باید مثبت باشد."
            )

        if self.github_http_timeout_sec <= 0:
            problems.append(
                "GITHUB_HTTP_TIMEOUT_SEC باید مثبت باشد."
            )

        if self.github_max_retries < 0:
            problems.append(
                "GITHUB_MAX_RETRIES نمی‌تواند منفی باشد."
            )

        # ----------------------------------------------------
        # On-chain
        # ----------------------------------------------------

        if not self.etherscan_api_key:
            problems.append(
                "ETHERSCAN_API_KEY تنظیم نشده — "
                "ردیابی Exchange Flow اتریوم غیرفعال خواهد بود."
            )

        if not self.coingecko_api_key:
            problems.append(
                "COINGECKO_API_KEY تنظیم نشده — "
                "قیمت‌گذاری برخی تراکنش‌های آن‌چین ممکن است "
                "با rate limit مواجه شود."
            )

        return problems


# ============================================================
# Global settings instance
# ============================================================

settings = Settings()