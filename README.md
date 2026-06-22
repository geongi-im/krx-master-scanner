# KRX Master Scanner

KRX 종목 데이터를 MariaDB에 캐시하고, 종합분석과 VCP 스캔 결과를 텔레그램으로 전송하는 스캐너입니다.

## 구성

- `main.py`: 실행 트리거, 데이터 수집, 종합분석, VCP 파이프라인
- `analysis.py`: 기존 종합분석 로직
- `vcp_scan.py`: MariaDB bulk OHLCV 로딩 기반 VCP 후보 스캔과 차트 생성
- `db_scheme.py`: MariaDB 테이블명과 스키마 생성 SQL
- `data/charts/`: 종합분석/VCP 차트 이미지
- `logs/`: 실행 로그

## 환경변수

`.env.example`을 참고해 `.env`를 구성합니다.

```env
TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN_HERE
TELEGRAM_CHAT_ID=YOUR_CHAT_ID_HERE

# MariaDB
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=krx_scanner
DB_USER=krx_user
DB_PASSWORD=CHANGE_ME
DB_TABLE_PREFIX=kms

# 공통 실행 설정
MAX_WORKERS=2
CACHE_TTL_HOURS=18
FETCH_RETRIES=3
REQUEST_TIMEOUT=10
TOP_SEND_LIMIT=20
SEND_CHARTS=true
FORCE_REFRESH=false

# 데이터 수집
COLLECT_ENABLED=true
COLLECT_DAYS=600

# 메인 분석 필터
FIRST_PASS_MIN_CLOSE=500
FIRST_PASS_MIN_AMOUNT=1000000000
MIN_AVG_TURNOVER=1000000000
MIN_ADR=1.5

# VCP 분석
VCP_ENABLED=true

# VCP 후보 필터
# 높이면 거래대금이 큰 종목만 남습니다.
VCP_MIN_AVG_TRADED_VALUE=15000000000
# 낮추면 52주 고점에 더 가까운 종목만 남습니다.
VCP_MAX_DROP_FROM_HIGH=0.18
# 낮추면 최근 피벗 고점에 더 가까운 종목만 남습니다.
VCP_MAX_PIVOT_GAP=0.15
# 낮추면 수축폭 감소가 더 뚜렷한 종목만 남습니다.
VCP_MAX_CONTRACTION_RATIO=1.0
# 높이면 최근 Pocket Pivot이 더 많이 나온 종목만 남습니다.
VCP_MIN_POCKET_PIVOT_COUNT=1
```

VCP 튜닝 변수는 후보 수와 품질에 직접 영향을 주는 5개만 환경변수로 둡니다.

- `VCP_MIN_AVG_TRADED_VALUE`: 20일 평균 거래대금 하한입니다. 높이면 유동성 큰 종목만 남습니다.
- `VCP_MAX_DROP_FROM_HIGH`: 52주 고점 대비 허용 이격입니다. 낮추면 신고가에 더 가까운 종목만 남습니다.
- `VCP_MAX_PIVOT_GAP`: 최근 20일 고점 대비 허용 이격입니다. 낮추면 돌파 지점에 가까운 종목만 남습니다.
- `VCP_MAX_CONTRACTION_RATIO`: 마지막 수축폭이 직전 수축폭 대비 허용되는 비율입니다. `1.0`이면 마지막 수축폭이 직전보다 같거나 작아야 합니다.
- `VCP_MIN_POCKET_PIVOT_COUNT`: 최근 14일 Pocket Pivot 최소 횟수입니다. 높이면 수급 확인 기준이 엄격해집니다.

VCP 엔진은 추가로 최근 수축폭이 단계적으로 작아지고, 최종 수축폭이 5% 미만이며, 거래량이 피크 대비 70% 이상 줄어드는 dry-up 구간을 요구합니다. 후보 차트에는 Pocket Pivot 발생일을 `PP` 마커로 표시합니다.

`VCP_ENABLED`는 VCP 파이프라인 실행 여부만 제어합니다. 차트 저장 위치는 `data/charts/`로 고정되며, 생성 후 3일이 지난 PNG 차트는 새 차트 저장 시 자동 삭제됩니다. VCP 조회 기간은 내부 계산 기준으로 400일을 사용하고, 텔레그램 발송 상한은 공통 `TOP_SEND_LIMIT`를 따릅니다.

## 실행

테스트 실행:

```bash
python main.py --dry-run --max-symbols 20 --no-charts
```

전체 실행:

```bash
python main.py
```

`python main.py`는 전체 OHLCV 데이터를 MariaDB에 먼저 수집한 뒤 종합분석을 실행하고, 이어서 VCP 스캔과 텔레그램 전송을 실행합니다. 종합분석만 실행하려면 `--no-vcp`를 사용합니다.

특정 기준일 실행:

```bash
python main.py --target-date 2026-06-19
python main.py --target-date 2026-06-19 --dry-run
```

`--target-date`는 해당 날짜 장마감 이후 기준으로 동작합니다. DB 캐시에 기준일 데이터가 충분히 있으면 전체 OHLCV 수집 루프를 생략하고 바로 시장국면, Analysis, VCP를 실행합니다. 캐시 커버리지가 부족하거나 확인에 실패하면 기존처럼 수집 단계로 fallback합니다.

텔레그램 메시지는 `시장국면 -> Analysis -> VCP` 순서로 전송됩니다. 시장국면 메시지에는 `2026년 6월 19일(금)` 형식의 타겟 날짜와 요일이 함께 표시됩니다.

VCP 스캔은 MariaDB에서 종목별로 반복 조회하지 않고, 스캔 기간의 OHLCV를 bulk로 읽은 뒤 종목별로 그룹화해서 계산합니다. `--force-refresh`나 `--no-project-cache`를 사용하는 경우에는 외부 데이터 조회 경로를 사용합니다.

VCP 단독 실행:

```bash
python vcp_scan.py
python vcp_scan.py --target-date 2026-06-19
```

OHLCV 캐시 품질 정리:

```bash
python main.py --repair-db-cache
```

이 옵션은 OHLC가 0 이하인 캐시 row를 삭제하고, `kms_ohlcv_cache_meta`를 실제 유효 OHLCV row 기준으로 재계산합니다.

## MariaDB 스키마

테이블명과 `CREATE TABLE` SQL은 `db_scheme.py`에 모아둡니다. 런타임은 필요한 테이블이 없으면 이 파일의 스키마 기준으로 자동 생성합니다.
