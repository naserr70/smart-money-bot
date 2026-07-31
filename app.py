import os
import time
import threading
import logging
import statistics
from collections import deque
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, jsonify

# ==================== لاگ‌گیری ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("smart_money_bot")

app = Flask(__name__)

# ==================== دریافت امن متغیرها ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()

# ==================== تنظیمات (قابل تغییر با Environment Variables) ====================
MIN_INFLOW_USD_5M = float(os.environ.get("MIN_INFLOW_USD_5M", 50_000))
VOLUME_SPIKE_RATIO = float(os.environ.get("VOLUME_SPIKE_RATIO", 2.5))
PRICE_PUMP_MIN = float(os.environ.get("PRICE_PUMP_MIN", 1.0))
PRICE_PUMP_MAX = float(os.environ.get("PRICE_PUMP_MAX", 8.0))
ALERT_COOLDOWN_SEC = int(os.environ.get("ALERT_COOLDOWN_SEC", 1800))  # ۳۰ دقیقه بین دو هشدار برای یک نماد
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", 300))
SEND_STATUS_REPORT = os.environ.get("SEND_STATUS_REPORT", "true").lower() == "true"
HISTORY_WINDOW = int(os.environ.get("HISTORY_WINDOW", 12))  # تعداد دورهای قبلی برای میانگین حجم واقعی (۱۲ دور = ۱ ساعت)

# 🗺️ لیست کامل و جامع ۲۴۰+ رمزارز بازار نوبیتکس (جفت‌ارزهای معادل USDT در بایننس)
NOBITEX_SYMBOLS = [
    # --- اصلی و محبوب (Major Cryptos) ---
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "AVAXUSDT",
    "TRXUSDT", "DOTUSDT", "LINKUSDT", "SHIBUSDT", "LTCUSDT", "BCHUSDT", "NEARUSDT", "UNIUSDT",
    "ETCUSDT", "XLMUSDT", "STXUSDT", "XMRUSDT", "FILUSDT", "ATOMUSDT", "EGLDUSDT", "ALGOUSDT",
    "VETUSDT", "ICPUSDT", "HBARUSDT", "THETAUSDT", "XTZUSDT", "EOSUSDT", "IOTAUSDT", "NEOUSDT",

    # --- لایه ۱، لایه ۲ و زیرساخت (L1 / L2 & Infrastructure) ---
    "APTUSDT", "SUIUSDT", "ARBUSDT", "OPUSDT", "MATICUSDT", "POLUSDT", "FTMUSDT", "INJUSDT",
    "TIAUSDT", "SEIUSDT", "STRKUSDT", "KASUSDT", "FLOWUSDT", "RONUSDT", "MANTRAUSDT", "MINAUSDT",
    "KAVAUSDT", "ASTRUSDT", "ANKRUSDT", "ROSEUSDT", "ZILUSDT", "IOTXUSDT", "ONEUSDT", "CKBUSDT",
    "GLMRUSDT", "MOVRUSDT", "STRAXUSDT", "KLAYUSDT", "CELOUSDT", "SKLUSDT", "QNTUSDT", "LDOUSDT",

    # --- میم‌کوین‌ها و توکن‌های تلگرامی (Memecoins & Telegram Ecosystem) ---
    "PEPEUSDT", "FLOKIUSDT", "BONKUSDT", "WIFUSDT", "NOTUSDT", "DOGSUSDT", "HMSTRUSDT", "TONUSDT",
    "MEMEUSDT", "PEOPLEUSDT", "BOMEUSDT", "NEIROUSDT", "CATSUSDT", "MAJORUSDT", "PENGUUSDT",
    "POPCATUSDT", "BABYDOGEUSDT", "1000SATSUSDT", "TURBOUSDT", "MYROUSDT", "MEWUSDT", "BRETTUSDT",
    "DEGENUSDT", "SLERFUSDT", "MOGUSDT", "COQUSDT", "SMILEUSDT", "LUNCUSDT", "USTCUSDT",

    # --- اکوسیستم دیفای و ارزهای حقوقی (DeFi & Yield) ---
    "AAVEUSDT", "GRTUSDT", "RUNEUSDT", "DYDXUSDT", "JUPUSDT", "PYTHUSDT", "PENDLEUSDT", "ENAUSDT",
    "ONDOUSDT", "OMUSDT", "RAYUSDT", "ORDIUSDT", "BLURUSDT", "ENSUSDT", "CRVUSDT", "MKRUSDT",
    "SNXUSDT", "COMPUSDT", "1INCHUSDT", "CAKEUSDT", "SUSHIUSDT", "CVXUSDT", "RPLUSDT", "BALUSDT",
    "FXSUSDT", "YFIUSDT", "KNCUSDT", "ZRXUSDT", "ALPHAUSDT", "BADGERUSDT", "REQUSDT",

    # --- هوش مصنوعی، متاورس و گیمینگ (AI, Metaverse & Gaming) ---
    "FETUSDT", "AGIXUSDT", "OCEANUSDT", "RENDERUSDT", "WLDUSDT", "ARKMUSDT", "JTOUSDT", "SANDUSDT",
    "MANAUSDT", "AXSUSDT", "CHZUSDT", "GALAUSDT", "GMTUSDT", "AUDIOUSDT", "SLPUSDT", "ILVUSDT",
    "ALICEUSDT", "MAGICUSDT", "HIGHUSDT", "YGGUSDT", "SUPERUSDT", "PIXELUSDT", "PORTALUSDT",

    # --- سایر نمادهای فعال در بازار نوبیتکس ---
    "JSTUSDT", "SUNUSDT", "LPTUSDT", "WOOUSDT", "HOTUSDT", "DENTUSDT", "RVNUSDT", "SPELLUSDT",
    "UMAUSDT", "IDUSDT", "MAVUSDT", "EDUUSDT", "SFPUSDT", "C98USDT", "TWTUSDT", "MASKUSDT",
    "API3USDT", "BANDUSDT", "TRBUSDT", "RSRUSDT", "STORJUSDT", "ARUSDT", "BNTUSDT", "NMRUSDT",
    "RADUSDT", "OXTUSDT", "BATUSDT", "ENJUSDT", "LRCUSDT", "SYSUSDT", "ZENUSDT", "QTUMUSDT"
]

