# KRX Master Scanner

KRX 종목 데이터를 MariaDB에 캐시하고, 종합분석과 VCP 스캔 결과를 텔레그램으로 전송하는 스캐너입니다.

## 구성

- `main.py`: 실행 트리거, 데이터 수집, 종합분석, VCP 파이프라인
- `analysis.py`: 퀀트 스캔 로직 (미너비니 필터 + 매물대 판독 + 스나이퍼 점수)
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

# VCP 핵심 운용 필터
VCP_MIN_AVG_TRADED_VALUE=5000000000
VCP_MAX_DROP_FROM_HIGH=0.25
VCP_MAX_PIVOT_GAP=0.15
VCP_MIN_CONTRACTION_SEGMENTS=2
VCP_MIN_SCORE=55
VCP_MIN_VOLUME_DRY_UP_RATIO=0.35
VCP_REQUIRE_MA_ALIGNMENT=false
```

후보 수와 운용 성격에 직접 영향을 주는 핵심 항목만 환경변수로 조정합니다.

- `VCP_MIN_AVG_TRADED_VALUE`: 최소 20일 평균 거래대금
- `VCP_MAX_DROP_FROM_HIGH`: 52주 고점 대비 최대 이격
- `VCP_MAX_PIVOT_GAP`: VCP 피벗까지 허용할 최대 이격
- `VCP_MIN_CONTRACTION_SEGMENTS`: 최소 수축 횟수(2T/3T 조정)
- `VCP_MIN_VOLUME_DRY_UP_RATIO`: 최근 거래량 최소 감소율
- `VCP_REQUIRE_MA_ALIGNMENT`: 50/150/200일선 완전 정렬 필수 여부
- `VCP_MIN_SCORE`: 최종 품질 점수 하한

VCP 정의에 가까운 값은 `vcp_scan.py` 상수로 고정합니다. 수축폭 순차 감소, 최종 수축 10% 미만, 피벗 돌파 후 최대 5% 확장, 120봉 수축 탐색, 피크 간격 10봉, 마지막 수축 거래량 90% 이하, 수축 간 거래량 감소 비중 50%, MA20/50/150/200, Pocket Pivot 20일 등이 여기에 해당합니다.

기본값은 최근 KRX 데이터에서 일주일 동안 중복 제외 3개 이상, 일일 약 3~5개의 관찰 후보를 확보하도록 보정했습니다. High-Low 수축폭은 2T 이상이면서 왼쪽에서 오른쪽으로 작아져야 하고, 최종 수축은 10% 미만이어야 합니다. 마지막 수축 구간 평균 거래량은 첫 수축의 90% 이하이고 수축 간 거래량 비교의 절반 이상이 감소 방향이어야 하며, 최근 거래량은 피크 대비 35% 이상 감소해야 합니다. 52주 고점 25% 이내와 피벗 15% 이내를 허용하되 피벗 위 5%를 넘긴 종목은 추격 후보에서 제외합니다. 완전한 50/150/200일선 정렬은 희소성을 고려해 기본 하드 필터가 아니라 품질 점수에 반영합니다.

후보는 0~100점 VCP 품질 점수로 정렬됩니다. 차트에는 T1~T6 수축폭, 피벗선, 52주 고점, MA20/MA50, Pocket Pivot, 거래량 MA10, 품질 등급·점수, 피벗 이격, 거래량 감소율과 최종 수축폭이 표시됩니다.

## 퀀트 스캔 스나이퍼 판독

퀀트 스캔 후보에는 기존 필터 결과에 더해 다음 판독이 추가됩니다.

- 매물대: 최근 120거래일 가격대별 거래량에서 최대 매물 집중 구간(POC)을 계산하고, 현재가 위치에 따라 `돌파 / 돌파 시도 / 임박 / 대기`로 판독합니다.
- VCP 수축폭: 최근 고점-저점 수축 구간의 하락률 추이(예: `-14.8% → -8.9% → -4.4%`)를 실측해 VCP 패턴 라인에 함께 표시합니다.
- 스나이퍼 점수: 매물대 위치, 수축폭과 감소 추세, RS 점수, 거래량, 셋업 등급(별점), NR3/쿨라메기 HTF를 가중 합산한 0~100 규칙 기반 점수와 `적극 / 중립 / 관망` 판정입니다. 통계 모델 예측이 아니라 투명한 규칙 합산 점수입니다.
- 차트: 매물대 밴드와 대표가 라인, 수축 지그재그(% 라벨), 20/50/200일 이동평균, 타점 분석 요약 박스가 함께 그려집니다. 조회 기간은 400일, 표시 구간은 최근 120봉입니다.

텔레그램 메시지에는 `🎯 [스나이퍼 판독]` 섹션으로 판독 문구와 매물대 구간이 추가되고, DB `result_json`에도 동일 필드(`supply_zone_*`, `sniper_*`, `vcp_contraction_*`)가 저장됩니다.

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
