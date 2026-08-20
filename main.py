"""
Smart Money Bot v2 — entry point.

Architecture
------------
Two independent background loops:

1. Market loop
   - Binance is preferred.
   - KuCoin is used as fallback when Binance is unavailable.
   - Binance and KuCoin candle histories are NEVER mixed.
   - Each exchange maintains its own 5-minute candle history.
   - 48 closed candles are used for smart-money / volume-flow detection.
   - Up to 864 closed candles (72 hours) are retained for pump/dump analysis.
   - The currently open candle is kept separately by CandleStore.
   - Every cycle fetches a few recent candles and advances the rolling window.

2. Whale loop
   - Independent on-chain exchange-wallet flow detection.
   - ETH / BSC / TRON.

Persistent candle history
--------------------------
CandleStore keeps:

    market_history/
        binance/
            BTCUSDT.json
            ...
        kucoin/
            BTCUSDT.json
            ...

The two sources are completely isolated.

GitHub persistence
------------------
Local dirty candle files are saved every cycle.
When GITHUB_TOKEN + GITHUB_REPO are set, GitHubCandleBackup
periodically uploads changed files in a single Trees API commit.
"""

import logging
import os
import threading
import time
import uuid

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, jsonify, request

import bot_commands
from access_control import AccessControl
from candle_store import CandleStore
from config import settings
from exchange_flow import ExchangeFlowTracker
from formatting import esc
from github_candle_backup import GitHubCandleBackup
from market_analyzer import MarketAnalyzer
from state import BotState
from telegram_notifier import TelegramNotifier


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

log = logging.getLogger("smart_money_bot")


# ============================================================
# Process identity
# ============================================================

INSTANCE_ID = uuid.uuid4().hex[:8]

log.info(
    "SMART MONEY BOT START | instance_id=%s",
    INSTANCE_ID,
)


# ============================================================
# Flask
# ============================================================

app = Flask(__name__)


# ============================================================
# HTTP session
# ============================================================

def build_http_session() -> requests.Session:
    """
    Shared HTTP session.

    Retries are deliberately limited. Binance 418 must NOT be hammered
    repeatedly because that can make rate-limit / IP restrictions worse.
    """

    session = requests.Session()

    retries = Retry(
        total=settings.http_max_retries,
        connect=settings.http_max_retries,
        read=settings.http_max_retries,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        max_retries=retries,
        pool_connections=20,
        pool_maxsize=20,
    )

    session.mount("https://", adapter)

    session.headers.update(
        {
            "User-Agent": "SmartMoneyBot/2.0",
            "Accept": "application/json",
        }
    )

    return session


http_session = build_http_session()


# ============================================================
# Persistent bot state
# ============================================================

state = BotState(
    history_window=settings.history_window,
    state_file_path=settings.state_file_path,
)


# ============================================================
# Access control
# ============================================================

access = AccessControl(
    settings.auth_state_file_path,
    settings.admin_chat_id_resolved,
    gist_id=settings.github_gist_id,
    gist_token=settings.github_gist_token,
    http_session=http_session,
)


# ============================================================
# Telegram notifier
# ============================================================

notifier = TelegramNotifier(
    settings.bot_token,
    settings.chat_id,
    timeout=settings.http_timeout_sec,
    max_retries=settings.http_max_retries,
)


# ============================================================
# Candle Store
# ============================================================

candle_store = CandleStore(
    root_path=settings.candle_store_path,
    max_candles=settings.history_candle_limit,
    github_enabled=settings.github_candle_store_enabled,
    github_token=settings.github_candle_store_token,
    github_repo=settings.github_candle_store_repo,
    github_branch=settings.github_candle_store_branch,
    github_sync_interval_sec=settings.github_candle_sync_interval_sec,
)

log.info(
    "CANDLE STORE READY | root=%s | max_candles=%s | github=%s",
    settings.candle_store_path,
    settings.history_candle_limit,
    "enabled" if settings.github_candle_store_enabled else "disabled",
)


# ============================================================
# GitHub candle backup (Trees API, single-commit batches)
# ============================================================

