"""공시 채점 (설계 5.4, 결정 12의 LLM 요인 1). **코드가 먼저, 남은 것만 LLM.**

분업의 기준은 결정 3 그대로다: 유형·비율처럼 숫자로 확정되는 것은 코드가 정하고, "이 실적이
큰 폭 개선인가" 같은 글 판단만 LLM 에 맡긴다. 그래서 같은 공시가 매일 같은 점수를 받고
(재현성), 호출 수도 하루 몇 건으로 줄어든다.

    1. newsgap.disclosure.kind_of 로 유형 → 2. 기준표의 decided_by=code 항목이면 확정
    3. 아니면 LLM(제목 + 있으면 본문 + 코드가 뽑은 수치) → 4. 종목별 합산·감쇠

**시점**: `first_seen_at <= as_of` 인 공시만 본다 (설계 2.3의 2번). 접수일(rcept_dt)만 보면
18:30 예비 판단이 그날 19시에 배포될 공시를 미리 아는 셈이 된다. 반대로 감쇠의 기준은
접수일이다 (결정 12: "접수일부터 선형 감쇠") — 사건이 일어난 날부터 시장이 소화하기 때문이다.

**결측 규칙**: 공시가 하나도 없는 종목은 점수 행을 쓰지 않는다. `factors/compute.py` 가 미리
깔아 둔 결측 행(score 0, missing=1)이 그대로 남아 계층 점수의 분모에서 빠진다 — 설계 5.1의
"이벤트 없음 = 0(중립) + 결측 표시, 가중 합에서 제외"가 이 구조다. 반대로 **공시가 있었는데
합이 0 이면 missing=0 인 진짜 0 점**이다 ("자료가 없다"와 "봤는데 중립이다"는 다르다).
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

from ..config import resolve_path
from ..factors.normalize import clip, linear_decay, rubric_score
from . import prompts, schemas
from .client import STATUS_BUDGET, STATUS_DISABLED, clip_text, to_datetime, tunable

log = logging.getLogger("advisor.llm.disclosure")

# 제목만으로 확정할 수 있는 것들. 기준표(disclosure_rubric)의 decided_by=code 항목과 짝이 맞는다.
RE_CORRECTION = re.compile(r"\(정정\)|정정공시|정정신고|기재정정")
RE_PERIODIC = re.compile(r"사업보고서|반기보고서|분기보고서|감사보고서|결산실적공시예고")
RE_RIGHTS_THIRD = re.compile(r"제3자\s*배정|제삼자\s*배정")
RE_RIGHTS_PUBLIC = re.compile(r"주주배정|일반공모|주주우선공모|무상증자\s*병행")
RE_BUYBACK = re.compile(r"취득|소각|신탁계약\s*체결")
RE_BUYBACK_OUT = re.compile(r"처분|신탁계약\s*해지|결과보고")

# disclosure.ratio 의 단위는 **퍼센트**다 (newsgap.disclosure 의 '매출액대비'와 같다).
# 0~1 의 비율로 채워 넣으면 대규모 계약이 전부 supply_small 로 떨어지므로 수집 쪽과 단위를 맞춘다.
RATIO_IS_PERCENT = True


@dataclass
class Scored:
    """공시 한 건의 채점 결과. factor_evidence 한 줄과 1:1 로 대응한다."""

    rcept_no: str
    code: str
    rubric_id: str
    level: int
    decided_by: str            # code | llm
    evidence: str = ""
    confidence: float = None
    decay: float = 1.0
    event_date: object = None
    elapsed: int = 0
    title: str = ""
    call_id: str = None


@dataclass
class DisclosureOutcome:
    """`stage.llm_factors` 가 그대로 저장하는 묶음."""

    factor_rows: list = field(default_factory=list)
    evidence_rows: list = field(default_factory=list)
    flag_rows: list = field(default_factory=list)
    scored: list = field(default_factory=list)
    n_code: int = 0
    n_llm: int = 0
    n_error: int = 0
    n_skipped: int = 0
    status: str = "OK"


# ---------------------------------------------------------------- 기준표·유형

def rubric_rows(cfg):
    return [r for r in (cfg.get("disclosure_rubric") or []) if isinstance(r, dict) and r.get("id")]


def rubric_levels(cfg):
    """{rubric_id: level}. 설정이 유일한 출처다 — 코드에 수준을 다시 적지 않는다 (결정 6)."""
    return {r["id"]: int(r.get("level", 0)) for r in rubric_rows(cfg)}


def kind_of(title):
    """공시 유형 (newsgap 재사용, 결정 15). newsgap 을 못 읽어도 채점이 멈추지는 않는다."""
    try:
        from ...newsgap.disclosure import kind_of as _kind_of
    except Exception:
        return "기타"
    return _kind_of(title or "")


def code_decide(title, kind, ratio, ratio_ok, cfg):
    """코드로 확정되는 경우만 (rubric_id, level, 근거) 를 준다. 애매하면 None → LLM (설계 5.4의 2번).

    확정하지 못하는 쪽으로 기우는 것이 안전하다. 잘못 확정하면 그 공시는 영영 LLM 을 못 보지만,
    LLM 으로 보낸 건은 틀려도 confidence·evidence 가 남아 사람이 되짚을 수 있다.
    """
    title = title or ""
    levels = rubric_levels(cfg)
    gov = re.compile(tunable(cfg, "llm.governance_pattern"))

    def hit(rid, why):
        return (rid, levels.get(rid, 0), why) if rid in levels else None

    # 정정·정기 보고 → routine. 단, 감사의견·횡령 같은 문구가 붙어 있으면 '단순'이 아니다.
    if (RE_CORRECTION.search(title) or RE_PERIODIC.search(title)) and not gov.search(title):
        return hit("routine", "정정·정기 보고")
    if kind == "유상증자":
        if RE_RIGHTS_THIRD.search(title):
            return hit("rights_third", "제3자 배정")
        if RE_RIGHTS_PUBLIC.search(title):
            return hit("rights_public", "일반공모·주주배정")
        return None                                   # 배정 방식이 제목에 없다 → LLM (설계 5.4의 2번)
    if kind == "전환사채":
        return hit("convertible", "전환사채·신주인수권부사채")
    if kind == "자기주식":
        if RE_BUYBACK_OUT.search(title):
            return None                               # 처분·신탁 해지는 방향이 반대다 → LLM
        if RE_BUYBACK.search(title):
            return hit("buyback", "자기주식 취득·소각")
        return None
    if kind == "공급계약":
        # ratio_ok 는 SQLite 에서 0/1 로 돌아온다. None(검산 안 함)과 0(검산 실패)은 다르다 —
        # 0 이면 블록 경계에서 숫자가 깨진 것이라 그 비율을 믿지 않는다 (newsgap.disclosure 의 규칙).
        if ratio is None or (ratio_ok is not None and not ratio_ok):
            return None                               # 비율을 모르거나 검산이 깨졌다 → LLM (설계 5.4의 3번)
        threshold = float(tunable(cfg, "llm.supply_large_ratio_pct"))
        pct = float(ratio)
        if pct >= threshold:
            return hit("supply_large", f"최근매출액 대비 {pct:.1f}% ≥ {threshold:g}%")
        return hit("supply_small", f"최근매출액 대비 {pct:.1f}% < {threshold:g}%")
    return None


# ---------------------------------------------------------------- 시점·감쇠

def _digits(x):
    return re.sub(r"\D", "", str(x or ""))


def event_date(row):
    """공시의 사건일(접수일). 'YYYYMMDD' 와 'YYYY-MM-DD' 를 모두 받는다."""
    d = _digits(row["rcept_dt"])[:8]
    if len(d) != 8:
        return None
    return to_datetime(f"{d[0:4]}-{d[4:6]}-{d[6:8]}").date()


def known_at(row, fallback_hour):
    """그 공시를 알 수 있었던 시각 (KST naive ISO). first_seen_at 이 없으면 접수일 fallback_hour 시.

    risk_flags.py 와 같은 규칙이다. DART 배포가 18:00 에 끝나므로 실제보다 늦으면 늦었지 빠르지 않다.
    """
    seen = row["first_seen_at"]
    if seen:
        return str(seen)
    d = event_date(row)
    return f"{d.isoformat()}T{int(fallback_hour):02d}:00:00.000" if d else None


def elapsed_trading_days(cal, event_day, ref_day):
    """사건일부터 기준일까지의 **거래일** 경과 수 (같은 날이면 0).

    달력 날짜로 세면 추석 연휴(9/24·25 휴장) 하나로 감쇠가 이틀 더 진행된다. 시장이 열리지
    않은 날은 그 공시를 소화할 기회도 없었으므로 거래일로 센다 (결정 11의 채점 규칙과 같은 눈금).
    """
    if event_day is None or ref_day is None:
        return 0
    if event_day > ref_day:
        return 0                                       # 시점 규칙 위반은 여기가 아니라 first_seen_at 이 막는다
    if cal is None:
        return (ref_day - event_day).days
    days = cal.trading_days_between(event_day, ref_day)
    if not days:
        return 0
    # 사건일이 휴장일이면 그날은 목록에 없다 → 목록 길이가 곧 경과 거래일 수가 된다.
    return max(0, len(days) - 1) if cal.is_trading_day(event_day) else len(days)


def _window_start(cal, ref_day, decay_days):
    """감쇠가 0 이 되기 전까지 거슬러 볼 접수일 하한. 그 밖은 계수 0 이라 점수에 기여하지 않는다."""
    if cal is None:
        return ref_day - timedelta(days=int(decay_days) * 2)
    return cal.add_trading_days(ref_day, -(int(decay_days) - 1))


def ref_day(cal, as_of):
    """감쇠를 재는 기준일. as_of 가 휴장일이면 직전 거래일로 당긴다 (열리지 않은 날은 경과가 아니다)."""
    day = to_datetime(as_of).date()
    if cal is not None and not cal.is_trading_day(day):
        return cal.prev_trading_day(day)
    return day


# ---------------------------------------------------------------- 본문·수치

_BODY_CONNS = {}


def _body_of(cfg, realkey):
    """LS 공시 속보(source 15)의 본문. 없는 것이 정상이라 실패는 조용히 None 이다 (설계 5.4의 4번).

    newsgap.db 는 수집기가 쓰는 DB라 **읽기 전용**으로만 연다 (설계 2.1).
    """
    if not realkey:
        return None
    try:
        path = str(resolve_path(cfg, "newsgap_db"))
        conn = _BODY_CONNS.get(path)
        if conn is None:
            import sqlite3
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            _BODY_CONNS[path] = conn
        row = conn.execute("SELECT body FROM news WHERE realkey=?", (str(realkey),)).fetchone()
    except Exception as exc:
        log.debug("공시 본문 조회 실패(%s): %s", realkey, exc)
        return None
    if not row or not row[0]:
        return None
    try:
        from ...newsgap.body_clean import clean
        return clean(row[0]) or None
    except Exception:
        return None


def _numbers_line(cfg, row, title, body):
    """프롬프트의 [수치] 블록. 본문이 있으면 newsgap 파서가 뽑은 항목을 그대로 쓴다."""
    if body:
        try:
            from ...newsgap.disclosure import parse, to_prompt
            line = to_prompt(parse(title, body))
            if line:
                return line
        except Exception:
            pass
    kind = row["kind"] or kind_of(title)
    parts = [f"공시종류={kind}"]
    if row["ratio"] is not None:
        parts.append(f"최근매출액대비={float(row['ratio']):.2f}%")
        if row["ratio_ok"] == 0:
            parts.append("[숫자 손상 의심]")
    return " | ".join(parts)


# ---------------------------------------------------------------- 채점

def candidates(store, cfg, cal, as_of, stocks=None, decay_days=20):
    """as_of 시점에 **알 수 있었던**, 감쇠 창 안의 공시 행 목록 (접수일 오름차순).

    stocks 를 주면 그 종목만 본다 — 대상 밖 종목의 공시를 채점해 봐야 점수 표에 들어갈 자리가 없다.
    """
    ref = ref_day(cal, as_of)
    lo = _digits(_window_start(cal, ref, decay_days))
    hi = _digits(ref)
    rows = store.conn.execute(
        "SELECT * FROM disclosure WHERE REPLACE(rcept_dt,'-','')>=? AND REPLACE(rcept_dt,'-','')<=? "
        "ORDER BY REPLACE(rcept_dt,'-',''), rcept_no", (lo, hi)).fetchall()
    limit = to_datetime(as_of).isoformat(timespec="milliseconds")
    fallback_hour = int(tunable(cfg, "llm.disclosure_fallback_hour"))
    out = []
    for r in rows:
        code = r["stock_code"]
        if not code or (stocks is not None and code not in stocks):
            continue
        seen = known_at(r, fallback_hour)
        if seen is None or seen > limit:               # 아직 알 수 없었던 공시는 없는 것과 같다
            continue
        out.append(r)
    return out


def _ask_llm(llm, cfg, row, title, body, numbers, rubric, ids):
    """한 건을 LLM 으로 채점한다. 결과가 OK 가 아니면 (None, 상태)."""
    system = prompts.disclosure_system(rubric, llm.prompt_ver)
    user = prompts.disclosure_user(title, corp=row["corp_name"], code=row["stock_code"],
                                   rcept_dt=row["rcept_dt"], numbers=numbers, body=body,
                                   ver=llm.prompt_ver)
    res = llm.call(schemas.TASK_DISCLOSURE, system, user, schemas.disclosure_schema(ids), "classify")
    if not res.ok:
        return None, res
    return res.data, res


def score_disclosures(store, cfg, cal, as_of, llm=None, stocks=None):
    """공시 점수·근거·지배구조 표시를 만든다. 예외를 밖으로 내보내지 않는다.

    반환은 `DisclosureOutcome` 이고, 저장은 `stage.llm_factors` 가 한다 (한 실행의 쓰기를 한곳에 모아
    둬야 중간 실패가 절반만 저장된 표를 남기지 않는다).
    """
    out = DisclosureOutcome()
    meta = (cfg.get("factors") or {}).get("stk_disclosure") or {}
    decay_days = int((meta.get("params") or {}).get("decay_days") or 20)
    sign = int(meta.get("sign") or 1)
    ids = [r["id"] for r in rubric_rows(cfg)]
    rubric = rubric_rows(cfg)
    levels = rubric_levels(cfg)
    gov = re.compile(tunable(cfg, "llm.governance_pattern"))
    max_calls = int(tunable(cfg, "llm.disclosure_max_calls"))
    body_max = int(tunable(cfg, "llm.body_max_chars"))
    ev_max = int(tunable(cfg, "llm.evidence_max_chars"))
    ref = ref_day(cal, as_of)

    rows = candidates(store, cfg, cal, as_of, stocks, decay_days)
    decided, pending = [], []
    for row in rows:
        title = row["report_nm"] or ""
        kind = row["kind"] or kind_of(title)
        hit = code_decide(title, kind, row["ratio"], row["ratio_ok"], cfg)
        if hit is not None:
            rid, level, why = hit
            decided.append((row, Scored(row["rcept_no"], row["stock_code"], rid, level, "code",
                                        why, None, title=title)))
        else:
            pending.append(row)

    # LLM 은 **최근 공시부터** 부른다. 감쇠 계수가 큰 건이 점수에 더 크게 기여하므로, 호출 수
    # 상한에 걸려 남는 건이 생긴다면 오래된 쪽이 남는 편이 낫다.
    pending.sort(key=lambda r: (_digits(r["rcept_dt"]), r["rcept_no"]), reverse=True)
    n_called = 0
    for i, row in enumerate(pending):
        if llm is None or n_called >= max_calls:
            out.n_skipped += 1
            continue
        title = row["report_nm"] or ""
        body = _body_of(cfg, row["ls_realkey"])
        body = body[:body_max] if body else None
        numbers = _numbers_line(cfg, row, title, body)
        data, res = _ask_llm(llm, cfg, row, title, body, numbers, rubric, ids)
        if res.status in (STATUS_DISABLED, STATUS_BUDGET):
            out.status = res.status                   # 나머지도 같은 이유로 막힐 것이다
            if res.status == STATUS_BUDGET:           # 예산은 실행 중에 풀리지 않는다 → 여기서 멈춘다
                out.n_skipped += len(pending) - i
                break
            llm = None                                # 껐으면 더 부르지 않는다 (기록만 늘어난다)
            out.n_skipped += 1
            continue
        if not res.cache_hit:
            n_called += 1
        if data is None:
            out.n_error += 1                          # 기권: 그 공시는 점수에 넣지 않는다
            continue
        rid = data["rubric_id"]
        # 기준표에 수준이 고정된 항목은 **코드의 값이 이긴다**. 모델은 어느 칸인지만 고르면 된다
        # (설계 5.4: 숫자는 코드, 글은 LLM). other_material 만 모델이 정한 수준을 그대로 쓴다.
        level = int(data["level"]) if rid == "other_material" else levels.get(rid, int(data["level"]))
        decided.append((row, Scored(row["rcept_no"], row["stock_code"], rid, level, "llm",
                                    clip_text(data.get("evidence"), ev_max),
                                    float(data.get("confidence", 0.0)), title=title,
                                    call_id=res.call_id)))

    # ---- 종목별 합산 (설계 5.2: Σ 수준/2 × 감쇠, [-1,+1] 로 자름)
    per_stock, raw_sum = {}, {}
    for row, sc in decided:
        day = event_date(row)
        sc.event_date = day
        sc.elapsed = elapsed_trading_days(cal, day, ref)
        sc.decay = linear_decay(sc.elapsed, decay_days)
        unit, _missing = rubric_score(sc.level, sign)
        per_stock.setdefault(sc.code, 0.0)
        per_stock[sc.code] += unit * sc.decay
        raw_sum[sc.code] = raw_sum.get(sc.code, 0.0) + unit
        out.scored.append(sc)
        if sc.decided_by == "code":
            out.n_code += 1
        else:
            out.n_llm += 1
        out.evidence_rows.append({
            "entity": sc.code, "factor_id": "stk_disclosure", "ref_type": "disclosure",
            "ref_id": sc.rcept_no,
            "summary": clip_text(f"{sc.rubric_id} level={sc.level:+d} "
                                 f"감쇠 {sc.decay:.2f}({sc.elapsed}거래일) [{sc.decided_by}] "
                                 f"{sc.title} — {sc.evidence}", 300)})
        # 지배구조 위험: 제목에서 보이면 코드, 모델이 본문에서 인용했으면 llm (설계 5.5)
        src = "code" if gov.search(sc.title or "") else ("llm" if gov.search(sc.evidence or "") else None)
        if src:
            out.flag_rows.append({"entity": sc.code, "flag_type": "governance", "src": src,
                                  "detail": clip_text(f"{row['rcept_dt']} {sc.title} — {sc.evidence}", 200)})

    for code, total in per_stock.items():
        out.factor_rows.append({"entity": code, "factor_id": "stk_disclosure",
                                "raw_value": raw_sum.get(code), "score": clip(total, -1.0, 1.0),
                                "missing": 0})
    log.info("공시 채점: 대상 %d건 → 코드 %d · LLM %d · 기권 %d · 보류 %d → 종목 %d개",
             len(rows), out.n_code, out.n_llm, out.n_error, out.n_skipped, len(out.factor_rows))
    return out


__all__ = ["DisclosureOutcome", "RATIO_IS_PERCENT", "Scored", "candidates", "code_decide",
           "elapsed_trading_days", "event_date", "kind_of", "known_at", "ref_day", "rubric_levels",
           "rubric_rows", "score_disclosures"]
