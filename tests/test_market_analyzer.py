import tempfile
import time
import unittest

import requests

from candle_store import Candle, CandleStore
from config import Settings
from market_analyzer import MarketAnalyzer
from state import BotState


class MarketAnalyzerTests(unittest.TestCase):
    def make_candle(self, index: int, quote_volume: float, change: float = 0.0) -> Candle:
        open_time = int(time.time() * 1000) - (index + 20) * 5 * 60 * 1000
        open_time -= open_time % (5 * 60 * 1000)
        open_price = 100.0
        close_price = open_price * (1.0 + change / 100.0)
        return Candle(open_time, open_time + 5 * 60 * 1000 - 1, open_price, max(open_price, close_price), min(open_price, close_price), close_price, 1.0, quote_volume, 1)

    def test_baseline_excludes_current_candle(self):
        history = [self.make_candle(i, 100.0) for i in range(60, 0, -1)]
        history.append(self.make_candle(0, 500.0, 1.0))
        baseline = MarketAnalyzer._previous_volume_mean(history, 48)
        self.assertEqual(baseline, 100.0)

    def test_two_x_volume_creates_bullish_anomaly(self):
        store = CandleStore(tempfile.mkdtemp(), max_candles=64)
        candles = [self.make_candle(i, 100.0) for i in range(60, 0, -1)]
        candles.append(self.make_candle(0, 250.0, 1.5))
        store.seed("binance", "BTCUSDT", candles)
        settings = Settings(
            candle_history_limit=64,
            pump_history_candles=64,
            pump_min_history_candles=20,
            volume_baseline_candles=48,
            volume_signal_multiplier=2.0,
            volume_spike_ratio=2.0,
            price_pump_min=1.0,
            price_pump_max=8.0,
            pump_zscore_enabled=False,
            alert_cooldown_sec=0,
        )
        state = BotState(64, tempfile.mktemp())
        analyzer = MarketAnalyzer(settings, state, requests.Session(), store)
        signal = analyzer._analyze_symbol("binance", "BTCUSDT", {"priceChangePercent": 2.0})
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction.value, "inflow")
        self.assertGreaterEqual(signal.spike_multiplier, 2.0)


if __name__ == "__main__":
    unittest.main()
