from __future__ import annotations

import logging
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import matplotlib as mpl
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
import requests
from bs4 import BeautifulSoup
from matplotlib.ticker import FuncFormatter

logger = logging.getLogger("krx-master-scanner")


@dataclass
class MarketRegime:
    ok: bool
    is_bull_market: bool
    current: float
    ma50: float
    kq_return_60: float
    error: str | None = None


@dataclass
class ScanResult:
    stars: int
    star_icon: str
    name: str
    code: str
    sector: str
    curr_p: float
    breakout_pct: float
    pole_ratio: float
    flag_depth: float
    vol_ratio: float
    curr_adr: float
    avg_turnover: float
    nr3_status: str
    vcp_status: str
    kulamegi_htf_status: str
    fib_summary: str
    rs_status: str
    rs_score: float
    stock_return_60: float
    entry_p: float
    target_p: float
    stop_p: float
    ref_date: str
    material_info: str = ""
    quant_scenario: str = ""


@dataclass
class AnalysisOutcome:
    status: str  # found | skipped | failed
    code: str
    name: str
    reason: str
    result: ScanResult | None = None
    error: str | None = None


def runtime() -> Any:
    """현재 실행 중인 main 런타임 모듈을 반환합니다.

    Returns:
        `fetch_ohlcv` 등 실행 함수가 들어 있는 main 모듈 객체입니다.
    """
    entrypoint = sys.modules.get("__main__")
    if entrypoint is not None and hasattr(entrypoint, "fetch_ohlcv"):
        return entrypoint

    loaded_main = sys.modules.get("main")
    if loaded_main is not None:
        return loaded_main

    import main as project_main

    return project_main


def check_market_regime(config: Any) -> MarketRegime:
    """코스닥 지수 기준으로 현재 시장 국면을 판단합니다.

    Args:
        config: OHLCV 조회에 사용할 실행 설정입니다.

    Returns:
        시장 데이터 조회 성공 여부와 50일선 기준 강세장 여부입니다.
    """
    project_main = runtime()
    start_date = (datetime.now(project_main.KST) - timedelta(days=150)).strftime("%Y-%m-%d")
    try:
        df = project_main.fetch_ohlcv("KQ11", start_date, config)
        if len(df) < 60:
            raise ValueError(f"코스닥 지수 데이터 부족: rows={len(df)}")
        ma50 = float(df["Close"].rolling(50).mean().iloc[-1])
        current = float(df["Close"].iloc[-1])
        kq_return_60 = float((df["Close"].iloc[-1] / df["Close"].iloc[-60] - 1) * 100)
        return MarketRegime(True, current >= ma50, current, ma50, kq_return_60)
    except Exception as exc:  # noqa: BLE001
        logger.exception("시장 국면 조회 실패")
        return MarketRegime(False, False, 0.0, 0.0, 0.0, str(exc))


