"""
Source-independent market analyzer.

Signal source selection:

    Binance available -> Binance
    Binance unavailable -> KuCoin

History:

    Binance -> Binance history only
    KuCoin  -> KuCoin history only

No historical data is copied between exchanges.

Volume:

    Current CLOSED 5m candle
        /
    raw average of previous 48 closed 5m candles

Minimum volume multiplier:

    2.0x

Pump/Dump:

    price-return anomaly against previous 864 candles.
"""

import logging
import statistics
from typing import Dict, List, Tuple

import requests

from candle_store import (
    CandleStore,
    SMART_MONEY_CANDLES,
    PUMP_DUMP_CANDLES,
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

        self.candle_store = CandleStore(
            root_path=settings.candle_history_path,
            max_candles=settings.history_window,
        )

        self.provider = MarketDataProvider(
            session,
            timeout=settings.http_timeout_sec,
            candle_store=self.candle_store,
            history_limit=settings.history_window,
        )

    # =========================================================
    # MAIN CYCLE
    # =========================================================

    def run_cycle(
        self,
    ) -> Tuple[
        List[MarketSignal],
        str,
        int,
    ]:

        ticker_stats, active_source = (
            self.provider.fetch()
        )

        if not ticker_stats:

            log.error(
                "NO MARKET DATA | no active source"
            )

            return [], active_source, 0

        log.info(
            "ANALYSIS START | source=%s symbols=%d",
            active_source,
            len(ticker_stats),
        )

        signals = []

        for symbol, ticker in ticker_stats.items():

            try:

                signal = self._analyze_symbol(
                    active_source,
                    symbol,
                    ticker,
                )

                if signal is not None:
                    signals.append(signal)

            except Exception as e:

                log.exception(
                    "ANALYSIS ERROR | "
                    "source=%s symbol=%s error=%s",
                    active_source,
                    symbol,
                    e,
                )

        # Save both sources' changed histories.
        self.candle_store.save_dirty()

        inflow = sum(
            1
            for s in signals
            if s.direction == SignalDirection.INFLOW
        )

        outflow = sum(
            1
            for s in signals
            if s.direction == SignalDirection.OUTFLOW
        )

        log.info(
            "ANALYSIS COMPLETE | source=%s "
            "signals=%d inflow=%d outflow=%d",
            active_source,
            len(signals),
            inflow,
            outflow,
        )

        return (
            signals,
            active_source,
            len(ticker_stats),
        )

    # =========================================================
    # SYMBOL
    # =========================================================

    def _analyze_symbol(
        self,
        source: str,
        symbol: str,
        ticker: dict,
    ):

        history_count = self.candle_store.count(
            source,
            symbol,
        )

        # -----------------------------------------------------
        # Need 48 previous CLOSED candles.
        # -----------------------------------------------------

        if history_count < SMART_MONEY_CANDLES:

            log.warning(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=INSUFFICIENT_VOLUME_HISTORY "
                "history=%d/%d",
                source,
                symbol,
                history_count,
                SMART_MONEY_CANDLES,
            )

            return None

        candles = self.candle_store.get_recent(
            source,
            symbol,
            SMART_MONEY_CANDLES + 1,
        )

        if len(candles) < SMART_MONEY_CANDLES:

            log.warning(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=NOT_ENOUGH_CANDLES",
                source,
                symbol,
            )

            return None

        # -----------------------------------------------------
        # Latest CLOSED candle.
        # -----------------------------------------------------

        current = candles[-1]

        previous_48 = candles[
            -SMART_MONEY_CANDLES - 1:-1
        ]

        if len(previous_48) != SMART_MONEY_CANDLES:

            log.warning(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=BAD_BASELINE_SIZE size=%d",
                source,
                symbol,
                len(previous_48),
            )

            return None

        current_price = current.close

        if current_price <= 0:

            log.warning(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=INVALID_PRICE price=%s",
                source,
                symbol,
                current_price,
            )

            return None

        # -----------------------------------------------------
        # 48-candle volume baseline.
        # -----------------------------------------------------

        baseline_values = [
            c.quote_volume
            for c in previous_48
            if c.quote_volume > 0
        ]

        if len(baseline_values) < SMART_MONEY_CANDLES:

            log.warning(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=INVALID_VOLUME_HISTORY valid=%d/%d",
                source,
                symbol,
                len(baseline_values),
                SMART_MONEY_CANDLES,
            )

            return None

        # IMPORTANT:
        # Raw arithmetic mean.
        # NO trimming.
        # NO normalization.
        baseline_volume = (
            sum(baseline_values)
            / len(baseline_values)
        )

        if baseline_volume <= 0:

            log.warning(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=ZERO_BASELINE",
                source,
                symbol,
            )

            return None

        current_volume = current.quote_volume

        if current_volume <= 0:

            log.warning(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=ZERO_CURRENT_VOLUME",
                source,
                symbol,
            )

            return None

        # -----------------------------------------------------
        # Volume spike.
        # -----------------------------------------------------

        spike_multiplier = (
            current_volume
            / baseline_volume
        )

        required_multiplier = max(
            2.0,
            self.settings.volume_spike_ratio,
        )

        is_volume_spike = (
            spike_multiplier
            >= required_multiplier
        )

        # -----------------------------------------------------
        # Price move of the CLOSED candle.
        # -----------------------------------------------------

        candle_price_change = (
            (
                current.close
                - current.open
            )
            / current.open
            * 100
            if current.open > 0
            else 0.0
        )

        if not is_volume_spike:

            log.info(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=VOLUME_BELOW_THRESHOLD "
                "current_volume=$%.0f "
                "baseline=$%.0f "
                "multiplier=%.2fx "
                "required=%.2fx "
                "price_change=%+.2f%%",
                source,
                symbol,
                current_volume,
                baseline_volume,
                spike_multiplier,
                required_multiplier,
                candle_price_change,
            )

            # Still log pump/dump readiness.
            self._log_pump_history_status(
                source,
                symbol,
            )

            return None

        # -----------------------------------------------------
        # Pump/Dump statistical analysis.
        # -----------------------------------------------------

        zscore = None

        if self.settings.pump_zscore_enabled:

            zscore = self._calculate_pump_zscore(
                source,
                symbol,
                candle_price_change,
            )

        is_statistical_pump = (
            zscore is not None
            and zscore
            >= self.settings.pump_zscore_threshold
        )

        is_statistical_dump = (
            zscore is not None
            and zscore
            <= -self.settings.pump_zscore_threshold
        )

        # -----------------------------------------------------
        # Direction.
        # -----------------------------------------------------

        if candle_price_change > 0:

            direction = (
                SignalDirection.INFLOW
            )

        elif candle_price_change < 0:

            direction = (
                SignalDirection.OUTFLOW
            )

        else:

            log.info(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=ZERO_PRICE_CHANGE",
                source,
                symbol,
            )

            return None

        # -----------------------------------------------------
        # Static pump/dump thresholds.
        # -----------------------------------------------------

        is_static_pump = (
            direction == SignalDirection.INFLOW
            and self.settings.price_pump_min
            <= candle_price_change
            <= self.settings.price_pump_max
        )

        is_static_dump = (
            direction == SignalDirection.OUTFLOW
            and -self.settings.price_pump_max
            <= candle_price_change
            <= -self.settings.price_pump_min
        )

        # -----------------------------------------------------
        # SIGNAL DECISION
        # -----------------------------------------------------

        signal_trigger = None

        if direction == SignalDirection.INFLOW:

            if (
                is_static_pump
                and is_statistical_pump
            ):
                signal_trigger = (
                    TriggerType.BOTH
                )

            elif is_static_pump:
                signal_trigger = (
                    TriggerType.STATIC
                )

            elif is_statistical_pump:
                signal_trigger = (
                    TriggerType.STATISTICAL
                )

        else:

            if (
                is_static_dump
                and is_statistical_dump
            ):
                signal_trigger = (
                    TriggerType.BOTH
                )

            elif is_static_dump:
                signal_trigger = (
                    TriggerType.STATIC
                )

            elif is_statistical_dump:
                signal_trigger = (
                    TriggerType.STATISTICAL
                )

        # -----------------------------------------------------
        # If volume spike exists but no pump/dump condition.
        # -----------------------------------------------------

        if signal_trigger is None:

            log.info(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=VOLUME_SPIKE_BUT_PRICE_FILTER_FAILED "
                "volume=%.2fx required=%.2fx "
                "price_change=%+.2f%% "
                "zscore=%s",
                source,
                symbol,
                spike_multiplier,
                required_multiplier,
                candle_price_change,
                (
                    f"{zscore:.2f}"
                    if zscore is not None
                    else "N/A"
                ),
            )

            return None

        # -----------------------------------------------------
        # Cooldown.
        # -----------------------------------------------------

        cooldown_key = (
            f"market:{source}:{symbol}"
        )

        if self.state.is_in_cooldown(
            cooldown_key,
            self.settings.alert_cooldown_sec,
        ):

            log.info(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=COOLDOWN multiplier=%.2fx "
                "zscore=%s",
                source,
                symbol,
                spike_multiplier,
                (
                    f"{zscore:.2f}"
                    if zscore is not None
                    else "N/A"
                ),
            )

            return None

        # -----------------------------------------------------
        # Final signal.
        # -----------------------------------------------------

        signal = MarketSignal(
            symbol=symbol,
            price=current_price,
            change_5m=candle_price_change,
            change_24h=float(
                ticker.get(
                    "priceChangePercent",
                    0.0,
                )
            ),
            inflow_usd=current_volume,
            spike_multiplier=spike_multiplier,
            direction=direction,
            trigger=signal_trigger,
            zscore=zscore,
            source=source,
        )

        self.state.mark_alerted(
            cooldown_key
        )

        log.warning(
            "SIGNAL FIRED | source=%s symbol=%s "
            "direction=%s trigger=%s "
            "volume=%.2fx required=%.2fx "
            "current=$%.0f baseline=$%.0f "
            "price_change=%+.2f%% zscore=%s",
            source,
            symbol,
            direction.value,
            signal_trigger.value,
            spike_multiplier,
            required_multiplier,
            current_volume,
            baseline_volume,
            candle_price_change,
            (
                f"{zscore:.2f}"
                if zscore is not None
                else "N/A"
            ),
        )

        return signal

    # =========================================================
    # PUMP / DUMP Z-SCORE
    # =========================================================

    def _calculate_pump_zscore(
        self,
        source: str,
        symbol: str,
        current_change: float,
    ):

        history = self.candle_store.get_recent(
            source,
            symbol,
            PUMP_DUMP_CANDLES + 1,
        )

        # Need 864 historical returns BEFORE current candle.
        if len(history) < PUMP_DUMP_CANDLES + 1:

            log.info(
                "PUMP/DUMP NOT READY | "
                "source=%s symbol=%s "
                "history=%d/%d",
                source,
                symbol,
                max(
                    0,
                    len(history) - 1,
                ),
                PUMP_DUMP_CANDLES,
            )

            return None

        returns = []

        # Each historical return is candle close-to-close.
        for i in range(
            1,
            len(history) - 1,
        ):

            previous = history[i - 1]
            candle = history[i]

            if previous.close <= 0:
                continue

            change = (
                (
                    candle.close
                    - previous.close
                )
                / previous.close
                * 100
            )

            returns.append(change)

        if len(returns) < PUMP_DUMP_CANDLES:

            log.info(
                "PUMP/DUMP NOT READY | "
                "source=%s symbol=%s "
                "valid_returns=%d/%d",
                source,
                symbol,
                len(returns),
                PUMP_DUMP_CANDLES,
            )

            return None

        mean = statistics.mean(
            returns
        )

        stdev = statistics.pstdev(
            returns
        )

        if stdev <= 0:
            return None

        return (
            current_change - mean
        ) / stdev

    def _log_pump_history_status(
        self,
        source: str,
        symbol: str,
    ):

        count = self.candle_store.count(
            source,
            symbol,
        )

        if count < PUMP_DUMP_CANDLES:

            log.info(
                "PUMP/DUMP NOT READY | "
                "source=%s symbol=%s "
                "history=%d/%d",
                source,
                symbol,
                count,
                PUMP_DUMP_CANDLES,
            )

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

        return (
            f"🟢 <b>گزارش رصد زنده مارکت</b>\n\n"
            f"⏰ <b>زمان (UTC):</b> "
            f"<code>{__import__('datetime').datetime.now(__import__('datetime').timezone.utc).strftime('%H:%M:%S')}</code>\n"
            f"🌐 <b>منبع فعال تحلیل:</b> "
            f"<code>{esc(data_source)}</code>\n"
            f"🔍 <b>ارزهای آنالیز شده:</b> "
            f"<code>{symbols_scanned}</code>\n"
            f"📥 <b>سیگنال ورود:</b> "
            f"<code>{inflow_count}</code>\n"
            f"📤 <b>سیگنال خروج:</b> "
            f"<code>{outflow_count}</code>\n"
            f"📡 <b>وضعیت:</b> فعال"
        )