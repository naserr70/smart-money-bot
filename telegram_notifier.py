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
        self.chat_id = chat_id
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

    def send(self, text: str) -> Optional[int]:
        if not self.is_configured():
            log.error("BOT_TOKEN ÛØ§ CHAT_ID ØªÙØ¸ÛÙ ÙØ´Ø¯Ù Ø§Ø³ØªØ Ø§Ø±Ø³Ø§Ù Ù¾ÛØ§Ù ÙØºÙ Ø´Ø¯.")
            return None
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            res = self.session.post(self._api_url("sendMessage"), json=payload, timeout=self.timeout)
            if res.status_code == 200:
                return res.json().get("result", {}).get("message_id")
            log.warning(f"ØªÙÚ¯Ø±Ø§Ù Ø®Ø·Ø§Û HTTP {res.status_code} Ø¨Ø±Ú¯Ø±Ø¯Ø§ÙØ¯: {res.text[:200]}")
        except requests.RequestException as e:
            log.error(f"Ø®Ø·Ø§ Ø¯Ø± Ø§Ø±Ø³Ø§Ù Ù¾ÛØ§Ù ØªÙÚ¯Ø±Ø§Ù: {e}")
        return None

    def delete(self, message_id: int) -> None:
        if not self.is_configured() or not message_id:
            return
        payload = {"chat_id": self.chat_id, "message_id": message_id}
        try:
            self.session.post(self._api_url("deleteMessage"), json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            log.error(f"Ø®Ø·Ø§ Ø¯Ø± Ø­Ø°Ù Ù¾ÛØ§Ù ØªÙÚ¯Ø±Ø§Ù: {e}")

    def send_temporary(self, text: str, delay_sec: int) -> None:
        """Send a message and schedule its deletion after `delay_sec` seconds (status reports)."""
        msg_id = self.send(text)
        if msg_id:
            threading.Thread(target=self._delayed_delete, args=(msg_id, delay_sec), daemon=True).start()

    def _delayed_delete(self, msg_id: int, delay_sec: int) -> None:
        time.sleep(delay_sec)
        self.delete(msg_id)

    def send_chunked(self, messages: List[str], max_len: int = 3500) -> None:
        """Send a list of messages, packing consecutive ones into <= max_len chunks
        so a burst of signals doesn't turn into a burst of separate Telegram messages."""
        if not messages:
            return
        buffer = ""
        for msg in messages:
            if len(buffer) + len(msg) + 2 > max_len:
                if buffer:
                    self.send(buffer)
                buffer = msg
            else:
                buffer = f"{buffer}\n\n{msg}" if buffer else msg
        if buffer:
            self.send(buffer)
