"""
Thread-safe bot state.

Candle history belongs exclusively to CandleStore and is injected into
the market analyzer.

BotState stores:
- cooldowns
- transaction dedupe
- health
- lightweight persistent state
"""

import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Optional


log = logging.getLogger("smart_money_bot.state")


class BotState:

    def __init__(
        self,
        history_window: int,
        state_file_path: Optional[str] = None,
        candle_store_path: str = "market_history",
    ):
        # candle_store_path is retained for backward compatibility.
        # Candle history is NO LONGER created here.
        del candle_store_path

        self._lock = threading.RLock()

        self._history_window = int(
            history_window
        )

        self._state_file_path = (
            state_file_path
        )

        self.previous_market_snapshot: Dict[
            str,
            dict,
        ] = {}

        self.last_alert_time: Dict[
            str,
            float,
        ] = {}

        self.seen_tx_hashes = deque(
            maxlen=5000
        )

        self.health = {
            "last_market_cycle_at": None,
            "last_whale_cycle_at": None,
            "last_error": None,
            "market_cycles_completed": 0,
            "whale_cycles_completed": 0,
            "started_at": datetime.now(
                timezone.utc
            ).isoformat(),
        }

        self._autosave_started = False

        self._load()

    # =========================================================
    # COOLDOWN
    # =========================================================

    def is_in_cooldown(
        self,
        key: str,
        cooldown_sec: int,
    ) -> bool:

        if cooldown_sec <= 0:
            return False

        with self._lock:
            last = self.last_alert_time.get(key)

        if last is None:
            return False

        return (
            time.time() - last
        ) < cooldown_sec

    def mark_alerted(
        self,
        key: str,
    ) -> None:

        with self._lock:
            self.last_alert_time[key] = (
                time.time()
            )

    # =========================================================
    # SNAPSHOTS
    # =========================================================

    def swap_snapshot(
        self,
        new_snapshot: dict,
    ) -> dict:

        with self._lock:
            old = self.previous_market_snapshot
            self.previous_market_snapshot = (
                new_snapshot
            )
            return old

    # =========================================================
    # TRANSACTIONS
    # =========================================================

    def is_new_tx(
        self,
        tx_hash: str,
    ) -> bool:

        if not tx_hash:
            return False

        with self._lock:

            if tx_hash in self.seen_tx_hashes:
                return False

            self.seen_tx_hashes.append(
                tx_hash
            )

            return True

    # =========================================================
    # HEALTH
    # =========================================================

    def record_market_cycle(
        self,
        error: Optional[str] = None,
    ) -> None:

        with self._lock:

            self.health[
                "last_market_cycle_at"
            ] = datetime.now(
                timezone.utc
            ).isoformat()

            self.health[
                "market_cycles_completed"
            ] += 1

            self.health[
                "last_error"
            ] = error

    def record_whale_cycle(
        self,
        error: Optional[str] = None,
    ) -> None:

        with self._lock:

            self.health[
                "last_whale_cycle_at"
            ] = datetime.now(
                timezone.utc
            ).isoformat()

            self.health[
                "whale_cycles_completed"
            ] += 1

            if error is not None:
                self.health[
                    "last_error"
                ] = error

    def snapshot_health(self) -> dict:

        with self._lock:
            return dict(
                self.health
            )

    # =========================================================
    # PERSISTENCE
    # =========================================================

    def _load(self) -> None:

        path = self._state_file_path

        if not path or not os.path.exists(path):
            return

        try:

            with open(
                path,
                "r",
                encoding="utf-8",
            ) as file:

                data = json.load(file)

            if not isinstance(data, dict):
                return

            with self._lock:

                loaded_alerts = data.get(
                    "last_alert_time",
                    {},
                )

                self.last_alert_time = (
                    loaded_alerts
                    if isinstance(
                        loaded_alerts,
                        dict,
                    )
                    else {}
                )

                loaded_hashes = data.get(
                    "seen_tx_hashes",
                    [],
                )

                if not isinstance(
                    loaded_hashes,
                    list,
                ):
                    loaded_hashes = []

                self.seen_tx_hashes = deque(
                    loaded_hashes,
                    maxlen=5000,
                )

        except (
            OSError,
            json.JSONDecodeError,
            TypeError,
        ) as exc:

            log.warning(
                "STATE LOAD FAILED | error=%s",
                exc,
            )

    def save(self) -> None:

        path = self._state_file_path

        if not path:
            return

        with self._lock:

            payload = {
                "last_alert_time": dict(
                    self.last_alert_time
                ),
                "seen_tx_hashes": list(
                    self.seen_tx_hashes
                ),
                "saved_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            }

        directory = os.path.dirname(
            os.path.abspath(path)
        )

        try:
            os.makedirs(
                directory,
                exist_ok=True,
            )

            tmp_path = (
                f"{path}.tmp"
            )

            with open(
                tmp_path,
                "w",
                encoding="utf-8",
            ) as file:

                json.dump(
                    payload,
                    file,
                    ensure_ascii=False,
                    indent=2,
                )

                file.flush()
                os.fsync(
                    file.fileno()
                )

            os.replace(
                tmp_path,
                path,
            )

        except OSError as exc:

            log.warning(
                "STATE SAVE FAILED | error=%s",
                exc,
            )

    # =========================================================
    # AUTOSAVE
    # =========================================================

    def start_autosave(
        self,
        interval_sec: int,
    ) -> None:

        interval_sec = int(
            interval_sec
        )

        if interval_sec <= 0:
            return

        with self._lock:

            if self._autosave_started:
                return

            self._autosave_started = True

        def _loop():

            while True:

                time.sleep(
                    interval_sec
                )

                try:
                    self.save()

                except Exception:
                    log.exception(
                        "STATE AUTOSAVE FAILED"
                    )

        threading.Thread(
            target=_loop,
            daemon=True,
            name="state-autosave",
        ).start()
        