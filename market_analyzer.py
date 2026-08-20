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
        # IMPORTANT:
        # Maintain BOTH histories independently.
        #
        # Never do:
        #
        # Binance unavailable -> copy KuCoin into Binance history
        #
        # That is explicitly forbidden.
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
    # HISTORY MAINTENANCE
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

                # Full history already exists.
                if current_count >= PUMP_HISTORY_CANDLES:
                    continue

                # -------------------------------------------------
                # Try loading existing local history first.
                # -------------------------------------------------

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

                # -------------------------------------------------
                # If enough history already exists, don't repeatedly
                # bootstrap it on every scan.
                #
                # CandleStore is responsible for maintaining the
                # rolling history after this bootstrap.
                # -------------------------------------------------

                if current_count >= SMART_MONEY_BASELINE_CANDLES:
                    continue

                # -------------------------------------------------
                # Bootstrap ONLY from the SAME exchange.
                # -------------------------------------------------

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

        # -----------------------------------------------------
        # history is already CLOSED candles.
        #
        # The final item is therefore the newest CLOSED candle,
        # never the currently open candle.
        # -----------------------------------------------------

        current_candle = history[-1]

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

        # -----------------------------------------------------
        # Current candle open -> close movement.
        # Used for the user-facing 5m price movement.
        # -----------------------------------------------------

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

        # -----------------------------------------------------
        # Smart-money volume threshold.
        # -----------------------------------------------------

        volume_threshold = (
            baseline
            * self.settings.volume_spike_ratio
        )

        is_volume_spike = (
            current_volume >= volume_threshold
        )

        # -----------------------------------------------------
        # Long-term CLOSED history.
        # -----------------------------------------------------

        long_history = history[
            -PUMP_HISTORY_CANDLES:
        ]

        long_volume_values = [
            float(c.quote_volume)
            for c in long_history
            if c.quote_volume > 0
        ]

        long_average = None

        if len(long_volume_values) >= 100:
            long_average = (
                sum(long_volume_values)
                / len(long_volume_values)
            )

        # -----------------------------------------------------
        # Statistical price anomaly.
        #
        # IMPORTANT FIX:
        #
        # The historical distribution is CLOSE-to-CLOSE returns.
        # Therefore the current observation must ALSO be a
        # CLOSE-to-CLOSE return.
        #
        # The old implementation compared:
        #
        #     current open -> close
        #
        # against:
        #
        #     previous close -> current close
        #
        # which is statistically inconsistent.
        # -----------------------------------------------------

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

            # Exclude the current observation from the baseline.
            # Otherwise the event being tested partially determines
            # its own mean/stdev.
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

        # -----------------------------------------------------
        # Static pump / dump.
        #
        # Static detection remains based on the CLOSED candle's
        # own open -> close movement.
        # -----------------------------------------------------

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

        # -----------------------------------------------------
        # NO SIGNAL
        # -----------------------------------------------------

        if not (
            static_pump
            or static_dump
            or statistical_pump
            or statistical_dump
        ):

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

        # -----------------------------------------------------
        # COOLDOWN
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
                "reason=COOLDOWN",
                source,
                symbol,
            )

            return None

        # -----------------------------------------------------
        # Direction / trigger
        # -----------------------------------------------------

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

        # -----------------------------------------------------
        # Signal
        # -----------------------------------------------------

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
            inflow_usd=current_volume,
            spike_multiplier=spike_multiplier,
            direction=direction,
            trigger=trigger,
            zscore=zscore,
            source=source,
        )

        self.state.mark_alerted(
            cooldown_key
        )

        log.warning(
            "SIGNAL FIRED | source=%s symbol=%s "
            "direction=%s trigger=%s "
            "volume=%.2f baseline=%.2f spike=%.2fX "
            "price=%.2f%% zscore=%s",
            source,
            symbol,
            direction.value,
            trigger.value,
            current_volume,
            baseline,
            spike_multiplier,
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