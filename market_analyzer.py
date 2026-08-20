"""
Independent exchange-aware market analyzer.

Signal logic:

1. Smart Money:
   New CLOSED 5m candle volume compared with previous 48 CLOSED candles.

2. Pump / Dump:
   Price movement and volume anomaly evaluated against long history
   up to 864 CLOSED 5m candles.

3. Binance is preferred.
4. KuCoin is fallback.
5. Histories are NEVER mixed.
"""

import logging
import statistics
from typing import Dict, List, Tuple

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

log = logging.getLogger(
    "smart_money_bot.market_analyzer"
)


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
            session,
            timeout=settings.http_timeout_sec,
        )
        self.candle_store = candle_store

    # =========================================================
    # MAIN CYCLE
    # =========================================================

    def run_cycle(
        self,
    ) -> Tuple[List[MarketSignal], str, int]:

        log.info(
            "MARKET FETCH START"
        )

        binance_tickers, kucoin_tickers = (
            self.provider.fetch_all_sources()
        )

        # -----------------------------------------------------
        # Maintain both histories independently.
        # -----------------------------------------------------

        self._maintain_history(
            "binance",
            binance_tickers,
        )

        self._maintain_history(
            "kucoin",
            kucoin_tickers,
        )

        # -----------------------------------------------------
        # Select active signal source.
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
                "ACTIVE MARKET SOURCE | KuCoin FALLBACK | Binance unavailable"
            )

        else:

            log.error(
                "ACTIVE MARKET SOURCE FAILED | Binance and KuCoin unavailable"
            )

            return [], "none", 0

        log.info(
            "ANALYSIS START | source=%s symbols=%s",
            active_source,
            len(ticker_stats),
        )

        signals = []

        # -----------------------------------------------------
        # Analyze ONLY active source.
        # -----------------------------------------------------

        for symbol, ticker in ticker_stats.items():

            try:

                new_signal = self._analyze_symbol(
                    active_source,
                    symbol,
                    ticker,
                )

                if new_signal:

                    signals.append(
                        new_signal
                    )

            except Exception as e:

                log.exception(
                    "ANALYSIS ERROR | source=%s symbol=%s error=%s",
                    active_source,
                    symbol,
                    e,
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

        log.info(
            "HISTORY MAINTENANCE | source=%s symbols=%s",
            source,
            len(tickers),
        )

        for symbol in tickers:

            try:

                if self.candle_store.count(
                    source,
                    symbol,
                ) >= PUMP_HISTORY_CANDLES:

                    continue

                loaded = self.candle_store.load(
                    source,
                    symbol,
                )

                current_count = (
                    self.candle_store.count(
                        source,
                        symbol,
                    )
                )

                if loaded and current_count >= (
                    SMART_MONEY_BASELINE_CANDLES
                ):
                    continue

                # -------------------------------------------------
                # Bootstrap ONLY from same exchange.
                # -------------------------------------------------

                if source == "binance":

                    candles = (
                        self.provider.fetch_binance_candles(
                            symbol,
                            PUMP_HISTORY_CANDLES,
                        )
                    )

                else:

                    candles = (
                        self.provider.fetch_kucoin_candles(
                            symbol,
                            PUMP_HISTORY_CANDLES,
                        )
                    )

                if candles:

                    self.candle_store.seed(
                        source,
                        symbol,
                        candles,
                    )

                    log.info(
                        "HISTORY BOOTSTRAP OK | source=%s symbol=%s candles=%s/%s",
                        source,
                        symbol,
                        self.candle_store.count(
                            source,
                            symbol,
                        ),
                        PUMP_HISTORY_CANDLES,
                    )

                else:

                    log.warning(
                        "HISTORY BOOTSTRAP FAILED | source=%s symbol=%s",
                        source,
                        symbol,
                    )

            except Exception as e:

                log.exception(
                    "HISTORY MAINTENANCE ERROR | source=%s symbol=%s error=%s",
                    source,
                    symbol,
                    e,
                )

    # =========================================================
    # SYMBOL ANALYSIS
    # =========================================================

    def _analyze_symbol(
        self,
        source: str,
        symbol: str,
        ticker: dict,
    ) -> MarketSignal:

        history = self.candle_store.get_closed(
            source,
            symbol,
        )

        if len(history) < SMART_MONEY_BASELINE_CANDLES + 1:

            log.warning(
                "NO_SIGNAL | source=%s symbol=%s reason=INSUFFICIENT_VOLUME_HISTORY history=%s/%s",
                source,
                symbol,
                len(history),
                SMART_MONEY_BASELINE_CANDLES + 1,
            )

            return None

        # -----------------------------------------------------
        # IMPORTANT:
        #
        # We analyze the most recently CLOSED candle.
        #
        # Not the current live candle.
        # -----------------------------------------------------

        current_candle = history[-1]

        baseline_candles = history[
            -SMART_MONEY_BASELINE_CANDLES - 1:
            -1
        ]

        if len(baseline_candles) != (
            SMART_MONEY_BASELINE_CANDLES
        ):

            log.warning(
                "NO_SIGNAL | source=%s symbol=%s reason=BASELINE_NOT_READY",
                source,
                symbol,
            )

            return None

        baseline_values = [
            c.quote_volume
            for c in baseline_candles
            if c.quote_volume > 0
        ]

        if len(baseline_values) != (
            SMART_MONEY_BASELINE_CANDLES
        ):

            log.warning(
                "NO_SIGNAL | source=%s symbol=%s reason=INVALID_BASELINE_VALUES",
                source,
                symbol,
            )

            return None

        baseline = (
            sum(baseline_values)
            / len(baseline_values)
        )

        current_volume = (
            current_candle.quote_volume
        )

        if baseline <= 0:

            log.warning(
                "NO_SIGNAL | source=%s symbol=%s reason=BASELINE_ZERO",
                source,
                symbol,
            )

            return None

        spike_multiplier = (
            current_volume / baseline
        )

        # -----------------------------------------------------
        # Price movement of CLOSED candle.
        # -----------------------------------------------------

        if current_candle.open <= 0:

            log.warning(
                "NO_SIGNAL | source=%s symbol=%s reason=INVALID_OPEN_PRICE",
                source,
                symbol,
            )

            return None

        price_change = (
            (
                current_candle.close
                - current_candle.open
            )
            / current_candle.open
        ) * 100

        # -----------------------------------------------------
        # Smart Money threshold.
        # -----------------------------------------------------

        volume_threshold = (
            baseline
            * self.settings.volume_spike_ratio
        )

        is_volume_spike = (
            current_volume
            >= volume_threshold
        )

        # -----------------------------------------------------
        # Long-term history.
        # -----------------------------------------------------

        long_history = history[
            -PUMP_HISTORY_CANDLES:
        ]

        long_volume_values = [
            c.quote_volume
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
        # Price anomaly using long history.
        # -----------------------------------------------------

        long_returns = []

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
                )
                * 100
            )

        zscore = None

        if (
            self.settings.pump_zscore_enabled
            and len(long_returns) >= 100
        ):

            mean = statistics.mean(
                long_returns
            )

            stdev = statistics.pstdev(
                long_returns
            )

            if stdev > 0:

                zscore = (
                    price_change - mean
                ) / stdev

        # -----------------------------------------------------
        # Static Pump / Dump.
        # -----------------------------------------------------

        static_pump = (
            is_volume_spike
            and
            self.settings.price_pump_min
            <= price_change
            <= self.settings.price_pump_max
        )

        static_dump = (
            is_volume_spike
            and
            -self.settings.price_pump_max
            <= price_change
            <= -self.settings.price_pump_min
        )

        statistical_pump = (
            zscore is not None
            and
            zscore
            >= self.settings.pump_zscore_threshold
            and
            is_volume_spike
        )

        statistical_dump = (
            zscore is not None
            and
            zscore
            <= -self.settings.pump_zscore_threshold
            and
            is_volume_spike
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
                "required=%.2fX price=%.2f%% zscore=%s",
                source,
                symbol,
                current_volume,
                baseline,
                spike_multiplier,
                self.settings.volume_spike_ratio,
                price_change,
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
                "NO_SIGNAL | source=%s symbol=%s reason=COOLDOWN",
                source,
                symbol,
            )

            return None

        # -----------------------------------------------------
        # Direction
        # -----------------------------------------------------

        if (
            static_pump
            or statistical_pump
        ):

            direction = (
                SignalDirection.INFLOW
            )

            if (
                static_pump
                and statistical_pump
            ):
                trigger = TriggerType.BOTH

            elif statistical_pump:
                trigger = TriggerType.STATISTICAL

            else:
                trigger = TriggerType.STATIC

        else:

            direction = (
                SignalDirection.OUTFLOW
            )

            if (
                static_dump
                and statistical_dump
            ):
                trigger = TriggerType.BOTH

            elif statistical_dump:
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
            change_24h=float(
                ticker.get(
                    "priceChangePercent",
                    0,
                )
            ),
            inflow_usd=current_volume,
            spike_multiplier=spike_multiplier,
            direction=direction,
            trigger=trigger,
            zscore=zscore,
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
            price_change,
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

        return (
            f"🟢 <b>گزارش رصد زنده مارکت</b>\n\n"
            f"⏰ <b>زمان (UTC):</b> "
            f"<code>{datetime.now(timezone.utc).strftime('%H:%M:%S')}</code>\n"
            f"🌐 <b>منبع فعال:</b> "
            f"<code>{esc(source_label)}</code>\n"
            f"🔍 <b>ارزهای آنالیز شده:</b> "
            f"<code>{symbols_scanned}</code>\n"
            f"📥 <b>سیگنال ورود:</b> "
            f"<code>{inflow_count}</code>\n"
            f"📤 <b>سیگنال خروج:</b> "
            f"<code>{outflow_count}</code>\n"
            f"📡 <b>وضعیت:</b> فعال"
        )