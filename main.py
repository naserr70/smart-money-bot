"""
Smart Money Bot v2 — entry point.

Architecture
------------
Two independent background loops:

1. Market loop
   - Priority: Binance → Bybit → KuCoin.
   - On start: restore from GitHub, else download last 864 5m candles.
   - Persist to local disk + GitHub so Render restarts keep history.
   - 48 closed candles for smart-money / volume-flow detection.
   - Up to 864 closed candles (72 hours) for pump/dump analysis.

2. Whale loop
   - Independent on-chain exchange-wallet flow detection.
   - ETH / BSC / TRON.

Persistent candle history
--------------------------
    market_history/
        binance/
        bybit/
        kucoin/

GitHub persistence
------------------
Local dirty candle files are saved every cycle.
When GITHUB_TOKEN + GITHUB_REPO are set, GitHubCandleBackup
periodically uploads changed files in a single Trees API commit.
"""

import atexit
import collections
import hashlib
import json
import logging
import os
import queue
import signal as os_signal
import sys
import tempfile
import threading
import time
import uuid

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, jsonify, request

import bot_commands
from access_control import AccessControl
from candle_store import CandleStore, VALID_SOURCES
from config import settings
from exchange_flow import ExchangeFlowTracker
from formatting import esc
from github_candle_backup import GitHubCandleBackup
from market_analyzer import MarketAnalyzer
from state import BotState
from telegram_notifier import TelegramNotifier


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

log = logging.getLogger("smart_money_bot")

INSTANCE_ID = uuid.uuid4().hex[:8]

log.info(
    "SMART MONEY BOT START | instance_id=%s",
    INSTANCE_ID,
)

app = Flask(__name__)


def build_http_session() -> requests.Session:

    session = requests.Session()

    # 429 is deliberately NOT retried here: Binance escalates repeated
    # requests after a 429 into an IP ban (418), and MarketDataProvider
    # already applies its own cooldown on 418/429. Retry-After is not
    # honoured by the adapter either, so a long header value can never
    # freeze the market/whale loop inside urllib3.
    retries = Retry(
        total=settings.http_max_retries,
        connect=settings.http_max_retries,
        read=settings.http_max_retries,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
        respect_retry_after_header=False,
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

state = BotState(
    history_window=settings.history_window,
    state_file_path=settings.state_file_path,
)

access = AccessControl(
    settings.auth_state_file_path,
    settings.admin_chat_id_resolved,
    gist_id=settings.github_gist_id,
    gist_token=settings.github_gist_token,
    http_session=http_session,
    github_repo=(
        settings.github_repo
        if settings.users_github_enabled
        else ""
    ),
    github_token=settings.github_token,
    github_branch=settings.github_branch,
    github_path=settings.users_github_path,
    encryption_secret=(
        settings.users_encryption_key
        or settings.bot_token
    ),
)

log.info(
    "USER STORAGE | backend=%s",
    access.storage_label(),
)

notifier = TelegramNotifier(
    settings.bot_token,
    settings.chat_id,
    timeout=settings.http_timeout_sec,
    max_retries=settings.http_max_retries,
)

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

github_backup = GitHubCandleBackup(
    session=http_session,
    repo=settings.github_repo,
    # Without a token the backup is "not configured": no uploads, no
    # restores (see CANDLE_GITHUB_BACKUP in config.py).
    token=(
        settings.github_token
        if settings.candle_github_backup_enabled
        else ""
    ),
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
        "GITHUB CANDLE BACKUP DISABLED | saves Render bandwidth; "
        "set CANDLE_GITHUB_BACKUP=true (+ GITHUB_TOKEN, GITHUB_REPO) to enable"
    )

market_analyzer = MarketAnalyzer(
    settings=settings,
    state=state,
    session=http_session,
    candle_store=candle_store,
)

log.info("MARKET ANALYZER READY | candle_store=injected")

whale_tracker = ExchangeFlowTracker(
    settings,
    state,
    http_session,
)


def _reset_http_pools_after_fork() -> None:
    """
    Under gunicorn --preload (Render's default) this module is imported once
    and then forked into the worker processes. Pooled keep-alive sockets
    created before the fork would be shared by two processes and garble each
    other's HTTP traffic, so every forked child starts with empty pools.
    Closing only drops the child's copy; the parent's connections stay open.
    """

    sessions = (
        http_session,
        notifier.session,
        getattr(
            getattr(whale_tracker, "price_feed", None),
            "session",
            None,
        ),
    )

    for session in sessions:

        if session is None:
            continue

        for adapter in list(session.adapters.values()):

            try:
                adapter.close()
            except Exception:
                pass


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        after_in_child=_reset_http_pools_after_fork,
    )


