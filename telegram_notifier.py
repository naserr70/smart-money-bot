import logging
import threading
import time
from typing import List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("smart_money_bot.telegram")


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout: int = 10, max_retries: int = 3):
        self.bot_token = bot_token
        self.chat_id = chat_id  # default/admin recipient, kept for backward compatibility
        self.timeout = timeout
        self.session = self._build_session(max_retries)

    @staticmethod
    def _build_session(max_retries: int) -> requests.Session:
        session = requests.Session()
        retries = Retry(
            total=max_retries,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "POST"]),
        )
        session.mount("https://", HTTPAdapter(max_retries=retries))
        session.headers.update({"User-Agent": "SmartMoneyBot/2.0"})
        return session

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/{method}"

    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(self, text: str, chat_id: str = None, reply_markup: dict = None) -> Optional[int]:
        """Send to `chat_id`, or to the default/admin chat_id if omitted.
        `reply_markup` is passed straight through to Telegram (e.g. an
        {"inline_keyboard": [[...]]} dict for a menu of buttons)."""
        target = chat_id or self.chat_id
        if not self.bot_token or not target:
            log.error("BOT_TOKEN یا chat_id تنظیم نشده است؛ ارسال پیام لغو شد.")
            return None
        payload = {
            "chat_id": target,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            res = self.session.post(self._api_url("sendMessage"), json=payload, timeout=self.timeout)
            if res.status_code == 200:
                return res.json().get("result", {}).get("message_id")
            log.warning(f"تلگرام خطای HTTP {res.status_code} برگرداند برای chat_id={target}: {res.text[:200]}")
        except requests.RequestException as e:
            log.error(f"خطا در ارسال پیام تلگرام به {target}: {e}")
        return None

    def edit_message(self, chat_id: str, message_id: int, text: str, reply_markup: dict = None) -> bool:
        """Edit an existing message's text/keyboard in place — used for menu
        navigation so pressing a button updates the same message instead of
        spamming a new one each time."""
        if not self.bot_token or not chat_id or not message_id:
            return False
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            res = self.session.post(self._api_url("editMessageText"), json=payload, timeout=self.timeout)
            if res.status_code == 200:
                return True
            log.warning(f"ویرایش پیام تلگرام ناموفق بود: {res.text[:200]}")
        except requests.RequestException as e:
            log.error(f"خطا در ویرایش پیام تلگرام: {e}")
        return False

    def answer_callback_query(self, callback_query_id: str, text: str = None) -> None:
        """Must be called for every button press, or Telegram shows an
        infinite loading spinner on the button in the user's app."""
        if not self.bot_token or not callback_query_id:
            return
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            self.session.post(self._api_url("answerCallbackQuery"), json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            log.error(f"خطا در پاسخ به callback_query: {e}")

    def delete(self, message_id: int, chat_id: str = None) -> None:
        target = chat_id or self.chat_id
        if not self.bot_token or not target or not message_id:
            return
        payload = {"chat_id": target, "message_id": message_id}
        try:
            self.session.post(self._api_url("deleteMessage"), json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            log.error(f"خطا در حذف پیام تلگرام: {e}")

    def send_temporary(self, text: str, delay_sec: int, chat_id: str = None) -> None:
        """Send a message and schedule its deletion after `delay_sec` seconds (status reports)."""
        target = chat_id or self.chat_id
        msg_id = self.send(text, chat_id=target)
        if msg_id:
            threading.Thread(target=self._delayed_delete, args=(msg_id, delay_sec, target), daemon=True).start()

    def _delayed_delete(self, msg_id: int, delay_sec: int, chat_id: str) -> None:
        time.sleep(delay_sec)
        self.delete(msg_id, chat_id=chat_id)

    def send_chunked(self, messages: List[str], max_len: int = 3500, chat_id: str = None) -> None:
        """Send a list of messages, packing consecutive ones into <= max_len chunks
        so a burst of signals doesn't turn into a burst of separate Telegram messages."""
        if not messages:
            return
        target = chat_id or self.chat_id
        buffer = ""
        for msg in messages:
            if len(buffer) + len(msg) + 2 > max_len:
                if buffer:
                    self.send(buffer, chat_id=target)
                buffer = msg
            else:
                buffer = f"{buffer}\n\n{msg}" if buffer else msg
        if buffer:
            self.send(buffer, chat_id=target)

    def broadcast(self, text: str, chat_ids: List[str]) -> None:
        """Send the same message to every chat_id in the list (deduped)."""
        for target in dict.fromkeys(chat_ids):  # dedupe, preserve order
            self.send(text, chat_id=target)

    def broadcast_chunked(self, messages: List[str], chat_ids: List[str], max_len: int = 3500) -> None:
        for target in dict.fromkeys(chat_ids):
            self.send_chunked(messages, max_len=max_len, chat_id=target)