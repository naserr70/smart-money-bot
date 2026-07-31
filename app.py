import os
import time
import threading
import requests
from flask import Flask

app = Flask(__name__)

# ==================== دریافت امن متغیرها ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()

# ==================== فیلترهای سخت‌گیرانه نهنگ و اسمارت مانی ====================
MIN_INFLOW_USD_5M = 50_000      # حداقل ۵۰ هزار دلار ورود پول خالص در ۵ دقیقه
VOLUME_SPIKE_RATIO = 2.5        # حداقل ۲.۵ برابر شدن حجم معاملاتی نسبت به میانگین
PRICE_PUMP_MIN = 1.0            # حداقل ۱.۰٪ رشد قیمت صعودی
PRICE_PUMP_MAX = 8.0            # سقف رشد ۵ دقیقه

# 🗺️ لیست ارزهای لیست‌شده در نوبیتکس (جفت‌ارزهای جهانی USDT)
NOBITEX_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "AVAXUSDT",
    "TRXUSDT", "DOTUSDT", "LINKUSDT", "SHIBUSDT", "LTCUSDT", "BCHUSDT", "NEARUSDT", "UNIUSDT",
    "APTUSDT", "SUIUSDT", "ICPUSDT", "ETCUSDT", "XLMUSDT", "STXUSDT", "XMRUSDT", "FILUSDT",
    "ARBUSDT", "RENDERUSDT", "VETUSDT", "MKRUSDT", "HBARUSDT", "OPUSDT", "INJUSDT", "PEPEUSDT",
    "FLOKIUSDT", "BONKUSDT", "WIFUSDT", "NOTUSDT", "DOGSUSDT", "HMSTRUSDT", "TONUSDT", "AAVEUSDT",
    "GRTUSDT", "ALGOUSDT", "FTMUSDT", "RUNEUSDT", "SANDUSDT", "MANAUSDT", "AXSUSDT", "CHZUSDT",
    "GALAUSDT", "DYDXUSDT", "STRKUSDT", "TIAUSDT", "SEIUSDT", "JUPUSDT", "PYTHUSDT", "PENDLEUSDT",
    "ENAUSDT", "ONDOUSDT", "OMUSDT", "RAYUSDT", "POPCATUSDT", "FETUSDT", "ORDIUSDT", "1000SATSUSDT"
]

previous_market_snapshot = {}

def send_telegram(text):
    """ارسال پیام هشدارهای ساختاریافته به تلگرام"""
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ Error: BOT_TOKEN or CHAT_ID is missing!")
        return False
        
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        return res.status_code == 200
    except Exception as e:
        print(f"Error sending Telegram message: {e}")
        return False

def fetch_binance_ticker_data():
    """دریافت داده‌های زنده و بدون محدودیت از API صرافی بایننس"""
    try:
        url = "https://api.binance.com/api/v3/ticker/24hr"
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            # فیلتر کردن فقط برای ارزهای نوبیتکس
            return {item["symbol"]: item for item in data if item["symbol"] in NOBITEX_SYMBOLS}
    except Exception as e:
        print(f"Error fetching Binance data: {e}")
    return {}

def analyze_smart_money():
    global previous_market_snapshot
    
    binance_stats = fetch_binance_ticker_data()
    smart_signals = []

    if binance_stats:
        current_snapshot = {}

        for sym, item in binance_stats.items():
            clean_sym = sym.replace("USDT", "")
            price_usd = float(item.get("lastPrice", 0))
            vol_24h_usd = float(item.get("quoteVolume", 0))
            change_24h = float(item.get("priceChangePercent", 0))

            current_snapshot[clean_sym] = {
                "price": price_usd,
                "vol_usd": vol_24h_usd
            }

            # بررسی و تحلیل ۵ دقیقه اخیر
            if clean_sym in previous_market_snapshot:
                prev_price = previous_market_snapshot[clean_sym]["price"]
                prev_vol = previous_market_snapshot[clean_sym]["vol_usd"]

                if prev_price > 0:
                    price_5m_change = ((price_usd - prev_price) / prev_price) * 100
                    vol_5m_inflow = vol_24h_usd - prev_vol
                    expected_5m_avg_vol = vol_24h_usd / 288

                    # فیلتر چندگانه ورود نهنگ
                    if (vol_5m_inflow >= (expected_5m_avg_vol * VOLUME_SPIKE_RATIO) and 
                        vol_5m_inflow >= MIN_INFLOW_USD_5M and 
                        PRICE_PUMP_MIN <= price_5m_change <= PRICE_PUMP_MAX):

                        spike_multiplier = vol_5m_inflow / expected_5m_avg_vol if expected_5m_avg_vol > 0 else 1
                        
                        smart_signals.append({
                            "symbol": clean_sym,
                            "price": price_usd,
                            "change_5m": price_5m_change,
                            "change_24h": change_24h,
                            "inflow_usd": vol_5m_inflow,
                            "spike_multiplier": spike_multiplier
                        })

        previous_market_snapshot = current_snapshot

    # ۱. ارسال سیگنال در صورت کشف نهنگ/اسمارت مانی
    if smart_signals:
        for s in smart_signals:
            alert_msg = (
                f"🚨 **سیگنال ورود پول هوشمند (SMART MONEY)** 🚨\n\n"
                f"🪙 **نماد:** #{s['symbol']} *(موجود در نوبیتکس)*\n"
                f"💵 **قیمت جهانی:** ${s['price']:,.4f}\n"
                f"📊 **تغییرات ۲۴ ساعته:** `{s['change_24h']:+.2f}%`\n\n"
                f"🔍 **شاخص‌های تاییدیه نهنگ (۵ دقیقه اخیر):**\n"
                f"📈 **رشد قیمت ۵ دقیقه:** `+{s['change_5m']:.2f}%`\n"
                f"🔥 **ورود پول خالص:** `${s['inflow_usd']/1e3:,.1f}K`\n"
                f"⚡ **جهش حجم معاملاتی:** `{s['spike_multiplier']:.1f}X` برابر میانگین\n\n"
                f"🎯 **توصیه:** *بررسی چارت در تایم‌فریم ۱۵ دقیقه و ورود پله‌ای.*"
            )
            send_telegram(alert_msg)

    # ۲. ارسال گزارش زنده ۵ دقیقه‌ای
    total_scanned = len(binance_stats) if binance_stats else 0
    status_msg = (
        f"🟢 **گزارش رصد زنده مارکت**\n\n"
        f"⏰ **زمان:** `{time.strftime('%H:%M:%S')}` UTC\n"
        f"🔍 **ارزهای آنالیز شده:** `{total_scanned}` از `{len(NOBITEX_SYMBOLS)}`\n"
        f"🎯 **سیگنال‌های نهنگ در این دور:** `{len(smart_signals)}` مورد\n"
        f"📡 **وضعیت سیستم:** فعال و ۲۴ ساعته"
    )
    send_telegram(status_msg)

def bot_loop():
    time.sleep(3)
    send_telegram("🚀 **سیستم تحلیل هوشمند اسمارت مانی (Binance Engine) فعال شد.**\nبازار بین‌المللی با سرعت و پایداری بالا در حال رصد است.")
    while True:
        analyze_smart_money()
        time.sleep(300) # هر ۵ دقیقه یک‌بار

def start_bot_thread():
    t = threading.Thread(target=bot_loop)
    t.daemon = True
    t.start()

start_bot_thread()

@app.route('/')
def health_check():
    return "Smart Money Bot is Alive & Scanning Binance Data!", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
