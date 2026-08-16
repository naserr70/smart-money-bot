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
        self._invites_file_path = f"{state_file_path}.invites" if state_file_path else None
        self.admin_chat_id = str(admin_chat_id) if admin_chat_id else ""
        # chat_id(str) -> {"granted_at": iso, "expires_at": iso|None, "label": str}
        self._users: Dict[str, dict] = {}
        # password(str) -> {"days": float|None, "created_at": iso, "label": str,
        #                    "used_by": chat_id|None, "used_at": iso|None}
        self._invites: Dict[str, dict] = {}
        self._load()
        self._load_invites()

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

    def _load_invites(self) -> None:
        if not self._invites_file_path or not os.path.exists(self._invites_file_path):
            return
        try:
            with open(self._invites_file_path, "r", encoding="utf-8") as f:
                self._invites = json.load(f)
            log.info(f"{len(self._invites)} رمز دعوت از دیسک بازیابی شد.")
        except (OSError, json.JSONDecodeError) as e:
            log.warning(f"بازیابی رمزهای دعوت ناموفق بود: {e}")

    def _save_invites(self) -> None:
        if not self._invites_file_path:
            return
        with self._lock:
            payload = dict(self._invites)
        tmp_path = f"{self._invites_file_path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._invites_file_path)
        except OSError as e:
            log.warning(f"ذخیره‌ی رمزهای دعوت ناموفق بود: {e}")

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
        """None means "unlimited / admin / not tracked with an expiry" —
        callers should check is_authorized() first if they need to
        distinguish "unlimited" from "no access at all"."""
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

    # ==================== per-user invite passwords ====================

    def create_invite(self, password: str, days: Optional[float], label: str = "") -> None:
        """Admin-defined, single-use password for one specific person. Set
        days=None for unlimited access once redeemed."""
        with self._lock:
            self._invites[password] = {
                "days": days,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "label": label,
                "used_by": None,
                "used_at": None,
            }
        self._save_invites()

    def consume_invite(self, password: str, chat_id: str):
        """Redeem a one-time invite password. Returns (found, days):
        found=False means no such unused invite exists (wrong password, or
        already used by someone else). found=True with days=None means
        unlimited access was granted via this invite."""
        with self._lock:
            entry = self._invites.get(password)
            if not entry or entry.get("used_by") is not None:
                return False, None
            entry["used_by"] = str(chat_id)
            entry["used_at"] = datetime.now(timezone.utc).isoformat()
            days = entry.get("days")
        self._save_invites()
        return True, days

    def list_unused_invites(self) -> Dict[str, dict]:
        with self._lock:
            return {p: dict(e) for p, e in self._invites.items() if e.get("used_by") is None}
