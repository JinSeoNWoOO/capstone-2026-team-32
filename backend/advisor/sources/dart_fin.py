"""OpenDART 정기보고서 재무 (다중회사 주요계정 `fnlttMultiAcnt.json`). 관찰 요인 `stk_earn_growth` 의 재료다.

2026-09-28 추가 (결정 12 의 "재무·실적 요인은 시간이 남으면 추가"). 요인의 가중치는 0 이라 판단에는
쓰이지 않고, 점수만 저장돼 사후 재평가의 재료가 된다 (결정 6).

## 무엇을 받는가

- **고유번호 매핑** `corpCode.xml`: 재무 API 는 종목 코드가 아니라 DART 고유번호(corp_code, 8자리)를
  받는다. ZIP 안의 CORPCODE.xml 을 풀어 {종목 코드: 고유번호} 를 `dart_corp` 에 둔다. 대상 종목 중
  표에 없는 코드가 생길 때만(신규 편입) 하루 한 번까지 다시 받는다.
- **주요계정** `fnlttMultiAcnt.json`: 한 번에 고유번호 최대 100개 × (사업연도, 보고서 코드) 하나.
  코스피200 이면 보고서 하나당 3번 부르면 된다. 매출액·영업이익·당기순이익만 옮긴다.
  지배주주순이익은 이 API 에 없어(2026-09-28 실측: 당기순이익(손실) 행이 두 번 오지만 값이 같다)
  받지 않는다. 필요하면 `fnlttSinglAcntAll`(회사·보고서마다 1회)로 넓혀야 한다.
- **공시일** `list.json` (정기공시 pblntf_ty=A): 접수번호 → 공시일(rcept_dt). 아래 시점 규칙에 쓴다.

보고서 코드: 11013 1분기, 11012 반기, 11014 3분기, 11011 사업보고서.

## 값의 뜻 (2026-09-28 삼성전자 2025년 응답으로 확인)

| 보고서 | thstrm_amount | thstrm_add_amount |
|---|---|---|
| 1분기 | 1~3월 (3개월) | 1~3월 누적 (같은 값) |
| 반기 | **4~6월 (3개월)** | 1~6월 누적 |
| 3분기 | **7~9월 (3개월)** | 1~9월 누적 |
| 사업보고서 | 1~12월 (연간) | 없음 |

그래서 **분기 단독값**은 1~3분기는 thstrm_amount 를 그대로 쓰고, **4분기 = 연간 − 3분기 누적**이다
(계산은 `factors/fin.py`). 여기서는 받은 그대로(amount, cum_amount) 저장만 한다 — 수집이 가공하면
가공 규칙을 바꿀 때 다시 받아야 한다.
연결(CFS)과 별도(OFS)는 둘 다 저장한다. 어느 쪽을 쓸지는 요인 쪽 규칙(CFS 우선, 없으면 OFS)이 정한다.

## 인지 시각 (시점 규칙, 설계 2.3)

값을 알 수 있게 되는 날은 **그 보고서의 공시일(rcept_dt)** 이다. 접수번호 앞 8자리(접수일)를 그대로 쓰지
않는 이유: 2026-09-28 `disclosure` 표 516건을 대조하니 12건은 공시일이 접수번호 날짜보다 1일(금→월은 3일)
늦었다. 장 마감 뒤 접수분이 **다음 영업일 공시**로 잡히기 때문이다. 접수번호 날짜를 쓰면 주말·휴일을 낀
만큼 미래를 본다.

그래서 공시일을 이 순서로 찾는다.
  1. `disclosure` 표 (공시 폴러 `dart.py` 가 이미 받아 둔 목록 — 실시간 운영에서는 대부분 여기서 찾는다)
  2. `fin_filing` 표 (이 모듈이 정기공시 목록을 받아 둔 것)
  3. 없으면 정기공시 목록(`list.json`, pblntf_ty=A, 3개월 창)을 받아 `fin_filing` 에 채우고 다시 찾는다
  4. 그래도 없으면 **접수번호 날짜 + `dart_fin_fallback_days`(7일)** — 보수적으로 늦춘 대체값 (rcept_dt_src='fallback')

공시 시각(시·분)은 주지 않는다. 그래서 **공시일 당일 18:30 예비 판단에서는 쓰지 않고, 다음 날 00:00
부터 쓴다** (`dart_fin_known_days_after` = 1, `dart_fin_known_at` = "00:00"). 하루 늦게 쓰는 손해는 채점 주
기간 60일에 비해 작고, 반대 방향의 누출은 막을 방법이 없다.

`knowledge_time` 은 수집 시각이 아니라 이 규칙으로 정한 값이다. 그래서 지금 과거 보고서를 받아도
재현 모드가 "그때 알 수 있었던 값"만 읽는다 (`factors/asof.fin_reports`).

**정정 보고서**: 같은 (보고서, 계정)이라도 접수번호가 다르면 행을 따로 남긴다(덧붙이기). 읽는 쪽은
as_of 이전에 알 수 있던 것 중 가장 늦은 접수번호를 쓴다. 다만 이 API 는 **지금 시점의 최종본 하나**만
주므로, 백필한 과거 구간에서는 원본이 아니라 정정본이 정정 공시일부터 보인다 (2025년 3분기 199건 중
4건이 2026년 정정본이었다). 원본 값은 복원할 수 없고, 그 사이에는 이전 분기가 쓰이거나 결측이 된다 —
미래 누출은 아니다.

## 호출 수 (DART 하루 20,000건)

매 배치에서 전부 다시 받지 않는다. (사업연도, 보고서) 마다 **아직 행이 없는 종목만** 묻고,
`fin_fetch_log` 에 조회 기록을 남겨 (1) 같은 날에는 다시 묻지 않고 (2) 기간 끝에서
`dart_fin_giveup_days` 가 지난 뒤에도 없던 보고서는 더 묻지 않는다. 기간이 아직 안 끝난 보고서는 묻지 않는다.
첫 백필(2024년~)은 주요계정 약 30회 + 정기공시 목록 약 100회이고, 이후에는 분기 보고서 제출 기간에만
하루 몇 번이다. 한 번의 수집에서 `dart_fin_max_calls` 를 넘기지 않는다 (넘으면 다음 배치가 잇는다).

## 실패

키가 없거나 응답이 오류면 **빈 결과 + 사유**를 돌려주고 죽지 않는다 (`dart.py` 와 같은 태도).
ingest 의 `fin` 단계가 예외를 잡아 요약에 남기므로, 여기서 터져도 그날 판단은 나온다.
"""
import io
import zipfile
from datetime import date, datetime, timedelta
from xml.etree import ElementTree

