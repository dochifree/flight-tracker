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
# summary.csv: (내부 키, 화면에 보이는 열 이름). 모든 가격은 왕복 검색 결과의 가격입니다.
SUMMARY_COLUMNS = [
    ("region", "지역"),
    ("origin_name", "출발"),
    ("dest_name", "도착"),
    ("route", "구간"),
    ("tier", "항공사 유형"),
    ("samples", "표본수"),
    ("median_krw", "중앙값 왕복(원)"),
    ("min_krw", "최저가 왕복(원)"),
    ("discount_pct", "최저가 할인율(%)"),
    ("min_nights", "최저가 박수"),
    ("min_leave", "최저가 연차(일)"),
    ("min_depart_date", "최저가 출발일"),
    ("min_return_date", "최저가 귀국일"),
    ("min_airlines", "최저가 항공사"),
    ("min_stops", "경유 횟수"),
    ("min_times", "가는편 시간"),
    ("min_seen", "최저가 확인일"),
    ("recent7_min_krw", "최근7일 최저가(원)"),
    ("updated_at", "갱신시각"),
]
ORIGIN_NAMES = {"ICN": "인천", "GMP": "김포"}


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


def leave_days(depart: date, ret: date, holidays: set) -> int:
    """출발일~귀국일(양끝 포함) 중 평일이면서 공휴일이 아닌 날 수 = 필요한 연차 일수.
    (출발일 저녁 비행기라서 연차가 덜 필요할 수 있지만, 보수적으로 하루로 셉니다.)"""
    n, d = 0, depart
    while d <= ret:
        if d.weekday() < 5 and d.isoformat() not in holidays:
            n += 1
        d += timedelta(days=1)
    return n


def leave_band(leave: int, split: int) -> str:
    """기준선(중앙값)은 연차를 적게 쓰는 일정끼리, 많이 쓰는 일정끼리 따로 계산한다."""
    return "low" if leave <= split else "high"


