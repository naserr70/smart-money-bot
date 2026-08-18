"""
Access control for the Telegram bot: password-gated entry, with the admin
(ADMIN_CHAT_ID / defaults to CHAT_ID) able to set a specific access duration
per user — the "give the bot to someone for a set amount of time"
requirement, plus admin-defined unique per-user invite passwords.

Persistence: Render's free web-service plan wipes local disk on every
restart/spin-down (this bit us repeatedly in practice — grants would
silently disappear a few minutes after being made). To fix that WITHOUT
needing a paid Render Persistent Disk, this class can sync its state to a
free GitHub Gist instead of (or in addition to) a local file:

  - If GITHUB_GIST_ID + GITHUB_GIST_TOKEN are set, every grant/revoke/
    invite-creation is written straight to that Gist, and state is loaded
    from the Gist at startup — this survives restarts because the Gist
    lives outside the container entirely.
  - If they're not set, falls back to the original local-file behavior
    (works fine on any host with real persistent disk; on Render's free
    tier it will keep resetting on restart, exactly as before).

Expiry is checked lazily (at authorization-check time and at broadcast
time) rather than via a background sweep thread — simpler, and just as
correct at this scale.

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

import requests

log = logging.getLogger("smart_money_bot.access_control")

GIST_API_BASE = "https://api.github.com/gists"
GIST_FILENAME = "smart_money_bot_access.json"


class AccessControl:
    def __init__(self, state_file_path: str, admin_chat_id: str,
                 gist_id: str = "", gist_token: str = "", http_session: requests.Session = None):
        self._lock = threading.RLock()
        self._state_file_path = state_file_path
        self.admin_chat_id = str(admin_chat_id) if admin_chat_id else ""
        self._gist_id = gist_id
        self._gist_token = gist_token
        self._http = http_session or requests.Session()

        # chat_id(str) -> {"granted_at": iso, "expires_at": iso|None, "label": str}
        self._users: Dict[str, dict] = {}
        # password(str) -> {"days": float|None, "created_at": iso, "label": str,
        #                    "used_by": chat_id|None, "used_at": iso|None}
        self._invites: Dict[str, dict] = {}
        self._load()

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
                return {"users": {}, "invites": {}}  # gist exists but this file isn't in it yet
            return json.loads(file_entry.get("content") or "{}")
        except (requests.RequestException, ValueError) as e:
            log.warning(f"خطا در خواندن GitHub Gist: {e}")
            return None

    def _gist_save(self) -> None:
        with self._lock:
            content = json.dumps({"users": self._users, "invites": self._invites}, ensure_ascii=False, indent=2)
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
            log.info(f"{len(self._users)} کاربر و {len(self._invites)} رمز دعوت از فایل محلی بازیابی شد.")
        except (OSError, json.JSONDecodeError) as e:
            log.warning(f"بازیابی لیست کاربران مجاز از فایل محلی ناموفق بود: {e}")

    def _save_local(self) -> None:
        if not self._state_file_path:
            return
        with self._lock:
            payload = {"users": self._users, "invites": self._invites}
        tmp_path = f"{self._state_file_path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._state_file_path)
        except OSError as e:
            log.warning(f"ذخیره‌ی لیست کاربران مجاز در فایل محلی ناموفق بود: {e}")

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