from .base import (Report, compact_date, env_value, iso_date, load_env, now_kst, parse_hhmm,
                   sources_cfg, to_date, to_kst, wall)

STATUS_OK = "000"
STATUS_NO_DATA = "013"

# 보고서 코드 → 분기 번호. 4 는 사업보고서(연간)다.
REPRT_QUARTER = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}
QUARTER_REPRT = {q: code for code, q in REPRT_QUARTER.items()}
# 분기 번호 → 기간 끝 (월, 일)
PERIOD_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
FS_DIVS = ("CFS", "OFS")
INCOME_SJ = ("IS", "CIS")
# list.json 은 고유번호 없이 부르면 검색 기간이 3개월로 제한된다 (2026-09-28 실측: status 100).
LIST_WINDOW_DAYS = 89
# 장 마감 뒤 접수분은 다음 영업일 공시로 잡힌다. 목록 창 끝을 이만큼 늘려 그 행도 받는다.
LIST_TAIL_DAYS = 7
SRC_DISCLOSURE, SRC_LIST, SRC_FALLBACK = "disclosure", "list", "fallback"


def period_end(year, quarter):
    """(사업연도, 분기) 의 기간 끝 날짜. 사업연도 = 회계연도이고 12월 결산을 전제한다.

    12월 결산이 아닌 회사도 bsns_year·reprt_code 로는 같은 자리에 들어오므로 전년 동기 비교
    자체는 성립한다(보고서 대 보고서). 기간 끝 날짜로 하는 판정(오래된 값 제외)만 어긋난다.
    """
    m, d = PERIOD_END[int(quarter)]
    return date(int(year), m, d)


# ---------------------------------------------------------------- 시점 규칙

