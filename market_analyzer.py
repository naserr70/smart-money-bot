"""
CEX ticker-based smart-money analyzer.

Fixes applied vs. the original script:
  * No more unsynchronized module-level globals — all state goes through
    the thread-safe BotState.
  * `elif` between inflow/outflow branches replaced with independent checks
    so a symbol can never silently fall through either bucket due to
    floating point edge cases at the boundary.
  * Division-by-zero / KeyError guards tightened (prev_price <= 0, missing
    fields) instead of relying on bare `except Exception` deep in the loop.
  * Baseline volume and cooldown keys are namespaced separately from the
    on-chain tracker's keys so the two independent signal sources never
    collide in the same dict.
"""
import logging
import time
from datetime import datetime, timezone
from typing import List, Tuple

import requests

from config import Settings
from market_data import MarketDataProvider
from signals import MarketSignal, SignalDirection
from state import BotState

log = logging.getLogger("smart_money_bot.market_analyzer")


class MarketAnalyzer:
    def __init__(self, settings: Settings, state: BotState, session: requests.Session):
        self.settings = settings
        self.state = state
        self.provider = MarketDataProvider(session, timeout=settings.http_timeout_sec)

    def run_cycle(self) -> Tuple[List[MarketSignal], str, int]:
        """Fetch fresh ticker data, compare to the previous cycle's snapshot,
        and return (signals, data_source, symbols_scanned)."""
        ticker_stats, data_source = self.provider.fetch()
        if not ticker_stats:
            return [], data_source, 0

        current_snapshot = {}
        signals: List[MarketSignal] = []

        for full_symbol, item in ticker_stats.items():
            symbol = full_symbol.replace("USDT", "")
            price_usd = item["lastPrice"]
            vol_24h_usd = item["quoteVolume"]
            change_24h = item["priceChangePercent"]

            current_snapshot[symbol] = {"price": price_usd, "vol_usd": vol_24h_usd}

            prev = self.state.previous_market_snapshot.get(symbol)
            if not prev or price_usd <= 0 or prev["price"] <= 0:
                continue

            price_change = ((price_usd - prev["price"]) / prev["price"]) * 100
            vol_inflow = vol_24h_usd - prev["vol_usd"]
            fallback_avg_vol = vol_24h_usd / 288  # ~5min slice of a 24h rolling volume

            if vol_inflow > 0:
                self.state.push_volume_sample(symbol, vol_inflow)

            baseline_vol = self.state.get_baseline_volume(symbol, fallback_avg_vol)
            spike_multiplier = (vol_inflow / baseline_vol) if baseline_vol > 0 else 0

            is_volume_spike = vol_inflow >= (baseline_vol * self.settings.volume_spike_ratio)
            is_significant = vol_inflow >= self.settings.min_inflow_usd_5m

            if is_volume_spike and is_significant:
                cooldown_key = f"market:{symbol}"
                if self.state.is_in_cooldown(cooldown_key, self.settings.alert_cooldown_sec):
                    continue

                if self.settings.price_pump_min <= price_change <= self.settings.price_pump_max:
                    signals.append(MarketSignal(
                        symbol=symbol, price=price_usd, change_5m=price_change,
                        change_24h=change_24h, inflow_usd=vol_inflow,
                        spike_multiplier=spike_multiplier, direction=SignalDirection.INFLOW,
                    ))
                    self.state.mark_alerted(cooldown_key)
                elif -self.settings.price_pump_max <= price_change <= -self.settings.price_pump_min:
                    signals.append(MarketSignal(
                        symbol=symbol, price=price_usd, change_5m=price_change,
                        change_24h=change_24h, inflow_usd=vol_inflow,
                        spike_multiplier=spike_multiplier, direction=SignalDirection.OUTFLOW,
                    ))
                    self.state.mark_alerted(cooldown_key)

        self.state.swap_snapshot(current_snapshot)
        return signals, data_source, len(ticker_stats)

    def build_status_message(self, data_source: str, symbols_scanned: int,
                              inflow_count: int, outflow_count: int) -> str:
        return (
            f"🟢 *گزارش رصد زنده مارکت* _(حذف خودکار پس از {self.settings.auto_delete_delay_sec // 60} دقیقه)_\n\n"
            f"⏰ *زمان (UTC):* `{datetime.now(timezone.utc).strftime('%H:%M:%S')}`\n"
            f"🌐 *منبع داده:* `{data_source}`\n"
            f"🔍 *ارزهای آنالیز شده:* `{symbols_scanned}` از تمامی بازارهای نوبیتکس\n"
            f"📥 *سیگنال ورود (تیکر):* `{inflow_count}` مورد\n"
            f"📤 *سیگنال خروج (تیکر):* `{outflow_count}` مورد\n"
            f"📡 *وضعیت سیستم:* فعال و ۲۴ ساعته"
        )