def plan_tasks(cfg: dict, pairs: list, state: dict, today: date) -> list:
    active = [p for p in pairs
              if state["pairs"].get(f"{p['origin']}-{p['dest']}", {}).get("skip_until", "") < today.isoformat()]
    if not active:
        return []
    first = today + timedelta(days=cfg["start_offset_days"])
    window = [first + timedelta(days=i) for i in range(cfg["horizon_days"])]
    per_pair = max(1, cfg["queries_per_run"] // len(active))
    holidays, split = set(cfg.get("holidays", [])), cfg.get("low_leave_max", 2)
    # 연차를 적게 쓰는 일정(low)과 많이 쓰는 일정(high)을 나눠 두고, low 쪽을 더 많이 뽑는다.
    # (연차 제한은 두지 않는다. 연차를 더 써도 싸면 알림 대상이다.)
    low, high = [], []
    for d in window:
        for n in cfg["nights"]:
            (low if leave_days(d, d + timedelta(days=n), holidays) <= split else high).append((d, n))
    n_low = min(len(low), round(per_pair * cfg.get("low_leave_share", 0.6)))
    n_high = min(len(high), per_pair - n_low)
    tasks = []
    for p in active:
        picks = random.sample(low, n_low) + random.sample(high, n_high)
        for d, n in picks:
            tasks.append({**p, "depart": d, "nights": n})
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


def carrier_group(airlines: str, groups: dict) -> str:
    """항공사 이름으로 비교 그룹을 정한다(저비용/대형). 목록에 없으면 그 항공사끼리만 비교한다."""
    first = (airlines or "").split("/")[0].strip()
    for label, keywords in groups.items():
        if any(k.lower() in first.lower() for k in keywords):
            return label
    return first or "미상"


def _row(o, d, tier, lst, pool, names, today, stamp):
    """lst: 기준선(중앙값)용 전체 표본, pool: 최저가를 고를 후보."""
    recent_cut = (today - timedelta(days=7)).isoformat()
    cheapest = min(pool, key=lambda s: s["price_krw"])
    med = int(statistics.median(s["price_krw"] for s in lst))
    recent = [s["price_krw"] for s in pool if s["collected_at"][:10] >= recent_cut]
    nm, rg = names.get((o, d), (d, ""))
    return {
        "region": rg, "origin_name": ORIGIN_NAMES.get(o, o), "dest_name": nm,
        "route": f"{o}→{d}", "tier": tier, "samples": len(lst), "median_krw": med,
        "min_krw": cheapest["price_krw"],
        "discount_pct": round((1 - cheapest["price_krw"] / med) * 100) if med else 0,
        "min_nights": cheapest.get("nights", ""), "min_leave": cheapest.get("leave", ""),
        "min_depart_date": cheapest["depart_date"], "min_return_date": cheapest["return_date"],
        "min_airlines": cheapest["airlines"], "min_stops": cheapest.get("stops", ""),
        "min_times": f"{cheapest.get('depart_time', '')}→{cheapest.get('arrive_time', '')}",
        "min_seen": cheapest["collected_at"][:10],
        "recent7_min_krw": min(recent) if recent else "",
        "updated_at": stamp,
    }


def _sort(rows: list) -> list:
    rows.sort(key=lambda r: (-r["discount_pct"], r["min_krw"]))
    return rows


def build_summary(by_pair: dict, names: dict, today: date, stamp: str) -> list:
    """노선x항공사유형별 요약(출발 전인 항목 중 최저가). 할인율 큰 줄이 맨 위."""
    out = []
    for (o, d, tier, _band), lst in by_pair.items():
        fut = [s for s in lst if s["depart_date"] > today.isoformat()]
        if fut:
            out.append(_row(o, d, tier, lst, fut, names, today, stamp))
    return _sort(out)


def build_offers(by_pair: dict, names: dict, today: date, stamp: str) -> list:
    """노선x항공사유형x박수x연차별 최저가. 웹페이지에서 박수·연차 조건으로 걸러 볼 때 쓴다."""
    out = []
    for (o, d, tier, _band), lst in by_pair.items():
        buckets: dict = {}
        for s in lst:
            if s["depart_date"] > today.isoformat():
                buckets.setdefault((s.get("nights"), s.get("leave")), []).append(s)
        for pool in buckets.values():
            out.append(_row(o, d, tier, lst, pool, names, today, stamp))
    return _sort(out)


def main() -> int:
    summary_only = "--summary-only" in sys.argv  # 조회 없이 요약 파일만 다시 만들 때
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
    tasks = [] if summary_only else plan_tasks(cfg, pairs, state, today)
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
    groups = cfg.get("carrier_groups", {})
    holidays, split = set(cfg.get("holidays", [])), cfg.get("low_leave_max", 2)
    by_pair: dict = {}  # (출발, 도착, 항공사 유형, 연차 구분) 별로 따로 모아 같은 조건끼리만 비교
    for r in recent:
        try:
            r["leave"] = leave_days(date.fromisoformat(r["depart_date"]), date.fromisoformat(r["return_date"]), holidays)
        except (ValueError, KeyError):
            continue
        r["tier"] = carrier_group(r.get("airlines", ""), groups)
        by_pair.setdefault((r["origin"], r["destination"], r["tier"], leave_band(r["leave"], split)), []).append(r)

    names = {(p["origin"], p["dest"]): (p["name"], p["region"]) for p in pairs}
    deals_path = data_dir / "deals.csv"
    old_deals = {}
    for r in read_csv(deals_path):
        old_deals[(r["origin"], r["destination"], r["depart_date"], r["return_date"])] = (r["collected_at"][:10], int(r["price_krw"]))

    new_deals = []
    for r in rows:
        r["leave"] = leave_days(date.fromisoformat(r["depart_date"]), date.fromisoformat(r["return_date"]), holidays)
        tier = carrier_group(r["airlines"], groups)
        samples = by_pair.get((r["origin"], r["destination"], tier, leave_band(r["leave"], split)), [])
        if len(samples) < acfg.get("min_samples", 8):
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
            **r, "dest_name": name, "tier": tier, "median_krw": int(med),
            "discount_pct": round((1 - r["price_krw"] / med) * 100),
        })
        old_deals[k] = (today.isoformat(), r["price_krw"])
    new_deals.sort(key=lambda d: -d["discount_pct"])
    append_csv(deals_path, DEAL_FIELDS, new_deals)
    for d in new_deals[:10]:
        msg = (f"✈ {d['origin']}→{d['destination']} {d['dest_name']} "
               f"{d['depart_date']}~{d['return_date']}({d['nights']}박, 연차 {d['leave']}일) {d['price_krw']:,}원 "
               f"({d['tier']} 평소 중앙값 {d['median_krw']:,}원 대비 -{d['discount_pct']}%) {d['airlines']} {d['depart_time']}→{d['arrive_time']}")
        print(msg)
        send_telegram(msg)
    if len(new_deals) > 10:
        send_telegram(f"그 외 {len(new_deals) - 10}건은 data/deals.csv에서 확인하세요.")

    # 노선별 요약 (할인율 큰 순)
    for fname, build in (("summary.csv", build_summary), ("offers.csv", build_offers)):
        table = build(by_pair, names, today, stamp)
        if table:
            with (data_dir / fname).open("w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow([h for _, h in SUMMARY_COLUMNS])
                for r in table:
                    w.writerow([r.get(k, "") for k, _ in SUMMARY_COLUMNS])

    print(f"완료: 저장 {len(rows)}건 / 결과없음 {counts['empty']}건 / 오류 {counts['error']}건 / "
          f"새 알림 {len(new_deals)}건 / 소요 {(time.time() - t0) / 60:.1f}분")
    return 1 if (aborted and not rows) or (not rows and counts["error"] and not counts["empty"]) else 0


if __name__ == "__main__":
    sys.exit(main())