github_backup = GitHubCandleBackup(
    session=http_session,
    repo=settings.github_repo,
    token=settings.github_token,
    branch=settings.github_branch,
    root_path=settings.github_candle_path,
    timeout=settings.github_http_timeout_sec,
    max_retries=settings.github_max_retries,
)

if github_backup.is_configured():
    log.info(
        "GITHUB CANDLE BACKUP CONFIGURED | repo=%s branch=%s",
        settings.github_repo,
        settings.github_branch,
    )
else:
    log.warning(
        "GITHUB CANDLE BACKUP DISABLED | "
        "set GITHUB_TOKEN and GITHUB_REPO to enable"
    )


# ============================================================
# Market analyzer
# ============================================================

market_analyzer = MarketAnalyzer(
    settings=settings,
    state=state,
    session=http_session,
    candle_store=candle_store,
)

log.info("MARKET ANALYZER READY | candle_store=injected")


# ============================================================
# Whale tracker
# ============================================================

whale_tracker = ExchangeFlowTracker(
    settings,
    state,
    http_session,
)


# ============================================================
# Helpers
# ============================================================

def broadcast_targets():
    """
    Admin + every currently-authorized non-expired user.

    Duplicates are removed while preserving order.
    """

    admin = settings.admin_chat_id_resolved

    targets = [admin] if admin else []

    targets.extend(
        access.active_chat_ids()
    )

    return list(
        dict.fromkeys(
            target
            for target in targets
            if target
        )
    )


def queue_dirty_to_github() -> int:
    """
    After local save, queue dirty (just-saved) payloads to GitHub backup.

    Returns number of files newly queued.
    """

    if not github_backup.is_configured():
        return 0

    queued = 0

    for source in ("binance", "kucoin"):
        # Symbols that were dirty before save_dirty cleared them are gone;
        # re-scan in-memory closed histories and queue any that differ.
        # Practical approach: queue every symbol that currently has history
        # and let the backup module hash-dedupe unchanged content.
        try:
            with candle_store._lock:
                symbols = list(
                    candle_store._closed.get(source, {}).keys()
                )
        except Exception:
            symbols = []

        for symbol in symbols:
            try:
                payload = candle_store.to_payload(source, symbol)
                if github_backup.queue(source, symbol, payload):
                    queued += 1
            except Exception:
                log.exception(
                    "GITHUB QUEUE FAILED | source=%s symbol=%s",
                    source,
                    symbol,
                )

    return queued


def save_candle_store():
    """Best-effort local persistence + optional GitHub queue."""

    try:
        candle_store.save_dirty()
    except Exception:
        log.exception("CANDLE STORE SAVE FAILED")
        return

    try:
        queued = queue_dirty_to_github()
        if queued:
            log.info(
                "GITHUB QUEUE | newly_queued=%s pending=%s",
                queued,
                github_backup.pending_count(),
            )
    except Exception:
        log.exception("GITHUB QUEUE STEP FAILED")


def start_candle_store():
    """Start GitHub backup background loop when configured."""

    if github_backup.is_configured():
        try:
            github_backup.start_background_loop(
                interval_sec=settings.github_candle_sync_interval_sec
            )
            log.info(
                "GITHUB CANDLE BACKUP LOOP STARTED | interval=%ss",
                settings.github_candle_sync_interval_sec,
            )
        except Exception:
            log.exception("GITHUB CANDLE BACKUP LOOP START FAILED")
    else:
        log.info("CANDLE STORE BACKGROUND WORKER NOT REQUIRED")


# ============================================================
# Market loop
# ============================================================

