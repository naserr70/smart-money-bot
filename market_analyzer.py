"""
Independent exchange-aware market analyzer.

Signal logic
------------
1. Smart money (inflow / outflow):
   Latest CLOSED 5m quote_volume vs mean of the previous 48 CLOSED
   5m candles from the SAME exchange.
2. Pump / Dump: rolling volume anomaly and price/z-score checks.
3. Active analysis priority: Binance → Bybit → KuCoin.
4. Histories are NEVER mixed across exchanges.

Startup bootstrap
-----------------
History restoration is deliberately performed BEFORE ticker discovery.

Important:
- Local history is never destroyed by a shorter API response.
- GitHub history is restored before API bootstrap.
- API bootstrap is only attempted for incomplete source/symbol pairs.
- Partial exchange responses are MERGED, never blindly used to replace
  an existing longer history.
- Runtime maintenance NEVER repeatedly downloads 864 candles.
- Live updates use only a small recent window.
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

# Only recent candles are requested during normal operation.
LIVE_UPDATE_LIMIT = 5


def _candles_from_payload(data: dict) -> List[Candle]:
    """
    Convert GitHub candle-store payload into Candle objects.

    Only the closed-history section is restored here.
    The 'current' candle is intentionally ignored because the current
    candle belongs to the live exchange stream and must not become part
    of the closed-history baseline.
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

    # Defensive deduplication.
    unique: Dict[int, Candle] = {}

    for candle in parsed:
        unique[candle.open_time] = candle

    return sorted(unique.values(), key=lambda c: c.open_time)


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

        self._last_signaled_open_time: Dict[str, int] = {}

        self._startup_bootstrap_done = False

    # ------------------------------------------------------------------
    # HISTORY HELPERS
    # ------------------------------------------------------------------

    @staticmethod
    def _only_closed_candles(candles: List[Candle]) -> List[Candle]:
        """
        Remove the currently-open candle.

        CandleStore calculations must always operate on CLOSED candles.
        """
        if not candles:
            return []

        now_ms = int(time.time() * 1000)

        result = [
            candle
            for candle in candles
            if candle.close_time < now_ms
        ]

        result.sort(key=lambda c: c.open_time)

        unique: Dict[int, Candle] = {}

        for candle in result:
            unique[candle.open_time] = candle

        return sorted(unique.values(), key=lambda c: c.open_time)

    def _merge_history(
        self,
        source: str,
        symbol: str,
        candles: List[Candle],
    ) -> int:
        """
        Merge incoming candles with the existing local history.

        CRITICAL:
        We never allow a short API response (e.g. KuCoin returning 100)
        to replace a longer existing history (e.g. 864).

        The merged result is deduplicated by open_time and capped by
        CandleStore.max_candles.
        """
        if not candles:
            return self.candle_store.count(source, symbol)

        incoming = self._only_closed_candles(candles)

        if not incoming:
            return self.candle_store.count(source, symbol)

        existing = self.candle_store.get_closed(source, symbol)

        merged: Dict[int, Candle] = {}

        for candle in existing:
            merged[candle.open_time] = candle

        for candle in incoming:
            merged[candle.open_time] = candle

        merged_list = sorted(
            merged.values(),
            key=lambda c: c.open_time,
        )

        merged_list = merged_list[-self.candle_store.max_candles:]

        # seed() is safe here because we have already merged the existing
        # history with the incoming API response.
        self.candle_store.seed(
            source,
            symbol,
            merged_list,
        )

        return self.candle_store.count(source, symbol)

    # ------------------------------------------------------------------
    # STARTUP BOOTSTRAP
    # ------------------------------------------------------------------

    def bootstrap_histories(
        self,
        github_backup=None,
        symbols: Optional[List[str]] = None,
        target_count: int = PUMP_HISTORY_CANDLES,
    ) -> dict:
        """
        Restore local/GitHub history first.

        API bootstrap is attempted only for source/symbol histories that
        remain incomplete.

        IMPORTANT:
        A short exchange response is merged into existing history.
        It can NEVER replace 864 candles with 100 candles.
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

        incomplete: Dict[str, List[str]] = {
            source: []
            for source in VALID_SOURCES
        }

        # ==============================================================
        # PHASE 1
        # LOCAL + GITHUB
        # ==============================================================

        for source in VALID_SOURCES:

            source_stats = {
                "symbols": len(symbol_list),
                "seeded": 0,
                "restored": 0,
                "incomplete": 0,
            }

            for symbol in symbol_list:

                try:

                    # --------------------------------------------------
                    # LOCAL
                    # --------------------------------------------------

                    self.candle_store.load(
                        source,
                        symbol,
                    )

                    count = self.candle_store.count(
                        source,
                        symbol,
                    )

                    if count >= target_count:

                        stats["already_full"] += 1
                        stats["local_ok"] += 1

                        continue

                    if count > 0:
                        stats["local_ok"] += 1

                    # --------------------------------------------------
                    # GITHUB
                    # --------------------------------------------------

                    if github_configured:

                        try:

                            payload = github_backup.download(
                                source,
                                symbol,
                            )

                        except Exception:

                            log.exception(
                                "GITHUB RESTORE ERROR | source=%s symbol=%s",
                                source,
                                symbol,
                            )

                            payload = None

                        if payload:

                            restored = _candles_from_payload(
                                payload
                            )

                            if restored:

                                before = self.candle_store.count(
                                    source,
                                    symbol,
                                )

                                after = self._merge_history(
                                    source,
                                    symbol,
                                    restored,
                                )

                                # Count GitHub restoration only when it
                                # actually improved/restored the history.
                                if after > before:

                                    stats["github_restored"] += 1
                                    source_stats["restored"] += 1

                                    log.info(
                                        "GITHUB RESTORE OK | source=%s symbol=%s candles=%s/%s",
                                        source,
                                        symbol,
                                        after,
                                        target_count,
                                    )

                                count = after

                    # --------------------------------------------------
                    # STILL INCOMPLETE
                    # --------------------------------------------------

                    if count >= target_count:
                        continue

                    incomplete[source].append(symbol)

                    source_stats["incomplete"] += 1

                except Exception:

                    stats["failed"] += 1

                    log.exception(
                        "STARTUP RESTORE ERROR | source=%s symbol=%s",
                        source,
                        symbol,
                    )

            stats["sources"][source] = source_stats

        # ==============================================================
        # PHASE 2
        # EXCHANGE API
        # ==============================================================

        missing_total = sum(
            len(items)
            for items in incomplete.values()
        )

        if missing_total:

            log.info(
                "STARTUP API HISTORY REQUIRED | missing=%s",
                missing_total,
            )

            try:

                (
                    binance_t,
                    bybit_t,
                    kucoin_t,
                ) = self.provider.fetch_all_sources()

            except Exception:

                log.exception(
                    "STARTUP MISSING-HISTORY TICKER FETCH FAILED"
                )

                binance_t = {}
                bybit_t = {}
                kucoin_t = {}

            source_tickers = {
                "binance": binance_t,
                "bybit": bybit_t,
                "kucoin": kucoin_t,
            }

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
                        "STARTUP API HISTORY SOURCE UNAVAILABLE | source=%s missing=%s",
                        source,
                        len(missing_symbols),
                    )

                    continue

                for symbol in missing_symbols:

                    if symbol not in tickers:

                        log.warning(
                            "STARTUP API HISTORY SYMBOL UNAVAILABLE | source=%s symbol=%s",
                            source,
                            symbol,
                        )

                        continue

                    try:

                        before = self.candle_store.count(
                            source,
                            symbol,
                        )

                        candles = self.provider.fetch_candles(
                            source=source,
                            symbol=symbol,
                            limit=target_count,
                        )

                        if not candles:

                            stats["failed"] += 1

                            log.warning(
                                "STARTUP API SEED FAILED | source=%s symbol=%s",
                                source,
                                symbol,
                            )

                            continue

                        # --------------------------------------------------
                        # IMPORTANT:
                        #
                        # NEVER:
                        #
                        # candle_store.seed(source, symbol, candles)
                        #
                        # directly.
                        #
                        # KuCoin may return only ~100 candles even when
                        # limit=864 is requested.
                        #
                        # We MERGE instead.
                        # --------------------------------------------------

                        stored = self._merge_history(
                            source,
                            symbol,
                            candles,
                        )

                        added = max(
                            0,
                            stored - before,
                        )

                        if stored >= target_count:

                            stats["api_seeded"] += 1

                            stats["sources"][source][
                                "seeded"
                            ] += 1

                            status = "OK"

                        else:

                            # Partial API response is NOT a hard failure.
                            # It means the exchange returned fewer candles
                            # than requested and history remains incomplete.
                            status = "PARTIAL"

                        log.info(
                            "STARTUP API SEED %s | source=%s symbol=%s "
                            "received=%s added=%s stored=%s/%s",
                            status,
                            source,
                            symbol,
                            len(candles),
                            added,
                            stored,
                            target_count,
                        )

                        # Gentle API pacing.
                        time.sleep(0.05)

                    except Exception:

                        stats["failed"] += 1

                        log.exception(
                            "STARTUP API SEED ERROR | source=%s symbol=%s",
                            source,
                            symbol,
                        )

        else:

            log.info(
                "STARTUP API HISTORY FETCH SKIPPED | "
                "all source histories already complete"
            )

        # ==============================================================
        # SAVE
        # ==============================================================

        self.candle_store.save_dirty()

        elapsed = time.time() - started

        self._startup_bootstrap_done = True

        log.info(
            "STARTUP BOOTSTRAP COMPLETE | "
            "local_ok=%s github_restored=%s api_seeded=%s "
            "already_full=%s failed=%s elapsed=%.1fs",
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

        log.info("MARKET FETCH START")

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
        # IMPORTANT:
        #
        # Runtime MUST NOT call a new 864-candle bootstrap repeatedly.
        #
        # Startup bootstrap owns historical acquisition.
        # Runtime only performs small live maintenance.
        # --------------------------------------------------------------

        for source, tickers in sources_tickers.items():

            self._maintain_history(
                source,
                tickers,
            )

        for source, tickers in sources_tickers.items():

            self._live_update_candles(
                source,
                tickers,
            )

        # --------------------------------------------------------------
        # ACTIVE SOURCE
        # --------------------------------------------------------------

        if binance_tickers:

            active_source = "binance"
            ticker_stats = binance_tickers

            log.info(
                "ACTIVE MARKET SOURCE | Binance PRIMARY"
            )

        elif bybit_tickers:

            active_source = "bybit"
            ticker_stats = bybit_tickers

            log.warning(
                "ACTIVE MARKET SOURCE | "
                "Bybit FALLBACK | Binance unavailable"
            )

        elif kucoin_tickers:

            active_source = "kucoin"
            ticker_stats = kucoin_tickers

            log.warning(
                "ACTIVE MARKET SOURCE | "
                "KuCoin FALLBACK | Binance and Bybit unavailable"
            )

        else:

            log.error(
                "ACTIVE MARKET SOURCE FAILED | "
                "Binance, Bybit and KuCoin unavailable"
            )

            return [], "none", 0

        log.info(
            "ANALYSIS START | source=%s symbols=%s",
            active_source,
            len(ticker_stats),
        )

        signals: List[MarketSignal] = []

        for symbol, ticker in ticker_stats.items():

            try:

                signal = self._analyze_symbol(
                    active_source,
                    symbol,
                    ticker,
                )

                if signal is not None:
                    signals.append(signal)

            except Exception:

                log.exception(
                    "ANALYSIS ERROR | source=%s symbol=%s",
                    active_source,
                    symbol,
                )

        log.info(
            "ANALYSIS COMPLETE | source=%s signals=%s symbols=%s",
            active_source,
            len(signals),
            len(ticker_stats),
        )

        return (
            signals,
            active_source,
            len(ticker_stats),
        )

    # ------------------------------------------------------------------
    # RUNTIME HISTORY MAINTENANCE
    # ------------------------------------------------------------------

    def _maintain_history(
        self,
        source: str,
        tickers: Dict[str, dict],
    ) -> None:

        if source not in VALID_SOURCES:

            raise ValueError(
                f"Unsupported market source: {source}"
            )

        if not tickers:
            return

        """
        DO NOT bootstrap 864 candles here.

        Historical bootstrap is handled by bootstrap_histories().

        This method only makes sure a symbol has a local store and,
        if it has absolutely no history, attempts one small recovery
        request.

        This prevents the old loop:

            count < 864
                -> fetch 864
                -> KuCoin returns 100
                -> seed 100
                -> next cycle fetch 864 again
                -> seed 100 again

        which destroys useful history and repeatedly hits the API.
        """

        for symbol in tickers:

            try:

                self.candle_store.load(
                    source,
                    symbol,
                )

                current_count = self.candle_store.count(
                    source,
                    symbol,
                )

                # Already has history.
                #
                # DO NOT fetch 864 again.
                if current_count > 0:
                    continue

                # Only completely empty symbols get a small recovery
                # request. Normal live updates will take over afterwards.
                candles = self.provider.fetch_candles(
                    source,
                    symbol,
                    LIVE_UPDATE_LIMIT,
                )

                if not candles:

                    log.warning(
                        "HISTORY RECOVERY EMPTY | "
                        "source=%s symbol=%s",
                        source,
                        symbol,
                    )

                    continue

                stored = self._merge_history(
                    source,
                    symbol,
                    candles,
                )

                log.info(
                    "HISTORY RECOVERY OK | "
                    "source=%s symbol=%s candles=%s/%s",
                    source,
                    symbol,
                    stored,
                    PUMP_HISTORY_CANDLES,
                )

            except Exception:

                log.exception(
                    "HISTORY MAINTENANCE ERROR | "
                    "source=%s symbol=%s",
                    source,
                    symbol,
                )

    # ------------------------------------------------------------------
    # LIVE CANDLE UPDATE
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

        for symbol in tickers:

            try:

                if self.candle_store.count(
                    source,
                    symbol,
                ) == 0:

                    self.candle_store.load(
                        source,
                        symbol,
                    )

                candles = self.provider.fetch_candles(
                    source,
                    symbol,
                    LIVE_UPDATE_LIMIT,
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
            "LIVE UPDATE DONE | source=%s symbols=%s newly_closed=%s",
            source,
            updated,
            closed_total,
        )

    # ------------------------------------------------------------------
    # SIGNAL BASELINE
    # ------------------------------------------------------------------

    @staticmethod
    def _baseline_mean(
        candles: List,
        count: int,
    ) -> Optional[float]:

        if count <= 0:
            return None

        # Need current closed candle + N previous candles.
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

    # ------------------------------------------------------------------
    # ANALYSIS
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

        minimum_smart = (
            SMART_MONEY_BASELINE_CANDLES + 1
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

        signal_key = f"{source}:{symbol}"

        last_ot = self._last_signaled_open_time.get(
            signal_key
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

        # ==============================================================
        # SMART MONEY — PREVIOUS 48 CLOSED CANDLES
        # ==============================================================

        baseline_48 = self._baseline_mean(
            history,
            SMART_MONEY_BASELINE_CANDLES,
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

        # ==============================================================
        # PUMP / DUMP VOLUME BASELINE
        # ==============================================================

        pump_baseline_count = min(
            PUMP_HISTORY_CANDLES,
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

        # ==============================================================
        # PRICE / Z-SCORE
        # ==============================================================

        long_history = history[
            -PUMP_HISTORY_CANDLES:
        ]

        current_close_to_close = None

        if (
            len(long_history) >= 2
            and long_history[-2].close > 0
        ):

            current_close_to_close = (
                (
                    long_history[-1].close
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

        # ==============================================================
        # PUMP / DUMP
        # ==============================================================

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

        # ==============================================================
        # SIGNAL PRIORITY
        # ==============================================================

        if is_pump or is_dump:

            if is_pump:

                direction = (
                    SignalDirection.INFLOW
                )

                trigger = (
                    TriggerType.BOTH
                    if static_pump
                    and statistical_pump
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
                    if static_dump
                    and statistical_dump
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

        # ==============================================================
        # COOLDOWN
        # ==============================================================

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

        try:

            price = float(
                current_candle.close
            )

        except (TypeError, ValueError):

            return None

        try:

            change_24h = float(
                ticker.get(
                    "priceChangePercent",
                    0.0,
                )
            )

        except (TypeError, ValueError):

            change_24h = 0.0

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
            "SIGNAL FIRED | source=%s symbol=%s "
            "path=%s direction=%s trigger=%s "
            "volume=%.2f baseline=%.2f spike=%.2fX "
            "inflow=%.2f price=%.2f%% zscore=%s",
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