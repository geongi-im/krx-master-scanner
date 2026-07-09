#!/usr/bin/env python3
"""
VCP scanner based on the MariaDB OHLCV cache.

This module is used by main.py after the master scan and can also be run
directly for standalone VCP checks.
"""

from __future__ import annotations

import argparse
import json
import math
import numbers
import platform
import re
import sys
import time
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import FinanceDataReader as fdr
import pandas as pd
import requests
from bs4 import BeautifulSoup

import db_scheme

try:
    from scipy.signal import find_peaks as scipy_find_peaks
except ImportError:  # pragma: no cover - depends on local environment
    scipy_find_peaks = None

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - depends on local environment

    def tqdm(iterable: Iterable, **_: object) -> Iterable:
        """tqdm이 없을 때 원본 iterable을 그대로 반환합니다.

        Args:
            iterable: 진행률 표시 없이 순회할 객체입니다.
            **_: tqdm 호환을 위해 무시할 키워드 인자입니다.

        Returns:
            입력 iterable입니다.
        """
        return iterable


warnings.filterwarnings("ignore")

MAX_DROP_FROM_HIGH = 0.18
MAX_PIVOT_GAP = 0.15
MIN_CONTRACTION_SEGMENTS = 2
MAX_FINAL_CONTRACTION_PCT = 5.0
VOLUME_DRY_UP_LOOKBACK_DAYS = 120
VOLUME_DRY_UP_WINDOW = 10
MIN_VOLUME_DRY_UP_RATIO = 0.70
APP_DIR = Path(__file__).resolve().parent
DEFAULT_CHARTS_DIR = APP_DIR / "data" / "charts"
CHART_RETENTION_DAYS = 3


def cleanup_old_chart_images(charts_dir: Path) -> int:
    """차트 디렉터리에서 3일이 지난 PNG 이미지를 삭제합니다."""
    charts_dir.mkdir(parents=True, exist_ok=True)
    cutoff_ts = time.time() - (CHART_RETENTION_DAYS * 24 * 60 * 60)
    deleted = 0

    for image_path in charts_dir.glob("*.png"):
        try:
            if image_path.is_file() and image_path.stat().st_mtime < cutoff_ts:
                image_path.unlink()
                deleted += 1
        except OSError as exc:
            print(f"Failed to delete old chart image: {image_path} ({exc})")

    return deleted


@dataclass(frozen=True)
class VcpCriteria:
    """VCP 스캔에 사용하는 지표 기준값입니다."""

    min_avg_traded_value: int = 15_000_000_000
    max_drop_from_high: float = MAX_DROP_FROM_HIGH
    max_pivot_gap: float = MAX_PIVOT_GAP
    min_contraction_segments: int = MIN_CONTRACTION_SEGMENTS
    max_contraction_ratio: float = 1.0
    max_final_contraction_pct: float = MAX_FINAL_CONTRACTION_PCT
    high_window: int = 252
    recent_high_window: int = 20
    volume_dry_up_lookback_days: int = VOLUME_DRY_UP_LOOKBACK_DAYS
    volume_dry_up_window: int = VOLUME_DRY_UP_WINDOW
    min_volume_dry_up_ratio: float = MIN_VOLUME_DRY_UP_RATIO
    fast_ma_window: int = 20
    mid_ma_window: int = 50
    long_ma_window: int = 150
    base_ma_window: int = 200
    require_price_above_fast_ma: bool = True
    require_price_above_mid_ma: bool = True
    require_ma_alignment: bool = True
    pocket_pivot_days: int = 14
    pocket_volume_window: int = 10
    min_pocket_pivot_count: int = 1


def runtime():
    """현재 실행 중인 프로젝트 런타임 모듈을 반환합니다.

    Returns:
        `fetch_ohlcv` 등 프로젝트 공용 함수가 들어 있는 main 모듈 객체입니다.
    """
    entrypoint = sys.modules.get("__main__")
    if entrypoint is not None and hasattr(entrypoint, "fetch_ohlcv"):
        return entrypoint

    loaded_main = sys.modules.get("main")
    if loaded_main is not None:
        return loaded_main

    import main as project_main

    return project_main


def safe_filename(value: str) -> str:
    """차트 파일명에 안전하지 않은 문자를 밑줄로 치환합니다.

    Args:
        value: 파일명에 사용할 원본 문자열입니다.

    Returns:
        안전한 파일명 문자열입니다.
    """
    return re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", value)


def is_finite_number(value: object) -> bool:
    """값이 DB 저장 가능한 유한 숫자인지 확인합니다.

    Args:
        value: 검사할 값입니다.

    Returns:
        값이 int 또는 float로 변환 가능하고 NaN/Infinity가 아니면 True입니다.
    """
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def sanitize_json_value(value: object) -> object:
    """JSON 저장 전에 NaN과 Infinity 값을 None으로 정리합니다.

    Args:
        value: 정리할 원본 값입니다.

    Returns:
        JSON 직렬화에 안전한 값입니다.
    """
    if isinstance(value, dict):
        return {key: sanitize_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, numbers.Real):
        return float(value) if math.isfinite(float(value)) else None
    return value


def resolve_target_datetime(target_date: str | datetime | None) -> datetime:
    """VCP 실행 기준일을 datetime으로 변환합니다."""
    if target_date is None:
        return datetime.today()
    try:
        parsed = pd.Timestamp(target_date)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("target_date는 YYYY-MM-DD 형식이어야 합니다.") from exc
    if pd.isna(parsed):
        raise ValueError("target_date는 YYYY-MM-DD 형식이어야 합니다.")
    return parsed.to_pydatetime()


def find_peaks(values: list[float], distance: int = 1) -> list[int]:
    """값 목록에서 지역 고점 인덱스를 찾습니다.

    Args:
        values: 고점을 찾을 숫자 목록입니다.
        distance: 고점 사이에 필요한 최소 인덱스 간격입니다.

    Returns:
        지역 고점으로 판정된 인덱스 목록입니다.
    """
    if scipy_find_peaks is not None:
        peaks, _ = scipy_find_peaks(values, distance=distance)
        return list(peaks)

    peaks: list[int] = []
    last_peak = -distance
    for idx in range(1, len(values) - 1):
        if idx - last_peak < distance:
            continue
        if values[idx] > values[idx - 1] and values[idx] > values[idx + 1]:
            peaks.append(idx)
            last_peak = idx
    return peaks


def setup_chart_style() -> None:
    """VCP 차트 생성을 위한 matplotlib 스타일과 한글 폰트를 설정합니다."""
    import matplotlib as mpl

    mpl.use("Agg")
    if platform.system() == "Windows":
        mpl.rcParams["font.family"] = "Malgun Gothic"
    elif platform.system() == "Darwin":
        mpl.rcParams["font.family"] = "AppleGothic"
    mpl.rcParams["axes.unicode_minus"] = False