def rcept_date(rcept_no):
    """접수번호 앞 8자리 → 접수일. 형식이 어긋나면 None."""
    text = str(rcept_no or "").strip()
    if len(text) < 8 or not text[:8].isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def knowledge_time_of(cfg, rcept_no, rcept_dt=None):
    """보고서 값을 판단에 쓸 수 있게 되는 시각 (KST naive ISO) 과 그 근거. 규칙은 모듈 머리말 참고.

    반환 (knowledge_time, 공시일 'YYYY-MM-DD', 근거). 공시일을 모르면 접수번호 날짜에
    `dart_fin_fallback_days` 를 더한 날을 공시일로 본다.
    """
    after = int(sources_cfg(cfg, "dart_fin_known_days_after"))
    at = parse_hhmm(sources_cfg(cfg, "dart_fin_known_at"))
    if rcept_dt:
        day, src = to_date(rcept_dt), None
    else:
        base = rcept_date(rcept_no)
        if base is None:
            return None, None, None
        day, src = base + timedelta(days=int(sources_cfg(cfg, "dart_fin_fallback_days"))), SRC_FALLBACK
    return wall(datetime.combine(day + timedelta(days=after), at)), iso_date(day), src


# ---------------------------------------------------------------- 파서 (네트워크 없음)

def parse_amount(text):
    """'1,234,000' | '-5,000' | '(5,000)' | '' | None → float | None."""
    if text is None:
        return None
    s = str(text).strip().replace(",", "")
    if s in ("", "-"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def parse_corp_codes(raw):
    """corpCode.xml (ZIP 바이트 또는 XML 바이트) → {종목 코드: (고유번호, 회사명, 변경일)}.

    종목 코드가 빈(비상장) 회사는 뺀다. 같은 종목 코드가 두 번 나오면 변경일이 늦은 쪽을 쓴다.
    """
    data = raw
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            data = z.read(z.namelist()[0])
    root = ElementTree.fromstring(data)
    out = {}
    for el in root.iter("list"):
        code = (el.findtext("stock_code") or "").strip()
        corp = (el.findtext("corp_code") or "").strip()
        if not code or not corp:
            continue
        rec = (corp, (el.findtext("corp_name") or "").strip() or None,
               (el.findtext("modify_date") or "").strip() or None)
        prev = out.get(code)
        if prev is None or (rec[2] or "") > (prev[2] or ""):
            out[code] = rec
    return out


def _norm(name):
    return "".join(str(name or "").split())


def account_key(cfg, account_nm):
    """응답의 account_nm → 우리 계정 키 (revenue | op_income | net_income). 모르는 계정은 None."""
    target = _norm(account_nm)
    for key, names in (sources_cfg(cfg, "dart_fin_accounts") or {}).items():
        if any(_norm(n) == target for n in names or ()):
            return key
    return None


def to_fin_rows(cfg, items, fetched_at=None):
    """fnlttMultiAcnt 응답 목록 → fin_quarterly 행 (공시일·인지 시각은 아직 비어 있다).

    손익계산서(IS·CIS) 행만 쓰고, 같은 (종목, 연도, 보고서, 연결/별도, 계정, 접수번호)가 두 번 오면
    (당기순이익(손실) 이 실제로 두 번 온다) 처음 것을 쓴다. 금액을 못 읽거나 접수번호가 이상한 행은 버린다.
    """
    rows, seen = [], set()
    for it in items or []:
        if str(it.get("sj_div") or "") not in INCOME_SJ:
            continue
        acc = account_key(cfg, it.get("account_nm"))
        reprt = str(it.get("reprt_code") or "").strip()
        fs_div = str(it.get("fs_div") or "").strip().upper()
        code = str(it.get("stock_code") or "").strip()
        rcept_no = str(it.get("rcept_no") or "").strip()
        if acc is None or reprt not in REPRT_QUARTER or fs_div not in FS_DIVS or not code:
            continue
        amount = parse_amount(it.get("thstrm_amount"))
        if rcept_date(rcept_no) is None or amount is None:
            continue
        key = (code, int(it.get("bsns_year")), reprt, fs_div, acc, rcept_no)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "stock_code": code,
            "corp_code": str(it.get("corp_code") or "").strip() or None,
            "bsns_year": int(it.get("bsns_year")),
            "reprt_code": reprt,
            "fs_div": fs_div,
            "account": acc,
            "amount": amount,
            "cum_amount": parse_amount(it.get("thstrm_add_amount")),
            "rcept_no": rcept_no,
            "rcept_dt": None,
            "rcept_dt_src": None,
            "knowledge_time": None,
            "fetched_at": fetched_at,
        })
    return rows


