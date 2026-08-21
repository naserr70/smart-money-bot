import tempfile
import time
import unittest

from candle_store import Candle, CandleStore


class CandleStoreTests(unittest.TestCase):
    def candle(self, offset_minutes: int, volume: float = 100.0) -> Candle:
        now = int(time.time() * 1000)
        open_time = now - (offset_minutes + 10) * 60 * 1000
        open_time -= open_time % (5 * 60 * 1000)
        return Candle(open_time, open_time + 5 * 60 * 1000 - 1, 100, 101, 99, 100.5, 10, volume, 1)

    def test_open_candle_is_rejected(self):
        store = CandleStore(tempfile.mkdtemp(), max_candles=10)
        now = int(time.time() * 1000)
        candle = Candle(now - 60_000, now + 240_000, 1, 1, 1, 1, 1, 1, 1)
        self.assertFalse(store.add_closed("binance", "BTCUSDT", candle))
        self.assertEqual(store.count("binance", "BTCUSDT"), 0)

    def test_sources_are_isolated(self):
        store = CandleStore(tempfile.mkdtemp(), max_candles=10)
        candle = self.candle(30)
        self.assertTrue(store.add_closed("binance", "BTCUSDT", candle))
        self.assertEqual(store.count("binance", "BTCUSDT"), 1)
        self.assertEqual(store.count("bybit", "BTCUSDT"), 0)

    def test_duplicate_closed_candle_is_idempotent(self):
        store = CandleStore(tempfile.mkdtemp(), max_candles=10)
        candle = self.candle(30)
        self.assertTrue(store.add_closed("binance", "BTCUSDT", candle))
        self.assertFalse(store.add_closed("binance", "BTCUSDT", candle))
        self.assertEqual(store.count("binance", "BTCUSDT"), 1)

    def test_history_is_bounded(self):
        store = CandleStore(tempfile.mkdtemp(), max_candles=3)
        candles = [self.candle(60 + i * 5, 100 + i) for i in range(5)]
        store.seed("binance", "BTCUSDT", candles)
        self.assertEqual(store.count("binance", "BTCUSDT"), 3)
        self.assertEqual(store.get_closed("binance", "BTCUSDT")[-1].quote_volume, 104)


if __name__ == "__main__":
    unittest.main()
