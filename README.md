# 항공권 최저가 트래커 (무료 구성)

fast-flights(Google Flights 비공식 스크레이퍼) + GitHub Actions 스케줄로 가격을 모아 `data/prices.csv`에 누적합니다.

## 쓰는 법
1. `routes.json`에서 노선, 날짜, 목표가(`target_price_krw`)를 수정합니다.
2. 이 폴더를 GitHub 저장소로 올립니다.
3. (선택) 텔레그램 알림: 저장소 Settings > Secrets and variables > Actions에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`를 등록합니다.
4. Actions 탭에서 `track-flights`를 수동 실행(Run workflow)해 첫 결과를 확인합니다. 이후 매일 2회 자동 실행됩니다.

## 로컬 테스트
```
pip install -r requirements.txt
python tracker.py
```

## 주의
- 비공식 스크레이핑이라 Google 쪽 변경이나 차단으로 조회가 실패할 수 있습니다. 실패해도 해당 건만 건너뜁니다.
- 조회 간격(`pause_seconds`)을 너무 줄이거나 노선·날짜를 과하게 늘리지 마세요.
- 수집된 가격은 Google Flights 화면 기준이며, 예약 전 항공사 사이트에서 최종 가격을 확인하세요.
