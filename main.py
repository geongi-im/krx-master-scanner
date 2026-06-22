#!/usr/bin/env python3
"""
KRX Master Scanner

기존 Jupyter/Colab 단일 셀 스캐너를 로컬 실행 가능한 Python 스크립트로 정리한 버전.
- Telegram token/chat_id는 .env에서 읽는다.
- FinanceDataReader OHLCV 호출은 MariaDB 캐시 + 재시도/backoff를 사용한다.
- 종목별 성공/스킵/실패 카운트와 결과를 MariaDB에 남긴다.
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
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import FinanceDataReader as fdr
import matplotlib as mpl

# 반드시 pyplot/mplfinance import 전에 Agg 설정
mpl.use("Agg")

import matplotlib.font_manager as fm
import pandas as pd
import requests
from dotenv import load_dotenv

import db_scheme
from analysis import ScanResult, analyze_stock, build_message, check_market_regime, generate_chart, get_stock_details

warnings.filterwarnings("ignore", category=FutureWarning)

KST = timezone(timedelta(hours=9))
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
CHART_DIR = DATA_DIR / "charts"
LOG_DIR = APP_DIR / "logs"
ASSET_DIR = APP_DIR / "assets"
CACHE_START_TOLERANCE_DAYS = 7
VCP_LOOKBACK_DAYS = 400
CHART_RETENTION_DAYS = 3
TARGET_CACHE_MIN_COVERAGE_RATIO = 0.90
TARGET_CACHE_MIN_READY_SYMBOLS = 1000

for directory in (DATA_DIR, CHART_DIR, LOG_DIR, ASSET_DIR):
    directory.mkdir(parents=True, exist_ok=True)

load_dotenv(APP_DIR / ".env")


def env_bool(name: str, default: bool) -> bool:
    """환경변수 문자열을 bool 값으로 변환합니다.

    Args:
        name: 읽을 환경변수 이름입니다.
        default: 환경변수가 없을 때 사용할 기본값입니다.

    Returns:
        환경변수가 참 값 문자열이면 True, 그 외에는 False입니다.
    """
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    """환경변수 문자열을 int 값으로 변환합니다.

    Args:
        name: 읽을 환경변수 이름입니다.
        default: 환경변수가 없거나 비어 있을 때 사용할 기본값입니다.

    Returns:
        변환된 정수 값입니다.
    """
    value = os.getenv(name)
    if not value:
        return default
    return int(value)


def env_float(name: str, default: float) -> float:
    """환경변수 문자열을 float 값으로 변환합니다.

    Args:
        name: 읽을 환경변수 이름입니다.
        default: 환경변수가 없거나 비어 있을 때 사용할 기본값입니다.

    Returns:
        변환된 실수 값입니다.
    """
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

    db_host: str = os.getenv("DB_HOST", "127.0.0.1")
    db_port: int = env_int("DB_PORT", 3306)
    db_name: str | None = os.getenv("DB_NAME")
    db_user: str | None = os.getenv("DB_USER")
    db_password: str | None = os.getenv("DB_PASSWORD")
    db_table_prefix: str = os.getenv("DB_TABLE_PREFIX", "kms")

    first_pass_min_close: int = env_int("FIRST_PASS_MIN_CLOSE", 500)
    first_pass_min_amount: int = env_int("FIRST_PASS_MIN_AMOUNT", 1_000_000_000)
    min_avg_turnover: int = env_int("MIN_AVG_TURNOVER", 1_000_000_000)
    min_adr: float = env_float("MIN_ADR", 1.5)

    top_send_limit: int = env_int("TOP_SEND_LIMIT", 20)
    send_charts: bool = env_bool("SEND_CHARTS", True)
    force_refresh: bool = env_bool("FORCE_REFRESH", False)
    target_date: str | None = None

    collect_enabled: bool = env_bool("COLLECT_ENABLED", True)
    collect_days: int = env_int("COLLECT_DAYS", 600)

    vcp_enabled: bool = field(default_factory=lambda: env_bool("VCP_ENABLED", True))
    vcp_min_avg_traded_value: int = field(default_factory=lambda: env_int("VCP_MIN_AVG_TRADED_VALUE", 15_000_000_000))
    vcp_max_drop_from_high: float = field(default_factory=lambda: env_float("VCP_MAX_DROP_FROM_HIGH", 0.18))
    vcp_max_pivot_gap: float = field(default_factory=lambda: env_float("VCP_MAX_PIVOT_GAP", 0.15))
    vcp_max_contraction_ratio: float = field(default_factory=lambda: env_float("VCP_MAX_CONTRACTION_RATIO", 1.0))
    vcp_min_pocket_pivot_count: int = field(default_factory=lambda: env_int("VCP_MIN_POCKET_PIVOT_COUNT", 1))

    @property
    def telegram_enabled(self) -> bool:
        """텔레그램 전송 설정이 실제 값으로 채워져 있는지 확인합니다.

        Returns:
            봇 토큰과 채팅 ID가 모두 유효하면 True입니다.
        """
        if not self.telegram_bot_token or not self.telegram_chat_id:
            return False
        placeholders = {"YOUR_BOT_TOKEN_HERE", "YOUR_CHAT_ID_HERE", ""}
        return self.telegram_bot_token not in placeholders and self.telegram_chat_id not in placeholders


def parse_target_date(value: str | None) -> str | None:
    """CLI 기준일 문자열을 YYYY-MM-DD 형식으로 검증해 반환합니다."""
    if value is None or not str(value).strip():
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--target-date는 YYYY-MM-DD 형식이어야 합니다.") from exc


def config_target_datetime(config: Config) -> datetime:
    """실행 기준 시각을 반환합니다. 기준일 지정 시 해당일 장마감 이후로 간주합니다."""
    if config.target_date:
        parsed = datetime.strptime(config.target_date, "%Y-%m-%d").date()
        return datetime(parsed.year, parsed.month, parsed.day, 20, 0, tzinfo=KST)
    return datetime.now(KST)


def config_end_date(config: Config) -> str | None:
    """OHLCV 조회 종료일 문자열을 반환합니다. 실시간 실행은 None입니다."""
    return config.target_date


def window_start_date(config: Config, days: int) -> str:
    """실행 기준일에서 지정 일수만큼 이전 날짜 문자열을 반환합니다."""
    return (config_target_datetime(config) - timedelta(days=days)).strftime("%Y-%m-%d")


def format_target_date(config: Config) -> str:
    """텔레그램 메시지에 표시할 타겟 날짜를 한국어 날짜/요일 형식으로 반환합니다."""
    weekday_names = ("월", "화", "수", "목", "금", "토", "일")
    target = config_target_datetime(config).date()
    return f"{target.year}년 {target.month}월 {target.day}일({weekday_names[target.weekday()]})"


def setup_logging() -> logging.Logger:
    """파일과 콘솔에 기록하는 애플리케이션 로거를 설정합니다.

    Returns:
        KRX 마스터 스캐너에서 사용할 로거입니다.
    """
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


def cleanup_old_chart_images(charts_dir: Path, *, now_ts: float | None = None) -> int:
    """차트 디렉터리에서 3일이 지난 PNG 이미지를 삭제합니다."""
    charts_dir.mkdir(parents=True, exist_ok=True)
    cutoff_ts = (time.time() if now_ts is None else now_ts) - (CHART_RETENTION_DAYS * 24 * 60 * 60)
    deleted = 0

    for image_path in charts_dir.glob("*.png"):
        try:
            if image_path.is_file() and image_path.stat().st_mtime < cutoff_ts:
                image_path.unlink()
                deleted += 1
        except OSError as exc:
            logger.warning("오래된 차트 이미지 삭제 실패: %s (%s)", image_path, exc)

    if deleted:
        logger.info("오래된 차트 이미지 삭제: dir=%s deleted=%s retention_days=%s", charts_dir, deleted, CHART_RETENTION_DAYS)
    return deleted


def setup_korean_font() -> None:
    """차트 이미지에서 한글이 깨지지 않도록 matplotlib 폰트를 설정합니다.

    Raises:
        requests.HTTPError: Linux 환경에서 한글 폰트 다운로드에 실패한 경우 발생합니다.
    """
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
    """파일명에 안전하지 않은 문자를 밑줄로 치환합니다.

    Args:
        value: 파일명으로 사용할 원본 문자열입니다.

    Returns:
        파일명에 사용할 수 있는 문자만 남긴 문자열입니다.
    """
    return re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", value)


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV 데이터프레임의 인덱스와 숫자 컬럼을 분석 가능한 형태로 정리합니다.

    Args:
        df: 원본 OHLCV 데이터프레임입니다.

    Returns:
        날짜 인덱스 정렬, 중복 제거, 숫자 변환, 0 이하 OHLC 제거를 마친 데이터프레임입니다.
    """
    if df.empty:
        return df
    normalized = df.copy()
    normalized.index = pd.to_datetime(normalized.index)
    normalized = normalized.sort_index()
    normalized = normalized[~normalized.index.duplicated(keep="last")]
    price_columns = ["Open", "High", "Low", "Close"]
    if all(column in normalized.columns for column in price_columns):
        numeric_columns = [column for column in [*price_columns, "Volume", "Amount"] if column in normalized.columns]
        for column in numeric_columns:
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
        normalized = normalized.dropna(subset=price_columns)
        normalized = normalized[(normalized[price_columns] > 0).all(axis=1)]
    return normalized


