"""항공권 스크리닝 트래커.

매 실행마다 (출발공항 x 도착공항) 조합에서 출발일을 무작위로 일부만 골라 왕복 최저가를 조회하고,
월별 CSV에 누적한 뒤, 같은 노선의 최근 중앙값보다 충분히 싸면 알림(deals.csv, 텔레그램)을 남긴다.
"""
import csv
import json
import os
import random
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fast_flights import FlightQuery, Passengers, create_query, get_flights

try:  # 라이브러리 버전에 따라 없을 수 있음
    from fast_flights import FlightsNotFound
except ImportError:
    class FlightsNotFound(Exception):
        pass

PARSE_ERRORS = (IndexError, KeyError, TypeError, ValueError, AttributeError)
ROOT = Path(__file__).parent
KST = timezone(timedelta(hours=9))
_debug_done = False

PRICE_FIELDS = [
    "collected_at", "region", "origin", "destination", "depart_date", "return_date", "nights",
    "price_krw", "airlines", "stops", "depart_time", "arrive_time", "duration_min",
]
DEAL_FIELDS = [
    "collected_at", "region", "dest_name", "origin", "destination", "depart_date", "return_date",
    "nights", "price_krw", "median_krw", "discount_pct", "airlines", "depart_time", "arrive_time",
]
SUMMARY_FIELDS = [
    "origin", "destination", "dest_name", "region", "samples", "median_krw",
    "min_krw", "min_depart_date", "min_return_date", "min_airlines", "updated_at",
]


# ---------- 유틸 ----------
def fmt_time(t) -> str:
    """[8] 처럼 분이 생략되거나 None이 섞인 시간 표기도 처리."""
    vals = list(getattr(t, "time", None) or [])
    vals += [None, None]
    return f"{(vals[0] or 0):02d}:{(vals[1] or 0):02d}"


def debug_response(q) -> None:
    """실패 시 구글이 실제로 무엇을 돌려줬는지 요약(첫 1회만)."""
    global _debug_done
    if _debug_done:
        return
    _debug_done = True
    try:
        from fast_flights import fetch_flights_html
        html = fetch_flights_html(q)
        html = html if isinstance(html, str) else str(getattr(html, "text", html))
        low = html.lower()
        title = html.split("<title>", 1)[1].split("</title>", 1)[0][:80] if "<title>" in html else "(없음)"
        print("  [진단] 응답 길이:", len(html), "/ 제목:", title)
        print("  [진단] ds:1 데이터 포함:", "ds:1" in html,
              "/ 동의화면 단서:", "consent" in low,
              "/ 비정상 트래픽 단서:", "unusual traffic" in low or "captcha" in low)
    except Exception as e:
        print(f"  [진단] 응답 확인 실패: {type(e).__name__}: {e}", file=sys.stderr)


def send_telegram(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=15)
    except Exception as e:
        print(f"텔레그램 전송 실패: {e}", file=sys.stderr)


def append_csv(path: Path, fields: list, rows: list) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerows(rows)