def broadcast_targets():

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


def send_startup_announcement_once() -> None:

    if access.is_startup_announced():
        log.info(
            "STARTUP ANNOUNCE SKIPPED | already sent previously"
        )
        return

    try:
        notifier.broadcast(
            (
                "🚀 <b>سیستم تحلیل هوشمند فعال شد.</b>\n"
                f"👨‍💻 <b>توسعه‌دهنده:</b> {esc(settings.developer_name)}\n"
                "پوشش بازار + تحلیل حجم + پامپ/دامپ + ردیابی آن‌چین."
            ),
            broadcast_targets(),
        )
        access.mark_startup_announced()
        log.info("STARTUP ANNOUNCE SENT")

    except Exception:
        log.exception("STARTUP ANNOUNCE FAILED")


def queue_dirty_to_github() -> int:

    if not github_backup.is_configured():
        return 0

    queued = 0

    for source in VALID_SOURCES:
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


def run_startup_history_bootstrap() -> None:
    """
    Restore from GitHub when possible, otherwise download last 864
    candles from live exchanges, then persist local + GitHub.
    """

    log.info("STARTUP HISTORY BOOTSTRAP BEGIN")

    try:
        stats = market_analyzer.bootstrap_histories(
            github_backup=github_backup,
            target_count=settings.history_candle_limit,
        )
        log.info("STARTUP HISTORY BOOTSTRAP STATS | %s", stats)
    except Exception:
        log.exception("STARTUP HISTORY BOOTSTRAP FAILED")

    try:
        save_candle_store()
    except Exception:
        log.exception("STARTUP HISTORY LOCAL SAVE FAILED")

    if github_backup.is_configured():
        try:
            ok = github_backup.backup()
            log.info(
                "STARTUP HISTORY GITHUB BACKUP | ok=%s pending=%s",
                ok,
                github_backup.pending_count(),
            )
        except Exception:
            log.exception("STARTUP HISTORY GITHUB BACKUP FAILED")


