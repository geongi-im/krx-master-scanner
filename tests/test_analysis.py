import unittest
from unittest.mock import patch

import pandas as pd

import analysis
import main


class AnalysisSniperTest(unittest.TestCase):
    def test_calculate_supply_zone_finds_heaviest_price_band(self):
        """가격대별 거래량이 가장 많은 구간을 매물대로 잡는지 검증합니다."""
        df = pd.DataFrame(
            {
                "High": [101.0] * 90 + [151.0] * 30,
                "Low": [99.0] * 90 + [149.0] * 30,
                "Close": [100.0] * 90 + [150.0] * 30,
                "Volume": [10] * 90 + [1000] * 30,
            },
            index=pd.date_range("2026-01-01", periods=120),
        )

        zone = analysis.calculate_supply_zone(df)

        self.assertIsNotNone(zone)
        self.assertAlmostEqual(float(zone["zone_price"]), 150.0, places=6)
        self.assertLessEqual(float(zone["zone_low"]), 150.0)
        self.assertGreaterEqual(float(zone["zone_high"]), 150.0)
        self.assertGreater(float(zone["volume_share"]), 0.9)

    def test_calculate_supply_zone_requires_enough_rows(self):
        """행 수가 부족하면 매물대를 계산하지 않는지 검증합니다."""
        df = pd.DataFrame(
            {
                "High": [101.0] * 10,
                "Low": [99.0] * 10,
                "Close": [100.0] * 10,
                "Volume": [10] * 10,
            }
        )

        self.assertIsNone(analysis.calculate_supply_zone(df))

    def test_classify_supply_zone_status_transitions(self):
        """현재가 위치에 따라 돌파, 돌파 시도, 임박, 대기를 구분하는지 검증합니다."""
        self.assertEqual(analysis.classify_supply_zone_status(160, 145, 155), "돌파")
        self.assertEqual(analysis.classify_supply_zone_status(150, 145, 155), "돌파 시도")
        self.assertEqual(analysis.classify_supply_zone_status(141, 145, 155), "임박")
        self.assertEqual(analysis.classify_supply_zone_status(120, 145, 155), "대기")

    def test_calculate_sniper_score_bands(self):
        """스나이퍼 점수가 신호 강도에 따라 적극/관망으로 나뉘는지 검증합니다."""
        strong_score, strong_verdict = analysis.calculate_sniper_score(
            supply_zone_status="돌파",
            final_contraction_pct=4.0,
            contraction_decreasing=True,
            rs_score=12.0,
            vol_ratio=1.8,
            stars=5,
            is_nr3=True,
            is_kulamegi_htf=True,
        )
        weak_score, weak_verdict = analysis.calculate_sniper_score(
            supply_zone_status="대기",
            final_contraction_pct=None,
            contraction_decreasing=False,
            rs_score=-5.0,
            vol_ratio=1.0,
            stars=3,
            is_nr3=False,
            is_kulamegi_htf=False,
        )

        self.assertGreaterEqual(strong_score, 70)
        self.assertLessEqual(strong_score, 100)
        self.assertEqual(strong_verdict, "적극")
        self.assertLess(weak_score, 50)
        self.assertEqual(weak_verdict, "관망")

    def test_analyze_stock_found_includes_sniper_fields(self):
        """조건을 통과한 종목의 분석 결과에 매물대와 스나이퍼 판독이 채워지는지 검증합니다."""
        rows = 460
        index = pd.date_range("2024-06-01", periods=rows)
        close = pd.Series([100 * (1.004**i) for i in range(rows)], index=index)
        df = pd.DataFrame(
            {
                "Open": close * 0.995,
                "High": close * 1.01,
                "Low": close * 0.985,
                "Close": close,
                "Volume": [5_000_000] * rows,
            },
            index=index,
        )
        df["Amount"] = df["Close"] * df["Volume"]

        config = main.Config()
        with patch.object(main, "fetch_ohlcv", return_value=df):
            outcome = analysis.analyze_stock(("000001", "테스트종목", "테스트섹터"), 0.0, config)

        self.assertEqual(outcome.status, "found")
        stock = outcome.result
        self.assertIsNotNone(stock.supply_zone_price)
        self.assertIn(stock.supply_zone_status, {"돌파", "돌파 시도", "임박", "대기"})
        self.assertTrue(stock.sniper_reading.startswith("["))
        self.assertIn("스나이퍼 점수", stock.sniper_reading)
        self.assertGreaterEqual(stock.sniper_score, 0)
        self.assertLessEqual(stock.sniper_score, 100)
        self.assertIn(stock.sniper_verdict, {"적극", "중립", "관망"})

    def test_build_message_includes_sniper_reading(self):
        """텔레그램 메시지에 스나이퍼 판독과 매물대 구간이 포함되는지 검증합니다."""
        stock = analysis.ScanResult(
            stars=4,
            star_icon="⭐⭐⭐⭐",
            name="KT&G",
            code="033780",
            sector="담배",
            curr_p=178000.0,
            breakout_pct=1.2,
            pole_ratio=1.35,
            flag_depth=0.08,
            vol_ratio=0.7,
            curr_adr=2.1,
            avg_turnover=25_000_000_000.0,
            nr3_status="✅ NR3 패턴 발생",
            vcp_status="✅ VCP 패턴 형성 중 (수축폭 -14.8% → -8.9% → -4.4%)",
            kulamegi_htf_status="✅ 쿨라메기 HTF 셋업",
            fib_summary="피보나치 레벨: 고점 상회",
            rs_status="✅ 초과 상승",
            rs_score=12.3,
            stock_return_60=20.0,
            entry_p=180000.0,
            target_p=198000.0,
            stop_p=172000.0,
            ref_date="07/01",
            material_info="🤝 [수급 현황] 정보 없음",
            quant_scenario="⚡ 시나리오",
            vcp_contraction_pct=4.4,
            vcp_contraction_trend="-14.8% → -8.9% → -4.4%",
            supply_zone_price=172664.0,
            supply_zone_low=171000.0,
            supply_zone_high=174000.0,
            supply_zone_status="돌파",
            sniper_score=78,
            sniper_verdict="적극",
            sniper_reading="[돌파] 매물대(172,664원) 돌파 / 스나이퍼 점수 78점 (적극)",
        )

        message = analysis.build_message(stock)

        self.assertIn("🎯 [스나이퍼 판독]", message)
        self.assertIn("매물대(172,664원)", message)
        self.assertIn("매물대 구간: 171,000 ~ 174,000원", message)
        self.assertIn("수축폭 -14.8% → -8.9% → -4.4%", message)


if __name__ == "__main__":
    unittest.main()
