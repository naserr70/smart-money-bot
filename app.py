import os
import time
import threading
import requests
from flask import Flask

app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is alive!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# خواندن مستقیم متغیرها
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()

def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ Error: BOT_TOKEN or CHAT_ID is missing in Environment Variables!")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        print(f"Telegram Response Status: {res.status_code}")
        return res.status_code == 200
    except Exception as e:
        print(f"Telegram Send Error: {e}")
        return False

def bot_loop():
    # تست اولیه بلافاصله بعد از روشن شدن
    start_success = send_telegram("🚀 **تست اتصال انجام شد!**\nربات با موفقیت روشن شد و سیستم هوشمند فعال است.")
    if not start_success:
        print("❌ Failed to send initial Telegram message. Check credentials!")

    counter = 1
    while True:
        time.sleep(300) # هر ۵ دقیقه
        msg = f"🟢 **گزارش زنده ربات (تست شماره {counter})**\n\n⏰ زمان: `{time.strftime('%H:%M:%S')}`\n📡 وضعیت: فعال و در حال رصد مارکت"
        send_telegram(msg)
        counter += 1

if __name__ == "__main__":
    t = threading.Thread(target=bot_loop)
    t.daemon = True
    t.start()
    run_flask()