def market_loop():
    """
    Main CEX market loop.

    IMPORTANT:
    Binance/KuCoin selection is handled by MarketData/MarketAnalyzer.

    This loop does NOT copy history from one exchange to another.
    """

    time.sleep(3)

    notifier.broadcast(
        f"🚀 <b>سیستم تحلیل هوشمند فعال شد.</b>\n"
        f"👨‍💻 <b>توسعه‌دهنده:</b> {esc(settings.developer_name)}\n"
        f"پوشش بازار + تحلیل حجم + پامپ/دامپ + ردیابی آن‌چین.",
        broadcast_targets(),
    )

    log.info(
        "MARKET LOOP STARTED | interval=%ss",
        settings.scan_interval_sec,
    )

    while True:

        cycle_started = time.time()

        try:

            log.info("MARKET CYCLE START")

            signals, data_source, scanned = (
                market_analyzer.run_cycle()
            )

            targets = broadcast_targets()

            inflow_n = sum(
                1
                for signal in signals
                if signal.direction.value == "inflow"
            )

            outflow_n = sum(
                1
                for signal in signals
                if signal.direction.value == "outflow"
            )

            elapsed = time.time() - cycle_started

            log.info(
                "MARKET CYCLE COMPLETE | "
                "source=%s | scanned=%s | "
                "signals=%s | inflow=%s | outflow=%s | "
                "elapsed=%.2fs",
                data_source,
                scanned,
                len(signals),
                inflow_n,
                outflow_n,
                elapsed,
            )

            if signals:

                log.info(
                    "MARKET SIGNALS BROADCAST | count=%s | targets=%s",
                    len(signals),
                    targets,
                )

                notifier.broadcast_chunked(
                    [
                        signal.to_telegram()
                        for signal in signals
                    ],
                    targets,
                )

            else:

                log.info(
                    "MARKET NO SIGNAL | "
                    "source=%s | scanned=%s | "
                    "reason=NO_CONFIRMED_SIGNAL",
                    data_source,
                    scanned,
                )

            if settings.send_status_report:

                status_msg = (
                    market_analyzer.build_status_message(
                        data_source,
                        scanned,
                        inflow_n,
                        outflow_n,
                    )
                )

                for target in targets:

                    try:

                        notifier.send_temporary(
                            status_msg,
                            settings.auto_delete_delay_sec,
                            chat_id=target,
                        )

                    except Exception:
                        log.exception(
                            "STATUS MESSAGE FAILED | target=%s",
                            target,
                        )

            state.record_market_cycle()
            save_candle_store()

        except Exception as e:

            log.exception("CRITICAL MARKET LOOP ERROR")

            state.record_market_cycle(
                error=str(e)
            )

            admin = settings.admin_chat_id_resolved

            if admin:

                try:

                    notifier.send_temporary(
                        (
                            "⚠️ <b>خطا در چرخه تحلیل مارکت</b>\n\n"
                            f"<code>{esc(str(e))}</code>\n\n"
                            "سیستم به کار خود ادامه می‌دهد."
                        ),
                        settings.auto_delete_delay_sec,
                        chat_id=admin,
                    )

                except Exception:
                    log.exception(
                        "FAILED TO SEND MARKET ERROR TO ADMIN"
                    )

        elapsed = time.time() - cycle_started

        sleep_for = max(
            1,
            settings.scan_interval_sec - elapsed,
        )

        log.info(
            "MARKET CYCLE SLEEP | seconds=%.1f",
            sleep_for,
        )

        time.sleep(sleep_for)


# ============================================================
# Whale loop
# ============================================================

def whale_loop():

    if not whale_tracker.is_enabled():

        log.warning(
            "WHALE TRACKER DISABLED | "
            "ETHERSCAN_API_KEY or exchange wallets not configured"
        )

        return

    time.sleep(8)

    log.info(
        "WHALE LOOP STARTED | interval=%ss",
        settings.whale_scan_interval_sec,
    )

    while True:

        cycle_started = time.time()

        try:

            log.info("WHALE CYCLE START")

            signals = whale_tracker.scan()

            targets = broadcast_targets()

            log.info(
                "WHALE CYCLE COMPLETE | "
                "signals=%s | targets=%s",
                len(signals),
                targets,
            )

            if signals:

                notifier.broadcast_chunked(
                    [
                        signal.to_telegram()
                        for signal in signals
                    ],
                    targets,
                )

            else:

                log.info("WHALE NO SIGNAL")

            state.record_whale_cycle()

        except Exception as e:

            log.exception("CRITICAL WHALE LOOP ERROR")

            state.record_whale_cycle(
                error=str(e)
            )

        elapsed = time.time() - cycle_started

        sleep_for = max(
            1,
            settings.whale_scan_interval_sec - elapsed,
        )

        time.sleep(sleep_for)


