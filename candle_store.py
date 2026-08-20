"""
Persistent source-separated rolling 5-minute candle store.

Each exchange has its own completely independent history.

Example:

market_history/
    binance/
        BTC.json
        ETH.json
        SUI.json
    kucoin/
        BTC.json
        ETH.json
        SUI.json

Rules:
    - Maximum 864 CLOSED candles per source/symbol = 72 hours.
    - Current/open candle is kept separately.
    - Binance data is NEVER copied to KuCoin.
    - KuCoin data is NEVER copied to Binance.
    - Historical calculations are always source-specific.
"""

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("smart_money_bot.candle_store")

CANDLE_LIMIT = 864
SMART_MONEY_CANDLES = 48
PUMP_DUMP_CANDLES = 864

SUPPORTED_SOURCES = ("binance", "kucoin")


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
            close_time=int(raw[6]),
            volume=float(raw[5]),
            quote_volume=float(raw[7]),
            trades=int(raw[8]),
        )

    @classmethod
    def from_kucoin(cls, raw: list) -> "Candle":
        """
        KuCoin Spot candle format:

        [
            timestamp,
            open,
            high,
            low,
            close,
            volume,
            turnover
        ]

        Current KuCoin documentation uses this structure for Spot Klines.
        """

        if len(raw) < 7:
            raise ValueError("Invalid KuCoin candle")

        return cls(
            open_time=int(raw[0]) * 1000,
            open=float(raw[1]),
            high=float(raw[2]),
            low=float(raw[3]),
            close=float(raw[4]),
            close_time=(int(raw[0]) + 300) * 1000 - 1,
            volume=float(raw[5]),
            quote_volume=float(raw[6]),
            trades=0,
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

        # Key is (source, symbol)
        self._closed: Dict[Tuple[str, str], deque] = {}
        self._current: Dict[Tuple[str, str], Candle] = {}
        self._dirty = set()

        for source in SUPPORTED_SOURCES:
            os.makedirs(
                os.path.join(self.root_path, source),
                exist_ok=True,
            )

    # ---------------------------------------------------------
    # validation
    # ---------------------------------------------------------

    def _normalize_source(self, source: str) -> str:
        source = str(source).strip().lower()

        if source not in SUPPORTED_SOURCES:
            raise ValueError(
                f"Unsupported candle source: {source}. "
                f"Supported: {SUPPORTED_SOURCES}"
            )

        return source

    # ---------------------------------------------------------
    # paths
    # ---------------------------------------------------------

    def _path(self, source: str, symbol: str) -> str:
        source = self._normalize_source(source)

        safe_symbol = "".join(
            c for c in symbol
            if c.isalnum() or c in ("_", "-")
        )

        directory = os.path.join(
            self.root_path,
            source,
        )

        os.makedirs(directory, exist_ok=True)

        return os.path.join(
            directory,
            f"{safe_symbol}.json",
        )

    # ---------------------------------------------------------
    # loading
    # ---------------------------------------------------------

    def load(self, source: str, symbol: str) -> bool:
        source = self._normalize_source(source)
        key = (source, symbol)

        with self._lock:
            if key in self._closed:
                return True

        path = self._path(source, symbol)

        if not os.path.exists(path):
            return False

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            candles = data.get("candles", [])

            parsed: List[Candle] = []

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
                except (
                    KeyError,
                    TypeError,
                    ValueError,
                ):
                    continue

            parsed.sort(key=lambda c: c.open_time)

            with self._lock:
                self._closed[key] = deque(
                    parsed[-self.max_candles:],
                    maxlen=self.max_candles,
                )

                current = data.get("current")

                if current:
                    try:
                        self._current[key] = Candle(
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
                    except (
                        KeyError,
                        TypeError,
                        ValueError,
                    ):
                        log.warning(
                            "INVALID CURRENT CANDLE | "
                            "source=%s symbol=%s",
                            source,
                            symbol,
                        )

            log.info(
                "HISTORY LOADED | source=%s symbol=%s candles=%d/%d",
                source,
                symbol,
                len(parsed[-self.max_candles:]),
                self.max_candles,
            )

            return True

        except (OSError, json.JSONDecodeError) as e:
            log.warning(
                "HISTORY LOAD ERROR | source=%s symbol=%s error=%s",
                source,
                symbol,
                e,
            )
            return False

    # ---------------------------------------------------------
    # bootstrap
    # ---------------------------------------------------------

    def seed(
        self,
        source: str,
        symbol: str,
        candles: List[Candle],
    ) -> None:
        source = self._normalize_source(source)

        if not candles:
            return

        key = (source, symbol)

        candles = sorted(
            candles,
            key=lambda c: c.open_time,
        )

        # Remove duplicate timestamps.
        unique = {}

        for candle in candles:
            unique[candle.open_time] = candle

        candles = sorted(
            unique.values(),
            key=lambda c: c.open_time,
        )

        with self._lock:
            self._closed[key] = deque(
                candles[-self.max_candles:],
                maxlen=self.max_candles,
            )

            self._dirty.add(key)

        log.info(
            "HISTORY SEEDED | source=%s symbol=%s candles=%d/%d",
            source,
            symbol,
            min(len(candles), self.max_candles),
            self.max_candles,
        )

    # ---------------------------------------------------------
    # update
    # ---------------------------------------------------------

    def update(
        self,
        source: str,
        symbol: str,
        candle: Candle,
    ) -> str:
        """
        Returns:

            current
                Same currently-open candle was updated.

            closed
                Previous current candle closed and was moved
                into the historical queue.

            new
                A closed candle was directly inserted.

            ignored
                Old/out-of-order candle.
        """

        source = self._normalize_source(source)
        key = (source, symbol)

        with self._lock:
            history = self._closed.setdefault(
                key,
                deque(maxlen=self.max_candles),
            )

            current = self._current.get(key)

            # First observation.
            if current is None:
                self._current[key] = candle
                return "current"

            # Same 5m candle: update its live values.
            if candle.open_time == current.open_time:
                self._current[key] = candle
                return "current"

            # Newer candle.
            if candle.open_time > current.open_time:

                # The old candle is now closed.
                if current.quote_volume > 0:
                    self._append_closed_locked(
                        key,
                        current,
                    )

                self._current[key] = candle

                self._dirty.add(key)

                log.debug(
                    "CANDLE CLOSED | source=%s symbol=%s "
                    "open_time=%s history=%d/%d",
                    source,
                    symbol,
                    current.open_time,
                    len(history),
                    self.max_candles,
                )

                return "closed"

            # Older candle.
            return "ignored"

    def _append_closed_locked(
        self,
        key: Tuple[str, str],
        candle: Candle,
    ) -> bool:

        history = self._closed.setdefault(
            key,
            deque(maxlen=self.max_candles),
        )

        if history:
            if candle.open_time == history[-1].open_time:
                history[-1] = candle
                return True

            if candle.open_time < history[-1].open_time:
                return False

        history.append(candle)

        return True

    def add_closed(
        self,
        source: str,
        symbol: str,
        candle: Candle,
    ) -> None:

        source = self._normalize_source(source)
        key = (source, symbol)

        with self._lock:
            inserted = self._append_closed_locked(
                key,
                candle,
            )

            if inserted:
                self._dirty.add(key)

    # ---------------------------------------------------------
    # getters
    # ---------------------------------------------------------

    def get_current(
        self,
        source: str,
        symbol: str,
    ) -> Optional[Candle]:

        source = self._normalize_source(source)
        key = (source, symbol)

        with self._lock:
            return self._current.get(key)

    def get_closed(
        self,
        source: str,
        symbol: str,
    ) -> List[Candle]:

        source = self._normalize_source(source)
        key = (source, symbol)

        with self._lock:
            return list(
                self._closed.get(key, ())
            )

    def get_recent(
        self,
        source: str,
        symbol: str,
        count: int,
    ) -> List[Candle]:

        if count <= 0:
            return []

        source = self._normalize_source(source)
        key = (source, symbol)

        with self._lock:
            history = self._closed.get(key, ())

            return list(history)[-count:]

    def count(
        self,
        source: str,
        symbol: str,
    ) -> int:

        source = self._normalize_source(source)
        key = (source, symbol)

        with self._lock:
            return len(
                self._closed.get(key, ())
            )

    def is_ready(
        self,
        source: str,
        symbol: str,
        required: int,
    ) -> bool:

        return self.count(
            source,
            symbol,
        ) >= required

    # ---------------------------------------------------------
    # statistics
    # ---------------------------------------------------------

    def average_quote_volume(
        self,
        source: str,
        symbol: str,
        count: int = SMART_MONEY_CANDLES,
    ) -> Optional[float]:

        candles = self.get_recent(
            source,
            symbol,
            count,
        )

        if len(candles) < count:
            return None

        values = [
            c.quote_volume
            for c in candles
            if c.quote_volume > 0
        ]

        if len(values) < count:
            return None

        # IMPORTANT:
        # Raw arithmetic mean is intentional.
        # No trimmed mean, no winsorization, no normalization.
        return sum(values) / len(values)

    def average_volume(
        self,
        source: str,
        symbol: str,
        count: int = SMART_MONEY_CANDLES,
    ) -> Optional[float]:

        candles = self.get_recent(
            source,
            symbol,
            count,
        )

        if len(candles) < count:
            return None

        values = [
            c.volume
            for c in candles
            if c.volume > 0
        ]

        if len(values) < count:
            return None

        return sum(values) / len(values)

    # ---------------------------------------------------------
    # latest closed candle
    # ---------------------------------------------------------

    def latest_closed(
        self,
        source: str,
        symbol: str,
    ) -> Optional[Candle]:

        source = self._normalize_source(source)
        key = (source, symbol)

        with self._lock:
            history = self._closed.get(key)

            if not history:
                return None

            return history[-1]

    # ---------------------------------------------------------
    # persistence
    # ---------------------------------------------------------

    def save(
        self,
        source: str,
        symbol: str,
    ) -> None:

        source = self._normalize_source(source)
        key = (source, symbol)

        with self._lock:
            history = list(
                self._closed.get(key, ())
            )

            current = self._current.get(key)

            payload = {
                "source": source,
                "symbol": symbol,
                "interval": "5m",
                "max_candles": self.max_candles,
                "updated_at": int(time.time() * 1000),
                "candles": [
                    asdict(c)
                    for c in history
                ],
                "current": (
                    asdict(current)
                    if current
                    else None
                ),
            }

        path = self._path(
            source,
            symbol,
        )

        tmp = f"{path}.tmp"

        try:
            with open(
                tmp,
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(
                    payload,
                    f,
                    separators=(",", ":"),
                )

            os.replace(
                tmp,
                path,
            )

            with self._lock:
                self._dirty.discard(key)

            log.debug(
                "HISTORY SAVED | source=%s symbol=%s candles=%d",
                source,
                symbol,
                len(history),
            )

        except OSError as e:
            log.warning(
                "HISTORY SAVE ERROR | source=%s symbol=%s error=%s",
                source,
                symbol,
                e,
            )

    def save_dirty(self) -> None:

        with self._lock:
            keys = list(self._dirty)

        for source, symbol in keys:
            self.save(
                source,
                symbol,
            )

    def save_all(self) -> None:

        with self._lock:
            keys = set(
                self._closed.keys()
            ) | set(
                self._current.keys()
            )

        for source, symbol in keys:
            self.save(
                source,
                symbol,
            )

    # ---------------------------------------------------------
    # diagnostics
    # ---------------------------------------------------------

    def status(
        self,
        source: str,
        symbol: str,
    ) -> dict:

        source = self._normalize_source(source)

        return {
            "source": source,
            "symbol": symbol,
            "closed_candles": self.count(
                source,
                symbol,
            ),
            "required_for_smart_money": SMART_MONEY_CANDLES,
            "required_for_pump_dump": PUMP_DUMP_CANDLES,
            "has_current": (
                self.get_current(
                    source,
                    symbol,
                )
                is not None
            ),
        }