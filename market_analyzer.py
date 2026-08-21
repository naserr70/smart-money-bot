"""Exchange-aware market analysis using closed 5m candles only."""

from __future__ import annotations

import logging
import statistics
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

from assets import NOBITEX_ALL_ASSETS
from candle_store import Candle, CandleStore, SMART_MONEY_BASELINE_CANDLES, PUMP_HISTORY_CANDLES, VALID_SOURCES
from config import Settings
from formatting import esc
from market_data import MarketDataProvider
from signals import MarketSignal, SignalDirection, TriggerType
from state import BotState

log = logging.getLogger("smart_money_bot.market_analyzer")
LIVE_UPDATE_LIMIT = 3


def _candles_from_payload(data: dict) -> List[Candle]:
    if not isinstance(data, dict) or not isinstance(data.get("candles"), list):
        return []
    parsed = []
    now_ms = int(time.time() * 1000)
    for item in data["candles"]:
        if not isinstance(item, dict):
            continue
        try:
            candle = Candle(int(item["open_time"]), int(item["close_time"]), float(item["open"]), float(item["high"]), float(item["low"]), float(item["close"]), float(item["volume"]), float(item["quote_volume"]), int(item.get("trades", 0)))
            if candle.close_time < now_ms:
                parsed.append(candle)
        except (KeyError, TypeError, ValueError):
            continue
    return sorted({c.open_time: c for c in parsed}.values(), key=lambda c: c.open_time)


def _robust_zscore(values: List[float], current: float) -> Optional[float]:
    if len(values) < 20:
        return None
    median = statistics.median(values)
    mad = statistics.median([abs(x - median) for x in values])
    if mad > 0:
        return (current - median) / (1.4826 * mad)
    stdev = statistics.pstdev(values)
    return (current - statistics.mean(values)) / stdev if stdev > 0 else None


