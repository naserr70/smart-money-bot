"""
Smart Money Bot v2 — entry point.

Two fully independent background loops run side by side — each produces
and sends its own signals without depending on the other:

  1. Market loop   (every SCAN_INTERVAL_SEC):
     CEX ticker volume/price spike + statistical pump detection.

  2. Whale loop     (every WHALE_SCAN_INTERVAL_SEC):
     On-chain exchange-wallet inflow/outflow detection (ETH/BSC/TRON).
     Skipped automatically (with a one-time log message) if not configured.

Alerts from both loops are broadcast to the admin (ADMIN_CHAT_ID / CHAT_ID)
plus every currently-authorized user (see access_control.py) — access is
password-gated via a Telegram webhook (see /telegram/webhook below); the
admin can grant a specific user access for a specific number of days via
the /grant command sent to the bot.
"""
import logging
import os
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, jsonify, request

import bot_commands
from access_control import AccessControl
from config import settings
from exchange_flow import ExchangeFlowTracker
from market_analyzer import MarketAnalyzer
from state import BotState
from telegram_notifier import TelegramNotifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("smart_money_bot")

app = Flask(__name__)


def build_http_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=settings.http_max_retries,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update({"User-Agent": "SmartMoneyBot/2.0"})
    return session


http_session = build_http_session()
state = BotState(history_window=settings.history_window, state_file_path=settings.state_file_path)
access = AccessControl(settings.auth_state_file_path, settings.admin_chat_id_resolved)
notifier = TelegramNotifier(
    settings.bot_token, settings.chat_id,
    timeout=settings.http_timeout_sec, max_retries=settings.http_max_retries,
)
market_analyzer = MarketAnalyzer(settings, state, http_session)
whale_tracker = ExchangeFlowTracker(settings, state, http_session)


def broadcast_targets():
    """Admin + every currently-authorized, non-expired user, deduped."""
    admin = settings.admin_chat_id_resolved
    targets = [admin] if admin else []
    targets.extend(access.active_chat_ids())
    return list(dict.fromkeys(t for t in targets if t))


def market_loop():
    time.sleep(3)
    notifier.broadcast(
        f"🚀 *سیستم تحلیل هوشمند فوق‌پایدار فعال شد.*\n"
        f"👨‍💻 *توسعه‌دهنده:* {settings.developer_name}\n"
        f"پوشش جامع تمام بازارهای ریالی و تتری نوبیتکس + ردیابی آن‌چین کیف‌پول صرافی‌ها.",
        broadcast_targets(),
    )
    while True:
        try:
            signals, data_source, scanned = market_analyzer.run_cycle()
            targets = broadcast_targets()
            if signals:
                notifier.broadcast_chunked([s.to_telegram() for s in signals], targets)

            if settings.send_status_report:
                inflow_n = sum(1 for s in signals if s.direction.value == "inflow")
                outflow_n = sum(1 for s in signals if s.direction.value == "outflow")
                status_msg = market_analyzer.build_status_message(data_source, scanned, inflow_n, outflow_n)
                for target in targets:
                    notifier.send_temporary(status_msg, settings.auto_delete_delay_sec, chat_id=target)
            state.record_market_cycle()
        except Exception as e:
            log.exception("خطای بحرانی در چرخه تحلیل مارکت")
            state.record_market_cycle(error=str(e))
            # Technical error messages go to the admin only, not every user.
            notifier.send_temporary(
                f"⚠️ خطا در چرخه تحلیل مارکت رخ داد: `{e}`\nسیستم به کار خود ادامه می‌دهد.",
                settings.auto_delete_delay_sec,
                chat_id=settings.admin_chat_id_resolved,
            )
        time.sleep(settings.scan_interval_sec)