def read_csv(path: Path) -> list:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ---------- 조회 ----------
def search(origin: str, dest: str, depart: date, nights: int, cfg: dict):
    """반환: (상태, 최저가 항목, 귀국일). 상태는 ok / empty / error."""
    ret = depart + timedelta(days=nights)
    ms = cfg.get("max_stops")
    q = create_query(
        flights=[
            FlightQuery(date=depart.isoformat(), from_airport=origin, to_airport=dest, max_stops=ms),
            FlightQuery(date=ret.isoformat(), from_airport=dest, to_airport=origin, max_stops=ms),
        ],
        seat="economy", trip="round-trip", passengers=Passengers(adults=1),
        language=cfg["language"], currency=cfg["currency"],
    )
    results = None
    for attempt in range(2):
        try:
            results = get_flights(q)
            break
        except FlightsNotFound:
            return "empty", None, ret
        except PARSE_ERRORS as e:  # 응답 구조를 읽지 못함: 재시도해도 같음
            print(f"  응답 해석 실패: {type(e).__name__}: {e}", file=sys.stderr)
            debug_response(q)
            return "error", None, ret
        except Exception as e:  # 네트워크 오류 등
            print(f"  재시도 {attempt + 1}/2: {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    if results is None:
        return "error", None, ret
    diag = getattr(results, "diagnostics", None)
    if diag is not None and getattr(diag, "status", "ok") in ("blocked", "http_error"):
        print(f"  진단: {diag}", file=sys.stderr)
        debug_response(q)
        return "error", None, ret
    priced = [r for r in results if r.price and r.price > 0]
    if not priced:
        return "empty", None, ret
    return "ok", min(priced, key=lambda r: r.price), ret


# ---------- 계획 ----------
def build_pairs(cfg: dict) -> list:
    pairs = []
    for origin in cfg["origins"]:
        for region, items in cfg["destinations"].items():
            for code, name in items.items():
                pairs.append({"origin": origin, "dest": code, "name": name, "region": region})
    return pairs


def plan_tasks(cfg: dict, pairs: list, state: dict, today: date) -> list:
    active = [p for p in pairs
              if state["pairs"].get(f"{p['origin']}-{p['dest']}", {}).get("skip_until", "") < today.isoformat()]
    if not active:
        return []
    first = today + timedelta(days=cfg["start_offset_days"])
    window = [first + timedelta(days=i) for i in range(cfg["horizon_days"])]
    per_pair = max(1, cfg["queries_per_run"] // len(active))
    tasks = []
    for p in active:
        for d in random.sample(window, min(per_pair, len(window))):
            tasks.append({**p, "depart": d, "nights": random.choice(cfg["nights"])})
    random.shuffle(tasks)
    return tasks[: cfg["queries_per_run"]]


# ---------- 기준선과 알림 ----------
def load_recent_prices(data_dir: Path, today: date, lookback_days: int) -> list:
    rows = []
    cutoff = today - timedelta(days=lookback_days)
    months = {(cutoff.year, cutoff.month), (today.year, today.month)}
    cur = date(cutoff.year, cutoff.month, 1)
    while (cur.year, cur.month) <= (today.year, today.month):
        months.add((cur.year, cur.month))
        cur = date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
    for y, m in sorted(months):
        for r in read_csv(data_dir / f"prices-{y}-{m:02d}.csv"):
            try:
                if date.fromisoformat(r["collected_at"][:10]) >= cutoff:
                    r["price_krw"] = int(r["price_krw"])
                    rows.append(r)
            except (ValueError, KeyError):
                continue
    return rows


def main() -> int:
    t0 = time.time()
    cfg = json.loads((ROOT / "routes.json").read_text(encoding="utf-8"))
    data_dir = ROOT / "data"
    now_kst = datetime.now(KST)
    today = now_kst.date()
    stamp = now_kst.strftime("%Y-%m-%d %H:%M")

    state_path = data_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    state.setdefault("pairs", {})

    pairs = build_pairs(cfg)
    tasks = plan_tasks(cfg, pairs, state, today)
    print(f"전체 조합 {len(pairs)}개 중 이번 조회 {len(tasks)}건 (제외된 조합 제외)")

    rows, counts = [], {"ok": 0, "empty": 0, "error": 0}
    consecutive_errors, aborted = 0, False
    for i, t in enumerate(tasks, 1):
        if (time.time() - t0) / 60 > cfg.get("max_minutes", 100):
            print("시간 예산을 모두 사용해 여기서 마무리합니다.")
            break
        key = f"{t['origin']}-{t['dest']}"
        st = state["pairs"].setdefault(key, {"empty": 0, "ok": 0, "skip_until": ""})
        if st.get("skip_until", "") >= today.isoformat():
            continue  # 이번 실행 중 제외 판정된 조합
        status, best, ret = search(t["origin"], t["dest"], t["depart"], t["nights"], cfg)
        counts[status] += 1
        if status == "ok":
            consecutive_errors = 0
            st["ok"] += 1
            st["empty"] = 0
            first, last = best.flights[0], best.flights[-1]
            rows.append({
                "collected_at": stamp, "region": t["region"], "origin": t["origin"],
                "destination": t["dest"], "depart_date": t["depart"].isoformat(),
                "return_date": ret.isoformat(), "nights": t["nights"], "price_krw": best.price,
                "airlines": "/".join(best.airlines), "stops": max(len(best.flights) - 1, 0),
                "depart_time": fmt_time(first.departure), "arrive_time": fmt_time(last.arrival),
                "duration_min": sum(s.duration for s in best.flights),
            })
        elif status == "empty":
            consecutive_errors = 0
            st["empty"] += 1
            limit = cfg.get("empty_limit_new", 5) if st["ok"] == 0 else cfg.get("empty_limit_known", 15)
            if st["empty"] >= limit:
                st["skip_until"] = (today + timedelta(days=cfg.get("skip_days", 14))).isoformat()
                st["empty"] = 0
                print(f"  {key}: 결과가 계속 없어 {cfg.get('skip_days', 14)}일간 조회에서 제외")
        else:
            consecutive_errors += 1
            if consecutive_errors >= cfg.get("abort_after_errors", 12):
                print("연속 오류가 많아(차단 가능성) 이번 실행을 중단합니다.", file=sys.stderr)
                aborted = True
                break
        if i % 50 == 0:
            print(f"  진행 {i}/{len(tasks)} (저장 {counts['ok']}, 결과없음 {counts['empty']}, 오류 {counts['error']})")
        time.sleep(cfg.get("pause_seconds", 2))

    # 저장
    month_file = data_dir / f"prices-{today.year}-{today.month:02d}.csv"
    append_csv(month_file, PRICE_FIELDS, rows)
    state["updated_at"] = stamp
    data_dir.mkdir(exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    # 기준선(중앙값) 계산 및 알림
    acfg = cfg.get("alert", {})
    recent = load_recent_prices(data_dir, today, acfg.get("lookback_days", 60))
    by_pair: dict = {}
    for r in recent:
        by_pair.setdefault((r["origin"], r["destination"]), []).append(r)

    names = {(p["origin"], p["dest"]): (p["name"], p["region"]) for p in pairs}
    deals_path = data_dir / "deals.csv"
    old_deals = {}
    for r in read_csv(deals_path):
        old_deals[(r["origin"], r["destination"], r["depart_date"], r["return_date"])] = (r["collected_at"][:10], int(r["price_krw"]))

    new_deals = []
    for r in rows:
        samples = by_pair.get((r["origin"], r["destination"]), [])
        if len(samples) < acfg.get("min_samples", 12):
            continue
        med = statistics.median(s["price_krw"] for s in samples)
        if r["price_krw"] > med * acfg.get("ratio", 0.75):
            continue
        k = (r["origin"], r["destination"], r["depart_date"], r["return_date"])
        prev = old_deals.get(k)
        if prev and (today - date.fromisoformat(prev[0])).days <= 14 and r["price_krw"] >= prev[1] * 0.97:
            continue  # 이미 비슷한 가격으로 알린 건
        name = names.get((r["origin"], r["destination"]), (r["destination"], ""))[0]
        new_deals.append({
            **r, "dest_name": name, "median_krw": int(med),
            "discount_pct": round((1 - r["price_krw"] / med) * 100),
        })
        old_deals[k] = (today.isoformat(), r["price_krw"])
    new_deals.sort(key=lambda d: -d["discount_pct"])
    append_csv(deals_path, DEAL_FIELDS, new_deals)
    for d in new_deals[:10]:
        msg = (f"✈ {d['origin']}→{d['destination']} {d['dest_name']} "
               f"{d['depart_date']}~{d['return_date']}({d['nights']}박) {d['price_krw']:,}원 "
               f"(평소 중앙값 {d['median_krw']:,}원 대비 -{d['discount_pct']}%) {d['airlines']} {d['depart_time']}→{d['arrive_time']}")
        print(msg)
        send_telegram(msg)
    if len(new_deals) > 10:
        send_telegram(f"그 외 {len(new_deals) - 10}건은 data/deals.csv에서 확인하세요.")

    # 노선별 요약
    summary = []
    for (o, d), lst in sorted(by_pair.items()):
        cheapest = min(lst, key=lambda s: s["price_krw"])
        nm, rg = names.get((o, d), (d, ""))
        summary.append({
            "origin": o, "destination": d, "dest_name": nm, "region": rg, "samples": len(lst),
            "median_krw": int(statistics.median(s["price_krw"] for s in lst)),
            "min_krw": cheapest["price_krw"], "min_depart_date": cheapest["depart_date"],
            "min_return_date": cheapest["return_date"], "min_airlines": cheapest["airlines"],
            "updated_at": stamp,
        })
    summary_path = data_dir / "summary.csv"
    if summary:
        with summary_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
            w.writeheader()
            w.writerows(summary)

    print(f"완료: 저장 {len(rows)}건 / 결과없음 {counts['empty']}건 / 오류 {counts['error']}건 / "
          f"새 알림 {len(new_deals)}건 / 소요 {(time.time() - t0) / 60:.1f}분")
    return 1 if (aborted and not rows) or (not rows and counts["error"] and not counts["empty"]) else 0


if __name__ == "__main__":
    sys.exit(main())