def get_clean_universe(max_symbols: int | None = None) -> pd.DataFrame:
    """VCP 스캔에 사용할 KRX 종목 유니버스를 구성합니다.

    Args:
        max_symbols: 테스트용 최대 종목 수입니다.

    Returns:
        Code, Name 컬럼을 가진 종목 유니버스입니다.

    Raises:
        RuntimeError: FDR 종목 리스트와 MariaDB 캐시 모두 사용할 수 없는 경우 발생합니다.
    """
    print("Loading KRX stock list...")
    try:
        krx_df = fdr.StockListing("KRX")
    except Exception as exc:  # noqa: BLE001 - fallback keeps scanner usable when FDR listing endpoint fails.
        print(f"KRX stock list load failed: {exc}")
        try:
            project_main = runtime()
            krx_df = project_main.load_krx_finder_listing(timeout=project_main.Config().request_timeout)
            print(f"Loaded KRX stock list from finder fallback: {len(krx_df)} symbols")
        except Exception as finder_exc:  # noqa: BLE001
            print(f"KRX finder fallback failed: {finder_exc}")
            print("Falling back to MariaDB OHLCV cache symbols...")
            project_main = runtime()

            config = project_main.Config()
            _, meta_table = project_main.db_table_names(config)
            limit_sql = "LIMIT %s" if max_symbols else ""
            connection = project_main.db_connection(config)
            try:
                project_main.ensure_ohlcv_cache_tables(connection, config)
                with connection.cursor() as cursor:
                    params = (max_symbols,) if max_symbols else ()
                    cursor.execute(
                        f"""
                        SELECT symbol AS Code, symbol AS Name
                        FROM {meta_table}
                        WHERE row_count >= 200
                        ORDER BY max_trade_date DESC, symbol
                        {limit_sql}
                        """,
                        params,
                    )
                    clean_universe = pd.DataFrame(cursor.fetchall())
            finally:
                connection.close()

            if clean_universe.empty:
                raise RuntimeError("MariaDB OHLCV cache has no symbols to scan") from exc
            print(f"Clean universe ready from MariaDB cache: {len(clean_universe)} symbols")
            return clean_universe

    blacklist = [
        "KODEX",
        "TIGER",
        "KINDEX",
        "KBSTAR",
        "ARIRANG",
        "HANARO",
        "KOSEF",
        "TIMEFOLIO",
        "ACE",
        "SOL",
        "ETF",
        "ETN",
        "REIT",
        "SPAC",
        "스팩",
        "리츠",
        r"제[0-9]+호",
        r"우$",
        "우B",
        "우C",
    ]

    name = krx_df["Name"].astype(str)
    clean_universe = krx_df[~name.str.contains("|".join(blacklist), na=False, case=False)]

    if "Code" in clean_universe.columns:
        clean_universe = clean_universe[clean_universe["Code"].astype(str).str.match(r"^\d{6}$")]

    if "Market" in clean_universe.columns:
        clean_universe = clean_universe[clean_universe["Market"].map(runtime().normalize_krx_market).isin(["KOSPI", "KOSDAQ"])]

    clean_universe = clean_universe[["Code", "Name"]].dropna().reset_index(drop=True)
    if max_symbols:
        clean_universe = clean_universe.head(max_symbols)

    print(f"Clean universe ready: {len(clean_universe)} symbols")
    return clean_universe


def calculate_swing_segments(close_prices: pd.Series) -> list[dict[str, object]]:
    """최근 가격 흐름에서 고점-저점 수축 구간을 계산합니다.

    Args:
        close_prices: 종가 시계열입니다.

    Returns:
        고점일, 저점일, 가격, 하락률을 담은 수축 구간 목록입니다.
    """
    if len(close_prices) < 120:
        return []

    recent = close_prices.tail(120)
    recent_120 = [float(value) for value in recent.values]
    peaks = find_peaks(recent_120, distance=10)
    troughs = find_peaks([-value for value in recent_120], distance=10)

    segments: list[dict[str, object]] = []
    for peak_idx in peaks:
        later_troughs = [trough_idx for trough_idx in troughs if trough_idx > peak_idx]
        if not later_troughs:
            continue
        trough_idx = later_troughs[0]
        peak_price = recent_120[peak_idx]
        trough_price = recent_120[trough_idx]
        if peak_price > 0:
            segments.append(
                {
                    "peak_date": recent.index[peak_idx],
                    "trough_date": recent.index[trough_idx],
                    "peak_price": peak_price,
                    "trough_price": trough_price,
                    "drop_pct": round((peak_price - trough_price) / peak_price * 100, 2),
                }
            )

    return segments


def calculate_swing_volatility(close_prices: pd.Series) -> list[float]:
    """최근 수축 구간의 하락률 목록을 계산합니다.

    Args:
        close_prices: 종가 시계열입니다.

    Returns:
        수축 구간별 하락률 목록입니다.
    """
    return [float(segment["drop_pct"]) for segment in calculate_swing_segments(close_prices)]


def classify_vcp_stage(drop_pct: float) -> str:
    """52주 고점 대비 하락률로 VCP 단계를 분류합니다.

    Args:
        drop_pct: 52주 고점 대비 하락률입니다.

    Returns:
        VCP 단계 라벨입니다.
    """
    if drop_pct <= 8.0:
        return "3T breakout-ready"
    if drop_pct <= 15.0:
        return "2T mid contraction"
    return "1T early contraction"