def whale_loop():
    if not whale_tracker.is_enabled():
        log.warning(
            "ماژول ردیابی ولت/صرافی غیرفعال است (ETHERSCAN_API_KEY یا لیست ولت‌ها تنظیم نشده). "
            "این حلقه اجرا نخواهد شد."
        )
        return

    time.sleep(8)
    while True:
        try:
            signals = whale_tracker.scan()
            if signals:
                notifier.broadcast_chunked([s.to_telegram() for s in signals], broadcast_targets())
            state.record_whale_cycle()
        except Exception as e:
            log.exception("خطای بحرانی در چرخه ردیابی آن‌چین")
            state.record_whale_cycle(error=str(e))
        time.sleep(settings.whale_scan_interval_sec)


def register_telegram_webhook():
    """Point Telegram at this service's /telegram/webhook route. Render sets
    RENDER_EXTERNAL_URL automatically; without it (e.g. local dev) this is
    skipped — set the webhook manually in that case."""
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not base_url or not settings.bot_token:
        log.warning("RENDER_EXTERNAL_URL یا BOT_TOKEN تنظیم نشده؛ webhook خودکار ثبت نشد.")
        return
    webhook_url = f"{base_url}/telegram/webhook"
    payload = {"url": webhook_url}
    if settings.telegram_webhook_secret:
        payload["secret_token"] = settings.telegram_webhook_secret
    try:
        res = http_session.post(
            f"https://api.telegram.org/bot{settings.bot_token}/setWebhook",
            json=payload,
            timeout=settings.http_timeout_sec,
        )
        if res.status_code == 200 and res.json().get("ok"):
            log.info(f"وبهوک تلگرام روی {webhook_url} ثبت شد.")
        else:
            log.warning(f"ثبت وبهوک تلگرام ناموفق بود: {res.text[:300]}")
    except requests.RequestException as e:
        log.warning(f"ثبت وبهوک تلگرام ناموفق بود: {e}")


def start_background_threads():
    for problem in settings.validate():
        log.warning(problem)

    register_telegram_webhook()
    threading.Thread(target=market_loop, daemon=True, name="market-loop").start()
    threading.Thread(target=whale_loop, daemon=True, name="whale-loop").start()
    state.start_autosave(settings.state_save_interval_sec)


start_background_threads()


@app.route("/")
def health_check():
    return "Smart Money Bot v2 — Market ticker + on-chain exchange-wallet tracking active.", 200


@app.route("/status")
def status():
    health = state.snapshot_health()
    health["whale_tracker_enabled"] = whale_tracker.is_enabled()
    health["authorized_users_count"] = len(access.active_chat_ids())
    return jsonify(health)


def build_admin_status_text() -> str:
    health = state.snapshot_health()
    lines = [
        "📈 *وضعیت ربات*\n",
        f"🕐 آخرین چرخه‌ی مارکت: `{health.get('last_market_cycle_at') or '-'}`",
        f"🐋 آخرین چرخه‌ی نهنگ: `{health.get('last_whale_cycle_at') or '-'}`",
        f"🔁 تعداد چرخه‌های مارکت: `{health.get('market_cycles_completed')}`",
        f"🔁 تعداد چرخه‌های نهنگ: `{health.get('whale_cycles_completed')}`",
        f"🐋 ماژول نهنگ فعال: `{'بله' if whale_tracker.is_enabled() else 'خیر'}`",
        f"👥 کاربران مجاز فعال: `{len(access.active_chat_ids())}`",
    ]
    if health.get("last_error"):
        lines.append(f"⚠️ آخرین خطا: `{health.get('last_error')}`")
    return "\n".join(lines)


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    # Telegram sends this header back exactly as registered via setWebhook's
    # secret_token — validating it is what stops a random POST to this
    # public URL from injecting fake commands (e.g. a forged /grant).
    if settings.telegram_webhook_secret:
        incoming = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if incoming != settings.telegram_webhook_secret:
            log.warning("درخواست webhook با secret token نامعتبر رد شد.")
            return "forbidden", 403

    update = request.get_json(silent=True) or {}
    try:
        bot_commands.handle_update(update, settings, access, notifier,
                                   admin_status_provider=build_admin_status_text)
    except Exception:
        log.exception("خطا در پردازش پیام ورودی تلگرام")
    return "ok", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
