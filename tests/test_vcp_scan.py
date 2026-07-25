import math
import unittest
from unittest.mock import patch

import pandas as pd

import main
import vcp_scan


class VcpScanTest(unittest.TestCase):
    def test_prepare_vcp_records_drops_required_nan_values(self):
        """DB 필수 숫자 필드에 NaN이 있는 VCP 후보를 제외하는지 검증합니다."""
        candidates = pd.DataFrame(
            [
                {
                    "code": "000080",
                    "name": "A",
                    "current_price": 1000,
                    "drop_from_52w_high_pct": math.nan,
                    "avg_traded_value_20": 20_000_000_000,
                    "recent_swing_drops_pct": [],
                },
                {
                    "code": "000100",
                    "name": "B",
                    "current_price": 2000,
                    "drop_from_52w_high_pct": 5.5,
                    "avg_traded_value_20": 30_000_000_000,
                    "recent_swing_drops_pct": [math.nan, 3.1],
                },
            ]
        )

        records = vcp_scan.prepare_vcp_records(candidates)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["code"], "000100")
        self.assertEqual(records[0]["recent_swing_drops_pct"], [None, 3.1])

    def test_is_finite_number_rejects_nan_and_infinity(self):
        """유한 숫자 검사에서 NaN과 Infinity를 거르는지 검증합니다."""
        self.assertTrue(vcp_scan.is_finite_number(1.0))
        self.assertFalse(vcp_scan.is_finite_number(math.nan))
        self.assertFalse(vcp_scan.is_finite_number(math.inf))
        self.assertFalse(vcp_scan.is_finite_number("not-number"))

    def test_bulk_rows_to_ohlcv_groups_normalizes_rows(self):
        """MariaDB bulk OHLCV 행을 종목별 데이터프레임으로 정리하는지 검증합니다."""
        rows = [
            {
                "symbol": "000080",
                "trade_date": "2026-06-18",
                "open_price": 1000,
                "high_price": 1100,
                "low_price": 990,
                "close_price": 1050,
                "volume": 10,
                "amount": 10500,
                "amount_source": "source",
            },
            {
                "symbol": "000080",
                "trade_date": "2026-06-18",
                "open_price": 1000,
                "high_price": 1200,
                "low_price": 990,
                "close_price": 1150,
                "volume": 20,
                "amount": 23000,
                "amount_source": "source",
            },
            {
                "symbol": "000100",
                "trade_date": "2026-06-18",
                "open_price": 0,
                "high_price": 1200,
                "low_price": 990,
                "close_price": 1150,
                "volume": 20,
                "amount": 23000,
                "amount_source": "source",
            },
        ]

        groups = vcp_scan.bulk_rows_to_ohlcv_groups(rows)

        self.assertEqual(list(groups), ["000080"])
        self.assertEqual(len(groups["000080"]), 1)
        self.assertEqual(float(groups["000080"]["Close"].iloc[0]), 1150.0)

    def test_has_contracting_swings_uses_configurable_ratio(self):
        """수축폭 감소 기준이 허용 비율 설정을 따르는지 검증합니다."""
        self.assertTrue(vcp_scan.has_contracting_swings([14.0, 4.5]))
        self.assertTrue(vcp_scan.has_contracting_swings([18.0, 8.0, 4.0]))
        self.assertFalse(vcp_scan.has_contracting_swings([9.0]))
        self.assertTrue(vcp_scan.has_contracting_swings([7.0, 12.0, 4.0]))
        self.assertTrue(vcp_scan.has_contracting_swings([18.0, 6.0, 5.0]))
        self.assertFalse(vcp_scan.has_contracting_swings([18.0, 6.0, 5.0], max_final_contraction_pct=5.0))
        self.assertFalse(vcp_scan.has_contracting_swings([10.0, 11.0], max_contraction_ratio=1.15))

    def test_calculate_swing_segments_uses_high_low_and_separate_peak_windows(self):
        """각 고점 구간의 High-Low 수축폭과 구간 거래량을 계산하는지 검증합니다."""
        index = pd.date_range("2026-01-01", periods=120)
        df = pd.DataFrame(
            {
                "Open": [112.0] * 120,
                "High": [113.0] * 120,
                "Low": [111.0] * 120,
                "Close": [112.0] * 120,
                "Volume": [1000.0] * 120,
            },
            index=index,
        )
        df.loc[index[10], ["High", "Close"]] = [120.0, 118.0]
        df.loc[index[15], ["Low", "Close"]] = [100.0, 102.0]
        df.loc[index[30], ["High", "Close"]] = [115.0, 113.0]
        df.loc[index[35], ["Low", "Close"]] = [105.0, 107.0]

        with patch.object(vcp_scan, "find_peaks", return_value=[10, 30]):
            segments = vcp_scan.calculate_swing_segments(df)

        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["peak_date"], index[10])
        self.assertEqual(segments[0]["trough_date"], index[15])
        self.assertAlmostEqual(float(segments[0]["drop_pct"]), 16.67, places=2)
        self.assertEqual(float(segments[0]["avg_volume"]), 1000.0)

    def test_segment_volume_must_contract_with_price(self):
        """마지막 수축 구간 거래량이 첫 구간보다 감소해야 하는지 검증합니다."""
        declining = [
            {"drop_pct": 15.0, "avg_volume": 1000.0},
            {"drop_pct": 9.0, "avg_volume": 800.0},
            {"drop_pct": 4.0, "avg_volume": 600.0},
        ]
        expanding = [
            {"drop_pct": 15.0, "avg_volume": 1000.0},
            {"drop_pct": 9.0, "avg_volume": 800.0},
            {"drop_pct": 4.0, "avg_volume": 1100.0},
        ]
        insufficient_decline = [
            {"drop_pct": 15.0, "avg_volume": 1000.0},
            {"drop_pct": 9.0, "avg_volume": 1100.0},
            {"drop_pct": 4.0, "avg_volume": 1200.0},
            {"drop_pct": 2.0, "avg_volume": 850.0},
        ]

        self.assertTrue(vcp_scan.has_contracting_segment_volume(declining))
        self.assertFalse(vcp_scan.has_contracting_segment_volume(expanding))
        self.assertFalse(vcp_scan.has_contracting_segment_volume(insufficient_decline))

    def test_vcp_score_rewards_mature_tight_structure(self):
        """3T, 타이트한 최종 수축, 강한 거래량 감소가 더 높은 점수를 받는지 검증합니다."""
        mature_score = vcp_scan.calculate_vcp_score(
            contraction_count=3,
            recent_swing_drops=[18.0, 9.0, 4.0],
            segment_volume_ratio=0.60,
            volume_dry_up_ratio=0.60,
            pivot_gap=0.02,
            ma_alignment=True,
            pocket_pivot_count=1,
        )
        developing_score = vcp_scan.calculate_vcp_score(
            contraction_count=2,
            recent_swing_drops=[12.0, 9.0],
            segment_volume_ratio=0.88,
            volume_dry_up_ratio=0.36,
            pivot_gap=0.12,
            ma_alignment=False,
            pocket_pivot_count=0,
        )

        self.assertGreater(mature_score, developing_score)
        self.assertEqual(vcp_scan.classify_vcp_quality(mature_score, 3), "A")
        self.assertEqual(vcp_scan.classify_vcp_quality(developing_score, 2), "C")

    def test_volume_dry_up_requires_peak_to_recent_drop_and_declining_phase(self):
        """거래량 dry-up 기준이 피크 대비 70% 이상 감소와 감소 구간을 함께 요구하는지 검증합니다."""
        volume = pd.Series([1000] * 20 + [900, 800, 700, 600, 500, 400, 300, 250, 220, 200] + [180] * 20)
        metrics = vcp_scan.calculate_volume_dry_up(volume, lookback_days=60, window=5)

        self.assertTrue(vcp_scan.has_volume_dry_up(volume, lookback_days=60, window=5, min_dry_up_ratio=0.70))
        self.assertGreaterEqual(float(metrics["dry_up_ratio"]), 0.70)
        self.assertTrue(metrics["has_declining_sequence"])

    def test_find_pocket_pivot_points_marks_high_volume_up_days(self):
        """Pocket Pivot 산출이 전일 대비 상승과 이전 거래량 고점 돌파를 함께 확인하는지 검증합니다."""
        df = pd.DataFrame(
            {
                "Open": [10, 10, 10, 10, 10, 10],
                "High": [11, 11, 11, 11, 11, 12],
                "Low": [9, 9, 9, 9, 9, 9],
                "Close": [10, 10.5, 10.2, 10.6, 10.4, 11.0],
                "Volume": [100, 120, 110, 105, 125, 200],
            },
            index=pd.date_range("2026-01-01", periods=6),
        )

        pivots = vcp_scan.find_pocket_pivot_points(df, days=3, volume_window=3)

        self.assertEqual(len(pivots), 1)
        self.assertEqual(pivots.index[0], pd.Timestamp("2026-01-06"))

    def test_pocket_pivot_compares_only_with_prior_down_day_volume(self):
        """Pocket Pivot 거래량 기준에서 이전 상승일 거래량은 제외하는지 검증합니다."""
        df = pd.DataFrame(
            {
                "Open": [10] * 6,
                "High": [11, 12, 11, 11, 11, 12],
                "Low": [9] * 6,
                "Close": [10.0, 10.8, 10.4, 10.5, 10.3, 11.0],
                "Volume": [100, 500, 120, 130, 110, 200],
            },
            index=pd.date_range("2026-02-01", periods=6),
        )

        pivots = vcp_scan.find_pocket_pivot_points(df, days=1, volume_window=5)

        self.assertEqual(list(pivots.index), [pd.Timestamp("2026-02-06")])

    def test_get_clean_universe_uses_krx_finder_fallback(self):
        """FDR KRX 목록이 실패하면 VCP 유니버스도 KRX finder fallback을 사용합니다."""
        finder = pd.DataFrame(
            [
                {"Code": "060310", "Name": "3S", "Market": "KOSDAQ"},
                {"Code": "095570", "Name": "AJ네트웍스", "Market": "KOSPI"},
                {"Code": "000001", "Name": "테스트스팩", "Market": "KOSDAQ"},
                {"Code": "000002", "Name": "코넥스", "Market": "KONEX"},
            ]
        )

        with (
            patch.object(vcp_scan.fdr, "StockListing", side_effect=ValueError("LOGOUT")),
            patch.object(main, "load_krx_finder_listing", return_value=finder),
        ):
            universe = vcp_scan.get_clean_universe()

        self.assertEqual(universe.to_dict("records"), [
            {"Code": "060310", "Name": "3S"},
            {"Code": "095570", "Name": "AJ네트웍스"},
        ])

    def test_run_vcp_engine_passes_target_date_to_bulk_cache_loader(self):
        """VCP 기준일이 bulk 캐시 조회 종료일로 전달되는지 검증합니다."""
        universe = pd.DataFrame(columns=["Code", "Name"])

        with (
            patch.object(vcp_scan, "get_clean_universe", return_value=universe),
            patch.object(vcp_scan, "load_project_cache_ohlcv_bulk", return_value={}) as load_bulk,
        ):
            result = vcp_scan.run_vcp_engine(target_date="2024-06-10", save_charts=False)

        self.assertTrue(result.empty)
        load_bulk.assert_called_once()
        self.assertEqual(load_bulk.call_args.args[1].date(), pd.Timestamp("2023-05-07").date())
        self.assertEqual(load_bulk.call_args.kwargs["end_date"].date(), pd.Timestamp("2024-06-10").date())

    def test_validate_vcp_criteria_rejects_invalid_windows(self):
        """VCP 기준값 검증이 잘못된 기간 설정을 차단하는지 확인합니다."""
        with self.assertRaises(ValueError):
            vcp_scan.validate_vcp_criteria(vcp_scan.VcpCriteria(recent_high_window=0))

    def test_resolve_symbol_name_uses_lookup_when_name_is_code(self):
        """유니버스 이름이 코드와 같으면 별도 조회 이름을 사용하는지 검증합니다."""
        original = vcp_scan.get_symbol_name
        try:
            vcp_scan.get_symbol_name = lambda symbol: "하이트진로"
            self.assertEqual(vcp_scan.resolve_symbol_name("000080", "000080"), "하이트진로")
            self.assertEqual(vcp_scan.resolve_symbol_name("000080", "하이트진로"), "하이트진로")
        finally:
            vcp_scan.get_symbol_name = original

    def test_format_vcp_message_includes_dynamic_interpretation(self):
        """VCP 텔레그램 메시지가 종목명, 기준 기간, 다양한 해석 문구를 포함하는지 검증합니다."""
        record = pd.Series(
            {
                "name": "하이트진로",
                "code": "000080",
                "current_price": 15490,
                "drop_from_52w_high_pct": 16.2,
                "vcp_stage": "3T breakout-ready",
                "vcp_score": 82,
                "quality_grade": "B",
                "recent_swing_drops_pct": [12.0, 8.0, 7.0],
                "contraction_ratio": 0.875,
                "pocket_pivot_count_14d": 2,
                "avg_traded_value_20": 25_000_000_000,
                "pivot_gap_pct": 12.3,
                "price_vs_fast_ma_pct": 4.2,
                "price_vs_mid_ma_pct": 8.4,
                "recent_high_window": 20,
                "pocket_pivot_days": 14,
            }
        )

        message = main.format_vcp_message(record, rank_no=1)

        self.assertIn("하이트진로(000080)", message)
        self.assertIn("[VCP 체크]", message)
        self.assertIn("12.0% → 8.0% → 7.0%", message)
        self.assertIn("VCP 피벗까지: 12.30%", message)
        self.assertIn("B등급 / 82점", message)
        self.assertIn("주의:", message)
        self.assertIn("수급:", message)

    def test_config_loads_vcp_environment_controls(self):
        """환경변수로 VCP 지표 기준을 제어할 수 있는지 검증합니다."""
        keys = (
            "VCP_MIN_VOLUME_DRY_UP_RATIO",
            "VCP_POCKET_PIVOT_DAYS",
            "VCP_MAX_SEGMENT_VOLUME_RATIO",
            "VCP_MIN_SEGMENT_VOLUME_DECLINE_FRACTION",
            "VCP_MIN_SCORE",
            "VCP_REQUIRE_MA_ALIGNMENT",
            "VCP_SWING_PEAK_DISTANCE",
        )
        original = {key: main.os.environ.get(key) for key in keys}
        try:
            main.os.environ["VCP_MIN_VOLUME_DRY_UP_RATIO"] = "0.4"
            main.os.environ["VCP_POCKET_PIVOT_DAYS"] = "30"
            main.os.environ["VCP_MAX_SEGMENT_VOLUME_RATIO"] = "0.8"
            main.os.environ["VCP_MIN_SEGMENT_VOLUME_DECLINE_FRACTION"] = "0.75"
            main.os.environ["VCP_MIN_SCORE"] = "72"
            main.os.environ["VCP_REQUIRE_MA_ALIGNMENT"] = "true"
            main.os.environ["VCP_SWING_PEAK_DISTANCE"] = "8"
            config = main.Config()
            self.assertEqual(config.vcp_min_volume_dry_up_ratio, 0.4)
            self.assertEqual(config.vcp_pocket_pivot_days, 30)
            self.assertEqual(config.vcp_max_segment_volume_ratio, 0.8)
            self.assertEqual(config.vcp_min_segment_volume_decline_fraction, 0.75)
            self.assertEqual(config.vcp_min_score, 72)
            self.assertTrue(config.vcp_require_ma_alignment)
            self.assertEqual(config.vcp_swing_peak_distance, 8)
        finally:
            for key, value in original.items():
                if value is None:
                    main.os.environ.pop(key, None)
                else:
                    main.os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