# ============================================================
# Telegram webhook
# ============================================================

def register_telegram_webhook():
    """
    Register Telegram webhook automatically on Render.
    """

    base_url = (
        os.environ
        .get("RENDER_EXTERNAL_URL", "")
        .rstrip("/")
    )

    if not base_url or not settings.bot_token:

        log.warning(
            "TELEGRAM WEBHOOK NOT REGISTERED | "
            "RENDER_EXTERNAL_URL or BOT_TOKEN missing"
        )

        return

    webhook_url = (
        f"{base_url}/telegram/webhook"
    )

    payload = {
        "url": webhook_url
    }

    if settings.telegram_webhook_secret:

        payload["secret_token"] = (
            settings.telegram_webhook_secret
        )

    try:

        response = http_session.post(
            (
                f"https://api.telegram.org/"
                f"bot{settings.bot_token}/setWebhook"
            ),
            json=payload,
            timeout=settings.http_timeout_sec,
        )

        if (
            response.status_code == 200
            and response.json().get("ok")
        ):

            log.info(
                "TELEGRAM WEBHOOK REGISTERED | url=%s",
                webhook_url,
            )

        else:

            log.warning(
                "TELEGRAM WEBHOOK REGISTRATION FAILED | "
                "status=%s | response=%s",
                response.status_code,
                response.text[:300],
            )

    except requests.RequestException as e:

        log.warning(
            "TELEGRAM WEBHOOK REGISTRATION FAILED | error=%s",
            e,
        )


# ============================================================
# Startup
# ============================================================

def start_background_threads():

    log.info(
        "BACKGROUND SERVICES INITIALIZING | instance=%s",
        INSTANCE_ID,
    )

    for problem in settings.validate():

        log.warning(
            "CONFIG WARNING | %s",
            problem,
        )

    register_telegram_webhook()

    start_candle_store()

    if not access.list_users():

        log.warning(
            "AUTHORIZED USERS EMPTY | "
            "admin=%s",
            settings.admin_chat_id_resolved,
        )

        admin_id = settings.admin_chat_id_resolved

        if admin_id:

            try:

                notifier.send(
                    (
                        "⚠️ <b>لیست کاربران مجاز خالی است.</b>\n\n"
                        "اگر قبلاً کاربری را مجاز کرده بودید و بعد از "
                        "ری‌استارت حذف شده، احتمالاً فایل محلی پاک شده است."
                    ),
                    chat_id=admin_id,
                )

            except Exception:
                log.exception(
                    "FAILED TO SEND EMPTY USERS WARNING"
                )

    market_thread = threading.Thread(
        target=market_loop,
        daemon=True,
        name="market-loop",
    )

    market_thread.start()

    log.info("THREAD STARTED | name=market-loop")

    whale_thread = threading.Thread(
        target=whale_loop,
        daemon=True,
        name="whale-loop",
    )

    whale_thread.start()

    log.info("THREAD STARTED | name=whale-loop")

    state.start_autosave(
        settings.state_save_interval_sec
    )

    log.info(
        "BOT STARTUP COMPLETE | instance=%s",
        INSTANCE_ID,
    )


start_background_threads()


# ============================================================
# Health check
# ============================================================

@app.route("/")
def health_check():

    return (
        "Smart Money Bot v2 — "
        "Market ticker + independent Binance/KuCoin "
        "candle history + on-chain tracking active.",
        200,
    )


# ============================================================
# Status API
# ============================================================

@app.route("/status")
def status():

    health = state.snapshot_health()

    health["whale_tracker_enabled"] = (
        whale_tracker.is_enabled()
    )

    health["authorized_users_count"] = (
        len(access.active_chat_ids())
    )

    health["instance_id"] = INSTANCE_ID

    try:

        backup_status = github_backup.status()

        health["candle_store"] = {
            "root_path": settings.candle_store_path,
            "max_candles": settings.history_candle_limit,
            "github_enabled": (
                settings.github_candle_store_enabled
            ),
            "github_backup": backup_status,
        }

    except Exception:

        health["candle_store"] = {
            "status": "unknown"
        }

    return jsonify(health)


