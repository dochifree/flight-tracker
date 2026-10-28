# 항공권 스크리닝 트래커 (무료 구성)

fast-flights 계열 라이브러리(Google Flights 비공식 스크레이퍼) + GitHub Actions 스케줄로,
인천/김포 출발 일본·중국·동남아 왕복 최저가를 무작위 표본으로 계속 조회해 월별 CSV에 쌓고,
같은 노선의 최근 중앙값보다 크게 싸면 `data/deals.csv`(와 텔레그램)에 알립니다.

## 결과 파일 (data 폴더)
- `deals.csv`: 알림 대상(평소보다 싼 건). 가장 먼저 보실 파일
- `summary.csv`: 노선별 표본 수, 중앙값, 최저가와 그 날짜
- `prices-YYYY-MM.csv`: 조회된 모든 가격 기록(월별로 이어쓰기)
- `state.json`: 직항이 없어 잠시 제외된 노선 등 내부 상태 (직접 고치지 않으셔도 됩니다)
- (예전 예시 실행으로 생긴 `prices.csv`는 더 이상 쓰지 않으니 지우셔도 됩니다)

## 설정 (routes.json)
- `origins`, `destinations`: 출발·도착 공항. 줄을 지우거나 추가하면 됩니다.
- `nights`: 박수 후보. 조회 때마다 이 중 하나를 무작위로 씁니다.
- `start_offset_days`, `horizon_days`: 조회 당일 기준 며칠 뒤부터, 며칠 범위의 출발일을 볼지
- `max_stops`: 0이면 직항만, 1이면 1회 경유까지, 지우면 제한 없음
- `queries_per_run`: 1회 실행 조회 건수, `pause_seconds`: 조회 간격
- `alert.ratio`: 중앙값의 몇 % 이하일 때 알림(0.75 = 25% 이상 저렴), `min_samples`: 기준선 최소 표본 수

## 텔레그램 알림(선택)
저장소 Settings > Secrets and variables > Actions에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`를 등록합니다.

## 주의
- 비공식 스크레이핑이라 Google 쪽 변경이나 차단으로 조회가 실패할 수 있습니다. 오류가 연속되면 실행을 중단합니다.
- 기준선은 표본이 쌓여야(노선당 12건 이상) 작동하므로 알림은 며칠 뒤부터 나옵니다.
- 가격은 Google Flights 화면 기준이며, 예약 전 항공사 사이트에서 최종 가격과 수하물 조건을 확인하세요.
