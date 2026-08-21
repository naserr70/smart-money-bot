"""Thread-safe persistent store for CLOSED 5-minute candles."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

log = logging.getLogger("smart_money_bot.candle_store")
CANDLE_LIMIT = 864
SMART_MONEY_BASELINE_CANDLES = 48
PUMP_HISTORY_CANDLES = 864
VALID_SOURCES = ("binance", "bybit", "kucoin")
CANDLE_INTERVAL_MS = 5 * 60 * 1000


@dataclass(frozen=True)
class Candle:
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int = 0

    @classmethod
    def from_binance(cls, raw: list) -> "Candle":
        return cls(int(raw[0]), int(raw[6]), float(raw[1]), float(raw[2]), float(raw[3]), float(raw[4]), float(raw[5]), float(raw[7]), int(raw[8]))

    @classmethod
    def from_bybit(cls, raw: list) -> "Candle":
        open_ms = int(float(raw[0]))
        return cls(open_ms, open_ms + CANDLE_INTERVAL_MS - 1, float(raw[1]), float(raw[2]), float(raw[3]), float(raw[4]), float(raw[5]), float(raw[6]), 0)

    @classmethod
    def from_kucoin(cls, raw: list) -> "Candle":
        open_ms = int(float(raw[0]) * 1000)
        return cls(open_ms, open_ms + CANDLE_INTERVAL_MS - 1, float(raw[1]), float(raw[3]), float(raw[4]), float(raw[2]), float(raw[5]), float(raw[6]), 0)


class CandleStore:
    """Independent rolling histories for Binance, Bybit and KuCoin."""

    def __init__(self, root_path: str = "market_history", max_candles: int = CANDLE_LIMIT, github_enabled: bool = False, github_token: str = "", github_repo: str = "", github_branch: str = "main", github_sync_interval_sec: int = 300, **kwargs):
        del kwargs, github_enabled, github_token, github_repo, github_branch, github_sync_interval_sec
        self.root_path = root_path
        self.max_candles = max(1, int(max_candles))
        self._lock = threading.RLock()
        self._closed: Dict[str, Dict[str, deque[Candle]]] = {s: {} for s in VALID_SOURCES}
        self._current: Dict[str, Dict[str, Candle]] = {s: {} for s in VALID_SOURCES}
        self._dirty = {s: set() for s in VALID_SOURCES}
        os.makedirs(self.root_path, exist_ok=True)
        for source in VALID_SOURCES:
            os.makedirs(os.path.join(self.root_path, source), exist_ok=True)

    @staticmethod
    def normalize_source(source: str) -> str:
        source = str(source).lower().strip()
        if source not in VALID_SOURCES:
            raise ValueError(f"Unsupported candle source: {source}")
        return source

    @staticmethod
    def _symbol_filename(symbol: str) -> str:
        safe = "".join(c for c in str(symbol).upper() if c.isalnum() or c in ("_", "-"))
        if not safe:
            raise ValueError(f"Invalid candle symbol: {symbol!r}")
        return safe

    def _path(self, source: str, symbol: str) -> str:
        return os.path.join(self.root_path, self.normalize_source(source), f"{self._symbol_filename(symbol)}.json")

    @staticmethod
    def _is_closed(candle: Candle, now_ms: Optional[int] = None) -> bool:
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        return int(candle.close_time) < now_ms

    @staticmethod
    def _parse_candle(item: dict) -> Optional[Candle]:
        try:
            return Candle(int(item["open_time"]), int(item["close_time"]), float(item["open"]), float(item["high"]), float(item["low"]), float(item["close"]), float(item["volume"]), float(item["quote_volume"]), int(item.get("trades", 0)))
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _dedupe_and_sort(candles: List[Candle]) -> List[Candle]:
        return sorted({c.open_time: c for c in candles}.values(), key=lambda c: c.open_time)

    def load(self, source: str, symbol: str) -> bool:
        source = self.normalize_source(source)
        with self._lock:
            if symbol in self._closed[source]:
                return True
        path = self._path(source, symbol)
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            parsed = []
            for item in data.get("candles", []) if isinstance(data, dict) else []:
                if isinstance(item, dict):
                    candle = self._parse_candle(item)
                    if candle and self._is_closed(candle):
                        parsed.append(candle)
            parsed = self._dedupe_and_sort(parsed)
            with self._lock:
                self._closed[source][symbol] = deque(parsed[-self.max_candles:], maxlen=self.max_candles)
            return True
        except (OSError, json.JSONDecodeError, TypeError):
            log.exception("HISTORY LOAD FAILED | source=%s symbol=%s", source, symbol)
            return False

    def seed(self, source: str, symbol: str, candles: List[Candle], mark_dirty: bool = True) -> None:
        source = self.normalize_source(source)
        valid = self._dedupe_and_sort([c for c in candles if self._is_closed(c)])
        with self._lock:
            self._closed[source][symbol] = deque(valid[-self.max_candles:], maxlen=self.max_candles)
            if mark_dirty:
                self._dirty[source].add(symbol)

    def add_closed(self, source: str, symbol: str, candle: Candle) -> bool:
        source = self.normalize_source(source)
        if not self._is_closed(candle):
            return False
        with self._lock:
            history = self._closed[source].setdefault(symbol, deque(maxlen=self.max_candles))
            if history and candle.open_time < history[-1].open_time:
                return False
            if history and candle.open_time == history[-1].open_time:
                if history[-1] == candle:
                    return False
                history[-1] = candle
            else:
                history.append(candle)
            self._dirty[source].add(symbol)
            return True

    def apply_recent(self, source: str, symbol: str, candles: List[Candle]) -> int:
        return sum(1 for candle in sorted(candles, key=lambda c: c.open_time) if self.add_closed(source, symbol, candle))

    def update(self, source: str, symbol: str, candle: Candle) -> str:
        return "closed" if self.add_closed(source, symbol, candle) else "ignored"

    def get_current(self, source: str, symbol: str) -> Optional[Candle]:
        source = self.normalize_source(source)
        with self._lock:
            return self._current[source].get(symbol)

    def get_closed(self, source: str, symbol: str) -> List[Candle]:
        source = self.normalize_source(source)
        with self._lock:
            return list(self._closed[source].get(symbol, ()))

    def get_recent(self, source: str, symbol: str, count: int) -> List[Candle]:
        if count <= 0:
            return []
        return self.get_closed(source, symbol)[-count:]

    def count(self, source: str, symbol: str) -> int:
        source = self.normalize_source(source)
        with self._lock:
            return len(self._closed[source].get(symbol, ()))

    def average_quote_volume(self, source: str, symbol: str, count: int = SMART_MONEY_BASELINE_CANDLES) -> Optional[float]:
        candles = self.get_recent(source, symbol, count)
        values = [c.quote_volume for c in candles if c.quote_volume > 0]
        return sum(values) / len(values) if len(candles) == count and len(values) == count else None

    def average_quote_volume_long(self, source: str, symbol: str, count: int = PUMP_HISTORY_CANDLES) -> Optional[float]:
        return self.average_quote_volume(source, symbol, count)

    def price_returns(self, source: str, symbol: str, count: int = PUMP_HISTORY_CANDLES) -> List[float]:
        candles = self.get_recent(source, symbol, count)
        return [(current.close - previous.close) / previous.close * 100.0 for previous, current in zip(candles, candles[1:]) if previous.close > 0]

    def dirty_symbols(self, source: str) -> List[str]:
        source = self.normalize_source(source)
        with self._lock:
            return sorted(self._dirty[source])

    def clear_dirty(self, source: str, symbol: str) -> None:
        source = self.normalize_source(source)
        with self._lock:
            self._dirty[source].discard(symbol)

    def to_payload(self, source: str, symbol: str) -> dict:
        source = self.normalize_source(source)
        return {
            "schema_version": 2,
            "source": source,
            "symbol": symbol,
            "interval": "5m",
            "max_candles": self.max_candles,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "candles": [asdict(c) for c in self.get_closed(source, symbol)],
        }

    def save(self, source: str, symbol: str) -> bool:
        payload = self.to_payload(source, symbol)
        path = self._path(source, symbol)
        tmp = f"{path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            return True
        except OSError:
            log.exception("HISTORY LOCAL SAVE FAILED | source=%s symbol=%s", source, symbol)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return False

    def save_dirty(self, source: Optional[str] = None) -> None:
        sources = [self.normalize_source(source)] if source else list(VALID_SOURCES)
        for src in sources:
            for symbol in self.dirty_symbols(src):
                if self.save(src, symbol):
                    self.clear_dirty(src, symbol)

    def save_all(self) -> None:
        for source in VALID_SOURCES:
            with self._lock:
                symbols = list(self._closed[source])
            for symbol in symbols:
                self.save(source, symbol)