def stamp_knowledge(cfg, rows, filing_dates):
    """행마다 공시일·인지 시각을 채운다. filing_dates 는 {접수번호: ('YYYY-MM-DD', 근거)}."""
    for r in rows:
        found = filing_dates.get(r["rcept_no"])
        kt, day, src = knowledge_time_of(cfg, r["rcept_no"], found[0] if found else None)
        r["knowledge_time"], r["rcept_dt"] = kt, day
        r["rcept_dt_src"] = found[1] if found else src
    return rows


def list_windows(dates, limit):
    """접수번호 날짜들 → 정기공시 목록을 받을 (시작, 끝) 창 목록.

    창 끝은 마지막 접수일보다 `LIST_TAIL_DAYS` 늦게 잡아 장 마감 뒤 접수분(다음 영업일 공시)도 받는다.
    꼬리까지 합친 창 길이가 3개월 제한(`LIST_WINDOW_DAYS`) 안에 들고, limit(기준일) 을 넘지 않는다.
    """
    out, covered = [], None
    span = LIST_WINDOW_DAYS - LIST_TAIL_DAYS
    for d in sorted(set(dates)):
        if covered is not None and d <= covered:
            continue
        covered = d + timedelta(days=span)
        end = min(covered + timedelta(days=LIST_TAIL_DAYS), limit)
        out.append((d, max(d, end)))
    return out


# ---------------------------------------------------------------- 네트워크

def _api_key(cfg, api_key=None, env=None):
    if api_key is not None:
        return api_key
    load_env(cfg)
    return env_value(sources_cfg(cfg, "dart_api_key_env"), env)


def _requests_get_json(cfg):
    timeout = int(sources_cfg(cfg, "http_timeout_sec"))

    def get(url, params):
        import requests
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    return get


def _requests_get_bytes(cfg):
    timeout = max(60, int(sources_cfg(cfg, "http_timeout_sec")))

    def get(url, params):
        import requests
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.content

    return get


def fetch_multi(cfg, corp_codes, year, reprt_code, api_key, http=None):
    """주요계정 한 번 호출. 반환 (항목 목록, 사유 또는 None). 013(자료 없음)은 빈 목록 + None."""
    get = http or _requests_get_json(cfg)
    params = {"crtfc_key": api_key, "corp_code": ",".join(corp_codes), "bsns_year": str(int(year)),
              "reprt_code": str(reprt_code)}
    data = get(sources_cfg(cfg, "dart_fin_url"), params) or {}
    status = str(data.get("status") or "")
    if status == STATUS_NO_DATA:
        return [], None
    if status != STATUS_OK:
        # 키 값은 절대 메시지에 넣지 않는다.
        return [], f"OpenDART 재무 status={status} ({data.get('message')})"
    return list(data.get("list") or []), None


def fetch_filings(cfg, bgn, end, api_key, http=None, budget=None):
    """정기공시 목록(pblntf_ty=A) 한 창. 반환 (행 목록, 호출 수, 사유 또는 None)."""
    get = http or _requests_get_json(cfg)
    out, page, total, calls = [], 1, 1, 0
    while page <= total:
        if budget is not None and calls >= budget:
            return out, calls, "호출 상한"
        params = {"crtfc_key": api_key, "bgn_de": compact_date(bgn), "end_de": compact_date(end),
                  "pblntf_ty": "A", "page_no": page, "page_count": int(sources_cfg(cfg, "dart_page_count"))}
        corp_cls = sources_cfg(cfg, "dart_corp_cls")
        if corp_cls:
            params["corp_cls"] = corp_cls
        data = get(sources_cfg(cfg, "dart_list_url"), params) or {}
        calls += 1
        status = str(data.get("status") or "")
        if status == STATUS_NO_DATA:
            return out, calls, None
        if status != STATUS_OK:
            return out, calls, f"OpenDART 목록 status={status} ({data.get('message')})"
        for it in data.get("list") or []:
            no = str(it.get("rcept_no") or "").strip()
            if no and it.get("rcept_dt"):
                out.append({"rcept_no": no, "stock_code": str(it.get("stock_code") or "").strip() or None,
                            "corp_code": str(it.get("corp_code") or "").strip() or None,
                            "report_nm": " ".join(str(it.get("report_nm") or "").split()) or None,
                            "rcept_dt": iso_date(it["rcept_dt"])})
        total = int(data.get("total_page") or 1)
        page += 1
    return out, calls, None


