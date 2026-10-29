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
def fmt_day(iso: str) -> str:
    """'2026-11-21' -> '11/21(토)'"""
    d = date.fromisoformat(iso)
    return f"{d.month}/{d.day}({'월화수목금토일'[d.weekday()]})"


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
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
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
def _fetch_best(q):
    """조회해서 가장 싼 항목을 고른다. 반환: (상태, 최저가 항목). 상태는 ok / empty / error."""
    results = None
    for attempt in range(2):
        try:
            results = get_flights(q)
            break
        except FlightsNotFound:
            return "empty", None
        except PARSE_ERRORS as e:  # 응답 구조를 읽지 못함: 재시도해도 같음
            print(f"  응답 해석 실패: {type(e).__name__}: {e}", file=sys.stderr)
            debug_response(q)
            return "error", None
        except Exception as e:  # 네트워크 오류 등
            print(f"  재시도 {attempt + 1}/2: {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    if results is None:
        return "error", None
    diag = getattr(results, "diagnostics", None)
    if diag is not None and getattr(diag, "status", "ok") in ("blocked", "http_error"):
        print(f"  진단: {diag}", file=sys.stderr)
        debug_response(q)
        return "error", None
    priced = [r for r in results if r.price and r.price > 0]
    if not priced:
        return "empty", None
    return "ok", min(priced, key=lambda r: r.price)


def search(origin: str, dest: str, depart: date, nights: int, cfg: dict):
    """왕복 조회. 반환: (상태, 최저가 항목, 귀국일)."""
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
    status, best = _fetch_best(q)
    return status, best, ret


def search_oneway(origin: str, dest: str, day: date, cfg: dict):
    """편도 조회. 반환: (상태, 최저가 항목)."""
    q = create_query(
        flights=[FlightQuery(date=day.isoformat(), from_airport=origin, to_airport=dest, max_stops=cfg.get("max_stops"))],
        seat="economy", trip="one-way", passengers=Passengers(adults=1),
        language=cfg["language"], currency=cfg["currency"],
    )
    return _fetch_best(q)


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


def nonwork_days(depart: date, ret: date, holidays: set) -> int:
    """출발일~귀국일(양끝 포함) 중 주말·공휴일인 날 수."""
    return (ret - depart).days + 1 - leave_days(depart, ret, holidays)


def split_quota(total: int, demand: dict, avail: dict) -> dict:
    """total개를 지역별 수요(demand)에 비례해 나누되, 지역별 가용 개수(avail)를 넘기지 않는다.
    남는 몫은 다른 지역에 다시 나눈다."""
    out = {r: 0 for r in demand}
    left = total
    live = {r for r in demand if avail.get(r, 0) > 0 and demand[r] > 0}
    while left > 0 and live:
        wsum = sum(demand[r] for r in live)
        share = {r: left * demand[r] / wsum for r in live}
        give = {r: min(int(share[r]), avail[r] - out[r]) for r in live}
        used = sum(give.values())
        if used == 0:  # 소수점 이하만 남았으면 몫이 큰 지역부터 1개씩
            for r in sorted(live, key=lambda r: -(share[r] - int(share[r]))):
                if left == 0:
                    break
                if out[r] < avail[r]:
                    out[r] += 1
                    left -= 1
            live = {r for r in live if out[r] < avail[r]}
            continue
        for r in live:
            out[r] += give[r]
        left -= used
        live = {r for r in live if out[r] < avail[r]}
    return out


def plan_tasks(cfg: dict, pairs: list, state: dict, today: date, seen: dict = None, now: datetime = None) -> list:
    """이번에 조회할 왕복 일정을 고른다.
    1) 직항이 아직 확인되지 않은 노선(저장된 가격이 한 번도 없는 노선)은 노선당 probe_per_run건만 먼저 시험 조회한다.
    2) 나머지 조회 건수는 지역별 (가중치 x 일정 수)에 비례해 나눈다. 그래서 지역 가중치가 클수록 더 자주 돈다.
    3) 지역 안에서는 "가장 오래 조회하지 않은 일정부터" 고른다. (한 번도 안 본 일정이 가장 먼저)"""
    seen = seen or {}
    active = [p for p in pairs
              if state["pairs"].get(f"{p['origin']}-{p['dest']}", {}).get("skip_until", "") < today.isoformat()]
    if not active:
        return []
    first = today + timedelta(days=cfg["start_offset_days"])
    window = [first + timedelta(days=i) for i in range(cfg["horizon_days"])]
    holidays, need = set(cfg.get("holidays", [])), cfg.get("min_nonwork_days", 2)
    # 주말·공휴일이 need일 이상 낀 (출발일, 박수) 조합만 후보로 삼는다. 연차 일수는 제한하지 않는다.
    combos = [(d, n) for d in window for n in cfg["nights"]
              if nonwork_days(d, d + timedelta(days=n), holidays) >= need]
    if not combos:
        return []
    probe = cfg.get("probe_per_run", 2)
    weights = cfg.get("region_weight", {})
    now = (now or datetime.now(KST)).replace(tzinfo=None)
    cap_h = 24 * 60  # 한 번도 조회하지 않은 일정의 '지난 시간'(시간 단위)
    age_cache = {}

    def age_hours(ts: str) -> float:
        if not ts:
            return cap_h
        if ts not in age_cache:
            try:
                age_cache[ts] = min(cap_h, max(0.0, (now - datetime.strptime(ts[:16], "%Y-%m-%d %H:%M")).total_seconds() / 3600))
            except ValueError:
                age_cache[ts] = cap_h
        return age_cache[ts]

    budget = cfg["queries_per_run"]
    chosen, by_region = [], {}
    for p in active:
        st = state["pairs"].get(f"{p['origin']}-{p['dest']}", {})
        mine = [(-age_hours(seen.get((p["origin"], p["dest"], d.isoformat(), str(n)), "")), random.random(), p, d, n)
                for d, n in combos]
        mine.sort(key=lambda x: (x[0], x[1]))
        if st.get("ok", 0) == 0:
            chosen.extend(mine[:probe])  # 시험 조회
        else:
            by_region.setdefault(p["region"], []).extend(mine)
    left = max(0, budget - len(chosen))
    demand = {r: float(weights.get(r, 1)) * len(items) for r, items in by_region.items()}
    quota = split_quota(left, demand, {r: len(items) for r, items in by_region.items()})
    for r, items in by_region.items():
        items.sort(key=lambda x: (x[0], x[1]))
        chosen.extend(items[: quota.get(r, 0)])
    tasks = [{**p, "depart": d, "nights": n} for _, _, p, d, n in chosen[:budget]]
    random.shuffle(tasks)  # 순서만 섞는다(한 노선에 연달아 몰리지 않게)
    return tasks


def plan_legs(cfg: dict, pairs: list, state: dict, today: date, leg_seen: dict = None) -> list:
    """편도 조합용 조회 계획. 노선마다 연속된 날짜 구간(block)의 가는 편·오는 편을 모두 조회한다.
    같은 시각에 조회한 편도끼리만 짝지어 왕복 일정을 만든다.
    구간은 무작위가 아니라 "조합이 가장 오래 갱신되지 않은 (노선, 구간)"부터 고른다."""
    budget = cfg.get("oneway_queries_per_run", 0)
    k = cfg.get("oneway_block_days", 10)
    if budget <= 0:
        return []
    leg_seen = leg_seen or {}
    active = [p for p in pairs
              if state["pairs"].get(f"{p['origin']}-{p['dest']}", {}).get("skip_until", "") < today.isoformat()]
    first = today + timedelta(days=cfg["start_offset_days"])
    k = min(k, cfg["horizon_days"])
    n_blocks = max(0, budget // (2 * k))
    holidays, need = set(cfg.get("holidays", [])), cfg.get("min_nonwork_days", 2)
    window = [first + timedelta(days=i) for i in range(cfg["horizon_days"])]
    valid_n = {d: [n for n in cfg["nights"] if nonwork_days(d, d + timedelta(days=n), holidays) >= need]
               for d in window}
    cap = 30  # 한 번도 조합이 만들어지지 않은 날짜의 '오래됨' 점수(일)

    def age(o: str, dst: str, d: date) -> int:
        ts = leg_seen.get((o, dst, d.isoformat()), "")
        if not ts:
            return cap
        try:
            return min(cap, (today - date.fromisoformat(ts[:10])).days)
        except ValueError:
            return cap

    per_region = {}
    for p in active:
        st = state["pairs"].get(f"{p['origin']}-{p['dest']}", {})
        # 직항이 확인되지 않은 노선은 편도 조합 조회를 하지 않는다(왕복 시험 조회로 먼저 확인)
        if st.get("ok", 0) == 0:
            continue
        ages = {d: age(p["origin"], p["dest"], d) for d in window}
        top = None
        for s0 in range(0, len(window) - k + 1):
            days = window[s0:s0 + k]
            last = days[-1]
            score = sum(ages[d] for d in days if any(d + timedelta(days=n) <= last for n in valid_n[d]))
            cand = (score, random.random(), s0)
            if top is None or cand > top:
                top = cand
        if top is not None:
            per_region.setdefault(p["region"], []).append((top[0], top[1], p, top[2]))
    weights = cfg.get("region_weight", {})
    demand = {r: float(weights.get(r, 1)) * len(v) for r, v in per_region.items()}
    quota = split_quota(n_blocks, demand, {r: len(v) for r, v in per_region.items()})
    picked = []
    for r, v in per_region.items():
        v.sort(key=lambda x: (-x[0], x[1]))
        picked.extend(v[: quota.get(r, 0)])
    return [{**p, "days": window[s0:s0 + k]} for _, _, p, s0 in picked]


def google_link(origin: str, dest: str, depart: str, ret: str = "") -> str:
    """같은 일정을 Google 항공권에서 바로 검색해 보는 링크(자연어 검색 주소)."""
    text = f"Flights from {origin} to {dest} on {depart}" + (f" through {ret}" if ret else " one way")
    return "https://www.google.com/travel/flights?hl=ko&curr=KRW&q=" + urllib.parse.quote(text)


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


def load_last_seen(data_dir: Path, today: date, lookback_days: int, state: dict):
    """일정별로 가장 최근에 조회한 시각을 모은다.
    seen: (출발, 도착, 출발일, 박수) -> 시각 (저장된 가격 + '결과 없음' 기록)
    leg_seen: (출발, 도착, 출발일) -> 시각 (편도 2장 조합이 만들어진 기록만)"""
    seen, leg_seen = {}, {}
    for r in load_recent_prices(data_dir, today, lookback_days):
        ts = r.get("collected_at", "")
        k = (r.get("origin"), r.get("destination"), r.get("depart_date"), str(r.get("nights")))
        if ts > seen.get(k, ""):
            seen[k] = ts
        if "+" in (r.get("airlines") or ""):
            k3 = (r.get("origin"), r.get("destination"), r.get("depart_date"))
            if ts > leg_seen.get(k3, ""):
                leg_seen[k3] = ts
    for ks, ts in state.get("empty_seen", {}).items():
        try:
            o, d, dep, n = ks.split("|")
        except ValueError:
            continue
        if ts > seen.get((o, d, dep, n), ""):
            seen[(o, d, dep, n)] = ts
    return seen, leg_seen


def carrier_group(airlines: str, groups: dict) -> str:
    """항공사 이름으로 비교 그룹을 정한다(저비용/대형). 목록에 없으면 그 항공사끼리만 비교한다.
    편도 2장 조합("A+B")은 두 항공사가 같은 유형이면 그 유형, 다르면 "혼합"으로 본다."""
    def one(name: str) -> str:
        first = (name or "").split("/")[0].strip()
        for label, keywords in groups.items():
            if any(k.lower() in first.lower() for k in keywords):
                return label
        return first or "미상"
    kinds = {one(part) for part in (airlines or "").split("+")}
    return kinds.pop() if len(kinds) == 1 else "혼합"


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


def plan_full(cfg: dict, pairs: list, state: dict, today: date) -> list:
    """전수 조회 계획: 조건에 맞는 모든 (노선, 출발일, 박수)를 순서대로 나열한다.
    아직 직항이 확인되지 않은 노선(저장된 가격이 한 번도 없는 노선)은 probe_total건만 시험 조회한다.
    같은 입력이면 항상 같은 목록이 나오므로, 여러 작업(shard)이 각자 자기 몫(tasks[shard::shards])만 가져가 나눠 조회할 수 있다."""
    active = [p for p in pairs
              if state["pairs"].get(f"{p['origin']}-{p['dest']}", {}).get("skip_until", "") < today.isoformat()]
    first = today + timedelta(days=cfg["start_offset_days"])
    window = [first + timedelta(days=i) for i in range(cfg["horizon_days"])]
    holidays, need = set(cfg.get("holidays", [])), cfg.get("min_nonwork_days", 2)
    combos = [(d, n) for d in window for n in cfg["nights"]
              if nonwork_days(d, d + timedelta(days=n), holidays) >= need]
    probe_total = cfg.get("probe_total", 6)
    tasks = []
    for p in active:
        st = state["pairs"].get(f"{p['origin']}-{p['dest']}", {})
        mine = [{**p, "depart": d, "nights": n} for d, n in combos]
        if st.get("ok", 0) == 0 and mine:
            step = max(1, len(mine) // probe_total)
            mine = mine[::step][:probe_total]
        tasks.extend(mine)
    tasks.sort(key=lambda t: (t["origin"], t["dest"], t["depart"], t["nights"]))
    return tasks


def collect(cfg: dict, state: dict, pairs: list, today: date, stamp: str, t0: float, tasks: list, blocks: list) -> dict:
    """주어진 왕복 조회 목록(tasks)과 편도 구간(blocks)을 실제로 조회해 결과를 돌려준다."""
    holidays, need = set(cfg.get("holidays", [])), cfg.get("min_nonwork_days", 2)
    delta: dict = {}  # 노선별로 이번에 성공(ok)/결과없음(empty)이 몇 번이었는지
    done = 0
    rows, counts = [], {"ok": 0, "empty": 0, "error": 0}
    consecutive_errors, aborted = 0, False
    for i, t in enumerate(tasks, 1):
        if (time.time() - t0) / 60 > cfg.get("max_minutes", 100):
            print("시간 예산을 모두 사용해 여기서 마무리합니다.")
            break
        done = i
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
            delta.setdefault(key, {"ok": 0, "empty": 0})["ok"] += 1
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
            delta.setdefault(key, {"ok": 0, "empty": 0})["empty"] += 1
            state["empty_seen"][f"{t['origin']}|{t['dest']}|{t['depart'].isoformat()}|{t['nights']}"] = stamp
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

    # 편도 조합: 같은 시각에 조회한 가는 편·오는 편을 짝지어 "왕복 일정"으로 만든다.
    # (항공사를 섞은 조합은 왕복 검색에 나오지 않을 수 있어, 따로 모아 비교한다.)
    combo_n = 0
    print(f"편도 조합용 구간 {len(blocks)}개 조회 시작")
    leg_errors, leg_aborted = 0, False
    for blk in blocks:
        if leg_aborted or (time.time() - t0) / 60 > cfg.get("max_minutes", 100):
            break
        key = f"{blk['origin']}-{blk['dest']}"
        st = state["pairs"].setdefault(key, {"empty": 0, "ok": 0, "skip_until": ""})
        out, back = {}, {}
        # 주말·공휴일이 충분히 낀 (가는 날, 오는 날) 쌍에 필요한 날짜만 조회한다.
        days = set(blk["days"])
        pairs_ok = [(d, d + timedelta(days=n)) for d in blk["days"] for n in cfg["nights"]
                    if d + timedelta(days=n) in days
                    and nonwork_days(d, d + timedelta(days=n), holidays) >= need]
        need_out = sorted({a for a, _ in pairs_ok})
        need_back = sorted({b for _, b in pairs_ok})
        todo = [(blk["origin"], blk["dest"], d, out) for d in need_out] + \
               [(blk["dest"], blk["origin"], d, back) for d in need_back]
        for a, b, d, store in todo:
            if st.get("skip_until", "") >= today.isoformat():
                break
            status, best = search_oneway(a, b, d, cfg)
            counts[status] += 1
            if status == "ok":
                leg_errors = 0
                st["ok"] += 1
                st["empty"] = 0
                delta.setdefault(key, {"ok": 0, "empty": 0})["ok"] += 1
                store[d] = best
            elif status == "empty":
                leg_errors = 0
                st["empty"] += 1
                delta.setdefault(key, {"ok": 0, "empty": 0})["empty"] += 1
                limit = cfg.get("empty_limit_new", 5) if st["ok"] == 0 else cfg.get("empty_limit_known", 15)
                if st["empty"] >= limit:
                    st["skip_until"] = (today + timedelta(days=cfg.get("skip_days", 14))).isoformat()
                    st["empty"] = 0
                    print(f"  {key}: 편도 결과가 계속 없어 {cfg.get('skip_days', 14)}일간 조회에서 제외")
            else:
                leg_errors += 1
                if leg_errors >= cfg.get("abort_after_errors", 12):
                    print("편도 조회 연속 오류가 많아 편도 조합 조회를 중단합니다.", file=sys.stderr)
                    leg_aborted = True
                    break
            time.sleep(cfg.get("pause_seconds", 2))
        for d, o_leg in out.items():
            for n in cfg["nights"]:
                r_day = d + timedelta(days=n)
                b_leg = back.get(r_day)
                if b_leg is None or (d, r_day) not in pairs_ok:
                    continue
                rows.append({
                    "collected_at": stamp, "region": blk["region"], "origin": blk["origin"],
                    "destination": blk["dest"], "depart_date": d.isoformat(),
                    "return_date": r_day.isoformat(), "nights": n, "price_krw": o_leg.price + b_leg.price,
                    "airlines": "/".join(o_leg.airlines) + "+" + "/".join(b_leg.airlines),
                    "stops": max(len(o_leg.flights) - 1, 0) + max(len(b_leg.flights) - 1, 0),
                    "depart_time": fmt_time(o_leg.flights[0].departure),
                    "arrive_time": fmt_time(o_leg.flights[-1].arrival),
                    "duration_min": sum(x.duration for x in o_leg.flights) + sum(x.duration for x in b_leg.flights),
                })
                combo_n += 1
    print(f"편도 조합 {combo_n}건 생성")

    return {"rows": rows, "counts": counts, "aborted": aborted or leg_aborted, "combo_n": combo_n,
            "delta": delta, "empty_seen": state.get("empty_seen", {}), "planned": len(tasks), "done": done}


def finalize(cfg: dict, data_dir: Path, state: dict, state_path: Path, pairs: list, today: date, stamp: str,
             rows: list, counts: dict, aborted: bool, combo_n: int, t0: float) -> int:
    """조회 결과를 저장하고, 기준선을 계산해 알림과 요약 파일을 만든다."""
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
    holidays, need = set(cfg.get("holidays", [])), cfg.get("min_nonwork_days", 2)
    by_pair: dict = {}  # (출발, 도착, 항공사 유형) 별로 따로 모아 같은 노선·같은 유형끼리만 비교
    # 기준선은 "지금 조회 범위(내일부터 horizon_days일)에 출발하는 일정"만으로 계산한다.
    # 이미 출발했거나 범위를 벗어난 일정의 가격은 지금과 시즌이 달라 제외한다.
    win_lo = today + timedelta(days=cfg["start_offset_days"])
    win_hi = win_lo + timedelta(days=cfg["horizon_days"] - 1)
    recent = [r for r in recent if win_lo.isoformat() <= r.get("depart_date", "") <= win_hi.isoformat()]
    configured = {(p["origin"], p["dest"]) for p in pairs}  # 지금 조회 대상에 없는 노선(예: 뺀 지역)은 제외
    recent = [r for r in recent if (r.get("origin"), r.get("destination")) in configured]
    for r in recent:
        try:
            r["leave"] = leave_days(date.fromisoformat(r["depart_date"]), date.fromisoformat(r["return_date"]), holidays)
        except (ValueError, KeyError):
            continue
        if (date.fromisoformat(r["return_date"]) - date.fromisoformat(r["depart_date"])).days + 1 - r["leave"] < need:
            continue  # 주말·공휴일이 충분히 끼지 않은 일정은 기준선에서도 뺀다
        r["tier"] = carrier_group(r.get("airlines", ""), groups)
        by_pair.setdefault((r["origin"], r["destination"], r["tier"], 0), []).append(r)

    names = {(p["origin"], p["dest"]): (p["name"], p["region"]) for p in pairs}
    deals_path = data_dir / "deals.csv"
    old_deals = {}
    for r in read_csv(deals_path):
        old_deals[(r["origin"], r["destination"], r["depart_date"], r["return_date"])] = (r["collected_at"][:10], int(r["price_krw"]))

    new_deals = []
    for r in rows:
        r["leave"] = leave_days(date.fromisoformat(r["depart_date"]), date.fromisoformat(r["return_date"]), holidays)
        if r["leave"] > acfg.get("max_leave_days", 3):
            continue  # 연차를 너무 많이 써야 하는 일정은 알리지 않는다
        tier = carrier_group(r["airlines"], groups)
        samples = by_pair.get((r["origin"], r["destination"], tier, 0), [])
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
        combo = "+" in d["airlines"]
        link = (f"가는 편 {google_link(d['origin'], d['destination'], d['depart_date'])}\n"
                f"오는 편 {google_link(d['destination'], d['origin'], d['return_date'])}") if combo \
            else f"확인 {google_link(d['origin'], d['destination'], d['depart_date'], d['return_date'])}"
        msg = (f"✈ {d['origin']}→{d['destination']} {d['dest_name']} "
               f"{fmt_day(d['depart_date'])}~{fmt_day(d['return_date'])} ({d['nights']}박, 연차 {d['leave']}일) {d['price_krw']:,}원 "
               f"({d['tier']} 평소 중앙값 {d['median_krw']:,}원 대비 -{d['discount_pct']}%) {d['airlines']} {d['depart_time']}→{d['arrive_time']}"
               + ("\n※ 편도 2장 조합 (각각 따로 예매)" if combo else "") + f"\n{link}")
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

    print(f"완료: 저장 {len(rows)}건(편도 조합 {combo_n}건 포함) / 결과없음 {counts['empty']}건 / 오류 {counts['error']}건 / "
          f"새 알림 {len(new_deals)}건 / 소요 {(time.time() - t0) / 60:.1f}분")
    return 1 if (aborted and not rows) or (not rows and counts["error"] and not counts["empty"]) else 0


def _arg(argv: list, name: str, default=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def _load_state(data_dir: Path):
    state_path = data_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    state.setdefault("pairs", {})
    return state, state_path


def collect_main(argv: list) -> int:
    """조회 담당. --out 파일을 주면 결과를 파일로만 저장하고(분산 실행용), 안 주면 이어서 저장·알림까지 한다."""
    summary_only = "--summary-only" in argv  # 조회 없이 요약 파일만 다시 만들 때
    shard, nshards = int(_arg(argv, "--shard", "0")), int(_arg(argv, "--shards", "1"))
    out = _arg(argv, "--out")
    t0 = time.time()
    cfg = json.loads((ROOT / "routes.json").read_text(encoding="utf-8"))
    data_dir = ROOT / "data"
    now_kst = datetime.now(KST)
    today = date.fromisoformat(_arg(argv, "--date")) if _arg(argv, "--date") else now_kst.date()
    stamp = now_kst.strftime("%Y-%m-%d %H:%M")
    state, state_path = _load_state(data_dir)

    pairs = build_pairs(cfg)
    # '결과 없음' 기록은 이미 지난 출발일이나 오래된 것은 지운다
    cutoff = (today - timedelta(days=cfg.get("seen_lookback_days", 45))).isoformat()
    state["empty_seen"] = {k: v for k, v in state.get("empty_seen", {}).items()
                           if k.split("|")[2:3] and k.split("|")[2] >= today.isoformat() and v[:10] >= cutoff}
    tasks, blocks = [], []
    if not summary_only:
        seen, leg_seen = load_last_seen(data_dir, today, cfg.get("seen_lookback_days", 45), state)
        if cfg.get("full_sweep"):
            # 전수 조회: 모든 작업이 같은 목록을 만들도록 시드를 고정하고, 자기 몫만 가져간다.
            tasks = plan_full(cfg, pairs, state, today)[shard::nshards]
            random.seed(f"{today.isoformat()}-legs")
            blocks = plan_legs(cfg, pairs, state, today, leg_seen)[shard::nshards]
            random.seed()
            random.shuffle(tasks)
        else:
            tasks = plan_tasks(cfg, pairs, state, today, seen, now_kst)
            blocks = plan_legs(cfg, pairs, state, today, leg_seen)
    print(f"[작업 {shard + 1}/{nshards}] 전체 조합 {len(pairs)}개 중 이번 조회 {len(tasks)}건 (제외된 조합 제외)")

    res = collect(cfg, state, pairs, today, stamp, t0, tasks, blocks)
    if out:
        Path(out).write_text(json.dumps({
            "shard": shard, "shards": nshards, "stamp": stamp, **res,
        }, ensure_ascii=False), encoding="utf-8")
        print(f"[작업 {shard + 1}/{nshards}] 저장 {len(res['rows'])}건(편도 조합 {res['combo_n']}건 포함) / "
              f"결과없음 {res['counts']['empty']}건 / 오류 {res['counts']['error']}건 / 소요 {(time.time() - t0) / 60:.1f}분")
        return 1 if (res["aborted"] and not res["rows"]) else 0
    return finalize(cfg, data_dir, state, state_path, pairs, today, stamp,
                    res["rows"], res["counts"], res["aborted"], res["combo_n"], t0)


def merge_main(argv: list) -> int:
    """분산 조회 결과(shard 파일들)를 합쳐 저장하고 알림·요약을 만든다."""
    import glob
    t0 = time.time()
    cfg = json.loads((ROOT / "routes.json").read_text(encoding="utf-8"))
    data_dir = ROOT / "data"
    now_kst = datetime.now(KST)
    today = date.fromisoformat(_arg(argv, "--date")) if _arg(argv, "--date") else now_kst.date()
    stamp = now_kst.strftime("%Y-%m-%d %H:%M")
    expect = int(_arg(argv, "--expect", "0") or 0)
    state, state_path = _load_state(data_dir)
    pairs = build_pairs(cfg)

    skip_opts = {"--date", "--expect"}
    names_, i = [], argv.index("--merge") + 1
    while i < len(argv):
        if argv[i] in skip_opts:
            i += 2
            continue
        names_.append(argv[i])
        i += 1
    files = sorted({f for pat in names_ for f in glob.glob(pat)})
    results = []
    for f in files:
        try:
            results.append(json.loads(Path(f).read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            print(f"결과 파일을 읽지 못했습니다: {f} ({e})", file=sys.stderr)
    if not results:
        print("합칠 조회 결과가 하나도 없습니다.", file=sys.stderr)
        send_telegram("⚠ 항공권 조회 결과가 하나도 저장되지 않았습니다. GitHub Actions 로그를 확인해 주세요.")
        return 1

    rows, counts = [], {"ok": 0, "empty": 0, "error": 0}
    agg, combo_n, aborted_n, short_n = {}, 0, 0, 0
    for r in results:
        rows.extend(r["rows"])
        for k in counts:
            counts[k] += r["counts"].get(k, 0)
        combo_n += r.get("combo_n", 0)
        for key, dl in r.get("delta", {}).items():
            a = agg.setdefault(key, {"ok": 0, "empty": 0})
            a["ok"] += dl["ok"]
            a["empty"] += dl["empty"]
        state.setdefault("empty_seen", {}).update(r.get("empty_seen", {}))
        aborted_n += 1 if r.get("aborted") else 0
        short_n += 1 if r.get("done", 0) < r.get("planned", 0) else 0
    # 노선 상태 반영: 한 번이라도 성공하면 정상, 성공 없이 결과없음만 쌓이면 일정 기간 제외
    for key, a in agg.items():
        st = state["pairs"].setdefault(key, {"empty": 0, "ok": 0, "skip_until": ""})
        if a["ok"] > 0:
            st["ok"] += a["ok"]
            st["empty"] = 0
        else:
            st["empty"] += a["empty"]
            limit = cfg.get("empty_limit_new", 5) if st["ok"] == 0 else cfg.get("empty_limit_known", 15)
            if st["empty"] >= limit:
                st["skip_until"] = (today + timedelta(days=cfg.get("skip_days", 14))).isoformat()
                st["empty"] = 0
                print(f"  {key}: 결과가 계속 없어 {cfg.get('skip_days', 14)}일간 조회에서 제외")
    total = max([expect] + [r.get("shards", 1) for r in results])
    if len(results) < total or aborted_n or short_n:
        msg = (f"⚠ 항공권 조회가 일부만 끝났습니다. 결과 {len(results)}/{total}개 작업 반영, "
               f"중단 {aborted_n}개, 시간 부족 {short_n}개. 차단 가능성이 있으니 Actions 로그를 확인해 주세요.")
        print(msg, file=sys.stderr)
        send_telegram(msg)
    print(f"합치기: 작업 {len(results)}/{total}개, 왕복·조합 {len(rows)}건")
    return finalize(cfg, data_dir, state, state_path, pairs, today, stamp, rows, counts, aborted_n > 0, combo_n, t0)


def main() -> int:
    argv = sys.argv[1:]
    if "--merge" in argv:
        return merge_main(argv)
    return collect_main(argv)


if __name__ == "__main__":
    sys.exit(main())