# ==================== وضعیت داخلی (state) ====================
previous_market_snapshot = {}
volume_history = {}          # symbol -> deque(maxlen=HISTORY_WINDOW) از inflowهای واقعی قبلی
last_alert_time = {}         # symbol -> epoch زمان آخرین هشدار (برای کول‌داون)
bot_state = {
    "last_cycle_at": None,
    "last_error": None,
    "cycles_completed": 0,
    "started_at": datetime.now(timezone.utc).isoformat(),
}
state_lock = threading.Lock()

# ==================== HTTP Session با Retry ====================
def build_session():
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    return session

http_session = build_session()


def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        log.error("BOT_TOKEN یا CHAT_ID تنظیم نشده است.")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
    try:
        res = http_session.post(url, json=payload, timeout=10)
        if res.status_code != 200:
            log.warning(f"ارسال تلگرام ناموفق: {res.status_code} - {res.text[:200]}")
        return res.status_code == 200
    except Exception as e:
        log.error(f"خطا در ارسال پیام تلگرام: {e}")
        return False


def send_telegram_chunked(messages, max_len=3500):
    """چند پیام کوچک را در قالب کمترین تعداد پیام تلگرام (برای جلوگیری از flood) ارسال می‌کند."""
    if not messages:
        return
    buffer = ""
    for msg in messages:
        if len(buffer) + len(msg) + 2 > max_len:
            send_telegram(buffer)
            buffer = msg
        else:
            buffer = f"{buffer}\n\n{msg}" if buffer else msg
    if buffer:
        send_telegram(buffer)


