"""
Thread-safe state container.

The original bot mutated module-level globals (previous_market_snapshot,
volume_history, last_alert_time) from a background thread while the Flask
/status route (a different thread, under gunicorn/multiple workers even more
so) could read overlapping structures without any lock — a data race. This
version wraps every read/write behind a single RLock and additionally
persists the parts that matter (cooldowns + dedupe set) to disk so a restart
doesn't immediately re-fire every alert that was in cooldown.
"""
import json
import logging
import os
import statistics
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Optional

log = logging.getLogger("smart_money_bot.state")


class BotState:
    def __init__(self, history_window: int, state_file_path: Optional[str] = None):
        self._lock = threading.RLock()
        self._history_window = history_window
        self._state_file_path = state_file_path

        self.previous_market_snapshot: Dict[str, dict] = {}
        self.volume_history: Dict[str, deque] = {}
        self.price_return_history: Dict[str, deque] = {}
        self.last_alert_time: Dict[str, float] = {}
        self.seen_tx_hashes: deque = deque(maxlen=5000)

        self.health = {
            "last_market_cycle_at": None,
            "last_whale_cycle_at": None,
            "last_error": None,
            "market_cycles_completed": 0,
            "whale_cycles_completed": 0,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }

        self._load()

    # ---------------- cooldown / alert bookkeeping ----------------

    def is_in_cooldown(self, key: str, cooldown_sec: int) -> bool:
        with self._lock:
            last = self.last_alert_time.get(key)
        if last is None:
            return False
        return (time.time() - last) < cooldown_sec

    def mark_alerted(self, key: str) -> None:
        with self._lock:
            self.last_alert_time[key] = time.time()

    # ---------------- volume baseline ----------------

    def get_baseline_volume(self, symbol: str, fallback_avg: float) -> float:
        with self._lock:
            hist = self.volume_history.get(symbol)
            if hist and len(hist) >= 3:
                return max(statistics.mean(hist), 1.0)
        return max(fallback_avg, 1.0)

    def push_volume_sample(self, symbol: str, value: float) -> None:
        with self._lock:
            hist = self.volume_history.setdefault(symbol, deque(maxlen=self._history_window))
            hist.append(value)

    def push_price_return_sample(self, symbol: str, pct_change: float) -> None:
        with self._lock:
            hist = self.price_return_history.setdefault(symbol, deque(maxlen=self._history_window))
            hist.append(pct_change)

    def get_return_zscore(self, symbol: str, current_pct_change: float) -> Optional[float]:
        """How many standard deviations `current_pct_change` is from this
        symbol's own recent per-cycle price-return distribution. Returns
        None until there's enough history (min 5 samples) to make the
        number meaningful — with too little history a z-score is noise, not
        signal, so callers should treat None as "can't judge yet", not "0".

        This lets a coin that normally barely moves get flagged on a much
        smaller absolute % move than a coin that's always volatile, which a
        single fixed PRICE_PUMP_MIN/MAX threshold across every asset can't
        do. It's a fairly short rolling window (HISTORY_WINDOW cycles), so
        treat it as a useful second signal alongside the static thresholds,
        not a replacement for human judgement."""
        with self._lock:
            hist = list(self.price_return_history.get(symbol, ()))
        if len(hist) < 5:
            return None
        mean = statistics.mean(hist)
        stdev = statistics.pstdev(hist)
        if stdev == 0:
            return None
        return (current_pct_change - mean) / stdev

    def swap_snapshot(self, new_snapshot: dict) -> dict:
        with self._lock:
            old = self.previous_market_snapshot
            self.previous_market_snapshot = new_snapshot
            return old

    # ---------------- on-chain dedupe ----------------

    def is_new_tx(self, tx_hash: str) -> bool:
        with self._lock:
            if tx_hash in self.seen_tx_hashes:
                return False
            self.seen_tx_hashes.append(tx_hash)
            return True

    # ---------------- health / status ----------------

    def record_market_cycle(self, error: Optional[str] = None) -> None:
        with self._lock:
            self.health["last_market_cycle_at"] = datetime.now(timezone.utc).isoformat()
            self.health["market_cycles_completed"] += 1
            self.health["last_error"] = error

    def record_whale_cycle(self, error: Optional[str] = None) -> None:
        with self._lock:
            self.health["last_whale_cycle_at"] = datetime.now(timezone.utc).isoformat()
            self.health["whale_cycles_completed"] += 1
            if error:
                self.health["last_error"] = error

    def snapshot_health(self) -> dict:
        with self._lock:
            return dict(self.health)

    # ---------------- persistence ----------------

    def _load(self) -> None:
        if not self._state_file_path or not os.path.exists(self._state_file_path):
            return
        try:
            with open(self._state_file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:
                self.last_alert_time = data.get("last_alert_time", {})
                self.seen_tx_hashes = deque(data.get("seen_tx_hashes", []), maxlen=5000)
            log.info("وضعیت قبلی از دیسک بازیابی شد.")
        except (OSError, json.JSONDecodeError) as e:
            log.warning(f"بازیابی وضعیت از دیسک ناموفق بود: {e}")

    def save(self) -> None:
        if not self._state_file_path:
            return
        with self._lock:
            payload = {
                "last_alert_time": self.last_alert_time,
                "seen_tx_hashes": list(self.seen_tx_hashes),
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
        tmp_path = f"{self._state_file_path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp_path, self._state_file_path)
        except OSError as e:
            log.warning(f"ذخیره وضعیت روی دیسک ناموفق بود: {e}")

    def start_autosave(self, interval_sec: int) -> None:
        if not self._state_file_path or interval_sec <= 0:
            return

        def _loop():
            while True:
                time.sleep(interval_sec)
                self.save()

        threading.Thread(target=_loop, daemon=True).start()