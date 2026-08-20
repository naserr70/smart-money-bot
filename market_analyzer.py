"""
CEX market analyzer.

Signal architecture:

    Volume / Smart Money:
        current CLOSED 5m candle
        compared against previous 48 CLOSED 5m candles

    Pump / Dump:
        current CLOSED 5m candle price move
        compared against long-term 864-candle history

Important:
    Binance and KuCoin candle stores are completely isolated.

    No MIN_INFLOW_USD_5M filter is used.

    A volume signal requires at least:
        current candle volume >= 2.0 * average of previous 48 candles

The current/open candle is never used for final signal generation.
"""

import logging
import statistics
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

from config import Settings
from formatting import esc
from market_data import MarketDataProvider
from signals import (
    MarketSignal,
    SignalDirection,
    TriggerType,
)
from state import BotState
from candle_store import CandleStore, Candle


log = logging.getLogger(
    "smart_money_bot.market_analyzer"
)


class MarketAnalyzer:
    def __init__(
        self,
        settings: Settings,
        state: BotState,
        session: requests.Session,
    ):
        self.settings = settings
        self.state = state
        self.provider = MarketDataProvider(
            session,
            timeout=settings.http_timeout_sec,
        )

        self.candle_store = CandleStore(
            root_path=getattr(
                settings,
                "candle_store_path",
                "market_history",
            ),
            max_candles=864,
        )

        self._bootstrapped_sources = set()

    # ---------------------------------------------------------
    # main cycle
    # ---------------------------------------------------------

    def run_cycle(
        self,
    ) -> Tuple[List[MarketSignal], str, int]:

        cycle_started = time.monotonic()

        ticker_stats, data_source = (
            self.provider.fetch()
        )

        if not ticker_stats:
            log.error(
                "MARKET CYCLE ABORTED | source=none | no ticker data"
            )

            return [], data_source, 0

        log.info(
            "========== MARKET CYCLE START | source=%s | symbols=%d ==========",
            data_source,
            len(ticker_stats),
        )

        counters = {
            "scanned": 0,
            "history_loaded": 0,
            "history_insufficient": 0,
            "no_previous_candle": 0,
            "invalid_price": 0,
            "invalid_volume": 0,
            "volume_below_2x": 0,
            "volume_passed": 0,
            "price_static_passed": 0,
            "price_static_failed": 0,
            "zscore_passed": 0,
            "zscore_failed": 0,
            "cooldown": 0,
            "signals": 0,
            "inflow": 0,
            "outflow": 0,
        }

        signals: List[MarketSignal] = []

        for full_symbol, item in ticker_stats.items():
            counters["scanned"] += 1

            symbol = full_symbol.replace(
                "USDT",
                "",
            )

            try:
                signal = self._analyze_symbol(
                    source=data_source,
                    symbol=symbol,
                    full_symbol=full_symbol,
                    item=item,
                    counters=counters,
                )

                if signal is not None:
                    signals.append(signal)

                    counters["signals"] += 1

                    if (
                        signal.direction
                        == SignalDirection.INFLOW
                    ):
                        counters["inflow"] += 1
                    else:
                        counters["outflow"] += 1

            except Exception as e:
                log.exception(
                    "ANALYSIS ERROR | source=%s | symbol=%s | error=%s",
                    data_source,
                    symbol,
                    e,
                )

        # Save changed source-specific candle files.
        try:
            self.candle_store.save_dirty()
        except Exception as e:
            log.exception(
                "CANDLE STORE SAVE ERROR | source=%s | error=%s",
                data_source,
                e,
            )

        elapsed = time.monotonic() - cycle_started

        log.info(
            "MARKET SUMMARY | "
            "source=%s | "
            "scanned=%d | "
            "history_loaded=%d | "
            "history_insufficient=%d | "
            "no_previous_candle=%d | "
            "invalid_price=%d | "
            "invalid_volume=%d | "
            "volume_below_2x=%d | "
            "volume_passed=%d | "
            "price_passed=%d | "
            "price_failed=%d | "
            "zscore_passed=%d | "
            "zscore_failed=%d | "
            "cooldown=%d | "
            "signals=%d | "
            "inflow=%d | "
            "outflow=%d | "
            "elapsed=%.2fs",
            data_source,
            counters["scanned"],
            counters["history_loaded"],
            counters["history_insufficient"],
            counters["no_previous_candle"],
            counters["invalid_price"],
            counters["invalid_volume"],
            counters["volume_below_2x"],
            counters["volume_passed"],
            counters["price_static_passed"],
            counters["price_static_failed"],
            counters["zscore_passed"],
            counters["zscore_failed"],
            counters["cooldown"],
            counters["signals"],
            counters["inflow"],
            counters["outflow"],
            elapsed,
        )

        log.info(
            "========== MARKET CYCLE END | source=%s | signals=%d ==========",
            data_source,
            len(signals),
        )

        return (
            signals,
            data_source,
            len(ticker_stats),
        )

    # ---------------------------------------------------------
    # symbol analysis
    # ---------------------------------------------------------

    def _analyze_symbol(
        self,
        source: str,
        symbol: str,
        full_symbol: str,
        item: dict,
        counters: dict,
    ) -> Optional[MarketSignal]:

        try:
            price_usd = float(
                item["lastPrice"]
            )

            change_24h = float(
                item["priceChangePercent"]
            )

        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            counters["invalid_price"] += 1

            log.warning(
                "SIGNAL REJECTED | source=%s | symbol=%s | reason=invalid_ticker",
                source,
                symbol,
            )

            return None

        if price_usd <= 0:
            counters["invalid_price"] += 1

            log.warning(
                "SIGNAL REJECTED | source=%s | symbol=%s | reason=price<=0",
                source,
                symbol,
            )

            return None

        # -----------------------------------------------------
        # Make sure source-specific history exists.
        # -----------------------------------------------------

        self._ensure_history(
            source,
            symbol,
            full_symbol,
        )

        candles = self.candle_store.get_closed(
            source,
            symbol,
        )

        if not candles:
            counters["no_previous_candle"] += 1

            log.debug(
                "SIGNAL REJECTED | source=%s | symbol=%s | reason=no_closed_candles",
                source,
                symbol,
            )

            return None

        counters["history_loaded"] += 1

        # -----------------------------------------------------
        # IMPORTANT:
        # Only CLOSED candle is allowed.
        # -----------------------------------------------------

        current_candle = candles[-1]

        if current_candle.close <= 0:
            counters["invalid_price"] += 1

            log.warning(
                "SIGNAL REJECTED | source=%s | symbol=%s | reason=closed_candle_price<=0",
                source,
                symbol,
            )

            return None

        # -----------------------------------------------------
        # Need previous 48 CLOSED candles.
        # The current candle is NOT part of baseline.
        # -----------------------------------------------------

        if len(candles) < 49:
            counters["history_insufficient"] += 1

            log.debug(
                "SIGNAL WAITING | source=%s | symbol=%s | reason=history_insufficient | have=%d | need=49",
                source,
                symbol,
                len(candles),
            )

            return None

        baseline_candles = candles[-49:-1]

        volume_values = [
            c.quote_volume
            for c in baseline_candles
            if c.quote_volume > 0
        ]

        if len(volume_values) < 24:
            counters["invalid_volume"] += 1

            log.warning(
                "SIGNAL REJECTED | source=%s | symbol=%s | reason=insufficient_valid_volume | valid=%d/48",
                source,
                symbol,
                len(volume_values),
            )

            return None

        baseline_volume = (
            sum(volume_values)
            / len(volume_values)
        )

        current_volume = (
            current_candle.quote_volume
        )

        if current_volume <= 0:
            counters["invalid_volume"] += 1

            log.debug(
                "SIGNAL REJECTED | source=%s | symbol=%s | reason=current_volume<=0",
                source,
                symbol,
            )

            return None

        # -----------------------------------------------------
        # Volume spike
        # -----------------------------------------------------

        spike_multiplier = (
            current_volume
            / baseline_volume
            if baseline_volume > 0
            else 0.0
        )

        # NO MIN_INFLOW_USD_5M FILTER.
        #
        # Minimum requirement is purely relative:
        # current closed candle >= 2x previous 48 average.
        volume_threshold = (
            getattr(
                self.settings,
                "volume_spike_ratio",
                2.0,
            )
        )

        is_volume_spike = (
            spike_multiplier
            >= volume_threshold
        )

        if not is_volume_spike:
            counters["volume_below_2x"] += 1

            log.debug(
                "SIGNAL REJECTED | source=%s | symbol=%s | reason=volume_below_threshold | current=%.2f | baseline=%.2f | spike=%.2fx | required=%.2fx",
                source,
                symbol,
                current_volume,
                baseline_volume,
                spike_multiplier,
                volume_threshold,
            )

            return None

        counters["volume_passed"] += 1

        # -----------------------------------------------------
        # Price movement
        # -----------------------------------------------------

        previous_candle = candles[-2]

        if previous_candle.close <= 0:
            counters["invalid_price"] += 1

            log.warning(
                "SIGNAL REJECTED | source=%s | symbol=%s | reason=previous_close<=0",
                source,
                symbol,
            )

            return None

        price_change = (
            (
                current_candle.close
                - previous_candle.close
            )
            / previous_candle.close
        ) * 100.0

        # -----------------------------------------------------
        # Static pump/dump
        # -----------------------------------------------------

        static_pump = (
            self.settings.price_pump_min
            <= price_change
            <= self.settings.price_pump_max
        )

        static_dump = (
            -self.settings.price_pump_max
            <= price_change
            <= -self.settings.price_pump_min
        )

        if static_pump or static_dump:
            counters["price_static_passed"] += 1
        else:
            counters["price_static_failed"] += 1

        # -----------------------------------------------------
        # Long-term statistical pump/dump
        #
        # Up to 864 CLOSED candles.
        # Current candle is included as the observation,
        # but NOT in its own historical distribution.
        # -----------------------------------------------------

        zscore = None

        if getattr(
            self.settings,
            "pump_zscore_enabled",
            True,
        ):
            long_history = candles[:-1]

            if len(long_history) >= 20:
                returns = []

                start_index = max(
                    1,
                    len(long_history) - 863,
                )

                for i in range(
                    start_index,
                    len(long_history),
                ):
                    prev_close = (
                        long_history[i - 1].close
                    )
                    close = (
                        long_history[i].close
                    )

                    if (
                        prev_close > 0
                        and close > 0
                    ):
                        returns.append(
                            (
                                (
                                    close
                                    - prev_close
                                )
                                / prev_close
                            )
                            * 100.0
                        )

                if len(returns) >= 20:
                    mean = statistics.mean(
                        returns
                    )

                    stdev = statistics.pstdev(
                        returns
                    )

                    if stdev > 0:
                        zscore = (
                            price_change - mean
                        ) / stdev

        statistical_pump = (
            zscore is not None
            and zscore
            >= self.settings.pump_zscore_threshold
        )

        statistical_dump = (
            zscore is not None
            and zscore
            <= -self.settings.pump_zscore_threshold
        )

        if (
            statistical_pump
            or statistical_dump
        ):
            counters["zscore_passed"] += 1
        else:
            counters["zscore_failed"] += 1

        # -----------------------------------------------------
        # Final direction
        #
        # Volume is mandatory.
        # Then either static price condition OR statistical
        # anomaly can produce the signal.
        # -----------------------------------------------------

        pump = (
            static_pump
            or statistical_pump
        )

        dump = (
            static_dump
            or statistical_dump
        )

        if not pump and not dump:
            log.debug(
                "SIGNAL REJECTED | source=%s | symbol=%s | reason=price_conditions_failed | "
                "volume=%.2fx | price=%.2f%% | zscore=%s",
                source,
                symbol,
                spike_multiplier,
                price_change,
                (
                    f"{zscore:.2f}"
                    if zscore is not None
                    else "N/A"
                ),
            )

            return None

        # -----------------------------------------------------
        # Cooldown
        # -----------------------------------------------------

        cooldown_key = (
            f"market:{source}:{symbol}"
        )

        if self.state.is_in_cooldown(
            cooldown_key,
            self.settings.alert_cooldown_sec,
        ):
            counters["cooldown"] += 1

            log.debug(
                "SIGNAL REJECTED | source=%s | symbol=%s | reason=cooldown",
                source,
                symbol,
            )

            return None

        # -----------------------------------------------------
        # Direction
        # -----------------------------------------------------

        if pump and not dump:
            direction = (
                SignalDirection.INFLOW
            )

            static_condition = static_pump
            statistical_condition = (
                statistical_pump
            )

        elif dump and not pump:
            direction = (
                SignalDirection.OUTFLOW
            )

            static_condition = static_dump
            statistical_condition = (
                statistical_dump
            )

        else:
            # Extremely unlikely if price_change is sane.
            # Resolve based on sign.
            if price_change >= 0:
                direction = (
                    SignalDirection.INFLOW
                )
                static_condition = static_pump
                statistical_condition = (
                    statistical_pump
                )
            else:
                direction = (
                    SignalDirection.OUTFLOW
                )
                static_condition = static_dump
                statistical_condition = (
                    statistical_dump
                )

        if (
            static_condition
            and statistical_condition
        ):
            trigger = TriggerType.BOTH
        elif statistical_condition:
            trigger = TriggerType.STATISTICAL
        else:
            trigger = TriggerType.STATIC

        # -----------------------------------------------------
        # Signal
        # -----------------------------------------------------

        signal = MarketSignal(
            symbol=symbol,
            price=current_candle.close,
            change_5m=price_change,
            change_24h=change_24h,
            inflow_usd=(
                current_volume
                if direction
                == SignalDirection.INFLOW
                else -current_volume
            ),
            spike_multiplier=spike_multiplier,
            direction=direction,
            trigger=trigger,
            zscore=zscore,
        )

        self.state.mark_alerted(
            cooldown_key
        )

        log.warning(
            "SIGNAL FIRED | source=%s | symbol=%s | direction=%s | "
            "volume=%.2fx | current_volume=$%.0f | baseline=$%.0f | "
            "price=%.2f%% | zscore=%s | trigger=%s",
            source,
            symbol,
            direction.value,
            spike_multiplier,
            current_volume,
            baseline_volume,
            price_change,
            (
                f"{zscore:.2f}"
                if zscore is not None
                else "N/A"
            ),
            trigger.value,
        )

        return signal

    # ---------------------------------------------------------
    # history bootstrap
    # ---------------------------------------------------------

    def _ensure_history(
        self,
        source: str,
        symbol: str,
        full_symbol: str,
    ) -> None:

        key = (
            f"{source}:{symbol}"
        )

        if key in self._bootstrapped_sources:
            return

        self._bootstrapped_sources.add(
            key
        )

        loaded = self.candle_store.load(
            source,
            symbol,
        )

        existing = self.candle_store.count(
            source,
            symbol,
        )

        if loaded and existing >= 864:
            log.info(
                "HISTORY READY | source=%s | symbol=%s | candles=%d/864 | source-isolated=yes",
                source,
                symbol,
                existing,
            )
            return

        # -----------------------------------------------------
        # Only Binance currently supports direct 5m bootstrap
        # in this provider.
        #
        # NEVER use Binance candles to populate KuCoin.
        # -----------------------------------------------------

        if source != "binance":
            log.warning(
                "HISTORY INSUFFICIENT | source=%s | symbol=%s | candles=%d/864 | "
                "Binance history NOT copied to KuCoin",
                source,
                symbol,
                existing,
            )
            return

        log.info(
            "HISTORY BOOTSTRAP | source=binance | symbol=%s | existing=%d | requesting closed 5m candles",
            symbol,
            existing,
        )

        candles = (
            self.provider.fetch_recent_5m_candles(
                full_symbol,
                limit=864,
            )
        )

        if not candles:
            log.error(
                "HISTORY BOOTSTRAP FAILED | source=binance | symbol=%s",
                symbol,
            )
            return

        self.candle_store.seed(
            "binance",
            symbol,
            candles,
        )

        log.info(
            "HISTORY BOOTSTRAP COMPLETE | source=binance | symbol=%s | candles=%d/864",
            symbol,
            len(candles),
        )

    # ---------------------------------------------------------
    # status
    # ---------------------------------------------------------

    def build_status_message(
        self,
        data_source: str,
        symbols_scanned: int,
        inflow_count: int,
        outflow_count: int,
    ) -> str:

        return (
            f"🟢 <b>گزارش رصد زنده مارکت</b>\n\n"
            f"⏰ <b>زمان (UTC):</b> "
            f"<code>{datetime.now(timezone.utc).strftime('%H:%M:%S')}</code>\n"
            f"🌐 <b>منبع داده:</b> "
            f"<code>{esc(data_source)}</code>\n"
            f"🔍 <b>ارزهای آنالیز شده:</b> "
            f"<code>{symbols_scanned}</code>\n"
            f"📥 <b>سیگنال ورود:</b> "
            f"<code>{inflow_count}</code>\n"
            f"📤 <b>سیگنال خروج:</b> "
            f"<code>{outflow_count}</code>\n"
            f"📡 <b>وضعیت سیستم:</b> فعال"
        )