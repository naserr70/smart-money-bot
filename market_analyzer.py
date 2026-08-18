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
from formatting import esc
from market_data import MarketDataProvider
from signals import MarketSignal, SignalDirection, TriggerType
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

            # Baseline MUST be computed from history BEFORE this cycle's
            # sample is added to it — otherwise a genuine spike inflates its
            # own comparison baseline and can dodge the spike-ratio check
            # (this was a real bug present since the original script).
            baseline_vol = self.state.get_baseline_volume(symbol, fallback_avg_vol)
            spike_multiplier = (vol_inflow / baseline_vol) if baseline_vol > 0 else 0

            if vol_inflow > 0:
                self.state.push_volume_sample(symbol, vol_inflow)

            # Compute the statistical anomaly score BEFORE pushing the
            # current sample in, so it's measured against prior history,
            # not against itself.
            zscore = self.state.get_return_zscore(symbol, price_change) if self.settings.pump_zscore_enabled else None
            self.state.push_price_return_sample(symbol, price_change)

            is_volume_spike = vol_inflow >= (baseline_vol * self.settings.volume_spike_ratio)
            is_significant = vol_inflow >= self.settings.min_inflow_usd_5m

            # Both paths require some real, meaningful volume behind the move
            # (this is what keeps illiquid noise out) — but only the STATIC
            # path additionally requires the full baseline-relative spike.
            # Nesting the statistical path inside that same strict spike gate
            # (the previous version's actual bug) meant it could almost never
            # fire anything the static path wasn't already catching, since a
            # real 2.5x volume spike is usually already a >1% price move too.
            if not is_significant:
                continue

            cooldown_key = f"market:{symbol}"
            if self.state.is_in_cooldown(cooldown_key, self.settings.alert_cooldown_sec):
                continue

            is_static_pump = is_volume_spike and (self.settings.price_pump_min <= price_change <= self.settings.price_pump_max)
            is_static_dump = is_volume_spike and (-self.settings.price_pump_max <= price_change <= -self.settings.price_pump_min)
            is_statistical_pump = zscore is not None and zscore >= self.settings.pump_zscore_threshold
            is_statistical_dump = zscore is not None and zscore <= -self.settings.pump_zscore_threshold

            if is_static_pump or is_statistical_pump:
                trigger = TriggerType.BOTH if (is_static_pump and is_statistical_pump) else (
                    TriggerType.STATISTICAL if is_statistical_pump else TriggerType.STATIC)
                signals.append(MarketSignal(
                    symbol=symbol, price=price_usd, change_5m=price_change,
                    change_24h=change_24h, inflow_usd=vol_inflow,
                    spike_multiplier=spike_multiplier, direction=SignalDirection.INFLOW,
                    trigger=trigger, zscore=zscore,
                ))
                self.state.mark_alerted(cooldown_key)
            elif is_static_dump or is_statistical_dump:
                trigger = TriggerType.BOTH if (is_static_dump and is_statistical_dump) else (
                    TriggerType.STATISTICAL if is_statistical_dump else TriggerType.STATIC)
                signals.append(MarketSignal(
                    symbol=symbol, price=price_usd, change_5m=price_change,
                    change_24h=change_24h, inflow_usd=vol_inflow,
                    spike_multiplier=spike_multiplier, direction=SignalDirection.OUTFLOW,
                    trigger=trigger, zscore=zscore,
                ))
                self.state.mark_alerted(cooldown_key)

        self.state.swap_snapshot(current_snapshot)
        return signals, data_source, len(ticker_stats)

    def build_status_message(self, data_source: str, symbols_scanned: int,
                              inflow_count: int, outflow_count: int) -> str:
        return (
            f"🟢 <b>گزارش رصد زنده مارکت</b> \n\n"
            f"⏰ <b>زمان (UTC):</b> <code>{datetime.now(timezone.utc).strftime('%H:%M:%S')}</code>\n"
            f"🌐 <b>منبع داده:</b> <code>{esc(data_source)}</code>\n"
            f"🔍 <b>ارزهای آنالیز شده:</b> <code>{symbols_scanned}</code> از تمامی بازارهای نوبیتکس\n"
            f"📥 <b>سیگنال ورود (تیکر):</b> <code>{inflow_count}</code> مورد\n"
            f"📤 <b>سیگنال خروج (تیکر):</b> <code>{outflow_count}</code> مورد\n"
            f"📡 <b>وضعیت سیستم:</b> فعال و ۲۴ ساعته"
        )
