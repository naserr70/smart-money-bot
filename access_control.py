"""
Access control for the Telegram bot: password-gated entry, with the admin
(ADMIN_CHAT_ID / defaults to CHAT_ID) able to set a specific access duration
per user — the "give the bot to someone for a set amount of time"
requirement, plus admin-defined unique per-user invite passwords.

Persistence: Render's free web-service plan wipes local disk on every
restart/spin-down. To keep grants and admin controls across restarts, this
class can sync its state to a free GitHub Gist instead of (or in addition to)
a local file.

This module is intentionally UI-agnostic: it only tracks state and answers
"is this chat_id currently allowed in?". The actual Telegram command parsing
lives in bot_commands.py.
"""
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

log = logging.getLogger("smart_money_bot.access_control")

GIST_API_BASE = "https://api.github.com/gists"
GIST_FILENAME = "smart_money_bot_access.json"

_SIGNAL_DELIVERY_STATE = {
    "smart_money": True,
    "whale": True,
    "pump_dump": True,
}


def signal_delivery_enabled(category: str) -> bool:
    return bool(_SIGNAL_DELIVERY_STATE.get(category, True))


class AccessControl:
    SIGNAL_CONTROL_DEFAULTS = {
        "smart_money": True,
        "whale": True,
        "pump_dump": True,
    }

    def __init__(self, state_file_path: str, admin_chat_id: str,
                 gist_id: str = "", gist_token: str = "", http_session: requests.Session = None):
        self._lock = threading.RLock()
        self._state_file_path = state_file_path
        self.admin_chat_id = str(admin_chat_id) if admin_chat_id else ""
        self._gist_id = gist_id
        self._gist_token = gist_token
        self._http = http_session or requests.Session()

        self._users: Dict[str, dict] = {}
        self._invites: Dict[str, dict] = {}
        # Permanent flags that must survive restarts (e.g. first-start announce
        # and the admin's per-category Telegram signal delivery controls).
        self._flags: Dict[str, bool] = {}
        self._load()
        self._sync_signal_delivery_state()

    # ==================== persistence backends ====================

    def _gist_enabled(self) -> bool:
        return bool(self._gist_id and self._gist_token)

    def _gist_headers(self) -> dict:
        return {"Authorization": f"token {self._gist_token}", "Accept": "application/vnd.github+json"}

    def _load(self) -> None:
        if self._gist_enabled():
            data = self._gist_fetch()
            if data is not None:
                self._users = data.get("users", {})
                self._invites = data.get("invites", {})
                self._flags = data.get("flags", {}) or {}
                log.info(f"{len(self._users)} کاربر و {len(self._invites)} رمز دعوت از GitHub Gist بازیابی شد.")
                return
            log.warning("بازیابی از GitHub Gist ناموفق بود؛ به فایل محلی برمی‌گردم (ممکن است خالی باشد).")
        self._load_local()

    def _persist(self) -> None:
        if self._gist_enabled():
            self._gist_save()
        else:
            self._save_local()

    def _gist_fetch(self) -> Optional[dict]:
        try:
            res = self._http.get(f"{GIST_API_BASE}/{self._gist_id}", headers=self._gist_headers(), timeout=10)
            if res.status_code != 200:
                log.warning(f"GitHub Gist GET خطا داد: {res.status_code} {res.text[:200]}")
                return None
            files = res.json().get("files", {})
            file_entry = files.get(GIST_FILENAME)
            if not file_entry:
                return {"users": {}, "invites": {}, "flags": {}}
            return json.loads(file_entry.get("content") or "{}")
        except (requests.RequestException, ValueError) as e:
            log.warning(f"خطا در خواندن GitHub Gist: {e}")
            return None

    def _gist_save(self) -> None:
        with self._lock:
            content = json.dumps({
                "users": self._users,
                "invites": self._invites,
                "flags": self._flags,
            }, ensure_ascii=False, indent=2)
        payload = {"files": {GIST_FILENAME: {"content": content}}}
        try:
            res = self._http.patch(f"{GIST_API_BASE}/{self._gist_id}", headers=self._gist_headers(),
                                    json=payload, timeout=10)
            if res.status_code != 200:
                log.warning(f"GitHub Gist PATCH خطا داد: {res.status_code} {res.text[:200]}")
        except requests.RequestException as e:
            log.warning(f"خطا در نوشتن روی GitHub Gist: {e}")

    def _load_local(self) -> None:
        if not self._state_file_path or not os.path.exists(self._state_file_path):
            return
        try:
            with open(self._state_file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._users = data.get("users", {})
            self._invites = data.get("invites", {})
            self._flags = data.get("flags", {}) or {}
            log.info(f"{len(self._users)} کاربر و {len(self._invites)} رمز دعوت از فایل محلی بازیابی شد.")
        except (OSError, json.JSONDecodeError) as e:
            log.warning(f"بازیابی لیست کاربران مجاز از فایل محلی ناموفق بود: {e}")

    def _save_local(self) -> None:
        if not self._state_file_path:
            return
        with self._lock:
            payload = {
                "users": self._users,
                "invites": self._invites,
                "flags": self._flags,
            }
        tmp_path = f"{self._state_file_path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._state_file_path)
        except OSError as e:
            log.warning(f"ذخیره‌ی لیست کاربران مجاز در فایل محلی ناموفق بود: {e}")

    # ==================== permanent flags ====================

    def is_startup_announced(self) -> bool:
        with self._lock:
            return bool(self._flags.get("startup_announced"))

    def mark_startup_announced(self) -> None:
        with self._lock:
            self._flags["startup_announced"] = True
        self._persist()

    # ==================== Telegram signal delivery controls ====================

    def _sync_signal_delivery_state(self) -> None:
        for category, default in self.SIGNAL_CONTROL_DEFAULTS.items():
            _SIGNAL_DELIVERY_STATE[category] = bool(
                self._flags.get(f"signal_{category}", default)
            )

    def is_signal_enabled(self, category: str) -> bool:
        default = self.SIGNAL_CONTROL_DEFAULTS.get(category, True)
        with self._lock:
            return bool(self._flags.get(f"signal_{category}", default))

    def set_signal_enabled(self, category: str, enabled: bool) -> bool:
        if category not in self.SIGNAL_CONTROL_DEFAULTS:
            raise ValueError(f"unknown signal category: {category}")
        enabled = bool(enabled)
        with self._lock:
            self._flags[f"signal_{category}"] = enabled
            _SIGNAL_DELIVERY_STATE[category] = enabled
        self._persist()
        return enabled

    def toggle_signal(self, category: str) -> bool:
        return self.set_signal_enabled(category, not self.is_signal_enabled(category))

    def signal_controls(self) -> Dict[str, bool]:
        return {category: self.is_signal_enabled(category) for category in self.SIGNAL_CONTROL_DEFAULTS}

    # ==================== users ====================

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

    def days_remaining(self, chat_id) -> Optional[float]:
        chat_id = str(chat_id)
        if self.is_admin(chat_id):
            return None
        with self._lock:
            entry = self._users.get(chat_id)
        if not entry:
            return None
        expires_at = entry.get("expires_at")
        if expires_at is None:
            return None
        delta = datetime.fromisoformat(expires_at) - datetime.now(timezone.utc)
        return max(delta.total_seconds() / 86400, 0.0)

    def get_entry(self, chat_id) -> Optional[dict]:
        with self._lock:
            entry = self._users.get(str(chat_id))
            return dict(entry) if entry else None

    def grant(self, chat_id, days: Optional[float], label: str = "") -> None:
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
        self._persist()

    def revoke(self, chat_id) -> bool:
        chat_id = str(chat_id)
        with self._lock:
            existed = self._users.pop(chat_id, None) is not None
        if existed:
            self._persist()
        return existed

    def active_chat_ids(self) -> List[str]:
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

    # ==================== per-user invite passwords ====================

    def create_invite(self, password: str, days: Optional[float], label: str = "") -> None:
        with self._lock:
            self._invites[password] = {
                "days": days,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "label": label,
                "used_by": None,
                "used_at": None,
            }
        self._persist()

    def consume_invite(self, password: str, chat_id: str):
        with self._lock:
            entry = self._invites.get(password)
            if not entry or entry.get("used_by") is not None:
                return False, None
            entry["used_by"] = str(chat_id)
            entry["used_at"] = datetime.now(timezone.utc).isoformat()
            days = entry.get("days")
        self._persist()
        return True, days

    def list_unused_invites(self) -> Dict[str, dict]:
        with self._lock:
            return {p: dict(e) for p, e in self._invites.items() if e.get("used_by") is None}