def fetch_binance_ticker_data():
    """دریافت داده‌های زنده جهانی از اندپوینت‌های مستقیم بایننس"""
    endpoints = [
        "https://api1.binance.com/api/v3/ticker/24hr",
        "https://api2.binance.com/api/v3/ticker/24hr",
        "https://api3.binance.com/api/v3/ticker/24hr",
        "https://api.binance.com/api/v3/ticker/24hr",
    ]

    for url in endpoints:
        try:
            res = http_session.get(url, timeout=8)
            if res.status_code == 200:
                data = res.json()
                filtered = {item["symbol"]: item for item in data if item.get("symbol") in NOBITEX_SYMBOLS}
                if filtered:
                    return filtered
                log.warning(f"بایننس {url} پاسخ داد اما هیچ نمادی مچ نشد.")
            else:
                # کد ۴۵۱ یعنی بلاک جغرافیایی (سرورهای آمریکایی مثل Render/AWS-US توسط بایننس مسدودند)
                log.warning(f"بایننس {url} -> HTTP {res.status_code}: {res.text[:150]}")
        except Exception as e:
            log.warning(f"اندپوینت {url} خطا داد: {e}")

    return {}


def fetch_kucoin_ticker_data():
    """
    منبع جایگزین: KuCoin معمولاً روی سرورهای کلاود (از جمله Render) بلاک جغرافیایی بایننس را ندارد.
    خروجی را دقیقاً هم‌شکل خروجی بایننس (symbol -> {lastPrice, quoteVolume, priceChangePercent}) می‌سازد
    تا بقیه‌ی کد بدون تغییر کار کند.
    """
    url = "https://api.kucoin.com/api/v1/market/allTickers"
    try:
        res = http_session.get(url, timeout=10)
        if res.status_code != 200:
            log.warning(f"کوکوین -> HTTP {res.status_code}: {res.text[:150]}")
            return {}

        payload = res.json()
        tickers = payload.get("data", {}).get("ticker", [])
        result = {}
        for t in tickers:
            raw_symbol = t.get("symbol", "")  # مثل "BTC-USDT"
            if not raw_symbol.endswith("-USDT"):
                continue
            binance_style_symbol = raw_symbol.replace("-USDT", "USDT")  # مثل "BTCUSDT"
            if binance_style_symbol not in NOBITEX_SYMBOLS:
                continue

            last_price = t.get("last")
            vol_value = t.get("volValue")  # حجم بر حسب USDT (معادل quoteVolume بایننس)
            change_rate = t.get("changeRate")  # کسری است، مثلاً 0.015 یعنی 1.5%

            if last_price is None or vol_value is None or change_rate is None:
                continue

            result[binance_style_symbol] = {
                "symbol": binance_style_symbol,
                "lastPrice": last_price,
                "quoteVolume": vol_value,
                "priceChangePercent": float(change_rate) * 100,
            }

        return result
    except Exception as e:
        log.warning(f"کوکوین خطا داد: {e}")
        return {}


def fetch_market_data():
    """ابتدا بایننس، در صورت شکست (مثلاً بلاک ۴۵۱ جغرافیایی) به کوکوین سوییچ می‌کند."""
    data = fetch_binance_ticker_data()
    if data:
        return data, "binance"

    log.warning("بایننس در دسترس نبود (به احتمال زیاد بلاک جغرافیایی) — سوییچ به KuCoin.")
    data = fetch_kucoin_ticker_data()
    if data:
        return data, "kucoin"

    return {}, "none"


def is_in_cooldown(symbol):
    last = last_alert_time.get(symbol)
    if last is None:
        return False
    return (time.time() - last) < ALERT_COOLDOWN_SEC


