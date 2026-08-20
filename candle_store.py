"""
Persistent source-isolated rolling 5-minute candle store.

Rules:
    - 864 closed candles = 72 hours of 5m history.
    - Current/open candle is stored separately.
    - Binance and KuCoin are COMPLETELY isolated.
    - A Binance candle can never enter the KuCoin baseline.
    - A KuCoin candle can never enter the Binance baseline.
    - The current/open candle is NEVER used for closed-candle signals.
    - New closed candles automatically push the oldest candle out.
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
            volume=float(raw[5]),
            close_time=int(raw[6]),
            quote_volume=float(raw[7]),
            trades=int(raw[8]),
        )

    @classmethod
    def from_dict(cls, data: dict) -> "Candle":
        return cls(
            open_time=int(data["open_time"]),
            close_time=int(data["close_time"]),
            open=float(data["open"]),
            high=float(data["high"]),
            low=float(data["low"]),
            close=float(data["close"]),
            volume=float(data["volume"]),
            quote_volume=float(data["quote_volume"]),
            trades=int(data.get("trades", 0)),
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

        # IMPORTANT:
        # source -> symbol -> deque
        self._closed: Dict[str, Dict[str, deque]] = {
            source: {} for source in SUPPORTED_SOURCES
        }

        # source -> symbol -> current candle
        self._current: Dict[str, Dict[str, Candle]] = {
            source: {} for source in SUPPORTED_SOURCES
        }

        self._dirty = {
            source: set() for source in SUPPORTED_SOURCES
        }

        os.makedirs(self.root_path, exist_ok=True)

        log.info(
            "CANDLE STORE initialized | path=%s | limit=%s | sources=%s",
            self.root_path,
            self.max_candles,
            ",".join(SUPPORTED_SOURCES),
        )

    # ---------------------------------------------------------
    # validation
    # ---------------------------------------------------------

    def _validate_source(self, source: str) -> str:
        source = (source or "").strip().lower()

        if source not in SUPPORTED_SOURCES:
            raise ValueError(
                f"Unsupported candle source: {source!r}. "
                f"Supported: {SUPPORTED_SOURCES}"
            )

        return source

    # ---------------------------------------------------------
    # paths
    # ---------------------------------------------------------

    def _path(self, source: str, symbol: str) -> str:
        source = self._validate_source(source)

        safe_symbol = "".join(
            c for c in symbol
            if c.isalnum() or c in ("_", "-")
        )

        source_dir = os.path.join(self.root_path, source)
        os.makedirs(source_dir, exist_ok=True)

        return os.path.join(
            source_dir,
            f"{safe_symbol}.json",
        )

    # ---------------------------------------------------------
    # loading
    # ---------------------------------------------------------

    def load(self, source: str, symbol: str) -> bool:
        source = self._validate_source(source)

        with self._lock:
            if symbol in self._closed[source]:
                return True

        path = self._path(source, symbol)

        if not os.path.exists(path):
            log.debug(
                "CANDLE LOAD | source=%s | symbol=%s | result=NOT_FOUND",
                source,
                symbol,
            )
            return False

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            file_source = str(data.get("source", "")).lower()

            # Never load a file into another source.
            if file_source and file_source != source:
                log.error(
                    "CANDLE SOURCE MISMATCH | requested=%s | file_source=%s | symbol=%s",
                    source,
                    file_source,
                    symbol,
                )
                return False

            candles = data.get("candles", [])

            parsed: List[Candle] = []

            for item in candles:
                try:
                    parsed.append(Candle.from_dict(item))
                except (KeyError, TypeError, ValueError):
                    continue

            parsed.sort(key=lambda x: x.open_time)

            with self._lock:
                self._closed[source][symbol] = deque(
                    parsed[-self.max_candles:],
                    maxlen=self.max_candles,
                )

                current = data.get("current")

                if current:
                    try:
                        self._current[source][symbol] = Candle.from_dict(current)
                    except (KeyError, TypeError, ValueError):
                        log.warning(
                            "CANDLE LOAD | invalid current candle | source=%s | symbol=%s",
                            source,
                            symbol,
                        )

            log.info(
                "CANDLE LOAD | source=%s | symbol=%s | closed=%d/%d | current=%s",
                source,
                symbol,
                len(parsed),
                self.max_candles,
                "yes" if current else "no",
            )

            return True

        except (OSError, json.JSONDecodeError) as e:
            log.error(
                "CANDLE LOAD ERROR | source=%s | symbol=%s | error=%s",
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
        source = self._validate_source(source)

        if not candles:
            log.warning(
                "CANDLE SEED | source=%s | symbol=%s | result=EMPTY",
                source,
                symbol,
            )
            return

        candles = sorted(
            candles,
            key=lambda x: x.open_time,
        )

        with self._lock:
            self._closed[source][symbol] = deque(
                candles[-self.max_candles:],
                maxlen=self.max_candles,
            )

            self._dirty[source].add(symbol)

            count = len(self._closed[source][symbol])

        log.info(
            "CANDLE SEED | source=%s | symbol=%s | stored=%d/%d",
            source,
            symbol,
            count,
            self.max_candles,
        )

    # ---------------------------------------------------------
    # candle update
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
                Existing open candle updated.

            closed
                Previous open candle became closed and was inserted.

            new
                New candle inserted as closed data.

            ignored
                Out-of-order candle.
        """

        source = self._validate_source(source)

        with self._lock:
            history = self._closed[source].setdefault(
                symbol,
                deque(maxlen=self.max_candles),
            )

            current = self._current[source].get(symbol)

            if current is None:
                self._current[source][symbol] = candle

                log.debug(
                    "CANDLE UPDATE | source=%s | symbol=%s | result=current | open_time=%s",
                    source,
                    symbol,
                    candle.open_time,
                )

                return "current"

            # Same 5m candle: update live candle.
            if candle.open_time == current.open_time:
                self._current[source][symbol] = candle

                return "current"

            # Newer candle arrived.
            if candle.open_time > current.open_time:
                history.append(current)

                self._current[source][symbol] = candle
                self._dirty[source].add(symbol)

                log.info(
                    "CANDLE CLOSED | source=%s | symbol=%s | closed_open=%s | history=%d/%d",
                    source,
                    symbol,
                    current.open_time,
                    len(history),
                    self.max_candles,
                )

                return "closed"

            log.debug(
                "CANDLE UPDATE | source=%s | symbol=%s | result=ignored_old_candle",
                source,
                symbol,
            )

            return "ignored"

    def add_closed(
        self,
        source: str,
        symbol: str,
        candle: Candle,
    ) -> None:
        source = self._validate_source(source)

        with self._lock:
            history = self._closed[source].setdefault(
                symbol,
                deque(maxlen=self.max_candles),
            )

            if history and candle.open_time <= history[-1].open_time:
                return

            history.append(candle)
            self._dirty[source].add(symbol)

        log.debug(
            "CANDLE ADD CLOSED | source=%s | symbol=%s | history=%d/%d",
            source,
            symbol,
            self.count(source, symbol),
            self.max_candles,
        )

    # ---------------------------------------------------------
    # getters
    # ---------------------------------------------------------

    def get_current(
        self,
        source: str,
        symbol: str,
    ) -> Optional[Candle]:
        source = self._validate_source(source)

        with self._lock:
            return self._current[source].get(symbol)

    def get_closed(
        self,
        source: str,
        symbol: str,
    ) -> List[Candle]:
        source = self._validate_source(source)

        with self._lock:
            return list(
                self._closed[source].get(symbol, ())
            )

    def get_recent(
        self,
        source: str,
        symbol: str,
        count: int,
    ) -> List[Candle]:
        source = self._validate_source(source)

        if count <= 0:
            return []

        with self._lock:
            history = self._closed[source].get(
                symbol,
                (),
            )

            return list(history)[-count:]

    def count(
        self,
        source: str,
        symbol: str,
    ) -> int:
        source = self._validate_source(source)

        with self._lock:
            return len(
                self._closed[source].get(symbol, ())
            )

    # ---------------------------------------------------------
    # statistics
    # ---------------------------------------------------------

    def average_quote_volume(
        self,
        source: str,
        symbol: str,
        count: int = BASELINE_CANDLES,
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

        if len(values) < max(10, count // 2):
            return None

        # IMPORTANT:
        # Plain arithmetic mean.
        # No trimmed mean / normalization.
        return sum(values) / len(values)

    def average_volume(
        self,
        source: str,
        symbol: str,
        count: int = BASELINE_CANDLES,
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

        if len(values) < max(10, count // 2):
            return None

        return sum(values) / len(values)

    # ---------------------------------------------------------
    # persistence
    # ---------------------------------------------------------

    def save(
        self,
        source: str,
        symbol: str,
    ) -> None:
        source = self._validate_source(source)

        with self._lock:
            history = list(
                self._closed[source].get(symbol, ())
            )

            current = self._current[source].get(symbol)

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

        path = self._path(source, symbol)
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

            os.replace(tmp, path)

            with self._lock:
                self._dirty[source].discard(symbol)

            log.debug(
                "CANDLE SAVE | source=%s | symbol=%s | closed=%d",
                source,
                symbol,
                len(history),
            )

        except OSError as e:
            log.error(
                "CANDLE SAVE ERROR | source=%s | symbol=%s | error=%s",
                source,
                symbol,
                e,
            )

    def save_dirty(self) -> None:
        with self._lock:
            pending = [
                (source, symbol)
                for source in SUPPORTED_SOURCES
                for symbol in self._dirty[source]
            ]

        for source, symbol in pending:
            self.save(source, symbol)

    def save_all(self) -> None:
        with self._lock:
            pending = [
                (source, symbol)
                for source in SUPPORTED_SOURCES
                for symbol in self._closed[source].keys()
            ]

        for source, symbol in pending:
            self.save(source, symbol)

    # ---------------------------------------------------------
    # diagnostics
    # ---------------------------------------------------------

    def get_status(
        self,
        source: str,
        symbol: str,
    ) -> dict:
        source = self._validate_source(source)

        with self._lock:
            return {
                "source": source,
                "symbol": symbol,
                "closed": len(
                    self._closed[source].get(symbol, ())
                ),
                "max": self.max_candles,
                "has_current": symbol in self._current[source],
                "dirty": symbol in self._dirty[source],
            }