class MarketAnalyzer:
    def __init__(self, settings: Settings, state: BotState, session: requests.Session, candle_store: CandleStore):
        self.settings = settings
        self.state = state
        self.session = session
        self.provider = MarketDataProvider(session=session, timeout=settings.http_timeout_sec)
        self.candle_store = candle_store
        self._last_analyzed_open_time: Dict[str, int] = {}
        self._startup_bootstrap_done = False

    def _enabled_sources(self) -> List[str]:
        return self.settings.enabled_market_sources()

    def bootstrap_histories(self, github_backup=None, symbols: Optional[List[str]] = None, target_count: int = PUMP_HISTORY_CANDLES) -> dict:
        if self._startup_bootstrap_done:
            return {"skipped": True}
        target_count = min(self.settings.candle_history_limit, max(1, int(target_count)))
        symbol_list = list(symbols or NOBITEX_ALL_ASSETS)
        started = time.time()
        stats = {"local": 0, "github": 0, "api": 0, "failed": 0, "sources": {}}
        enabled = self._enabled_sources()
        try:
            source_tickers = dict(zip(VALID_SOURCES, self.provider.fetch_all_sources(enabled)))
        except Exception:
            log.exception("STARTUP TICKER FETCH FAILED")
            source_tickers = {source: {} for source in VALID_SOURCES}

        for source in enabled:
            tickers = source_tickers.get(source, {})
            source_stats = {"symbols": 0, "restored": 0, "seeded": 0, "failed": 0}
            for symbol in [s for s in symbol_list if s in tickers]:
                source_stats["symbols"] += 1
                try:
                    self.candle_store.load(source, symbol)
                    count = self.candle_store.count(source, symbol)
                    if count >= target_count:
                        stats["local"] += 1
                        continue
                    if github_backup and github_backup.is_configured():
                        payload = github_backup.download(source, symbol)
                        restored = _candles_from_payload(payload) if payload else []
                        if restored:
                            self.candle_store.seed(source, symbol, restored, mark_dirty=False)
                            count = self.candle_store.count(source, symbol)
                            if count >= target_count:
                                stats["github"] += 1
                                source_stats["restored"] += 1
                                continue
                    candles = self.provider.fetch_candles(source, symbol, target_count)
                    if candles:
                        self.candle_store.seed(source, symbol, candles, mark_dirty=True)
                        stats["api"] += 1
                        source_stats["seeded"] += 1
                    else:
                        stats["failed"] += 1
                        source_stats["failed"] += 1
                except Exception:
                    stats["failed"] += 1
                    source_stats["failed"] += 1
                    log.exception("BOOTSTRAP SYMBOL ERROR | source=%s symbol=%s", source, symbol)
            stats["sources"][source] = source_stats

        self.candle_store.save_dirty()
        self._startup_bootstrap_done = True
        stats["elapsed_sec"] = round(time.time() - started, 2)
        log.info("STARTUP BOOTSTRAP COMPLETE | %s", stats)
        return stats

    def run_cycle(self) -> Tuple[List[MarketSignal], str, int]:
        enabled = self._enabled_sources()
        try:
            raw = self.provider.fetch_all_sources(enabled)
            source_tickers = dict(zip(VALID_SOURCES, raw))
        except Exception:
            log.exception("MARKET TICKER FETCH FAILED")
            return [], "none", 0
        for source in enabled:
            self._maintain_history(source, source_tickers.get(source, {}))
        for source in enabled:
            self._live_update_candles(source, source_tickers.get(source, {}))
        active_source, ticker_stats = self._select_active_source(source_tickers)
        if not active_source:
            return [], "none", 0
        signals: List[MarketSignal] = []
        for symbol, ticker in ticker_stats.items():
            try:
                signal = self._analyze_symbol(active_source, symbol, ticker)
                if signal:
                    signals.append(signal)
            except Exception:
                log.exception("ANALYSIS ERROR | source=%s symbol=%s", active_source, symbol)
        return signals, active_source, len(ticker_stats)

    def _select_active_source(self, source_tickers: Dict[str, Dict[str, dict]]) -> Tuple[str, Dict[str, dict]]:
        order = [self.settings.preferred_market_source]
        for source in ("binance", "bybit", "kucoin"):
            if source not in order:
                order.append(source)
        for source in order:
            tickers = source_tickers.get(source) or {}
            if source in self._enabled_sources() and tickers:
                return source, tickers
        return "", {}

    def _maintain_history(self, source: str, tickers: Dict[str, dict]) -> None:
        if not tickers:
            return
        target = min(self.settings.candle_history_limit, PUMP_HISTORY_CANDLES)
        for symbol in tickers:
            try:
                self.candle_store.load(source, symbol)
                if self.candle_store.count(source, symbol) >= target:
                    continue
                candles = self.provider.fetch_candles(source, symbol, target)
                if candles:
                    self.candle_store.seed(source, symbol, candles, mark_dirty=True)
            except Exception:
                log.exception("HISTORY MAINTENANCE ERROR | source=%s symbol=%s", source, symbol)

    def _live_update_candles(self, source: str, tickers: Dict[str, dict]) -> None:
        if not tickers:
            return
        closed = 0
        for symbol in tickers:
            try:
                closed += self.candle_store.apply_recent(source, symbol, self.provider.fetch_candles(source, symbol, LIVE_UPDATE_LIMIT))
            except Exception:
                log.exception("LIVE UPDATE ERROR | source=%s symbol=%s", source, symbol)
        log.info("LIVE UPDATE | source=%s symbols=%s new_closed=%s", source, len(tickers), closed)

    @staticmethod
    def _previous_volume_mean(history: List[Candle], count: int) -> Optional[float]:
        if count <= 0 or len(history) < count + 1:
            return None
        values = [c.quote_volume for c in history[-(count + 1):-1] if c.quote_volume > 0]
        return sum(values) / count if len(values) == count else None

    @staticmethod
    def _price_change(open_price: float, close_price: float) -> Optional[float]:
        return (close_price - open_price) / open_price * 100.0 if open_price > 0 else None

    def _analyze_symbol(self, source: str, symbol: str, ticker: dict) -> Optional[MarketSignal]:
        history = self.candle_store.get_closed(source, symbol)
        minimum = max(self.settings.volume_baseline_candles + 1, self.settings.pump_min_history_candles)
        if len(history) < minimum:
            return None
        current = history[-1]
        key = f"{source}:{symbol}"
        if self._last_analyzed_open_time.get(key) == current.open_time:
            return None
        self._last_analyzed_open_time[key] = current.open_time
        if current.quote_volume <= 0 or current.open <= 0 or current.close <= 0:
            return None
        change = self._price_change(current.open, current.close)
        if change is None:
            return None

        baseline_48 = self._previous_volume_mean(history, self.settings.volume_baseline_candles)
        spike_48 = current.quote_volume / baseline_48 if baseline_48 else None
        volume_anomaly = bool(self.settings.volume_signal_enabled and baseline_48 and current.quote_volume >= baseline_48 * self.settings.volume_signal_multiplier)

        pump_window = history[-self.settings.pump_history_candles:]
        prior_pump = pump_window[:-1]
        pump_values = [c.quote_volume for c in prior_pump if c.quote_volume > 0]
        pump_baseline = sum(pump_values) / len(pump_values) if pump_values else None
        pump_spike = current.quote_volume / pump_baseline if pump_baseline else None
        pump_volume_anomaly = bool(pump_baseline and current.quote_volume >= pump_baseline * self.settings.volume_spike_ratio)

        returns: List[float] = []
        for previous, candle in zip(pump_window, pump_window[1:]):
            if previous.close > 0:
                returns.append((candle.close - previous.close) / previous.close * 100.0)
        current_return = returns[-1] if returns else None
        zscore = _robust_zscore(returns[:-1], current_return) if self.settings.pump_zscore_enabled and current_return is not None and len(returns) > self.settings.pump_min_history_candles else None

        static_pump = pump_volume_anomaly and self.settings.price_pump_min <= change <= self.settings.price_pump_max
        static_dump = pump_volume_anomaly and -self.settings.price_pump_max <= change <= -self.settings.price_pump_min
        statistical_pump = pump_volume_anomaly and zscore is not None and zscore >= self.settings.pump_zscore_threshold
        statistical_dump = pump_volume_anomaly and zscore is not None and zscore <= -self.settings.pump_zscore_threshold

        if static_pump or statistical_pump:
            direction = SignalDirection.INFLOW
            trigger = TriggerType.BOTH if static_pump and statistical_pump else TriggerType.STATISTICAL if statistical_pump else TriggerType.STATIC
            path, baseline, spike = "pump_dump_72h", pump_baseline or 0.0, pump_spike or 0.0
        elif static_dump or statistical_dump:
            direction = SignalDirection.OUTFLOW
            trigger = TriggerType.BOTH if static_dump and statistical_dump else TriggerType.STATISTICAL if statistical_dump else TriggerType.STATIC
            path, baseline, spike = "pump_dump_72h", pump_baseline or 0.0, pump_spike or 0.0
        elif volume_anomaly and abs(change) > 0:
            direction = SignalDirection.INFLOW if change > 0 else SignalDirection.OUTFLOW
            trigger = TriggerType.STATIC
            path, baseline, spike = "volume_anomaly_48", baseline_48 or 0.0, spike_48 or 0.0
        else:
            return None

        cooldown_key = f"market:{source}:{symbol}"
        if self.state.is_in_cooldown(cooldown_key, self.settings.alert_cooldown_sec):
            return None
        excess_volume = max(0.0, current.quote_volume - baseline)
        try:
            change_24h = float(ticker.get("priceChangePercent", 0.0))
        except (TypeError, ValueError):
            change_24h = 0.0
        signal = MarketSignal(symbol, current.close, change, change_24h, excess_volume, spike, direction, trigger, zscore, source, path)
        self.state.mark_alerted(cooldown_key)
        return signal

    def build_status_message(self, data_source: str, symbols_scanned: int, inflow_count: int, outflow_count: int) -> str:
        label = {"binance": "Binance", "bybit": "Bybit", "kucoin": "KuCoin", "none": "هیچ‌کدام"}.get(data_source, data_source)
        now_utc = datetime.now(timezone.utc).strftime("%H:%M:%S")
        return f"📡 <b>وضعیت رصد</b>\n\n⏰ <code>{now_utc}</code> UTC\n🌐 منبع: <code>{esc(label)}</code>\n🔍 نمادها: <code>{symbols_scanned}</code>\n🟢 سیگنال صعودی: <code>{inflow_count}</code>  🔴 سیگنال نزولی: <code>{outflow_count}</code>"