def get_dynamic_baseline(symbol, fallback_avg):
    """
    به‌جای فرض توزیع یکنواخت حجم ۲۴ ساعته، از میانگین واقعی inflowهای قبلی همان نماد
    استفاده می‌کند تا مبنای مقایسه دقیق‌تر و متناسب با رفتار خودِ آن کوین باشد.
    """
    hist = volume_history.get(symbol)
    if hist and len(hist) >= 3:
        return max(statistics.mean(hist), 1.0)
    return max(fallback_avg, 1.0)


def analyze_smart_money():
    global previous_market_snapshot

    binance_stats, data_source = fetch_market_data()
    inflow_signals = []
    outflow_signals = []

    if binance_stats:
        current_snapshot = {}

        for sym, item in binance_stats.items():
            clean_sym = sym.replace("USDT", "")
            try:
                price_usd = float(item.get("lastPrice", 0))
                vol_24h_usd = float(item.get("quoteVolume", 0))
                change_24h = float(item.get("priceChangePercent", 0))
            except (TypeError, ValueError):
                continue

            current_snapshot[clean_sym] = {"price": price_usd, "vol_usd": vol_24h_usd}

            if clean_sym in previous_market_snapshot and price_usd > 0:
                prev_price = previous_market_snapshot[clean_sym]["price"]
                prev_vol = previous_market_snapshot[clean_sym]["vol_usd"]

                if prev_price <= 0:
                    continue

                price_5m_change = ((price_usd - prev_price) / prev_price) * 100
                vol_5m_inflow = vol_24h_usd - prev_vol
                fallback_avg_vol = vol_24h_usd / 288

                # به‌روزرسانی تاریخچه‌ی حجم واقعی این نماد (فقط مقادیر مثبت معنادار)
                if vol_5m_inflow > 0:
                    hist = volume_history.setdefault(clean_sym, deque(maxlen=HISTORY_WINDOW))
                    hist.append(vol_5m_inflow)

                baseline_vol = get_dynamic_baseline(clean_sym, fallback_avg_vol)
                spike_multiplier = (vol_5m_inflow / baseline_vol) if baseline_vol > 0 else 1

                # فیلتر ورود پول هوشمند (Whale Inflow / Pump)
                if (
                    vol_5m_inflow >= (baseline_vol * VOLUME_SPIKE_RATIO)
                    and vol_5m_inflow >= MIN_INFLOW_USD_5M
                    and PRICE_PUMP_MIN <= price_5m_change <= PRICE_PUMP_MAX
                    and not is_in_cooldown(clean_sym)
                ):
                    inflow_signals.append({
                        "symbol": clean_sym, "price": price_usd, "change_5m": price_5m_change,
                        "change_24h": change_24h, "inflow_usd": vol_5m_inflow,
                        "spike_multiplier": spike_multiplier,
                    })
                    last_alert_time[clean_sym] = time.time()

                # فیلتر خروج پول هوشمند (Whale Outflow / Dump) — همان شرایط ولی با افت قیمت
                elif (
                    vol_5m_inflow >= (baseline_vol * VOLUME_SPIKE_RATIO)
                    and vol_5m_inflow >= MIN_INFLOW_USD_5M
                    and -PRICE_PUMP_MAX <= price_5m_change <= -PRICE_PUMP_MIN
                    and not is_in_cooldown(clean_sym)
                ):
                    outflow_signals.append({
                        "symbol": clean_sym, "price": price_usd, "change_5m": price_5m_change,
                        "change_24h": change_24h, "inflow_usd": vol_5m_inflow,
                        "spike_multiplier": spike_multiplier,
                    })
                    last_alert_time[clean_sym] = time.time()

        previous_market_snapshot = current_snapshot

    messages = []

    for s in inflow_signals:
        messages.append(
            f"🚨 **ورود پول هوشمند (SMART MONEY IN)** 🚨\n\n"
            f"🪙 **نماد:** #{s['symbol']} *(موجود در نوبیتکس)*\n"
            f"💵 **قیمت جهانی:** ${s['price']:,.4f}\n"
            f"📊 **تغییرات ۲۴ ساعته:** `{s['change_24h']:+.2f}%`\n\n"
            f"📈 **رشد قیمت ۵ دقیقه:** `+{s['change_5m']:.2f}%`\n"
            f"🔥 **ورود پول خالص:** `${s['inflow_usd']/1e3:,.1f}K`\n"
            f"⚡ **جهش حجم معاملاتی:** `{s['spike_multiplier']:.1f}X` برابر میانگین واقعی\n\n"
            f"🎯 **توصیه:** بررسی چارت در تایم‌فریم ۱۵ دقیقه و ورود پله‌ای."
        )

    for s in outflow_signals:
        messages.append(
            f"🔻 **خروج پول هوشمند (SMART MONEY OUT)** 🔻\n\n"
            f"🪙 **نماد:** #{s['symbol']} *(موجود در نوبیتکس)*\n"
            f"💵 **قیمت جهانی:** ${s['price']:,.4f}\n"
            f"📊 **تغییرات ۲۴ ساعته:** `{s['change_24h']:+.2f}%`\n\n"
            f"📉 **افت قیمت ۵ دقیقه:** `{s['change_5m']:.2f}%`\n"
            f"🔥 **خروج پول خالص (تخمینی):** `${s['inflow_usd']/1e3:,.1f}K`\n"
            f"⚡ **جهش حجم معاملاتی:** `{s['spike_multiplier']:.1f}X` برابر میانگین واقعی\n\n"
            f"🎯 **توصیه:** احتمال توزیع/خروج نهنگ؛ احتیاط در نگهداری پوزیشن."
        )

    if messages:
        send_telegram_chunked(messages)

    if SEND_STATUS_REPORT:
        total_scanned = len(binance_stats) if binance_stats else 0
        status_msg = (
            f"🟢 **گزارش رصد زنده مارکت**\n\n"
            f"⏰ **زمان (UTC):** `{datetime.now(timezone.utc).strftime('%H:%M:%S')}`\n"
            f"🌐 **منبع داده:** `{data_source}`\n"
            f"🔍 **ارزهای آنالیز شده:** `{total_scanned}` از بازار نوبیتکس\n"
            f"📥 **سیگنال ورود:** `{len(inflow_signals)}` مورد\n"
            f"📤 **سیگنال خروج:** `{len(outflow_signals)}` مورد\n"
            f"📡 **وضعیت سیستم:** فعال و ۲۴ ساعته"
        )
        send_telegram(status_msg)

    with state_lock:
        bot_state["last_cycle_at"] = datetime.now(timezone.utc).isoformat()
        bot_state["cycles_completed"] += 1
        bot_state["last_error"] = None


def bot_loop():
    time.sleep(3)
    send_telegram(
        "🚀 **سیستم تحلیل هوشمند فوق‌پایدار فعال شد.**\n"
        "پوشش کامل بیش از ۲۰۰ ارز موجود در نوبیتکس، تشخیص ورود و خروج نهنگ."
    )
    while True:
        try:
            analyze_smart_money()
        except Exception as e:
            log.exception("خطای بحرانی در چرخه تحلیل")
            with state_lock:
                bot_state["last_error"] = str(e)
            # به کاربر هم اطلاع بده تا متوجه قطعی سکوت‌آمیز نشود
            send_telegram(f"⚠️ خطا در چرخه تحلیل رخ داد: `{e}`\nسیستم به کار خود ادامه می‌دهد.")
        time.sleep(SCAN_INTERVAL_SEC)


def start_bot_thread():
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()


start_bot_thread()


@app.route("/")
def health_check():
    return "Smart Money Bot is Scanning Full Nobitex Asset List!", 200


@app.route("/status")
def status():
    with state_lock:
        return jsonify(bot_state)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
