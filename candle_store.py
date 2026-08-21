"""Thread-safe persistent store for CLOSED 5-minute candles.

The store deliberately has no concept of a signal candle that is still open.
Exchange adapters are responsible for returning closed candles only; the store
also validates close_time as a second safety boundary.
"""

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
        return cls(
            open_time=int(raw[0]), close_time=int(raw[6]),
            open=float(raw[1]), high=float(raw[2]), low=float(raw[3]),
            close=float(raw[4]), volume=float(raw[5]),
            quote_volume=float(raw[7]), trades=int(raw[8]),
        )

    @classmethod
    def from_bybit(cls, raw: list) -> "Candle":
        open_ms = int(float(raw[0]))
        return cls(
            open_time=open_ms,
            close_time=open_ms + CANDLE_INTERVAL_MS - 1,
            open=float(raw[1]), high=float(raw[2]), low=float(raw[3]),
            close=float(raw[4]), volume=float(raw[5]),
            quote_volume=float(raw[6]), trades=0,
        )

    @classmethod
    def from_kucoin(cls, raw: list) -> "Candle":
        open_ms = int(float(raw[0]) * 1000)
        return cls(
            open_time=open_ms,
            close_time=open_ms + CANDLE_INTERVAL_MS - 1,
            open=float(raw[1]), close=float(raw[2]),
            high=float(raw[3]), low=float(raw[4]),
            volume=float(raw[5]), quote_volume=float(raw[6]), trades=0,
        )


class CandleStore:
    """Independent rolling histories for Binance, Bybit and KuCoin."""

    def __init__(
        self,
        root_path: str = "market_history",
        max_candles: int = CANDLE_LIMIT,
        github_enabled: bool = False,
        github_token: str = "",
        github_repo: str = "",
        github_branch: str = "main",
        github_sync_interval_sec: int = 300,
        **kwargs,
    ):
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
        source = self.normalize_source(source)
        return os.path.join(self.root_path, source, f"{self._symbol_filename(symbol)}.json")

    @staticmethod
    def _is_closed(candle: Candle, now_ms: Optional[int] = None) -> bool:
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        return int(candle.close_time) < now_ms

    @staticmethod
    def _parse_candle(item: dict) -> Optional[Candle]:
        try:
            return Candle(
                open_time=int(item["open_time"]), close_time=int(item["close_time"]),
                open=float(item["open"]), high=float(item["high"]), low=float(item["low"]),
                close=float(item["close"]), volume=float(item["volume"]),
                quote_volume=float(item["quote_volume"]), trades=int(item.get("trades", 0)),
            )
        except (KeyError, TypeError, ValueError):
            return None

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
                self._dirty[source].add(symbol)
            log.info("HISTORY LOADED | source=%s symbol=%s candles=%s/%s", source, symbol, len(parsed[-self.max_candles:]), self.max_candles)
            return True
        except (OSError, json.JSONDecodeError, TypeError):
            log.exception("HISTORY LOAD FAILED | source=%s symbol=%s", source, symbol)
            return False

    @staticmethod
    def _dedupe_and_sort(candles: List[Candle]) -> List[Candle]:
        unique = {c.open_time: c for c in candles}
        return sorted(unique.values(), key=lambda c: c.open_time)

    def seed(self, source: str, symbol: str, candles: List[Candle]) -> None:
        source = self.normalize_source(source)
        now_ms = int(time.time() * 1000)
        valid = [c for c in candles if self._is_closed(c, now_ms)]
        valid = self._dedupe_and_sort(valid)
        with self._lock:
            self._closed[source][symbol] = deque(valid[-self.max_candles:], maxlen=self.max_candles)
            self._dirty[source].add(symbol)
        log.info("HISTORY SEEDED | source=%s symbol=%s candles=%s/%s", source, symbol, len(valid[-self.max_candles:]), self.max_candles)

    def add_closed(self, source: str, symbol: str, candle: Candle) -> bool:
        source = self.normalize_source(source)
        if not self._is_closed(candle):
            log.debug("CLOSED CANDLE REJECTED | source=%s symbol=%s open_time=%s", source, symbol, candle.open_time)
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
        added = 0
        for candle in sorted(candles, key=lambda c: c.open_time):
            if self.add_closed(source, symbol, candle):
                added += 1
        return added

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
        source = self.normalize_source(source)
        with self._lock:
            return list(self._closed[source].get(symbol, ())) [-count:]

    def count(self, source: str, symbol: str) -> int:
        source = self.normalize_source(source)
        with self._lock:
            return len(self._closed[source].get(symbol, ()))

    def average_quote_volume(self, source: str, symbol: str, count: int = SMART_MONEY_BASELINE_CANDLES) -> Optional[float]:
        candles = self.get_recent(source, symbol, count)
        if len(candles) < count:
            return None
        values = [c.quote_volume for c in candles if c.quote_volume > 0]
        return sum(values) / len(values) if len(values) == count else None

    def average_quote_volume_long(self, source: str, symbol: str, count: int = PUMP_HISTORY_CANDLES) -> Optional[float]:
        candles = self.get_recent(source, symbol, count)
        if len(candles) < count:
            return None
        values = [c.quote_volume for c in candles if c.quote_volume > 0]
        return sum(values) / len(values) if len(values) == count else None

    def price_returns(self, source: str, symbol: str, count: int = PUMP_HISTORY_CANDLES) -> List[float]:
        candles = self.get_recent(source, symbol, count)
        result: List[float] = []
        for previous, current in zip(candles, candles[1:]):
            if previous.close > 0:
                result.append((current.close - previous.close) / previous.close * 100.0)
        return result

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
        with self._lock:
            history = list(self._closed[source].get(symbol, ()))
        return {
            "schema_version": 2,
            "source": source,
            "symbol": symbol,
            "interval": "5m",
            "max_candles": self.max_candles,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "candles": [asdict(c) for c in history],
        }

    def save(self, source: str, symbol: str) -> bool:
        source = self.normalize_source(source)
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
