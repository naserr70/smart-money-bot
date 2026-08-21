"""
Persistent independent rolling 5-minute candle store.

IMPORTANT:
- Binance, Bybit and KuCoin histories are completely independent.
- Maximum history = 864 CLOSED 5m candles = 72 hours.
- The currently-open candle is NOT part of the closed history.
- A new closed candle replaces the oldest candle automatically.
- Signal calculations must use closed candles only.
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
SMART_MONEY_BASELINE_CANDLES = 48
PUMP_HISTORY_CANDLES = 864

VALID_SOURCES = ("binance", "bybit", "kucoin")


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
            close_time=int(raw[6]),
            open=float(raw[1]),
            high=float(raw[2]),
            low=float(raw[3]),
            close=float(raw[4]),
            volume=float(raw[5]),
            quote_volume=float(raw[7]),
            trades=int(raw[8]),
        )

    @classmethod
    def from_bybit(cls, raw: list) -> "Candle":
        """
        Bybit v5 kline list item (already oldest-first after reverse):
        [startTime, open, high, low, close, volume, turnover]

        startTime is ms. turnover is quote volume (USDT for spot).
        """
        open_ms = int(float(raw[0]))
        return cls(
            open_time=open_ms,
            close_time=open_ms + 5 * 60 * 1000 - 1,
            open=float(raw[1]),
            high=float(raw[2]),
            low=float(raw[3]),
            close=float(raw[4]),
            volume=float(raw[5]),
            quote_volume=float(raw[6]),
            trades=0,
        )

    @classmethod
    def from_kucoin(cls, raw: list) -> "Candle":
        """
        KuCoin kline format (official):
        [time, open, close, high, low, volume, turnover]

        time is in seconds (unix).
        """
        open_ms = int(float(raw[0]) * 1000)
        return cls(
            open_time=open_ms,
            close_time=open_ms + 5 * 60 * 1000 - 1,
            open=float(raw[1]),
            close=float(raw[2]),
            high=float(raw[3]),
            low=float(raw[4]),
            volume=float(raw[5]),
            quote_volume=float(raw[6]),
            trades=0,
        )


class CandleStore:
    """
    Histories are physically separated by source:

        market_history/binance/...
        market_history/bybit/...
        market_history/kucoin/...

    NEVER mix sources.
    """

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
        del kwargs

        self.root_path = root_path
        self.max_candles = max_candles
        self._lock = threading.RLock()

        self._closed: Dict[str, Dict[str, deque]] = {
            src: {} for src in VALID_SOURCES
        }

        self._current: Dict[str, Dict[str, Candle]] = {
            src: {} for src in VALID_SOURCES
        }

        self._dirty = {
            src: set() for src in VALID_SOURCES
        }

        self.github_enabled = bool(github_enabled and github_token and github_repo)
        self.github_token = github_token or ""
        self.github_repo = github_repo or ""
        self.github_branch = github_branch or "main"
        self.github_sync_interval_sec = max(60, int(github_sync_interval_sec))

        os.makedirs(self.root_path, exist_ok=True)
        for src in VALID_SOURCES:
            os.makedirs(os.path.join(self.root_path, src), exist_ok=True)

    @staticmethod
    def normalize_source(source: str) -> str:
        source = source.lower().strip()

        if source not in VALID_SOURCES:
            raise ValueError(f"Unsupported candle source: {source}")

        return source

    def _path(self, source: str, symbol: str) -> str:
        source = self.normalize_source(source)

        safe = "".join(
            c for c in symbol
            if c.isalnum() or c in ("_", "-")
        )

        return os.path.join(
            self.root_path,
            source,
            f"{safe}.json",
        )

    def load(self, source: str, symbol: str) -> bool:
        source = self.normalize_source(source)

        with self._lock:
            if symbol in self._closed[source]:
                return True

        path = self._path(source, symbol)

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

            parsed.sort(key=lambda c: c.open_time)

            current = None
            raw_current = data.get("current")

            if raw_current:
                try:
                    current = Candle(
                        open_time=int(raw_current["open_time"]),
                        close_time=int(raw_current["close_time"]),
                        open=float(raw_current["open"]),
                        high=float(raw_current["high"]),
                        low=float(raw_current["low"]),
                        close=float(raw_current["close"]),
                        volume=float(raw_current["volume"]),
                        quote_volume=float(raw_current["quote_volume"]),
                        trades=int(raw_current.get("trades", 0)),
                    )
                except (KeyError, TypeError, ValueError):
                    current = None

            with self._lock:
                self._closed[source][symbol] = deque(
                    parsed[-self.max_candles:],
                    maxlen=self.max_candles,
                )

                if current:
                    # A "current" candle is only meaningful if it's strictly
                    # newer than the latest CLOSED candle we just loaded. A
                    # stale current (e.g. saved right before a restart, while
                    # the closed history on disk is from further back) must
                    # never be kept — update() would later append it onto the
                    # end of the closed deque as if it were the newest candle,
                    # silently breaking chronological order and corrupting
                    # every baseline/z-score calculation downstream.
                    last_closed = parsed[-1] if parsed else None
                    if last_closed is not None and current.open_time <= last_closed.open_time:
                        log.warning(
                            "HISTORY LOAD | source=%s symbol=%s "
                            "stale current candle discarded "
                            "(current_open_time=%s <= last_closed_open_time=%s)",
                            source,
                            symbol,
                            current.open_time,
                            last_closed.open_time,
                        )
                    else:
                        self._current[source][symbol] = current

            log.info(
                "HISTORY LOADED | source=%s symbol=%s candles=%s/%s current=%s",
                source,
                symbol,
                len(parsed),
                self.max_candles,
                bool(current),
            )

            return True

        except (OSError, json.JSONDecodeError) as e:
            log.warning(
                "HISTORY LOAD FAILED | source=%s symbol=%s error=%s",
                source,
                symbol,
                e,
            )
            return False

    def seed(
        self,
        source: str,
        symbol: str,
        candles: List[Candle],
    ) -> None:

        source = self.normalize_source(source)

        if not candles:
            log.warning(
                "HISTORY SEED EMPTY | source=%s symbol=%s",
                source,
                symbol,
            )
            return

        candles = sorted(
            candles,
            key=lambda c: c.open_time,
        )

        unique = {}

        for candle in candles:
            unique[candle.open_time] = candle

        candles = sorted(
            unique.values(),
            key=lambda c: c.open_time,
        )

        with self._lock:
            self._closed[source][symbol] = deque(
                candles[-self.max_candles:],
                maxlen=self.max_candles,
            )

            # seed() wholesale-replaces the closed history (bootstrap /
            # GitHub restore / fresh API fetch). Any previously-tracked
            # "current" candle was relative to the OLD history and its
            # relationship to this new history is not guaranteed — keeping
            # it around risks update() later appending a stale/out-of-order
            # candle onto the end of the freshly-seeded deque. Drop it; the
            # next live tick will establish a correct new current.
            self._current[source].pop(symbol, None)

            self._dirty[source].add(symbol)

        log.info(
            "HISTORY SEEDED | source=%s symbol=%s candles=%s/%s",
            source,
            symbol,
            len(candles[-self.max_candles:]),
            self.max_candles,
        )

    def update(
        self,
        source: str,
        symbol: str,
        candle: Candle,
    ) -> str:

        source = self.normalize_source(source)

        with self._lock:

            history = self._closed[source].setdefault(
                symbol,
                deque(maxlen=self.max_candles),
            )

            current = self._current[source].get(symbol)

            if current is None:

                self._current[source][symbol] = candle

                log.debug(
                    "CURRENT CANDLE CREATED | source=%s symbol=%s open_time=%s",
                    source,
                    symbol,
                    candle.open_time,
                )

                return "current"

            if candle.open_time == current.open_time:

                self._current[source][symbol] = candle

                return "current"

            if candle.open_time > current.open_time:

                closed_candle = current

                # Defensive guard: never append a candle that would break
                # the deque's strictly-increasing open_time ordering (e.g.
                # a stale "current" left over from a restart/restore race).
                # Every downstream calculation (_baseline_mean slicing,
                # z-score, candle_price_change) assumes this history is in
                # chronological order — silently violating it corrupts
                # signals without raising any error.
                if history and closed_candle.open_time <= history[-1].open_time:
                    log.warning(
                        "CANDLE DISCARDED OUT_OF_ORDER | source=%s symbol=%s "
                        "stale_current_open_time=%s <= last_closed_open_time=%s",
                        source,
                        symbol,
                        closed_candle.open_time,
                        history[-1].open_time,
                    )
                    self._current[source][symbol] = candle
                    self._dirty[source].add(symbol)
                    return "current"

                history.append(closed_candle)

                self._current[source][symbol] = candle

                self._dirty[source].add(symbol)

                log.info(
                    "CANDLE CLOSED | source=%s symbol=%s open_time=%s quote_volume=%.2f history=%s/%s",
                    source,
                    symbol,
                    closed_candle.open_time,
                    closed_candle.quote_volume,
                    len(history),
                    self.max_candles,
                )

                return "closed"

            log.warning(
                "CANDLE IGNORED OUT_OF_ORDER | source=%s symbol=%s incoming=%s current=%s",
                source,
                symbol,
                candle.open_time,
                current.open_time,
            )

            return "ignored"

    def add_closed(
        self,
        source: str,
        symbol: str,
        candle: Candle,
    ) -> bool:

        source = self.normalize_source(source)

        with self._lock:

            history = self._closed[source].setdefault(
                symbol,
                deque(maxlen=self.max_candles),
            )

            if history:

                if candle.open_time == history[-1].open_time:
                    history[-1] = candle
                    self._dirty[source].add(symbol)
                    return True

                if candle.open_time < history[-1].open_time:
                    return False

            history.append(candle)

            self._dirty[source].add(symbol)

            return True

    def apply_recent(
        self,
        source: str,
        symbol: str,
        candles: List[Candle],
    ) -> int:
        if not candles:
            return 0

        candles = sorted(candles, key=lambda c: c.open_time)
        closed_count = 0

        for candle in candles:
            result = self.update(source, symbol, candle)
            if result == "closed":
                closed_count += 1

        return closed_count

    def get_current(
        self,
        source: str,
        symbol: str,
    ) -> Optional[Candle]:

        source = self.normalize_source(source)

        with self._lock:
            return self._current[source].get(symbol)

    def get_closed(
        self,
        source: str,
        symbol: str,
    ) -> List[Candle]:

        source = self.normalize_source(source)

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

        if count <= 0:
            return []

        source = self.normalize_source(source)

        with self._lock:
            history = self._closed[source].get(symbol, ())

            return list(history)[-count:]

    def count(
        self,
        source: str,
        symbol: str,
    ) -> int:

        source = self.normalize_source(source)

        with self._lock:
            return len(
                self._closed[source].get(symbol, ())
            )

    def average_quote_volume(
        self,
        source: str,
        symbol: str,
        count: int = SMART_MONEY_BASELINE_CANDLES,
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

        return sum(values) / len(values)

    def average_quote_volume_long(
        self,
        source: str,
        symbol: str,
        count: int = PUMP_HISTORY_CANDLES,
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

        return sum(values) / len(values)

    def price_returns(
        self,
        source: str,
        symbol: str,
        count: int = PUMP_HISTORY_CANDLES,
    ) -> List[float]:

        candles = self.get_recent(
            source,
            symbol,
            count,
        )

        returns = []

        for previous, current in zip(
            candles,
            candles[1:],
        ):

            if previous.close <= 0:
                continue

            change = (
                (current.close - previous.close)
                / previous.close
            ) * 100

            returns.append(change)

        return returns

    def dirty_symbols(self, source: str) -> List[str]:

        source = self.normalize_source(source)

        with self._lock:
            return list(self._dirty[source])

    def to_payload(
        self,
        source: str,
        symbol: str,
    ) -> dict:

        source = self.normalize_source(source)

        with self._lock:

            history = list(
                self._closed[source].get(symbol, ())
            )

            current = self._current[source].get(symbol)

        return {
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

    def save(
        self,
        source: str,
        symbol: str,
    ) -> None:

        source = self.normalize_source(source)

        payload = self.to_payload(
            source,
            symbol,
        )

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
                    ensure_ascii=False,
                    separators=(",", ":"),
                )

            os.replace(
                tmp,
                path,
            )

            with self._lock:
                self._dirty[source].discard(symbol)

        except OSError as e:

            log.warning(
                "HISTORY LOCAL SAVE FAILED | source=%s symbol=%s error=%s",
                source,
                symbol,
                e,
            )

    def save_dirty(
        self,
        source: Optional[str] = None,
    ) -> None:

        sources = (
            [self.normalize_source(source)]
            if source
            else list(VALID_SOURCES)
        )

        for src in sources:

            for symbol in self.dirty_symbols(src):
                self.save(src, symbol)

    def save_all(self) -> None:

        for source in VALID_SOURCES:

            with self._lock:
                symbols = list(
                    self._closed[source].keys()
                )

            for symbol in symbols:
                self.save(source, symbol)
