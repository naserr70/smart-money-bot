"""
GitHub Candle Backup
====================

Persistent rolling 5-minute candle backup using GitHub Git Trees API.

Storage:

    market_history/
        binance/
        bybit/
        kucoin/

IMPORTANT
---------
Binance, Bybit and KuCoin histories are completely independent.

Environment variables (primary):

    GITHUB_TOKEN
    GITHUB_REPO

Also accepted for backward compatibility:

    GITHUB_CANDLE_REPO
    GITHUB_GIST_TOKEN
"""

import base64
import hashlib
import json
import logging
import os
import threading
import time
from typing import Dict, Iterable, Optional, Tuple

import requests

log = logging.getLogger("smart_money_bot.github_candle_backup")


DEFAULT_BRANCH = "main"
DEFAULT_ROOT_PATH = "market_history"
DEFAULT_BACKUP_INTERVAL = 300
DEFAULT_MAX_RETRIES = 5

SOURCE_BINANCE = "binance"
SOURCE_BYBIT = "bybit"
SOURCE_KUCOIN = "kucoin"

VALID_SOURCES = {
    SOURCE_BINANCE,
    SOURCE_BYBIT,
    SOURCE_KUCOIN,
}


def _first_env(*keys: str, default: str = "") -> str:
    for key in keys:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return default


