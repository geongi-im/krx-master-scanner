from __future__ import annotations

import re


def sanitize_table_prefix(prefix: str) -> str:
    """MariaDB 테이블 접두사를 검증하고 정리합니다."""
    clean_prefix = prefix.rstrip("_")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", clean_prefix):
        raise ValueError("DB_TABLE_PREFIX는 영문자로 시작하고 영문/숫자/밑줄만 포함해야 합니다.")
    return clean_prefix


def ohlcv_table_names(prefix: str) -> tuple[str, str]:
    """OHLCV 캐시와 캐시 메타 테이블명을 반환합니다."""
    clean_prefix = sanitize_table_prefix(prefix)
    return f"{clean_prefix}_ohlcv", f"{clean_prefix}_ohlcv_cache_meta"


def scan_table_names(prefix: str) -> tuple[str, str]:
    """종합분석 실행 이력과 결과 테이블명을 반환합니다."""
    clean_prefix = sanitize_table_prefix(prefix)
    return f"{clean_prefix}_scan_runs", f"{clean_prefix}_scan_results"


def vcp_table_names(prefix: str) -> tuple[str, str]:
    """VCP 실행 이력과 후보 테이블명을 반환합니다."""
    clean_prefix = sanitize_table_prefix(prefix)
    return f"{clean_prefix}_vcp_runs", f"{clean_prefix}_vcp_candidates"


def ohlcv_table_sql(prefix: str) -> list[str]:
    """OHLCV 캐시 테이블 생성 SQL 목록을 반환합니다."""
    ohlcv_table, meta_table = ohlcv_table_names(prefix)
    return [
        f"""
        CREATE TABLE IF NOT EXISTS {ohlcv_table} (
          symbol VARCHAR(20) NOT NULL,
          trade_date DATE NOT NULL,
          open_price DECIMAL(20,4) NOT NULL,
          high_price DECIMAL(20,4) NOT NULL,
          low_price DECIMAL(20,4) NOT NULL,
          close_price DECIMAL(20,4) NOT NULL,
          volume BIGINT UNSIGNED NOT NULL,
          amount BIGINT UNSIGNED NULL,
          amount_source VARCHAR(20) NOT NULL DEFAULT 'computed',
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (symbol, trade_date),
          INDEX idx_{ohlcv_table}_trade_date (trade_date),
          INDEX idx_{ohlcv_table}_symbol_updated (symbol, updated_at)
        ) ENGINE=InnoDB
          DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {meta_table} (
          symbol VARCHAR(20) NOT NULL,
          min_trade_date DATE NULL,
          max_trade_date DATE NULL,
          row_count INT UNSIGNED NOT NULL DEFAULT 0,
          last_cached_at DATETIME NOT NULL,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (symbol),
          INDEX idx_{meta_table}_last_cached_at (last_cached_at)
        ) ENGINE=InnoDB
          DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """,
    ]


def scan_report_table_sql(prefix: str) -> list[str]:
    """종합분석 결과 테이블 생성 SQL 목록을 반환합니다."""
    runs_table, results_table = scan_table_names(prefix)
    return [
        f"""
        CREATE TABLE IF NOT EXISTS {runs_table} (
          id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
          start_ts VARCHAR(20) NOT NULL,
          found_total INT UNSIGNED NOT NULL DEFAULT 0,
          stats_json LONGTEXT NOT NULL,
          created_at DATETIME NOT NULL,
          updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (id),
          INDEX idx_{runs_table}_start_ts (start_ts),
          INDEX idx_{runs_table}_created_at (created_at)
        ) ENGINE=InnoDB
          DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {results_table} (
          id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
          run_id BIGINT UNSIGNED NOT NULL,
          rank_no INT UNSIGNED NOT NULL,
          code VARCHAR(20) NOT NULL,
          name VARCHAR(100) NOT NULL,
          sector VARCHAR(100) NOT NULL,
          stars INT NOT NULL,
          rs_score DECIMAL(20,6) NOT NULL,
          avg_turnover DECIMAL(24,4) NOT NULL,
          curr_p DECIMAL(20,4) NOT NULL,
          result_json LONGTEXT NOT NULL,
          created_at DATETIME NOT NULL,
          PRIMARY KEY (id),
          INDEX idx_{results_table}_run_rank (run_id, rank_no),
          INDEX idx_{results_table}_code (code),
          INDEX idx_{results_table}_stars (stars),
          CONSTRAINT fk_{results_table}_run
            FOREIGN KEY (run_id) REFERENCES {runs_table} (id)
            ON DELETE CASCADE
        ) ENGINE=InnoDB
          DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """,
    ]


def vcp_result_table_sql(prefix: str) -> list[str]:
    """VCP 결과 테이블 생성 SQL 목록을 반환합니다."""
    runs_table, candidates_table = vcp_table_names(prefix)
    return [
        f"""
        CREATE TABLE IF NOT EXISTS {runs_table} (
          id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
          started_at DATETIME NOT NULL,
          elapsed_seconds DECIMAL(20,3) NOT NULL,
          candidates_total INT UNSIGNED NOT NULL DEFAULT 0,
          params_json LONGTEXT NOT NULL,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (id),
          INDEX idx_{runs_table}_started_at (started_at)
        ) ENGINE=InnoDB
          DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {candidates_table} (
          id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
          run_id BIGINT UNSIGNED NOT NULL,
          rank_no INT UNSIGNED NOT NULL,
          code VARCHAR(20) NOT NULL,
          name VARCHAR(100) NOT NULL,
          current_price DECIMAL(20,4) NOT NULL,
          drop_from_52w_high_pct DECIMAL(20,6) NOT NULL,
          avg_traded_value_20 DECIMAL(24,4) NOT NULL,
          candidate_json LONGTEXT NOT NULL,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (id),
          INDEX idx_{candidates_table}_run_rank (run_id, rank_no),
          INDEX idx_{candidates_table}_code (code),
          CONSTRAINT fk_{candidates_table}_run
            FOREIGN KEY (run_id) REFERENCES {runs_table} (id)
            ON DELETE CASCADE
        ) ENGINE=InnoDB
          DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """,
    ]


def all_table_sql(prefix: str) -> list[str]:
    """애플리케이션이 사용하는 전체 테이블 생성 SQL 목록을 반환합니다."""
    return [
        *ohlcv_table_sql(prefix),
        *scan_report_table_sql(prefix),
        *vcp_result_table_sql(prefix),
    ]


def execute_schema_sql(connection, statements: list[str]) -> None:
    """전달받은 스키마 SQL을 실행하고 커밋합니다."""
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
    connection.commit()


def ensure_ohlcv_cache_tables(connection, prefix: str) -> None:
    """OHLCV 캐시 테이블을 생성합니다."""
    execute_schema_sql(connection, ohlcv_table_sql(prefix))


def ensure_scan_report_tables(connection, prefix: str) -> None:
    """종합분석 결과 테이블을 생성합니다."""
    execute_schema_sql(connection, scan_report_table_sql(prefix))


def ensure_vcp_result_tables(connection, prefix: str) -> None:
    """VCP 결과 테이블을 생성합니다."""
    execute_schema_sql(connection, vcp_result_table_sql(prefix))


def ensure_all_tables(connection, prefix: str) -> None:
    """애플리케이션 전체 테이블을 생성합니다."""
    execute_schema_sql(connection, all_table_sql(prefix))
