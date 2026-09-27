"""
Access control for the Telegram bot: password-gated entry, with the admin
(ADMIN_CHAT_ID / defaults to CHAT_ID) able to set a specific access duration
per user — the "give the bot to someone for a set amount of time"
requirement, plus admin-defined unique per-user invite passwords.

Persistence
-----------
Render's free web-service plan wipes local disk on every restart/spin-down,
so the state (users, invites, admin flags such as the message on/off
switches) lives in one remote copy plus a shared local file:

    1. The GitHub repository — when GITHUB_TOKEN + GITHUB_REPO are set (same
       credentials as the candle backup). The file is ENCRYPTED because the
       repo can be public and the state contains chat IDs and invite
       passwords (key from USERS_ENCRYPTION_KEY, else BOT_TOKEN).
    2. A GitHub Gist — only when the repository can't be used. If both are
       configured, the Gist's content is imported into the repo once.
    3. Local file only.

Several processes share this state: under gunicorn with --preload (Render's
default GUNICORN_CMD_ARGS) the signal loops run in the master process while
Telegram webhooks are handled in forked worker processes, each with its own
copy in memory. Every change is written to the shared local file and every
process re-reads that file when it changes, so a switch flipped in the menu
reaches the process that sends the messages within seconds. Remote saves
merge only this process's own pending changes onto the latest remote copy,
so concurrent writers never undo each other.

This module is intentionally UI-agnostic: it only tracks state and answers
"is this chat_id currently allowed in?". The actual Telegram command parsing
lives in bot_commands.py.
"""
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests

log = logging.getLogger("smart_money_bot.access_control")

GIST_API_BASE = "https://api.github.com/gists"
GIST_FILENAME = "smart_money_bot_access.json"
GIST_LOAD_ATTEMPTS = 3

GITHUB_API_BASE = "https://api.github.com"
USERS_GITHUB_DEFAULT_PATH = "bot_data/access_state.enc.json"
STATE_FILE_FORMAT = "hmac-sha256-ctr-v1"
# Render auto-deploys on pushes to the tracked branch; state commits must not.
STATE_COMMIT_MESSAGE = "[skip render] chore: persist bot access state (encrypted)"

SECTIONS = ("users", "invites", "flags")

# Each process re-checks the shared local file at most this often (cheap stat).
LOCAL_REFRESH_MIN_INTERVAL_SEC = 2.0
# Background check of the remote copy: rarely when in sync (conditional GET,
# ~1 KB), more often while unsynced or with changes still waiting to upload.
REMOTE_REFRESH_INTERVAL_SEC = 900.0
REMOTE_RETRY_INTERVAL_SEC = 60.0
REMOTE_SAVE_ATTEMPTS = 3
REMOTE_TIMEOUT_SEC = 10

_SIGNAL_DELIVERY_STATE = {
    "smart_money": True,
    "whale": True,
    "pump_dump": True,
    "status_report": True,
}

# The AccessControl of this process, so signal_delivery_enabled() (used by the
# Telegram notifier) always answers from the latest shared state.
_ACTIVE_INSTANCE = None

_MISSING = object()


def signal_delivery_enabled(category: str) -> bool:
    instance = _ACTIVE_INSTANCE
    if instance is not None:
        try:
            instance.refresh()
        except Exception:
            log.exception("ACCESS STATE REFRESH FAILED")
    return bool(_SIGNAL_DELIVERY_STATE.get(category, True))


def _parse_expiry(expires_at) -> Optional[datetime]:
    """Parse a stored ISO timestamp as an aware UTC datetime.

    Naive timestamps (older/hand-edited state) are treated as UTC instead of
    raising TypeError when compared with an aware "now" — that exception used
    to break broadcast_targets() and with it every signal delivery.
    Returns None for unparseable values (callers treat those as expired).
    """
    try:
        parsed = datetime.fromisoformat(str(expires_at))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_unexpired(expires_at, now: Optional[datetime] = None) -> bool:
    if expires_at is None:
        return True
    parsed = _parse_expiry(expires_at)
    if parsed is None:
        log.warning("تاریخ انقضای نامعتبر در لیست کاربران: %r (منقضی در نظر گرفته شد)", expires_at)
        return False
    return parsed > (now or datetime.now(timezone.utc))


