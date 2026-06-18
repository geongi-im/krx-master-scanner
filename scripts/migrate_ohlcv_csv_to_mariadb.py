#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

import pandas as pd
from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_DIR = APP_DIR / "data" / "cache"
DEFAULT_TABLE_PREFIX = "kms"


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    database: str
    user: str
    password: str
    charset: str = "utf8mb4"


@dataclass(frozen=True)
class ImportRow:
    symbol: str
    trade_date: object
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: int
    amount: int | None
    amount_source: str


@dataclass(frozen=True)
class ImportResult:
    symbol: str
    file_path: Path
    row_count: int
    min_trade_date: object | None
    max_trade_date: object | None


def env_value(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def build_db_config(args: argparse.Namespace) -> DbConfig:
    host = args.db_host or env_value("DB_HOST", "127.0.0.1")
    port = int(args.db_port or env_value("DB_PORT", "3306"))
    database = args.db_name or env_value("DB_NAME")
    user = args.db_user or env_value("DB_USER")
    password = args.db_password or env_value("DB_PASSWORD")

    missing = [
        name
        for name, value in (
            ("DB_NAME", database),
            ("DB_USER", user),
            ("DB_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"Missing DB settings: {', '.join(missing)}")

    return DbConfig(
        host=str(host),
        port=port,
        database=str(database),
        user=str(user),
        password=str(password),
    )


def sanitize_table_prefix(prefix: str) -> str:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", prefix):
        raise ValueError("table prefix must start with a letter and contain only letters, numbers, and underscores")
    return prefix.rstrip("_")


def table_names(prefix: str) -> tuple[str, str]:
    clean_prefix = sanitize_table_prefix(prefix)
    return f"{clean_prefix}_ohlcv", f"{clean_prefix}_ohlcv_cache_meta"


def create_table_sql(prefix: str) -> list[str]:
    ohlcv_table, meta_table = table_names(prefix)
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


def connect(config: DbConfig):
    try:
        import pymysql
    except ImportError as exc:
        raise SystemExit("PyMySQL is required. Install dependencies with: pip install -r requirements.txt") from exc

    return pymysql.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.database,
        charset=config.charset,
        autocommit=False,
        cursorclass=pymysql.cursors.Cursor,
    )


def create_tables(connection, prefix: str) -> None:
    with connection.cursor() as cursor:
        for statement in create_table_sql(prefix):
            cursor.execute(statement)
    connection.commit()


def symbol_from_cache_path(path: Path) -> str:
    stem = path.stem
    if stem.startswith("ohlcv_"):
        stem = stem[len("ohlcv_") :]
    if not stem:
        raise ValueError(f"Cannot infer symbol from file name: {path.name}")
    return stem


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    renamed = {column: str(column).strip() for column in df.columns}
    df = df.rename(columns=renamed)

    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(sorted(missing))}")

    return df


def read_cache_csv(path: Path) -> pd.DataFrame:
    raw = pd.read_csv(path, encoding="utf-8-sig")
    raw = raw.rename(columns={column: str(column).strip() for column in raw.columns})
    if "Date" in raw.columns:
        date_values = raw.pop("Date")
    else:
        date_column = raw.columns[0]
        date_values = raw.pop(date_column)

    df = normalize_columns(raw)
    df.index = pd.to_datetime(date_values, errors="coerce")
    if df.index.isna().all():
        raise ValueError("CSV does not contain a usable Date column or date index")
    df = df[df.index.notna()]
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df


def to_decimal(value: object, field: str) -> Decimal:
    if pd.isna(value):
        raise ValueError(f"{field} is empty")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field} is not numeric: {value}") from exc


def to_int(value: object, field: str) -> int:
    if pd.isna(value):
        raise ValueError(f"{field} is empty")
    return int(Decimal(str(value)))


def first_existing_column(df: pd.DataFrame, candidates: Iterable[str]) -> str | None:
    for column in candidates:
        if column in df.columns:
            return column
    return None


def rows_from_frame(symbol: str, df: pd.DataFrame) -> list[ImportRow]:
    amount_column = first_existing_column(df, ("Amount", "amount", "Traded_Value", "traded_value"))
    rows: list[ImportRow] = []

    for trade_date, row in df.iterrows():
        open_price = to_decimal(row["Open"], "Open")
        high_price = to_decimal(row["High"], "High")
        low_price = to_decimal(row["Low"], "Low")
        close_price = to_decimal(row["Close"], "Close")
        volume = to_int(row["Volume"], "Volume")

        if amount_column:
            amount = to_int(row[amount_column], amount_column)
            amount_source = "source"
        else:
            amount = int((close_price * Decimal(volume)).to_integral_value())
            amount_source = "computed"

        rows.append(
            ImportRow(
                symbol=symbol,
                trade_date=trade_date.date(),
                open_price=open_price,
                high_price=high_price,
                low_price=low_price,
                close_price=close_price,
                volume=volume,
                amount=amount,
                amount_source=amount_source,
            )
        )

    return rows


def chunked(rows: list[ImportRow], batch_size: int) -> Iterable[list[ImportRow]]:
    for index in range(0, len(rows), batch_size):
        yield rows[index : index + batch_size]


def upsert_rows(connection, prefix: str, rows: list[ImportRow], batch_size: int) -> None:
    if not rows:
        return

    ohlcv_table, _ = table_names(prefix)
    sql = f"""
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
"""
    with connection.cursor() as cursor:
        for batch in chunked(rows, batch_size):
            cursor.executemany(
                sql,
                [
                    (
                        row.symbol,
                        row.trade_date,
                        row.open_price,
                        row.high_price,
                        row.low_price,
                        row.close_price,
                        row.volume,
                        row.amount,
                        row.amount_source,
                    )
                    for row in batch
                ],
            )


def upsert_meta(connection, prefix: str, result: ImportResult) -> None:
    _, meta_table = table_names(prefix)
    sql = f"""
INSERT INTO {meta_table} (
  symbol, min_trade_date, max_trade_date, row_count, last_cached_at
)
VALUES (%s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  min_trade_date = VALUES(min_trade_date),
  max_trade_date = VALUES(max_trade_date),
  row_count = VALUES(row_count),
  last_cached_at = VALUES(last_cached_at),
  updated_at = CURRENT_TIMESTAMP
"""
    with connection.cursor() as cursor:
        cursor.execute(
            sql,
            (
                result.symbol,
                result.min_trade_date,
                result.max_trade_date,
                result.row_count,
                datetime.now(),
            ),
        )


def import_file(connection, prefix: str, path: Path, batch_size: int, dry_run: bool) -> ImportResult:
    symbol = symbol_from_cache_path(path)
    df = read_cache_csv(path)
    rows = rows_from_frame(symbol, df)

    min_trade_date = rows[0].trade_date if rows else None
    max_trade_date = rows[-1].trade_date if rows else None
    result = ImportResult(symbol, path, len(rows), min_trade_date, max_trade_date)

    if not dry_run:
        upsert_rows(connection, prefix, rows, batch_size)
        upsert_meta(connection, prefix, result)
        connection.commit()

    return result


def find_cache_files(cache_dir: Path, pattern: str, limit: int | None) -> list[Path]:
    files = sorted(path for path in cache_dir.glob(pattern) if path.is_file())
    if limit is not None:
        return files[:limit]
    return files


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create MariaDB OHLCV tables and import existing CSV cache files.")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help="Directory containing ohlcv_*.csv files.")
    parser.add_argument("--pattern", default="ohlcv_*.csv", help="CSV file glob pattern.")
    parser.add_argument("--table-prefix", default=DEFAULT_TABLE_PREFIX, help="Table prefix. Defaults to kms.")
    parser.add_argument("--batch-size", type=int, default=1000, help="Number of OHLCV rows per executemany batch.")
    parser.add_argument("--limit", type=int, default=None, help="Import only the first N files for a trial run.")
    parser.add_argument("--dry-run", action="store_true", help="Read CSV files and print import counts without writing to DB.")
    parser.add_argument("--create-only", action="store_true", help="Create tables and exit without importing CSV files.")
    parser.add_argument("--skip-create", action="store_true", help="Skip CREATE TABLE statements.")

    parser.add_argument("--db-host", default=None, help="MariaDB host. Defaults to DB_HOST or 127.0.0.1.")
    parser.add_argument("--db-port", default=None, help="MariaDB port. Defaults to DB_PORT or 3306.")
    parser.add_argument("--db-name", default=None, help="MariaDB database. Defaults to DB_NAME.")
    parser.add_argument("--db-user", default=None, help="MariaDB user. Defaults to DB_USER.")
    parser.add_argument("--db-password", default=None, help="MariaDB password. Defaults to DB_PASSWORD.")
    return parser.parse_args()


def main() -> int:
    load_dotenv(APP_DIR / ".env")
    args = parse_args()
    prefix = sanitize_table_prefix(args.table_prefix)

    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be greater than 0")

    if args.dry_run and args.create_only:
        for statement in create_table_sql(prefix):
            print(statement.strip())
            print(";")
        return 0

    if args.dry_run:
        files = find_cache_files(args.cache_dir, args.pattern, args.limit)
        if not files:
            print(f"No CSV files found: {args.cache_dir / args.pattern}")
            return 0

        total_rows = 0
        for path in files:
            result = import_file(None, prefix, path, args.batch_size, dry_run=True)
            total_rows += result.row_count
            print(
                f"Would import {result.row_count:>6} rows "
                f"{result.symbol} {result.min_trade_date}..{result.max_trade_date} from {result.file_path.name}"
            )

        print(f"Dry run only. files={len(files)} rows={total_rows}")
        return 0

    db_config = build_db_config(args)
    connection = connect(db_config)
    try:
        if not args.skip_create:
            create_tables(connection, prefix)
            print(f"Created tables: {', '.join(table_names(prefix))}")

        if args.create_only:
            return 0

        files = find_cache_files(args.cache_dir, args.pattern, args.limit)
        if not files:
            print(f"No CSV files found: {args.cache_dir / args.pattern}")
            return 0

        total_rows = 0
        for path in files:
            try:
                result = import_file(connection, prefix, path, args.batch_size, args.dry_run)
            except Exception:
                connection.rollback()
                raise

            total_rows += result.row_count
            action = "Would import" if args.dry_run else "Imported"
            print(
                f"{action} {result.row_count:>6} rows "
                f"{result.symbol} {result.min_trade_date}..{result.max_trade_date} from {result.file_path.name}"
            )

        print(f"Done. files={len(files)} rows={total_rows}")
        if args.dry_run:
            print("Dry run only. No database changes were written.")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
