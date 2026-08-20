"""
Independent exchange-aware market analyzer.

Signal logic
------------
1. Smart Money:
   Most recently CLOSED 5m candle volume compared with previous
   48 CLOSED 5m candles from the SAME exchange.

2. Pump / Dump:
   Price movement and volume anomaly evaluated against the long
   CLOSED-candle history, up to 864 candles.

3. Binance is preferred for the current analysis cycle.
4. KuCoin is fallback for the current analysis cycle.
5. Binance and KuCoin histories are NEVER mixed.

The currently active exchange only controls which exchange's data is
analyzed. It never controls or overwrites the other exchange's history.
"""

import logging
import statistics
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

from candle_store import (
    CandleStore,
    SMART_MONEY_BASELINE_CANDLES,
    PUMP_HISTORY_CANDLES,
)
from config import Settings
from formatting import esc
from market_data import MarketDataProvider
from signals import (
    MarketSignal,
    SignalDirection,
    TriggerType,
)
from state import BotState


log = logging.getLogger("smart_money_bot.market_analyzer")

# How many recent candles to fetch each cycle for the rolling update.
LIVE_UPDATE_LIMIT = 5


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
        )

        self.candle_store = candle_store

        # Track last closed open_time we already evaluated for signals,
        # so we only fire once per newly closed candle.
        self._last_signaled_open_time: Dict[str, int] = {}

    # =========================================================
    # MAIN CYCLE
    # =========================================================

    def run_cycle(
        self,
    ) -> Tuple[List[MarketSignal], str, int]:

        log.info("MARKET FETCH START")

        try:
            binance_tickers, kucoin_tickers = (
                self.provider.fetch_all_sources()
            )
        except Exception:
            log.exception("MARKET TICKER FETCH FAILED")
            return [], "none", 0

        # -----------------------------------------------------
        # Maintain BOTH histories independently.
        # -----------------------------------------------------

        self._maintain_history(
            source="binance",
            tickers=binance_tickers,
        )

        self._maintain_history(
            source="kucoin",
            tickers=kucoin_tickers,
        )

        # -----------------------------------------------------
        # Live rolling update: fetch a few recent candles and
        # push them into the store so history advances.
        # -----------------------------------------------------

        self._live_update_candles(
            source="binance",
            tickers=binance_tickers,
        )

        self._live_update_candles(
            source="kucoin",
            tickers=kucoin_tickers,
        )

        # -----------------------------------------------------
        # Select source for CURRENT ANALYSIS ONLY.
        # -----------------------------------------------------

        if binance_tickers:
            active_source = "binance"
            ticker_stats = binance_tickers

            log.info(
                "ACTIVE MARKET SOURCE | Binance PRIMARY"
            )

        elif kucoin_tickers:
            active_source = "kucoin"
            ticker_stats = kucoin_tickers

            log.warning(
                "ACTIVE MARKET SOURCE | KuCoin FALLBACK | "
                "Binance unavailable"
            )

        else:
            log.error(
                "ACTIVE MARKET SOURCE FAILED | "
                "Binance and KuCoin unavailable"
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
                    source=active_source,
                    symbol=symbol,
                    ticker=ticker,
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

    # =========================================================
    # HISTORY MAINTENANCE (bootstrap)
    # =========================================================

    def _maintain_history(
        self,
        source: str,
        tickers: Dict[str, dict],
    ) -> None:

        if source not in {"binance", "kucoin"}:
            raise ValueError(
                f"Unsupported market source: {source}"
            )

        if not tickers:
            log.info(
                "HISTORY MAINTENANCE SKIPPED | "
                "source=%s | reason=no_tickers",
                source,
            )
            return

        log.info(
            "HISTORY MAINTENANCE | source=%s symbols=%s",
            source,
            len(tickers),
        )

        for symbol in tickers:

            try:
                current_count = self.candle_store.count(
                    source,
                    symbol,
                )

                if current_count >= PUMP_HISTORY_CANDLES:
                    continue

                self.candle_store.load(
                    source,
                    symbol,
                )

                current_count = self.candle_store.count(
                    source,
                    symbol,
                )

                if current_count >= PUMP_HISTORY_CANDLES:
                    continue

                # Enough for smart-money baseline; live updates
                # will continue to grow the window.
                if current_count >= SMART_MONEY_BASELINE_CANDLES:
                    continue

                if source == "binance":
                    candles = self.provider.fetch_binance_candles(
                        symbol=symbol,
                        limit=PUMP_HISTORY_CANDLES,
                    )
                else:
                    candles = self.provider.fetch_kucoin_candles(
                        symbol=symbol,
                        limit=PUMP_HISTORY_CANDLES,
                    )

                if not candles:
                    log.warning(
                        "HISTORY BOOTSTRAP FAILED | "
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

                stored_count = self.candle_store.count(
                    source,
                    symbol,
                )

                log.info(
                    "HISTORY BOOTSTRAP OK | "
                    "source=%s symbol=%s candles=%s/%s",
                    source,
                    symbol,
                    stored_count,
                    PUMP_HISTORY_CANDLES,
                )

            except Exception:
                log.exception(
                    "HISTORY MAINTENANCE ERROR | "
                    "source=%s symbol=%s",
                    source,
                    symbol,
                )

    # =========================================================
    # LIVE CANDLE UPDATE (rolling window)
    # =========================================================

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
                # Ensure local history is loaded before updating.
                if self.candle_store.count(source, symbol) == 0:
                    self.candle_store.load(source, symbol)

                if source == "binance":
                    candles = self.provider.fetch_binance_candles(
                        symbol=symbol,
                        limit=LIVE_UPDATE_LIMIT,
                    )
                else:
                    candles = self.provider.fetch_kucoin_candles(
                        symbol=symbol,
                        limit=LIVE_UPDATE_LIMIT,
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
                    "LIVE UPDATE ERROR | source=%s symbol=%s",
                    source,
                    symbol,
                )

        log.info(
            "LIVE UPDATE DONE | source=%s symbols=%s newly_closed=%s",
            source,
            updated,
            closed_total,
        )

    # =========================================================
    # SYMBOL ANALYSIS
    # =========================================================

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

        minimum_required = (
            SMART_MONEY_BASELINE_CANDLES + 1
        )

        if len(history) < minimum_required:

            log.warning(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=INSUFFICIENT_VOLUME_HISTORY "
                "history=%s/%s",
                source,
                symbol,
                len(history),
                minimum_required,
            )

            return None

        current_candle = history[-1]

        # Only evaluate a candle once (when it first becomes the
        # newest closed candle).
        signal_key = f"{source}:{symbol}"
        last_ot = self._last_signaled_open_time.get(signal_key)

        if last_ot is not None and current_candle.open_time <= last_ot:
            return None

        baseline_candles = history[
            -SMART_MONEY_BASELINE_CANDLES - 1:-1
        ]

        if len(baseline_candles) != (
            SMART_MONEY_BASELINE_CANDLES
        ):
            log.warning(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=BASELINE_NOT_READY",
                source,
                symbol,
            )
            return None

        baseline_values = [
            float(c.quote_volume)
            for c in baseline_candles
            if c.quote_volume > 0
        ]

        if len(baseline_values) != (
            SMART_MONEY_BASELINE_CANDLES
        ):
            log.warning(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=INVALID_BASELINE_VALUES",
                source,
                symbol,
            )
            return None

        baseline = (
            sum(baseline_values)
            / len(baseline_values)
        )

        current_volume = float(
            current_candle.quote_volume
        )

        if baseline <= 0 or current_volume <= 0:
            log.warning(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=INVALID_VOLUME",
                source,
                symbol,
            )
            return None

        spike_multiplier = (
            current_volume / baseline
        )

        # Estimated additional volume (net inflow approximation).
        estimated_inflow = max(
            0.0,
            current_volume - baseline,
        )

        if current_candle.open <= 0:
            log.warning(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=INVALID_OPEN_PRICE",
                source,
                symbol,
            )
            return None

        candle_price_change = (
            (
                current_candle.close
                - current_candle.open
            )
            / current_candle.open
        ) * 100.0

        volume_threshold = (
            baseline
            * self.settings.volume_spike_ratio
        )

        is_volume_spike = (
            current_volume >= volume_threshold
        )

        long_history = history[
            -PUMP_HISTORY_CANDLES:
        ]

        current_close_to_close = None

        if len(long_history) >= 2:

            previous_candle = long_history[-2]

            if previous_candle.close > 0:
                current_close_to_close = (
                    (
                        current_candle.close
                        - previous_candle.close
                    )
                    / previous_candle.close
                ) * 100.0

        long_returns: List[float] = []

        for previous, current in zip(
            long_history,
            long_history[1:],
        ):

            if previous.close <= 0:
                continue

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
                        current_close_to_close - mean
                    ) / stdev

        static_pump = (
            is_volume_spike
            and self.settings.price_pump_min
            <= candle_price_change
            <= self.settings.price_pump_max
        )

        static_dump = (
            is_volume_spike
            and -self.settings.price_pump_max
            <= candle_price_change
            <= -self.settings.price_pump_min
        )

        statistical_pump = (
            zscore is not None
            and zscore
            >= self.settings.pump_zscore_threshold
            and is_volume_spike
        )

        statistical_dump = (
            zscore is not None
            and zscore
            <= -self.settings.pump_zscore_threshold
            and is_volume_spike
        )

        if not (
            static_pump
            or static_dump
            or statistical_pump
            or statistical_dump
        ):

            # Still mark as processed so we don't re-evaluate
            # the same closed candle every cycle.
            self._last_signaled_open_time[signal_key] = (
                current_candle.open_time
            )

            log.info(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=THRESHOLD_NOT_MET "
                "volume=%.2f baseline=%.2f spike=%.2fX "
                "required=%.2fX price=%.2f%% "
                "close_to_close=%s zscore=%s",
                source,
                symbol,
                current_volume,
                baseline,
                spike_multiplier,
                self.settings.volume_spike_ratio,
                candle_price_change,
                (
                    f"{current_close_to_close:.2f}%"
                    if current_close_to_close is not None
                    else "N/A"
                ),
                (
                    f"{zscore:.2f}"
                    if zscore is not None
                    else "N/A"
                ),
            )

            return None

        cooldown_key = (
            f"market:{source}:{symbol}"
        )

        if self.state.is_in_cooldown(
            cooldown_key,
            self.settings.alert_cooldown_sec,
        ):

            self._last_signaled_open_time[signal_key] = (
                current_candle.open_time
            )

            log.info(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=COOLDOWN",
                source,
                symbol,
            )

            return None

        if static_pump or statistical_pump:

            direction = SignalDirection.INFLOW

            if static_pump and statistical_pump:
                trigger = TriggerType.BOTH
            elif statistical_pump:
                trigger = TriggerType.STATISTICAL
            else:
                trigger = TriggerType.STATIC

        else:

            direction = SignalDirection.OUTFLOW

            if static_dump and statistical_dump:
                trigger = TriggerType.BOTH
            elif statistical_dump:
                trigger = TriggerType.STATISTICAL
            else:
                trigger = TriggerType.STATIC

        try:
            price = float(current_candle.close)
        except (TypeError, ValueError):
            log.warning(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=INVALID_CLOSE_PRICE",
                source,
                symbol,
            )
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
        )

        self.state.mark_alerted(
            cooldown_key
        )

        self._last_signaled_open_time[signal_key] = (
            current_candle.open_time
        )

        log.warning(
            "SIGNAL FIRED | source=%s symbol=%s "
            "direction=%s trigger=%s "
            "volume=%.2f baseline=%.2f spike=%.2fX "
            "inflow=%.2f price=%.2f%% zscore=%s",
            source,
            symbol,
            direction.value,
            trigger.value,
            current_volume,
            baseline,
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

    # =========================================================
    # STATUS
    # =========================================================

    def build_status_message(
        self,
        data_source: str,
        symbols_scanned: int,
        inflow_count: int,
        outflow_count: int,
    ) -> str:

        source_label = {
            "binance": "Binance",
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
            "🟢 <b>گزارش رصد زنده مارکت</b>\n\n"
            f"⏰ <b>زمان (UTC):</b> "
            f"<code>{now_utc}</code>\n"
            f"🌐 <b>منبع فعال:</b> "
            f"<code>{esc(source_label)}</code>\n"
            f"🔍 <b>ارزهای آنالیز شده:</b> "
            f"<code>{symbols_scanned}</code>\n"
            f"📥 <b>سیگنال ورود:</b> "
            f"<code>{inflow_count}</code>\n"
            f"📤 <b>سیگنال خروج:</b> "
            f"<code>{outflow_count}</code>\n"
            "📡 <b>وضعیت:</b> فعال"
        )
