"""항공권 최저가 추적기: fast-flights로 조회 -> CSV 누적 -> 조건 충족 시 알림."""
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query, get_flights

ROOT = Path(__file__).parent
CSV_PATH = ROOT / "data" / "prices.csv"
ALERT_PATH = ROOT / "data" / "alerts.txt"
FIELDS = [
    "collected_at", "route", "origin", "destination", "depart_date", "return_date",
    "price_krw", "airlines", "stops", "depart_time", "arrive_time", "duration_min",
]
KST = timezone(timedelta(hours=9))


def load_history() -> dict:
    """(route, depart_date) -> 지금까지의 최저가"""
    best: dict = {}
    if not CSV_PATH.exists():
        return best
    with CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key = (row["route"], row["depart_date"])
            price = int(row["price_krw"])
            best[key] = min(best.get(key, price), price)
    return best


def search(route: dict, depart: str, cfg: dict):
    ret = None
    flights = [FlightQuery(date=depart, from_airport=route["origin"], to_airport=route["destination"],
                           max_stops=route.get("max_stops"))]
    if route["trip"] == "round-trip":
        ret = (date.fromisoformat(depart) + timedelta(days=route["stay_days"])).isoformat()
        flights.append(FlightQuery(date=ret, from_airport=route["destination"], to_airport=route["origin"],
                                   max_stops=route.get("max_stops")))
    q = create_query(
        flights=flights, seat="economy", trip=route["trip"],
        passengers=Passengers(adults=1), language=cfg["language"], currency=cfg["currency"],
    )
    for attempt in range(3):
        try:
            results = get_flights(q)
            break
        except FlightsNotFound:
            return None, ret
        except Exception as e:  # 네트워크 오류, 차단 등
            print(f"  재시도 {attempt + 1}/3: {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(10 * (attempt + 1))
    else:
        return None, ret
    priced = [r for r in results if r.price and r.price > 0]
    return (min(priced, key=lambda r: r.price) if priced else None), ret


def fmt_time(t) -> str:
    return f"{t.time[0]:02d}:{t.time[1]:02d}"


def notify(text: str) -> None:
    ALERT_PATH.parent.mkdir(exist_ok=True)
    with ALERT_PATH.open("a", encoding="utf-8") as f:
        f.write(text + "\n")
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=15)
        except Exception as e:
            print(f"텔레그램 전송 실패: {e}", file=sys.stderr)


def main() -> int:
    cfg = json.loads((ROOT / "routes.json").read_text(encoding="utf-8"))
    history = load_history()
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    rows, alerts, failures = [], [], 0

    for route in cfg["routes"]:
        for depart in route["dates"]:
            print(f"조회: {route['name']} {depart}")
            best, ret = search(route, depart, cfg)
            time.sleep(cfg.get("pause_seconds", 4))
            if best is None:
                failures += 1
                print("  결과 없음 또는 조회 실패")
                continue
            first = best.flights[0]
            last = best.flights[-1]
            rows.append({
                "collected_at": now, "route": route["name"], "origin": route["origin"],
                "destination": route["destination"], "depart_date": depart, "return_date": ret or "",
                "price_krw": best.price, "airlines": "/".join(best.airlines),
                "stops": max(len(best.flights) - 1, 0),
                "depart_time": fmt_time(first.departure), "arrive_time": fmt_time(last.arrival),
                "duration_min": sum(s.duration for s in best.flights),
            })
            prev = history.get((route["name"], depart))
            reasons = []
            if best.price <= route["target_price_krw"]:
                reasons.append(f"목표가 {route['target_price_krw']:,}원 이하")
            if prev is not None and best.price < prev:
                reasons.append(f"기존 최저가 {prev:,}원 경신")
            if reasons:
                alerts.append(f"✈ {route['name']} {depart}{' ~ ' + ret if ret else ''}: "
                              f"{best.price:,}원 ({', '.join(reasons)}) / {rows[-1]['airlines']} "
                              f"{rows[-1]['depart_time']}→{rows[-1]['arrive_time']}")

    CSV_PATH.parent.mkdir(exist_ok=True)
    new_file = not CSV_PATH.exists()
    with CSV_PATH.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(rows)

    for a in alerts:
        print(a)
        notify(a)
    print(f"완료: 저장 {len(rows)}건, 알림 {len(alerts)}건, 실패 {failures}건")
    return 1 if rows == [] and failures else 0


if __name__ == "__main__":
    sys.exit(main())