# ============================================================
# Admin status
# ============================================================

def build_admin_status_text() -> str:

    health = state.snapshot_health()

    gist_active = bool(
        settings.github_gist_id and settings.github_gist_token
    )

    backup_ok = (
        github_backup.is_configured()
        and github_backup.status().get("last_backup_ok")
    )

    lines = [
        "📈 <b>وضعیت ربات</b>\n",

        (
            f"🆔 instance_id: "
            f"<code>{esc(INSTANCE_ID)}</code>"
        ),

        (
            f"💾 حافظه کاربران: "
            f"<code>{'فعال (Gist)' if gist_active else 'محلی'}</code>"
        ),

        (
            f"🕐 آخرین چرخه مارکت: "
            f"<code>{esc(health.get('last_market_cycle_at') or '-')}</code>"
        ),

        (
            f"🐋 آخرین چرخه نهنگ: "
            f"<code>{esc(health.get('last_whale_cycle_at') or '-')}</code>"
        ),

        (
            f"🔁 تعداد چرخه مارکت: "
            f"<code>{health.get('market_cycles_completed')}</code>"
        ),

        (
            f"🔁 تعداد چرخه نهنگ: "
            f"<code>{health.get('whale_cycles_completed')}</code>"
        ),

        (
            f"🐋 ماژول نهنگ: "
            f"<code>{'فعال' if whale_tracker.is_enabled() else 'غیرفعال'}</code>"
        ),

        (
            f"👥 کاربران فعال: "
            f"<code>{len(access.active_chat_ids())}</code>"
        ),

        (
            f"🕯 ذخیره کندل: "
            f"<code>{esc(str(settings.candle_store_path))}</code>"
        ),

        (
            f"☁️ پشتیبان GitHub کندل: "
            f"<code>{'فعال' if settings.github_candle_store_enabled else 'غیرفعال'}</code>"
        ),

        (
            f"☁️ آخرین بک‌آپ: "
            f"<code>{'موفق' if backup_ok else '—'}</code>"
        ),

        (
            f"📊 سقف تاریخچه: "
            f"<code>{settings.history_candle_limit} کندل 5m</code>"
        ),
    ]

    if health.get("last_error"):

        lines.append(
            (
                f"⚠️ آخرین خطا: "
                f"<code>{esc(health.get('last_error'))}</code>"
            )
        )

    return "\n".join(lines)


# ============================================================
# Telegram webhook endpoint
# ============================================================

@app.route(
    "/telegram/webhook",
    methods=["POST"],
)
def telegram_webhook():

    if settings.telegram_webhook_secret:

        incoming = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            "",
        )

        if (
            incoming
            != settings.telegram_webhook_secret
        ):

            log.warning(
                "TELEGRAM WEBHOOK REJECTED | "
                "invalid secret token"
            )

            return "forbidden", 403

    update = (
        request.get_json(
            silent=True
        )
        or {}
    )

    try:

        bot_commands.handle_update(
            update,
            settings,
            access,
            notifier,
            admin_status_provider=build_admin_status_text,
        )

    except Exception:

        log.exception(
            "TELEGRAM UPDATE PROCESSING ERROR"
        )

    return "ok", 200


# ============================================================
# Graceful-ish shutdown helper
# ============================================================

def shutdown_persistence():

    log.info("PERSISTENCE SHUTDOWN START")

    try:
        save_candle_store()
    except Exception:
        log.exception("FINAL CANDLE STORE SAVE FAILED")

    try:
        if github_backup.is_configured():
            github_backup.backup()
    except Exception:
        log.exception("FINAL GITHUB BACKUP FAILED")

    try:
        state.save()
    except Exception:
        log.exception("FINAL BOT STATE SAVE FAILED")

    log.info("PERSISTENCE SHUTDOWN COMPLETE")


# ============================================================
# Local development
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            8080,
        )
    )

    log.info(
        "FLASK SERVER START | port=%s",
        port,
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