def refresh_corp_codes(store, cfg, api_key, http_bytes=None, now=None):
    """corpCode.xml 을 받아 dart_corp 를 채운다. 반환 저장 건수."""
    get = http_bytes or _requests_get_bytes(cfg)
    raw = get(sources_cfg(cfg, "dart_corp_url"), {"crtfc_key": api_key})
    mapping = parse_corp_codes(raw)
    return store.put_dart_corps(mapping, fetched_at=wall(now or now_kst()))


# ---------------------------------------------------------------- 증분 수집

def due_reports(cfg, as_of_date):
    """조회할 만한 (사업연도, 보고서 코드). 기간이 끝난 것만, 설정의 시작 연도부터 오래된 순."""
    start = int(sources_cfg(cfg, "dart_fin_start_year"))
    out = []
    for year in range(start, as_of_date.year + 1):
        for q in (1, 2, 3, 4):
            if period_end(year, q) < as_of_date:
                out.append((year, QUARTER_REPRT[q]))
    return out


def pending_corps(store, cfg, codes, corp_of, year, reprt, as_of_date):
    """이 보고서를 아직 물어볼 필요가 있는 (종목, 고유번호).

    이미 행이 있거나, 오늘 이미 물었거나, 기간 끝에서 포기 일수가 지난 뒤에 물었는데 없던 것은 뺀다.
    """
    have = store.fin_codes_with(year, reprt)
    log = store.fin_fetch_log(year, reprt)
    giveup = iso_date(period_end(year, REPRT_QUARTER[reprt]) + timedelta(
        days=int(sources_cfg(cfg, "dart_fin_giveup_days"))))
    today = iso_date(as_of_date)
    out = []
    for code in codes:
        corp = corp_of.get(code)
        if corp is None or code in have:
            continue
        last = log.get(corp)
        if last:
            last_day = str(last)[:10]
            if last_day >= today or last_day > giveup:
                continue
        out.append((code, corp))
    return out


def resolve_filing_dates(store, cfg, rcept_nos, as_of_date, api_key, http=None, budget=None):
    """{접수번호: (공시일, 근거)} — disclosure → fin_filing → 정기공시 목록 순으로 찾는다.

    반환 (사전, 호출 수, 사유 또는 None). 목록 조회가 실패해도 죽지 않는다 (못 찾은 것은 대체값으로).
    """
    nos = sorted(set(rcept_nos))
    found = {no: (d, SRC_DISCLOSURE) for no, d in store.disclosure_dates(nos).items()}
    found.update({no: (d, SRC_LIST) for no, d in store.fin_filing_dates(
        [n for n in nos if n not in found]).items()})
    missing = [n for n in nos if n not in found]
    calls, why = 0, None
    if missing:
        days = [rcept_date(n) for n in missing if rcept_date(n)]
        for bgn, end in list_windows(days, as_of_date):
            left = None if budget is None else budget - calls
            if left is not None and left <= 0:
                why = "호출 상한"
                break
            rows, n, why = fetch_filings(cfg, bgn, end, api_key, http=http, budget=left)
            calls += n
            store.put_fin_filings(rows)
            if why:
                break
        found.update({no: (d, SRC_LIST) for no, d in store.fin_filing_dates(missing).items()})
    return found, calls, why


