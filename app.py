import os
import time
import threading
import requests
from flask import Flask

# ==================== تنظیمات وب‌سرور ====================
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Nobitex Smart Money Bot is Running Alive!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# ==================== تنظیمات ربات تلگرام ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")

# فیلترهای سخت‌گیرانه اسمارت مانی
MIN_INFLOW_USD_5M = 50_000      # حداقل ۵۰ هزار دلار ورود پول در ۵ دقیقه
VOLUME_SPIKE_RATIO = 3.0        # حداقل ۳ برابر شدن حجم نسبت به میانگین
PRICE_PUMP_MIN = 1.2            # حداقل ۱.۲٪ رشد قیمت
PRICE_PUMP_MAX = 8.0            # سقف رشد ۵ دقیقه (جلوگیری از فومو و پامپ‌های هیجانی)

COIN_MAP = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin", "XRP": "ripple",
    "ADA": "cardano", "DOGE": "dogecoin", "AVAX": "avalanche-2", "TRX": "tron", "DOT": "polkadot",
    "LINK": "chainlink", "SHIB": "shiba-inu", "LTC": "litecoin", "BCH": "bitcoin-cash", "NEAR": "near",
    "UNI": "uniswap", "APT": "aptos", "SUI": "sui", "ICP": "internet-computer", "ETC": "ethereum-classic",
    "XLM": "stellar", "STX": "blockstack", "XMR": "monero", "FIL": "filecoin", "ARB": "arbitrum",
    "RENDER": "render-token", "VET": "vechain", "MKR": "maker", "HBAR": "hedera-hashgraph", "OP": "optimism",
    "INJ": "injective-protocol", "PEPE": "pepe", "FLOKI": "floki", "BONK": "bonk", "WIF": "dogwifhat",
    "NOT": "notcoin", "DOGS": "dogs-2", "HMSTR": "hamster-kombat", "TON": "the-open-network", "AAVE": "aave",
    "GRT": "the-graph", "ALGO": "algorand", "FTM": "fantom", "RUNE": "thorchain", "SAND": "the-sandbox",
    "MANA": "decentraland", "AXS": "axie-infinity", "CHZ": "chiliz", "GALA": "gala", "DYDX": "dydx",
    "STRK": "starknet", "TIA": "celestia", "SEI": "sei-network", "JUP": "jupiter-exchange-solana",
    "PYTH": "pyth-network", "PENDLE": "pendle", "ENA": "ethena", "ONDO": "ondo-finance", "OM": "mantra-dao",
    "RAY": "raydium", "POPCAT": "popcat", "FET": "fetch-ai", "ORDI": "ordi", "SATS": "1000sats-ordinals"
}

previous_market_snapshot = {}

def send_telegram(text):
    """ارسال پیام به تلگرام"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Error sending Telegram message: {e}")

def fetch_coingecko_data(gecko_ids):
    """دریافت داده‌های مارکت بین‌المللی"""
    all_data = {}
    chunk_size = 40
    for i in range(0, len(gecko_ids), chunk_size):
        chunk = gecko_ids[i:i + chunk_size]
        params = {
            "ids": ",".join(chunk),
            "vs_currencies": "usd",
            "include_24hr_vol": "true",
            "include_24hr_change": "true"
        }
        try:
            res = requests.get("https://api.coingecko.com/api/v3/simple/price", params=params, timeout=10)
            if res.status_code == 200:
                all_data.update(res.json())
        except Exception as e:
            print(f"Error fetching Gecko data: {e}")
        time.sleep(1.2)
    return all_data

def analyze_smart_money():
    global previous_market_snapshot
    
    gecko_ids = list(COIN_MAP.values())
    global_stats = fetch_coingecko_data(gecko_ids)

    if not global_stats:
        return

    current_snapshot = {}
    smart_signals = []

    for sym, gecko_id in COIN_MAP.items():
        if gecko_id not in global_stats:
            continue

        item = global_stats[gecko_id]
        price_usd = float(item.get("usd", 0))
        vol_24h_usd = float(item.get("usd_24h_vol", 0))
        change_24h = float(item.get("usd_24hr_change", 0))

        current_snapshot[sym] = {
            "price": price_usd,
            "vol_usd": vol_24h_usd
        }

        if sym in previous_market_snapshot:
            prev_price = previous_market_snapshot[sym]["price"]
            prev_vol = previous_market_snapshot[sym]["vol_usd"]

            if prev_price > 0:
                price_5m_change = ((price_usd - prev_price) / prev_price) * 100
                vol_5m_inflow = vol_24h_usd - prev_vol
                expected_5m_avg_vol = vol_24h_usd / 288

                # فیلتر چندگانه تشخیص نهنگ واقعی
                if (vol_5m_inflow >= (expected_5m_avg_vol * VOLUME_SPIKE_RATIO) and 
                    vol_5m_inflow >= MIN_INFLOW_USD_5M and 
                    PRICE_PUMP_MIN <= price_5m_change <= PRICE_PUMP_MAX):

                    spike_multiplier = vol_5m_inflow / expected_5m_avg_vol if expected_5m_avg_vol > 0 else 1
                    
                    smart_signals.append({
                        "symbol": sym,
                        "price": price_usd,
                        "change_5m": price_5m_change,
                        "change_24h": change_24h,
                        "inflow_usd": vol_5m_inflow,
                        "spike_multiplier": spike_multiplier
                    })

    previous_market_snapshot = current_snapshot

    if smart_signals:
        for s in smart_signals:
            alert_msg = (
                f"🚨 **سیگنال ورود پول هوشمند (SMART MONEY INFLOW)** 🚨\n\n"
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

def bot_loop():
    send_telegram("🤖 **ربات هوشمند رصد اسمارت مانی روشن شد.**")
    while True:
        analyze_smart_money()
        time.sleep(300)

if __name__ == "__main__":
    t = threading.Thread(target=bot_loop)
    t.daemon = True
    t.start()
    run_flask()
