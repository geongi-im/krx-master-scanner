import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import main


class OhlcvCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_dir = Path(self.tmp.name)
        self.cache_patcher = patch.object(main, "CACHE_DIR", self.cache_dir)
        self.cache_patcher.start()
        self.addCleanup(self.cache_patcher.stop)
        self.config = main.Config(fetch_retries=1, cache_ttl_hours=18)

    @staticmethod
    def frame(start: str, periods: int) -> pd.DataFrame:
        index = pd.date_range(start, periods=periods, freq="D")
        return pd.DataFrame(
            {
                "Open": range(100, 100 + periods),
                "High": range(101, 101 + periods),
                "Low": range(99, 99 + periods),
                "Close": range(100, 100 + periods),
                "Volume": range(1000, 1000 + periods),
            },
            index=index,
        )

    def test_fetch_ohlcv_reuses_fixed_symbol_cache_and_fetches_only_incremental_range(self):
        cached = self.frame("2024-01-01", 5)
        main.write_cached_ohlcv("005930", cached)
        old_mtime = datetime.now().timestamp() - 24 * 3600
        main.cache_path("005930").touch()
        main.cache_path("005930").chmod(0o644)
        import os

        os.utime(main.cache_path("005930"), (old_mtime, old_mtime))

        requested_start = "2024-01-01"
        incremental = self.frame("2024-01-05", 3)
        calls = []

        def fake_data_reader(symbol, start_date):
            calls.append((symbol, start_date))
            return incremental

        with patch.object(main.fdr, "DataReader", side_effect=fake_data_reader):
            result = main.fetch_ohlcv("005930", requested_start, self.config)

        self.assertEqual(calls, [("005930", "2024-01-05")])
        self.assertEqual(result.index.min(), pd.Timestamp("2024-01-01"))
        self.assertEqual(result.index.max(), pd.Timestamp("2024-01-07"))
        self.assertEqual(len(result), 7)
        self.assertTrue(main.cache_path("005930").exists())
        self.assertFalse((self.cache_dir / "ohlcv_005930_2024-01-01.csv").exists())

    def test_fetch_ohlcv_uses_fresh_cache_without_network_when_requested_range_is_covered(self):
        cached = self.frame("2024-01-01", 5)
        main.write_cached_ohlcv("005930", cached)

        with patch.object(main.fdr, "DataReader") as data_reader:
            result = main.fetch_ohlcv("005930", "2024-01-02", self.config)

        data_reader.assert_not_called()
        self.assertEqual(result.index.min(), pd.Timestamp("2024-01-02"))
        self.assertEqual(result.index.max(), pd.Timestamp("2024-01-05"))
        self.assertEqual(len(result), 4)

    def test_fetch_ohlcv_refetches_full_requested_range_when_cache_starts_too_late(self):
        cached = self.frame("2024-01-10", 2)
        main.write_cached_ohlcv("005930", cached)
        old_mtime = datetime.now().timestamp() - 24 * 3600
        import os

        os.utime(main.cache_path("005930"), (old_mtime, old_mtime))
        fetched = self.frame("2024-01-01", 12)

        with patch.object(main.fdr, "DataReader", return_value=fetched) as data_reader:
            result = main.fetch_ohlcv("005930", "2024-01-01", self.config)

        data_reader.assert_called_once_with("005930", "2024-01-01")
        self.assertEqual(result.index.min(), pd.Timestamp("2024-01-01"))
        self.assertEqual(result.index.max(), pd.Timestamp("2024-01-12"))


if __name__ == "__main__":
    unittest.main()
