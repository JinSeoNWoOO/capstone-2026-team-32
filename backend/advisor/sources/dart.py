"""OpenDART 공시 목록 (`/api/list.json`). 공시가 **언제 배포됐는가**를 아는 두 경로 중 하나다.

나머지 하나는 LS 공시 속보(`lsnews.py`)다. 둘 중 **빠른 쪽**이 그 공시를 처음 알 수 있었던 시각이고
(설계 5.4), 그 규칙은 `store.upsert_disclosure` 가 지킨다. 여기서는 "우리가 목록에서 처음 본 시각"을
그대로 `first_seen_at` 으로 적어 주면 된다 — 10분 간격 폴러가 도는 한 실제 배포 시각과 10분 안쪽으로 붙는다.

## 키가 없으면 죽지 않는다

`DART_API_KEY` 가 없거나 키가 틀리면 **빈 결과 + 사유**를 돌려준다. 공시는 요인 하나
(`stk_disclosure`)의 재료일 뿐이라, 이것 때문에 그날 판단 전체를 잃는 것이 더 나쁘다 (결정 15의
"LLM 이 없어도 판단이 나온다"와 같은 태도).

응답 규약: `status` "000" 정상 / "013" 조회 결과 없음 / "010" 키 오류. `total_page` 만큼 페이지를
넘기고, `page_count` 는 최대 100 이다.
"""
from datetime import timedelta

from .base import (Report, compact_date, env_value, iso_date, load_env, now_kst, sources_cfg,
                   to_date, to_kst, wall)

# newsgap 이 이미 쓰고 있는 공시 유형 분류를 그대로 쓴다 (두 모드가 같은 유형 이름을 말하게).
from ...newsgap.disclosure import kind_of

STATUS_OK = "000"
STATUS_NO_DATA = "013"
FIRST_SEEN_SRC = "dart_poll"


def fetch_list(cfg, bgn_de, end_de, http=None, api_key=None, env=None, report=None):
    """공시 목록을 날짜 구간으로 받아 온다. 반환은 (행 목록, 사유 또는 None).

    `http` 는 (url, params) → dict 인 함수다 (테스트가 네트워크 없이 돌게).
    """
    report = report if report is not None else Report()
    if api_key is None:
        load_env(cfg)
        api_key = env_value(sources_cfg(cfg, "dart_api_key_env"), env)
    if not api_key:
        report.fallback("dart_no_api_key")
        return [], "DART_API_KEY 가 없습니다 (공시 목록을 건너뜁니다)"

    url = sources_cfg(cfg, "dart_list_url")
    page_count = int(sources_cfg(cfg, "dart_page_count"))
    max_pages = int(sources_cfg(cfg, "dart_max_pages"))
    corp_cls = sources_cfg(cfg, "dart_corp_cls")
    get = http or _requests_get(cfg)

    out, page, total_page = [], 1, 1
    while page <= min(total_page, max_pages):
        params = {"crtfc_key": api_key, "bgn_de": compact_date(bgn_de), "end_de": compact_date(end_de),
                  "page_no": page, "page_count": page_count}
        if corp_cls:
            params["corp_cls"] = corp_cls
        data = get(url, params) or {}
        status = str(data.get("status") or "")
        if status == STATUS_NO_DATA:
            return out, None
        if status != STATUS_OK:
            # 키 오류·한도 초과 등. 메시지는 남기되 키 값은 절대 로그에 넣지 않는다.
            report.fallback(f"dart_status:{status}")
            return out, f"OpenDART 응답 status={status} ({data.get('message')})"
        out.extend(data.get("list") or [])
        total_page = int(data.get("total_page") or 1)
        page += 1
    if total_page > max_pages:
        report.fallback("dart_page_limit")
    return out, None


def to_disclosure_rows(items, first_seen_at):
    """OpenDART 목록 항목 → store.upsert_disclosure 가 받는 dict.

    `stock_code` 가 빈 항목(비상장 법인·펀드)은 버린다. 종목에 붙일 수 없는 공시는 점수에 쓸 수 없다.
    `report_nm` 은 오른쪽에 공백이 잔뜩 붙어 오므로 다듬는다 (제목 대조에 쓰기 때문).
    """
    rows = []
    for item in items or []:
        code = str(item.get("stock_code") or "").strip()
        if not code:
            continue
        report_nm = " ".join(str(item.get("report_nm") or "").split())
        rows.append({
            "rcept_no": str(item.get("rcept_no") or "").strip(),
            "stock_code": code,
            "corp_name": (item.get("corp_name") or "").strip() or None,
            "report_nm": report_nm or None,
            "rcept_dt": iso_date(item["rcept_dt"]) if item.get("rcept_dt") else None,
            "first_seen_at": first_seen_at,
            "first_seen_src": FIRST_SEEN_SRC,
            "ls_realkey": None,
            "kind": kind_of(report_nm),
            "ratio": None,
            "ratio_ok": None,
            "body_src": None,
        })
    return [r for r in rows if r["rcept_no"]]


def sync(store, cfg, as_of, bgn_de=None, end_de=None, http=None, report=None, now=None):
    """최근 구간의 공시 목록을 받아 disclosure 에 넣는다. 반환 {"rows", "new", "provider", "note"}.

    `first_seen_at` 은 **조회 시각**이다 (설계 5.4: 폴러의 최초 조회 시각). 기준 시각을 넘지 않게
    as_of 로 자른다 — 재현 모드가 아니어도 `--as-of` 를 과거로 준 수동 실행이 있을 수 있다.
    """
    report = report if report is not None else Report()
    limit_date = to_kst(as_of).date()
    end_de = to_date(end_de) if end_de else limit_date
    if bgn_de is None:
        bgn_de = end_de - timedelta(days=int(sources_cfg(cfg, "dart_lookback_days")))
    end_de = min(to_date(end_de), limit_date)              # 기준일 이후 공시는 받지 않는다

    seen_at = min(to_kst(now or now_kst()), to_kst(as_of))
    items, note = fetch_list(cfg, bgn_de, end_de, http=http, report=report)
    rows = to_disclosure_rows(items, wall(seen_at))
    rows = [r for r in rows if r["rcept_dt"] is None or to_date(r["rcept_dt"]) <= limit_date]

    known = _known_rcept_nos(store, [r["rcept_no"] for r in rows])
    new = 0
    for row in rows:
        store.upsert_disclosure(row)
        if row["rcept_no"] not in known:
            new += 1
    report.used("dart", "opendart")
    return {"rows": len(rows), "new": new, "provider": "opendart", "note": note,
            "range": [iso_date(bgn_de), iso_date(end_de)]}


def _known_rcept_nos(store, rcept_nos):
    """이미 저장돼 있는 접수번호 집합 (새로 들어온 건수를 세기 위해)."""
    known = set()
    for i in range(0, len(rcept_nos), 500):               # SQLite 변수 한도를 넘지 않게 쪼갠다
        chunk = rcept_nos[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for row in store.conn.execute(
                f"SELECT rcept_no FROM disclosure WHERE rcept_no IN ({marks})", chunk):
            known.add(row[0])
    return known


def _requests_get(cfg):
    timeout = int(sources_cfg(cfg, "http_timeout_sec"))

    def get(url, params):
        import requests
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    return get


__all__ = ["fetch_list", "to_disclosure_rows", "sync", "STATUS_OK", "STATUS_NO_DATA",
           "FIRST_SEEN_SRC"]
