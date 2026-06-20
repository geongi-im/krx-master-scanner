import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

import main


class OhlcvCacheTest(unittest.TestCase):
    def setUp(self):
        """OHLCV 캐시 테스트에 사용할 기본 설정을 준비합니다."""
        self.config = main.Config(fetch_retries=1, cache_ttl_hours=18)

    @staticmethod
    def frame(start: str, periods: int) -> pd.DataFrame:
        """테스트용 OHLCV 데이터프레임을 생성합니다.

        Args:
            start: 시작 날짜 문자열입니다.
            periods: 생성할 일수입니다.

        Returns:
            OHLCV와 Amount 컬럼을 가진 테스트 데이터프레임입니다.
        """
        index = pd.date_range(start, periods=periods, freq="D")
        return pd.DataFrame(
            {
                "Open": range(100, 100 + periods),
                "High": range(101, 101 + periods),
                "Low": range(99, 99 + periods),
                "Close": range(100, 100 + periods),
                "Volume": range(1000, 1000 + periods),
                "Amount": [(100 + index) * (1000 + index) for index in range(periods)],
                "AmountSource": ["computed"] * periods,
            },
            index=index,
        )

    def test_fetch_ohlcv_reuses_db_cache_and_fetches_only_incremental_range(self):
        """캐시가 있으면 마지막 캐시일부터 증분 구간만 조회하는지 검증합니다."""
        cached = self.frame("2024-01-01", 5)
        requested_start = "2024-01-01"
        incremental = self.frame("2024-01-05", 3)
        calls = []

        def fake_data_reader(symbol, start_date):
            """FDR 증분 조회 호출 인자를 기록하고 테스트 데이터를 반환합니다.

            Args:
                symbol: 조회 대상 종목 코드입니다.
                start_date: 조회 시작일입니다.

            Returns:
                Amount 컬럼이 없는 증분 OHLCV 데이터프레임입니다.
            """
            calls.append((symbol, start_date))
            return incremental.drop(columns=["Amount", "AmountSource"])

        with (
            patch.object(main, "read_cached_ohlcv", return_value=cached),
            patch.object(main, "cache_is_fresh", return_value=False),
            patch.object(main.fdr, "DataReader", side_effect=fake_data_reader),
            patch.object(main, "write_cached_ohlcv") as write_cached,
        ):
            result = main.fetch_ohlcv("005930", requested_start, self.config)

        self.assertEqual(calls, [("005930", "2024-01-05")])
        self.assertEqual(result.index.min(), pd.Timestamp("2024-01-01"))
        self.assertEqual(result.index.max(), pd.Timestamp("2024-01-07"))
        self.assertEqual(len(result), 7)
        write_cached.assert_called_once()
        self.assertEqual(write_cached.call_args.args[0], "005930")
        self.assertIs(write_cached.call_args.args[2], self.config)

    def test_fetch_ohlcv_uses_fresh_db_cache_without_network_when_requested_range_is_covered(self):
        """신선한 DB 캐시가 있으면 외부 조회 없이 캐시만 사용하는지 검증합니다."""
        cached = self.frame("2024-01-02", 4)

        with (
            patch.object(main, "read_cached_ohlcv", return_value=cached),
            patch.object(main, "cache_is_fresh", return_value=True),
            patch.object(main.fdr, "DataReader") as data_reader,
        ):
            result = main.fetch_ohlcv("005930", "2024-01-02", self.config)

        data_reader.assert_not_called()
        self.assertEqual(result.index.min(), pd.Timestamp("2024-01-02"))
        self.assertEqual(result.index.max(), pd.Timestamp("2024-01-05"))
        self.assertEqual(len(result), 4)

    def test_fetch_ohlcv_refetches_full_requested_range_when_db_cache_misses(self):
        """DB 캐시가 없으면 요청 시작일부터 전체 구간을 조회하는지 검증합니다."""
        fetched = self.frame("2024-01-01", 12).drop(columns=["Amount", "AmountSource"])

        with (
            patch.object(main, "read_cached_ohlcv", return_value=None),
            patch.object(main.fdr, "DataReader", return_value=fetched) as data_reader,
            patch.object(main, "write_cached_ohlcv"),
        ):
            result = main.fetch_ohlcv("005930", "2024-01-01", self.config)

        data_reader.assert_called_once_with("005930", "2024-01-01")
        self.assertEqual(result.index.min(), pd.Timestamp("2024-01-01"))
        self.assertEqual(result.index.max(), pd.Timestamp("2024-01-12"))

    def test_fetch_ohlcv_falls_back_to_db_cache_when_incremental_refresh_fails(self):
        """증분 갱신 실패 시 기존 DB 캐시를 fallback으로 반환하는지 검증합니다."""
        cached = self.frame("2024-01-01", 5)

        with (
            patch.object(main, "read_cached_ohlcv", return_value=cached),
            patch.object(main, "cache_is_fresh", return_value=False),
            patch.object(main.fdr, "DataReader", side_effect=RuntimeError("LOGOUT")),
            patch.object(main, "write_cached_ohlcv") as write_cached,
        ):
            result = main.fetch_ohlcv("005930", "2024-01-01", self.config)

        self.assertEqual(result.index.min(), pd.Timestamp("2024-01-01"))
        self.assertEqual(result.index.max(), pd.Timestamp("2024-01-05"))
        write_cached.assert_not_called()

    def test_normalize_ohlcv_drops_rows_with_zero_ohlc(self):
        """OHLC 중 0 이하 값이 있는 행을 정규화 과정에서 제거하는지 검증합니다."""
        df = self.frame("2024-01-01", 3)
        df.loc[pd.Timestamp("2024-01-02"), ["Open", "High", "Low"]] = 0

        normalized = main.normalize_ohlcv(df)

        self.assertEqual(list(normalized.index), [pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-03")])

    def test_db_rows_are_converted_to_analysis_dataframe(self):
        """MariaDB 조회 행이 분석용 OHLCV 데이터프레임으로 변환되는지 검증합니다."""
        rows = [
            {
                "trade_date": pd.Timestamp("2024-01-02").date(),
                "open_price": 100,
                "high_price": 110,
                "low_price": 90,
                "close_price": 105,
                "volume": 1000,
                "amount": 105000,
                "amount_source": "computed",
            }
        ]

        df = main.ohlcv_from_db_rows(rows)

        self.assertEqual(df.index[0], pd.Timestamp("2024-01-02"))
        self.assertEqual(df["Open"].iloc[0], 100)
        self.assertEqual(df["Amount"].iloc[0], 105000)
        self.assertEqual(df["AmountSource"].iloc[0], "computed")

    def test_expected_latest_trade_date_handles_weekend_and_intraday_runs(self):
        """기대 최신 거래일이 주말과 장중 실행 시각을 고려하는지 검증합니다."""
        self.assertEqual(main.expected_latest_trade_date(datetime(2026, 6, 20, 10, 0)), date(2026, 6, 19))
        self.assertEqual(main.expected_latest_trade_date(datetime(2026, 6, 19, 10, 0)), date(2026, 6, 18))
        self.assertEqual(main.expected_latest_trade_date(datetime(2026, 6, 19, 20, 0)), date(2026, 6, 19))
        self.assertEqual(main.expected_latest_trade_date(datetime(2026, 6, 22, 10, 0)), date(2026, 6, 19))

    def test_collect_ohlcv_data_fetches_index_and_full_collection_universe(self):
        """전체 수집 단계가 KQ11과 수집 유니버스 전체 종목을 조회하는지 검증합니다."""
        config = main.Config(max_workers=1, collect_days=10)
        universe = pd.DataFrame(
            [
                {"Code": "000080", "Name": "A", "Sector": ""},
                {"Code": "000100", "Name": "B", "Sector": ""},
            ]
        )
        calls = []

        def fake_fetch(symbol, start_date, fetch_config):
            """수집 대상 호출 순서를 기록하고 테스트 OHLCV 데이터를 반환합니다.

            Args:
                symbol: 조회 대상 코드입니다.
                start_date: 조회 시작일입니다.
                fetch_config: 조회에 사용할 설정입니다.

            Returns:
                테스트 OHLCV 데이터프레임입니다.
            """
            calls.append((symbol, start_date, fetch_config))
            return self.frame("2024-01-01", 2)

        with (
            patch.object(main, "load_collection_universe", return_value=universe),
            patch.object(main, "fetch_ohlcv", side_effect=fake_fetch),
        ):
            stats = main.collect_ohlcv_data(config)

        self.assertEqual([call[0] for call in calls], ["KQ11", "000080", "000100"])
        self.assertTrue(all(call[2] is config for call in calls))
        self.assertEqual(stats["targets_total"], 3)
        self.assertEqual(stats["status_collected"], 3)

    def test_run_collects_all_data_before_limited_analysis(self):
        """분석용 max_symbols가 전체 수집 단계에 전달되지 않는지 검증합니다."""
        config = main.Config(max_workers=1)
        empty_universe = pd.DataFrame(columns=["Code", "Name", "Sector"])
        regime = SimpleNamespace(ok=True, is_bull_market=True, current=0.0, ma50=0.0, kq_return_60=0.0)

        with (
            patch.object(main, "setup_korean_font"),
            patch.object(main, "collect_ohlcv_data", return_value=main.Counter({"targets_total": 3})) as collect,
            patch.object(main, "check_market_regime", return_value=regime),
            patch.object(main, "load_universe", return_value=empty_universe) as load_universe,
            patch.object(main, "save_reports", return_value=1),
            patch.object(main, "send_telegram_msg"),
            patch.object(main, "run_vcp_pipeline") as run_vcp,
        ):
            exit_code = main.run(config, dry_run=True, max_symbols=2, no_charts=True)

        self.assertEqual(exit_code, 0)
        collect.assert_called_once_with(config)
        load_universe.assert_called_once_with(config, max_symbols=2)
        run_vcp.assert_called_once_with(config, dry_run=True, max_symbols=2, no_charts=True)


if __name__ == "__main__":
    unittest.main()
