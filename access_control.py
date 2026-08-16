"""
Access control for the Telegram bot: password-gated entry, with the admin
(ADMIN_CHAT_ID, defaults to CHAT_ID) able to set a specific access duration
per user — exactly the "give the bot to someone for a set amount of time"
requirement.

Persisted to disk so a Render restart doesn't wipe out who currently has
access. Expiry is checked lazily (at authorization-check time and at
broadcast time) rather than via a background sweep thread — simpler, and
just as correct at this scale.

This module is intentionally UI-agnostic: it only tracks state and answers
"is this chat_id currently allowed in?". The actual Telegram command
parsing (/start, /grant, password text, etc.) lives in bot_commands.py.
"""
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

log = logging.getLogger("smart_money_bot.access_control")


class AccessControl:
    def __init__(self, state_file_path: str, admin_chat_id: str):
        self._lock = threading.RLock()
        self._state_file_path = state_file_path
        self.admin_chat_id = str(admin_chat_id) if admin_chat_id else ""
        # chat_id(str) -> {"granted_at": iso, "expires_at": iso|None, "label": str}
        self._users: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self._state_file_path or not os.path.exists(self._state_file_path):
            return
        try:
            with open(self._state_file_path, "r", encoding="utf-8") as f:
                self._users = json.load(f)
            log.info(f"{len(self._users)} کاربر مجاز از دیسک بازیابی شد.")
        except (OSError, json.JSONDecodeError) as e:
            log.warning(f"بازیابی لیست کاربران مجاز ناموفق بود: {e}")

    def save(self) -> None:
        if not self._state_file_path:
            return
        with self._lock:
            payload = dict(self._users)
        tmp_path = f"{self._state_file_path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._state_file_path)
        except OSError as e:
            log.warning(f"ذخیره‌ی لیست کاربران مجاز ناموفق بود: {e}")

    def is_admin(self, chat_id) -> bool:
        return bool(self.admin_chat_id) and str(chat_id) == self.admin_chat_id

    def is_authorized(self, chat_id) -> bool:
        chat_id = str(chat_id)
        if self.is_admin(chat_id):
            return True
        with self._lock:
            entry = self._users.get(chat_id)
        if not entry:
            return False
        expires_at = entry.get("expires_at")
        if expires_at is None:
            return True
        return datetime.fromisoformat(expires_at) > datetime.now(timezone.utc)

    def expiry_text(self, chat_id) -> str:
        chat_id = str(chat_id)
        if self.is_admin(chat_id):
            return "نامحدود (ادمین)"
        with self._lock:
            entry = self._users.get(chat_id)
        if not entry:
            return "دسترسی ندارید"
        expires_at = entry.get("expires_at")
        if expires_at is None:
            return "نامحدود"
        return datetime.fromisoformat(expires_at).strftime("%Y-%m-%d %H:%M UTC")

    def grant(self, chat_id, days: Optional[float], label: str = "") -> None:
        """days=None means unlimited access. days=0 or negative effectively revokes."""
        chat_id = str(chat_id)
        expires_at = None
        if days is not None:
            expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        with self._lock:
            self._users[chat_id] = {
                "granted_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": expires_at,
                "label": label,
            }
        self.save()

    def revoke(self, chat_id) -> bool:
        chat_id = str(chat_id)
        with self._lock:
            existed = self._users.pop(chat_id, None) is not None
        if existed:
            self.save()
        return existed

    def active_chat_ids(self) -> List[str]:
        """All currently-authorized, non-expired, non-admin chat_ids (admin
        is handled separately by the caller since it always gets messages)."""
        now = datetime.now(timezone.utc)
        result = []
        with self._lock:
            items = list(self._users.items())
        for chat_id, entry in items:
            expires_at = entry.get("expires_at")
            if expires_at is None or datetime.fromisoformat(expires_at) > now:
                result.append(chat_id)
        return result

    def list_users(self) -> Dict[str, dict]:
        with self._lock:
            return dict(self._users)
