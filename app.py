import os
import time
import threading
import requests
from flask import Flask

app = Flask(__name__)

# خواندن متغیرهای محیطی
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
        print(f"📡 Telegram API Status: {res.status_code} - Response: {res.text}")
        return res.status_code == 200
    except Exception as e:
        print(f"❌ Telegram Send Exception: {e}")
        return False

def bot_loop():
    print("🚀 Starting Bot Loop Thread...")
    time.sleep(3) # شکیبایی کوتاه برای استیبل شدن وب‌سرور
    
    # پیام تست اولیه
    start_success = send_telegram("🚀 **تست اتصال انجام شد!**\nربات با موفقیت روشن شد و سیستم هوشمند فعال است.")
    if not start_success:
        print("❌ Initial message failed. Please check BOT_TOKEN and CHAT_ID!")

    counter = 1
    while True:
        time.sleep(300) # هر ۵ دقیقه
        msg = f"🟢 **گزارش زنده ربات (تست شماره {counter})**\n\n⏰ زمان: `{time.strftime('%H:%M:%S')}` UTC\n📡 وضعیت: فعال و در حال رصد مارکت"
        send_telegram(msg)
        counter += 1

# 🟢 این بخش کلیدی است: اجرا شدن Thread خارج از شرط __main__ جهت سازگاری کامل با Gunicorn
def start_bot_thread():
    t = threading.Thread(target=bot_loop)
    t.daemon = True
    t.start()

start_bot_thread()

@app.route('/')
def health_check():
    return "Bot is alive and running!", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
