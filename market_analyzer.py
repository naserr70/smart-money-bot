"""
Independent exchange-aware market analyzer.

Signal logic
------------
1. Smart money (inflow / outflow):
   Latest CLOSED 5m quote_volume vs mean of the previous 48 CLOSED
   5m candles from the SAME exchange.
   Direction follows candle price change (up = inflow, down = outflow).

2. Pump / Dump:
   Volume anomaly vs mean of the previous candles inside the rolling
   864-candle window (72 hours), plus static price band and/or z-score.

3. Active analysis priority:
   Binance → Bybit → KuCoin

4. Histories are NEVER mixed across exchanges.
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
    VALID_SOURCES,
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

        self._last_signaled_open_time: Dict[str, int] = {}

    def run_cycle(
        self,
    ) -> Tuple[List[MarketSignal], str, int]:

        log.info("MARKET FETCH START")

        try:
            binance_tickers, bybit_tickers, kucoin_tickers = (
                self.provider.fetch_all_sources()
            )
        except Exception:
            log.exception("MARKET TICKER FETCH FAILED")
            return [], "none", 0

        sources_tickers = {
            "binance": binance_tickers,
            "bybit": bybit_tickers,
            "kucoin": kucoin_tickers,
        }

        for source, tickers in sources_tickers.items():
            self._maintain_history(source=source, tickers=tickers)

        for source, tickers in sources_tickers.items():
            self._live_update_candles(source=source, tickers=tickers)

        if binance_tickers:
            active_source = "binance"
            ticker_stats = binance_tickers
            log.info("ACTIVE MARKET SOURCE | Binance PRIMARY")
        elif bybit_tickers:
            active_source = "bybit"
            ticker_stats = bybit_tickers
            log.warning(
                "ACTIVE MARKET SOURCE | Bybit FALLBACK | "
                "Binance unavailable"
            )
        elif kucoin_tickers:
            active_source = "kucoin"
            ticker_stats = kucoin_tickers
            log.warning(
                "ACTIVE MARKET SOURCE | KuCoin FALLBACK | "
                "Binance and Bybit unavailable"
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

                candles = self.provider.fetch_candles(
                    source=source,
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
                if self.candle_store.count(source, symbol) == 0:
                    self.candle_store.load(source, symbol)

                candles = self.provider.fetch_candles(
                    source=source,
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

    @staticmethod
    def _baseline_mean(
        candles: List,
        count: int,
    ) -> Optional[float]:
        """
        Mean quote_volume of the last `count` candles before the newest
        candle in `candles` (newest is excluded).

        `candles` must already be ordered oldest → newest and include
        the signal candle as the last element.
        """

        if count <= 0 or len(candles) < count + 1:
            return None

        prior = candles[-(count + 1):-1]

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

        # Smart money needs 48 prior + 1 signal candle.
        minimum_smart = SMART_MONEY_BASELINE_CANDLES + 1

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
        last_ot = self._last_signaled_open_time.get(signal_key)

        if last_ot is not None and current_candle.open_time <= last_ot:
            return None

        current_volume = float(current_candle.quote_volume)

        if current_volume <= 0:
            log.warning(
                "NO_SIGNAL | source=%s symbol=%s "
                "reason=INVALID_VOLUME",
                source,
                symbol,
            )
            return None

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

        # -------------------------------------------------
        # 1) Smart-money baseline: previous 48 closed candles
        # -------------------------------------------------

        baseline_48 = self._baseline_mean(
            history,
            SMART_MONEY_BASELINE_CANDLES,
        )

        smart_spike = None
        smart_inflow = 0.0

        if baseline_48 is not None and baseline_48 > 0:

            smart_spike = current_volume / baseline_48
            smart_inflow = max(0.0, current_volume - baseline_48)

            is_smart_volume_spike = (
                current_volume
                >= baseline_48 * self.settings.volume_spike_ratio
            )

        else:

            is_smart_volume_spike = False

        # Direction for smart money: sign of candle body.
        smart_inflow_signal = (
            is_smart_volume_spike
            and candle_price_change > 0
        )

        smart_outflow_signal = (
            is_smart_volume_spike
            and candle_price_change < 0
        )

        # -------------------------------------------------
        # 2) Pump/dump baseline: previous candles in 72h window
        # -------------------------------------------------

        baseline_72h = self._baseline_mean(
            history,
            PUMP_HISTORY_CANDLES,
        )

        pump_spike = None

        if baseline_72h is not None and baseline_72h > 0:

            pump_spike = current_volume / baseline_72h

            is_pump_volume_spike = (
                current_volume
                >= baseline_72h * self.settings.volume_spike_ratio
            )

        else:

            is_pump_volume_spike = False

        long_history = history[-PUMP_HISTORY_CANDLES:]

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

        is_pump = static_pump or statistical_pump
        is_dump = static_dump or statistical_dump

        # -------------------------------------------------
        # Decide which signal (if any) wins this candle
        # Priority: pump/dump over pure smart-money
        # -------------------------------------------------

        if is_pump or is_dump:

            if is_pump:

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

            # Report spike vs the 72h baseline used for this path.
            spike_multiplier = (
                pump_spike if pump_spike is not None else 0.0
            )

            estimated_inflow = max(
                0.0,
                current_volume - (baseline_72h or 0.0),
            )

            baseline_used = baseline_72h or 0.0
            path = "pump_dump_72h"

        elif smart_inflow_signal or smart_outflow_signal:

            direction = (
                SignalDirection.INFLOW
                if smart_inflow_signal
                else SignalDirection.OUTFLOW
            )

            # Smart-money path has no static/z band — mark STATIC
            # as the volume-flow style trigger.
            trigger = TriggerType.STATIC

            spike_multiplier = (
                smart_spike if smart_spike is not None else 0.0
            )

            estimated_inflow = smart_inflow
            baseline_used = baseline_48 or 0.0
            path = "smart_money_48"

        else:

            self._last_signaled_open_time[signal_key] = (
                current_candle.open_time
            )

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
            "SIGNAL FIRED | source=%s symbol=%s path=%s "
            "direction=%s trigger=%s "
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
