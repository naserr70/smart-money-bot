"""Production entry point for Smart Money Bot."""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
import uuid

import requests
from flask import Flask, jsonify, request
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import bot_commands
from access_control import AccessControl
from candle_store import CandleStore, VALID_SOURCES
from config import settings
from exchange_flow import ExchangeFlowTracker
from formatting import esc
from github_candle_backup import GitHubCandleBackup
from market_analyzer import MarketAnalyzer
from process_lock import acquire
from state import BotState
from telegram_notifier import TelegramNotifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("smart_money_bot")

INSTANCE_ID = uuid.uuid4().hex[:8]
app = Flask(__name__)


def build_http_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=max(0, settings.http_max_retries),
        connect=max(0, settings.http_max_retries),
        read=max(0, settings.http_max_retries),
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST", "PATCH"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": "SmartMoneyBot/3.0", "Accept": "application/json"})
    return session


http_session = build_http_session()
state = BotState(history_window=settings.history_window, state_file_path=settings.state_file_path)
access = AccessControl(
    settings.auth_state_file_path,
    settings.admin_chat_id_resolved,
    gist_id=settings.github_gist_id,
    gist_token=settings.github_gist_token,
    http_session=http_session,
)
notifier = TelegramNotifier(settings.bot_token, settings.chat_id, timeout=settings.http_timeout_sec, max_retries=settings.http_max_retries)
candle_store = CandleStore(root_path=settings.candle_store_path, max_candles=settings.history_candle_limit)
github_backup = GitHubCandleBackup(
    session=http_session,
    repo=settings.github_repo,
    token=settings.github_token,
    branch=settings.github_branch,
    root_path=settings.github_candle_path,
    timeout=settings.github_http_timeout_sec,
    max_retries=settings.github_max_retries,
)
market_analyzer = MarketAnalyzer(settings, state, http_session, candle_store)
whale_tracker = ExchangeFlowTracker(settings, state, http_session)

_process_lock = None


def broadcast_targets() -> list[str]:
    targets = []
    if settings.admin_chat_id_resolved:
        targets.append(settings.admin_chat_id_resolved)
    targets.extend(access.active_chat_ids())
    return list(dict.fromkeys(t for t in targets if t))


def queue_dirty_to_github() -> int:
    if not github_backup.is_configured() or not settings.github_sync_dirty_only:
        return 0
    queued = 0
    limit = settings.github_max_files_per_sync
    for source in VALID_SOURCES:
        for symbol in candle_store.dirty_symbols(source):
            if limit > 0 and queued >= limit:
                return queued
            try:
                if github_backup.queue(source, symbol, candle_store.to_payload(source, symbol)):
                    queued += 1
            except Exception:
                log.exception("GITHUB QUEUE FAILED | source=%s symbol=%s", source, symbol)
    return queued


def save_candle_store() -> None:
    try:
        queued = queue_dirty_to_github()
        if queued:
            log.info("GITHUB QUEUED | files=%s pending=%s", queued, github_backup.pending_count())
    finally:
        candle_store.save_dirty()


def start_candle_backup() -> None:
    if not github_backup.is_configured():
        return
    github_backup.start_background_loop(settings.github_candle_sync_interval_sec)


def register_telegram_webhook() -> bool:
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not base_url or not settings.bot_token:
        log.warning("TELEGRAM WEBHOOK NOT REGISTERED | RENDER_EXTERNAL_URL/BOT_TOKEN missing")
        return False
    if not settings.telegram_webhook_secret:
        log.error("TELEGRAM WEBHOOK NOT REGISTERED | TELEGRAM_WEBHOOK_SECRET is mandatory")
        return False
    try:
        response = http_session.post(
            f"https://api.telegram.org/bot{settings.bot_token}/setWebhook",
            json={"url": f"{base_url}/telegram/webhook", "secret_token": settings.telegram_webhook_secret},
            timeout=settings.http_timeout_sec,
        )
        data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        ok = response.status_code == 200 and data.get("ok") is True
        if not ok:
            log.error("TELEGRAM WEBHOOK REGISTRATION FAILED | status=%s", response.status_code)
        return ok
    except (requests.RequestException, ValueError):
        log.exception("TELEGRAM WEBHOOK REGISTRATION ERROR")
        return False


def run_startup_history_bootstrap() -> None:
    try:
        stats = market_analyzer.bootstrap_histories(github_backup=github_backup, target_count=settings.candle_history_limit)
        log.info("STARTUP HISTORY BOOTSTRAP | %s", stats)
    except Exception:
        log.exception("STARTUP HISTORY BOOTSTRAP FAILED")
    save_candle_store()
    if github_backup.is_configured():
        github_backup.backup()


def send_startup_announcement_once() -> None:
    key = "bot_startup_announce"
    ttl = 30 * 365 * 24 * 3600
    if state.is_in_cooldown(key, ttl):
        return
    targets = broadcast_targets()
    if not targets:
        return
    try:
        notifier.broadcast(
            "🚀 <b>سیستم تحلیل بازار فعال شد.</b>\n"
            f"👨‍💻 <b>توسعه‌دهنده:</b> {esc(settings.developer_name)}\n"
            "تحلیل حجم/قیمت + پامپ/دامپ + ردیابی جریان صرافی فعال است.",
            targets,
        )
        state.mark_alerted(key)
        state.save()
    except Exception:
        log.exception("STARTUP ANNOUNCEMENT FAILED")