def sync(store, cfg, as_of, codes, http=None, http_bytes=None, api_key=None, env=None,
         report=None, now=None):
    """대상 종목의 정기보고서 주요계정을 증분으로 받는다. 반환 {"rows", "calls", "provider", "note", ...}.

    as_of 이후에야 알 수 있는 행(knowledge_time > as_of)은 저장하지 않고 그 종목은 다음 배치에서
    다시 묻는다 (수집 지점의 시점 방어선, base.py 머리말).
    """
    report = report if report is not None else Report()
    key = _api_key(cfg, api_key, env)
    if not key:
        report.fallback("dart_fin_no_api_key")
        return {"rows": 0, "calls": 0, "provider": None,
                "note": "DART_API_KEY 가 없습니다 (재무 수집을 건너뜁니다)"}

    as_of_t = to_kst(as_of)
    as_of_wall, as_of_date = wall(as_of_t), as_of_t.date()
    seen_at = wall(min(to_kst(now or now_kst()), as_of_t))
    max_calls = int(sources_cfg(cfg, "dart_fin_max_calls"))
    chunk = int(sources_cfg(cfg, "dart_fin_chunk"))
    codes = sorted({str(c) for c in codes or ()})
    calls, notes = 0, []

    if http_bytes is None and http is not None:
        http_bytes = http                 # 가짜 http 를 주입한 호출(테스트)은 고유번호 목록도 그쪽으로 — 네트워크 금지
    corp_of = store.dart_corp_map()
    if any(c not in corp_of for c in codes):
        last = store.dart_corp_fetched_at()
        if last is None or str(last)[:10] < iso_date(as_of_date):
            calls += 1
            try:
                refresh_corp_codes(store, cfg, key, http_bytes=http_bytes, now=now)
            except Exception as exc:      # 매핑을 못 받으면 이미 아는 종목만 묻는다 (배치는 계속)
                report.fallback("dart_fin_corp_code")
                notes.append(f"고유번호 목록을 못 받았다 ({type(exc).__name__})")
            corp_of = store.dart_corp_map()
    unmapped = [c for c in codes if c not in corp_of]

    # 1) 주요계정: 아직 행이 없는 (보고서, 종목) 만 묻는다
    fetched, asked, stop = [], [], None
    for year, reprt in due_reports(cfg, as_of_date):
        todo = pending_corps(store, cfg, codes, corp_of, year, reprt, as_of_date)
        for i in range(0, len(todo), chunk):
            if calls >= max_calls:
                stop = f"호출 상한 {max_calls} 에 닿아 나머지는 다음 배치로 미룬다"
                break
            part = todo[i:i + chunk]
            items, why = fetch_multi(cfg, [corp for _, corp in part], year, reprt, key, http=http)
            calls += 1
            if why:
                stop = why
                break
            fetched += to_fin_rows(cfg, items, fetched_at=seen_at)
            asked += [(code, corp, year, reprt) for code, corp in part]
        if stop:
            break

    # 2) 공시일 → 인지 시각. 목록 조회에 남은 호출 수만 쓴다.
    dates, n, why = resolve_filing_dates(store, cfg, [r["rcept_no"] for r in fetched], as_of_date, key,
                                         http=http, budget=max(0, max_calls - calls))
    calls += n
    if why:
        report.fallback("dart_fin_filing_dates")
        notes.append(f"공시일 조회 중단({why}) — 못 찾은 보고서는 접수일 + 대체 일수로 늦춰 쓴다")
    rows = [r for r in stamp_knowledge(cfg, fetched, dates)
            if r["knowledge_time"] and r["knowledge_time"] <= as_of_wall]
    store.put_fin_rows(rows)
    found = {(r["stock_code"], r["bsns_year"], r["reprt_code"]) for r in rows}
    store.log_fin_fetch([(corp, year, reprt, seen_at, 1 if (code, year, reprt) in found else 0)
                         for code, corp, year, reprt in asked])
    store.commit()

    if stop:
        report.fallback("dart_fin_call_limit" if stop.startswith("호출 상한") else "dart_fin_status")
        notes.insert(0, stop)
    report.used("fin", "opendart_fin")
    return {"rows": len(rows), "calls": calls, "provider": "opendart_fin",
            "note": "; ".join(notes) or None, "unmapped": len(unmapped),
            "fallback_dates": sum(1 for r in rows if r["rcept_dt_src"] == SRC_FALLBACK)}


__all__ = ["REPRT_QUARTER", "QUARTER_REPRT", "PERIOD_END", "STATUS_OK", "STATUS_NO_DATA",
           "SRC_DISCLOSURE", "SRC_LIST", "SRC_FALLBACK", "account_key", "due_reports", "fetch_filings",
           "fetch_multi", "knowledge_time_of", "list_windows", "parse_amount", "parse_corp_codes",
           "pending_corps", "period_end", "rcept_date", "refresh_corp_codes", "resolve_filing_dates",
           "stamp_knowledge", "sync", "to_fin_rows"]
