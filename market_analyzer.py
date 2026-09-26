"""
Independent exchange-aware market analyzer.

Signal logic
------------
1. Smart money:
   Latest CLOSED 5m quote_volume vs mean of previous 48 CLOSED
   5m candles from the SAME exchange.

2. Pump / Dump:
   Rolling volume anomaly + price/z-score checks.

3. Active analysis priority:
   Binance → Bybit → KuCoin.

4. Histories are NEVER mixed across exchanges.

History lifecycle
-----------------
Startup:
    local filesystem
        ↓
    GitHub backup
        ↓
    exchange API ONLY if still incomplete
        ↓
    save locally

Runtime:
    NEVER re-download the full 864-candle history.
    ONLY update the latest closed candles.

This is important on Render because the filesystem is ephemeral
and Binance rate limits / KuCoin pagination must not be triggered
repeatedly on every market cycle.
"""

import logging
import statistics
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

from assets import NOBITEX_ALL_ASSETS
from candle_store import (
    Candle,
    CandleStore,
    SMART_MONEY_BASELINE_CANDLES,
    PUMP_HISTORY_CANDLES,
    VALID_SOURCES,
)
from config import Settings
from formatting import esc
from market_data import MarketDataProvider
from signals import MarketSignal, SignalDirection, TriggerType
from state import BotState


log = logging.getLogger("smart_money_bot.market_analyzer")

# Only recent candles are requested during normal runtime. With the
# gap-sized catch-up below a normal cycle asks for 3 (previous closed,
# just-closed, current), which is all the analysis needs.
LIVE_UPDATE_LIMIT = 3

CANDLE_INTERVAL_MS = 5 * 60 * 1000

# Upper bound for one runtime fetch. MarketDataProvider.fetch_candles()
# switches to bootstrap mode at limit >= 100, so runtime must stay below it.
# A gap up to (this - 2) candles (~8h) is closed by the next live update.
MAX_LIVE_CATCHUP_CANDLES = 99

# Per-symbol analysis source priority.
SOURCE_PRIORITY = ("binance", "bybit", "kucoin")

# Stop trying GitHub restore during one startup after this many
# consecutive download errors (GitHub unreachable / token broken).
GITHUB_RESTORE_MAX_CONSECUTIVE_FAILURES = 5

# Candles are only kept up to date on the exchange each symbol is analysed
# on (not on all three), which cuts exchange traffic by ~60% on Render's
# metered bandwidth. When a symbol moves to another exchange (e.g. Binance
# down) its history there is backfilled on demand, a bounded number per
# cycle, and not retried for a while if the exchange has little history.
MAX_BACKFILLS_PER_CYCLE = 40
BACKFILL_RETRY_SEC = 6 * 3600


def _candles_from_payload(data: dict) -> List[Candle]:
    """
    Convert GitHub candle-store JSON payload into Candle objects.
    """
    if not isinstance(data, dict):
        return []

    raw_list = data.get("candles") or []

    if not isinstance(raw_list, list):
        return []

    parsed: List[Candle] = []

    for item in raw_list:
        if not isinstance(item, dict):
            continue

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

    return parsed