def market_loop() -> None:
    time.sleep(3)
    send_startup_announcement_once()
    run_startup_history_bootstrap()
    while True:
        started = time.time()
        try:
            signals, source, scanned = market_analyzer.run_cycle()
            targets = broadcast_targets()
            if signals:
                notifier.broadcast_chunked([s.to_telegram() for s in signals], targets)
            inflow = sum(s.direction.value == "inflow" for s in signals)
            outflow = sum(s.direction.value == "outflow" for s in signals)
            if settings.send_status_report:
                status = market_analyzer.build_status_message(source, scanned, inflow, outflow)
                for target in targets:
                    notifier.send_temporary(status, settings.auto_delete_delay_sec, chat_id=target)
            state.record_market_cycle()
            save_candle_store()
        except Exception as exc:
            state.record_market_cycle(error=str(exc))
            log.exception("CRITICAL MARKET LOOP ERROR")
        time.sleep(max(1.0, settings.scan_interval_sec - (time.time() - started)))


def whale_loop() -> None:
    if not whale_tracker.is_enabled():
        log.warning("WHALE TRACKER DISABLED")
        return
    time.sleep(8)
    while True:
        started = time.time()
        try:
            signals = whale_tracker.scan()
            if signals:
                notifier.broadcast_chunked([s.to_telegram() for s in signals], broadcast_targets())
            state.record_whale_cycle()
        except Exception as exc:
            state.record_whale_cycle(error=str(exc))
            log.exception("CRITICAL WHALE LOOP ERROR")
        time.sleep(max(1.0, settings.whale_scan_interval_sec - (time.time() - started)))


def build_admin_status_text() -> str:
    health = state.snapshot_health()
    backup = github_backup.status()
    return "\n".join([
        "📈 <b>وضعیت ربات</b>",
        f"🆔 instance: <code>{esc(INSTANCE_ID)}</code>",
        f"🕐 market: <code>{esc(str(health.get('last_market_cycle_at') or '-'))}</code>",
        f"🐋 whale: <code>{esc(str(health.get('last_whale_cycle_at') or '-'))}</code>",
        f"🔁 market cycles: <code>{health.get('market_cycles_completed', 0)}</code>",
        f"🔁 whale cycles: <code>{health.get('whale_cycles_completed', 0)}</code>",
        f"👥 users: <code>{len(access.active_chat_ids())}</code>",
        f"💾 candle history: <code>{settings.history_candle_limit} closed 5m</code>",
        f"☁️ GitHub pending: <code>{backup.get('pending_files', 0)}</code>",
        f"☁️ last backup: <code>{'OK' if backup.get('last_backup_ok') else '—'}</code>",
    ])


def start_background_services() -> None:
    global _process_lock
    for problem in settings.validate():
        log.warning("CONFIG WARNING | %s", problem)
    lock_path = os.environ.get("BOT_PROCESS_LOCK_FILE", "/tmp/smart-money-bot.lock")
    _process_lock = acquire(lock_path)
    if _process_lock is None:
        log.warning("BACKGROUND SERVICES SKIPPED | another process owns %s", lock_path)
        return
    register_telegram_webhook()
    start_candle_backup()
    threading.Thread(target=market_loop, daemon=True, name="market-loop").start()
    threading.Thread(target=whale_loop, daemon=True, name="whale-loop").start()
    state.start_autosave(settings.state_save_interval_sec)
    log.info("BACKGROUND SERVICES STARTED | instance=%s", INSTANCE_ID)


@app.get("/")
def health_check():
    return "Smart Money Bot v3", 200


@app.get("/status")
def public_status():
    # Public endpoint intentionally contains no user count, instance id,
    # repository information or last-error text.
    return jsonify({"status": "ok"})


@app.post("/telegram/webhook")
def telegram_webhook():
    expected = settings.telegram_webhook_secret
    if not expected:
        return "webhook not configured", 503
    incoming = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if incoming != expected:
        log.warning("TELEGRAM WEBHOOK REJECTED | invalid secret")
        return "forbidden", 403
    update = request.get_json(silent=True)
    if not isinstance(update, dict):
        return "bad request", 400
    try:
        bot_commands.handle_update(update, settings, access, notifier, admin_status_provider=build_admin_status_text)
    except Exception:
        log.exception("TELEGRAM UPDATE PROCESSING ERROR")
    return "ok", 200


def shutdown_persistence() -> None:
    try:
        save_candle_store()
    except Exception:
        log.exception("FINAL CANDLE SAVE FAILED")
    try:
        if github_backup.is_configured():
            github_backup.backup()
    except Exception:
        log.exception("FINAL GITHUB BACKUP FAILED")
    try:
        state.save()
    except Exception:
        log.exception("FINAL STATE SAVE FAILED")


atexit.register(shutdown_persistence)
start_background_services()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