def db_table_prefix(config: Config) -> str:
    """환경설정의 DB 테이블 접두사를 검증하고 정리합니다.

    Args:
        config: DB 테이블 접두사를 포함한 실행 설정입니다.

    Returns:
        끝의 밑줄을 제거한 유효한 테이블 접두사입니다.

    Raises:
        RuntimeError: 접두사가 허용된 식별자 형식이 아닌 경우 발생합니다.
    """
    try:
        return db_scheme.sanitize_table_prefix(config.db_table_prefix)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def db_table_names(config: Config) -> tuple[str, str]:
    """OHLCV 캐시와 메타 테이블명을 생성합니다.

    Args:
        config: DB 테이블 접두사를 포함한 실행 설정입니다.

    Returns:
        OHLCV 테이블명과 캐시 메타 테이블명입니다.
    """
    return db_scheme.ohlcv_table_names(db_table_prefix(config))


def ensure_ohlcv_cache_tables(connection, config: Config) -> None:
    """OHLCV 캐시 테이블이 없으면 생성합니다."""
    db_scheme.ensure_ohlcv_cache_tables(connection, db_table_prefix(config))


def db_connection(config: Config):
    """MariaDB 연결 객체를 생성합니다.

    Args:
        config: DB 접속정보와 타임아웃 설정입니다.

    Returns:
        PyMySQL 연결 객체입니다.

    Raises:
        RuntimeError: 필수 DB 환경변수가 없거나 PyMySQL이 설치되지 않은 경우 발생합니다.
    """
    missing = [
        name
        for name, value in (
            ("DB_NAME", config.db_name),
            ("DB_USER", config.db_user),
            ("DB_PASSWORD", config.db_password),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"MariaDB 접속 환경변수가 없습니다: {', '.join(missing)}")

    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError("PyMySQL이 필요합니다. `pip install -r requirements.txt`를 실행하세요.") from exc

    return pymysql.connect(
        host=config.db_host,
        port=config.db_port,
        user=config.db_user,
        password=config.db_password,
        database=config.db_name,
        charset="utf8mb4",
        connect_timeout=config.request_timeout,
        read_timeout=max(config.request_timeout, 20),
        write_timeout=max(config.request_timeout, 20),
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def ohlcv_from_db_rows(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """MariaDB OHLCV 조회 결과를 분석용 데이터프레임으로 변환합니다.

    Args:
        rows: DictCursor로 조회한 OHLCV 행 목록입니다.

    Returns:
        DateTimeIndex와 표준 OHLCV 컬럼을 가진 데이터프레임입니다.
    """
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume", "Amount", "AmountSource"])

    df = pd.DataFrame(rows)
    df = df.rename(
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
    df.index = pd.to_datetime(df.pop("Date"))
    return normalize_ohlcv(df)


def decimal_value(value: object) -> Decimal:
    """값을 Decimal로 변환합니다.

    Args:
        value: Decimal로 변환할 값입니다.

    Returns:
        문자열 기반으로 변환한 Decimal 값입니다.
    """
    return Decimal(str(value))


def int_value(value: object) -> int:
    """값을 정수로 변환합니다.

    Args:
        value: 정수로 변환할 값입니다.

    Returns:
        Decimal 경유로 변환한 정수 값입니다.
    """
    return int(Decimal(str(value)))


def ohlcv_db_params(symbol: str, df: pd.DataFrame) -> list[tuple[object, ...]]:
    """OHLCV 데이터프레임을 upsert용 파라미터 목록으로 변환합니다.

    Args:
        symbol: 종목 코드입니다.
        df: 저장할 OHLCV 데이터프레임입니다.

    Returns:
        MariaDB executemany에 전달할 튜플 목록입니다.
    """
    params: list[tuple[object, ...]] = []
    for trade_date, row in df.iterrows():
        open_price = decimal_value(row["Open"])
        high_price = decimal_value(row["High"])
        low_price = decimal_value(row["Low"])
        close_price = decimal_value(row["Close"])
        volume = int_value(row["Volume"]) if "Volume" in row.index and pd.notna(row["Volume"]) else 0

        if "Amount" in row.index and pd.notna(row["Amount"]):
            amount = int_value(row["Amount"])
            amount_source = str(row["AmountSource"]) if "AmountSource" in row.index and pd.notna(row["AmountSource"]) else "source"
        else:
            amount = int((close_price * Decimal(volume)).to_integral_value())
            amount_source = "computed"

        params.append(
            (
                symbol,
                pd.Timestamp(trade_date).date(),
                open_price,
                high_price,
                low_price,
                close_price,
                volume,
                amount,
                amount_source,
            )
        )
    return params


def rebuild_ohlcv_cache_meta(connection, config: Config, *, symbol: str | None = None) -> int:
    """실제 유효 OHLCV row 기준으로 캐시 메타 정보를 재계산합니다."""
    ohlcv_table, meta_table = db_table_names(config)
    valid_where = """
        open_price > 0
        AND high_price > 0
        AND low_price > 0
        AND close_price > 0
    """
    symbol_filter = "AND symbol = %s" if symbol is not None else ""
    params: tuple[object, ...] = (symbol,) if symbol is not None else ()

    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT COUNT(DISTINCT symbol) AS symbol_count
            FROM {ohlcv_table}
            WHERE {valid_where}
              {symbol_filter}
            """,
            params,
        )
        updated_symbols = int(cursor.fetchone()["symbol_count"] or 0)

        if symbol is not None:
            cursor.execute(
                f"""
                DELETE FROM {meta_table}
                WHERE symbol = %s
                  AND NOT EXISTS (
                    SELECT 1
                    FROM {ohlcv_table}
                    WHERE symbol = %s
                      AND {valid_where}
                  )
                """,
                (symbol, symbol),
            )

        cursor.execute(
            f"""
            INSERT INTO {meta_table} (
              symbol, min_trade_date, max_trade_date, row_count, last_cached_at
            )
            SELECT
              symbol,
              MIN(trade_date) AS min_trade_date,
              MAX(trade_date) AS max_trade_date,
              COUNT(*) AS row_count,
              NOW() AS last_cached_at
            FROM {ohlcv_table}
            WHERE {valid_where}
              {symbol_filter}
            GROUP BY symbol
            ON DUPLICATE KEY UPDATE
              min_trade_date = VALUES(min_trade_date),
              max_trade_date = VALUES(max_trade_date),
              row_count = VALUES(row_count),
              last_cached_at = VALUES(last_cached_at),
              updated_at = CURRENT_TIMESTAMP
            """,
            params,
        )
    return updated_symbols


def delete_invalid_ohlcv_rows(connection, config: Config) -> int:
    """분석에 사용할 수 없는 OHLC row를 캐시 테이블에서 삭제합니다."""
    ohlcv_table, _ = db_table_names(config)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            DELETE FROM {ohlcv_table}
            WHERE open_price <= 0
               OR high_price <= 0
               OR low_price <= 0
               OR close_price <= 0
            """
        )
        return int(cursor.rowcount)


def repair_ohlcv_cache(config: Config) -> dict[str, int]:
    """기존 OHLCV 캐시 오염 row를 제거하고 메타 정보를 재계산합니다."""
    connection = db_connection(config)
    try:
        ensure_ohlcv_cache_tables(connection, config)
        deleted_invalid_rows = delete_invalid_ohlcv_rows(connection, config)
        updated_meta_symbols = rebuild_ohlcv_cache_meta(connection, config)
        connection.commit()
        return {
            "deleted_invalid_rows": deleted_invalid_rows,
            "updated_meta_symbols": updated_meta_symbols,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def read_cached_ohlcv(symbol: str, start_date: str, config: Config, *, end_date: str | None = None) -> pd.DataFrame | None:
    """MariaDB에서 요청 시작일 이후의 OHLCV 캐시를 읽습니다.

    Args:
        symbol: 조회할 종목 코드입니다.
        start_date: 필요한 데이터의 시작일입니다.
        config: DB 접속정보와 캐시 설정입니다.
        end_date: 필요한 데이터의 종료일입니다. None이면 종료일 제한이 없습니다.

    Returns:
        캐시가 유효하면 OHLCV 데이터프레임, 사용할 캐시가 없으면 None입니다.
    """
    if config.force_refresh:
        return None

    requested_start = pd.Timestamp(start_date)
    requested_end = pd.Timestamp(end_date) if end_date else None
    ohlcv_table, meta_table = db_table_names(config)
    connection = db_connection(config)
    try:
        ensure_ohlcv_cache_tables(connection, config)
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT min_trade_date, max_trade_date
                FROM {meta_table}
                WHERE symbol = %s
                """,
                (symbol,),
            )
            meta = cursor.fetchone()
            if not meta:
                return None

            min_trade_date = meta["min_trade_date"]
            max_trade_date = meta["max_trade_date"]
            if not min_trade_date or not max_trade_date:
                return None
            if pd.Timestamp(min_trade_date) > requested_start + pd.Timedelta(days=CACHE_START_TOLERANCE_DAYS):
                return None
            if pd.Timestamp(max_trade_date) < requested_start:
                return None
            if requested_end is not None and pd.Timestamp(min_trade_date) > requested_end:
                return None

            end_filter_sql = "AND trade_date <= %s" if requested_end is not None else ""
            params: tuple[object, ...]
            if requested_end is not None:
                params = (symbol, requested_start.date(), requested_end.date())
            else:
                params = (symbol, requested_start.date())
            cursor.execute(
                f"""
                SELECT
                  trade_date,
                  open_price,
                  high_price,
                  low_price,
                  close_price,
                  volume,
                  amount,
                  amount_source
                FROM {ohlcv_table}
                WHERE symbol = %s
                  AND trade_date >= %s
                  {end_filter_sql}
                  AND open_price > 0
                  AND high_price > 0
                  AND low_price > 0
                  AND close_price > 0
                ORDER BY trade_date
                """,
                params,
            )
            df = ohlcv_from_db_rows(list(cursor.fetchall()))
            if df.empty:
                return None
            return df
    finally:
        connection.close()


def expected_latest_trade_date(now: datetime | None = None) -> date:
    """현재 시각 기준으로 기대하는 최신 거래일을 계산합니다.

    Args:
        now: 계산 기준 시각입니다. None이면 현재 KST 시각을 사용합니다.

    Returns:
        주말과 장중 실행을 고려한 기대 최신 거래일입니다.
    """
    current = now or datetime.now(KST)
    candidate = current.date()

    if current.weekday() >= 5:
        candidate -= timedelta(days=current.weekday() - 4)
    elif current.hour < 18:
        candidate -= timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)

    return candidate


def cache_is_fresh(symbol: str, config: Config, *, end_date: str | None = None) -> bool:
    """종목의 MariaDB 캐시가 TTL 안에 갱신되었는지 확인합니다.

    Args:
        symbol: 확인할 종목 코드입니다.
        config: 캐시 TTL과 DB 접속정보를 포함한 설정입니다.
        end_date: 필요한 데이터의 종료일입니다. 지정되면 해당 기준일 데이터 포함 여부를 우선합니다.

    Returns:
        캐시가 강제 갱신 대상이 아니고 TTL 안에 있으며 기대 최신 거래일까지 포함하면 True입니다.
    """
    if config.force_refresh:
        return False

    _, meta_table = db_table_names(config)
    connection = db_connection(config)
    try:
        ensure_ohlcv_cache_tables(connection, config)
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT last_cached_at, max_trade_date
                FROM {meta_table}
                WHERE symbol = %s
                """,
                (symbol,),
            )
            row = cursor.fetchone()
            if not row or not row["last_cached_at"]:
                return False
            if end_date:
                parsed_end = pd.Timestamp(end_date).date()
                reference_now = datetime(parsed_end.year, parsed_end.month, parsed_end.day, 20, 0, tzinfo=KST)
            else:
                reference_now = config_target_datetime(config)
            if not row["max_trade_date"] or row["max_trade_date"] < expected_latest_trade_date(reference_now):
                return False
            if end_date:
                return True
            last_cached_at = row["last_cached_at"]
            age_hours = (datetime.now() - last_cached_at).total_seconds() / 3600
            return age_hours <= config.cache_ttl_hours
    finally:
        connection.close()


def write_cached_ohlcv(symbol: str, df: pd.DataFrame, config: Config | None = None) -> None:
    """OHLCV 데이터를 MariaDB 캐시 테이블에 upsert합니다.

    Args:
        symbol: 저장할 종목 코드입니다.
        df: 저장할 OHLCV 데이터프레임입니다.
        config: DB 접속정보입니다. None이면 기본 Config를 사용합니다.
    """
    config = config or Config()
    df = normalize_ohlcv(df)
    if df.empty:
        return

    params = ohlcv_db_params(symbol, df)
    if not params:
        return

    ohlcv_table, _ = db_table_names(config)
    connection = db_connection(config)
    try:
        ensure_ohlcv_cache_tables(connection, config)
        with connection.cursor() as cursor:
            cursor.executemany(
                f"""
                INSERT INTO {ohlcv_table} (
                  symbol, trade_date,
                  open_price, high_price, low_price, close_price,
                  volume, amount, amount_source
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  open_price = VALUES(open_price),
                  high_price = VALUES(high_price),
                  low_price = VALUES(low_price),
                  close_price = VALUES(close_price),
                  volume = VALUES(volume),
                  amount = VALUES(amount),
                  amount_source = VALUES(amount_source),
                  updated_at = CURRENT_TIMESTAMP
                """,
                params,
            )
            rebuild_ohlcv_cache_meta(connection, config, symbol=symbol)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def merge_ohlcv(cached: pd.DataFrame | None, fetched: pd.DataFrame, start_date: str, *, end_date: str | None = None) -> pd.DataFrame:
    """캐시 데이터와 새로 조회한 OHLCV 데이터를 병합합니다.

    Args:
        cached: 기존 MariaDB 캐시 데이터입니다.
        fetched: FDR에서 새로 조회한 데이터입니다.
        start_date: 반환 데이터의 최소 시작일입니다.
        end_date: 반환 데이터의 최대 종료일입니다. None이면 종료일 제한이 없습니다.

    Returns:
        중복 날짜를 정리하고 시작일 이후만 남긴 OHLCV 데이터프레임입니다.
    """
    fetched = normalize_ohlcv(fetched)
    if cached is not None and not cached.empty:
        merged = pd.concat([cached, fetched])
    else:
        merged = fetched
    merged = normalize_ohlcv(merged)
    result = merged.loc[merged.index >= pd.Timestamp(start_date)]
    if end_date:
        result = result.loc[result.index <= pd.Timestamp(end_date)]
    return result


def fetch_ohlcv(symbol: str, start_date: str, config: Config, *, end_date: str | None = None) -> pd.DataFrame:
    """MariaDB 캐시를 우선 사용해 종목 OHLCV 데이터를 조회합니다.

    Args:
        symbol: 조회할 종목 코드입니다.
        start_date: 필요한 데이터의 시작일입니다.
        config: 캐시, 재시도, DB 접속 설정입니다.
        end_date: 필요한 데이터의 종료일입니다. None이면 현재까지 조회합니다.

    Returns:
        분석에 사용할 OHLCV 데이터프레임입니다.

    Raises:
        RuntimeError: 캐시도 없고 외부 조회도 모두 실패한 경우 발생합니다.
    """
    end_date = end_date or config_end_date(config)
    cached = read_cached_ohlcv(symbol, start_date, config, end_date=end_date)
    if cached is not None and cache_is_fresh(symbol, config, end_date=end_date):
        return cached

    fetch_start = start_date
    if cached is not None and not cached.empty:
        # 마지막 저장일을 하루 겹쳐 받아서 당일 데이터 수정/보정분은 새 값으로 덮어쓴다.
        fetch_start = cached.index.max().strftime("%Y-%m-%d")
    if end_date and pd.Timestamp(fetch_start) > pd.Timestamp(end_date):
        return cached.loc[cached.index >= pd.Timestamp(start_date)] if cached is not None else pd.DataFrame()

    last_error: Exception | None = None
    for attempt in range(1, config.fetch_retries + 1):
        try:
            fetched = fdr.DataReader(symbol, fetch_start, end_date) if end_date else fdr.DataReader(symbol, fetch_start)
            if fetched is None or fetched.empty:
                raise ValueError("empty dataframe")
            df = merge_ohlcv(cached, fetched, start_date, end_date=end_date)
            write_cached_ohlcv(symbol, df, config)
            return df
        except Exception as exc:  # noqa: BLE001 - 종목별 실패를 집계해야 함
            last_error = exc
            sleep_seconds = min(2 ** attempt, 10)
            logger.warning("OHLCV 조회 실패: symbol=%s attempt=%s/%s error=%s", symbol, attempt, config.fetch_retries, exc)
            if attempt < config.fetch_retries:
                time.sleep(sleep_seconds)

    if cached is not None and not cached.empty and not config.force_refresh:
        logger.warning("OHLCV 갱신 실패. 기존 DB 캐시 사용: symbol=%s error=%s", symbol, last_error)
        result = cached.loc[cached.index >= pd.Timestamp(start_date)]
        if end_date:
            result = result.loc[result.index <= pd.Timestamp(end_date)]
        return result

    raise RuntimeError(f"OHLCV 조회 실패: {symbol}: {last_error}")


def telegram_post(url: str, *, data: dict[str, Any], files: dict[str, Any] | None, config: Config) -> None:
    """Telegram API에 요청을 보내고 rate limit을 재시도합니다.

    Args:
        url: Telegram API 엔드포인트입니다.
        data: 요청 form 데이터입니다.
        files: 업로드할 파일 데이터입니다.
        config: 요청 타임아웃 설정입니다.

    Raises:
        RuntimeError: 재시도 후에도 Telegram 전송이 실패한 경우 발생합니다.
        requests.HTTPError: Telegram API가 오류 상태 코드를 반환한 경우 발생합니다.
    """
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
    """Telegram 메시지 길이 제한에 맞게 본문을 나눕니다.

    Args:
        text: 전송할 원본 메시지입니다.
        limit: 한 메시지의 최대 문자 수입니다.

    Returns:
        길이 제한을 넘지 않는 메시지 조각 목록입니다.
    """
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
    """텍스트 메시지를 Telegram으로 전송합니다.

    Args:
        text: 전송할 메시지입니다.
        config: Telegram 토큰과 채팅 ID를 포함한 설정입니다.
        dry_run: 실제 전송 대신 로그만 남길지 여부입니다.
    """
    if dry_run or not config.telegram_enabled:
        logger.info("Telegram 메시지 스킵(dry_run=%s enabled=%s): %s", dry_run, config.telegram_enabled, text[:120])
        return
    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    for chunk in split_message(text):
        telegram_post(url, data={"chat_id": config.telegram_chat_id, "text": chunk}, files=None, config=config)
        time.sleep(0.4)


def send_telegram_photo(photo_path: Path, config: Config, *, dry_run: bool = False) -> None:
    """차트 이미지 파일을 Telegram으로 전송합니다.

    Args:
        photo_path: 전송할 이미지 파일 경로입니다.
        config: Telegram 토큰과 채팅 ID를 포함한 설정입니다.
        dry_run: 실제 전송 대신 로그만 남길지 여부입니다.
    """
    if dry_run or not config.telegram_enabled:
        logger.info("Telegram 사진 스킵(dry_run=%s enabled=%s): %s", dry_run, config.telegram_enabled, photo_path)
        return
    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendPhoto"
    with photo_path.open("rb") as fp:
        telegram_post(url, data={"chat_id": config.telegram_chat_id}, files={"photo": fp}, config=config)


def record_float(record: pd.Series, key: str) -> float | None:
    """Pandas 행에서 숫자 값을 안전하게 꺼냅니다.

    Args:
        record: 후보 정보가 담긴 Pandas 행입니다.
        key: 읽을 컬럼 이름입니다.

    Returns:
        유효한 숫자이면 float 값이고, 값이 없거나 NaN이면 None입니다.
    """
    value = record.get(key, None)
    if value is None or pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_vcp_interpretation(record: pd.Series) -> str:
    """VCP 후보 지표에 따라 다양한 해석 문구를 생성합니다.

    Args:
        record: VCP 후보 정보가 들어 있는 Pandas 행입니다.

    Returns:
        텔레그램 메시지에 넣을 해석 문구입니다.
    """
    drop_pct = record_float(record, "drop_from_52w_high_pct")
    pivot_gap_pct = record_float(record, "pivot_gap_pct")
    avg_traded_value = record_float(record, "avg_traded_value_20") or 0
    pocket_pivot_count = int(record_float(record, "pocket_pivot_count") or record_float(record, "pocket_pivot_count_14d") or 0)
    fast_ma_gap = record_float(record, "price_vs_fast_ma_pct")
    mid_ma_gap = record_float(record, "price_vs_mid_ma_pct")
    contraction_ratio = record_float(record, "contraction_ratio")

    if contraction_ratio is None:
        swing_drops = record.get("recent_swing_drops_pct", [])
        if isinstance(swing_drops, list) and len(swing_drops) >= 2 and swing_drops[-2]:
            contraction_ratio = float(swing_drops[-1]) / float(swing_drops[-2])

    lines: list[str] = []
    if drop_pct is not None:
        if drop_pct <= 8:
            lines.append("강점: 52주 고점과 매우 가까워 돌파 확인 구간에 있습니다.")
        elif drop_pct <= 15:
            lines.append("맥락: 신고가권으로 복귀 중이지만 상단 확인까지는 약간의 거리가 있습니다.")
        else:
            lines.append("주의: 52주 고점 이격이 남아 있어 즉시 돌파보다 회복 지속성이 중요합니다.")

    if pivot_gap_pct is not None:
        if pivot_gap_pct <= 5:
            lines.append("매매 포인트: 최근 피벗과 가까워 거래량 동반 돌파 여부를 바로 확인할 만합니다.")
        elif pivot_gap_pct <= 10:
            lines.append("확인 포인트: 피벗까지 여유가 있어 상단 접근 시 거래량 변화를 봐야 합니다.")
        else:
            lines.append("주의: 최근 고점까지 거리가 있어 눌림 후 재수축 여부를 한 번 더 확인해야 합니다.")

    if contraction_ratio is not None:
        if contraction_ratio <= 0.7:
            lines.append("수축: 직전보다 변동폭이 크게 줄어 에너지 응축 신호가 비교적 뚜렷합니다.")
        elif contraction_ratio <= 1.0:
            lines.append("수축: 변동폭은 줄었지만 강한 압축보다는 완만한 정리 구간에 가깝습니다.")
        else:
            lines.append("수축: 허용 기준은 통과했지만 최근 변동폭 감소가 약해 추가 수렴 확인이 필요합니다.")

    if pocket_pivot_count >= 2:
        lines.append(f"수급: 최근 Pocket Pivot이 {pocket_pivot_count}회라 매수세 유입이 반복된 편입니다.")
    elif pocket_pivot_count == 1:
        lines.append("수급: Pocket Pivot이 1회 확인되어 초기 수급 단서로 볼 수 있습니다.")
    else:
        lines.append("수급: Pocket Pivot이 부족하므로 돌파 당일 거래량 확인 비중을 높여야 합니다.")

    if avg_traded_value >= 50_000_000_000:
        lines.append("유동성: 평균 거래대금이 충분해 체결 부담은 상대적으로 낮은 편입니다.")
    elif avg_traded_value >= 15_000_000_000:
        lines.append("유동성: 거래대금 기준은 통과했지만 호가와 체결 강도 확인이 필요합니다.")

    if fast_ma_gap is not None and mid_ma_gap is not None:
        if fast_ma_gap >= 10:
            lines.append(f"과열 체크: 단기선 대비 +{fast_ma_gap:.1f}%라 돌파 실패 시 흔들림이 커질 수 있습니다.")
        elif fast_ma_gap >= 0 and mid_ma_gap >= 0:
            lines.append(f"추세: 단기선 대비 +{fast_ma_gap:.1f}%, 중기선 대비 +{mid_ma_gap:.1f}%로 추세 위에 있습니다.")

    if not lines:
        lines.append("확인: 차트의 수축 구간, 피벗 상단, 돌파 거래량을 함께 확인하세요.")

    return "\n".join(f"   - {line}" for line in lines[:6])


def format_vcp_message(record: pd.Series, *, rank_no: int) -> str:
    """VCP 후보 한 종목의 텔레그램 메시지를 생성합니다.

    Args:
        record: VCP 후보 정보가 들어 있는 Pandas 행입니다.
        rank_no: 텔레그램 메시지에 표시할 후보 순번입니다.

    Returns:
        텔레그램으로 전송할 VCP 후보 메시지입니다.
    """
    swing_drops = record.get("recent_swing_drops_pct", [])
    if isinstance(swing_drops, list) and swing_drops:
        swing_text = " → ".join(f"{float(value):.1f}%" for value in swing_drops[-3:])
    else:
        swing_text = "-"

    name = str(record.get("name", "")).strip()
    code = str(record.get("code", "")).strip()
    current_price = float(record.get("current_price", 0))
    drop_pct = float(record.get("drop_from_52w_high_pct", 0))
    avg_traded_value = float(record.get("avg_traded_value_20", 0))
    pocket_pivot_count = int(record.get("pocket_pivot_count", record.get("pocket_pivot_count_14d", 0)))
    pivot_gap_pct = record.get("pivot_gap_pct", None)
    pivot_gap_text = f"{float(pivot_gap_pct):.2f}%" if pivot_gap_pct is not None and pd.notna(pivot_gap_pct) else "-"
    recent_high_window = int(record.get("recent_high_window", 20) or 20)
    pocket_pivot_days = int(record.get("pocket_pivot_days", 14) or 14)
    interpretation = format_vcp_interpretation(record)

    return (
        f"🔎 [VCP 후보 #{rank_no}] {name}({code})\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 현재가: {current_price:,.0f}원\n"
        f"📍 52주 고점 대비: -{drop_pct:.2f}% | 단계: {record.get('vcp_stage', '-')}\n"
        f"🎯 최근 {recent_high_window}일 고점까지: {pivot_gap_text}\n"
        f"💰 20일 평균 거래대금: {avg_traded_value:,.0f}원\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📊 [VCP 체크]\n"
        f"   - 최근 수축폭: {swing_text}\n"
        f"   - Pocket Pivot({pocket_pivot_days}일): {pocket_pivot_count}회\n"
        "   - 조건: 고점권 + 수축폭 기준 + 거래대금/Pocket Pivot 통과\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🧭 [해석]\n"
        f"{interpretation}"
    )


def format_market_regime_message(regime: Any, config: Config) -> str:
    """시장 국면 점검 결과를 독립 텔레그램 메시지로 구성합니다."""
    prefix = f"📅 타겟 날짜: {format_target_date(config)}\n"
    if not regime.ok:
        return f"{prefix}⚠️ [시장국면]\n코스닥 지수 조회 실패: {regime.error}\n시장 국면은 '불명'으로 표시하고 스캔은 계속합니다."

    if not regime.is_bull_market:
        return (
            f"{prefix}"
            "🛑 [시장국면]\n"
            f"코스닥 지수({regime.current:,.2f})가 50일선({regime.ma50:,.2f})을 하회합니다.\n"
            f"60거래일 수익률: {regime.kq_return_60:+.2f}%\n"
            "수익 보전과 현금 비중 확대를 우선합니다."
        )

    return (
        f"{prefix}"
        "✅ [시장국면]\n"
        f"코스닥 지수({regime.current:,.2f})가 50일선({regime.ma50:,.2f}) 위에 있습니다.\n"
        f"60거래일 수익률: {regime.kq_return_60:+.2f}%\n"
        "시장 국면은 기본 스캔 진행에 우호적입니다."
    )


def run_vcp_pipeline(config: Config, *, dry_run: bool, max_symbols: int | None, no_charts: bool) -> None:
    """종합분석 이후 VCP 스캔과 텔레그램 전송을 실행합니다.

    Args:
        config: 실행 환경과 필터 기준을 담은 설정 객체입니다.
        dry_run: 실제 텔레그램 전송 없이 로그만 남길지 여부입니다.
        max_symbols: 테스트용으로 제한할 최대 종목 수입니다.
        no_charts: 차트 이미지 생성과 전송을 생략할지 여부입니다.
    """
    if not config.vcp_enabled:
        logger.info("VCP 스캔 비활성화")
        return

    from vcp_scan import VcpCriteria, run_vcp_scan

    save_charts = config.send_charts and not no_charts
    criteria = VcpCriteria(
        min_avg_traded_value=config.vcp_min_avg_traded_value,
        max_drop_from_high=config.vcp_max_drop_from_high,
        max_pivot_gap=config.vcp_max_pivot_gap,
        max_contraction_ratio=config.vcp_max_contraction_ratio,
        min_pocket_pivot_count=config.vcp_min_pocket_pivot_count,
    )
    logger.info("VCP 스캔 시작")

    try:
        candidates, vcp_run_id, elapsed_seconds = run_vcp_scan(
            max_symbols=max_symbols,
            criteria=criteria,
            days=VCP_LOOKBACK_DAYS,
            target_date=config.target_date,
            save_charts=save_charts,
            charts_dir=CHART_DIR,
            use_project_cache=True,
            force_refresh=config.force_refresh,
        )
    except Exception as exc:  # noqa: BLE001 - 기존 분석 결과 전송 후 VCP 실패만 별도 알림 처리
        logger.exception("VCP 스캔 실패")
        send_telegram_msg(f"⚠️ [VCP 스캔 실패]\n{exc}", config, dry_run=dry_run)
        return

    logger.info("VCP 스캔 결과 DB 저장: run_id=%s candidates=%s", vcp_run_id, len(candidates))

    if candidates.empty:
        send_telegram_msg("🔎 [VCP]\n💡 VCP 조건을 만족하는 종목이 없습니다.", config, dry_run=dry_run)
        return

    send_limit = min(config.top_send_limit, len(candidates))
    intro_msg = (
        "🔎 [VCP]\n"
        f"[VCP 스캔 결과] 후보 종목: {len(candidates)}개\n"
        f"⏱ 소요 시간: {elapsed_seconds:.1f}초\n\n"
        f"👇 상위 {send_limit}개 VCP 차트와 브리핑을 보냅니다."
    )
    send_telegram_msg(intro_msg, config, dry_run=dry_run)

    for rank_no, (_, record) in enumerate(candidates.head(send_limit).iterrows(), start=1):
        chart_path = record.get("chart_path", "")
        if save_charts and chart_path:
            photo_path = Path(str(chart_path))
            if photo_path.exists():
                send_telegram_photo(photo_path, config, dry_run=dry_run)
                time.sleep(0.5)
            else:
                logger.warning("VCP 차트 파일 없음: %s", photo_path)
        send_telegram_msg(format_vcp_message(record, rank_no=rank_no), config, dry_run=dry_run)
        time.sleep(0.5)


def load_collection_universe_from_db_cache(config: Config, max_symbols: int | None = None) -> pd.DataFrame:
    """MariaDB 캐시에서 전체 수집 대상 종목 유니버스를 구성합니다.

    Args:
        config: DB 접속정보입니다.
        max_symbols: 테스트용 최대 종목 수입니다.

    Returns:
        Code, Name, Sector 컬럼을 가진 수집 대상 종목 유니버스입니다.
    """
    _, meta_table = db_table_names(config)
    limit_sql = "LIMIT %s" if max_symbols else ""
    connection = db_connection(config)
    try:
        ensure_ohlcv_cache_tables(connection, config)
        with connection.cursor() as cursor:
            params: tuple[object, ...] = (max_symbols,) if max_symbols else ()
            cursor.execute(
                f"""
                SELECT
                  symbol AS Code,
                  symbol AS Name,
                  '' AS Sector
                FROM {meta_table}
                WHERE symbol REGEXP '^[0-9]{{6}}$'
                  AND row_count > 0
                ORDER BY symbol
                {limit_sql}
                """,
                params,
            )
            universe = pd.DataFrame(cursor.fetchall())
    finally:
        connection.close()

    if universe.empty:
        return pd.DataFrame(columns=["Code", "Name", "Sector"])
    return universe[["Code", "Name", "Sector"]].fillna("").reset_index(drop=True)


def load_collection_universe(config: Config, max_symbols: int | None = None) -> pd.DataFrame:
    """OHLCV 전체 수집에 사용할 종목 유니버스를 구성합니다.

    Args:
        config: DB fallback에 사용할 실행 설정입니다.
        max_symbols: 테스트용 최대 종목 수입니다.

    Returns:
        Code, Name, Sector 컬럼을 가진 수집 대상 종목 유니버스입니다.
    """
    try:
        basic = fdr.StockListing("KRX")
    except Exception as exc:  # noqa: BLE001
        logger.warning("KRX 종목 리스트 조회 실패. MariaDB OHLCV 캐시 종목으로 전체 수집 진행: %s", exc)
        return load_collection_universe_from_db_cache(config, max_symbols=max_symbols)

    try:
        desc = fdr.StockListing("KRX-DESC")[["Code", "Sector"]]
        universe = pd.merge(basic, desc, on="Code", how="left")
    except Exception as exc:  # noqa: BLE001
        logger.warning("KRX-DESC 조회 실패. Sector 없이 전체 수집 진행: %s", exc)
        universe = basic.copy()
        universe["Sector"] = ""

    universe = universe[universe["Market"].isin(["KOSPI", "KOSDAQ"])]
    universe = universe[universe["Code"].astype(str).str.match(r"^\d{6}$")]
    universe = universe[~universe["Name"].astype(str).str.contains(r"스팩|제[0-9]+호|우$|우B|우C|리츠|ETF|ETN", regex=True, na=False)]
    universe = universe[["Code", "Name", "Sector"]].fillna("").reset_index(drop=True)
    if max_symbols:
        universe = universe.head(max_symbols)
    return universe


def get_target_date_cache_status(config: Config) -> dict[str, Any]:
    """target_date 기준 분석에 필요한 OHLCV 캐시 커버리지를 계산합니다."""
    if not config.target_date:
        return {"is_ready": False, "reason": "target_date 없음"}

    _, meta_table = db_table_names(config)
    target_trade_date = expected_latest_trade_date(config_target_datetime(config))
    stock_start_limit = (pd.Timestamp(window_start_date(config, config.collect_days)) + pd.Timedelta(days=CACHE_START_TOLERANCE_DAYS)).date()
    index_start_limit = (pd.Timestamp(window_start_date(config, 150)) + pd.Timedelta(days=CACHE_START_TOLERANCE_DAYS)).date()

    connection = db_connection(config)
    try:
        ensure_ohlcv_cache_tables(connection, config)
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT
                  SUM(CASE
                    WHEN symbol = 'KQ11'
                     AND min_trade_date <= %s
                     AND max_trade_date >= %s
                    THEN 1 ELSE 0 END) AS index_ready,
                  SUM(CASE
                    WHEN symbol REGEXP '^[0-9]{{6}}$'
                     AND row_count >= 200
                     AND min_trade_date <= %s
                    THEN 1 ELSE 0 END) AS eligible_stocks,
                  SUM(CASE
                    WHEN symbol REGEXP '^[0-9]{{6}}$'
                     AND row_count >= 200
                     AND min_trade_date <= %s
                     AND max_trade_date >= %s
                    THEN 1 ELSE 0 END) AS ready_stocks
                FROM {meta_table}
                WHERE symbol = 'KQ11'
                   OR symbol REGEXP '^[0-9]{{6}}$'
                """,
                (index_start_limit, target_trade_date, stock_start_limit, stock_start_limit, target_trade_date),
            )
            row = cursor.fetchone() or {}
    finally:
        connection.close()

    index_ready = int(row.get("index_ready") or 0) > 0
    eligible_stocks = int(row.get("eligible_stocks") or 0)
    ready_stocks = int(row.get("ready_stocks") or 0)
    coverage_ratio = (ready_stocks / eligible_stocks) if eligible_stocks else 0.0
    is_ready = (
        index_ready
        and ready_stocks >= TARGET_CACHE_MIN_READY_SYMBOLS
        and coverage_ratio >= TARGET_CACHE_MIN_COVERAGE_RATIO
    )
    return {
        "is_ready": is_ready,
        "target_trade_date": target_trade_date,
        "index_ready": index_ready,
        "eligible_stocks": eligible_stocks,
        "ready_stocks": ready_stocks,
        "coverage_ratio": coverage_ratio,
        "min_ready_symbols": TARGET_CACHE_MIN_READY_SYMBOLS,
        "min_coverage_ratio": TARGET_CACHE_MIN_COVERAGE_RATIO,
    }


def collect_ohlcv_data(config: Config, *, max_symbols: int | None = None) -> Counter:
    """종합분석과 VCP 실행 전에 전체 OHLCV 데이터를 MariaDB로 갱신합니다.

    Args:
        config: 전체 수집에 사용할 실행 설정입니다.
        max_symbols: 테스트용 수집 종목 수 제한입니다. 운영 실행에서는 None을 사용합니다.

    Returns:
        수집 대상 수, 성공/실패 수, 최신 거래일 분포를 담은 Counter입니다.
    """
    stats: Counter = Counter()
    if not config.collect_enabled:
        logger.info("OHLCV 전체 수집 비활성화")
        return stats

    if config.target_date:
        try:
            cache_status = get_target_date_cache_status(config)
            if cache_status["is_ready"]:
                ready_stocks = int(cache_status["ready_stocks"])
                eligible_stocks = int(cache_status["eligible_stocks"])
                coverage_pct = float(cache_status["coverage_ratio"]) * 100
                stats["targets_total"] = ready_stocks + 1
                stats["status_cache_ready"] = ready_stocks + 1
                stats["collection_skipped_target_cache_ready"] = 1
                stats[f"latest_{cache_status['target_trade_date']}"] = ready_stocks + 1
                logger.info(
                    "target_date 캐시 충분. OHLCV 전체 수집 생략: target_trade_date=%s ready_stocks=%s eligible_stocks=%s coverage=%.2f%% index_ready=%s",
                    cache_status["target_trade_date"],
                    ready_stocks,
                    eligible_stocks,
                    coverage_pct,
                    cache_status["index_ready"],
                )
                return stats

            logger.info("target_date 캐시 부족. OHLCV 전체 수집 진행: %s", cache_status)
        except Exception as exc:  # noqa: BLE001 - 캐시 확인 실패 시 기존 수집 경로로 진행
            logger.warning("target_date 캐시 커버리지 확인 실패. OHLCV 전체 수집 진행: %s", exc)

    stock_start_date = window_start_date(config, config.collect_days)
    index_start_date = window_start_date(config, 150)
    universe = load_collection_universe(config, max_symbols=max_symbols)
    stock_symbols = [str(code) for code in universe["Code"].dropna().astype(str).tolist()]
    symbols = ["KQ11", *stock_symbols]
    stats["targets_total"] = len(symbols)
    logger.info(
        "OHLCV 전체 수집 시작: targets=%s stock_start_date=%s index_start_date=%s target_date=%s",
        len(symbols),
        stock_start_date,
        index_start_date,
        config.target_date or "-",
    )

    failed_samples: list[str] = []
    with ThreadPoolExecutor(max_workers=config.max_workers) as executor:
        futures = {
            executor.submit(fetch_ohlcv, symbol, index_start_date if symbol == "KQ11" else stock_start_date, config): symbol
            for symbol in symbols
        }
        for idx, future in enumerate(as_completed(futures), start=1):
            symbol = futures[future]
            try:
                df = future.result()
                stats["status_collected"] += 1
                if not df.empty:
                    latest_date = pd.Timestamp(df.index.max()).strftime("%Y-%m-%d")
                    stats[f"latest_{latest_date}"] += 1
            except Exception as exc:  # noqa: BLE001 - 종목별 수집 실패는 집계하고 다음 종목을 계속 처리한다.
                stats["status_failed"] += 1
                if len(failed_samples) < 20:
                    failed_samples.append(f"{symbol}: {exc}")

            if idx % 50 == 0 or idx == len(symbols):
                logger.info(
                    "OHLCV 수집 진행률: %s/%s collected=%s failed=%s",
                    idx,
                    len(symbols),
                    stats["status_collected"],
                    stats["status_failed"],
                )

    if failed_samples:
        logger.warning("OHLCV 수집 실패 샘플: %s", failed_samples)
    logger.info("OHLCV 전체 수집 종료: stats=%s", dict(stats))
    return stats


def load_universe_from_db_cache(config: Config, max_symbols: int | None = None) -> pd.DataFrame:
    """FDR 종목 리스트가 실패했을 때 MariaDB 캐시에서 분석 대상 종목을 구성합니다.

    Args:
        config: 필터 기준과 DB 접속정보입니다.
        max_symbols: 테스트용 최대 종목 수입니다.

    Returns:
        Code, Name, Sector 컬럼을 가진 종목 유니버스입니다.
    """
    ohlcv_table, meta_table = db_table_names(config)
    limit_sql = "LIMIT %s" if max_symbols else ""
    connection = db_connection(config)
    try:
        ensure_ohlcv_cache_tables(connection, config)
        with connection.cursor() as cursor:
            if config.target_date:
                target_trade_date = expected_latest_trade_date(config_target_datetime(config))
                params: list[object] = [target_trade_date, target_trade_date, config.first_pass_min_close, config.first_pass_min_amount]
                if max_symbols:
                    params.append(max_symbols)
                cursor.execute(
                    f"""
                    SELECT
                      m.symbol AS Code,
                      m.symbol AS Name,
                      '' AS Sector
                    FROM {meta_table} m
                    INNER JOIN (
                      SELECT symbol, MAX(trade_date) AS max_trade_date
                      FROM {ohlcv_table}
                      WHERE trade_date <= %s
                        AND open_price > 0
                        AND high_price > 0
                        AND low_price > 0
                        AND close_price > 0
                      GROUP BY symbol
                    ) latest
                      ON latest.symbol = m.symbol
                    INNER JOIN {ohlcv_table} o
                      ON o.symbol = latest.symbol
                     AND o.trade_date = latest.max_trade_date
                    WHERE m.symbol REGEXP '^[0-9]{{6}}$'
                      AND m.row_count >= 260
                      AND latest.max_trade_date >= %s
                      AND o.close_price >= %s
                      AND COALESCE(o.amount, o.close_price * o.volume) >= %s
                    ORDER BY latest.max_trade_date DESC, m.symbol
                    {limit_sql}
                    """,
                    tuple(params),
                )
            else:
                params = [config.first_pass_min_close, config.first_pass_min_amount]
                if max_symbols:
                    params.append(max_symbols)
                cursor.execute(
                    f"""
                    SELECT
                      m.symbol AS Code,
                      m.symbol AS Name,
                      '' AS Sector
                    FROM {meta_table} m
                    INNER JOIN {ohlcv_table} o
                      ON o.symbol = m.symbol
                     AND o.trade_date = m.max_trade_date
                    WHERE m.symbol REGEXP '^[0-9]{{6}}$'
                      AND m.row_count >= 260
                      AND o.open_price > 0
                      AND o.high_price > 0
                      AND o.low_price > 0
                      AND o.close_price >= %s
                      AND COALESCE(o.amount, o.close_price * o.volume) >= %s
                    ORDER BY m.max_trade_date DESC, m.symbol
                    {limit_sql}
                    """,
                    tuple(params),
                )
            universe = pd.DataFrame(cursor.fetchall())
    finally:
        connection.close()

    if universe.empty:
        return pd.DataFrame(columns=["Code", "Name", "Sector"])
    return universe[["Code", "Name", "Sector"]].fillna("").reset_index(drop=True)


def load_universe(config: Config, max_symbols: int | None = None) -> pd.DataFrame:
    """KRX 종목 리스트를 불러오고 1차 필터를 적용합니다.

    Args:
        config: 가격과 거래대금 필터 기준입니다.
        max_symbols: 테스트용 최대 종목 수입니다.

    Returns:
        Code, Name, Sector 컬럼을 가진 분석 대상 종목 유니버스입니다.
    """
    if config.target_date:
        logger.info("target_date 실행: MariaDB OHLCV 캐시 기준으로 분석 유니버스 구성")
        return load_universe_from_db_cache(config, max_symbols=max_symbols)

    try:
        basic = fdr.StockListing("KRX")
    except Exception as exc:  # noqa: BLE001
        logger.warning("KRX 종목 리스트 조회 실패. MariaDB OHLCV 캐시 종목으로 진행: %s", exc)
        return load_universe_from_db_cache(config, max_symbols=max_symbols)

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


def db_scan_table_names(config: Config) -> tuple[str, str]:
    """종합분석 실행 이력과 결과 테이블명을 생성합니다.

    Args:
        config: DB 테이블 접두사를 포함한 실행 설정입니다.

    Returns:
        스캔 실행 테이블명과 결과 테이블명입니다.
    """
    return db_scheme.scan_table_names(db_table_prefix(config))


def ensure_scan_report_tables(connection, config: Config) -> None:
    """종합분석 결과 저장에 필요한 MariaDB 테이블을 생성합니다.

    Args:
        connection: 활성 MariaDB 연결 객체입니다.
        config: DB 테이블 접두사를 포함한 실행 설정입니다.
    """
    db_scheme.ensure_scan_report_tables(connection, db_table_prefix(config))


def save_reports(results: list[ScanResult], stats: Counter, start_ts: str, config: Config) -> int:
    """종합분석 실행 통계와 후보 결과를 MariaDB에 저장합니다.

    Args:
        results: 종합분석에서 포착된 후보 목록입니다.
        stats: 실행 중 집계한 상태와 스킵 사유 통계입니다.
        start_ts: 실행 시작 시각 문자열입니다.
        config: DB 접속정보와 테이블 접두사 설정입니다.

    Returns:
        저장된 스캔 실행 이력의 ID입니다.
    """
    runs_table, results_table = db_scan_table_names(config)
    now = datetime.now(KST).replace(tzinfo=None)
    stats_payload = json.dumps(dict(stats), ensure_ascii=False, default=str)
    result_rows = [asdict(result) for result in results]

    connection = db_connection(config)
    try:
        ensure_scan_report_tables(connection, config)
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {runs_table} (
                  start_ts, found_total, stats_json, created_at
                )
                VALUES (%s, %s, %s, %s)
                """,
                (start_ts, len(results), stats_payload, now),
            )
            run_id = int(cursor.lastrowid)

            if result_rows:
                cursor.executemany(
                    f"""
                    INSERT INTO {results_table} (
                      run_id, rank_no, code, name, sector, stars,
                      rs_score, avg_turnover, curr_p, result_json, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            run_id,
                            rank_no,
                            row["code"],
                            row["name"],
                            row["sector"],
                            row["stars"],
                            row["rs_score"],
                            row["avg_turnover"],
                            row["curr_p"],
                            json.dumps(row, ensure_ascii=False, default=str),
                            now,
                        )
                        for rank_no, row in enumerate(result_rows, start=1)
                    ],
                )
        connection.commit()
        return run_id
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def run(config: Config, *, dry_run: bool, max_symbols: int | None, no_charts: bool) -> int:
    """종합분석을 실행하고 이후 VCP 스캔 파이프라인을 이어서 실행합니다.

    Args:
        config: 전체 실행 설정입니다.
        dry_run: 실제 텔레그램 전송 없이 실행할지 여부입니다.
        max_symbols: 테스트용 최대 종목 수입니다.
        no_charts: 차트 생성과 전송을 생략할지 여부입니다.

    Returns:
        프로세스 종료 코드입니다.
    """
    setup_korean_font()
    start_ts = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    reference_dt = config_target_datetime(config)
    now_display = reference_dt.strftime("%Y-%m-%d %H:%M:%S")
    logger.info("마스터 스캐너 시작: %s 기준일=%s", now_display, config.target_date or "-")

    stats: Counter = Counter()
    collection_stats = collect_ohlcv_data(config)
    for key, value in collection_stats.items():
        stats[f"collection_{key}"] = value

    regime = check_market_regime(config)
    send_telegram_msg(format_market_regime_message(regime, config), config, dry_run=dry_run)

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

    scan_run_id = save_reports(found, stats, start_ts, config)
    logger.info("스캔 결과 DB 저장: run_id=%s found=%s", scan_run_id, len(found))

    if found:
        sectors = [result.sector for result in found if result.sector]
        sector_warning = ""
        if sectors:
            top_sector, count = Counter(sectors).most_common(1)[0]
            if count >= 3:
                sector_warning = f"⚠️ [섹터 집중 경고] 포착된 종목 중 {count}개가 '{top_sector}'에 집중되어 있습니다.\n\n"

        intro_msg = (
            "📊 [Analysis]\n"
            f"🔔 [퀀트 스캔 결과] 포착 종목: {len(found)}개\n"
            f"⏰ 스캔 일시: {now_display}\n\n"
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
        no_result_msg = "📊 [Analysis]\n💡 조건을 만족하는 종목이 없습니다."
        send_telegram_msg(no_result_msg, config, dry_run=dry_run)
        logger.info("조건 만족 종목 없음")

    run_vcp_pipeline(config, dry_run=dry_run, max_symbols=max_symbols, no_charts=no_charts)

    logger.info("스캐너 종료: stats=%s", dict(stats))
    return 0


def parse_args() -> argparse.Namespace:
    """명령행 인자를 파싱합니다.

    Returns:
        argparse로 파싱된 실행 옵션입니다.
    """
    parser = argparse.ArgumentParser(description="KRX Master Scanner")
    parser.add_argument("--dry-run", action="store_true", help="Telegram 전송 없이 로컬 실행/리포트만 생성")
    parser.add_argument("--max-symbols", type=int, default=None, help="테스트용 분석 종목 수 제한")
    parser.add_argument("--workers", type=int, default=None, help="병렬 분석 worker 수 override")
    parser.add_argument("--force-refresh", action="store_true", help="OHLCV 캐시 무시")
    parser.add_argument("--no-charts", action="store_true", help="차트 생성/전송 생략")
    parser.add_argument("--no-vcp", action="store_true", help="종합분석 후 VCP 스캔 생략")
    parser.add_argument("--repair-db-cache", action="store_true", help="invalid OHLCV row 삭제 후 캐시 메타 재계산")
    parser.add_argument("--target-date", type=parse_target_date, default=None, help="해당 날짜 장마감 기준으로 실행 (YYYY-MM-DD)")
    return parser.parse_args()


def main() -> int:
    """CLI 진입점에서 설정을 구성하고 스캐너를 실행합니다.

    Returns:
        프로세스 종료 코드입니다.
    """
    args = parse_args()
    config = Config()
    if args.target_date:
        config = replace(config, target_date=args.target_date)
    if args.workers is not None:
        config = replace(config, max_workers=args.workers)
    if args.force_refresh:
        config = replace(config, force_refresh=True)
    if args.no_vcp:
        config = replace(config, vcp_enabled=False)
    if args.repair_db_cache:
        result = repair_ohlcv_cache(config)
        logger.info("OHLCV 캐시 정리 완료: %s", result)
        return 0
    return run(config, dry_run=args.dry_run, max_symbols=args.max_symbols, no_charts=args.no_charts)


if __name__ == "__main__":
    raise SystemExit(main())
