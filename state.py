"""
Thread-safe bot state.

Market candle history is delegated to CandleStore.
Cooldowns and transaction dedupe remain here.
"""

import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Optional

from candle_store import CandleStore

log = logging.getLogger("smart_money_bot.state")


class BotState:

    def __init__(
        self,
        history_window: int,
        state_file_path: Optional[str] = None,
        candle_store_path: str = "market_history",
    ):
        self._lock = threading.RLock()

        self._history_window = history_window
        self._state_file_path = state_file_path

        self.previous_market_snapshot: Dict[str, dict] = {}

        self.last_alert_time: Dict[str, float] = {}

        self.seen_tx_hashes = deque(
            maxlen=5000
        )

        self.candles = CandleStore(
            root_path=candle_store_path,
            max_candles=864,
        )

        self.health = {
            "last_market_cycle_at": None,
            "last_whale_cycle_at": None,
            "last_error": None,
            "market_cycles_completed": 0,
            "whale_cycles_completed": 0,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }

        self._load()

    # ---------------------------------------------------------
    # cooldown
    # ---------------------------------------------------------

    def is_in_cooldown(
        self,
        key: str,
        cooldown_sec: int,
    ) -> bool:

        with self._lock:
            last = self.last_alert_time.get(key)

        if last is None:
            return False

        return (time.time() - last) < cooldown_sec

    def mark_alerted(self, key: str) -> None:
        with self._lock:
            self.last_alert_time[key] = time.time()

    # ---------------------------------------------------------
    # snapshots
    # ---------------------------------------------------------

    def swap_snapshot(self, new_snapshot: dict) -> dict:
        with self._lock:
            old = self.previous_market_snapshot
            self.previous_market_snapshot = new_snapshot
            return old

    # ---------------------------------------------------------
    # transactions
    # ---------------------------------------------------------

    def is_new_tx(self, tx_hash: str) -> bool:

        with self._lock:
            if tx_hash in self.seen_tx_hashes:
                return False

            self.seen_tx_hashes.append(tx_hash)

            return True

    # ---------------------------------------------------------
    # health
    # ---------------------------------------------------------

    def record_market_cycle(
        self,
        error: Optional[str] = None,
    ) -> None:

        with self._lock:
            self.health["last_market_cycle_at"] = (
                datetime.now(timezone.utc).isoformat()
            )

            self.health["market_cycles_completed"] += 1
            self.health["last_error"] = error

    def record_whale_cycle(
        self,
        error: Optional[str] = None,
    ) -> None:

        with self._lock:
            self.health["last_whale_cycle_at"] = (
                datetime.now(timezone.utc).isoformat()
            )

            self.health["whale_cycles_completed"] += 1

            if error:
                self.health["last_error"] = error

    def snapshot_health(self) -> dict:
        with self._lock:
            return dict(self.health)

    # ---------------------------------------------------------
    # persistence
    # ---------------------------------------------------------

    def _load(self) -> None:

        if (
            not self._state_file_path
            or not os.path.exists(self._state_file_path)
        ):
            return

        try:
            with open(
                self._state_file_path,
                "r",
                encoding="utf-8",
            ) as f:
                data = json.load(f)

            with self._lock:
                self.last_alert_time = data.get(
                    "last_alert_time",
                    {},
                )

                self.seen_tx_hashes = deque(
                    data.get("seen_tx_hashes", []),
                    maxlen=5000,
                )

        except (OSError, json.JSONDecodeError) as e:
            log.warning(
                "بازیابی state ناموفق بود: %s",
                e,
            )

    def save(self) -> None:

        if not self._state_file_path:
            return

        with self._lock:
            payload = {
                "last_alert_time": self.last_alert_time,
                "seen_tx_hashes": list(
                    self.seen_tx_hashes
                ),
                "saved_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            }

        tmp_path = f"{self._state_file_path}.tmp"

        try:
            with open(
                tmp_path,
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(
                    payload,
                    f,
                    ensure_ascii=False,
                )

            os.replace(
                tmp_path,
                self._state_file_path,
            )

        except OSError as e:
            log.warning(
                "ذخیره state ناموفق بود: %s",
                e,
            )

    def save_market_history(self) -> None:
        self.candles.save_dirty()

    def start_autosave(
        self,
        interval_sec: int,
    ) -> None:

        if interval_sec <= 0:
            return

        def _loop():
            while True:
                time.sleep(interval_sec)

                try:
                    self.save()
                    self.save_market_history()

                except Exception:
                    log.exception(
                        "خطا در autosave"
                    )

        threading.Thread(
            target=_loop,
            daemon=True,
        ).start()