"""
Persistent rolling 5-minute candle store.

For every symbol we keep exactly up to 864 closed 5m candles
(72 hours). The current, still-open candle is kept separately so
its live volume can be used for immediate signal detection.

Storage is local JSON first. GitHub persistence can be layered on
top through the GitHub persistence methods below.
"""

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

log = logging.getLogger("smart_money_bot.candle_store")

CANDLE_LIMIT = 864
BASELINE_CANDLES = 48


@dataclass
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
            open_time=int(raw[0]),
            open=float(raw[1]),
            high=float(raw[2]),
            low=float(raw[3]),
            close=float(raw[4]),
            volume=float(raw[5]),
            close_time=int(raw[6]),
            quote_volume=float(raw[7]),
            trades=int(raw[8]),
        )


class CandleStore:
    def __init__(
        self,
        root_path: str = "market_history",
        max_candles: int = CANDLE_LIMIT,
    ):
        self.root_path = root_path
        self.max_candles = max_candles
        self._lock = threading.RLock()

        self._closed: Dict[str, deque] = {}
        self._current: Dict[str, Candle] = {}
        self._dirty = set()

        os.makedirs(self.root_path, exist_ok=True)

    # ---------------------------------------------------------
    # paths
    # ---------------------------------------------------------

    def _path(self, symbol: str) -> str:
        safe = "".join(
            c for c in symbol
            if c.isalnum() or c in ("_", "-")
        )
        return os.path.join(self.root_path, f"{safe}.json")

    # ---------------------------------------------------------
    # loading
    # ---------------------------------------------------------

    def load(self, symbol: str) -> bool:
        with self._lock:
            if symbol in self._closed:
                return True

        path = self._path(symbol)

        if not os.path.exists(path):
            return False

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            candles = data.get("candles", [])

            parsed = []
            for item in candles:
                try:
                    parsed.append(
                        Candle(
                            open_time=int(item["open_time"]),
                            close_time=int(item["close_time"]),
                            open=float(item["open"]),
                            high=float(item["high"]),
                            low=float(item["low"]),
                            close=float(item["close"]),
                            volume=float(item["volume"]),
                            quote_volume=float(item["quote_volume"]),
                            trades=int(item.get("trades", 0)),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue

            parsed.sort(key=lambda x: x.open_time)

            with self._lock:
                self._closed[symbol] = deque(
                    parsed[-self.max_candles:],
                    maxlen=self.max_candles,
                )

                current = data.get("current")
                if current:
                    try:
                        self._current[symbol] = Candle(
                            open_time=int(current["open_time"]),
                            close_time=int(current["close_time"]),
                            open=float(current["open"]),
                            high=float(current["high"]),
                            low=float(current["low"]),
                            close=float(current["close"]),
                            volume=float(current["volume"]),
                            quote_volume=float(current["quote_volume"]),
                            trades=int(current.get("trades", 0)),
                        )
                    except (KeyError, TypeError, ValueError):
                        pass

            return True

        except (OSError, json.JSONDecodeError) as e:
            log.warning("خطا در بارگذاری تاریخچه %s: %s", symbol, e)
            return False

    # ---------------------------------------------------------
    # bootstrap
    # ---------------------------------------------------------

    def seed(self, symbol: str, candles: List[Candle]) -> None:
        if not candles:
            return

        candles = sorted(candles, key=lambda x: x.open_time)

        with self._lock:
            self._closed[symbol] = deque(
                candles[-self.max_candles:],
                maxlen=self.max_candles,
            )
            self._dirty.add(symbol)

    # ---------------------------------------------------------
    # candle update
    # ---------------------------------------------------------

    def update(self, symbol: str, candle: Candle) -> str:
        """
        Returns:
            "current"  -> updated current candle
            "closed"   -> previous current candle closed
            "new"      -> new closed candle inserted
        """

        with self._lock:
            history = self._closed.setdefault(
                symbol,
                deque(maxlen=self.max_candles),
            )

            current = self._current.get(symbol)

            # No current candle yet.
            if current is None:
                self._current[symbol] = candle
                return "current"

            # Same candle: replace with newest live values.
            if candle.open_time == current.open_time:
                self._current[symbol] = candle
                return "current"

            # New candle arrived.
            if candle.open_time > current.open_time:
                history.append(current)

                self._current[symbol] = candle
                self._dirty.add(symbol)

                return "closed"

            # Out-of-order/old response.
            return "ignored"

    def add_closed(self, symbol: str, candle: Candle) -> None:
        with self._lock:
            history = self._closed.setdefault(
                symbol,
                deque(maxlen=self.max_candles),
            )

            if history and candle.open_time <= history[-1].open_time:
                return

            history.append(candle)
            self._dirty.add(symbol)

    # ---------------------------------------------------------
    # getters
    # ---------------------------------------------------------

    def get_current(self, symbol: str) -> Optional[Candle]:
        with self._lock:
            return self._current.get(symbol)

    def get_closed(self, symbol: str) -> List[Candle]:
        with self._lock:
            return list(self._closed.get(symbol, ()))

    def get_recent(self, symbol: str, count: int) -> List[Candle]:
        with self._lock:
            history = self._closed.get(symbol, ())
            if count <= 0:
                return []
            return list(history)[-count:]

    def count(self, symbol: str) -> int:
        with self._lock:
            return len(self._closed.get(symbol, ()))

    # ---------------------------------------------------------
    # statistics
    # ---------------------------------------------------------

    def average_quote_volume(
        self,
        symbol: str,
        count: int = BASELINE_CANDLES,
    ) -> Optional[float]:

        candles = self.get_recent(symbol, count)

        if len(candles) < count:
            return None

        values = [
            c.quote_volume
            for c in candles
            if c.quote_volume > 0
        ]

        if len(values) < max(10, count // 2):
            return None

        return sum(values) / len(values)

    def average_volume(
        self,
        symbol: str,
        count: int = BASELINE_CANDLES,
    ) -> Optional[float]:

        candles = self.get_recent(symbol, count)

        if len(candles) < count:
            return None

        values = [
            c.volume
            for c in candles
            if c.volume > 0
        ]

        if len(values) < max(10, count // 2):
            return None

        return sum(values) / len(values)

    # ---------------------------------------------------------
    # persistence
    # ---------------------------------------------------------

    def save(self, symbol: str) -> None:
        with self._lock:
            history = list(self._closed.get(symbol, ()))
            current = self._current.get(symbol)

            payload = {
                "symbol": symbol,
                "interval": "5m",
                "max_candles": self.max_candles,
                "updated_at": int(time.time() * 1000),
                "candles": [asdict(c) for c in history],
                "current": asdict(current) if current else None,
            }

        path = self._path(symbol)
        tmp = f"{path}.tmp"

        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))

            os.replace(tmp, path)

            with self._lock:
                self._dirty.discard(symbol)

        except OSError as e:
            log.warning("ذخیره تاریخچه %s ناموفق بود: %s", symbol, e)

    def save_dirty(self) -> None:
        with self._lock:
            symbols = list(self._dirty)

        for symbol in symbols:
            self.save(symbol)

    def save_all(self) -> None:
        with self._lock:
            symbols = list(self._closed.keys())

        for symbol in symbols:
            self.save(symbol)