def market_loop():

    time.sleep(3)

    send_startup_announcement_once()

    # Full 72h window before first analysis cycle.
    run_startup_history_bootstrap()

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

                # Each signal is sent as its own Telegram message
                # (no batching of multiple signals into one message).
                for signal in signals:
                    notifier.broadcast(
                        signal.to_telegram(),
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

            if (
                settings.send_status_report
                and access.is_signal_enabled("status_report")
            ):

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

                # Each whale signal is sent as its own Telegram message.
                for signal in signals:
                    notifier.broadcast(
                        signal.to_telegram(),
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


def register_telegram_webhook():

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


# The signal loops must run in exactly ONE process. With gunicorn --preload
# they run in the master; without it every worker imports this module, and
# each would otherwise start its own loops and send every message N times.
LOOP_LOCK_RETRY_SEC = 30

_loop_owner = {
    "pid": None,
    "lock_handle": None,
}


def _loop_lock_path() -> str:

    digest = hashlib.sha1(
        os.path.abspath(os.getcwd()).encode("utf-8")
    ).hexdigest()[:12]

    return os.path.join(
        tempfile.gettempdir(),
        f"smart_money_bot_loops_{digest}.lock",
    )


def _try_acquire_loop_lock() -> bool:

    try:
        import fcntl
    except ImportError:
        return True  # non-Unix: single-process deployments only

    try:
        handle = open(_loop_lock_path(), "a+")
    except OSError:
        log.exception("LOOP LOCK FILE UNAVAILABLE | running loops anyway")
        return True

    try:
        fcntl.flock(
            handle.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
    except OSError:
        handle.close()
        return False

    # Keep the file open for the life of the process (holds the lock).
    _loop_owner["lock_handle"] = handle

    return True


def _standby_for_loops() -> None:

    while True:

        time.sleep(LOOP_LOCK_RETRY_SEC)

        if _try_acquire_loop_lock():

            log.warning(
                "BACKGROUND LOOPS TAKEN OVER | pid=%s",
                os.getpid(),
            )

            _start_loops()

            return


def start_background_threads():

    if not _try_acquire_loop_lock():

        log.info(
            "BACKGROUND LOOPS RUN IN ANOTHER PROCESS | pid=%s | standby",
            os.getpid(),
        )

        threading.Thread(
            target=_standby_for_loops,
            daemon=True,
            name="loops-standby",
        ).start()

        return

    _start_loops()


def _start_loops():

    _loop_owner["pid"] = os.getpid()

    log.info(
        "BACKGROUND SERVICES INITIALIZING | instance=%s pid=%s",
        INSTANCE_ID,
        os.getpid(),
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


@app.route("/")
def health_check():

    return (
        "Smart Money Bot v2 — "
        "Market ticker + independent Binance/Bybit/KuCoin "
        "candle history + on-chain tracking active.",
        200,
    )


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


def build_admin_status_text() -> str:

    health = state.snapshot_health()

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
            f"<code>{esc(access.storage_status_text())}</code>"
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

    if _already_seen(update):

        log.info(
            "TELEGRAM UPDATE DUPLICATE IGNORED | update_id=%s",
            update.get("update_id"),
        )

        return "ok", 200

    # Answer Telegram at once and handle the update in the background.
    # A slow handler (GitHub save, broadcast) used to risk a worker timeout;
    # Telegram then re-sent the same update and a menu toggle flipped back.
    _submit_update(update)

    return "ok", 200


MAX_REMEMBERED_UPDATES = 2000

_update_lock = threading.Lock()
_seen_update_ids = collections.OrderedDict()
_update_worker = {
    "pid": None,
    "queue": None,
}


def _seen_updates_path() -> str:

    digest = hashlib.sha1(
        os.path.abspath(os.getcwd()).encode("utf-8")
    ).hexdigest()[:12]

    return os.path.join(
        tempfile.gettempdir(),
        f"smart_money_bot_updates_{digest}.json",
    )


def _already_seen_shared(update_id: int) -> bool:
    """
    Check-and-record in a file shared by every gunicorn worker, so a
    re-delivered update is ignored even when it reaches another worker.
    """

    try:
        import fcntl
    except ImportError:
        return False

    try:

        with open(_seen_updates_path(), "a+", encoding="utf-8") as handle:

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

            handle.seek(0)

            try:
                seen = json.loads(handle.read() or "[]")
            except ValueError:
                seen = []

            if not isinstance(seen, list):
                seen = []

            if update_id in seen:
                return True

            seen.append(update_id)

            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(seen[-MAX_REMEMBERED_UPDATES:]))
            handle.flush()

    except OSError:
        log.exception("TELEGRAM UPDATE DEDUPE FILE UNAVAILABLE")

    return False


def _already_seen(update: dict) -> bool:

    update_id = update.get("update_id")

    if not isinstance(update_id, int):
        return False

    with _update_lock:

        if update_id in _seen_update_ids:
            return True

        _seen_update_ids[update_id] = None

        while len(_seen_update_ids) > MAX_REMEMBERED_UPDATES:
            _seen_update_ids.popitem(last=False)

        return _already_seen_shared(update_id)


def _process_update(update: dict) -> None:

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


def _update_worker_loop(pending_updates: "queue.Queue") -> None:

    while True:
        _process_update(pending_updates.get())


def _submit_update(update: dict) -> None:
    """One ordered worker thread per process (forked workers start their own)."""

    with _update_lock:

        if _update_worker["pid"] != os.getpid():

            pending_updates = queue.Queue(maxsize=500)

            threading.Thread(
                target=_update_worker_loop,
                args=(pending_updates,),
                daemon=True,
                name="telegram-updates",
            ).start()

            _update_worker["pid"] = os.getpid()
            _update_worker["queue"] = pending_updates

        pending_updates = _update_worker["queue"]

    try:

        pending_updates.put_nowait(update)

    except queue.Full:

        log.warning("TELEGRAM UPDATE QUEUE FULL | processing inline")

        _process_update(update)


def shutdown_persistence():

    # Only the process that runs the loops owns this data. Forked gunicorn
    # workers inherit this hook but hold stale copies, which must never
    # overwrite the real files on exit.
    if _loop_owner["pid"] != os.getpid():
        return

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


# Flush candle history / GitHub queue / bot state when the process exits
# (e.g. SIGTERM from Render during a redeploy). Previously never called.
atexit.register(shutdown_persistence)


if __name__ == "__main__":

    # Turn SIGTERM into a normal exit so the atexit persistence hook runs
    # when the platform stops the process (gunicorn already does this).
    os_signal.signal(
        os_signal.SIGTERM,
        lambda *_: sys.exit(0),
    )

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