def generate_quant_scenario(
    curr_p: float,
    flag_high: float,
    high52: float,
    vol_ratio: float,
    flag_depth: float,
    ref_open: float,
    ref_date: str,
) -> tuple[str, float, float, float]:
    """후보 종목의 매수, 목표, 손절 시나리오를 계산합니다.

    Args:
        curr_p: 현재가입니다.
        flag_high: 플래그 구간 상단 가격입니다.
        high52: 52주 최고가입니다.
        vol_ratio: 최근 거래량 비율입니다.
        flag_depth: 플래그 조정 깊이입니다.
        ref_open: 기준 봉의 시가입니다.
        ref_date: 기준 날짜 문자열입니다.

    Returns:
        시나리오 설명, 진입가, 목표가, 손절가입니다.
    """
    entry_price = flag_high
    target_1 = entry_price * 1.10
    target_2_str = f"{high52:,.0f}원 (52주 매물대)" if high52 > target_1 else "신고가 (추세 홀딩)"
    stop_price = ref_open

    potential_profit = target_1 - entry_price
    potential_loss = entry_price - stop_price
    if potential_loss > 0:
        rr_ratio = potential_profit / potential_loss
        rr_str = f" | ⚖️ 손익비 1 : {rr_ratio:.2f}"
    else:
        rr_str = " | ⚖️ 손익비 계산 불가 (기준봉 시가가 매수가보다 높음)"

    vol_desc = "거래량이 바짝 마르며(VCP)" if vol_ratio < 0.8 else "안정적인 거래량을 유지하며"
    depth_desc = f"{flag_depth * 100:.1f}%의 타이트한 수렴" if flag_depth < 0.15 else "변동성 축소"

    text = (
        "⚡ [자체 퀀트 타점 & 시나리오]\n"
        f"- {vol_desc} {depth_desc} 패턴을 완성 중이므로, 상단 돌파 시 강한 시세 분출이 기대됩니다.\n"
        f"- [트레이딩 플랜] 현재가: {curr_p:,.0f}원 | 매수가: {entry_price:,.0f}원 (돌파 시) | "
        f"1차 목표가: {target_1:,.0f}원 (+10%) | 2차 목표가: {target_2_str} | "
        f"손절가: {stop_price:,.0f}원 ({ref_date} 기준봉 이탈 시){rr_str} | 타임스탑: 5일\n"
        "- 주가가 상승할 때 환희에 빠지지 말고, 수익 구간에서는 이익을 현금화하는 규칙을 우선합니다."
    )
    return text, entry_price, target_1, stop_price


def get_stock_details(code: str, name: str, config: Any) -> str:
    """후보 종목의 뉴스와 수급 정보를 조회해 요약 문자열을 만듭니다.

    Args:
        code: 종목 코드입니다.
        name: 종목명입니다.
        config: 외부 요청 타임아웃 설정입니다.

    Returns:
        텔레그램 메시지에 포함할 재료 요약 문자열입니다.
    """
    project_main = runtime()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"}
    supply_summary = "🤝 [수급 현황] 정보 없음"
    today = datetime.now(project_main.KST).replace(tzinfo=None)
    one_month_ago = today - timedelta(days=30)

    try:
        frgn_url = f"https://finance.naver.com/item/frgn.naver?code={code}"
        res_frgn = requests.get(frgn_url, headers=headers, timeout=config.request_timeout)
        res_frgn.raise_for_status()
        soup_frgn = BeautifulSoup(res_frgn.text, "html.parser")

        inst_sum, frgn_sum, count = 0, 0, 0
        for row in soup_frgn.select("table.type2 tr[onmouseout]"):
            tds = row.select("td")
            if len(tds) >= 7:
                try:
                    inst_sum += int(tds[5].text.strip().replace(",", ""))
                    frgn_sum += int(tds[6].text.strip().replace(",", ""))
                    count += 1
                except ValueError:
                    continue
            if count >= 3:
                break

        if count > 0:
            inst_str = f"+{inst_sum:,}" if inst_sum > 0 else f"{inst_sum:,}"
            frgn_str = f"+{frgn_sum:,}" if frgn_sum > 0 else f"{frgn_sum:,}"
            supply_summary = f"🤝 [최근 3일 수급] 기관: {inst_str}주 | 외국인: {frgn_str}주"
    except Exception as exc:  # noqa: BLE001
        logger.info("수급 조회 실패: %s %s", code, exc)

    valid_news: list[str] = []
    try:
        news_url = f"https://finance.naver.com/item/news_news.naver?code={code}"
        res_news = requests.get(news_url, headers=headers, timeout=config.request_timeout)
        res_news.raise_for_status()
        soup_news = BeautifulSoup(res_news.text, "html.parser")
        for row in soup_news.select("table.type5 tbody tr"):
            title_tag = row.select_one(".tit")
            date_tag = row.select_one(".date")
            if title_tag and date_tag:
                try:
                    news_date = datetime.strptime(date_tag.text.strip(), "%Y.%m.%d %H:%M")
                    if news_date >= one_month_ago:
                        valid_news.append(f"[네이버] {title_tag.text.strip()}")
                except ValueError:
                    continue
    except Exception as exc:  # noqa: BLE001
        logger.info("네이버 뉴스 조회 실패: %s %s", code, exc)

    try:
        encoded_query = quote(f'"{name} 특징주" OR "{name} 주식"')
        google_rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"
        res_google = requests.get(google_rss_url, timeout=config.request_timeout)
        res_google.raise_for_status()
        root = ET.fromstring(res_google.text)
        for item in root.findall(".//item")[:3]:
            title_node = item.find("title")
            if title_node is not None and title_node.text:
                clean_title = title_node.text.rsplit(" - ", 1)[0]
                valid_news.append(f"[구글] {clean_title}")
    except Exception as exc:  # noqa: BLE001
        logger.info("구글 뉴스 조회 실패: %s %s", code, exc)

    if valid_news:
        unique_news = list(dict.fromkeys(valid_news))[:5]
        news_summary = "📰 [최신 핵심 뉴스]\n" + "\n".join(f"- {news}" for news in unique_news)
    else:
        news_summary = "📰 최근 1달 내 뚜렷한 관련 뉴스/이슈 없음"

    return f"{supply_summary}\n────────────────\n{news_summary}"


