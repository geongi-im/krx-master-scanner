#!/usr/bin/env python3
"""
KRX Master Scanner

기존 Jupyter/Colab 단일 셀 스캐너를 로컬 실행 가능한 Python 스크립트로 정리한 버전.
- Telegram token/chat_id는 .env에서 읽는다.
- FinanceDataReader OHLCV 호출은 로컬 CSV 캐시 + 재시도/backoff를 사용한다.
- 종목별 성공/스킵/실패 카운트와 결과 CSV를 남긴다.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import re
import time
import warnings
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import FinanceDataReader as fdr
import matplotlib as mpl

# 반드시 pyplot/mplfinance import 전에 Agg 설정
mpl.use("Agg")

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

warnings.filterwarnings("ignore", category=FutureWarning)

KST = timezone(timedelta(hours=9))
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
CHART_DIR = DATA_DIR / "charts"
REPORT_DIR = DATA_DIR / "reports"
LOG_DIR = APP_DIR / "logs"
ASSET_DIR = APP_DIR / "assets"

for directory in (DATA_DIR, CACHE_DIR, CHART_DIR, REPORT_DIR, LOG_DIR, ASSET_DIR):
    directory.mkdir(parents=True, exist_ok=True)

load_dotenv(APP_DIR / ".env")


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    return int(value)


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    return float(value)


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str | None = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = os.getenv("TELEGRAM_CHAT_ID")

    max_workers: int = env_int("MAX_WORKERS", 4)
    cache_ttl_hours: int = env_int("CACHE_TTL_HOURS", 18)
    fetch_retries: int = env_int("FETCH_RETRIES", 3)
    request_timeout: int = env_int("REQUEST_TIMEOUT", 10)

    first_pass_min_close: int = env_int("FIRST_PASS_MIN_CLOSE", 500)
    first_pass_min_amount: int = env_int("FIRST_PASS_MIN_AMOUNT", 1_000_000_000)
    min_avg_turnover: int = env_int("MIN_AVG_TURNOVER", 1_000_000_000)
    min_adr: float = env_float("MIN_ADR", 1.5)

    top_send_limit: int = env_int("TOP_SEND_LIMIT", 20)
    send_charts: bool = env_bool("SEND_CHARTS", True)
    force_refresh: bool = env_bool("FORCE_REFRESH", False)

    @property
    def telegram_enabled(self) -> bool:
        if not self.telegram_bot_token or not self.telegram_chat_id:
            return False
        placeholders = {"YOUR_BOT_TOKEN_HERE", "YOUR_CHAT_ID_HERE", ""}
        return self.telegram_bot_token not in placeholders and self.telegram_chat_id not in placeholders


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


def setup_logging() -> logging.Logger:
    now = datetime.now(KST).strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"{now}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger("krx-master-scanner")


logger = setup_logging()


def setup_korean_font() -> None:
    system = platform.system()
    if system == "Windows":
        mpl.rcParams["font.family"] = "Malgun Gothic"
    elif system == "Darwin":
        mpl.rcParams["font.family"] = "AppleGothic"
    else:
        font_path = ASSET_DIR / "NanumGothic-Regular.ttf"
        if not font_path.exists():
            url = "https://github.com/google/fonts/raw/main/ofl/nanumgothic/NanumGothic-Regular.ttf"
            logger.info("한글 폰트 다운로드: %s", font_path)
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            font_path.write_bytes(response.content)
        fm.fontManager.addfont(str(font_path))
        prop = fm.FontProperties(fname=str(font_path))
        mpl.rcParams["font.family"] = prop.get_name()
    mpl.rcParams["axes.unicode_minus"] = False


def safe_filename(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", value)


def cache_path(symbol: str) -> Path:
    """종목별 고정 OHLCV 캐시 경로.

    날짜를 파일명에 넣지 않아야 매일 실행 시 기존 600일치 데이터를 재사용하고
    마지막 저장일 이후 구간만 추가 조회할 수 있다.
    """
    return CACHE_DIR / f"ohlcv_{safe_filename(symbol)}.csv"


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    normalized = df.copy()
    normalized.index = pd.to_datetime(normalized.index)
    normalized = normalized.sort_index()
    normalized = normalized[~normalized.index.duplicated(keep="last")]
    return normalized


def read_cached_ohlcv(symbol: str, start_date: str, config: Config) -> pd.DataFrame | None:
    if config.force_refresh:
        return None

    path = cache_path(symbol)
    if not path.exists():
        return None

    df = normalize_ohlcv(pd.read_csv(path, index_col=0, parse_dates=True))
    if df.empty:
        return None

    requested_start = pd.Timestamp(start_date)
    if df.index.min() > requested_start or df.index.max() < requested_start:
        return None

    return df.loc[df.index >= requested_start]


def cache_is_fresh(symbol: str, config: Config) -> bool:
    path = cache_path(symbol)
    if not path.exists():
        return False
    age_hours = (time.time() - path.stat().st_mtime) / 3600
    return age_hours <= config.cache_ttl_hours


def write_cached_ohlcv(symbol: str, df: pd.DataFrame) -> None:
    df = normalize_ohlcv(df)
    if not df.empty:
        df.to_csv(cache_path(symbol), encoding="utf-8-sig")


def merge_ohlcv(cached: pd.DataFrame | None, fetched: pd.DataFrame, start_date: str) -> pd.DataFrame:
    fetched = normalize_ohlcv(fetched)
    if cached is not None and not cached.empty:
        merged = pd.concat([cached, fetched])
    else:
        merged = fetched
    merged = normalize_ohlcv(merged)
    return merged.loc[merged.index >= pd.Timestamp(start_date)]


def fetch_ohlcv(symbol: str, start_date: str, config: Config) -> pd.DataFrame:
    cached = read_cached_ohlcv(symbol, start_date, config)
    if cached is not None and cache_is_fresh(symbol, config):
        return cached

    fetch_start = start_date
    if cached is not None and not cached.empty:
        # 마지막 저장일을 하루 겹쳐 받아서 당일 데이터 수정/보정분은 새 값으로 덮어쓴다.
        fetch_start = cached.index.max().strftime("%Y-%m-%d")

    last_error: Exception | None = None
    for attempt in range(1, config.fetch_retries + 1):
        try:
            fetched = fdr.DataReader(symbol, fetch_start)
            if fetched is None or fetched.empty:
                raise ValueError("empty dataframe")
            df = merge_ohlcv(cached, fetched, start_date)
            write_cached_ohlcv(symbol, df)
            return df
        except Exception as exc:  # noqa: BLE001 - 종목별 실패를 집계해야 함
            last_error = exc
            sleep_seconds = min(2 ** attempt, 10)
            logger.warning("OHLCV 조회 실패: symbol=%s attempt=%s/%s error=%s", symbol, attempt, config.fetch_retries, exc)
            if attempt < config.fetch_retries:
                time.sleep(sleep_seconds)

    raise RuntimeError(f"OHLCV 조회 실패: {symbol}: {last_error}")


def telegram_post(url: str, *, data: dict[str, Any], files: dict[str, Any] | None, config: Config) -> None:
    for attempt in range(1, 4):
        response = requests.post(url, data=data, files=files, timeout=config.request_timeout)
        if response.status_code == 429:
            retry_after = response.json().get("parameters", {}).get("retry_after", 3)
            logger.warning("Telegram rate limit: retry_after=%s", retry_after)
            time.sleep(int(retry_after) + 1)
            continue
        response.raise_for_status()
        return
    raise RuntimeError(f"Telegram 전송 실패: {response.text[:300]}")


def split_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remain = text
    while len(remain) > limit:
        cut = remain.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remain[:cut])
        remain = remain[cut:].lstrip()
    if remain:
        chunks.append(remain)
    return chunks


def send_telegram_msg(text: str, config: Config, *, dry_run: bool = False) -> None:
    if dry_run or not config.telegram_enabled:
        logger.info("Telegram 메시지 스킵(dry_run=%s enabled=%s): %s", dry_run, config.telegram_enabled, text[:120])
        return
    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    for chunk in split_message(text):
        telegram_post(url, data={"chat_id": config.telegram_chat_id, "text": chunk}, files=None, config=config)
        time.sleep(0.4)


def send_telegram_photo(photo_path: Path, config: Config, *, dry_run: bool = False) -> None:
    if dry_run or not config.telegram_enabled:
        logger.info("Telegram 사진 스킵(dry_run=%s enabled=%s): %s", dry_run, config.telegram_enabled, photo_path)
        return
    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendPhoto"
    with photo_path.open("rb") as fp:
        telegram_post(url, data={"chat_id": config.telegram_chat_id}, files={"photo": fp}, config=config)


def check_market_regime(config: Config) -> MarketRegime:
    start_date = (datetime.now(KST) - timedelta(days=150)).strftime("%Y-%m-%d")
    try:
        df = fetch_ohlcv("KQ11", start_date, config)
        if len(df) < 60:
            raise ValueError(f"코스닥 지수 데이터 부족: rows={len(df)}")
        ma50 = float(df["Close"].rolling(50).mean().iloc[-1])
        current = float(df["Close"].iloc[-1])
        kq_return_60 = float((df["Close"].iloc[-1] / df["Close"].iloc[-60] - 1) * 100)
        return MarketRegime(True, current >= ma50, current, ma50, kq_return_60)
    except Exception as exc:  # noqa: BLE001
        logger.exception("시장 국면 조회 실패")
        return MarketRegime(False, False, 0.0, 0.0, 0.0, str(exc))


def load_universe(config: Config, max_symbols: int | None = None) -> pd.DataFrame:
    basic = fdr.StockListing("KRX")
    try:
        desc = fdr.StockListing("KRX-DESC")[["Code", "Sector"]]
        universe = pd.merge(basic, desc, on="Code", how="left")
    except Exception as exc:  # noqa: BLE001
        logger.warning("KRX-DESC 조회 실패. Sector 없이 진행: %s", exc)
        universe = basic.copy()
        universe["Sector"] = ""

    universe = universe[universe["Market"].isin(["KOSPI", "KOSDAQ"])]
    universe = universe[universe["Code"].astype(str).str.match(r"^\d{6}$")]
    universe = universe[~universe["Name"].astype(str).str.contains(r"스팩|제[0-9]+호|우$|우B|우C|리츠|ETF|ETN", regex=True, na=False)]

    if "Close" in universe.columns:
        universe = universe[universe["Close"].fillna(0) >= config.first_pass_min_close]

    # FDR KRX listing에는 보통 Amount(거래대금)가 들어온다. 없으면 스킵.
    if "Amount" in universe.columns:
        universe = universe[universe["Amount"].fillna(0) >= config.first_pass_min_amount]

    universe = universe[["Code", "Name", "Sector"]].fillna("").reset_index(drop=True)
    if max_symbols:
        universe = universe.head(max_symbols)
    return universe


def generate_quant_scenario(
    curr_p: float,
    flag_high: float,
    high52: float,
    vol_ratio: float,
    flag_depth: float,
    ref_open: float,
    ref_date: str,
) -> tuple[str, float, float, float]:
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


def get_stock_details(code: str, name: str, config: Config) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"}
    supply_summary = "🤝 [수급 현황] 정보 없음"
    today = datetime.now(KST).replace(tzinfo=None)
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


def analyze_stock(stock_info: tuple[str, str, str], kq_return_60: float, config: Config) -> AnalysisOutcome:
    code, name, sector = stock_info
    start_date = (datetime.now(KST) - timedelta(days=600)).strftime("%Y-%m-%d")

    try:
        df = fetch_ohlcv(code, start_date, config)
        if len(df) < 260:
            return AnalysisOutcome("skipped", code, name, "데이터 260행 미만")

        df = df.copy()
        for window in (10, 20, 50, 150, 200):
            df[f"MA{window}"] = df["Close"].rolling(window=window).mean()
        df["High52"] = df["High"].rolling(window=250).max()
        df["Low52"] = df["Low"].rolling(window=250).min()
        df["AvgVol50"] = df["Volume"].rolling(window=50).mean()
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
        avg_turnover = float(today["AvgVol50"] * curr_p)

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

        # 기존 코드의 핵심 조건은 유지하되, 너무 늦은 돌파 종목은 제외한다.
        if not (flag_high * 0.95 <= curr_p <= flag_high * 1.03):
            return AnalysisOutcome("skipped", code, name, "돌파 가격 범위 이탈")

        # 손절가가 매수가보다 높거나 손실폭이 지나치게 큰 케이스 제외.
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


def generate_chart(code: str, name: str, entry_p: float, target_p: float, stop_p: float, config: Config) -> Path | None:
    try:
        start_date = (datetime.now(KST) - timedelta(days=120)).strftime("%Y-%m-%d")
        df = fetch_ohlcv(code, start_date, config)
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
            returnfig=True,
        )

        ax = axlist[0]
        bbox_props = dict(boxstyle="round,pad=0.3", fc="white", ec="none", alpha=0.7)
        ax.text(0, target_p, " 목표가", color="red", fontsize=10, va="bottom", ha="left", fontweight="bold", bbox=bbox_props)
        ax.text(0, entry_p, " 매수가", color="green", fontsize=10, va="bottom", ha="left", fontweight="bold", bbox=bbox_props)
        ax.text(0, stop_p, " 손절가", color="blue", fontsize=10, va="top", ha="left", fontweight="bold", bbox=bbox_props)

        output = CHART_DIR / f"chart_{code}_{datetime.now(KST).strftime('%Y%m%d_%H%M%S')}.png"
        fig.savefig(output, dpi=140, bbox_inches="tight")
        plt.close(fig)
        return output
    except Exception as exc:  # noqa: BLE001
        logger.exception("차트 생성 실패: %s %s", code, exc)
        return None


def build_message(stock: ScanResult) -> str:
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


def save_reports(results: list[ScanResult], stats: Counter, start_ts: str) -> tuple[Path, Path]:
    rows = [asdict(result) for result in results]
    csv_path = REPORT_DIR / f"scan_results_{start_ts}.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False, encoding="utf-8-sig")

    stats_path = REPORT_DIR / f"scan_stats_{start_ts}.json"
    stats_path.write_text(json.dumps(dict(stats), ensure_ascii=False, indent=2), encoding="utf-8")
    return csv_path, stats_path


def run(config: Config, *, dry_run: bool, max_symbols: int | None, no_charts: bool) -> int:
    setup_korean_font()
    start_ts = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    now_display = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    logger.info("마스터 스캐너 시작: %s", now_display)

    stats: Counter = Counter()

    regime = check_market_regime(config)
    if not regime.ok:
        regime_msg = f"⚠️ [시장 데이터 경고] 코스닥 지수 조회 실패: {regime.error}\n시장 국면은 '불명'으로 표시하고 스캔은 계속합니다.\n\n"
    elif not regime.is_bull_market:
        regime_msg = (
            f"🛑 [시장 경고] 코스닥 지수({regime.current:,.2f})가 50일선({regime.ma50:,.2f})을 하회합니다.\n"
            "수익 보전과 현금 비중 확대를 권장합니다.\n\n"
        )
    else:
        regime_msg = ""

    universe = load_universe(config, max_symbols=max_symbols)
    stats["universe_after_first_filter"] = len(universe)
    logger.info("1차 필터 후 분석 대상: %s개", len(universe))

    found: list[ScanResult] = []
    skip_reasons: Counter = Counter()
    fail_reasons: Counter = Counter()

    stocks = list(universe[["Code", "Name", "Sector"]].itertuples(index=False, name=None))
    with ThreadPoolExecutor(max_workers=config.max_workers) as executor:
        futures = {executor.submit(analyze_stock, stock, regime.kq_return_60, config): stock for stock in stocks}
        for idx, future in enumerate(as_completed(futures), start=1):
            outcome = future.result()
            stats[f"status_{outcome.status}"] += 1
            if outcome.status == "found" and outcome.result:
                found.append(outcome.result)
            elif outcome.status == "skipped":
                skip_reasons[outcome.reason] += 1
            else:
                fail_reasons[outcome.reason or outcome.error or "unknown"] += 1

            if idx % 50 == 0 or idx == len(stocks):
                logger.info("진행률: %s/%s found=%s skipped=%s failed=%s", idx, len(stocks), len(found), stats["status_skipped"], stats["status_failed"])

    found.sort(key=lambda x: (x.stars, x.rs_score, x.avg_turnover), reverse=True)
    stats["found_total"] = len(found)
    stats["skip_reasons"] = dict(skip_reasons.most_common(20))
    stats["fail_reasons"] = dict(fail_reasons.most_common(20))

    # 뉴스/수급은 통과 종목에만 붙인다. 전 종목 크롤링 금지.
    for result in found[: config.top_send_limit]:
        result.material_info = get_stock_details(result.code, result.name, config)

    csv_path, stats_path = save_reports(found, stats, start_ts)
    logger.info("결과 CSV 저장: %s", csv_path)
    logger.info("통계 JSON 저장: %s", stats_path)

    if found:
        sectors = [result.sector for result in found if result.sector]
        sector_warning = ""
        if sectors:
            top_sector, count = Counter(sectors).most_common(1)[0]
            if count >= 3:
                sector_warning = f"⚠️ [섹터 집중 경고] 포착된 종목 중 {count}개가 '{top_sector}'에 집중되어 있습니다.\n\n"

        intro_msg = (
            f"{regime_msg}🔔 [퀀트 스캔 결과] 포착 종목: {len(found)}개\n"
            f"⏰ 스캔 일시: {now_display}\n"
            f"📄 CSV: {csv_path}\n"
            f"📊 Stats: {stats_path}\n\n"
            f"{sector_warning}👇 상위 {min(config.top_send_limit, len(found))}개 브리핑을 시작합니다."
        )
        send_telegram_msg(intro_msg, config, dry_run=dry_run)

        for result in found[: config.top_send_limit]:
            chart_file = None
            if config.send_charts and not no_charts:
                chart_file = generate_chart(result.code, result.name, result.entry_p, result.target_p, result.stop_p, config)
                if chart_file:
                    send_telegram_photo(chart_file, config, dry_run=dry_run)
                    time.sleep(0.5)
            send_telegram_msg(build_message(result), config, dry_run=dry_run)
            time.sleep(0.5)
    else:
        no_result_msg = f"{regime_msg}💡 오늘은 조건을 만족하는 종목이 없습니다.\n📄 CSV: {csv_path}\n📊 Stats: {stats_path}"
        send_telegram_msg(no_result_msg, config, dry_run=dry_run)
        logger.info("조건 만족 종목 없음")

    logger.info("스캐너 종료: stats=%s", dict(stats))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KRX Master Scanner")
    parser.add_argument("--dry-run", action="store_true", help="Telegram 전송 없이 로컬 실행/리포트만 생성")
    parser.add_argument("--max-symbols", type=int, default=None, help="테스트용 분석 종목 수 제한")
    parser.add_argument("--workers", type=int, default=None, help="병렬 분석 worker 수 override")
    parser.add_argument("--force-refresh", action="store_true", help="OHLCV 캐시 무시")
    parser.add_argument("--no-charts", action="store_true", help="차트 생성/전송 생략")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = Config()
    if args.workers is not None or args.force_refresh:
        config = Config(
            max_workers=args.workers if args.workers is not None else config.max_workers,
            force_refresh=True if args.force_refresh else config.force_refresh,
        )
    return run(config, dry_run=args.dry_run, max_symbols=args.max_symbols, no_charts=args.no_charts)


if __name__ == "__main__":
    raise SystemExit(main())