def save_vcp_chart(
    df: pd.DataFrame,
    *,
    name: str,
    symbol: str,
    vcp_stage: str,
    drop_pct: float,
    swing_segments: list[dict[str, object]],
    pocket_pivot_points: pd.DataFrame | None = None,
    output_dir: Path,
    high_window: int = 252,
) -> Path:
    """VCP 후보의 가격, 거래량, 수축 구간 차트를 저장합니다.

    Args:
        df: OHLCV 데이터프레임입니다.
        name: 종목명입니다.
        symbol: 종목 코드입니다.
        vcp_stage: VCP 단계 라벨입니다.
        drop_pct: 52주 고점 대비 하락률입니다.
        swing_segments: 차트에 표시할 수축 구간 목록입니다.
        pocket_pivot_points: 차트에 표시할 Pocket Pivot 발생일 목록입니다.
        output_dir: 차트 이미지를 저장할 디렉터리입니다.

    Returns:
        저장된 차트 이미지 파일 경로입니다.
    """
    setup_chart_style()

    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    from matplotlib.ticker import FuncFormatter

    output_dir.mkdir(parents=True, exist_ok=True)
    cleanup_old_chart_images(output_dir)
    recent = df.tail(120).copy()
    recent.index = pd.to_datetime(recent.index)
    x_positions = list(range(len(recent)))

    fig, (ax_price, ax_volume) = plt.subplots(
        2,
        1,
        sharex=True,
        figsize=(10, 7),
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.04},
    )

    body_width = 0.55
    min_body = max((recent["High"].max() - recent["Low"].min()) * 0.002, 1.0)
    up_color = "#e41f26"
    down_color = "#0047d9"

    for x_pos, (_, row) in zip(x_positions, recent.iterrows(), strict=True):
        open_price = float(row["Open"])
        high_price = float(row["High"])
        low_price = float(row["Low"])
        close_price = float(row["Close"])
        color = up_color if close_price >= open_price else down_color

        ax_price.vlines(x_pos, low_price, high_price, color=color, linewidth=0.8)
        body_low = min(open_price, close_price)
        body_height = max(abs(close_price - open_price), min_body)
        ax_price.add_patch(
            Rectangle(
                (x_pos - body_width / 2, body_low),
                body_width,
                body_height,
                facecolor=color,
                edgecolor=color,
                linewidth=0.6,
            )
        )

        volume = float(row["Volume"]) / 1_000_000
        ax_volume.bar(x_pos, volume, color=color, width=body_width)

    if len(recent) >= 20:
        ax_price.plot(x_positions, recent["Close"].rolling(20).mean(), color="#2a7fb8", linewidth=1.0, alpha=0.9)
    if len(recent) >= 60:
        ax_price.plot(x_positions, recent["Close"].rolling(60).mean(), color="#f28e2b", linewidth=1.0, alpha=0.9)

    high_52w = float(df["High"].rolling(window=high_window).max().iloc[-1])
    ax_price.axhline(high_52w, color="#d62728", linestyle="--", linewidth=1.0, alpha=0.55)

    date_to_pos = {date: idx for idx, date in enumerate(recent.index)}
    visible_segments = [
        segment
        for segment in swing_segments
        if pd.Timestamp(segment["peak_date"]) in date_to_pos and pd.Timestamp(segment["trough_date"]) in date_to_pos
    ][-3:]

    for segment in visible_segments:
        peak_date = pd.Timestamp(segment["peak_date"])
        trough_date = pd.Timestamp(segment["trough_date"])
        peak_x = date_to_pos[peak_date]
        trough_x = date_to_pos[trough_date]
        peak_price = float(segment["peak_price"])
        trough_price = float(segment["trough_price"])
        segment_drop_pct = float(segment["drop_pct"])

        ax_price.plot([peak_x, trough_x], [peak_price, trough_price], color="#f28e2b", linewidth=1.4)
        ax_price.annotate(
            f"-{segment_drop_pct:.1f}%",
            xy=(trough_x, trough_price),
            xytext=(0, -18),
            textcoords="offset points",
            ha="center",
            color="red",
            fontsize=10,
            fontweight="bold",
            bbox={"boxstyle": "round,pad=0.2", "facecolor": "#fff6ba", "edgecolor": "none", "alpha": 0.85},
        )

    if pocket_pivot_points is not None and not pocket_pivot_points.empty:
        visible_pivots = pocket_pivot_points.copy()
        visible_pivots.index = pd.to_datetime(visible_pivots.index)
        visible_pivots = visible_pivots[visible_pivots.index.isin(recent.index)]
        if not visible_pivots.empty:
            price_range = max(float(recent["High"].max() - recent["Low"].min()), 1.0)
            ax_price.scatter([], [], marker="^", s=70, color="#6f2dbd", edgecolors="white", linewidths=0.7, label="Pocket Pivot")
            for pivot_date, pivot_row in visible_pivots.iterrows():
                pivot_x = date_to_pos[pd.Timestamp(pivot_date)]
                pivot_high = float(pivot_row["High"])
                pivot_volume = float(pivot_row["Volume"]) / 1_000_000
                marker_y = pivot_high + price_range * 0.035

                ax_price.scatter(
                    pivot_x,
                    marker_y,
                    marker="^",
                    s=70,
                    color="#6f2dbd",
                    edgecolors="white",
                    linewidths=0.7,
                    zorder=6,
                )
                ax_price.annotate(
                    "PP",
                    xy=(pivot_x, marker_y),
                    xytext=(0, 8),
                    textcoords="offset points",
                    ha="center",
                    va="bottom",
                    color="#4c1d95",
                    fontsize=9,
                    fontweight="bold",
                )
                ax_volume.scatter(
                    pivot_x,
                    pivot_volume,
                    marker="^",
                    s=58,
                    color="#6f2dbd",
                    edgecolors="white",
                    linewidths=0.7,
                    zorder=5,
                )
            ax_price.legend(loc="upper left", fontsize=8, frameon=True)

    stage_label = vcp_stage.split()[0]
    ax_price.set_title(f"[VCP {stage_label}] {name} ({symbol}) - 신고가까지 {drop_pct:.2f}%", fontsize=15, fontweight="bold", pad=14)
    ax_price.set_ylabel("Price")
    ax_price.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))
    ax_volume.set_ylabel("Volume 10^6")

    for axis in (ax_price, ax_volume):
        axis.grid(True, linestyle=":", linewidth=0.8, alpha=0.75)
        axis.set_axisbelow(True)

    tick_count = min(6, len(recent))
    if tick_count:
        tick_step = max(1, len(recent) // tick_count)
        ticks = list(range(0, len(recent), tick_step))
        if ticks[-1] != len(recent) - 1:
            ticks.append(len(recent) - 1)
        ax_volume.set_xticks(ticks)
        ax_volume.set_xticklabels([recent.index[idx].strftime("%y.%m.%d") for idx in ticks], rotation=45, ha="right")

    fig.tight_layout()
    output_path = output_dir / f"chart_{safe_filename(name)}_{symbol}.png"
    fig.savefig(output_path, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return output_path


@lru_cache(maxsize=2048)
def get_symbol_name(symbol: str) -> str:
    """종목 코드에 해당하는 종목명을 조회합니다.

    Args:
        symbol: 종목 코드입니다.

    Returns:
        조회된 종목명입니다. 실패하면 종목 코드를 반환합니다.
    """
    try:
        universe = fdr.StockListing("KRX")
        matched = universe[universe["Code"].astype(str) == str(symbol)]
        if not matched.empty:
            return str(matched.iloc[0]["Name"])
    except Exception:
        pass

    try:
        project_main = runtime()
        universe = project_main.load_krx_finder_listing(timeout=project_main.Config().request_timeout)
        matched = universe[universe["Code"].astype(str) == str(symbol)]
        if not matched.empty:
            return str(matched.iloc[0]["Name"])
    except Exception:
        pass

    try:
        response = requests.get(
            f"https://finance.naver.com/item/main.naver?code={symbol}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "euc-kr"
        soup = BeautifulSoup(response.text, "html.parser")

        title_node = soup.select_one("div.wrap_company h2 a") or soup.select_one("title")
        if title_node and title_node.get_text(strip=True):
            title = title_node.get_text(strip=True)
            name = title.split(":", 1)[0].strip()
            name = name.replace("네이버페이 증권", "").replace("네이버 금융", "").strip()
            if name and name != symbol:
                return name
    except Exception:
        pass
    return symbol


def resolve_symbol_name(symbol: str, candidate_name: object) -> str:
    """VCP 후보의 표시용 종목명을 결정합니다.

    Args:
        symbol: 종목 코드입니다.
        candidate_name: 유니버스에서 전달된 종목명 후보입니다.

    Returns:
        코드가 아닌 실제 종목명을 우선 반환하고, 실패하면 종목 코드를 반환합니다.
    """
    name = str(candidate_name or "").strip()
    if name and name != symbol and not re.fullmatch(r"\d{6}", name):
        return name
    return get_symbol_name(symbol)


def validate_vcp_criteria(criteria: VcpCriteria) -> None:
    """VCP 기준값이 계산 가능한 범위인지 검증합니다.

    Args:
        criteria: VCP 스캔에 사용할 지표 기준값입니다.

    Raises:
        ValueError: 기간, 비율, 최소 횟수 값이 유효하지 않을 때 발생합니다.
    """
    window_values = {
        "high_window": criteria.high_window,
        "recent_high_window": criteria.recent_high_window,
        "volume_dry_up_lookback_days": criteria.volume_dry_up_lookback_days,
        "volume_dry_up_window": criteria.volume_dry_up_window,
        "fast_ma_window": criteria.fast_ma_window,
        "mid_ma_window": criteria.mid_ma_window,
        "long_ma_window": criteria.long_ma_window,
        "base_ma_window": criteria.base_ma_window,
        "pocket_pivot_days": criteria.pocket_pivot_days,
        "pocket_volume_window": criteria.pocket_volume_window,
    }
    invalid_windows = [name for name, value in window_values.items() if value <= 0]
    if invalid_windows:
        raise ValueError(f"VCP 기간 기준은 1 이상이어야 합니다: {', '.join(invalid_windows)}")
    if criteria.min_avg_traded_value < 0:
        raise ValueError("VCP 최소 평균 거래대금은 0 이상이어야 합니다.")
    if criteria.max_drop_from_high < 0 or criteria.max_pivot_gap < 0:
        raise ValueError("VCP 이격 기준은 0 이상이어야 합니다.")
    if criteria.min_contraction_segments < 1:
        raise ValueError("VCP 최소 수축 구간 수는 1 이상이어야 합니다.")
    if criteria.max_contraction_ratio <= 0:
        raise ValueError("VCP 수축 비율 기준은 0보다 커야 합니다.")
    if criteria.max_final_contraction_pct <= 0:
        raise ValueError("VCP 최종 수축폭 기준은 0보다 커야 합니다.")
    if criteria.min_volume_dry_up_ratio < 0 or criteria.min_volume_dry_up_ratio > 1:
        raise ValueError("VCP 거래량 감소율 기준은 0 이상 1 이하이어야 합니다.")
    if criteria.min_pocket_pivot_count < 0:
        raise ValueError("VCP Pocket Pivot 최소 횟수는 0 이상이어야 합니다.")


def passes_vcp_trend(
    *,
    current_price: float,
    fast_ma: float,
    mid_ma: float,
    long_ma: float,
    base_ma: float,
    criteria: VcpCriteria,
) -> bool:
    """VCP 후보가 이동평균 추세 기준을 통과하는지 확인합니다.

    Args:
        current_price: 현재 종가입니다.
        fast_ma: 단기 이동평균 값입니다.
        mid_ma: 중기 이동평균 값입니다.
        long_ma: 장기 이동평균 값입니다.
        base_ma: 기준 이동평균 값입니다.
        criteria: 이동평균 조건 사용 여부와 기간 기준입니다.

    Returns:
        설정된 이동평균 조건을 모두 만족하면 True입니다.
    """
    if criteria.require_price_above_fast_ma and current_price <= fast_ma:
        return False
    if criteria.require_price_above_mid_ma and current_price <= mid_ma:
        return False
    if criteria.require_ma_alignment and not (mid_ma > long_ma > base_ma):
        return False
    return True


def has_contracting_swings(
    swing_drops: list[float],
    *,
    min_segments: int = MIN_CONTRACTION_SEGMENTS,
    max_contraction_ratio: float = 1.0,
    max_final_contraction_pct: float = MAX_FINAL_CONTRACTION_PCT,
) -> bool:
    """최근 VCP 수축폭이 실제로 감소하는지 확인합니다.

    Args:
        swing_drops: 최근 고점-저점 구간별 하락률 목록입니다.
        min_segments: 필요한 최소 수축 구간 수입니다.
        max_contraction_ratio: 직전 수축폭 대비 허용되는 최대 비율입니다.
        max_final_contraction_pct: 마지막 수축폭의 최대 허용값입니다.

    Returns:
        최소 수축 구간 수를 만족하고 최근 수축폭이 점차 작아지면 True입니다.
    """
    if len(swing_drops) < min_segments:
        return False

    recent_count = min(len(swing_drops), max(min_segments, 3))
    recent = swing_drops[-recent_count:]
    if recent[-1] >= max_final_contraction_pct:
        return False

    for previous_drop, current_drop in zip(recent, recent[1:]):
        if current_drop >= previous_drop:
            return False
        if current_drop > previous_drop * max_contraction_ratio:
            return False
    return True


def calculate_volume_dry_up(
    volume: pd.Series,
    *,
    lookback_days: int = VOLUME_DRY_UP_LOOKBACK_DAYS,
    window: int = VOLUME_DRY_UP_WINDOW,
) -> dict[str, object]:
    """거래량이 피크 이후 충분히 말랐는지 판단할 기초 지표를 계산합니다.

    Args:
        volume: 거래량 시계열입니다.
        lookback_days: 거래량 dry-up을 볼 최근 기간입니다.
        window: 거래량 평균을 계산할 기간입니다.

    Returns:
        피크 평균 거래량, 최근 평균 거래량, 감소율, 감소 구간 존재 여부입니다.
    """
    clean_volume = pd.to_numeric(volume.tail(lookback_days), errors="coerce").dropna()
    if len(clean_volume) < window * 2:
        return {
            "peak_avg_volume": None,
            "recent_avg_volume": None,
            "dry_up_ratio": None,
            "has_declining_sequence": False,
        }

    rolling_volume = clean_volume.rolling(window=window).mean().dropna()
    if len(rolling_volume) < 2:
        return {
            "peak_avg_volume": None,
            "recent_avg_volume": None,
            "dry_up_ratio": None,
            "has_declining_sequence": False,
        }

    peak_position = int(rolling_volume.to_numpy().argmax())
    peak_avg = float(rolling_volume.iloc[peak_position])
    recent_avg = float(rolling_volume.iloc[-1])
    if peak_avg <= 0:
        dry_up_ratio = None
    else:
        dry_up_ratio = (peak_avg - recent_avg) / peak_avg

    after_peak = rolling_volume.iloc[peak_position:]
    after_peak_values = [float(value) for value in after_peak]
    has_declining_sequence = False
    for start_idx in range(0, max(len(after_peak_values) - 2, 0)):
        window_values = after_peak_values[start_idx : start_idx + 3]
        if window_values[0] > window_values[1] > window_values[2]:
            has_declining_sequence = True
            break

    return {
        "peak_avg_volume": peak_avg,
        "recent_avg_volume": recent_avg,
        "dry_up_ratio": dry_up_ratio,
        "has_declining_sequence": has_declining_sequence,
    }


def has_volume_dry_up(
    volume: pd.Series,
    *,
    lookback_days: int = VOLUME_DRY_UP_LOOKBACK_DAYS,
    window: int = VOLUME_DRY_UP_WINDOW,
    min_dry_up_ratio: float = MIN_VOLUME_DRY_UP_RATIO,
) -> bool:
    """거래량이 피크 대비 충분히 줄고 감소 구간을 만들었는지 확인합니다."""
    metrics = calculate_volume_dry_up(volume, lookback_days=lookback_days, window=window)
    dry_up_ratio = metrics["dry_up_ratio"]
    return (
        dry_up_ratio is not None
        and float(dry_up_ratio) >= min_dry_up_ratio
        and bool(metrics["has_declining_sequence"])
    )


def find_pocket_pivot_points(
    df: pd.DataFrame,
    *,
    days: int,
    volume_window: int,
) -> pd.DataFrame:
    """최근 기간에서 Pocket Pivot 발생일을 찾습니다."""
    pivot_df = df.copy()
    pivot_df["Prev_Close"] = pivot_df["Close"].shift(1)
    pivot_df["Max_Volume_Window"] = pivot_df["Volume"].shift(1).rolling(window=volume_window).max()
    recent_window = pivot_df.iloc[-days:]
    return recent_window[
        (recent_window["Close"] > recent_window["Prev_Close"])
        & (recent_window["Volume"] > recent_window["Max_Volume_Window"])
    ]


def fetch_ohlcv_data(
    symbol: str,
    *,
    start_date: datetime,
    end_date: datetime,
    limit_end_date: bool = True,
    use_project_cache: bool,
    force_refresh: bool,
) -> pd.DataFrame:
    """VCP 분석에 사용할 OHLCV 데이터를 조회합니다.

    Args:
        symbol: 종목 코드입니다.
        start_date: 조회 시작일입니다.
        end_date: 조회 종료일입니다.
        limit_end_date: 프로젝트 캐시 조회에도 종료일을 강제할지 여부입니다.
        use_project_cache: 프로젝트 MariaDB 캐시를 사용할지 여부입니다.
        force_refresh: 캐시를 무시하고 새로 조회할지 여부입니다.

    Returns:
        OHLCV 데이터프레임입니다.
    """
    if use_project_cache:
        project_main = runtime()

        config = project_main.Config(force_refresh=force_refresh)
        return project_main.fetch_ohlcv(
            symbol,
            start_date.strftime("%Y-%m-%d"),
            config,
            end_date=end_date.strftime("%Y-%m-%d") if limit_end_date else None,
        )

    return fdr.DataReader(symbol, start_date, end_date)


def bulk_rows_to_ohlcv_groups(rows: list[dict[str, object]]) -> dict[str, pd.DataFrame]:
    """MariaDB bulk OHLCV 조회 결과를 종목별 데이터프레임으로 변환합니다.

    Args:
        rows: `symbol`과 OHLCV 컬럼을 포함한 MariaDB 조회 결과 목록입니다.

    Returns:
        종목 코드를 키로 하고 표준 OHLCV 데이터프레임을 값으로 갖는 딕셔너리입니다.
    """
    if not rows:
        return {}

    df = pd.DataFrame(rows).rename(
        columns={
            "trade_date": "Date",
            "open_price": "Open",
            "high_price": "High",
            "low_price": "Low",
            "close_price": "Close",
            "volume": "Volume",
            "amount": "Amount",
            "amount_source": "AmountSource",
        }
    )
    required_columns = ["symbol", "Date", "Open", "High", "Low", "Close", "Volume"]
    if not all(column in df.columns for column in required_columns):
        return {}

    df["Date"] = pd.to_datetime(df["Date"])
    for column in ["Open", "High", "Low", "Close", "Volume", "Amount"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    price_columns = ["Open", "High", "Low", "Close"]
    df = df.dropna(subset=["Date", *price_columns])
    df = df[(df[price_columns] > 0).all(axis=1)]
    df = df.sort_values(["symbol", "Date"])

    groups: dict[str, pd.DataFrame] = {}
    value_columns = ["Date", "Open", "High", "Low", "Close", "Volume", "Amount", "AmountSource"]
    available_columns = [column for column in value_columns if column in df.columns]
    for symbol, group in df.groupby("symbol", sort=False):
        frame = group[available_columns].copy()
        frame.index = pd.to_datetime(frame.pop("Date"))
        frame = frame[~frame.index.duplicated(keep="last")]
        groups[str(symbol)] = frame
    return groups


def load_project_cache_ohlcv_bulk(universe: pd.DataFrame, start_date: datetime, *, end_date: datetime | None = None) -> dict[str, pd.DataFrame]:
    """VCP 스캔 대상 OHLCV를 MariaDB에서 bulk로 한 번에 조회합니다.

    Args:
        universe: `Code` 컬럼을 가진 VCP 스캔 대상 종목 목록입니다.
        start_date: 조회 시작일입니다.
        end_date: 조회 종료일입니다. None이면 종료일 제한이 없습니다.

    Returns:
        종목 코드를 키로 하고 OHLCV 데이터프레임을 값으로 갖는 딕셔너리입니다.
    """
    symbols = list(dict.fromkeys(str(code).zfill(6) for code in universe["Code"].dropna()))
    if not symbols:
        return {}

    project_main = runtime()
    config = project_main.Config()
    ohlcv_table, _ = project_main.db_table_names(config)
    groups: dict[str, pd.DataFrame] = {}
    chunk_size = 500

    connection = project_main.db_connection(config)
    try:
        project_main.ensure_ohlcv_cache_tables(connection, config)
        with connection.cursor() as cursor:
            for offset in range(0, len(symbols), chunk_size):
                chunk = symbols[offset : offset + chunk_size]
                placeholders = ", ".join(["%s"] * len(chunk))
                end_filter_sql = "AND trade_date <= %s" if end_date is not None else ""
                params: tuple[object, ...]
                if end_date is not None:
                    params = (*chunk, start_date.date(), end_date.date())
                else:
                    params = (*chunk, start_date.date())
                cursor.execute(
                    f"""
                    SELECT
                      symbol,
                      trade_date,
                      open_price,
                      high_price,
                      low_price,
                      close_price,
                      volume,
                      amount,
                      amount_source
                    FROM {ohlcv_table}
                    WHERE symbol IN ({placeholders})
                      AND trade_date >= %s
                      {end_filter_sql}
                      AND open_price > 0
                      AND high_price > 0
                      AND low_price > 0
                      AND close_price > 0
                    ORDER BY symbol, trade_date
                    """,
                    params,
                )
                groups.update(bulk_rows_to_ohlcv_groups(list(cursor.fetchall())))
    finally:
        connection.close()

    print(f"Loaded OHLCV from MariaDB bulk cache: {len(groups)}/{len(symbols)} symbols")
    return groups


def generate_symbol_chart(
    symbol: str,
    *,
    name: str | None,
    days: int,
    target_date: str | None,
    charts_dir: Path,
    use_project_cache: bool,
    force_refresh: bool,
) -> Path:
    """단일 종목의 실제 데이터 기반 VCP 차트를 생성합니다.

    Args:
        symbol: 종목 코드입니다.
        name: 차트에 표시할 종목명입니다. None이면 조회합니다.
        days: 조회할 과거 일수입니다.
        target_date: 차트 생성 기준일입니다. None이면 현재 기준입니다.
        charts_dir: 차트를 저장할 디렉터리입니다.
        use_project_cache: 프로젝트 MariaDB 캐시를 사용할지 여부입니다.
        force_refresh: 캐시를 무시하고 새로 조회할지 여부입니다.

    Returns:
        저장된 차트 이미지 파일 경로입니다.

    Raises:
        ValueError: 차트 생성에 필요한 OHLCV 데이터가 부족한 경우 발생합니다.
    """
    end_date = resolve_target_datetime(target_date)
    start_date = end_date - timedelta(days=days)
    df = fetch_ohlcv_data(
        symbol,
        start_date=start_date,
        end_date=end_date,
        limit_end_date=target_date is not None,
        use_project_cache=use_project_cache,
        force_refresh=force_refresh,
    )
    if df is None or len(df) < 120:
        raise ValueError(f"Not enough OHLCV data for {symbol}: rows={0 if df is None else len(df)}")

    high_52w = float(df["High"].rolling(window=252, min_periods=1).max().iloc[-1])
    current_price = float(df["Close"].iloc[-1])
    drop_pct = ((high_52w - current_price) / high_52w * 100) if high_52w else 0.0
    vcp_stage = classify_vcp_stage(drop_pct)
    swing_segments = calculate_swing_segments(df["Close"])
    pocket_pivot_points = find_pocket_pivot_points(df, days=14, volume_window=10)

    return save_vcp_chart(
        df,
        name=name or get_symbol_name(symbol),
        symbol=symbol,
        vcp_stage=vcp_stage,
        drop_pct=drop_pct,
        swing_segments=swing_segments,
        pocket_pivot_points=pocket_pivot_points,
        output_dir=charts_dir,
    )


def run_vcp_engine(
    *,
    max_symbols: int | None = None,
    min_avg_traded_value: int = 15_000_000_000,
    max_drop_from_high: float = MAX_DROP_FROM_HIGH,
    max_pivot_gap: float = MAX_PIVOT_GAP,
    criteria: VcpCriteria | None = None,
    days: int = 400,
    target_date: str | None = None,
    save_charts: bool = True,
    charts_dir: Path | None = None,
    use_project_cache: bool = True,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """전체 유니버스에서 VCP 조건을 만족하는 후보를 찾습니다.

    Args:
        max_symbols: 테스트용 최대 종목 수입니다.
        min_avg_traded_value: 20일 평균 거래대금 최소 기준입니다.
        days: 조회할 과거 일수입니다.
        target_date: VCP 계산 기준일입니다. None이면 현재 기준입니다.
        save_charts: 후보 차트 이미지를 저장할지 여부입니다.
        charts_dir: 차트 이미지를 저장할 디렉터리입니다.
        use_project_cache: 프로젝트 MariaDB 캐시를 사용할지 여부입니다.
        force_refresh: 캐시를 무시하고 새로 조회할지 여부입니다.

    Returns:
        VCP 후보 정보를 담은 데이터프레임입니다.
    """
    if criteria is None:
        criteria = VcpCriteria(
            min_avg_traded_value=min_avg_traded_value,
            max_drop_from_high=max_drop_from_high,
            max_pivot_gap=max_pivot_gap,
        )
    validate_vcp_criteria(criteria)

    universe = get_clean_universe(max_symbols=max_symbols)

    end_date = resolve_target_datetime(target_date)
    start_date = end_date - timedelta(days=days)
    bulk_ohlcv_by_symbol: dict[str, pd.DataFrame] | None = None
    if use_project_cache and not force_refresh:
        bulk_ohlcv_by_symbol = load_project_cache_ohlcv_bulk(universe, start_date, end_date=end_date)

    vcp_candidates: list[dict[str, object]] = []

    iterator = tqdm(universe.iterrows(), total=len(universe), desc="Scanning VCP")
    for _, row in iterator:
        symbol = str(row["Code"]).zfill(6)
        name = row["Name"]

        try:
            if bulk_ohlcv_by_symbol is not None:
                df = bulk_ohlcv_by_symbol.get(symbol)
            else:
                df = fetch_ohlcv_data(
                    symbol,
                    start_date=start_date,
                    end_date=end_date,
                    limit_end_date=target_date is not None,
                    use_project_cache=use_project_cache,
                    force_refresh=force_refresh,
                )
            if df is None or len(df) < 200:
                continue

            df = df.copy()
            df["Traded_Value"] = df["Amount"].fillna(df["Close"] * df["Volume"]) if "Amount" in df.columns else df["Close"] * df["Volume"]
            avg_traded_value_20 = df["Traded_Value"].rolling(window=20).mean().iloc[-1]
            if not is_finite_number(avg_traded_value_20):
                continue
            if avg_traded_value_20 < criteria.min_avg_traded_value:
                continue

            df["SMA_FAST"] = df["Close"].rolling(window=criteria.fast_ma_window).mean()
            df["SMA_MID"] = df["Close"].rolling(window=criteria.mid_ma_window).mean()
            df["SMA_LONG"] = df["Close"].rolling(window=criteria.long_ma_window).mean()
            df["SMA_BASE"] = df["Close"].rolling(window=criteria.base_ma_window).mean()
            df["High_Window"] = df["High"].rolling(window=criteria.high_window).max()

            current_price = float(df["Close"].iloc[-1])
            sma_fast = float(df["SMA_FAST"].iloc[-1])
            sma_mid = float(df["SMA_MID"].iloc[-1])
            sma_long = float(df["SMA_LONG"].iloc[-1])
            sma_base = float(df["SMA_BASE"].iloc[-1])
            high_52w = float(df["High_Window"].iloc[-1])
            required_values = [current_price, high_52w]
            if criteria.require_price_above_fast_ma:
                required_values.append(sma_fast)
            if criteria.require_price_above_mid_ma:
                required_values.append(sma_mid)
            if criteria.require_ma_alignment:
                required_values.extend([sma_mid, sma_long, sma_base])
            if not all(is_finite_number(value) for value in required_values):
                continue
            if high_52w <= 0:
                continue

            if not passes_vcp_trend(
                current_price=current_price,
                fast_ma=sma_fast,
                mid_ma=sma_mid,
                long_ma=sma_long,
                base_ma=sma_base,
                criteria=criteria,
            ):
                continue

            drop_from_high = (high_52w - current_price) / high_52w
            if not is_finite_number(drop_from_high):
                continue
            if drop_from_high > criteria.max_drop_from_high:
                continue

            drop_pct = drop_from_high * 100
            vcp_stage = classify_vcp_stage(drop_pct)

            swing_segments = calculate_swing_segments(df["Close"])
            swing_drops = [float(segment["drop_pct"]) for segment in swing_segments if is_finite_number(segment["drop_pct"])]
            if not has_contracting_swings(
                swing_drops,
                min_segments=criteria.min_contraction_segments,
                max_contraction_ratio=criteria.max_contraction_ratio,
                max_final_contraction_pct=criteria.max_final_contraction_pct,
            ):
                continue

            volume_dry_up = calculate_volume_dry_up(
                df["Volume"],
                lookback_days=criteria.volume_dry_up_lookback_days,
                window=criteria.volume_dry_up_window,
            )
            dry_up_ratio = volume_dry_up["dry_up_ratio"]
            if (
                dry_up_ratio is None
                or float(dry_up_ratio) < criteria.min_volume_dry_up_ratio
                or not bool(volume_dry_up["has_declining_sequence"])
            ):
                continue

            recent_high = float(df["High"].tail(criteria.recent_high_window).max())
            if not is_finite_number(recent_high) or recent_high <= 0:
                continue
            pivot_gap = (recent_high - current_price) / recent_high
            if not is_finite_number(pivot_gap) or pivot_gap > criteria.max_pivot_gap:
                continue

            pocket_pivot_points = find_pocket_pivot_points(
                df,
                days=criteria.pocket_pivot_days,
                volume_window=criteria.pocket_volume_window,
            )
            if len(pocket_pivot_points) < criteria.min_pocket_pivot_count:
                continue

            display_name = resolve_symbol_name(str(symbol), name)
            contraction_ratio = None
            if len(swing_drops) >= 2 and swing_drops[-2] != 0:
                contraction_ratio = swing_drops[-1] / swing_drops[-2]
            chart_path = ""
            if save_charts and charts_dir is not None:
                chart_path = str(
                    save_vcp_chart(
                        df,
                        name=display_name,
                        symbol=symbol,
                        vcp_stage=vcp_stage,
                        drop_pct=drop_pct,
                        swing_segments=swing_segments,
                        pocket_pivot_points=pocket_pivot_points,
                        output_dir=charts_dir,
                        high_window=criteria.high_window,
                    )
                )

            vcp_candidates.append(
                {
                    "name": display_name,
                    "code": symbol,
                    "current_price": int(current_price),
                    "drop_from_52w_high_pct": round(drop_pct, 2),
                    "vcp_stage": vcp_stage,
                    "recent_swing_drops_pct": swing_drops,
                    "pocket_pivot_count": len(pocket_pivot_points),
                    "pocket_pivot_count_14d": len(pocket_pivot_points),
                    "avg_traded_value_20": int(avg_traded_value_20),
                    "pivot_gap_pct": round(pivot_gap * 100, 2),
                    "contraction_ratio": round(contraction_ratio, 3) if contraction_ratio is not None else None,
                    "final_contraction_pct": round(swing_drops[-1], 2) if swing_drops else None,
                    "volume_dry_up_pct": round(float(dry_up_ratio) * 100, 2),
                    "volume_peak_avg": int(float(volume_dry_up["peak_avg_volume"] or 0)),
                    "recent_volume_avg": int(float(volume_dry_up["recent_avg_volume"] or 0)),
                    "price_vs_fast_ma_pct": round((current_price / sma_fast - 1) * 100, 2) if sma_fast else None,
                    "price_vs_mid_ma_pct": round((current_price / sma_mid - 1) * 100, 2) if sma_mid else None,
                    "recent_high_window": criteria.recent_high_window,
                    "pocket_pivot_days": criteria.pocket_pivot_days,
                    "chart_path": chart_path,
                }
            )
        except Exception as exc:  # noqa: BLE001 - skip per-symbol data issues
            print(f"Skip {name}({symbol}): {exc}")
            continue

    return pd.DataFrame(vcp_candidates)


def prepare_vcp_records(candidates: pd.DataFrame) -> list[dict[str, object]]:
    """VCP 후보 데이터프레임을 DB 저장 가능한 record 목록으로 정리합니다.

    Args:
        candidates: VCP 후보 데이터프레임입니다.

    Returns:
        필수 숫자 필드가 유효하고 JSON 직렬화가 안전한 record 목록입니다.
    """
    if candidates.empty:
        return []

    records: list[dict[str, object]] = []
    required_numbers = ("current_price", "drop_from_52w_high_pct", "avg_traded_value_20")
    for record in candidates.to_dict("records"):
        if not all(is_finite_number(record.get(field)) for field in required_numbers):
            continue
        records.append(sanitize_json_value(record))
    return records


def save_vcp_results_to_db(
    candidates: pd.DataFrame,
    *,
    started_at: datetime,
    elapsed_seconds: float,
    params: dict[str, object],
) -> int:
    """VCP 실행 이력과 후보 결과를 MariaDB에 저장합니다.

    Args:
        candidates: VCP 후보 데이터프레임입니다.
        started_at: VCP 스캔 시작 시각입니다.
        elapsed_seconds: VCP 스캔 소요 시간입니다.
        params: 실행 파라미터입니다.

    Returns:
        저장된 VCP 실행 이력의 ID입니다.
    """
    project_main = runtime()

    config = project_main.Config()
    prefix = project_main.db_table_prefix(config)
    runs_table, candidates_table = db_scheme.vcp_table_names(prefix)
    records = prepare_vcp_records(candidates)

    connection = project_main.db_connection(config)
    try:
        db_scheme.ensure_vcp_result_tables(connection, prefix)
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {runs_table} (
                  started_at, elapsed_seconds, candidates_total, params_json
                )
                VALUES (%s, %s, %s, %s)
                """,
                (
                    started_at,
                    elapsed_seconds,
                    len(records),
                    json.dumps(params, ensure_ascii=False, default=str),
                ),
            )
            run_id = int(cursor.lastrowid)

            if records:
                cursor.executemany(
                    f"""
                    INSERT INTO {candidates_table} (
                      run_id, rank_no, code, name, current_price,
                      drop_from_52w_high_pct, avg_traded_value_20,
                      candidate_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            run_id,
                            rank_no,
                            record["code"],
                            record["name"],
                            record["current_price"],
                            record["drop_from_52w_high_pct"],
                            record["avg_traded_value_20"],
                            json.dumps(record, ensure_ascii=False, default=str, allow_nan=False),
                        )
                        for rank_no, record in enumerate(records, start=1)
                    ],
                )
        connection.commit()
        return run_id
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def run_vcp_scan(
    *,
    max_symbols: int | None = None,
    min_avg_traded_value: int = 15_000_000_000,
    max_drop_from_high: float = MAX_DROP_FROM_HIGH,
    max_pivot_gap: float = MAX_PIVOT_GAP,
    criteria: VcpCriteria | None = None,
    days: int = 400,
    target_date: str | None = None,
    save_charts: bool = True,
    charts_dir: Path | None = None,
    use_project_cache: bool = True,
    force_refresh: bool = False,
) -> tuple[pd.DataFrame, int, float]:
    """VCP 엔진 실행과 DB 저장을 한 번에 수행합니다.

    Args:
        max_symbols: 테스트용 최대 종목 수입니다.
        min_avg_traded_value: 20일 평균 거래대금 최소 기준입니다.
        days: 조회할 과거 일수입니다.
        target_date: VCP 계산 기준일입니다. None이면 현재 기준입니다.
        save_charts: 후보 차트 이미지를 저장할지 여부입니다.
        charts_dir: 차트 이미지를 저장할 디렉터리입니다.
        use_project_cache: 프로젝트 MariaDB 캐시를 사용할지 여부입니다.
        force_refresh: 캐시를 무시하고 새로 조회할지 여부입니다.

    Returns:
        후보 데이터프레임, DB 실행 이력 ID, 소요 시간 초입니다.
    """
    started_at_dt = datetime.now()
    started_timer = time.perf_counter()
    if criteria is None:
        criteria = VcpCriteria(
            min_avg_traded_value=min_avg_traded_value,
            max_drop_from_high=max_drop_from_high,
            max_pivot_gap=max_pivot_gap,
        )
    validate_vcp_criteria(criteria)

    candidates = run_vcp_engine(
        max_symbols=max_symbols,
        criteria=criteria,
        days=days,
        target_date=target_date,
        save_charts=save_charts,
        charts_dir=charts_dir,
        use_project_cache=use_project_cache,
        force_refresh=force_refresh,
    )
    elapsed_seconds = time.perf_counter() - started_timer
    candidates = candidates.sort_values(by="drop_from_52w_high_pct") if not candidates.empty else candidates

    run_id = save_vcp_results_to_db(
        candidates,
        started_at=started_at_dt,
        elapsed_seconds=elapsed_seconds,
        params={
            "max_symbols": max_symbols,
            "criteria": asdict(criteria),
            "days": days,
            "target_date": target_date,
            "save_charts": save_charts,
            "use_project_cache": use_project_cache,
            "force_refresh": force_refresh,
        },
    )
    return candidates, run_id, elapsed_seconds


def parse_args() -> argparse.Namespace:
    """VCP 스캐너 CLI 인자를 파싱합니다.

    Returns:
        argparse로 파싱된 VCP 실행 옵션입니다.
    """
    parser = argparse.ArgumentParser(description="VCP scanner runner.")
    parser.add_argument("--max-symbols", type=int, default=None, help="Limit symbols for a quick test run.")
    parser.add_argument("--symbol", type=str, default=None, help="Generate one real-data chart for this stock code.")
    parser.add_argument("--name", type=str, default=None, help="Display name for --symbol chart.")
    parser.add_argument("--days", type=int, default=400, help="Historical calendar days to request.")
    parser.add_argument("--target-date", type=str, default=None, help="Run as of this date after market close (YYYY-MM-DD).")
    parser.add_argument(
        "--min-avg-traded-value",
        type=int,
        default=15_000_000_000,
        help="Minimum 20-day average traded value. Default: 15,000,000,000.",
    )
    parser.add_argument(
        "--max-drop-from-high",
        type=float,
        default=MAX_DROP_FROM_HIGH,
        help="Maximum drop from 52-week high as a ratio. Default: 0.18.",
    )
    parser.add_argument(
        "--max-pivot-gap",
        type=float,
        default=MAX_PIVOT_GAP,
        help="Maximum gap from recent 20-day high as a ratio. Default: 0.15.",
    )
    parser.add_argument("--min-contraction-segments", type=int, default=MIN_CONTRACTION_SEGMENTS)
    parser.add_argument("--max-contraction-ratio", type=float, default=1.0)
    parser.add_argument("--max-final-contraction-pct", type=float, default=MAX_FINAL_CONTRACTION_PCT)
    parser.add_argument("--high-window", type=int, default=252)
    parser.add_argument("--recent-high-window", type=int, default=20)
    parser.add_argument("--volume-dry-up-lookback-days", type=int, default=VOLUME_DRY_UP_LOOKBACK_DAYS)
    parser.add_argument("--volume-dry-up-window", type=int, default=VOLUME_DRY_UP_WINDOW)
    parser.add_argument("--min-volume-dry-up-ratio", type=float, default=MIN_VOLUME_DRY_UP_RATIO)
    parser.add_argument("--fast-ma-window", type=int, default=20)
    parser.add_argument("--mid-ma-window", type=int, default=50)
    parser.add_argument("--long-ma-window", type=int, default=150)
    parser.add_argument("--base-ma-window", type=int, default=200)
    parser.add_argument("--require-price-above-fast-ma", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--require-price-above-mid-ma", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--require-ma-alignment", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--pocket-pivot-days", type=int, default=14)
    parser.add_argument("--pocket-volume-window", type=int, default=10)
    parser.add_argument("--min-pocket-pivot-count", type=int, default=1)
    parser.add_argument(
        "--charts-dir",
        type=Path,
        default=DEFAULT_CHARTS_DIR,
        help="Directory for generated chart PNG files.",
    )
    parser.add_argument("--no-charts", action="store_true", help="Skip chart PNG generation.")
    parser.add_argument("--no-project-cache", action="store_true", help="Do not reuse main.py MariaDB OHLCV cache.")
    parser.add_argument("--force-refresh", action="store_true", help="Ignore project cache and refresh OHLCV data.")
    return parser.parse_args()


def main() -> int:
    """VCP 스캐너 단독 실행 진입점입니다.

    Returns:
        프로세스 종료 코드입니다.
    """
    args = parse_args()

    if args.symbol:
        chart_path = generate_symbol_chart(
            args.symbol,
            name=args.name,
            days=args.days,
            target_date=args.target_date,
            charts_dir=args.charts_dir,
            use_project_cache=not args.no_project_cache,
            force_refresh=args.force_refresh,
        )
        print(f"Saved real-data chart: {chart_path.resolve()}")
        return 0

    print("Starting VCP scanner...\n")
    criteria = VcpCriteria(
        min_avg_traded_value=args.min_avg_traded_value,
        max_drop_from_high=args.max_drop_from_high,
        max_pivot_gap=args.max_pivot_gap,
        min_contraction_segments=args.min_contraction_segments,
        max_contraction_ratio=args.max_contraction_ratio,
        max_final_contraction_pct=args.max_final_contraction_pct,
        high_window=args.high_window,
        recent_high_window=args.recent_high_window,
        volume_dry_up_lookback_days=args.volume_dry_up_lookback_days,
        volume_dry_up_window=args.volume_dry_up_window,
        min_volume_dry_up_ratio=args.min_volume_dry_up_ratio,
        fast_ma_window=args.fast_ma_window,
        mid_ma_window=args.mid_ma_window,
        long_ma_window=args.long_ma_window,
        base_ma_window=args.base_ma_window,
        require_price_above_fast_ma=args.require_price_above_fast_ma,
        require_price_above_mid_ma=args.require_price_above_mid_ma,
        require_ma_alignment=args.require_ma_alignment,
        pocket_pivot_days=args.pocket_pivot_days,
        pocket_volume_window=args.pocket_volume_window,
        min_pocket_pivot_count=args.min_pocket_pivot_count,
    )
    final_stocks, vcp_run_id, elapsed_seconds = run_vcp_scan(
        max_symbols=args.max_symbols,
        criteria=criteria,
        days=args.days,
        target_date=args.target_date,
        save_charts=not args.no_charts,
        charts_dir=args.charts_dir,
        use_project_cache=not args.no_project_cache,
        force_refresh=args.force_refresh,
    )

    print("\n=======================================================")
    print(f"VCP scan complete. Candidates: {len(final_stocks)}")
    print(f"Elapsed time: {elapsed_seconds:.1f} seconds ({elapsed_seconds / 60:.1f} minutes)")
    print("=======================================================\n")

    print(f"Saved DB Run ID: {vcp_run_id}")

    if final_stocks.empty:
        print("No candidates matched the current conditions.")
        return 0

    print(final_stocks.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
