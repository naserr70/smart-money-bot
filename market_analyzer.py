import logging
import statistics
import time
from datetime import datetime, timezone
from typing import List, Tuple

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

log = logging.getLogger("smart_money_bot.market_analyzer")


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

        self._bootstrapped = set()

    # ---------------------------------------------------------
    # main cycle
    # ---------------------------------------------------------

    def run_cycle(
        self,
    ) -> Tuple[List[MarketSignal], str, int]:

        ticker_stats, data_source = self.provider.fetch()

        if not ticker_stats:
            return [], data_source, 0

        signals: List[MarketSignal] = []

        current_snapshot = {}

        for symbol, item in ticker_stats.items():

            try:
                full_symbol = item["binance_symbol"]

                price = float(item["lastPrice"])
                change_24h = float(
                    item["priceChangePercent"]
                )

            except (KeyError, TypeError, ValueError):
                continue

            if price <= 0:
                continue

            current_snapshot[symbol] = {
                "price": price,
            }

            # -------------------------------------------------
            # Bootstrap 72h history
            # -------------------------------------------------

            if symbol not in self._bootstrapped:

                loaded = self.state.candles.load(
                    symbol
                )

                if not loaded or self.state.candles.count(
                    symbol
                ) < self.settings.pump_history_candles:

                    self._bootstrap_symbol(
                        symbol,
                        full_symbol,
                    )

                self._bootstrapped.add(symbol)

            # -------------------------------------------------
            # Current live 5m candle
            # -------------------------------------------------

            candle = self.provider.fetch_current_5m_candle(
                full_symbol
            )

            if candle is None:
                continue

            update_type = self.state.candles.update(
                symbol,
                candle,
            )

            # -------------------------------------------------
            # 4-hour volume baseline
            # -------------------------------------------------

            baseline_4h = (
                self.state.candles.average_quote_volume(
                    symbol,
                    self.settings.volume_baseline_candles,
                )
            )

            if baseline_4h is None or baseline_4h <= 0:
                continue

            current_volume = candle.quote_volume

            # IMPORTANT:
            # No elapsed-time normalization.
            # Current live volume is compared directly with
            # the average of previous CLOSED 5m candles.

            spike_multiplier = (
                current_volume / baseline_4h
            )

            volume_inflow = (
                current_volume - baseline_4h
            )

            # -------------------------------------------------
            # Volume signal
            # -------------------------------------------------

            is_volume_spike = (
                current_volume
                >= baseline_4h
                * self.settings.volume_spike_ratio
            )

            is_significant = (
                volume_inflow
                >= self.settings.min_inflow_usd_5m
            )

            # -------------------------------------------------
            # 72h price anomaly
            # -------------------------------------------------

            zscore = None

            if self.settings.pump_zscore_enabled:

                zscore = self._get_price_zscore(
                    symbol,
                    candle,
                )

            # -------------------------------------------------
            # price movement
            # -------------------------------------------------

            prev_candle = self._previous_closed_candle(
                symbol
            )

            if prev_candle is None:
                continue

            if prev_candle.close <= 0:
                continue

            price_change = (
                (candle.close - prev_candle.close)
                / prev_candle.close
            ) * 100

            # -------------------------------------------------
            # We need meaningful volume before any signal.
            # -------------------------------------------------

            if not is_significant:
                continue

            cooldown_key = f"market:{symbol}"

            if self.state.is_in_cooldown(
                cooldown_key,
                self.settings.alert_cooldown_sec,
            ):
                continue

            # -------------------------------------------------
            # Static pump / dump
            # -------------------------------------------------

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

            # -------------------------------------------------
            # Statistical pump / dump
            # -------------------------------------------------

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

            # -------------------------------------------------
            # IN
            # -------------------------------------------------

            if static_pump or statistical_pump:

                trigger = self._trigger(
                    static_pump,
                    statistical_pump,
                )

                signals.append(
                    MarketSignal(
                        symbol=symbol,
                        price=candle.close,
                        change_5m=price_change,
                        change_24h=change_24h,
                        inflow_usd=volume_inflow,
                        spike_multiplier=spike_multiplier,
                        direction=SignalDirection.INFLOW,
                        trigger=trigger,
                        zscore=zscore,
                    )
                )

                self.state.mark_alerted(
                    cooldown_key
                )

            # -------------------------------------------------
            # OUT
            # -------------------------------------------------

            elif static_dump or statistical_dump:

                trigger = self._trigger(
                    static_dump,
                    statistical_dump,
                )

                signals.append(
                    MarketSignal(
                        symbol=symbol,
                        price=candle.close,
                        change_5m=price_change,
                        change_24h=change_24h,
                        inflow_usd=volume_inflow,
                        spike_multiplier=spike_multiplier,
                        direction=SignalDirection.OUTFLOW,
                        trigger=trigger,
                        zscore=zscore,
                    )
                )

                self.state.mark_alerted(
                    cooldown_key
                )

        self.state.swap_snapshot(
            current_snapshot
        )

        return (
            signals,
            data_source,
            len(ticker_stats),
        )

    # ---------------------------------------------------------
    # bootstrap
    # ---------------------------------------------------------

    def _bootstrap_symbol(
        self,
        symbol: str,
        binance_symbol: str,
    ) -> None:

        try:
            candles = self.provider.fetch_5m_klines(
                binance_symbol,
                limit=864,
            )

            if not candles:
                return

            # The last returned candle may still be open.
            # Keep it separate from the closed history.
            now_ms = int(time.time() * 1000)

            closed = [
                candle
                for candle in candles
                if candle.close_time < now_ms
            ]

            current = candles[-1]

            self.state.candles.seed(
                symbol,
                closed[-864:],
            )

            if current.close_time >= now_ms:
                self.state.candles.update(
                    symbol,
                    current,
                )

            self.state.candles.save(symbol)

            log.info(
                "تاریخچه 5m برای %s آماده شد: %d کندل",
                symbol,
                len(closed[-864:]),
            )

        except Exception:
            log.exception(
                "bootstrap برای %s ناموفق بود",
                symbol,
            )

    # ---------------------------------------------------------
    # price z-score
    # ---------------------------------------------------------

    def _get_price_zscore(
        self,
        symbol: str,
        current_candle,
    ):

        candles = self.state.candles.get_recent(
            symbol,
            self.settings.pump_history_candles,
        )

        if len(candles) < 48:
            return None

        returns = []

        previous = None

        for candle in candles:

            if previous is not None:
                if previous.close > 0:
                    returns.append(
                        (
                            candle.close
                            - previous.close
                        )
                        / previous.close
                        * 100
                    )

            previous = candle

        if not returns:
            return None

        # Current live candle return is compared
        # against historical 72h candle returns.

        if candles[-1].close <= 0:
            return None

        current_return = (
            (
                current_candle.close
                - candles[-1].close
            )
            / candles[-1].close
        ) * 100

        if len(returns) < 20:
            return None

        mean = statistics.mean(returns)
        stdev = statistics.pstdev(returns)

        if stdev <= 0:
            return None

        return (
            current_return - mean
        ) / stdev

    # ---------------------------------------------------------
    # previous candle
    # ---------------------------------------------------------

    def _previous_closed_candle(
        self,
        symbol: str,
    ):

        candles = self.state.candles.get_recent(
            symbol,
            2,
        )

        if not candles:
            return None

        return candles[-1]

    # ---------------------------------------------------------
    # trigger
    # ---------------------------------------------------------

    @staticmethod
    def _trigger(
        static: bool,
        statistical: bool,
    ):

        if static and statistical:
            return TriggerType.BOTH

        if statistical:
            return TriggerType.STATISTICAL

        return TriggerType.STATIC

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
            f"<code>{inflow_count}</code> مورد\n"
            f"📤 <b>سیگنال خروج:</b> "
            f"<code>{outflow_count}</code> مورد\n"
            f"📡 <b>وضعیت سیستم:</b> فعال و ۲۴ ساعته"
        )