class GitHubCandleBackup:
    """
    GitHub persistence layer for rolling 5-minute candle history.

    One backup operation can contain many changed candle files but creates
    only ONE Git commit.

    Each exchange uses a separate directory.
    """

    def __init__(
        self,
        session: requests.Session,
        repo: Optional[str] = None,
        token: Optional[str] = None,
        branch: Optional[str] = None,
        root_path: Optional[str] = None,
        timeout: int = 15,
        max_retries: Optional[int] = None,
    ):
        self.session = session

        self.repo = (
            (repo or "").strip()
            or _first_env("GITHUB_REPO", "GITHUB_CANDLE_REPO")
        )

        self.token = (
            (token or "").strip()
            or _first_env("GITHUB_TOKEN", "GITHUB_GIST_TOKEN")
        )

        self.branch = (
            (branch or "").strip()
            or _first_env(
                "GITHUB_BRANCH",
                "GITHUB_CANDLE_BRANCH",
                default=DEFAULT_BRANCH,
            )
            or DEFAULT_BRANCH
        )

        self.root_path = (
            (root_path or "").strip()
            or _first_env(
                "GITHUB_CANDLE_PATH",
                default=DEFAULT_ROOT_PATH,
            )
            or DEFAULT_ROOT_PATH
        ).strip("/")

        self.timeout = max(5, int(timeout))

        if max_retries is not None:
            self.max_retries = max(1, int(max_retries))
        else:
            raw = _first_env(
                "GITHUB_MAX_RETRIES",
                "GITHUB_CANDLE_MAX_RETRIES",
                default=str(DEFAULT_MAX_RETRIES),
            )
            try:
                self.max_retries = max(1, int(raw))
            except (TypeError, ValueError):
                self.max_retries = DEFAULT_MAX_RETRIES

        self.api_base = "https://api.github.com"

        self._lock = threading.RLock()

        self._content_hashes: Dict[str, str] = {}
        self._pending: Dict[str, str] = {}

        self._last_backup_at: Optional[float] = None
        self._last_backup_ok = False
        self._last_error: Optional[str] = None

        self._backup_in_progress = False

    def is_configured(self) -> bool:
        return bool(self.repo and self.token)

    def status(self) -> dict:
        with self._lock:
            return {
                "configured": self.is_configured(),
                "repo": self.repo or "",
                "branch": self.branch,
                "root_path": self.root_path,
                "pending_files": len(self._pending),
                "backup_in_progress": self._backup_in_progress,
                "last_backup_at": self._last_backup_at,
                "last_backup_ok": self._last_backup_ok,
                "last_error": self._last_error,
            }

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "SmartMoneyBot-CandleBackup/2.0",
        }

    def _url(self, path: str) -> str:
        return f"{self.api_base}{path}"

    @staticmethod
    def _safe_symbol(symbol: str) -> str:
        return "".join(
            c
            for c in str(symbol)
            if c.isalnum()
            or c in ("_", "-", ".")
        )

    def _file_path(
        self,
        source: str,
        symbol: str,
    ) -> str:

        if source not in VALID_SOURCES:
            raise ValueError(
                f"Invalid candle source: {source}"
            )

        safe_symbol = self._safe_symbol(symbol)

        if not safe_symbol:
            raise ValueError(
                f"Invalid candle symbol: {symbol!r}"
            )

        return (
            f"{self.root_path}/"
            f"{source}/"
            f"{safe_symbol}.json"
        )

    @staticmethod
    def _hash_text(content: str) -> str:
        return hashlib.sha256(
            content.encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _normalize_payload(payload) -> str:
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def queue(
        self,
        source: str,
        symbol: str,
        payload,
    ) -> bool:

        if not self.is_configured():
            return False

        path = self._file_path(
            source,
            symbol,
        )

        content = self._normalize_payload(
            payload
        )

        content_hash = self._hash_text(
            content
        )

        with self._lock:
            old_hash = self._content_hashes.get(
                path
            )

            if old_hash == content_hash:
                return False

            self._pending[path] = content
            self._content_hashes[path] = content_hash

        return True

    def queue_raw(
        self,
        source: str,
        symbol: str,
        content: str,
    ) -> bool:

        if not self.is_configured():
            return False

        path = self._file_path(
            source,
            symbol,
        )

        content_hash = self._hash_text(
            content
        )

        with self._lock:
            old_hash = self._content_hashes.get(
                path
            )

            if old_hash == content_hash:
                return False

            self._pending[path] = content
            self._content_hashes[path] = content_hash

        return True

    def _get_branch(self) -> Optional[dict]:

        url = self._url(
            f"/repos/{self.repo}/branches/{self.branch}"
        )

        try:
            response = self.session.get(
                url,
                headers=self._headers(),
                timeout=self.timeout,
            )

        except requests.RequestException as exc:
            self._set_error(
                f"GET_BRANCH_NETWORK_ERROR: {exc}"
            )
            return None

        if response.status_code != 200:

            self._set_error(
                "GET_BRANCH_ERROR | "
                f"status={response.status_code} | "
                f"body={response.text[:500]}"
            )

            return None

        try:
            return response.json()

        except ValueError:

            self._set_error(
                "GET_BRANCH_ERROR | invalid JSON"
            )

            return None

    def _get_commit_tree_sha(
        self,
        commit_sha: str,
    ) -> Optional[str]:

        url = self._url(
            f"/repos/{self.repo}/git/commits/{commit_sha}"
        )

        try:
            response = self.session.get(
                url,
                headers=self._headers(),
                timeout=self.timeout,
            )

        except requests.RequestException as exc:

            self._set_error(
                f"GET_COMMIT_NETWORK_ERROR: {exc}"
            )

            return None

        if response.status_code != 200:

            self._set_error(
                "GET_COMMIT_ERROR | "
                f"status={response.status_code} | "
                f"body={response.text[:500]}"
            )

            return None

        try:
            data = response.json()

        except ValueError:

            self._set_error(
                "GET_COMMIT_ERROR | invalid JSON"
            )

            return None

        return (
            data
            .get("tree", {})
            .get("sha")
        )

    def _create_tree(
        self,
        base_tree_sha: str,
        files: Dict[str, str],
    ) -> Optional[str]:

        tree = []

        for path, content in files.items():

            tree.append(
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "content": content,
                }
            )

        payload = {
            "base_tree": base_tree_sha,
            "tree": tree,
        }

        url = self._url(
            f"/repos/{self.repo}/git/trees"
        )

        try:
            response = self.session.post(
                url,
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )

        except requests.RequestException as exc:

            self._set_error(
                f"CREATE_TREE_NETWORK_ERROR: {exc}"
            )

            return None

        if response.status_code not in (
            200,
            201,
        ):

            self._set_error(
                "CREATE_TREE_ERROR | "
                f"status={response.status_code} | "
                f"body={response.text[:500]}"
            )

            return None

        try:
            return response.json().get(
                "sha"
            )

        except ValueError:

            self._set_error(
                "CREATE_TREE_ERROR | invalid JSON"
            )

            return None

    def _create_commit(
        self,
        tree_sha: str,
        parent_sha: str,
    ) -> Optional[str]:

        timestamp = time.strftime(
            "%Y-%m-%d %H:%M:%S UTC",
            time.gmtime(),
        )

        payload = {
            "message": (
                "Update rolling 5m candle history "
                f"({timestamp})"
            ),
            "tree": tree_sha,
            "parents": [
                parent_sha
            ],
        }

        url = self._url(
            f"/repos/{self.repo}/git/commits"
        )

        try:
            response = self.session.post(
                url,
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )

        except requests.RequestException as exc:

            self._set_error(
                f"CREATE_COMMIT_NETWORK_ERROR: {exc}"
            )

            return None

        if response.status_code not in (
            200,
            201,
        ):

            self._set_error(
                "CREATE_COMMIT_ERROR | "
                f"status={response.status_code} | "
                f"body={response.text[:500]}"
            )

            return None

        try:
            return response.json().get(
                "sha"
            )

        except ValueError:

            self._set_error(
                "CREATE_COMMIT_ERROR | invalid JSON"
            )

            return None

    def _update_branch(
        self,
        commit_sha: str,
        expected_parent_sha: str,
    ) -> Tuple[bool, bool]:

        url = self._url(
            f"/repos/{self.repo}/git/refs/heads/{self.branch}"
        )

        payload = {
            "sha": commit_sha,
            "force": False,
        }

        try:
            response = self.session.patch(
                url,
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )

        except requests.RequestException as exc:

            self._set_error(
                f"UPDATE_REF_NETWORK_ERROR: {exc}"
            )

            return False, False

        if response.status_code == 200:

            return True, False

        if response.status_code in (
            409,
            422,
        ):

            body = response.text[:1000]

            log.warning(
                "GITHUB BRANCH CONFLICT | "
                "branch=%s expected_parent=%s "
                "status=%s body=%s",
                self.branch,
                expected_parent_sha[:12],
                response.status_code,
                body,
            )

            return False, True

        self._set_error(
            "UPDATE_REF_ERROR | "
            f"status={response.status_code} | "
            f"body={response.text[:500]}"
        )

        return False, False

    def backup(self) -> bool:

        if not self.is_configured():

            log.warning(
                "GITHUB CANDLE BACKUP SKIPPED | "
                "GITHUB_TOKEN / GITHUB_REPO missing"
            )

            return False

        with self._lock:

            if self._backup_in_progress:

                log.warning(
                    "GITHUB CANDLE BACKUP SKIPPED | "
                    "another backup is already running"
                )

                return False

            if not self._pending:

                self._last_backup_at = time.time()
                self._last_backup_ok = True
                self._last_error = None

                log.info(
                    "GITHUB CANDLE BACKUP | "
                    "nothing changed"
                )

                return True

            self._backup_in_progress = True

            files = dict(
                self._pending
            )

        try:

            log.info(
                "GITHUB CANDLE BACKUP START | "
                "repo=%s branch=%s files=%s "
                "retries=%s",
                self.repo,
                self.branch,
                len(files),
                self.max_retries,
            )

            for attempt in range(
                1,
                self.max_retries + 1,
            ):

                log.info(
                    "GITHUB BACKUP ATTEMPT | "
                    "attempt=%s/%s",
                    attempt,
                    self.max_retries,
                )

                branch = self._get_branch()

                if not branch:

                    log.warning(
                        "GITHUB BACKUP RETRY | "
                        "could not read branch HEAD"
                    )

                    self._sleep_retry(
                        attempt
                    )

                    continue

                parent_sha = (
                    branch
                    .get("commit", {})
                    .get("sha")
                )

                if not parent_sha:

                    self._set_error(
                        "BRANCH_HEAD_MISSING"
                    )

                    self._sleep_retry(
                        attempt
                    )

                    continue

                log.info(
                    "GITHUB HEAD | "
                    "branch=%s sha=%s",
                    self.branch,
                    parent_sha[:12],
                )

                base_tree_sha = (
                    self._get_commit_tree_sha(
                        parent_sha
                    )
                )

                if not base_tree_sha:

                    self._sleep_retry(
                        attempt
                    )

                    continue

                tree_sha = self._create_tree(
                    base_tree_sha,
                    files,
                )

                if not tree_sha:

                    self._sleep_retry(
                        attempt
                    )

                    continue

                commit_sha = self._create_commit(
                    tree_sha,
                    parent_sha,
                )

                if not commit_sha:

                    self._sleep_retry(
                        attempt
                    )

                    continue

                log.info(
                    "GITHUB COMMIT CREATED | "
                    "commit=%s parent=%s",
                    commit_sha[:12],
                    parent_sha[:12],
                )

                success, conflict = (
                    self._update_branch(
                        commit_sha,
                        parent_sha,
                    )
                )

                if success:

                    with self._lock:

                        for path, content in files.items():

                            if (
                                self._pending.get(path)
                                == content
                            ):
                                self._pending.pop(
                                    path,
                                    None,
                                )

                        self._last_backup_at = (
                            time.time()
                        )

                        self._last_backup_ok = True
                        self._last_error = None

                    log.info(
                        "GITHUB CANDLE BACKUP OK | "
                        "files=%s commit=%s",
                        len(files),
                        commit_sha[:12],
                    )

                    return True

                if conflict:

                    log.warning(
                        "GITHUB CONCURRENCY CONFLICT | "
                        "another commit changed %s "
                        "while backup was running | "
                        "retrying...",
                        self.branch,
                    )

                    self._sleep_retry(
                        attempt,
                        conflict=True,
                    )

                    continue

                self._sleep_retry(
                    attempt
                )

            self._set_error(
                "GITHUB BACKUP FAILED | "
                f"all {self.max_retries} attempts exhausted"
            )

            return False

        finally:

            with self._lock:
                self._backup_in_progress = False

    @staticmethod
    def _sleep_retry(
        attempt: int,
        conflict: bool = False,
    ) -> None:

        if conflict:

            delay = min(
                2.0,
                0.5 * attempt,
            )

        else:

            delay = min(
                10.0,
                1.0 * attempt,
            )

        time.sleep(delay)

    def download(
        self,
        source: str,
        symbol: str,
    ) -> Optional[dict]:

        if not self.is_configured():
            return None

        path = self._file_path(
            source,
            symbol,
        )

        url = self._url(
            f"/repos/{self.repo}/contents/{path}"
        )

        params = {
            "ref": self.branch,
        }

        try:

            response = self.session.get(
                url,
                headers=self._headers(),
                params=params,
                timeout=self.timeout,
            )

        except requests.RequestException as exc:

            self._set_error(
                f"DOWNLOAD_NETWORK_ERROR: {exc}"
            )

            return None

        if response.status_code == 404:

            return None

        if response.status_code != 200:

            self._set_error(
                "DOWNLOAD_ERROR | "
                f"status={response.status_code} | "
                f"body={response.text[:500]}"
            )

            return None

        try:

            data = response.json()

        except ValueError:

            self._set_error(
                "DOWNLOAD_ERROR | invalid JSON"
            )

            return None

        encoded = data.get(
            "content"
        )

        if not encoded:
            return None

        try:

            encoded = encoded.replace(
                "\n",
                "",
            )

            decoded = (
                base64.b64decode(
                    encoded
                )
                .decode("utf-8")
            )

            return json.loads(
                decoded
            )

        except (
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:

            self._set_error(
                f"DOWNLOAD_DECODE_ERROR: {exc}"
            )

            return None

    def restore_symbols(
        self,
        source: str,
        symbols: Iterable[str],
    ) -> Dict[str, dict]:

        if source not in VALID_SOURCES:

            raise ValueError(
                f"Invalid source: {source}"
            )

        symbol_list = list(symbols)

        restored: Dict[str, dict] = {}

        for symbol in symbol_list:

            try:

                data = self.download(
                    source,
                    symbol,
                )

            except Exception:

                log.exception(
                    "GITHUB RESTORE ERROR | "
                    "source=%s symbol=%s",
                    source,
                    symbol,
                )

                continue

            if data is not None:

                restored[
                    symbol
                ] = data

        log.info(
            "GITHUB RESTORE | "
            "source=%s restored=%s requested=%s",
            source,
            len(restored),
            len(symbol_list),
        )

        return restored

    def start_background_loop(
        self,
        interval_sec: int = DEFAULT_BACKUP_INTERVAL,
    ) -> threading.Thread:

        interval_sec = max(
            60,
            int(interval_sec),
        )

        def worker():

            log.info(
                "GITHUB CANDLE BACKUP LOOP STARTED | "
                "interval=%ss repo=%s branch=%s",
                interval_sec,
                self.repo or "-",
                self.branch,
            )

            time.sleep(10)

            while True:

                try:

                    self.backup()

                except Exception:

                    log.exception(
                        "GITHUB CANDLE BACKUP LOOP ERROR"
                    )

                time.sleep(
                    interval_sec
                )

        thread = threading.Thread(
            target=worker,
            daemon=True,
            name="github-candle-backup",
        )

        thread.start()

        return thread

    def pending_count(self) -> int:

        with self._lock:
            return len(
                self._pending
            )

    def _set_error(
        self,
        message: str,
    ) -> None:

        with self._lock:

            self._last_backup_ok = False
            self._last_error = message

        log.error(
            "GITHUB CANDLE BACKUP ERROR | %s",
            message,
        )


def build_github_candle_backup(
    session: requests.Session,
    timeout: int = 15,
) -> GitHubCandleBackup:

    return GitHubCandleBackup(
        session=session,
        timeout=timeout,
    )
