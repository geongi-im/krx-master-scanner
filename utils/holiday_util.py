"""
KRX 휴장일 조회 유틸.

krx-holiday-updater 가 매일 갱신하는 mqway.krx_holiday 테이블을 읽어 거래일 여부를 판단한다.
다른 프로젝트의 utils/ 폴더에 이 파일을 그대로 복사해서 쓴다. 원본은 krx-holiday-updater/utils/holiday_util.py 이다.

- 테이블에는 KRX 휴장일만 있고 주말은 없다. 주말이 아니고 테이블에 없으면 거래일이다.
- 조회한 연도의 휴장일이 0건이면 아직 데이터가 없는 연도이므로 HolidayDataError 를 낸다.
  DB 접속이 실패해도 예외가 그대로 나간다. 어느 경우든 거래일로 가정하지 말고 실패로 처리한다.

사용 예:
    from utils.holiday_util import HolidayUtil

    holiday = HolidayUtil()
    if not holiday.is_today_trading_day():
        return
    base_date = holiday.get_previous_trading_day().strftime("%Y%m%d")
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pymysql
from dotenv import load_dotenv

load_dotenv()

KST = timezone(timedelta(hours=9))
HOLIDAY_TABLE = "mqway.krx_holiday"


class HolidayDataError(RuntimeError):
    """해당 연도의 휴장일 데이터가 없을 때 발생한다."""


class HolidayUtil:
    """mqway.krx_holiday 기반 KRX 거래일 판단.

    연도별 휴장일은 처음 조회할 때 한 번 읽고 인스턴스에 캐시한다.
    """

    def __init__(self, conn: Any = None, table: str = HOLIDAY_TABLE) -> None:
        """
        Args:
            conn: 이미 열려 있는 pymysql 연결입니다. 주면 그 연결을 쓰고 닫지 않습니다.
                None 이면 환경변수(DB_* 또는 MYSQL_*)로 조회할 때마다 접속했다가 닫습니다.
            table: 휴장일 테이블입니다. DB 이름을 붙여 두면 DB_NAME 이 다른 프로젝트에서도 조회됩니다.
        """
        self._conn = conn
        self._table = table
        self._cache: dict[int, dict[date, str]] = {}

    def get_holidays(self, year: int) -> dict[date, str]:
        """해당 연도의 KRX 휴장일(주말 제외)을 돌려준다.

        Returns:
            휴장일 날짜를 키, 휴장 사유를 값으로 하는 dict 입니다.

        Raises:
            HolidayDataError: 해당 연도 데이터가 없을 때 발생합니다.
        """
        if year not in self._cache:
            rows = self._query_year(year)
            if not rows:
                raise HolidayDataError(
                    f"{self._table} 에 {year}년 휴장일 데이터가 없습니다. krx-holiday-updater 실행 여부를 확인하세요."
                )
            self._cache[year] = {_to_date(day): name for day, name in rows}
        return dict(self._cache[year])

    def is_holiday(self, day: date | datetime | str) -> bool:
        """KRX 휴장일인지 확인한다. 주말은 따로 보지 않는다."""
        target = _to_date(day)
        return target in self._holidays_of(target.year)

    def get_holiday_name(self, day: date | datetime | str) -> str | None:
        """휴장 사유를 돌려준다. 휴장일이 아니면 None 이다. 주말은 None 이다."""
        target = _to_date(day)
        return self._holidays_of(target.year).get(target)

    def is_trading_day(self, day: date | datetime | str) -> bool:
        """주말도 휴장일도 아니면 거래일이다."""
        target = _to_date(day)
        return target.weekday() < 5 and not self.is_holiday(target)

    def is_today_trading_day(self) -> bool:
        """오늘(KST)이 거래일인지 확인한다."""
        return self.is_trading_day(today_kst())

    def get_previous_trading_day(self, day: date | datetime | str | None = None) -> date:
        """기준일 직전 거래일을 돌려준다. 기준일은 포함하지 않으며 기본값은 오늘(KST)이다."""
        return self._step_trading_day(day, -1)

    def get_next_trading_day(self, day: date | datetime | str | None = None) -> date:
        """기준일 다음 거래일을 돌려준다. 기준일은 포함하지 않으며 기본값은 오늘(KST)이다."""
        return self._step_trading_day(day, 1)

    def _step_trading_day(self, day: date | datetime | str | None, step: int) -> date:
        target = _to_date(day) if day is not None else today_kst()
        target += timedelta(days=step)
        while not self.is_trading_day(target):
            target += timedelta(days=step)
        return target

    def _holidays_of(self, year: int) -> dict[date, str]:
        if year not in self._cache:
            self.get_holidays(year)
        return self._cache[year]

    def _query_year(self, year: int) -> list[tuple[Any, str]]:
        sql = f"SELECT cal_date, holiday_name FROM {self._table} WHERE cal_date BETWEEN %s AND %s"
        conn = self._conn or _connect_from_env()
        try:
            # 호출하는 쪽 연결이 DictCursor 여도 튜플로 받도록 커서 종류를 고정한다.
            with conn.cursor(pymysql.cursors.Cursor) as cursor:
                cursor.execute(sql, (date(year, 1, 1), date(year, 12, 31)))
                return list(cursor.fetchall())
        finally:
            if self._conn is None:
                conn.close()


def today_kst() -> date:
    """한국 시간 기준 오늘 날짜."""
    return datetime.now(KST).date()


def _to_date(value: date | datetime | str) -> date:
    """date, datetime, 'YYYYMMDD', 'YYYY-MM-DD' 를 date 로 바꾼다."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"날짜 형식을 읽을 수 없습니다: {value!r} (YYYYMMDD 또는 YYYY-MM-DD)")


def _env(*keys: str, default: str = "") -> str:
    for key in keys:
        value = os.getenv(key, "").strip()
        if value:
            return value
    return default


def _connect_from_env() -> Any:
    """DB_* 환경변수로 접속한다. 없으면 MYSQL_* 를 쓴다. DB 는 지정하지 않고 테이블 이름의 DB 를 쓴다."""
    host = _env("DB_HOST", "MYSQL_HOST")
    user = _env("DB_USER", "MYSQL_USER")
    password = _env("DB_PASSWORD", "MYSQL_PASSWORD")
    if not host or not user:
        raise HolidayDataError("휴장일 DB 접속 정보가 없습니다. DB_HOST, DB_USER, DB_PASSWORD 를 설정하세요.")
    return pymysql.connect(
        host=host,
        port=int(_env("DB_PORT", "MYSQL_PORT", default="3306")),
        user=user,
        password=password,
        charset="utf8mb4",
        connect_timeout=10,
    )


# 모듈 테스트용
if __name__ == "__main__":
    util = HolidayUtil()
    today = today_kst()
    print(f"오늘 {today} 거래일 여부: {util.is_today_trading_day()}")
    print(f"직전 거래일: {util.get_previous_trading_day()}")
    print(f"다음 거래일: {util.get_next_trading_day()}")
    for day, name in sorted(util.get_holidays(today.year).items()):
        print(f"  {day} {name}")
