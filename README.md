# KRX Master Scanner

기존 노트북/단일 셀 형태의 한국 주식 마스터 스캐너를 로컬 Python 실행 형태로 정리한 버전입니다.

## 구성

- `main.py` : 실행 스크립트
- `.env` : Telegram Bot Token / Chat ID / 실행 설정
- `.env.example` : 환경변수 샘플
- `requirements.txt` : 필요한 Python 패키지
- `data/cache/` : FinanceDataReader OHLCV CSV 캐시. 종목별 고정 파일(`ohlcv_005930.csv`)에 누적 저장하고, 매일 실행 시 마지막 저장일 이후 구간만 추가 조회합니다.
- `data/reports/` : 스캔 결과 CSV, 통계 JSON
- `data/charts/` : 생성된 차트 이미지
- `logs/` : 실행 로그

## 설치

```bash
cd ~/apps/krx-master-scanner
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`.env`에서 아래 값만 실제 값으로 교체하세요.

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

현재 `.env`의 `TELEGRAM_CHAT_ID`는 원본 코드에 있던 값(`980596387`)을 넣어뒀고, 토큰은 원본 파일에서 마스킹되어 있어 placeholder 상태입니다.

## 실행

Telegram 전송 없이 테스트:

```bash
cd ~/apps/krx-master-scanner
source .venv/bin/activate
python main.py --dry-run --max-symbols 20 --no-charts
```

전체 실행:

```bash
python main.py
```

캐시 무시하고 재조회:

```bash
python main.py --force-refresh
```

병렬 수 조정:

```bash
python main.py --workers 2
```

## 리팩토링에서 반영한 필수 개선

- Telegram `BOT_TOKEN`, `CHAT_ID`를 `.env`로 분리
- `FinanceDataReader.DataReader` 호출에 종목별 고정 CSV 캐시 적용
- 매일 운영 시 기존 OHLCV 캐시를 재사용하고 마지막 저장일 이후 구간만 incremental refresh
- 종목별 OHLCV 조회에 재시도/backoff 적용
- 병렬 수 기본값을 `10`에서 `4`로 낮춰 차단 가능성 완화
- 1차 필터에서 가격/당일 거래대금 기준으로 OHLCV 조회 대상 축소
- 전 종목 분석 결과를 `found / skipped / failed`로 집계
- 주요 스킵 사유와 실패 사유를 `data/reports/scan_stats_*.json`으로 저장
- 통과 종목 전체를 `data/reports/scan_results_*.csv`로 저장
- 네이버 수급/뉴스 조회는 통과 종목에만 수행
- Telegram 메시지 3900자 단위 분할, 429 rate limit 재시도 처리
- 시장 지수 조회 실패 시 강세장으로 간주하지 않고 경고 표시
- 52주 고점/저점은 종가가 아니라 `High`/`Low` 기준으로 계산
- 손절가가 매수가보다 높거나 손절폭이 15% 초과인 후보 제외

## 주의

이 스캐너는 투자 참고용 후보 탐색 도구입니다. 자동매매나 매수/매도 권유 로직이 아닙니다.
