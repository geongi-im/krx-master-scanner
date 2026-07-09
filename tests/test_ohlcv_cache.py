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

    def test_load_krx_finder_listing_parses_current_finder_response(self):
        """KRX finder fallback이 종목 코드, 이름, 시장을 표준 컬럼으로 변환하는지 검증합니다."""

        class FakeResponse:
            text = "{\"block1\": []}"
            content = (
                '{"block1": ['
                '{"short_code": "060310", "codeName": "3S", "marketEngName": "KOSDAQ"},'
                '{"short_code": "095570", "codeName": "AJ네트웍스", "marketEngName": "KOSPI"},'
                '{"short_code": "1234H0", "codeName": "테스트스팩", "marketEngName": "KOSDAQ GLOBAL"}'
                "]}"
            ).encode("utf-8")

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "block1": [
                        {"short_code": "060310", "codeName": "3S", "marketEngName": "KOSDAQ"},
                        {"short_code": "095570", "codeName": "AJ네트웍스", "marketEngName": "KOSPI"},
                        {"short_code": "1234H0", "codeName": "테스트스팩", "marketEngName": "KOSDAQ GLOBAL"},
                    ]
                }

        with patch.object(main.requests, "post", return_value=FakeResponse()) as post:
            listing = main.load_krx_finder_listing(timeout=3)

        post.assert_called_once()
        self.assertEqual(list(listing.columns), ["Code", "Name", "Market"])
        self.assertEqual(listing.loc[0, "Code"], "060310")
        self.assertEqual(listing.loc[2, "Market"], "KOSDAQ")
        self.assertFalse(bool(listing.attrs["has_market_data"]))

    def test_load_collection_universe_uses_krx_finder_when_fdr_listing_fails(self):
        """FDR KRX 목록이 깨져도 KRX finder fallback으로 수집 유니버스를 구성합니다."""
        config = main.Config()
        finder = pd.DataFrame(
            [
                {"Code": "060310", "Name": "3S", "Market": "KOSDAQ"},
                {"Code": "095570", "Name": "AJ네트웍스", "Market": "KOSPI"},
                {"Code": "1234H0", "Name": "문자코드", "Market": "KOSDAQ"},
                {"Code": "000001", "Name": "테스트스팩", "Market": "KOSDAQ"},
                {"Code": "000002", "Name": "우선주우", "Market": "KOSPI"},
                {"Code": "000003", "Name": "코넥스", "Market": "KONEX"},
            ]
        )
        desc = pd.DataFrame(
            [
                {"Code": "060310", "Sector": "기계"},
                {"Code": "095570", "Sector": "서비스"},
            ]
        )

        with (
            patch.object(main.fdr, "StockListing", side_effect=ValueError("LOGOUT")),
            patch.object(main, "load_krx_finder_listing", return_value=finder),
            patch.object(main, "load_krx_desc_listing", return_value=desc),
        ):
            universe = main.load_collection_universe(config)

        self.assertEqual(universe.to_dict("records"), [
            {"Code": "060310", "Name": "3S", "Sector": "기계"},
            {"Code": "095570", "Name": "AJ네트웍스", "Sector": "서비스"},
        ])

    def test_load_krx_listing_skips_fdr_when_direct_krx_listing_is_available(self):
        """정상 KRX 직접 endpoint가 있으면 FDR 시총 endpoint를 호출하지 않습니다."""
        config = main.Config()
        finder = pd.DataFrame([{"Code": "060310", "Name": "3S", "Market": "KOSDAQ"}])
        desc = pd.DataFrame([{"Code": "060310", "Sector": "Machine"}])

        with (
            patch.object(main, "load_krx_finder_listing", return_value=finder),
            patch.object(main, "load_krx_desc_listing", return_value=desc),
            patch.object(main.fdr, "StockListing") as stock_listing,
        ):
            listing = main.load_krx_listing(config)

        stock_listing.assert_not_called()
        self.assertEqual(listing.to_dict("records"), [{"Code": "060310", "Name": "3S", "Market": "KOSDAQ", "Sector": "Machine"}])

    def test_load_universe_uses_db_price_filter_when_finder_has_no_market_data(self):
        """finder fallback처럼 가격/거래대금 컬럼이 없으면 DB 캐시 기반 1차 필터를 사용합니다."""
        config = main.Config()
        listing = pd.DataFrame([{"Code": "060310", "Name": "3S", "Market": "KOSDAQ", "Sector": ""}])
        db_universe = pd.DataFrame([{"Code": "060310", "Name": "3S", "Sector": ""}])

        with (
            patch.object(main, "load_krx_listing", return_value=listing),
            patch.object(main, "load_universe_from_db_cache", return_value=db_universe) as load_db,
        ):
            universe = main.load_universe(config, max_symbols=5)

        load_db.assert_called_once_with(config, max_symbols=5)
        self.assertIs(universe, db_universe)

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

    def test_fetch_ohlcv_respects_explicit_end_date(self):
        """기준일 실행 시 FDR 종료일과 반환 데이터 상한을 함께 적용하는지 검증합니다."""
        fetched = self.frame("2024-01-01", 5).drop(columns=["Amount", "AmountSource"])

        with (
            patch.object(main, "read_cached_ohlcv", return_value=None),
            patch.object(main.fdr, "DataReader", return_value=fetched) as data_reader,
            patch.object(main, "write_cached_ohlcv"),
        ):
            result = main.fetch_ohlcv("005930", "2024-01-01", self.config, end_date="2024-01-03")

        data_reader.assert_called_once_with("005930", "2024-01-01", "2024-01-03")
        self.assertEqual(result.index.min(), pd.Timestamp("2024-01-01"))
        self.assertEqual(result.index.max(), pd.Timestamp("2024-01-03"))

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

    def test_rebuild_ohlcv_cache_meta_uses_valid_rows_from_db(self):
        """캐시 메타가 전달받은 부분 데이터가 아니라 DB의 유효 row 기준으로 재계산되는지 검증합니다."""

        class FakeCursor:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=()):
                self.calls.append((sql, params))

            def fetchone(self):
                return {"symbol_count": 1}

        class FakeConnection:
            def __init__(self):
                self.cursor_obj = FakeCursor()

            def cursor(self):
                return self.cursor_obj

        connection = FakeConnection()

        updated = main.rebuild_ohlcv_cache_meta(connection, self.config, symbol="005930")

        self.assertEqual(updated, 1)
        executed_sql = "\n".join(sql for sql, _ in connection.cursor_obj.calls)
        self.assertIn("COUNT(DISTINCT symbol)", executed_sql)
        self.assertIn("SELECT\n              symbol,\n              MIN(trade_date)", executed_sql)
        self.assertIn("open_price > 0", executed_sql)
        self.assertIn("ON DUPLICATE KEY UPDATE", executed_sql)
        self.assertEqual(connection.cursor_obj.calls[0][1], ("005930",))

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

    def test_collect_ohlcv_data_uses_target_date_for_start_dates(self):
        """기준일 실행 시 전체 수집 시작일이 현재일이 아니라 기준일에서 계산되는지 검증합니다."""
        config = main.Config(max_workers=1, collect_days=10, target_date="2024-06-10")
        universe = pd.DataFrame([{"Code": "000080", "Name": "A", "Sector": ""}])
        calls = []

        def fake_fetch(symbol, start_date, fetch_config):
            calls.append((symbol, start_date, fetch_config.target_date))
            return self.frame("2024-01-01", 2)

        with (
            patch.object(main, "get_target_date_cache_status", return_value={"is_ready": False, "reason": "cache miss"}),
            patch.object(main, "load_collection_universe", return_value=universe),
            patch.object(main, "fetch_ohlcv", side_effect=fake_fetch),
        ):
            main.collect_ohlcv_data(config)

        self.assertEqual(calls, [("KQ11", "2024-01-12", "2024-06-10"), ("000080", "2024-05-31", "2024-06-10")])

    def test_collect_ohlcv_data_skips_collection_when_target_cache_is_ready(self):
        """target_date 데이터가 DB에 충분하면 전체 수집 루프를 생략하는지 검증합니다."""
        config = main.Config(max_workers=1, collect_days=10, target_date="2024-06-10")
        cache_status = {
            "is_ready": True,
            "target_trade_date": date(2024, 6, 10),
            "index_ready": True,
            "eligible_stocks": 1300,
            "ready_stocks": 1250,
            "coverage_ratio": 1250 / 1300,
        }

        with (
            patch.object(main, "get_target_date_cache_status", return_value=cache_status),
            patch.object(main, "load_collection_universe") as load_collection_universe,
            patch.object(main, "fetch_ohlcv") as fetch_ohlcv,
        ):
            stats = main.collect_ohlcv_data(config)

        load_collection_universe.assert_not_called()
        fetch_ohlcv.assert_not_called()
        self.assertEqual(stats["collection_skipped_target_cache_ready"], 1)
        self.assertEqual(stats["status_cache_ready"], 1251)
        self.assertEqual(stats["latest_2024-06-10"], 1251)

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

    def test_run_sends_market_regime_and_scan_result_as_separate_messages(self):
        """시장 전망 메시지와 기본분석 결과 메시지가 별도 텔레그램 메시지로 전송되는지 검증합니다."""
        config = main.Config(max_workers=1)
        empty_universe = pd.DataFrame(columns=["Code", "Name", "Sector"])
        regime = SimpleNamespace(ok=True, is_bull_market=False, current=966.59, ma50=1110.54, kq_return_60=-3.21)

        with (
            patch.object(main, "setup_korean_font"),
            patch.object(main, "collect_ohlcv_data", return_value=main.Counter()),
            patch.object(main, "check_market_regime", return_value=regime),
            patch.object(main, "load_universe", return_value=empty_universe),
            patch.object(main, "save_reports", return_value=1),
            patch.object(main, "send_telegram_msg") as send_msg,
            patch.object(main, "run_vcp_pipeline"),
        ):
            exit_code = main.run(config, dry_run=True, max_symbols=2, no_charts=True)

        self.assertEqual(exit_code, 0)
        self.assertGreaterEqual(send_msg.call_count, 2)
        market_message = send_msg.call_args_list[0].args[0]
        scan_message = send_msg.call_args_list[1].args[0]
        self.assertIn("[시장국면]", market_message)
        self.assertIn("50일선", market_message)
        self.assertIn("[퀀트 스캔]", scan_message)
        self.assertIn("조건을 만족하는 종목이 없습니다.", scan_message)

    def test_format_market_regime_message_includes_bullish_status(self):
        """강세장일 때도 독립 시장 전망 메시지를 생성하는지 검증합니다."""
        config = main.Config(target_date="2024-06-10")
        regime = SimpleNamespace(ok=True, is_bull_market=True, current=1200.0, ma50=1100.0, kq_return_60=5.5)

        message = main.format_market_regime_message(regime, config)

        self.assertIn("2024년 6월 10일(월)", message)
        self.assertIn("[시장국면]", message)
        self.assertIn("50일선", message)
        self.assertIn("+5.50%", message)


if __name__ == "__main__":
    unittest.main()