def _empty_state() -> dict:
    return {section: {} for section in SECTIONS}


def _sanitize_state(data) -> dict:
    state = _empty_state()
    if isinstance(data, dict):
        for section in SECTIONS:
            value = data.get(section) or {}
            if isinstance(value, dict):
                state[section] = dict(value)
    return state


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text) -> bytes:
    return base64.b64decode(str(text).encode("ascii"), validate=True)


class _StateCipher:
    """Authenticated encryption using only the standard library.

    HMAC-SHA256 as a PRF in counter mode produces the keystream, and an
    independent HMAC-SHA256 key authenticates nonce + ciphertext
    (encrypt-then-MAC). No third-party package is needed, so storage can
    never be silently disabled by a missing dependency on the server.
    """

    NONCE_BYTES = 16

    def __init__(self, secret: str):
        master = hashlib.sha256(f"smart-money-bot/access-state/v2:{secret}".encode("utf-8")).digest()
        self._enc_key = hmac.new(master, b"encrypt", hashlib.sha256).digest()
        self._mac_key = hmac.new(master, b"authenticate", hashlib.sha256).digest()

    def _xor_keystream(self, nonce: bytes, data: bytes) -> bytes:
        if not data:
            return b""
        blocks = []
        for counter in range((len(data) + 31) // 32):
            blocks.append(hmac.new(self._enc_key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        keystream = b"".join(blocks)[:len(data)]
        return (int.from_bytes(data, "big") ^ int.from_bytes(keystream, "big")).to_bytes(len(data), "big")

    def _tag(self, nonce: bytes, ciphertext: bytes) -> bytes:
        return hmac.new(self._mac_key, STATE_FILE_FORMAT.encode("ascii") + nonce + ciphertext,
                        hashlib.sha256).digest()

    def encrypt(self, plain: bytes) -> dict:
        nonce = os.urandom(self.NONCE_BYTES)
        ciphertext = self._xor_keystream(nonce, plain)
        return {"nonce": _b64(nonce), "data": _b64(ciphertext), "tag": _b64(self._tag(nonce, ciphertext))}

    def decrypt(self, envelope: dict) -> bytes:
        nonce = _unb64(envelope["nonce"])
        ciphertext = _unb64(envelope["data"])
        tag = _unb64(envelope["tag"])
        if len(nonce) != self.NONCE_BYTES or not hmac.compare_digest(tag, self._tag(nonce, ciphertext)):
            raise ValueError("authentication failed")
        return self._xor_keystream(nonce, ciphertext)


class GitHubRepoStateStore:
    """Encrypted access-state file inside the GitHub repository.

    Uses the Contents API (one small file, one commit per change, optimistic
    concurrency through the file's blob sha). Reads go through the API, not
    raw.githubusercontent.com, whose CDN can serve a minutes-old copy.
    """

    label = "GitHub (رمزنگاری‌شده)"

    def __init__(self, session: requests.Session, repo: str, token: str, secret: str,
                 branch: str = "main", path: str = USERS_GITHUB_DEFAULT_PATH,
                 timeout: int = REMOTE_TIMEOUT_SEC):
        self.session = session
        self.repo = (repo or "").strip()
        self.token = (token or "").strip()
        self.branch = (branch or "main").strip()
        self.path = (path or USERS_GITHUB_DEFAULT_PATH).strip("/")
        self.timeout = max(1, int(timeout))
        self._cipher = _StateCipher(secret)
        self._sha: Optional[str] = None
        self._etag: Optional[str] = None
        self._remote_digest: Optional[str] = None
        self.last_error = ""

    @classmethod
    def build(cls, session: requests.Session, repo: str, token: str, secret: str,
              branch: str = "main", path: str = USERS_GITHUB_DEFAULT_PATH) -> Optional["GitHubRepoStateStore"]:
        if not (repo and "/" in repo and token):
            return None
        if not secret:
            log.warning("ذخیره‌ی کاربران روی GitHub غیرفعال است: کلید رمزنگاری "
                        "(USERS_ENCRYPTION_KEY یا BOT_TOKEN) تنظیم نشده.")
            return None
        return cls(session, repo, token, secret, branch=branch, path=path)

    @staticmethod
    def _digest(state: dict) -> str:
        return hashlib.sha256(json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def _url(self) -> str:
        return f"{GITHUB_API_BASE}/repos/{self.repo}/contents/{self.path}"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "SmartMoneyBot/2.0",
        }

    def fetch(self, conditional: bool = False) -> Tuple[str, Optional[dict]]:
        """("ok", state) | ("missing", {}) | ("not_modified", None) | ("error", None).

        On "error" callers must NOT overwrite the remote file.
        """
        headers = self._headers()
        if conditional and self._etag:
            headers["If-None-Match"] = self._etag
        try:
            res = self.session.get(self._url(), params={"ref": self.branch}, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            self.last_error = f"اتصال: {e}"
            log.warning(f"خطا در خواندن لیست کاربران از GitHub: {e}")
            return "error", None
        if res.status_code == 304:
            return "not_modified", None
        if res.status_code == 404:
            self._sha = None
            self._etag = None
            self._remote_digest = None
            return "missing", {}
        if res.status_code != 200:
            self.last_error = f"HTTP {res.status_code}"
            log.warning(f"خواندن لیست کاربران از GitHub خطا داد: {res.status_code} {res.text[:200]}")
            return "error", None
        try:
            meta = res.json()
            envelope = json.loads(base64.b64decode((meta.get("content") or "").replace("\n", "")).decode("utf-8"))
            if envelope.get("format") != STATE_FILE_FORMAT:
                self.last_error = "فرمت ناشناخته"
                log.error("فایل کاربران روی GitHub فرمت ناشناخته دارد؛ روی آن نوشته نمی‌شود.")
                return "error", None
            data = json.loads(self._cipher.decrypt(envelope).decode("utf-8"))
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError, binascii.Error) as e:
            self.last_error = "رمزگشایی ناموفق (کلید عوض شده؟)"
            log.error("رمزگشایی/خواندن لیست کاربران از GitHub ناموفق بود (%s). اگر BOT_TOKEN را تغییر داده‌اید، "
                      "مقدار قبلی را در USERS_ENCRYPTION_KEY بگذارید. روی فایل چیزی نوشته نمی‌شود.", e)
            return "error", None
        state = _sanitize_state(data)
        self._sha = meta.get("sha")
        self._etag = (getattr(res, "headers", None) or {}).get("ETag")
        self._remote_digest = self._digest(state)
        self.last_error = ""
        return "ok", state

    def save(self, state: dict) -> Tuple[str, str]:
        """("ok" | "conflict" | "error", detail)."""
        digest = self._digest(state)
        if digest == self._remote_digest:
            return "ok", "unchanged"
        plain = json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8")
        envelope = {
            "format": STATE_FILE_FORMAT,
            "note": "Smart Money Bot access state (users, invites, admin settings). Encrypted; "
                    "the key is derived from USERS_ENCRYPTION_KEY or BOT_TOKEN.",
            **self._cipher.encrypt(plain),
        }
        body = {
            "message": STATE_COMMIT_MESSAGE,
            "content": _b64(json.dumps(envelope, indent=2).encode("utf-8")),
            "branch": self.branch,
        }
        if self._sha:
            body["sha"] = self._sha
        try:
            res = self.session.put(self._url(), json=body, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as e:
            self.last_error = f"اتصال: {e}"
            log.warning(f"خطا در ذخیره‌ی لیست کاربران روی GitHub: {e}")
            return "error", self.last_error
        if res.status_code in (200, 201):
            try:
                self._sha = res.json()["content"]["sha"]
            except (ValueError, KeyError, TypeError):
                self._sha = None
            self._etag = None
            self._remote_digest = digest
            self.last_error = ""
            return "ok", ""
        if res.status_code in (409, 422):
            return "conflict", f"HTTP {res.status_code}"
        self.last_error = f"HTTP {res.status_code}"
        if res.status_code in (401, 403, 404):
            self.last_error += " (دسترسی GITHUB_TOKEN به ریپو را بررسی کنید)"
        log.warning(f"ذخیره‌ی لیست کاربران روی GitHub خطا داد: {res.status_code} {res.text[:200]}")
        return "error", self.last_error


class GistStateStore:
    """Plain-JSON state in a GitHub Gist (legacy backend)."""

    label = "GitHub Gist"

    def __init__(self, session: requests.Session, gist_id: str, token: str, timeout: int = REMOTE_TIMEOUT_SEC):
        self.session = session
        self.gist_id = gist_id
        self.token = token
        self.timeout = timeout
        self._etag: Optional[str] = None
        self.last_error = ""

    def _headers(self) -> dict:
        return {"Authorization": f"token {self.token}", "Accept": "application/vnd.github+json"}

    def fetch(self, conditional: bool = False) -> Tuple[str, Optional[dict]]:
        headers = self._headers()
        if conditional and self._etag:
            headers["If-None-Match"] = self._etag
        try:
            res = self.session.get(f"{GIST_API_BASE}/{self.gist_id}", headers=headers, timeout=self.timeout)
            if res.status_code == 304:
                return "not_modified", None
            if res.status_code != 200:
                self.last_error = f"HTTP {res.status_code}"
                log.warning(f"GitHub Gist GET خطا داد: {res.status_code} {res.text[:200]}")
                return "error", None
            files = res.json().get("files", {})
            file_entry = files.get(GIST_FILENAME)
            self._etag = (getattr(res, "headers", None) or {}).get("ETag")
            if not file_entry:
                return "missing", {}
            data = json.loads(file_entry.get("content") or "{}")
            if not isinstance(data, dict):
                self.last_error = "ساختار نامعتبر"
                log.warning("محتوای GitHub Gist ساختار معتبری ندارد.")
                return "error", None
            self.last_error = ""
            return "ok", _sanitize_state(data)
        except (requests.RequestException, ValueError, AttributeError) as e:
            self.last_error = f"اتصال: {e}"
            log.warning(f"خطا در خواندن GitHub Gist: {e}")
            return "error", None

    def save(self, state: dict) -> Tuple[str, str]:
        content = json.dumps(state, ensure_ascii=False, indent=2)
        payload = {"files": {GIST_FILENAME: {"content": content}}}
        try:
            res = self.session.patch(f"{GIST_API_BASE}/{self.gist_id}", headers=self._headers(),
                                     json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            self.last_error = f"اتصال: {e}"
            log.warning(f"خطا در نوشتن روی GitHub Gist: {e}")
            return "error", self.last_error
        if res.status_code != 200:
            self.last_error = f"HTTP {res.status_code}"
            log.warning(f"GitHub Gist PATCH خطا داد: {res.status_code} {res.text[:200]}")
            return "error", self.last_error
        self._etag = None
        self.last_error = ""
        return "ok", ""


class AccessControl:
    SIGNAL_CONTROL_DEFAULTS = {
        "smart_money": True,
        "whale": True,
        "pump_dump": True,
        # Periodic "📡 وضعیت رصد" message sent every market cycle.
        "status_report": True,
    }

    def __init__(self, state_file_path: str, admin_chat_id: str,
                 gist_id: str = "", gist_token: str = "", http_session: requests.Session = None,
                 github_repo: str = "", github_token: str = "", github_branch: str = "main",
                 github_path: str = USERS_GITHUB_DEFAULT_PATH, encryption_secret: str = ""):
        global _ACTIVE_INSTANCE
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()
        self._state_file_path = state_file_path
        self.admin_chat_id = str(admin_chat_id) if admin_chat_id else ""
        self._gist_id = gist_id
        self._gist_token = gist_token
        self._http = http_session or requests.Session()

        gist_store = GistStateStore(self._http, gist_id, gist_token) if (gist_id and gist_token) else None
        repo_store = GitHubRepoStateStore.build(
            self._http, github_repo, github_token, encryption_secret,
            branch=github_branch, path=github_path,
        )
        # The repository is the primary store whenever it can be used; an
        # existing Gist is then only imported once (when the repo file is new).
        self._remote = repo_store or gist_store
        self._migration_source = gist_store if (repo_store and gist_store) else None

        self._users: Dict[str, dict] = {}
        self._invites: Dict[str, dict] = {}
        # Permanent flags that must survive restarts (e.g. first-start announce
        # and the admin's per-category Telegram signal delivery controls).
        self._flags: Dict[str, bool] = {}
        # This process's changes not yet confirmed in the remote copy
        # (None = deleted). Merged onto the latest remote/local state so
        # concurrent writers never undo each other.
        self._pending: Dict[str, dict] = _empty_state()
        # True once the remote state was read successfully. Until then we must
        # never write the remote copy, or a transient read error at startup
        # would overwrite every stored user with an empty list.
        self._remote_synced = False
        self._local_sig = None
        self._last_local_check = 0.0
        self._last_save_ok: Optional[bool] = None
        self._last_save_at: Optional[float] = None
        self._last_save_error = ""
        self._refresher_pid: Optional[int] = None

        self._load()
        self._sync_signal_delivery_state()
        _ACTIVE_INSTANCE = self

    # ==================== state helpers ====================

    def _state_snapshot(self) -> dict:
        with self._lock:
            return {
                "users": dict(self._users),
                "invites": dict(self._invites),
                "flags": dict(self._flags),
            }

    def _apply_base_state(self, data) -> None:
        """State = base (remote/local copy) + this process's pending changes."""
        base = _sanitize_state(data)
        with self._lock:
            for section in SECTIONS:
                for key, value in self._pending[section].items():
                    if value is None:
                        base[section].pop(key, None)
                    else:
                        base[section][key] = value
            self._users = base["users"]
            self._invites = base["invites"]
            self._flags = base["flags"]
        self._sync_signal_delivery_state()

    def _apply_loaded_state(self, data: dict) -> None:
        self._apply_base_state(data)

    def _set(self, section: str, key: str, value) -> None:
        """Change one entry (None deletes it) and remember it as pending."""
        with self._lock:
            target = getattr(self, f"_{section}")
            if value is None:
                target.pop(key, None)
            else:
                target[key] = value
            self._pending[section][key] = value

    def _has_pending(self) -> bool:
        with self._lock:
            return any(self._pending[section] for section in SECTIONS)

    # ==================== persistence backends ====================

    def _gist_enabled(self) -> bool:
        return bool(self._gist_id and self._gist_token)

    def _remote_backend(self) -> Optional[str]:
        if self._remote is None:
            return None
        return "repo" if isinstance(self._remote, GitHubRepoStateStore) else "gist"

    def storage_label(self) -> str:
        if self._remote is None:
            return "محلی (با ری‌استارت پاک می‌شود)"
        return self._remote.label

    def storage_status_text(self) -> str:
        """One line for the admin status page."""
        label = self.storage_label()
        if self._remote is None:
            return f"⚠️ {label}"
        with self._lock:
            ok, error = self._last_save_ok, self._last_save_error
        if not self._remote_synced:
            return f"⚠️ {label} — خوانده نشد: {self._remote.last_error or 'نامشخص'}"
        if ok is False or self._has_pending():
            return f"⚠️ {label} — آخرین ذخیره ناموفق: {error or 'در انتظار'}"
        return f"✅ {label}"

    def persistence_warning(self) -> str:
        """Non-empty when the latest change could not be saved remotely."""
        if self._remote is None:
            return ("⚠️ ذخیره‌ی آنلاین تنظیم نشده (GITHUB_TOKEN و GITHUB_REPO)؛ "
                    "این تغییر با ری‌استارت بعدی از بین می‌رود.")
        with self._lock:
            if self._last_save_ok is not False:
                return ""
            error = self._last_save_error
        return (f"⚠️ ذخیره روی {self.storage_label()} ناموفق بود ({error}). "
                "تغییر اعمال شده و خودکار دوباره ذخیره می‌شود؛ تا آن موقع با ری‌استارت ممکن است از بین برود.")

    def _record_save(self, ok: bool, error: str = "") -> None:
        with self._lock:
            self._last_save_ok = ok
            self._last_save_at = time.time()
            self._last_save_error = error

    def _load(self) -> None:
        if self._remote is not None:
            label = self.storage_label()
            status, data = "error", None
            for attempt in range(GIST_LOAD_ATTEMPTS):
                status, data = self._remote.fetch()
                if status in ("ok", "missing"):
                    break
                if attempt < GIST_LOAD_ATTEMPTS - 1:
                    time.sleep(2 * (attempt + 1))
            if status in ("ok", "missing"):
                self._remote_synced = True
                seed = self._initial_seed() if status == "missing" else None
                if seed:
                    # First use of this storage: carry the existing users over.
                    with self._lock:
                        for section in SECTIONS:
                            self._pending[section].update(seed[section])
                    self._apply_base_state({})
                    self._push_remote()
                else:
                    self._apply_base_state(data)
                self._save_local()
                log.info(f"{len(self._users)} کاربر و {len(self._invites)} رمز دعوت از {label} بازیابی شد.")
                return
            log.warning(f"بازیابی از {label} ناموفق بود؛ به فایل محلی برمی‌گردم (ممکن است خالی باشد). "
                        "تا وقتی نسخه‌ی آنلاین دوباره خوانده نشود، روی آن چیزی نوشته نمی‌شود.")
        self._load_local()

    def _initial_seed(self) -> Optional[dict]:
        """Existing state to import when the remote file doesn't exist yet."""
        if self._migration_source is not None:
            status, data = self._migration_source.fetch()
            if status == "ok" and any(data[section] for section in SECTIONS):
                log.info("لیست کاربران از GitHub Gist به فایل رمزنگاری‌شده‌ی ریپو منتقل می‌شود.")
                return data
        local = self._read_local_file()
        if local is not None and any(local[1][section] for section in SECTIONS):
            return local[1]
        return None

    def _persist(self) -> bool:
        """Save locally (shared with the other processes) and remotely."""
        self._save_local()
        if self._remote is None:
            with self._lock:
                self._pending = _empty_state()
            return True
        ok = self._push_remote()
        if ok:
            self._save_local()
        return ok

    def _push_remote(self) -> bool:
        with self._save_lock:
            need_fetch = not self._remote_synced
            for _ in range(REMOTE_SAVE_ATTEMPTS):
                if need_fetch:
                    status, data = self._remote.fetch()
                    if status == "error":
                        self._record_save(False, self._remote.last_error or "خواندن نسخه‌ی آنلاین")
                        log.warning("نسخه‌ی آنلاین لیست کاربران خوانده نشد؛ برای جلوگیری از پاک شدن کاربران، "
                                    "فعلاً فقط در فایل محلی ذخیره شد.")
                        return False
                    if status in ("ok", "missing"):
                        self._apply_base_state(data)
                    self._remote_synced = True
                with self._lock:
                    snapshot = self._state_snapshot()
                    sent = {section: dict(self._pending[section]) for section in SECTIONS}
                result, detail = self._remote.save(snapshot)
                if result == "ok":
                    with self._lock:
                        for section in SECTIONS:
                            for key, value in sent[section].items():
                                if self._pending[section].get(key, _MISSING) == value:
                                    self._pending[section].pop(key, None)
                    self._record_save(True)
                    return True
                if result == "conflict":
                    need_fetch = True  # someone else saved meanwhile: merge onto theirs
                    continue
                self._record_save(False, detail)
                return False
            self._record_save(False, "تداخل هم‌زمان")
            return False

    # ---------- shared local file ----------

    @staticmethod
    def _file_signature(path: str):
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def _read_local_file(self):
        """(signature, state) or None."""
        path = self._state_file_path
        if not path:
            return None
        sig = self._file_signature(path)
        if sig is None:
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log.warning(f"خواندن فایل محلی کاربران مجاز ناموفق بود: {e}")
            return None
        if not isinstance(data, dict):
            log.warning("فایل محلی کاربران مجاز ساختار معتبری ندارد.")
            return None
        return sig, _sanitize_state(data)

    def _load_local(self) -> None:
        loaded = self._read_local_file()
        if loaded is None:
            return
        self._local_sig = loaded[0]
        self._apply_base_state(loaded[1])
        log.info(f"{len(self._users)} کاربر و {len(self._invites)} رمز دعوت از فایل محلی بازیابی شد.")

    def _save_local(self) -> None:
        path = self._state_file_path
        if not path:
            return
        payload = self._state_snapshot()
        # Unique temp name: several processes may save at the same time.
        tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            sig = self._file_signature(tmp_path)
            os.replace(tmp_path, path)
            with self._lock:
                self._local_sig = sig
        except OSError as e:
            log.warning(f"ذخیره‌ی لیست کاربران مجاز در فایل محلی ناموفق بود: {e}")
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    # ---------- keeping every process up to date ----------

    def refresh(self, force_local: bool = False) -> None:
        """Pick up changes made by other processes (cheap; throttled)."""
        self._ensure_remote_refresher()
        now = time.time()
        if not force_local and now - self._last_local_check < LOCAL_REFRESH_MIN_INTERVAL_SEC:
            return
        self._last_local_check = now
        path = self._state_file_path
        if not path:
            return
        sig = self._file_signature(path)
        with self._lock:
            unchanged = sig is None or sig == self._local_sig
        if unchanged:
            return
        loaded = self._read_local_file()
        if loaded is None:
            return
        with self._lock:
            self._local_sig = loaded[0]
        self._apply_base_state(loaded[1])

    def _ensure_remote_refresher(self) -> None:
        """One background thread per process (forked workers start their own)."""
        if self._remote is None or self._refresher_pid == os.getpid():
            return
        with self._lock:
            if self._refresher_pid == os.getpid():
                return
            self._refresher_pid = os.getpid()
        threading.Thread(target=self._remote_refresh_loop, daemon=True, name="access-state-sync").start()

    def _remote_refresh_loop(self) -> None:
        while True:
            waiting = (not self._remote_synced) or self._has_pending()
            time.sleep(REMOTE_RETRY_INTERVAL_SEC if waiting else REMOTE_REFRESH_INTERVAL_SEC)
            try:
                self._remote_refresh_once()
            except Exception:
                log.exception("ACCESS STATE REMOTE SYNC FAILED")

    def _remote_refresh_once(self) -> None:
        if self._remote is None:
            return
        self.refresh(force_local=True)
        if self._has_pending() or not self._remote_synced:
            # Changes that couldn't be uploaded earlier (GitHub was down),
            # or the startup read failed: fetch, merge and upload.
            if self._push_remote():
                self._save_local()
            return
        status, data = self._remote.fetch(conditional=True)
        if status == "ok":
            before = self._state_snapshot()
            self._apply_base_state(data)
            if self._state_snapshot() != before:
                self._save_local()

    # ==================== permanent flags ====================

    def is_startup_announced(self) -> bool:
        self.refresh()
        with self._lock:
            return bool(self._flags.get("startup_announced"))

    def mark_startup_announced(self) -> None:
        self.refresh(force_local=True)
        self._set("flags", "startup_announced", True)
        self._persist()

    # ==================== Telegram signal delivery controls ====================

    def _sync_signal_delivery_state(self) -> None:
        with self._lock:
            flags = dict(self._flags)
        for category, default in self.SIGNAL_CONTROL_DEFAULTS.items():
            _SIGNAL_DELIVERY_STATE[category] = bool(flags.get(f"signal_{category}", default))

    def is_signal_enabled(self, category: str) -> bool:
        self.refresh()
        default = self.SIGNAL_CONTROL_DEFAULTS.get(category, True)
        with self._lock:
            return bool(self._flags.get(f"signal_{category}", default))

    def set_signal_enabled(self, category: str, enabled: bool) -> bool:
        if category not in self.SIGNAL_CONTROL_DEFAULTS:
            raise ValueError(f"unknown signal category: {category}")
        enabled = bool(enabled)
        self.refresh(force_local=True)
        self._set("flags", f"signal_{category}", enabled)
        self._sync_signal_delivery_state()
        self._persist()
        return enabled

    def toggle_signal(self, category: str) -> bool:
        self.refresh(force_local=True)
        return self.set_signal_enabled(category, not self.is_signal_enabled(category))

    def set_all_signals(self, enabled: bool) -> None:
        """Turn every message category on/off with a single save."""
        enabled = bool(enabled)
        self.refresh(force_local=True)
        for category in self.SIGNAL_CONTROL_DEFAULTS:
            self._set("flags", f"signal_{category}", enabled)
        self._sync_signal_delivery_state()
        self._persist()

    def signal_controls(self) -> Dict[str, bool]:
        return {category: self.is_signal_enabled(category) for category in self.SIGNAL_CONTROL_DEFAULTS}

    # ==================== users ====================

    def is_admin(self, chat_id) -> bool:
        return bool(self.admin_chat_id) and str(chat_id) == self.admin_chat_id

    def is_authorized(self, chat_id) -> bool:
        chat_id = str(chat_id)
        if self.is_admin(chat_id):
            return True
        self.refresh()
        with self._lock:
            entry = self._users.get(chat_id)
        if not entry:
            return False
        return _is_unexpired(entry.get("expires_at"))

    def expiry_text(self, chat_id) -> str:
        chat_id = str(chat_id)
        if self.is_admin(chat_id):
            return "نامحدود (ادمین)"
        self.refresh()
        with self._lock:
            entry = self._users.get(chat_id)
        if not entry:
            return "دسترسی ندارید"
        expires_at = entry.get("expires_at")
        if expires_at is None:
            return "نامحدود"
        parsed = _parse_expiry(expires_at)
        if parsed is None:
            return "نامعتبر"
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def days_remaining(self, chat_id) -> Optional[float]:
        chat_id = str(chat_id)
        if self.is_admin(chat_id):
            return None
        self.refresh()
        with self._lock:
            entry = self._users.get(chat_id)
        if not entry:
            return None
        expires_at = entry.get("expires_at")
        if expires_at is None:
            return None
        parsed = _parse_expiry(expires_at)
        if parsed is None:
            return 0.0
        delta = parsed - datetime.now(timezone.utc)
        return max(delta.total_seconds() / 86400, 0.0)

    def get_entry(self, chat_id) -> Optional[dict]:
        self.refresh()
        with self._lock:
            entry = self._users.get(str(chat_id))
            return dict(entry) if entry else None

    def grant(self, chat_id, days: Optional[float], label: str = "") -> None:
        chat_id = str(chat_id)
        expires_at = None
        if days is not None:
            expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        self.refresh(force_local=True)
        self._set("users", chat_id, {
            "granted_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at,
            "label": label,
        })
        self._persist()

    def revoke(self, chat_id) -> bool:
        chat_id = str(chat_id)
        self.refresh(force_local=True)
        with self._lock:
            existed = chat_id in self._users
        if existed:
            self._set("users", chat_id, None)
            self._persist()
        return existed

    def active_chat_ids(self) -> List[str]:
        self.refresh()
        now = datetime.now(timezone.utc)
        result = []
        with self._lock:
            items = list(self._users.items())
        for chat_id, entry in items:
            if not isinstance(entry, dict):
                continue
            if _is_unexpired(entry.get("expires_at"), now):
                result.append(chat_id)
        return result

    def list_users(self) -> Dict[str, dict]:
        self.refresh()
        with self._lock:
            return dict(self._users)

    # ==================== per-user invite passwords ====================

    def create_invite(self, password: str, days: Optional[float], label: str = "") -> None:
        self.refresh(force_local=True)
        self._set("invites", password, {
            "days": days,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "label": label,
            "used_by": None,
            "used_at": None,
        })
        self._persist()

    def consume_invite(self, password: str, chat_id: str):
        self.refresh(force_local=True)
        with self._lock:
            entry = self._invites.get(password)
            if not isinstance(entry, dict) or entry.get("used_by") is not None:
                return False, None
            updated = dict(entry)
            updated["used_by"] = str(chat_id)
            updated["used_at"] = datetime.now(timezone.utc).isoformat()
            days = updated.get("days")
        self._set("invites", password, updated)
        self._persist()
        return True, days

    def list_unused_invites(self) -> Dict[str, dict]:
        self.refresh()
        with self._lock:
            return {p: dict(e) for p, e in self._invites.items()
                    if isinstance(e, dict) and e.get("used_by") is None}
