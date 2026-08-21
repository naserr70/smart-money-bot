"""GitHub-backed persistence for rolling candle JSON files."""

import base64
import hashlib
import json
import logging
import threading
import time
from typing import Dict, Optional

import requests

log = logging.getLogger("smart_money_bot.github_backup")


class GitHubCandleBackup:
    def __init__(self, session: requests.Session, repo: str, token: str,
                 branch: str = "main", root_path: str = "market_history",
                 timeout: int = 20, max_retries: int = 3):
        self.session = session
        self.repo = (repo or "").strip()
        self.token = (token or "").strip()
        self.branch = (branch or "main").strip()
        self.root_path = (root_path or "market_history").strip("/")
        self.timeout = max(1, int(timeout))
        self.max_retries = max(0, int(max_retries))
        self._lock = threading.RLock()
        self._pending: Dict[str, str] = {}
        self._committed_hashes: Dict[str, str] = {}
        self._worker_started = False
        self._last_backup_ok = None
        self._last_backup_at = None
        self._last_error = ""
        self._last_commit_sha = ""

    def is_configured(self) -> bool:
        return bool(self.repo and self.token and "/" in self.repo)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "SmartMoneyBot/2.0",
        }

    def _url(self, suffix: str) -> str:
        return f"https://api.github.com/repos/{self.repo}/{suffix.lstrip('/')}"

    @staticmethod
    def _path(source: str, symbol: str, root_path: str) -> str:
        safe = "".join(c for c in symbol if c.isalnum() or c in ("_", "-"))
        return f"{root_path}/{source}/{safe}.json"

    @staticmethod
    def _semantic_content(content: str) -> str:
        try:
            payload = json.loads(content)
            if isinstance(payload, dict):
                payload = dict(payload)
                payload.pop("updated_at", None)
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError):
            return content

    @classmethod
    def _hash(cls, content: str) -> str:
        return hashlib.sha256(cls._semantic_content(content).encode("utf-8")).hexdigest()

    def queue(self, source: str, symbol: str, payload: dict) -> bool:
        if not self.is_configured() or not source or not symbol or not isinstance(payload, dict):
            return False
        path = self._path(source, symbol, self.root_path)
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        digest = self._hash(content)
        with self._lock:
            if self._committed_hashes.get(path) == digest:
                return False
            previous = self._pending.get(path)
            if previous is not None and self._hash(previous) == digest:
                return False
            self._pending[path] = content
        return True

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def _request(self, method: str, url: str, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("headers", self._headers())
        last = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(method, url, **kwargs)
                if response.status_code not in (429, 500, 502, 503, 504):
                    return response
                last = response
            except requests.RequestException as exc:
                last = exc
            if attempt < self.max_retries:
                time.sleep(min(2 ** attempt, 8))
        if isinstance(last, requests.Response):
            return last
        raise last if isinstance(last, Exception) else RuntimeError("GitHub request failed")

    def download(self, source: str, symbol: str) -> Optional[dict]:
        if not self.is_configured():
            return None
        path = self._path(source, symbol, self.root_path)
        response = self._request("GET", self._url(f"contents/{path}"), params={"ref": self.branch})
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            log.warning("GITHUB DOWNLOAD FAILED | path=%s status=%s", path, response.status_code)
            return None
        try:
            data = response.json()
            encoded = data.get("content", "").replace("\n", "")
            return json.loads(base64.b64decode(encoded).decode("utf-8"))
        except (ValueError, TypeError, KeyError, base64.binascii.Error, UnicodeDecodeError) as exc:
            log.warning("GITHUB DOWNLOAD INVALID | path=%s error=%s", path, exc)
            return None

    def backup(self) -> bool:
        if not self.is_configured():
            return False
        with self._lock:
            if not self._pending:
                self._last_backup_ok = True
                self._last_backup_at = time.time()
                return True
            items = list(self._pending.items())[:20]
        try:
            ref = self._request("GET", self._url(f"git/ref/heads/{self.branch}"))
            if ref.status_code != 200:
                raise RuntimeError(f"ref lookup HTTP {ref.status_code}: {ref.text[:200]}")
            head_sha = ref.json()["object"]["sha"]
            commit = self._request("GET", self._url(f"git/commits/{head_sha}"))
            if commit.status_code != 200:
                raise RuntimeError(f"commit lookup HTTP {commit.status_code}: {commit.text[:200]}")
            base_tree = commit.json()["tree"]["sha"]

            tree_entries = []
            for path, content in items:
                blob = self._request("POST", self._url("git/blobs"), json={"content": content, "encoding": "utf-8"})
                if blob.status_code not in (200, 201):
                    raise RuntimeError(f"blob create HTTP {blob.status_code}: {blob.text[:200]}")
                tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob.json()["sha"]})

            tree = self._request("POST", self._url("git/trees"), json={"base_tree": base_tree, "tree": tree_entries})
            if tree.status_code not in (200, 201):
                raise RuntimeError(f"tree create HTTP {tree.status_code}: {tree.text[:200]}")
            tree_sha = tree.json()["sha"]

            new_commit = self._request("POST", self._url("git/commits"), json={
                "message": f"chore: persist candle history ({len(items)} files)",
                "tree": tree_sha,
                "parents": [head_sha],
            })
            if new_commit.status_code not in (200, 201):
                raise RuntimeError(f"commit create HTTP {new_commit.status_code}: {new_commit.text[:200]}")
            commit_sha = new_commit.json()["sha"]

            update = self._request("PATCH", self._url(f"git/refs/heads/{self.branch}"), json={"sha": commit_sha, "force": False})
            if update.status_code != 200:
                raise RuntimeError(f"ref update HTTP {update.status_code}: {update.text[:200]}")

            with self._lock:
                for path, content in items:
                    if self._pending.get(path) == content:
                        self._pending.pop(path, None)
                    self._committed_hashes[path] = self._hash(content)
                self._last_commit_sha = commit_sha
                self._last_backup_ok = True
                self._last_backup_at = time.time()
                self._last_error = ""
            log.info("GITHUB BACKUP OK | files=%s commit=%s pending=%s", len(items), commit_sha[:12], self.pending_count())
            return True
        except Exception as exc:
            with self._lock:
                self._last_backup_ok = False
                self._last_backup_at = time.time()
                self._last_error = str(exc)
            log.exception("GITHUB BACKUP FAILED")
            return False

    def start_background_loop(self, interval_sec: int = 300) -> None:
        interval_sec = max(30, int(interval_sec))
        with self._lock:
            if self._worker_started:
                return
            self._worker_started = True

        def worker():
            while True:
                time.sleep(interval_sec)
                try:
                    self.backup()
                except Exception:
                    log.exception("GITHUB BACKUP WORKER ERROR")

        threading.Thread(target=worker, daemon=True, name="github-candle-backup").start()

    def status(self) -> dict:
        with self._lock:
            return {
                "configured": self.is_configured(),
                "branch": self.branch,
                "pending": len(self._pending),
                "worker_started": self._worker_started,
                "last_backup_ok": self._last_backup_ok,
                "last_backup_at": self._last_backup_at,
                "last_commit_sha": self._last_commit_sha,
                "last_error": self._last_error,
            }
