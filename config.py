"""
Centralized, validated configuration for the Smart Money Bot.

CEX volume logic:
  * 48 candles  = 4 hours     -> Smart Money / volume baseline
  * 864 candles = 72 hours    -> Pump / Dump statistical baseline

The current 5-minute candle is NOT normalized or extrapolated.
Its actual accumulated quote volume is compared directly with the
historical completed-candle baseline. This means a signal can fire
at any point during the current 5-minute candle as soon as the
configured conditions are satisfied.
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
    return _env_str(key, str(default)).lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# ----------------------------------------------------------------------
# Exchange wallets
# ----------------------------------------------------------------------

DEFAULT_EXCHANGE_WALLETS: Dict[str, Dict[str, str]] = {
    "ETH": {
        "0x28c6c06298d514db089934071355e5743bf21d60": "Binance 14",
        "0xf977814e90da44bfa03b6295a0616a897441acec": "Binance Hot Wallet 20",
    },
    "BSC": {
        # Add verified BscScan-labelled exchange wallets if required.
    },
    "TRON": {
        # Add verified Tronscan-labelled exchange wallets if required.
    },
}


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:

    # ==============================================================
    # Telegram
    # ==============================================================

    bot_token: str = field(
        default_factory=lambda: _env_str("BOT_TOKEN")
    )

    chat_id: str = field(
        default_factory=lambda: _env_str("CHAT_ID")
    )

    # ==============================================================
    # Access control
    # ==============================================================

    bot_access_password: str = field(
        default_factory=lambda: _env_str("BOT_ACCESS_PASSWORD")
    )

    admin_chat_id: str = field(
        default_factory=lambda: _env_str("ADMIN_CHAT_ID")
    )

    developer_name: str = field(
        default_factory=lambda: _env_str(
            "DEVELOPER_NAME",
            "ناصر رومی‌پور",
        )
    )

    default_access_duration_days: float = field(
        default_factory=lambda: _env_float(
            "DEFAULT_ACCESS_DURATION_DAYS",
            30,
        )
    )

    auth_state_file_path: str = field(
        default_factory=lambda: _env_str(
            "AUTH_STATE_FILE_PATH",
            "authorized_users.json",
        )
    )

    github_gist_id: str = field(
        default_factory=lambda: _env_str("GITHUB_GIST_ID")
    )

    github_gist_token: str = field(
        default_factory=lambda: _env_str("GITHUB_GIST_TOKEN")
    )

    telegram_webhook_secret: str = field(
        default_factory=lambda: _env_str(
            "TELEGRAM_WEBHOOK_SECRET"
        )
    )

    # ==============================================================
    # CEX ticker / smart-money detection
    # ==============================================================

    # حداقل مبلغ خالص حجم اضافه‌شده در چرخه جاری.
    #
    # مثال:
    # اگر حجم فعلی نسبت به snapshot قبلی 80,000 دلار بیشتر شده باشد
    # و این مقدار از MIN_INFLOW_USD_5M بیشتر باشد، شرط مبلغ برقرار است.
    min_inflow_usd_5m: float = field(
        default_factory=lambda: _env_float(
            "MIN_INFLOW_USD_5M",
            50_000,
        )
    )

    # چند برابر شدن حجم نسبت به baseline چهار ساعته.
    #
    # Smart Money:
    # current_volume_delta >= baseline_4h * volume_spike_ratio
    #
    # مقدار 2.5 یعنی حداقل 2.5 برابر baseline.
    volume_spike_ratio: float = field(
        default_factory=lambda: _env_float(
            "VOLUME_SPIKE_RATIO",
            2.5,
        )
    )

    # --------------------------------------------------------------
    # Smart Money baseline
    # --------------------------------------------------------------
    #
    # 48 × 5min = 4 hours
    #
    # این تاریخچه برای تشخیص ورود/خروج پول هوشمند استفاده می‌شود.
    #
    smart_money_history_candles: int = field(
        default_factory=lambda: _env_int(
            "SMART_MONEY_HISTORY_CANDLES",
            48,
        )
    )

    # --------------------------------------------------------------
    # Pump / Dump price thresholds
    # --------------------------------------------------------------

    price_pump_min: float = field(
        default_factory=lambda: _env_float(
            "PRICE_PUMP_MIN",
            1.0,
        )
    )

    price_pump_max: float = field(
        default_factory=lambda: _env_float(
            "PRICE_PUMP_MAX",
            8.0,
        )
    )

    # --------------------------------------------------------------
    # Pump / Dump statistical history
    # --------------------------------------------------------------
    #
    # 864 × 5min = 72 hours
    #
    # این تاریخچه فقط برای تشخیص رفتار غیرعادی قیمت استفاده می‌شود.
    #
    pump_dump_history_candles: int = field(
        default_factory=lambda: _env_int(
            "PUMP_DUMP_HISTORY_CANDLES",
            864,
        )
    )

    pump_zscore_enabled: bool = field(
        default_factory=lambda: _env_bool(
            "PUMP_ZSCORE_ENABLED",
            True,
        )
    )

    pump_zscore_threshold: float = field(
        default_factory=lambda: _env_float(
            "PUMP_ZSCORE_THRESHOLD",
            3.0,
        )
    )

    # --------------------------------------------------------------
    # Alert control
    # --------------------------------------------------------------

    alert_cooldown_sec: int = field(
        default_factory=lambda: _env_int(
            "ALERT_COOLDOWN_SEC",
            1800,
        )
    )

    scan_interval_sec: int = field(
        default_factory=lambda: _env_int(
            "SCAN_INTERVAL_SEC",
            300,
        )
    )

    # ==============================================================
    # Candle storage
    # ==============================================================

    # 72 hours × 12 candles/hour = 864 candles.
    #
    # این مقدار ظرفیت اصلی rolling candle storage است.
    history_window: int = field(
        default_factory=lambda: _env_int(
            "HISTORY_WINDOW",
            864,
        )
    )

    candle_interval_minutes: int = field(
        default_factory=lambda: _env_int(
            "CANDLE_INTERVAL_MINUTES",
            5,
        )
    )

    candle_storage_enabled: bool = field(
        default_factory=lambda: _env_bool(
            "CANDLE_STORAGE_ENABLED",
            True,
        )
    )

    candle_storage_file_path: str = field(
        default_factory=lambda: _env_str(
            "CANDLE_STORAGE_FILE_PATH",
            "market_candles.json",
        )
    )

    # --------------------------------------------------------------
    # Historical bootstrap
    # --------------------------------------------------------------
    #
    # تعداد کندل‌هایی که هنگام bootstrap از Binance درخواست می‌شود.
    #
    # 864 = دقیقاً 72 ساعت.
    #
    bootstrap_candle_limit: int = field(
        default_factory=lambda: _env_int(
            "BOOTSTRAP_CANDLE_LIMIT",
            864,
        )
    )

    # چند وقت یک‌بار تاریخچه روی disk/GitHub ذخیره شود.
    candle_storage_save_interval_sec: int = field(
        default_factory=lambda: _env_int(
            "CANDLE_STORAGE_SAVE_INTERVAL_SEC",
            300,
        )
    )

    # ==============================================================
    # Status / housekeeping
    # ==============================================================

    send_status_report: bool = field(
        default_factory=lambda: _env_bool(
            "SEND_STATUS_REPORT",
            True,
        )
    )

    auto_delete_delay_sec: int = field(
        default_factory=lambda: _env_int(
            "AUTO_DELETE_DELAY_SEC",
            300,
        )
    )

    # ==============================================================
    # On-chain exchange-flow tracking
    # ==============================================================

    etherscan_api_key: str = field(
        default_factory=lambda: _env_str(
            "ETHERSCAN_API_KEY"
        )
    )

    bscscan_api_key: str = field(
        default_factory=lambda: _env_str(
            "BSCSCAN_API_KEY"
        )
    )

    tron_api_key: str = field(
        default_factory=lambda: _env_str(
            "TRON_API_KEY"
        )
    )

    whale_min_usd: float = field(
        default_factory=lambda: _env_float(
            "WHALE_MIN_USD",
            500_000,
        )
    )

    whale_scan_interval_sec: int = field(
        default_factory=lambda: _env_int(
            "WHALE_SCAN_INTERVAL_SEC",
            120,
        )
    )

    whale_cooldown_sec: int = field(
        default_factory=lambda: _env_int(
            "WHALE_COOLDOWN_SEC",
            900,
        )
    )

    coingecko_api_key: str = field(
        default_factory=lambda: _env_str(
            "COINGECKO_API_KEY"
        )
    )

    exchange_wallets: Dict[str, Dict[str, str]] = field(
        default_factory=lambda: _load_exchange_wallets()
    )

    # ==============================================================
    # Persistence
    # ==============================================================

    state_file_path: str = field(
        default_factory=lambda: _env_str(
            "STATE_FILE_PATH",
            "bot_state.json",
        )
    )

    state_save_interval_sec: int = field(
        default_factory=lambda: _env_int(
            "STATE_SAVE_INTERVAL_SEC",
            60,
        )
    )

    # ==============================================================
    # HTTP
    # ==============================================================

    http_timeout_sec: int = field(
        default_factory=lambda: _env_int(
            "HTTP_TIMEOUT_SEC",
            10,
        )
    )

    http_max_retries: int = field(
        default_factory=lambda: _env_int(
            "HTTP_MAX_RETRIES",
            3,
        )
    )

    # ==============================================================
    # Derived values
    # ==============================================================

    @property
    def admin_chat_id_resolved(self) -> str:
        return self.admin_chat_id or self.chat_id

    @property
    def history_window_hours(self) -> float:
        return (
            self.history_window
            * self.candle_interval_minutes
            / 60
        )

    @property
    def smart_money_history_hours(self) -> float:
        return (
            self.smart_money_history_candles
            * self.candle_interval_minutes
            / 60
        )

    @property
    def pump_dump_history_hours(self) -> float:
        return (
            self.pump_dump_history_candles
            * self.candle_interval_minutes
            / 60
        )

    # ==============================================================
    # Validation
    # ==============================================================

    def validate(self) -> List[str]:
        problems: List[str] = []

        if not self.bot_token:
            problems.append(
                "BOT_TOKEN تنظیم نشده است — ارسال پیام غیرممکن خواهد بود."
            )

        if not self.chat_id:
            problems.append(
                "CHAT_ID تنظیم نشده است — ارسال پیام غیرممکن خواهد بود."
            )

        if not self.bot_access_password:
            problems.append(
                "BOT_ACCESS_PASSWORD تنظیم نشده — هر کسی که chat_id "
                "ربات را پیدا کند می‌تواند بدون رمز درخواست دسترسی بدهد."
            )

        if not self.telegram_webhook_secret:
            problems.append(
                "TELEGRAM_WEBHOOK_SECRET تنظیم نشده — وبهوک بدون "
                "این مقدار قابل جعل است."
            )

        if not (self.github_gist_id and self.github_gist_token):
            problems.append(
                "GITHUB_GIST_ID/GITHUB_GIST_TOKEN تنظیم نشده — "
                "لیست کاربران مجاز فقط روی دیسک نگه داشته می‌شود."
            )

        if self.price_pump_min >= self.price_pump_max:
            problems.append(
                "PRICE_PUMP_MIN باید کوچکتر از PRICE_PUMP_MAX باشد."
            )

        if self.scan_interval_sec <= 0:
            problems.append(
                "SCAN_INTERVAL_SEC باید مثبت باشد."
            )

        if self.smart_money_history_candles < 10:
            problems.append(
                "SMART_MONEY_HISTORY_CANDLES نباید کمتر از 10 باشد."
            )

        if self.pump_dump_history_candles < 50:
            problems.append(
                "PUMP_DUMP_HISTORY_CANDLES برای تحلیل آماری خیلی کوچک است."
            )

        if self.history_window < self.pump_dump_history_candles:
            problems.append(
                "HISTORY_WINDOW باید حداقل به اندازه "
                "PUMP_DUMP_HISTORY_CANDLES باشد."
            )

        if self.candle_interval_minutes <= 0:
            problems.append(
                "CANDLE_INTERVAL_MINUTES باید مثبت باشد."
            )

        if self.volume_spike_ratio <= 0:
            problems.append(
                "VOLUME_SPIKE_RATIO باید بزرگ‌تر از صفر باشد."
            )

        if self.min_inflow_usd_5m < 0:
            problems.append(
                "MIN_INFLOW_USD_5M نمی‌تواند منفی باشد."
            )

        if self.alert_cooldown_sec < 0:
            problems.append(
                "ALERT_COOLDOWN_SEC نمی‌تواند منفی باشد."
            )

        if not self.etherscan_api_key:
            problems.append(
                "ETHERSCAN_API_KEY تنظیم نشده — ماژول ردیابی "
                "ولت/صرافی Ethereum غیرفعال می‌ماند."
            )

        if not self.coingecko_api_key:
            problems.append(
                "COINGECKO_API_KEY تنظیم نشده — قیمت‌گذاری آن‌چین "
                "ممکن است روی IPهای مشترک rate-limit شود."
            )

        return problems


# ----------------------------------------------------------------------
# Exchange wallet loader
# ----------------------------------------------------------------------

def _load_exchange_wallets() -> Dict[str, Dict[str, str]]:
    """
    Allow overriding/extending the wallet watch-list via:

    EXCHANGE_WALLETS_JSON

    Example:

    {
        "ETH": {
            "0xabc...": "Some Exchange"
        },
        "BSC": {
            "0xdef...": "Some Exchange"
        }
    }
    """

    raw = os.environ.get(
        "EXCHANGE_WALLETS_JSON",
        "",
    ).strip()

    if not raw:
        return DEFAULT_EXCHANGE_WALLETS

    try:
        parsed = json.loads(raw)

        merged = {
            chain: dict(addrs)
            for chain, addrs in DEFAULT_EXCHANGE_WALLETS.items()
        }

        if not isinstance(parsed, dict):
            return DEFAULT_EXCHANGE_WALLETS

        for chain, addrs in parsed.items():
            if not isinstance(addrs, dict):
                continue

            merged.setdefault(chain, {}).update(addrs)

        return merged

    except (json.JSONDecodeError, AttributeError, TypeError):
        return DEFAULT_EXCHANGE_WALLETS


# ----------------------------------------------------------------------
# Global settings instance
# ----------------------------------------------------------------------

settings = Settings()