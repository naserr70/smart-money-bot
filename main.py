"""
Smart Money Bot v2 — entry point.

Two fully independent background loops run side by side, exactly as
requested — each produces and sends its own signals without depending on
the other:

  1. Market loop   (every SCAN_INTERVAL_SEC):
     CEX ticker volume/price spike detection (Binance -> KuCoin fallback).

  2. Whale loop     (every WHALE_SCAN_INTERVAL_SEC):
     On-chain exchange-wallet inflow/outflow detection (Etherscan/BscScan).
     Skipped automatically (with a one-time log message) if no
     ETHERSCAN_API_KEY is configured, rather than crashing the whole bot.
"""
import logging
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, jsonify

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
notifier = TelegramNotifier(
    settings.bot_token, settings.chat_id,
    timeout=settings.http_timeout_sec, max_retries=settings.http_max_retries,
)
market_analyzer = MarketAnalyzer(settings, state, http_session)
whale_tracker = ExchangeFlowTracker(settings, state, http_session)


def market_loop():
    time.sleep(3)
    notifier.send_temporary(
        "🚀 *سیستم تحلیل هوشمند فوق‌پایدار فعال شد.*\n"
        "پوشش جامع تمام بازارهای ریالی و تتری نوبیتکس + ردیابی آن‌چین کیف‌پول صرافی‌ها.",
        settings.auto_delete_delay_sec,
    )
    while True:
        try:
            signals, data_source, scanned = market_analyzer.run_cycle()
            if signals:
                notifier.send_chunked([s.to_telegram() for s in signals])

            if settings.send_status_report:
                inflow_n = sum(1 for s in signals if s.direction.value == "inflow")
                outflow_n = sum(1 for s in signals if s.direction.value == "outflow")
                notifier.send_temporary(
                    market_analyzer.build_status_message(data_source, scanned, inflow_n, outflow_n),
                    settings.auto_delete_delay_sec,
                )
            state.record_market_cycle()
        except Exception as e:
            log.exception("خطای بحرانی در چرخه تحلیل مارکت")
            state.record_market_cycle(error=str(e))
            notifier.send_temporary(
                f"⚠️ خطا در چرخه تحلیل مارکت رخ داد: `{e}`\nسیستم به کار خود ادامه می‌دهد.",
                settings.auto_delete_delay_sec,
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
                notifier.send_chunked([s.to_telegram() for s in signals])
            state.record_whale_cycle()
        except Exception as e:
            log.exception("خطای بحرانی در چرخه ردیابی آن‌چین")
            state.record_whale_cycle(error=str(e))
        time.sleep(settings.whale_scan_interval_sec)


def start_background_threads():
    for problem in settings.validate():
        log.warning(problem)

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
    return jsonify(health)


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