class MarketAnalyzer:

    def __init__(
        self,
        settings: Settings,
        state: BotState,
        session: requests.Session,
        candle_store: CandleStore,
    ):
        self.settings = settings
        self.state = state
        self.session = session

        self.provider = MarketDataProvider(
            session=session,
            timeout=settings.http_timeout_sec,
            binance_enabled=settings.binance_enabled,
            kucoin_enabled=settings.kucoin_enabled,
        )

        self.candle_store = candle_store

        # source:symbol -> last candle that produced a signal
        self._last_signaled_open_time: Dict[str, int] = {}

        # Bootstrap must execute once per process.
        self._startup_bootstrap_done = False

        # source:symbol -> last on-demand history backfill attempt (epoch s)
        self._backfill_attempted: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # HISTORY FRESHNESS HELPERS
    # ------------------------------------------------------------------

    def _smart_baseline_count(self) -> int:
        value = getattr(
            self.settings,
            "volume_baseline_candles",
            SMART_MONEY_BASELINE_CANDLES,
        )
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = SMART_MONEY_BASELINE_CANDLES
        return value if value > 0 else SMART_MONEY_BASELINE_CANDLES

    def _pump_history_count(self) -> int:
        value = getattr(
            self.settings,
            "pump_history_candles",
            PUMP_HISTORY_CANDLES,
        )
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = PUMP_HISTORY_CANDLES
        return value if value > 1 else PUMP_HISTORY_CANDLES

    @staticmethod
    def _ticker_is_live(ticker) -> bool:
        """A ticker with zero 24h quote volume is a halted/delisted market."""
        try:
            return float(
                (ticker or {}).get("quoteVolume", 0.0)
            ) > 0
        except (AttributeError, TypeError, ValueError):
            return False

    def _missing_candles(
        self,
        source: str,
        symbol: str,
    ) -> Optional[int]:
        """
        Number of 5m intervals between the newest candle we know
        (closed or current) and now. None when there is no history.
        """

        newest = None

        last_closed = self.candle_store.get_recent(
            source,
            symbol,
            1,
        )

        if last_closed:
            newest = last_closed[-1].open_time

        current = self.candle_store.get_current(
            source,
            symbol,
        )

        if current is not None and (
            newest is None
            or current.open_time > newest
        ):
            newest = current.open_time

        if newest is None:
            return None

        now_ms = int(time.time() * 1000)

        return max(
            0,
            (now_ms - newest) // CANDLE_INTERVAL_MS,
        )

    def _history_is_fresh(
        self,
        source: str,
        symbol: str,
    ) -> bool:
        """
        True when the next live update can reach back to the newest stored
        candle. A "full" (864) but hours-old history is NOT fresh: using it
        would leave a gap and close a half-built candle as final.
        """

        missing = self._missing_candles(
            source,
            symbol,
        )

        return (
            missing is not None
            and missing <= MAX_LIVE_CATCHUP_CANDLES - 2
        )

    def _select_analysis_sources(
        self,
        sources_tickers: Dict[str, Dict[str, dict]],
    ) -> Dict[str, Tuple[str, dict]]:
        """
        symbol -> (exchange, ticker): the highest-priority exchange
        (Binance → Bybit → KuCoin) where the symbol is actually trading.
        """

        selected: Dict[str, Tuple[str, dict]] = {}

        for source in SOURCE_PRIORITY:

            for symbol, ticker in (
                sources_tickers.get(source) or {}
            ).items():

                if symbol in selected:
                    continue

                if not self._ticker_is_live(ticker):
                    continue

                selected[symbol] = (
                    source,
                    ticker,
                )

        return selected

    def _needs_backfill(
        self,
        source: str,
        symbol: str,
    ) -> bool:
        """
        History can't be continued by a small live update: missing,
        older than the catch-up window, or too short to analyse.
        """

        missing = self._missing_candles(
            source,
            symbol,
        )

        if (
            missing is None
            or missing > MAX_LIVE_CATCHUP_CANDLES - 2
        ):
            return True

        return (
            self.candle_store.count(source, symbol)
            < self._smart_baseline_count() + 1
        )

    def _backfill_due(
        self,
        source: str,
        symbol: str,
    ) -> bool:
        last = self._backfill_attempted.get(
            f"{source}:{symbol}"
        )
        return (
            last is None
            or time.time() - last >= BACKFILL_RETRY_SEC
        )

    def _backfill_history(
        self,
        source: str,
        symbol: str,
    ) -> bool:

        self._backfill_attempted[
            f"{source}:{symbol}"
        ] = time.time()

        candles = self.provider.fetch_candles(
            source,
            symbol,
            self.candle_store.max_candles,
        )

        if not candles:
            return False

        self.candle_store.seed(
            source,
            symbol,
            candles,
        )

        return True

    def _live_fetch_limit(
        self,
        source: str,
        symbol: str,
    ) -> int:

        missing = self._missing_candles(
            source,
            symbol,
        )

        if missing is None:
            return LIVE_UPDATE_LIMIT

        return max(
            LIVE_UPDATE_LIMIT,
            min(
                MAX_LIVE_CATCHUP_CANDLES,
                missing + 2,
            ),
        )

    # ------------------------------------------------------------------
    # STARTUP HISTORY BOOTSTRAP
    # ------------------------------------------------------------------

    def bootstrap_histories(
        self,
        github_backup=None,
        symbols: Optional[List[str]] = None,
        target_count: int = PUMP_HISTORY_CANDLES,
    ) -> dict:
        """
        Restore histories BEFORE normal ticker discovery.

        Order:

            local
              ↓
            GitHub
              ↓
            Exchange API only for incomplete histories

        IMPORTANT:
        This method is the ONLY place where full historical bootstrap
        is performed.

        run_cycle() MUST NOT call full history bootstrap again.
        """

        if self._startup_bootstrap_done:
            log.info(
                "STARTUP BOOTSTRAP SKIPPED | already completed"
            )
            return {"skipped": True}

        target_count = max(1, int(target_count))

        symbol_list = list(
            symbols or NOBITEX_ALL_ASSETS
        )

        started = time.time()

        github_configured = bool(
            github_backup
            and github_backup.is_configured()
        )

        github_failures = 0

        log.info(
            "STARTUP BOOTSTRAP START | symbols=%s target=%s github=%s",
            len(symbol_list),
            target_count,
            github_configured,
        )

        stats = {
            "local_ok": 0,
            "github_restored": 0,
            "api_seeded": 0,
            "failed": 0,
            "already_full": 0,
            "sources": {},
        }

        # --------------------------------------------------------------
        # PHASE 1
        # Local + GitHub restoration
        #
        # DO NOT call ticker APIs here.
        # --------------------------------------------------------------

        incomplete: Dict[str, List[str]] = {
            source: []
            for source in VALID_SOURCES
        }

        for source in VALID_SOURCES:

            source_stats = {
                "symbols": len(symbol_list),
                "seeded": 0,
                "restored": 0,
                "incomplete": 0,
            }

            for symbol in symbol_list:

                try:

                    # Load existing local history.
                    self.candle_store.load(
                        source,
                        symbol,
                    )

                    count = self.candle_store.count(
                        source,
                        symbol,
                    )

                    fresh = self._history_is_fresh(
                        source,
                        symbol,
                    )

                    # --------------------------------------------------
                    # Already complete AND recent enough to be continued
                    # by the live update without a gap.
                    # --------------------------------------------------

                    if count >= target_count and fresh:

                        stats["already_full"] += 1
                        stats["local_ok"] += 1

                        log.debug(
                            "STARTUP HISTORY COMPLETE LOCAL | "
                            "source=%s symbol=%s candles=%s/%s",
                            source,
                            symbol,
                            count,
                            target_count,
                        )

                        continue

                    # Existing but incomplete or stale.
                    if count > 0:
                        stats["local_ok"] += 1

                    # --------------------------------------------------
                    # GitHub restore
                    # --------------------------------------------------

                    if github_configured:

                        try:

                            payload = github_backup.download(
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

                            payload = None

                            # Each failed download costs several seconds of
                            # retries; don't let an unreachable GitHub hold
                            # the first market cycle hostage for an hour.
                            github_failures += 1

                            if (
                                github_failures
                                >= GITHUB_RESTORE_MAX_CONSECUTIVE_FAILURES
                            ):

                                github_configured = False

                                log.warning(
                                    "GITHUB RESTORE SKIPPED FOR THIS STARTUP | "
                                    "consecutive_failures=%s",
                                    github_failures,
                                )

                        else:

                            github_failures = 0

                        if payload:

                            restored = _candles_from_payload(
                                payload
                            )

                            if restored:

                                self.candle_store.seed(
                                    source,
                                    symbol,
                                    restored,
                                )

                                count = (
                                    self.candle_store.count(
                                        source,
                                        symbol,
                                    )
                                )

                                fresh = self._history_is_fresh(
                                    source,
                                    symbol,
                                )

                                if count >= target_count and fresh:

                                    stats["github_restored"] += 1
                                    source_stats["restored"] += 1

                                    log.info(
                                        "GITHUB RESTORE OK | "
                                        "source=%s symbol=%s "
                                        "candles=%s/%s",
                                        source,
                                        symbol,
                                        count,
                                        target_count,
                                    )

                    # --------------------------------------------------
                    # Still incomplete or stale
                    # --------------------------------------------------

                    if count < target_count or not fresh:

                        incomplete[source].append(
                            symbol
                        )

                        source_stats["incomplete"] += 1

                except Exception:

                    stats["failed"] += 1

                    log.exception(
                        "STARTUP RESTORE ERROR | "
                        "source=%s symbol=%s",
                        source,
                        symbol,
                    )

            stats["sources"][source] = source_stats

        # --------------------------------------------------------------
        # PHASE 2
        #
        # Only now contact exchange ticker APIs.
        #
        # This phase is ONLY for incomplete histories.
        # --------------------------------------------------------------

        missing_total = sum(
            len(symbols)
            for symbols in incomplete.values()
        )

        if missing_total:

            log.info(
                "STARTUP HISTORY INCOMPLETE | "
                "missing_source_symbols=%s",
                missing_total,
            )

            try:

                (
                    binance_tickers,
                    bybit_tickers,
                    kucoin_tickers,
                ) = self.provider.fetch_all_sources()

            except Exception:

                log.exception(
                    "STARTUP MISSING-HISTORY TICKER FETCH FAILED"
                )

                binance_tickers = {}
                bybit_tickers = {}
                kucoin_tickers = {}

            source_tickers = {
                "binance": binance_tickers,
                "bybit": bybit_tickers,
                "kucoin": kucoin_tickers,
            }

            # Only the exchange each symbol is analysed on is seeded now;
            # the others are backfilled on demand if ever needed.
            analysis_source = {
                symbol: chosen
                for symbol, (chosen, _) in (
                    self._select_analysis_sources(
                        source_tickers
                    ).items()
                )
            }

            stats["deferred"] = 0

            # ----------------------------------------------------------
            # Seed each source independently.
            # ----------------------------------------------------------

            for source in VALID_SOURCES:

                tickers = source_tickers.get(
                    source,
                    {},
                )

                missing_symbols = incomplete[source]

                if not missing_symbols:
                    continue

                if not tickers:

                    log.warning(
                        "STARTUP API HISTORY SOURCE UNAVAILABLE | "
                        "source=%s missing=%s",
                        source,
                        len(missing_symbols),
                    )

                    continue

                for symbol in missing_symbols:

                    if analysis_source.get(symbol) != source:
                        stats["deferred"] += 1
                        continue

                    # --------------------------------------------------
                    # IMPORTANT:
                    # ticker existence does NOT guarantee candle
                    # endpoint support.
                    # --------------------------------------------------

                    if (
                        symbol not in tickers
                        or not self._ticker_is_live(
                            tickers.get(symbol)
                        )
                    ):

                        log.warning(
                            "STARTUP API HISTORY SYMBOL UNAVAILABLE | "
                            "source=%s symbol=%s",
                            source,
                            symbol,
                        )

                        continue

                    try:

                        log.info(
                            "STARTUP API HISTORY FETCH | "
                            "source=%s symbol=%s target=%s",
                            source,
                            symbol,
                            target_count,
                        )

                        self._backfill_attempted[
                            f"{source}:{symbol}"
                        ] = time.time()

                        candles = self.provider.fetch_candles(
                            source=source,
                            symbol=symbol,
                            limit=target_count,
                        )

                        if not candles:

                            stats["failed"] += 1

                            log.warning(
                                "STARTUP API SEED FAILED | "
                                "source=%s symbol=%s",
                                source,
                                symbol,
                            )

                            continue

                        self.candle_store.seed(
                            source,
                            symbol,
                            candles,
                        )

                        stored = self.candle_store.count(
                            source,
                            symbol,
                        )

                        if stored >= target_count:

                            stats["api_seeded"] += 1
                            stats["sources"][source]["seeded"] += 1

                            log.info(
                                "STARTUP API SEED OK | "
                                "source=%s symbol=%s "
                                "candles=%s/%s",
                                source,
                                symbol,
                                stored,
                                target_count,
                            )

                        else:

                            stats["failed"] += 1

                            log.warning(
                                "STARTUP API SEED INCOMPLETE | "
                                "source=%s symbol=%s "
                                "candles=%s/%s",
                                source,
                                symbol,
                                stored,
                                target_count,
                            )

                        # Small delay to reduce pressure on APIs.
                        time.sleep(0.05)

                    except Exception:

                        stats["failed"] += 1

                        log.exception(
                            "STARTUP API SEED ERROR | "
                            "source=%s symbol=%s",
                            source,
                            symbol,
                        )

        else:

            log.info(
                "STARTUP API HISTORY FETCH SKIPPED | "
                "all source histories already complete"
            )

        # --------------------------------------------------------------
        # Persist dirty local histories.
        # --------------------------------------------------------------

        try:
            self.candle_store.save_dirty()
        except Exception:
            log.exception(
                "STARTUP HISTORY SAVE FAILED"
            )

        elapsed = time.time() - started

        self._startup_bootstrap_done = True

        log.info(
            "STARTUP BOOTSTRAP COMPLETE | "
            "local_ok=%s github_restored=%s "
            "api_seeded=%s already_full=%s "
            "failed=%s elapsed=%.1fs",
            stats["local_ok"],
            stats["github_restored"],
            stats["api_seeded"],
            stats["already_full"],
            stats["failed"],
            elapsed,
        )

        return stats

    # ------------------------------------------------------------------
    # NORMAL MARKET CYCLE
    # ------------------------------------------------------------------

    def run_cycle(
        self,
    ) -> Tuple[List[MarketSignal], str, int]:

        log.info(
            "MARKET FETCH START"
        )

        try:

            (
                binance_tickers,
                bybit_tickers,
                kucoin_tickers,
            ) = self.provider.fetch_all_sources()

        except Exception:

            log.exception(
                "MARKET TICKER FETCH FAILED"
            )

            return [], "none", 0

        sources_tickers = {
            "binance": binance_tickers,
            "bybit": bybit_tickers,
            "kucoin": kucoin_tickers,
        }

        # --------------------------------------------------------------
        # IMPORTANT
        #
        # DO NOT CALL _maintain_history() HERE.
        #
        # Runtime only updates the latest candles; a full history is
        # fetched only by the startup bootstrap or, for a symbol whose
        # history on its analysis exchange is missing/stale, by the
        # bounded on-demand backfill in _live_update_candles().
        # --------------------------------------------------------------

        # --------------------------------------------------------------
        # Select analysis source PER SYMBOL.
        #
        # Binance → Bybit → KuCoin
        #
        # Previously a whole cycle used one exchange, so every Nobitex
        # asset missing from Binance spot (HYPE and ~20 others) was never
        # analysed while Binance was up. Each symbol now uses the highest
        # priority exchange where it is actually trading. Histories are
        # still never mixed across exchanges.
        # --------------------------------------------------------------

        if binance_tickers:

            active_source = "binance"

            log.info(
                "ACTIVE MARKET SOURCE | Binance PRIMARY"
            )

        elif bybit_tickers:

            active_source = "bybit"

            log.warning(
                "ACTIVE MARKET SOURCE | "
                "Bybit FALLBACK | Binance unavailable"
            )

        elif kucoin_tickers:

            active_source = "kucoin"

            log.warning(
                "ACTIVE MARKET SOURCE | "
                "KuCoin FALLBACK | "
                "Binance and Bybit unavailable"
            )

        else:

            log.error(
                "ACTIVE MARKET SOURCE FAILED | "
                "Binance, Bybit and KuCoin unavailable"
            )

            return [], "none", 0

        selected = self._select_analysis_sources(
            sources_tickers
        )

        # Keep candles current ONLY on the exchange each symbol is
        # analysed on (see MAX_BACKFILLS_PER_CYCLE).
        for source in SOURCE_PRIORITY:

            self._live_update_candles(
                source,
                {
                    symbol: ticker
                    for symbol, (chosen, ticker) in selected.items()
                    if chosen == source
                },
            )

        per_source = {
            source: sum(
                1
                for chosen, _ in selected.values()
                if chosen == source
            )
            for source in SOURCE_PRIORITY
        }

        log.info(
            "ANALYSIS START | primary=%s symbols=%s | "
            "binance=%s bybit=%s kucoin=%s",
            active_source,
            len(selected),
            per_source["binance"],
            per_source["bybit"],
            per_source["kucoin"],
        )

        signals: List[MarketSignal] = []

        for symbol, (source, ticker) in selected.items():

            try:

                signal = self._analyze_symbol(
                    source,
                    symbol,
                    ticker,
                )

                if signal is not None:
                    signals.append(signal)

            except Exception:

                log.exception(
                    "ANALYSIS ERROR | "
                    "source=%s symbol=%s",
                    source,
                    symbol,
                )

        log.info(
            "ANALYSIS COMPLETE | "
            "primary=%s signals=%s symbols=%s",
            active_source,
            len(signals),
            len(selected),
        )

        return (
            signals,
            active_source,
            len(selected),
        )

    # ------------------------------------------------------------------
    # RUNTIME LIVE UPDATE
    # ------------------------------------------------------------------

    def _live_update_candles(
        self,
        source: str,
        tickers: Dict[str, dict],
    ) -> None:

        if not tickers:
            return

        updated = 0
        closed_total = 0
        skipped_dead = 0
        gaps = 0
        backfilled = 0

        for symbol, ticker in tickers.items():

            try:

                # Halted/delisted markets (zero 24h volume) only return
                # stale candles — don't spend a request on them.
                if not self._ticker_is_live(ticker):
                    skipped_dead += 1
                    continue

                # Load local history only if not already loaded.
                if self.candle_store.count(
                    source,
                    symbol,
                ) == 0:

                    self.candle_store.load(
                        source,
                        symbol,
                    )

                # ------------------------------------------------------
                # Missing / stale / too-short history on this exchange
                # (e.g. the symbol just moved here from Binance): fetch
                # its closed history once, a bounded number per cycle.
                # ------------------------------------------------------

                if (
                    self._needs_backfill(source, symbol)
                    and self._backfill_due(source, symbol)
                ):

                    if backfilled >= MAX_BACKFILLS_PER_CYCLE:
                        # Wait for next cycle's quota; a small live
                        # update now would leave a permanent gap.
                        continue

                    backfilled += 1

                    if self._backfill_history(
                        source,
                        symbol,
                    ):
                        updated += 1

                    continue

                # ------------------------------------------------------
                # Normally 3 recent candles; after a pause/restart just
                # enough to reach back to the newest stored candle, so no
                # gap is created and a half-built candle is never closed
                # as final.
                # ------------------------------------------------------

                limit = self._live_fetch_limit(
                    source,
                    symbol,
                )

                missing = self._missing_candles(
                    source,
                    symbol,
                )

                if (
                    missing is not None
                    and missing > MAX_LIVE_CATCHUP_CANDLES - 2
                ):
                    gaps += 1

                candles = self.provider.fetch_candles(
                    source,
                    symbol,
                    limit,
                )

                if not candles:
                    continue

                closed = self.candle_store.apply_recent(
                    source,
                    symbol,
                    candles,
                )

                updated += 1
                closed_total += closed

            except Exception:

                log.exception(
                    "LIVE UPDATE ERROR | "
                    "source=%s symbol=%s",
                    source,
                    symbol,
                )

        log.info(
            "LIVE UPDATE DONE | "
            "source=%s symbols=%s newly_closed=%s "
            "backfilled=%s skipped_inactive=%s",
            source,
            updated,
            closed_total,
            backfilled,
            skipped_dead,
        )

        if gaps:

            log.warning(
                "LIVE UPDATE HISTORY GAP | source=%s symbols=%s | "
                "newest stored candle older than the live catch-up "
                "window; those histories contain a gap",
                source,
                gaps,
            )

    # ------------------------------------------------------------------
    # BASELINE
    # ------------------------------------------------------------------

    @staticmethod
    def _baseline_mean(
        candles: List,
        count: int,
    ) -> Optional[float]:

        if count <= 0:
            return None

        if len(candles) < count + 1:
            return None

        prior = candles[
            -(count + 1):-1
        ]

        if len(prior) != count:
            return None

        values = [
            float(c.quote_volume)
            for c in prior
            if c.quote_volume > 0
        ]

        if len(values) != count:
            return None

        return sum(values) / len(values)

    @staticmethod
    def _change_24h_from_history(
        history: List,
    ) -> Optional[float]:

        candles_24h = 24 * 60 * 60 * 1000 // CANDLE_INTERVAL_MS

        if len(history) <= candles_24h:
            return None

        current = history[-1]
        base = history[-(candles_24h + 1)]

        # Only when that candle really is 24h earlier (no gaps between).
        if (
            current.open_time - base.open_time
            != candles_24h * CANDLE_INTERVAL_MS
            or base.close <= 0
        ):
            return None

        return (
            (current.close - base.close)
            / base.close
        ) * 100.0

    # ------------------------------------------------------------------
    # SYMBOL ANALYSIS
    # ------------------------------------------------------------------

    def _analyze_symbol(
        self,
        source: str,
        symbol: str,
        ticker: dict,
    ) -> Optional[MarketSignal]:

        history = self.candle_store.get_closed(
            source,
            symbol,
        )

        # VOLUME_BASELINE_CANDLES / PUMP_HISTORY_CANDLES were defined in
        # config but ignored (hard-coded 48 / 864). Same defaults.
        smart_baseline_count = self._smart_baseline_count()
        pump_history_count = self._pump_history_count()

        minimum_smart = (
            smart_baseline_count + 1
        )

        if len(history) < minimum_smart:

            log.warning(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=INSUFFICIENT_VOLUME_HISTORY "
                "history=%s/%s",
                source,
                symbol,
                len(history),
                minimum_smart,
            )

            return None

        current_candle = history[-1]

        signal_key = (
            f"{source}:{symbol}"
        )

        last_ot = (
            self._last_signaled_open_time.get(
                signal_key
            )
        )

        if (
            last_ot is not None
            and current_candle.open_time <= last_ot
        ):
            return None

        current_volume = float(
            current_candle.quote_volume
        )

        if (
            current_volume <= 0
            or current_candle.open <= 0
        ):
            return None

        candle_price_change = (
            (
                current_candle.close
                - current_candle.open
            )
            / current_candle.open
        ) * 100.0

        # --------------------------------------------------------------
        # SMART MONEY — previous 48 CLOSED candles
        # --------------------------------------------------------------

        baseline_48 = self._baseline_mean(
            history,
            smart_baseline_count,
        )

        smart_spike = None
        smart_inflow = 0.0

        if (
            baseline_48 is not None
            and baseline_48 > 0
        ):

            smart_spike = (
                current_volume
                / baseline_48
            )

            smart_inflow = max(
                0.0,
                current_volume - baseline_48,
            )

            is_smart_volume_spike = (
                current_volume
                >= baseline_48
                * self.settings.volume_spike_ratio
            )

        else:

            is_smart_volume_spike = False

        smart_inflow_signal = (
            is_smart_volume_spike
            and candle_price_change > 0
        )

        smart_outflow_signal = (
            is_smart_volume_spike
            and candle_price_change < 0
        )

        # --------------------------------------------------------------
        # PUMP / DUMP
        # --------------------------------------------------------------

        pump_baseline_count = min(
            pump_history_count,
            len(history) - 1,
        )

        baseline_72h = None

        if (
            pump_baseline_count
            >= self.settings.pump_min_history_candles
        ):

            baseline_72h = self._baseline_mean(
                history,
                pump_baseline_count,
            )

        pump_spike = None

        if (
            baseline_72h is not None
            and baseline_72h > 0
        ):

            pump_spike = (
                current_volume
                / baseline_72h
            )

            is_pump_volume_spike = (
                current_volume
                >= baseline_72h
                * self.settings.volume_spike_ratio
            )

        else:

            is_pump_volume_spike = False

        long_history = history[
            -pump_history_count:
        ]

        current_close_to_close = None

        if (
            len(long_history) >= 2
            and long_history[-2].close > 0
        ):

            current_close_to_close = (
                (
                    current_candle.close
                    - long_history[-2].close
                )
                / long_history[-2].close
            ) * 100.0

        long_returns: List[float] = []

        for previous, current in zip(
            long_history,
            long_history[1:],
        ):

            if previous.close > 0:

                long_returns.append(
                    (
                        (
                            current.close
                            - previous.close
                        )
                        / previous.close
                    ) * 100.0
                )

        # --------------------------------------------------------------
        # Z-SCORE
        # --------------------------------------------------------------

        zscore = None

        if (
            self.settings.pump_zscore_enabled
            and current_close_to_close is not None
            and len(long_returns) >= 100
        ):

            baseline_returns = long_returns[:-1]

            if len(baseline_returns) >= 99:

                mean = statistics.mean(
                    baseline_returns
                )

                stdev = statistics.pstdev(
                    baseline_returns
                )

                if stdev > 0:

                    zscore = (
                        current_close_to_close
                        - mean
                    ) / stdev

        # --------------------------------------------------------------
        # STATIC PUMP / DUMP
        # --------------------------------------------------------------

        static_pump = (
            is_pump_volume_spike
            and self.settings.price_pump_min
            <= candle_price_change
            <= self.settings.price_pump_max
        )

        static_dump = (
            is_pump_volume_spike
            and -self.settings.price_pump_max
            <= candle_price_change
            <= -self.settings.price_pump_min
        )

        statistical_pump = (
            zscore is not None
            and zscore
            >= self.settings.pump_zscore_threshold
            and is_pump_volume_spike
        )

        statistical_dump = (
            zscore is not None
            and zscore
            <= -self.settings.pump_zscore_threshold
            and is_pump_volume_spike
        )

        is_pump = (
            static_pump
            or statistical_pump
        )

        is_dump = (
            static_dump
            or statistical_dump
        )

        # --------------------------------------------------------------
        # SIGNAL CLASSIFICATION
        # --------------------------------------------------------------

        if is_pump or is_dump:

            if is_pump:

                direction = (
                    SignalDirection.INFLOW
                )

                trigger = (
                    TriggerType.BOTH
                    if static_pump and statistical_pump
                    else (
                        TriggerType.STATISTICAL
                        if statistical_pump
                        else TriggerType.STATIC
                    )
                )

            else:

                direction = (
                    SignalDirection.OUTFLOW
                )

                trigger = (
                    TriggerType.BOTH
                    if static_dump and statistical_dump
                    else (
                        TriggerType.STATISTICAL
                        if statistical_dump
                        else TriggerType.STATIC
                    )
                )

            spike_multiplier = (
                pump_spike
                if pump_spike is not None
                else 0.0
            )

            estimated_inflow = max(
                0.0,
                current_volume
                - (baseline_72h or 0.0),
            )

            baseline_used = (
                baseline_72h or 0.0
            )

            path = "pump_dump_72h"

        elif (
            smart_inflow_signal
            or smart_outflow_signal
        ):

            direction = (
                SignalDirection.INFLOW
                if smart_inflow_signal
                else SignalDirection.OUTFLOW
            )

            trigger = TriggerType.STATIC

            spike_multiplier = (
                smart_spike
                if smart_spike is not None
                else 0.0
            )

            estimated_inflow = smart_inflow

            baseline_used = (
                baseline_48 or 0.0
            )

            path = "smart_money_48"

        else:

            self._last_signaled_open_time[
                signal_key
            ] = current_candle.open_time

            log.info(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=THRESHOLD_NOT_MET "
                "vol=%.2f baseline48=%s spike48=%s "
                "baseline72h=%s spike72h=%s "
                "required=%.2fX price=%.2f%% zscore=%s",
                source,
                symbol,
                current_volume,
                (
                    f"{baseline_48:.2f}"
                    if baseline_48 is not None
                    else "N/A"
                ),
                (
                    f"{smart_spike:.2f}X"
                    if smart_spike is not None
                    else "N/A"
                ),
                (
                    f"{baseline_72h:.2f}"
                    if baseline_72h is not None
                    else "N/A"
                ),
                (
                    f"{pump_spike:.2f}X"
                    if pump_spike is not None
                    else "N/A"
                ),
                self.settings.volume_spike_ratio,
                candle_price_change,
                (
                    f"{zscore:.2f}"
                    if zscore is not None
                    else "N/A"
                ),
            )

            return None

        # --------------------------------------------------------------
        # COOLDOWN
        # --------------------------------------------------------------

        cooldown_key = (
            f"market:{source}:{symbol}"
        )

        if self.state.is_in_cooldown(
            cooldown_key,
            self.settings.alert_cooldown_sec,
        ):

            self._last_signaled_open_time[
                signal_key
            ] = current_candle.open_time

            return None

        # --------------------------------------------------------------
        # PRICE
        # --------------------------------------------------------------

        try:

            price = float(
                current_candle.close
            )

        except (TypeError, ValueError):

            return None

        # 24h change up to the signal candle's close, from the candle
        # history itself (ticker lists of the backup exchanges are reused
        # for up to 30 min). Falls back to the ticker value when the
        # history doesn't reach exactly 24h back.
        change_24h = self._change_24h_from_history(
            history
        )

        if change_24h is None:

            try:

                change_24h = float(
                    ticker.get(
                        "priceChangePercent",
                        0.0,
                    )
                )

            except (TypeError, ValueError, AttributeError):

                change_24h = 0.0

        # --------------------------------------------------------------
        # CREATE SIGNAL
        # --------------------------------------------------------------

        signal = MarketSignal(
            symbol=symbol,
            price=price,
            change_5m=candle_price_change,
            change_24h=change_24h,
            inflow_usd=estimated_inflow,
            spike_multiplier=spike_multiplier,
            direction=direction,
            trigger=trigger,
            zscore=zscore,
            source=source,
            path=path,
        )

        self.state.mark_alerted(
            cooldown_key
        )

        self._last_signaled_open_time[
            signal_key
        ] = current_candle.open_time

        log.warning(
            "SIGNAL FIRED | "
            "source=%s symbol=%s path=%s "
            "direction=%s trigger=%s "
            "volume=%.2f baseline=%.2f "
            "spike=%.2fX inflow=%.2f "
            "price=%.2f%% zscore=%s",
            source,
            symbol,
            path,
            direction.value,
            trigger.value,
            current_volume,
            baseline_used,
            spike_multiplier,
            estimated_inflow,
            candle_price_change,
            (
                f"{zscore:.2f}"
                if zscore is not None
                else "N/A"
            ),
        )

        return signal

    # ------------------------------------------------------------------
    # STATUS
    # ------------------------------------------------------------------

    def build_status_message(
        self,
        data_source: str,
        symbols_scanned: int,
        inflow_count: int,
        outflow_count: int,
    ) -> str:

        source_label = {
            "binance": "Binance",
            "bybit": "Bybit",
            "kucoin": "KuCoin",
            "none": "هیچ‌کدام",
        }.get(
            data_source,
            data_source,
        )

        now_utc = datetime.now(
            timezone.utc
        ).strftime("%H:%M:%S")

        return (
            "📡 <b>وضعیت رصد</b>\n\n"
            f"⏰ <code>{now_utc}</code> UTC\n"
            f"🌐 منبع: <code>{esc(source_label)}</code>\n"
            f"🔍 نمادها: <code>{symbols_scanned}</code>\n"
            f"🟢 ورود: <code>{inflow_count}</code>  "
            f"🔴 خروج: <code>{outflow_count}</code>"
        )