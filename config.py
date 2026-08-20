"""
Centralized, validated configuration for the Smart Money Bot.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List


def _env_str(
    key: str,
    default: str = "",
) -> str:

    return os.environ.get(
        key,
        default,
    ).strip()


def _env_float(
    key: str,
    default: float,
) -> float:

    try:
        return float(
            os.environ.get(
                key,
                default,
            )
        )
    except (
        TypeError,
        ValueError,
    ):
        return default


def _env_int(
    key: str,
    default: int,
) -> int:

    try:
        return int(
            os.environ.get(
                key,
                default,
            )
        )
    except (
        TypeError,
        ValueError,
    ):
        return default


def _env_bool(
    key: str,
    default: bool,
) -> bool:

    return (
        _env_str(
            key,
            str(default),
        ).lower()
        in (
            "1",
            "true",
            "yes",
            "on",
        )
    )


DEFAULT_EXCHANGE_WALLETS: Dict[
    str,
    Dict[str, str],
] = {

    "ETH": {
        "0x28c6c06298d514db089934071355e5743bf21d60":
            "Binance 14",

        "0xf977814e90da44bfa03b6295a0616a897441acec":
            "Binance Hot Wallet 20",
    },

    "BSC": {},

    "TRON": {},
}


@dataclass(frozen=True)
class Settings:

    # =========================================================
    # Telegram
    # =========================================================

    bot_token: str = field(
        default_factory=lambda:
        _env_str("BOT_TOKEN")
    )

    chat_id: str = field(
        default_factory=lambda:
        _env_str("CHAT_ID")
    )

    # =========================================================
    # Access control
    # =========================================================

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
        _env_str(
            "TELEGRAM_WEBHOOK_SECRET"
        )
    )

    # =========================================================
    # MARKET ANALYSIS
    # =========================================================

    # IMPORTANT:
    # MIN_INFLOW_USD_5M intentionally removed.
    #
    # Signal volume filter is now:
    #
    # current closed 5m candle >=
    # 2x previous 48 closed candles average
    #
    # Or a larger configured VOLUME_SPIKE_RATIO.

    volume_spike_ratio: float = field(
        default_factory=lambda:
        _env_float(
            "VOLUME_SPIKE_RATIO",
            2.0,
        )
    )

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

    alert_cooldown_sec: int = field(
        default_factory=lambda:
        _env_int(
            "ALERT_COOLDOWN_SEC",
            1800,
        )
    )

    scan_interval_sec: int = field(
        default_factory=lambda:
        _env_int(
            "SCAN_INTERVAL_SEC",
            300,
        )
    )

    # 864 closed 5m candles = 72 hours.
    history_window: int = field(
        default_factory=lambda:
        _env_int(
            "HISTORY_WINDOW",
            864,
        )
    )

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

    # Separate directory for source-specific candle history.
    candle_history_path: str = field(
        default_factory=lambda:
        _env_str(
            "CANDLE_HISTORY_PATH",
            "market_history",
        )
    )

    # =========================================================
    # Status
    # =========================================================

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

    # =========================================================
    # On-chain
    # =========================================================

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

    exchange_wallets: Dict[
        str,
        Dict[str, str],
    ] = field(
        default_factory=lambda:
        _load_exchange_wallets()
    )

    # =========================================================
    # Persistence
    # =========================================================

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

    # =========================================================
    # HTTP
    # =========================================================

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

    # =========================================================
    # ADMIN
    # =========================================================

    @property
    def admin_chat_id_resolved(self) -> str:

        return (
            self.admin_chat_id
            or self.chat_id
        )

    # =========================================================
    # VALIDATION
    # =========================================================

    def validate(self) -> List[str]:

        problems = []

        if not self.bot_token:

            problems.append(
                "BOT_TOKEN تنظیم نشده است."
            )

        if not self.chat_id:

            problems.append(
                "CHAT_ID تنظیم نشده است."
            )

        if not self.bot_access_password:

            problems.append(
                "BOT_ACCESS_PASSWORD تنظیم نشده است."
            )

        if not self.telegram_webhook_secret:

            problems.append(
                "TELEGRAM_WEBHOOK_SECRET تنظیم نشده است."
            )

        if not (
            self.github_gist_id
            and self.github_gist_token
        ):

            problems.append(
                "GITHUB_GIST_ID/GITHUB_GIST_TOKEN "
                "تنظیم نشده است."
            )

        if self.price_pump_min >= self.price_pump_max:

            problems.append(
                "PRICE_PUMP_MIN باید کوچکتر "
                "از PRICE_PUMP_MAX باشد."
            )

        if self.scan_interval_sec <= 0:

            problems.append(
                "SCAN_INTERVAL_SEC باید مثبت باشد."
            )

        if self.history_window < 864:

            problems.append(
                "HISTORY_WINDOW باید حداقل 864 باشد."
            )

        if self.volume_spike_ratio < 2.0:

            problems.append(
                "VOLUME_SPIKE_RATIO نمی‌تواند کمتر "
                "از 2.0 باشد."
            )

        if not self.etherscan_api_key:

            problems.append(
                "ETHERSCAN_API_KEY تنظیم نشده است."
            )

        if not self.coingecko_api_key:

            problems.append(
                "COINGECKO_API_KEY تنظیم نشده است."
            )

        return problems


def _load_exchange_wallets():

    raw = os.environ.get(
        "EXCHANGE_WALLETS_JSON",
        "",
    ).strip()

    if not raw:
        return DEFAULT_EXCHANGE_WALLETS

    try:

        parsed = json.loads(raw)

        merged = {
            chain: dict(addresses)
            for chain, addresses
            in DEFAULT_EXCHANGE_WALLETS.items()
        }

        for chain, addresses in parsed.items():

            merged.setdefault(
                chain,
                {},
            ).update(addresses)

        return merged

    except (
        json.JSONDecodeError,
        AttributeError,
    ):

        return DEFAULT_EXCHANGE_WALLETS


settings = Settings()