def analyze_stock(stock_info: tuple[str, str, str], kq_return_60: float, config: Any) -> AnalysisOutcome:
    """종목 하나에 대해 종합분석 조건을 평가합니다.

    Args:
        stock_info: 종목 코드, 종목명, 섹터 튜플입니다.
        kq_return_60: 코스닥 60거래일 수익률입니다.
        config: 필터 기준과 OHLCV 조회 설정입니다.

    Returns:
        분석 성공, 스킵, 실패 상태와 후보 결과입니다.
    """
    project_main = runtime()
    code, name, sector = stock_info
    start_date = (datetime.now(project_main.KST) - timedelta(days=600)).strftime("%Y-%m-%d")

    try:
        df = project_main.fetch_ohlcv(code, start_date, config)
        if len(df) < 260:
            return AnalysisOutcome("skipped", code, name, "데이터 260행 미만")

        df = df.copy()
        for window in (10, 20, 50, 150, 200):
            df[f"MA{window}"] = df["Close"].rolling(window=window).mean()
        df["High52"] = df["High"].rolling(window=250).max()
        df["Low52"] = df["Low"].rolling(window=250).min()
        df["AvgVol50"] = df["Volume"].rolling(window=50).mean()
        if "Amount" not in df.columns:
            df["Amount"] = df["Close"] * df["Volume"]
        else:
            df["Amount"] = df["Amount"].fillna(df["Close"] * df["Volume"])
        df["AvgAmount50"] = df["Amount"].rolling(window=50).mean()
        df["ADR_Day"] = ((df["High"] - df["Low"]) / df["Close"]) * 100

        clean_df = df.dropna()
        if len(clean_df) < 60:
            return AnalysisOutcome("skipped", code, name, "클린 데이터 60행 미만")

        today = clean_df.iloc[-1]
        past_20 = clean_df.iloc[-21]
        prev_day = clean_df.iloc[-2]

        curr_p = float(today["Close"])
        high52 = float(today["High52"])
        low52 = float(today["Low52"])
        curr_adr = float(clean_df["ADR_Day"].iloc[-20:].mean())
        avg_turnover = float(today["AvgAmount50"])

        skip_checks = [
            (curr_p < config.first_pass_min_close, "현재가 기준 미달"),
            (avg_turnover < config.min_avg_turnover, "평균 거래대금 기준 미달"),
            (curr_adr < config.min_adr, "ADR 기준 미달"),
            (curr_p <= today["MA150"] or curr_p <= today["MA200"], "장기 이평선 하회"),
            (today["MA150"] <= today["MA200"], "MA150 <= MA200"),
            (today["MA200"] < past_20["MA200"], "MA200 하락"),
            (today["MA50"] <= today["MA150"] or today["MA50"] <= today["MA200"], "MA50 정배열 실패"),
            (curr_p <= today["MA50"], "현재가 MA50 하회"),
            (curr_p < low52 * 1.30, "52주 저점 대비 상승폭 부족"),
            (curr_p < high52 * 0.75, "52주 고점 대비 위치 부족"),
        ]
        for condition, reason in skip_checks:
            if condition:
                return AnalysisOutcome("skipped", code, name, reason)

        recent_60 = clean_df.iloc[-61:-1]
        pole_low = float(recent_60["Low"].min())
        pole_high = float(recent_60["High"].max())
        pole_ratio = pole_high / pole_low if pole_low else 0.0
        if pole_ratio < 1.20:
            return AnalysisOutcome("skipped", code, name, "60일 깃대 상승률 부족")

        recent_15 = clean_df.iloc[-16:-1]
        flag_high = float(recent_15["High"].max())
        flag_low = float(recent_15["Low"].min())
        flag_depth = (flag_high - flag_low) / flag_low if flag_low else 999.0
        if flag_depth > 0.25:
            return AnalysisOutcome("skipped", code, name, "15일 수렴폭 과대")

        yang_bongs = recent_15[recent_15["Close"] > recent_15["Open"]]
        if not yang_bongs.empty:
            ref_candle = yang_bongs.loc[yang_bongs["Volume"].idxmax()]
            ref_open = float(ref_candle["Open"])
            ref_date = ref_candle.name.strftime("%m/%d")
        else:
            ref_open = flag_low
            ref_date = "최근 저점"

        avg_vol50 = float(today["AvgVol50"])
        if avg_vol50 <= 0:
            return AnalysisOutcome("skipped", code, name, "평균 거래량 0")
        vol_ratio = float(today["Volume"] / avg_vol50)

        if not (flag_high * 0.95 <= curr_p <= flag_high * 1.03):
            return AnalysisOutcome("skipped", code, name, "돌파 가격 범위 이탈")

        entry_price = flag_high
        if ref_open >= entry_price:
            return AnalysisOutcome("skipped", code, name, "손절가 >= 매수가")
        stop_loss_pct = (entry_price - ref_open) / entry_price
        if stop_loss_pct > 0.15:
            return AnalysisOutcome("skipped", code, name, "손절폭 15% 초과")

        stars = 5 if pole_ratio >= 1.50 and flag_depth <= 0.12 and vol_ratio >= 1.5 else (
            4 if pole_ratio >= 1.30 and flag_depth <= 0.15 and vol_ratio >= 1.2 else 3
        )
        star_icon = "⭐" * stars
        breakout_pct = ((curr_p - flag_high) / flag_high) * 100

        stock_return_60 = float((clean_df["Close"].iloc[-1] / clean_df["Close"].iloc[-60] - 1) * 100)
        rs_score = stock_return_60 - kq_return_60
        rs_status = (
            f"✅ 초과 상승 (RS 스코어: {rs_score:+.1f}점 / 지수대비 {rs_score:+.1f}%)"
            if rs_score > 0
            else f"❌ 지수 하회 (RS 스코어: {rs_score:+.1f}점 / 지수대비 {rs_score:+.1f}%)"
        )

        range_today = float(today["High"] - today["Low"])
        range_prev1 = float(clean_df.iloc[-2]["High"] - clean_df.iloc[-2]["Low"])
        range_prev2 = float(clean_df.iloc[-3]["High"] - clean_df.iloc[-3]["Low"])
        is_nr3 = range_today < range_prev1 and range_today < range_prev2
        nr3_status = "✅ NR3 패턴 발생" if is_nr3 else "❌ NR3 패턴 미발생"

        is_vcp = vol_ratio < 0.8 and flag_depth < 0.15
        vcp_status = "✅ VCP 패턴 형성 중" if is_vcp else "❌ VCP 패턴 미형성"

        ma50_up = today["MA50"] > prev_day["MA50"]
        ma150_up = today["MA150"] > prev_day["MA150"]
        ma200_up = today["MA200"] > prev_day["MA200"]
        is_kulamegi_htf = bool(ma50_up and ma150_up and ma200_up and today["MA50"] > today["MA150"] > today["MA200"] and curr_p > today["MA50"])
        kulamegi_htf_status = "✅ 쿨라메기 HTF 셋업" if is_kulamegi_htf else "❌ 쿨라메기 HTF 셋업 미발생"

        fib_summary = "피보나치 레벨: 계산 불가"
        if pole_high > pole_low:
            diff = pole_high - pole_low
            fib_levels = {
                "23.6%": pole_high - diff * 0.236,
                "38.2%": pole_high - diff * 0.382,
                "50.0%": pole_high - diff * 0.500,
                "61.8%": pole_high - diff * 0.618,
                "78.6%": pole_high - diff * 0.786,
                "100.0%": pole_low,
            }
            current_fib_desc = "해당 없음"
            if curr_p >= pole_high:
                current_fib_desc = f"고점 ({pole_high:,.0f}원) 상회"
            elif curr_p <= pole_low:
                current_fib_desc = f"저점 ({pole_low:,.0f}원) 하회"
            else:
                sorted_levels = sorted((value, label) for label, value in fib_levels.items())[::-1]
                for idx in range(len(sorted_levels) - 1):
                    upper_val, _ = sorted_levels[idx]
                    lower_val, lower_name = sorted_levels[idx + 1]
                    if lower_val <= curr_p <= upper_val:
                        current_fib_desc = f"{lower_name} ({lower_val:,.0f}원) ~ {upper_val:,.0f}원 구간"
                        break
            fib_summary = f"피보나치 레벨: {current_fib_desc}"

        quant_scenario, entry_p, target_p, stop_p = generate_quant_scenario(
            curr_p=curr_p,
            flag_high=flag_high,
            high52=high52,
            vol_ratio=vol_ratio,
            flag_depth=flag_depth,
            ref_open=ref_open,
            ref_date=ref_date,
        )

        result = ScanResult(
            stars=stars,
            star_icon=star_icon,
            name=name,
            code=code,
            sector=sector,
            curr_p=curr_p,
            breakout_pct=breakout_pct,
            pole_ratio=pole_ratio,
            flag_depth=flag_depth,
            vol_ratio=vol_ratio,
            curr_adr=curr_adr,
            avg_turnover=avg_turnover,
            nr3_status=nr3_status,
            vcp_status=vcp_status,
            kulamegi_htf_status=kulamegi_htf_status,
            fib_summary=fib_summary,
            rs_status=rs_status,
            rs_score=rs_score,
            stock_return_60=stock_return_60,
            entry_p=entry_p,
            target_p=target_p,
            stop_p=stop_p,
            ref_date=ref_date,
            quant_scenario=quant_scenario,
        )
        return AnalysisOutcome("found", code, name, "통과", result=result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("종목 분석 실패: %s %s", code, name)
        return AnalysisOutcome("failed", code, name, "예외", error=str(exc))


def generate_chart(code: str, name: str, entry_p: float, target_p: float, stop_p: float, config: Any) -> Path | None:
    """종합분석 후보의 차트 이미지를 생성합니다.

    Args:
        code: 종목 코드입니다.
        name: 종목명입니다.
        entry_p: 진입가입니다.
        target_p: 목표가입니다.
        stop_p: 손절가입니다.
        config: OHLCV 조회와 차트 저장 경로 설정입니다.

    Returns:
        생성된 차트 파일 경로입니다. 데이터가 부족하면 None입니다.
    """
    project_main = runtime()
    try:
        start_date = (datetime.now(project_main.KST) - timedelta(days=120)).strftime("%Y-%m-%d")
        df = project_main.fetch_ohlcv(code, start_date, config)
        if len(df) < 20:
            return None

        mc = mpf.make_marketcolors(up="r", down="b", edge="inherit", wick="inherit", volume="inherit")
        rc_params = {"font.family": mpl.rcParams["font.family"], "axes.unicode_minus": False}
        style = mpf.make_mpf_style(marketcolors=mc, gridstyle=":", rc=rc_params)
        hlines_config = dict(
            hlines=[target_p, entry_p, stop_p],
            colors=["r", "g", "b"],
            linestyle="--",
            alpha=0.6,
            linewidths=1.5,
        )

        fig, axlist = mpf.plot(
            df,
            type="candle",
            volume=True,
            mav=(5, 10, 20, 50),
            title=f"{name} ({code}) - 3 Months Setup",
            hlines=hlines_config,
            style=style,
            figsize=(10, 6),
            datetime_format="%y.%m.%d",
            returnfig=True,
        )

        ax = axlist[0]
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))
        bbox_props = dict(boxstyle="round,pad=0.3", fc="white", ec="none", alpha=0.7)
        ax.text(0, target_p, " 목표가", color="red", fontsize=10, va="bottom", ha="left", fontweight="bold", bbox=bbox_props)
        ax.text(0, entry_p, " 매수가", color="green", fontsize=10, va="bottom", ha="left", fontweight="bold", bbox=bbox_props)
        ax.text(0, stop_p, " 손절가", color="blue", fontsize=10, va="top", ha="left", fontweight="bold", bbox=bbox_props)

        project_main.cleanup_old_chart_images(project_main.CHART_DIR)
        output = project_main.CHART_DIR / f"chart_{code}_{datetime.now(project_main.KST).strftime('%Y%m%d_%H%M%S')}.png"
        fig.savefig(output, dpi=140, bbox_inches="tight")
        plt.close(fig)
        return output
    except Exception as exc:  # noqa: BLE001
        logger.exception("차트 생성 실패: %s %s", code, exc)
        return None


