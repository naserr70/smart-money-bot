"""
CEX ticker + 5m closed-candle smart-money analyzer.

IMPORTANT:

The current/open candle is NOT used for signal generation.

For every newly closed 5m candle:

    1. Read previous 48 CLOSED candles.
    2. Calculate simple arithmetic mean quote volume.
    3. Compare the newly closed candle against that mean.
    4. Require volume >= VOLUME_SPIKE_RATIO * baseline.
    5. Calculate price movement.
    6. Calculate 72h z-score from stored history.
    7. Generate signal.
    8. ONLY THEN store the new candle.

This prevents the current signal candle from contaminating
its own baseline.
"""

import logging
import statistics
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

from candle_store import Candle, CandleStore
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
        candle_store: Optional[CandleStore] = None,
    ):

        self.settings = settings
        self.state = state

        self.provider = MarketDataProvider(
            session,
            timeout=settings.http_timeout_sec,
        )

        self.candle_store = (
            candle_store
            or CandleStore(
                root_path=settings.candle_store_path,
                max_candles=settings.history_window,
            )
        )

        self._loaded_symbols = set()

    # =========================================================
    # Main cycle
    # =========================================================

    def run_cycle(
        self,
    ) -> Tuple[
        List[MarketSignal],
        str,
        int,
    ]:

        ticker_stats, data_source = (
            self.provider.fetch()
        )

        if not ticker_stats:
            return (
                [],
                data_source,
                0,
            )

        signals: List[MarketSignal] = []

        # -----------------------------------------------------
        # Process every available symbol
        # -----------------------------------------------------

        for full_symbol, item in ticker_stats.items():

            symbol = full_symbol.replace(
                "USDT",
                "",
            )

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

                continue

            if price_usd <= 0:
                continue

            # -------------------------------------------------
            # Load local candle history
            # -------------------------------------------------

            self._ensure_loaded(
                symbol
            )

            # -------------------------------------------------
            # Update candle store
            # -------------------------------------------------

            new_candle = self._get_latest_closed_candle(
                full_symbol
            )

            if new_candle is None:
                continue

            # -------------------------------------------------
            # IMPORTANT:
            #
            # At this moment new_candle is the newly CLOSED
            # candle.
            #
            # It has NOT been inserted into the store yet.
            #
            # Therefore get_recent(48) contains only the
            # previous 48 candles.
            # -------------------------------------------------

            signal = self._analyze_closed_candle(
                symbol=symbol,
                price_usd=price_usd,
                change_24h=change_24h,
                candle=new_candle,
            )

            if signal is not None:
                signals.append(signal)

            # -------------------------------------------------
            # After analysis:
            #
            # store the new candle.
            # -------------------------------------------------

            self.candle_store.add_closed(
                symbol,
                new_candle,
            )

        # -----------------------------------------------------
        # Persist changed local candles
        # -----------------------------------------------------

        self.candle_store.save_dirty()

        return (
            signals,
            data_source,
            len(ticker_stats),
        )

    # =========================================================
    # Candle loading
    # =========================================================

    def _ensure_loaded(
        self,
        symbol: str,
    ) -> None:

        if symbol in self._loaded_symbols:
            return

        loaded = self.candle_store.load(
            symbol
        )

        self._loaded_symbols.add(
            symbol
        )

        if loaded:
            return

        # No local history.
        #
        # Bootstrap with real Binance history.
        #
        # This should happen only once per symbol,
        # not every scan.

        try:

            candles = (
                self.provider.fetch_recent_5m_candles(
                    f"{symbol}USDT",
                    limit=self.settings.history_window,
                )
            )

            if not candles:
                return

            parsed = []

            for raw in candles:

                try:

                    candle = Candle.from_binance(
                        raw
                    )

                    # Do not bootstrap an open candle
                    # into closed history.
                    #
                    # Binance's close time is used here.
                    #
                    # If it is still in the future,
                    # skip it.

                    now_ms = (
                        int(
                            datetime.now(
                                timezone.utc
                            ).timestamp()
                            * 1000
                        )
                    )

                    if candle.close_time > now_ms:
                        continue

                    parsed.append(
                        candle
                    )

                except (
                    TypeError,
                    ValueError,
                    IndexError,
                ):
                    continue

            if parsed:

                self.candle_store.seed(
                    symbol,
                    parsed,
                )

                self.candle_store.save(
                    symbol
                )

        except Exception as e:

            log.warning(
                "Bootstrap تاریخچه %s ناموفق بود: %s",
                symbol,
                e,
            )

    # =========================================================
    # Get latest closed candle
    # =========================================================

    def _get_latest_closed_candle(
        self,
        binance_symbol: str,
    ) -> Optional[Candle]:

        raw = (
            self.provider.fetch_latest_5m_candles(
                binance_symbol
            )
        )

        if not raw:
            return None

        now_ms = int(
            datetime.now(
                timezone.utc
            ).timestamp()
            * 1000
        )

        candidates = []

        for item in raw:

            try:

                candle = Candle.from_binance(
                    item
                )

            except (
                TypeError,
                ValueError,
                IndexError,
            ):
                continue

            # -------------------------------------------------
            # Only CLOSED candles.
            # -------------------------------------------------

            if candle.close_time <= now_ms:
                candidates.append(
                    candle
                )

        if not candidates:
            return None

        candidates.sort(
            key=lambda c: c.open_time
        )

        return candidates[-1]

    # =========================================================
    # Analyze newly closed candle
    # =========================================================

    def _analyze_closed_candle(
        self,
        symbol: str,
        price_usd: float,
        change_24h: float,
        candle: Candle,
    ) -> Optional[MarketSignal]:

        previous = self.candle_store.get_recent(
            symbol,
            self.settings.baseline_candles,
        )

        # Need a complete 48-candle baseline.
        if len(previous) < self.settings.baseline_candles:
            return None

        # -----------------------------------------------------
        # 1. SIMPLE AVERAGE
        #
        # No normalization.
        # No trimmed mean.
        # No median.
        # -----------------------------------------------------

        volumes = [
            c.quote_volume
            for c in previous
            if c.quote_volume > 0
        ]

        if len(volumes) < self.settings.baseline_candles:
            return None

        baseline_volume = (
            sum(volumes)
            / len(volumes)
        )

        if baseline_volume <= 0:
            return None

        current_volume = (
            candle.quote_volume
        )

        if current_volume <= 0:
            return None

        # -----------------------------------------------------
        # 2. VOLUME SPIKE
        # -----------------------------------------------------

        spike_multiplier = (
            current_volume
            / baseline_volume
        )

        is_volume_spike = (
            spike_multiplier
            >= self.settings.volume_spike_ratio
        )

        # If volume did not reach 2x baseline,
        # there is no smart-money volume signal.
        #
        # BUT we still calculate the price anomaly below,
        # because pump/dump is independent.
        # -----------------------------------------------------

        # -----------------------------------------------------
        # 3. PRICE CHANGE OF THE CLOSED CANDLE
        # -----------------------------------------------------

        previous_candle = previous[-1]

        if previous_candle.close <= 0:
            return None

        price_change = (
            (
                candle.close
                - previous_candle.close
            )
            / previous_candle.close
        ) * 100.0

        # -----------------------------------------------------
        # 4. 72H PRICE RETURN DISTRIBUTION
        # -----------------------------------------------------

        zscore = None

        if self.settings.pump_zscore_enabled:

            zscore = (
                self._calculate_72h_zscore(
                    symbol,
                    price_change,
                )
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
        # 5. STATIC PRICE CONDITIONS
        # -----------------------------------------------------

        is_static_pump = (
            price_change
            >= self.settings.price_pump_min
            and price_change
            <= self.settings.price_pump_max
        )

        is_static_dump = (
            price_change
            <= -self.settings.price_pump_min
            and price_change
            >= -self.settings.price_pump_max
        )

        # -----------------------------------------------------
        # 6. SIGNAL DECISION
        # -----------------------------------------------------

        # Smart-money volume signal:
        # MUST have >= 2x average.
        #
        # Pump/dump:
        # can be detected independently from volume.
        #
        # This preserves the original idea that volume and
        # price anomaly are complementary signals.

        if not is_volume_spike:
            return None

        if (
            not is_static_pump
            and not is_static_dump
            and not is_statistical_pump
            and not is_statistical_dump
        ):
            return None

        cooldown_key = (
            f"market:{symbol}"
        )

        if self.state.is_in_cooldown(
            cooldown_key,
            self.settings.alert_cooldown_sec,
        ):
            return None

        # -----------------------------------------------------
        # 7. Direction
        # -----------------------------------------------------

        if (
            is_static_pump
            or is_statistical_pump
        ):

            direction = (
                SignalDirection.INFLOW
            )

            static = is_static_pump
            statistical = (
                is_statistical_pump
            )

        elif (
            is_static_dump
            or is_statistical_dump
        ):

            direction = (
                SignalDirection.OUTFLOW
            )

            static = is_static_dump
            statistical = (
                is_statistical_dump
            )

        else:
            return None

        # -----------------------------------------------------
        # 8. Trigger label
        # -----------------------------------------------------

        if static and statistical:

            trigger = (
                TriggerType.BOTH
            )

        elif statistical:

            trigger = (
                TriggerType.STATISTICAL
            )

        else:

            trigger = (
                TriggerType.STATIC
            )

        # -----------------------------------------------------
        # 9. Create signal
        # -----------------------------------------------------

        signal = MarketSignal(
            symbol=symbol,

            price=candle.close,

            change_5m=price_change,

            change_24h=change_24h,

            # This is the actual quote volume of the
            # closed candle, not 24h-volume delta.
            inflow_usd=current_volume,

            spike_multiplier=spike_multiplier,

            direction=direction,

            trigger=trigger,

            zscore=zscore,

            baseline_volume_usd=baseline_volume,

            current_candle_volume_usd=current_volume,

            baseline_candles=self.settings.baseline_candles,
        )

        self.state.mark_alerted(
            cooldown_key
        )

        return signal

    # =========================================================
    # 72h Z-score
    # =========================================================

    def _calculate_72h_zscore(
        self,
        symbol: str,
        current_pct_change: float,
    ) -> Optional[float]:

        candles = self.candle_store.get_recent(
            symbol,
            self.settings.history_window,
        )

        if len(candles) < 5:
            return None

        returns = []

        previous_close = candles[0].close

        for candle in candles[1:]:

            if previous_close <= 0:
                previous_close = candle.close
                continue

            pct = (
                (
                    candle.close
                    - previous_close
                )
                / previous_close
            ) * 100.0

            returns.append(pct)

            previous_close = candle.close

        if len(returns) < 5:
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
            current_pct_change - mean
        ) / stdev

    # =========================================================
    # Status
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
            f"<code>"
            f"{datetime.now(timezone.utc).strftime('%H:%M:%S')}"
            f"</code>\n"

            f"🌐 <b>منبع داده:</b> "
            f"<code>{esc(data_source)}</code>\n"

            f"🔍 <b>ارزهای آنالیز شده:</b> "
            f"<code>{symbols_scanned}</code>\n"

            f"📥 <b>سیگنال ورود:</b> "
            f"<code>{inflow_count}</code> مورد\n"

            f"📤 <b>سیگنال خروج:</b> "
            f"<code>{outflow_count}</code> مورد\n"

            f"📚 <b>Baseline حجم:</b> "
            f"<code>48 × 5m</code>\n"

            f"📖 <b>تاریخچه:</b> "
            f"<code>864 × 5m = 72h</code>\n"

            f"📡 <b>وضعیت سیستم:</b> "
            f"فعال و ۲۴ ساعته"
        )