def build_message(stock: ScanResult) -> str:
    """종합분석 후보 한 종목의 텔레그램 메시지를 생성합니다.

    Args:
        stock: 분석 결과 데이터입니다.

    Returns:
        텔레그램으로 전송할 종목 브리핑 메시지입니다.
    """
    sector = stock.sector if str(stock.sector).lower() != "nan" and stock.sector else "기타"
    return (
        f"{stock.star_icon} [정통 셋업] {stock.name}({stock.code}) - {sector}\n"
        f"💰 현재가: {stock.curr_p:,.0f}원 (수렴 돌파: {stock.breakout_pct:+.1f}%)\n"
        f"📈 깃대: +{(stock.pole_ratio - 1) * 100:.0f}% | 🎌 수렴: {stock.flag_depth * 100:.1f}%\n"
        f"🔥 거래량: {stock.vol_ratio:.1f}배 | ADR: {stock.curr_adr:.1f}%\n"
        "────────────────\n"
        "📊 [핵심 기술적 분석]\n"
        f"   - 현재가: {stock.curr_p:,.0f}원\n"
        f"   - 평균 거래대금(50일): {stock.avg_turnover:,.0f}원\n"
        f"   - 시장 상대강도: {stock.rs_status}\n"
        f"   - 3-Bar (NR3): {stock.nr3_status}\n"
        f"   - VCP 패턴: {stock.vcp_status}\n"
        f"   - 쿨라메기 HTF 셋업: {stock.kulamegi_htf_status}\n"
        f"   - {stock.fib_summary}\n"
        "────────────────\n"
        f"{stock.material_info}\n"
        "────────────────\n"
        f"{stock.quant_scenario}"
    )
