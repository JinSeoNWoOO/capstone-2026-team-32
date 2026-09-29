"""리포트 JSON 조립 (설계 10.1). advisor.db 를 **읽기만** 해서 API 가 그대로 내보낼 dict 를 만든다.

이 파일의 모양을 정하는 규칙 네 가지.

1. **읽기 전용.** 모든 함수는 `Store(path, readonly=True)` 로 연 store 를 받는다. 리포트가 판단
   기록을 건드리면 증빙(결정 13)이 무너진다. 계산은 전부 여기서 하고 DB 에는 아무것도 쓰지 않는다.
2. **비어 있어도 500 을 내지 않는다.** 배치가 한 번도 안 돌았거나 테이블이 아직 없는 상태가
   개발 중에는 오히려 정상이다. 그런 경우 예외를 올리지 않고 `{"available": False, "reason": …}`
   를 돌려준다 — 화면이 "아직 기록이 없습니다"를 그릴 수 있게.
3. **실시간과 재현을 절대 섞지 않는다** (설계 7.4). 모든 조회는 `mode` 를 받고, 포트폴리오는
   id 의 `@replay` 꼬리표로 갈라 본다. 하나의 곡선에 두 모드를 이어 붙이면 그 순간 성과 주장이
   오염된다.
4. **조정값은 인자로 받는다.** advisor.config.yaml 은 다른 담당이 소유하고, 아직 설정에 없는
   값(표에 몇 줄을 보일 것인가 등)의 기본값 표는 `backend/app/services/advisor.py` 의 DEFAULTS
   한 곳에 있다. 여기 인자 기본값은 그 표 없이 직접 불렀을 때의 마지막 방어선이다.

돌려주는 값은 전부 JSON 으로 그대로 나가는 dict·list·수·문자열·None 이다 (sqlite3.Row 를 흘리지 않는다).
"""
import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .calendar import TradingCalendar, to_date
from .config import ConfigError, resolve_path

KST = timezone(timedelta(hours=9))

MARKET_ENTITY = "MARKET"                    # 시장 계층의 유일한 entity (factors/market.py 와 같은 값)
REPLAY_SUFFIX = "@replay"                   # 재현 모드 포트폴리오 id 의 꼬리표 (설계 6.3)
VARIANTS = ("v0", "llm")
STAGES = ("final", "prelim")                # 기본값을 고를 때의 우선순위: 최종이 그날의 마지막 판단

LAYER_LABELS = {"market": "시장", "sector": "섹터", "stock": "종목"}
ROLE_LABELS = {"cash": "현금", "core": "핵심", "sector": "섹터", "stock": "종목"}
ROLE_ORDER = ("cash", "core", "sector", "stock")
STAGE_LABELS = {"prelim": "예비", "final": "최종"}
VARIANT_LABELS = {"v0": "v0 (규칙)", "llm": "LLM 조정"}
STATUS_LABELS = {"ok": "완료", "error": "실패", "skipped": "휴장", "running": "실행 중"}

# 위험 표시 (설계 5.5). 화면에 한글로 보이되 값 자체는 DB 의 flag_type 을 그대로 쓴다.
FLAG_LABELS = {"halt": "거래정지", "market_action": "시장조치", "governance": "지배구조",
               "spike": "급등락", "theme": "테마 의심"}

# 설정의 factors 블록에 표시 이름(name)이 없을 때 쓰는 한글 이름. 설정에 name 이 생기면 그쪽이 이긴다.
FACTOR_LABELS = {
    "mkt_trend": "지수 추세 (200일 이동평균)",
    "mkt_vol": "지수 변동성 (20일)",
    "mkt_overnight": "밤사이 해외 시장",
    "mkt_credit": "신용잔고 비율 (관찰)",
    "sec_flow": "섹터 수급 (20일 순매수)",
    "sec_trend": "섹터 추세 (200일 이동평균)",
    "stk_high52": "52주 고가 대비",
    "stk_flow": "종목 수급 (20일 순매수)",
    "stk_disclosure": "공시 채점 (LLM)",
    "news_risk": "뉴스 분류 (관찰, LLM)",
}

# 포트폴리오 이름과 갈래 (설계 6.3). 화면의 범례가 이 표에서 나온다.
PORTFOLIO_LABELS = {
    "sys_final_llm": ("주 포트폴리오 (최종·LLM)", "system"),
    "sys_final_v0": ("최종·v0 (기준선 4)", "system"),
    "sys_prelim_llm": ("예비 판단 (기준선 5)", "system"),
    "bl_kodex200": ("KODEX 200 보유 (기준선 1)", "baseline"),
    "bl_sma10m": ("이동평균 규칙 (기준선 2)", "baseline"),
    "bl_6040": ("60/40 (기준선 3)", "baseline"),
}

# LLM 조정의 근거 종류 (factor_evidence.ref_type). 화면의 배지 글자가 여기서 나온다.
EVIDENCE_REF_LABELS = {"news": "뉴스", "disclosure": "공시"}

# factor_evidence.summary 의 기계용 머리말. 뉴스는 "policy 방향 +1 심각도 2 감쇠 1.00(0거래일)",
# 공시는 "buyback level=+1 감쇠 0.90(2거래일) [code]" 로 시작한다 (news_risk.py·disclosure_score.py).
# 두 모양 모두 "감쇠 x.xx(N거래일)" 을 지나므로 거기까지(+ 공시의 [code]·[llm] 판정 주체)를 머리말로
# 떼어 낸다. 판정 주체는 영문 소문자로만 받는다 — "[아프리카 녹색전환]" 같은 기사 제목의 말머리를
# 머리말로 삼키지 않게.
_EVIDENCE_HEAD = re.compile(
    r"^(?P<head>.*?감쇠\s*[\d.]+\((?P<elapsed>\d+)거래일\)(?:\s*\[[a-z_+]+\])?)\s*(?P<rest>.*)$", re.S)
_EVIDENCE_SEP = " — "                       # 요약에서 제목과 LLM 한 줄 이유를 가르는 구분자

TRADING_DAYS_PER_YEAR = 252                 # 변동성 연율화 계수 (portfolio.py 와 같은 값)
START_NAV = 1.0                             # 초기 NAV (설계 6.2)


# ---------------------------------------------------------------- 낮은 수준 도구

def _rows(store, sql, params=()):
    """조회 결과 목록. 테이블이 아직 없거나 DB 가 깨져 있어도 빈 목록으로 물러선다 (규칙 2)."""
    try:
        return store.conn.execute(sql, tuple(params)).fetchall()
    except sqlite3.Error:
        return []


def _one(store, sql, params=()):
    rows = _rows(store, sql, params)
    return rows[0] if rows else None


def _f(value):
    """float 또는 None. NaN·무한대는 JSON 으로 못 나가므로 None 으로 떨군다."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


def _b(value):
    return bool(value)


def _json(text):
    """DB 에 문자열로 들어 있는 JSON 을 판다. 깨져 있으면 원문을 그대로 돌려준다."""
    if text in (None, ""):
        return None
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _unavailable(reason, **extra):
    out = {"available": False, "reason": reason}
    out.update(extra)
    return out


def _today():
    return datetime.now(KST).strftime("%Y-%m-%d")


def parse_note(note):
    """run.note 를 푼다. 수집 요약(JSON) 이거나 오류 메시지(평문)다.

    수집 담당이 넣는 요약의 모양은 `{"ingest": {"providers": …, "fallbacks_used": […]}, …}` 이고,
    아직 그 모듈이 없을 때는 note 가 비어 있거나 평문이다. 둘 다 그대로 받아 넘긴다.
    """
    out = {"text": None, "summary": None, "providers": None, "fallbacks_used": None,
           "ingest_error": None}
    if note in (None, ""):
        return out
    data = _json(note)
    if not isinstance(data, dict):
        out["text"] = str(note)
        return out
    out["summary"] = data
    ingest = data.get("ingest")
    if not isinstance(ingest, dict):
        ingest = data
    providers = ingest.get("providers")
    if isinstance(providers, dict):
        providers = [{"name": k, "detail": v} for k, v in providers.items()]
    elif isinstance(providers, (list, tuple)):
        providers = list(providers)
    elif providers is not None:
        providers = [providers]
    out["providers"] = providers
    fallbacks = ingest.get("fallbacks_used")
    if isinstance(fallbacks, dict):
        fallbacks = [f"{k}: {v}" for k, v in fallbacks.items()]
    elif isinstance(fallbacks, (list, tuple)):
        fallbacks = list(fallbacks)
    elif fallbacks is not None:
        fallbacks = [fallbacks]
    out["fallbacks_used"] = fallbacks
    err = ingest.get("error") or data.get("error")
    out["ingest_error"] = str(err) if err else None
    return out


def _status(row):
    """run.status 는 쓰는 쪽에 따라 대소문자가 다르다 (store 기본값 'OK', run.py 상수 'ok')."""
    return str(row["status"] or "").lower() if row is not None else None


# ---------------------------------------------------------------- 이름표

def names_at(store, date):
    """그 날짜 이전(포함) **가장 최근 스냅샷**의 자산 이름표 {code: {name, kind, sector}}.

    종목마다 따로 최근 스냅샷을 찾는다 — 지수에서 빠진 종목도 그때의 이름은 남아 있어야
    과거 판단 화면에 코드만 덩그러니 남지 않는다.
    """
    sql = ("SELECT u.code, u.name, u.kind, u.sector FROM universe u "
           "JOIN (SELECT code, MAX(as_of_date) AS m FROM universe WHERE as_of_date<=? GROUP BY code) x "
           "  ON x.code=u.code AND x.m=u.as_of_date")
    out = {}
    for r in _rows(store, sql, (str(date),)):
        out[r["code"]] = {"name": r["name"], "kind": r["kind"], "sector": r["sector"]}
    return out


def _name_of(names, code):
    meta = names.get(code)
    return (meta or {}).get("name") or code


def factor_meta(cfg, layer=None):
    """설정의 factors 블록 → 표시용 메타데이터 목록 (설정 파일의 등장 순서를 지킨다)."""
    out = []
    for fid, meta in ((cfg or {}).get("factors") or {}).items():
        if not isinstance(meta, dict):
            continue
        if layer is not None and meta.get("layer") != layer:
            continue
        out.append({
            "factor_id": fid,
            "name": meta.get("name") or FACTOR_LABELS.get(fid, fid),
            "layer": meta.get("layer"),
            "weight": meta.get("weight"),
            "sign": meta.get("sign"),
            "horizon": meta.get("horizon"),
            "transform": meta.get("transform"),
            "source": meta.get("source"),
            "llm": bool(meta.get("llm")),
            "stages": list(meta.get("stages") or ()) or None,
        })
    return out


# ---------------------------------------------------------------- 실행 찾기

def available_dates(store, mode="live", limit=400):
    """날짜 선택기가 쓰는 목록. 최신 날짜가 앞이고, 날짜마다 어느 단계가 있는지 함께 준다."""
    rows = _rows(store,
                 "SELECT r.as_of AS d, r.stage, r.status, "
                 "       MAX(EXISTS(SELECT 1 FROM decision x WHERE x.run_id=r.run_id)) AS has_decision "
                 "FROM run r WHERE r.mode=? GROUP BY r.as_of, r.stage "
                 "ORDER BY r.as_of DESC", (str(mode),))
    by_date = {}
    order = []
    for r in rows:
        day = r["d"]
        if day not in by_date:
            by_date[day] = {"date": day, "stages": [], "has_decision": False}
            order.append(day)
        by_date[day]["stages"].append({"stage": r["stage"], "status": str(r["status"] or "").lower()})
        by_date[day]["has_decision"] = by_date[day]["has_decision"] or bool(r["has_decision"])
    return [by_date[d] for d in order[:limit]]


def runs_on(store, date, mode="live"):
    """그 날짜의 실행 {단계: 상태}. 스케줄러가 "오늘 이미 띄웠는가"를 이걸로 본다."""
    out = {}
    for r in _rows(store, "SELECT stage, status FROM run WHERE mode=? AND as_of=? "
                          "ORDER BY run_id DESC", (str(mode), str(date))):
        out.setdefault(r["stage"], str(r["status"] or "").lower())
    return out


def resolve_run(store, date=None, stage=None, mode="live"):
    """조회 조건 → run 행 하나. 날짜를 생략하면 **판단이 남은 가장 최근 날짜**를 고른다.

    단계를 생략하면 그날 실행 중 판단 시각이 가장 늦은 것(보통 저녁의 예비)을 고르되,
    판단 행이 있는 실행을 먼저 본다 — 최신 실행이 실패로 끝났으면 그 앞의 성공한 실행을 보여야
    화면이 빈 채로 남지 않는다.
    """
    sql = ("SELECT r.*, EXISTS(SELECT 1 FROM decision d WHERE d.run_id=r.run_id) AS has_decision "
           "FROM run r WHERE r.mode=?")
    params = [str(mode)]
    if date:
        sql += " AND r.as_of=?"
        params.append(str(date))
    if stage:
        sql += " AND r.stage=?"
        params.append(str(stage))
    sql += (" ORDER BY has_decision DESC, r.as_of DESC, "
            "COALESCE(r.decision_time, r.started_at) DESC, r.run_id DESC LIMIT 1")
    return _one(store, sql, params)


def run_info(row):
    """run 행 → 화면용 dict. note 는 수집 요약·오류로 갈라 담는다."""
    if row is None:
        return None
    status = _status(row)
    return {
        "run_id": row["run_id"],
        "stage": row["stage"],
        "stage_label": STAGE_LABELS.get(row["stage"], row["stage"]),
        "mode": row["mode"],
        "as_of": row["as_of"],
        "decision_time": row["decision_time"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "config_hash": row["config_hash"],
        "git_sha": row["git_sha"],
        "llm_used": _b(row["llm_used"]),
        "fallback_used": _b(row["fallback_used"]),
        "note": parse_note(row["note"]),
    }


def _variants_of(store, run_id):
    """그 실행에 남은 판단 버전. llm 이 없는 날은 LLM 이 기권한 날이다 (결정 15)."""
    rows = _rows(store, "SELECT DISTINCT variant FROM decision WHERE run_id=? ORDER BY variant",
                 (run_id,))
    found = [r["variant"] for r in rows]
    return [v for v in VARIANTS if v in found] + [v for v in found if v not in VARIANTS]


# ---------------------------------------------------------------- 점수표

def _factor_rows(store, run_id, entity=None):
    sql = "SELECT * FROM factor_value WHERE run_id=?"
    params = [run_id]
    if entity is not None:
        sql += " AND entity=?"
        params.append(entity)
    return _rows(store, sql + " ORDER BY entity, factor_id", params)


def _breakdown(metas, values):
    """요인 메타 + 그 실행의 값 → 표 한 줄씩. 결측은 분자·분모에서 모두 뺀다 (설계 5.3).

    기여도는 '그 요인이 계층 점수를 얼마나 끌어올렸는가'다: 가중치 × 점수 ÷ Σ가중치.
    기여도를 모두 더하면 그 계층 점수가 된다 — 화면이 점수 분해를 그대로 보여줄 수 있게.
    가중치 0 인 관찰 요인은 점수는 보이지만 기여도가 0 이다 (결정 6).
    """
    total_w = 0.0
    for meta in metas:
        row = values.get(meta["factor_id"])
        if row is None or row["missing"]:
            continue
        total_w += float(meta.get("weight") or 0)
    out = []
    for meta in metas:
        row = values.get(meta["factor_id"])
        missing = row is None or bool(row["missing"])
        score = None if row is None else _f(row["score"])
        weight = meta.get("weight")
        contrib = None
        if not missing and score is not None and total_w > 0:
            contrib = float(weight or 0) * score / total_w
        item = dict(meta)
        item.update({
            "raw_value": None if row is None else _f(row["raw_value"]),
            "score": None if missing else score,
            "missing": missing,
            "contribution": contrib,
        })
        out.append(item)
    return out


def _layer_score_of(breakdown):
    """분해된 기여도의 합 = 계층 점수. 쓸 수 있는 요인이 하나도 없으면 None (설계 5.3)."""
    parts = [b["contribution"] for b in breakdown if b["contribution"] is not None]
    return round(sum(parts), 6) if parts else None


def market_breakdown(store, cfg, run_id):
    """시장 점수의 구성 (설계 10.2 ②의 첫 표)."""
    values = {r["factor_id"]: r for r in _factor_rows(store, run_id, MARKET_ENTITY)}
    return _breakdown(factor_meta(cfg, "market"), values)


def _entity_scores(store, run_id, layer):
    """계층별 composite 행 {variant: {entity: row}}."""
    out = {}
    for r in _rows(store, "SELECT * FROM composite WHERE run_id=? AND layer=?", (run_id, layer)):
        out.setdefault(r["variant"], {})[r["entity"]] = r
    return out


def _flags_by_entity(store, run_id):
    out = {}
    for r in _rows(store, "SELECT * FROM risk_flag WHERE run_id=? ORDER BY entity, flag_type",
                   (run_id,)):
        out.setdefault(r["entity"], []).append({
            "flag_type": r["flag_type"],
            "label": FLAG_LABELS.get(r["flag_type"], r["flag_type"]),
            "src": r["src"],
            "detail": r["detail"],
        })
    return out


def _score_table(store, cfg, run, layer, names=None, top_n=None):
    """한 계층의 점수표. 요인별 점수를 열로 펼치고 v0·LLM 종합 점수를 나란히 놓는다."""
    run_id = run["run_id"]
    names = names_at(store, run["as_of"]) if names is None else names
    metas = factor_meta(cfg, layer)
    comps = _entity_scores(store, run_id, layer)
    flags = _flags_by_entity(store, run_id)

    by_entity = {}
    for r in _factor_rows(store, run_id):
        by_entity.setdefault(r["entity"], {})[r["factor_id"]] = r

    entities = set(comps.get("v0", {})) | set(comps.get("llm", {}))
    if layer == "market":
        entities |= {MARKET_ENTITY}
    if not entities:
        # composite 가 아직 없으면 요인 값이 남은 대상만으로라도 표를 만든다
        entities = {e for e, vals in by_entity.items()
                    if any(fid in vals for fid in (m["factor_id"] for m in metas))}

    rows = []
    for entity in entities:
        v0 = comps.get("v0", {}).get(entity)
        llm = comps.get("llm", {}).get(entity)
        meta = names.get(entity) or {}
        rows.append({
            "entity": entity,
            "name": meta.get("name") or (entity if entity != MARKET_ENTITY else "시장 전체"),
            "kind": meta.get("kind"),
            "sector": meta.get("sector"),
            "layer": layer,
            "score_v0": None if v0 is None else _f(v0["final_score"]),
            "score_llm": None if llm is None else _f(llm["final_score"]),
            "base_score": None if v0 is None else _f(v0["base_score"]),
            "adj": None if llm is None else _f(llm["adj"]),
            "vetoed": bool(llm["vetoed"]) if llm is not None else False,
            "flags": flags.get(entity, []),
            "factors": _breakdown(metas, by_entity.get(entity, {})),
        })
    rows.sort(key=lambda r: (r["score_v0"] is None, -(r["score_v0"] or 0.0), r["entity"]))
    total = len(rows)
    if top_n:
        rows = rows[:int(top_n)]
    return {"layer": layer, "layer_label": LAYER_LABELS.get(layer, layer),
            "factors": metas, "rows": rows, "total": total}


# ---------------------------------------------------------------- 비중

def _weights(store, run_id, variant):
    return _rows(store, "SELECT * FROM target_weight WHERE run_id=? AND variant=? ORDER BY role, asset",
                 (run_id, variant))


def _weight_map(store, run_id, variant):
    return {r["asset"]: r for r in _weights(store, run_id, variant)}


def _role_totals(weight_map):
    totals = {role: 0.0 for role in ROLE_ORDER}
    for row in weight_map.values():
        role = row["role"] if row["role"] in totals else "stock"
        totals[role] += _f(row["weight"]) or 0.0
    return totals


def _weight_table(store, run_id, names, variants):
    """v0 와 LLM 비중을 한 표에 나란히 (설계 10.2 ③)."""
    maps = {v: _weight_map(store, run_id, v) for v in variants}
    assets = set()
    for m in maps.values():
        assets |= set(m)
    rows = []
    for asset in assets:
        any_row = next((maps[v][asset] for v in variants if asset in maps[v]), None)
        role = any_row["role"] if any_row is not None else "stock"
        item = {
            "asset": asset,
            "name": _name_of(names, asset),
            "role": role,
            "role_label": ROLE_LABELS.get(role, role),
        }
        for v in variants:
            row = maps[v].get(asset)
            item[f"weight_{v}"] = _f(row["weight"]) if row is not None else None
        if "v0" in variants and "llm" in variants:
            a, b = item.get("weight_v0"), item.get("weight_llm")
            item["delta"] = None if (a is None and b is None) else (b or 0.0) - (a or 0.0)
        rows.append(item)
    order = {role: i for i, role in enumerate(ROLE_ORDER)}
    rows.sort(key=lambda r: (order.get(r["role"], 9), -(r.get("weight_v0") or r.get("weight_llm") or 0.0)))
    return rows


# ---------------------------------------------------------------- LLM 조정의 근거

def _factor_names(cfg):
    """{요인 id: 표시 이름}. factor_meta 와 같은 규칙 (설정의 name → FACTOR_LABELS → id)."""
    out = dict(FACTOR_LABELS)
    out.update({m["factor_id"]: m["name"] for m in factor_meta(cfg)})
    return out


def _factor_list(items, factor_names, with_reason=False):
    """adopted_json·rejected_json → [{factor_id, name(, reason)}].

    LLM 출력 모양은 스키마(llm/schemas.py)상 채택은 id 문자열, 기각은 {factor_id, reason} 이지만
    예전 기록이나 손으로 넣은 행이 다를 수 있어 둘 다 받아 준다. 모르는 모양은 원문을 이름으로 둔다.
    """
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if isinstance(item, dict):
            fid = item.get("factor_id") or item.get("id")
            reason = item.get("reason")
        else:
            fid, reason = item, None
        fid = None if fid is None else str(fid)
        entry = {"factor_id": fid, "name": factor_names.get(fid, fid) if fid else str(item)}
        if with_reason:
            entry["reason"] = reason
        out.append(entry)
    return out


def _parse_dt(text):
    """ISO 문자열 또는 뉴스의 YYYYMMDDHHMMSS → 시간대 없는 KST datetime. 못 읽으면 None."""
    if not text:
        return None
    raw = str(text).strip()
    if raw.isdigit() and len(raw) >= 8:          # 뉴스 YYYYMMDDHHMMSS, DART 원형 접수일 YYYYMMDD
        try:
            return datetime.strptime(raw[:14].ljust(14, "0"), "%Y%m%d%H%M%S")
        except ValueError:
            return None
    try:
        out = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return out.astimezone(KST).replace(tzinfo=None) if out.tzinfo else out


def _mmdd_hhmm(dt):
    return f"{dt.month}/{dt.day} {dt:%H:%M}"


def _ago_label(minutes):
    """판단 시각과 근거 시각의 차이를 사람이 읽는 말로. 음수면 판단 **뒤**의 근거다."""
    after = minutes < 0
    m = abs(int(minutes))
    days, rest = divmod(m, 1440)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days}일")
    if hours:
        parts.append(f"{hours}시간")
    if mins or not parts:
        parts.append(f"{mins}분")
    return f"판단 {' '.join(parts)} {'후' if after else '전'}"


def _split_summary(summary, title=None):
    """근거 요약 → (머리말, 제목, 한 줄 이유, 기록된 경과 거래일).

    제목을 원장(뉴스 DB·disclosure 표)에서 알면 그걸로 자른다 — 제목 안에 '—' 가 들어 있어도
    어긋나지 않게. 모르면 첫 ' — ' 에서 자른다.
    """
    text = str(summary or "")
    head, elapsed, rest = None, None, text
    m = _EVIDENCE_HEAD.match(text)
    if m:
        head, rest = m.group("head").strip(), m.group("rest")
        elapsed = int(m.group("elapsed"))
    if title and rest.startswith(title):
        detail = rest[len(title):].strip()
        if detail.startswith("—"):
            detail = detail[1:].strip()
        return head, title, detail or None, elapsed
    if _EVIDENCE_SEP in rest:
        t, detail = rest.split(_EVIDENCE_SEP, 1)
        return head, t.strip() or None, detail.strip() or None, elapsed
    return head, rest.strip() or None, None, elapsed


def _newsgap_path(cfg):
    try:
        path = Path(resolve_path(cfg or {}, "newsgap_db"))
    except (ConfigError, KeyError, TypeError, AttributeError):
        return None
    return path if path.exists() else None


def _news_meta(cfg, keys):
    """newsgap.db 의 뉴스 {realkey: {ls_datetime, recv_wall, title}}.

    **읽기 전용 URI** 로만 연다 — 이 DB 는 뉴스 수집기가 쓰는 중이고 리포트는 손대면 안 된다.
    DB 가 없거나 잠겨 있으면 빈 dict (시각 없이 요약만 보인다, 규칙 2).
    """
    keys = sorted({str(k) for k in keys if k})
    path = _newsgap_path(cfg) if keys else None
    if path is None:
        return {}
    out = {}
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return {}
    try:
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            sql = ("SELECT realkey, ls_datetime, recv_wall, title FROM news WHERE realkey IN (%s)"
                   % ",".join("?" * len(chunk)))
            for realkey, ls_dt, recv, title in conn.execute(sql, chunk).fetchall():
                out[realkey] = {"ls_datetime": ls_dt, "recv_wall": recv, "title": title}
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return out


def _elapsed_trading_days(cal, event_day, ref_day):
    """사건일부터 기준일까지의 거래일 경과 수. llm/disclosure_score.elapsed_trading_days 와 같은 규칙이다
    (감쇠에 쓴 값과 화면 숫자가 같아야 하므로). 리포트가 배치 모듈을 import 하지 않으려고 옮겨 적었다."""
    if event_day is None or ref_day is None or event_day > ref_day:
        return 0
    days = cal.trading_days_between(event_day, ref_day)
    if not days:
        return 0
    return max(0, len(days) - 1) if cal.is_trading_day(event_day) else len(days)


def _evidence_item(row, factor_names, decision_dt, news, disclosures, cal_ref):
    """factor_evidence 행 하나 → 화면용 근거 (종류·제목·시각·판단 대비 경과)."""
    ref_type, ref_id = row["ref_type"], row["ref_id"]
    item = {
        "factor_id": row["factor_id"],
        "factor_name": factor_names.get(row["factor_id"], row["factor_id"]),
        "ref_type": ref_type,
        "ref_label": EVIDENCE_REF_LABELS.get(ref_type, ref_type),
        "ref_id": ref_id,
        "summary": row["summary"],
        "published_at": None,       # 뉴스: 기사 시각(ls_datetime) / 공시: 접수일(rcept_dt)
        "received_at": None,        # 뉴스: 수신 시각(recv_wall) / 공시: 처음 확인 시각(first_seen_at)
        "minutes_before": None,
        "trading_days_before": None,
        "after_decision": None,
        "time_label": None,
        "age_label": None,
    }
    title = None
    if ref_type == "news":
        meta = news.get(str(ref_id)) or {}
        title = meta.get("title")
        pub = _parse_dt(meta.get("ls_datetime"))
        if pub is not None:
            item["published_at"] = pub.isoformat(timespec="seconds")
            item["time_label"] = _mmdd_hhmm(pub)
        item["received_at"] = meta.get("recv_wall")
        if pub is not None and decision_dt is not None:
            minutes = math.floor((decision_dt - pub).total_seconds() / 60.0)
            item["minutes_before"] = minutes
            item["after_decision"] = minutes < 0
            item["age_label"] = _ago_label(minutes)
    elif ref_type == "disclosure":
        d = disclosures.get(str(ref_id))
        if d is not None:
            title = d["report_nm"]
            item["disclosure"] = {"report_nm": d["report_nm"], "kind": d["kind"],
                                  "rcept_dt": d["rcept_dt"], "first_seen_at": d["first_seen_at"],
                                  "first_seen_src": d["first_seen_src"]}
            day = _parse_dt(d["rcept_dt"])
            item["published_at"] = day.date().isoformat() if day is not None else d["rcept_dt"]
            item["received_at"] = d["first_seen_at"]
    head, t, detail, recorded = _split_summary(row["summary"], title)
    if detail and t and detail.strip() == t.strip():
        detail = None                   # 공시 요약은 제목을 이유 자리에 한 번 더 적는 경우가 많다
    item.update({"head": head, "title": t, "detail": detail, "elapsed_recorded": recorded})

    if ref_type == "disclosure":
        try:
            event_day = to_date(item["published_at"]) if item["published_at"] else None
        except (TypeError, ValueError):
            event_day = None
        days = None
        if event_day is not None:
            try:
                days = _elapsed_trading_days(cal_ref()[0], event_day, cal_ref()[1])
            except Exception:           # 달력을 못 만들면 기록된 값으로 물러선다
                days = None
        if days is None:
            days = recorded
        item["trading_days_before"] = days
        seen = _parse_dt(item["received_at"])
        if seen is not None and decision_dt is not None:
            item["minutes_before"] = math.floor((decision_dt - seen).total_seconds() / 60.0)
            item["after_decision"] = item["minutes_before"] < 0
        if event_day is not None:
            item["time_label"] = f"접수 {event_day.month}/{event_day.day}"
            item["age_label"] = ("접수 " + f"{event_day.month}/{event_day.day}"
                                 + ("" if days is None else
                                    (" · 판단일 당일" if days == 0 else f" · {days}거래일 전")))
    elif item["trading_days_before"] is None:
        item["trading_days_before"] = recorded
    return item


def _sort_evidence(items):
    """최근 근거가 먼저, 시각을 모르는 근거는 뒤로 (같은 시각이면 참조 id 순)."""
    items.sort(key=lambda it: str(it.get("ref_id")))
    items.sort(key=lambda it: str(it.get("published_at") or ""), reverse=True)
    return items


def llm_adjustments(store, run_id, names, limit=30, cfg=None, run=None, evidence_limit=20):
    """LLM 이 실제로 건드린 자산만 (설계 10.2 ③). 조정 없음·거부 없음인 자산은 빼서 화면을 비운다.

    채택·기각 목록과 이유를 그대로 실어 보낸다 — "무엇을 근거로 올렸고 무엇을 왜 버렸는가"가
    이 모드의 결과물이고 (결정 4), 숫자만 남기면 사후에 그 판단을 검증할 수 없다.

    여기에 **근거 목록과 그 시각**을 붙인다 (같은 run·entity 의 factor_evidence). 이유 한 줄만
    보이면 "옛날 뉴스를 보고 판단했나?" 를 화면에서 가릴 수 없다. 뉴스는 기사 시각과 판단까지
    걸린 시간, 공시는 접수일과 경과 거래일(감쇠에 쓴 것과 같은 눈금)을 보인다. 채택 요인에 딸린
    근거가 있으면 그것만, 없으면 그 자산의 전체 근거를 보이고 `evidence_scope` 로 어느 쪽인지 알린다.
    기존 필드는 그대로 두고 추가만 한다 (다른 화면·테스트 호환).
    """
    if run is None:
        run = _one(store, "SELECT * FROM run WHERE run_id=?", (run_id,))
    decision_dt = None
    as_of = None
    if run is not None:
        decision_dt = _parse_dt(run["decision_time"] or run["started_at"])
        as_of = run["as_of"]
    factor_names = _factor_names(cfg)

    comps = _rows(store, "SELECT * FROM composite WHERE run_id=? AND variant='llm' "
                         "AND (COALESCE(adj,0) <> 0 OR vetoed=1)", (run_id,))
    entities = [r["entity"] for r in comps]
    ev_by_entity = {}
    if entities:
        sql = ("SELECT * FROM factor_evidence WHERE run_id=? AND entity IN (%s) ORDER BY factor_id"
               % ",".join("?" * len(entities)))
        for e in _rows(store, sql, [run_id, *entities]):
            ev_by_entity.setdefault(e["entity"], []).append(e)
    all_ev = [e for rows_ in ev_by_entity.values() for e in rows_]
    news = _news_meta(cfg, [e["ref_id"] for e in all_ev if e["ref_type"] == "news"])
    disc_keys = sorted({str(e["ref_id"]) for e in all_ev if e["ref_type"] == "disclosure" and e["ref_id"]})
    disclosures = {}
    for i in range(0, len(disc_keys), 500):
        chunk = disc_keys[i:i + 500]
        for d in _rows(store, "SELECT * FROM disclosure WHERE rcept_no IN (%s)" % ",".join("?" * len(chunk)),
                       chunk):
            disclosures[str(d["rcept_no"])] = d

    cal_cache = []

    def cal_ref():
        """(달력, 감쇠 기준일). 공시 근거가 있을 때만 만든다 (market_daily 를 한 번 읽는다).
        기준일은 판단 날짜, 휴장일이면 직전 거래일 — disclosure_score.ref_day 와 같다."""
        if not cal_cache:
            cal = TradingCalendar(cfg or {}, store)
            ref = to_date(as_of) if as_of else (decision_dt.date() if decision_dt else None)
            if ref is not None and not cal.is_trading_day(ref):
                ref = cal.prev_trading_day(ref)
            cal_cache.append((cal, ref))
        return cal_cache[0]

    rows = []
    for r in comps:
        adopted = _json(r["adopted_json"])
        rejected = _json(r["rejected_json"])
        adopted_factors = _factor_list(adopted, factor_names)
        adopted_ids = {a["factor_id"] for a in adopted_factors if a["factor_id"]}
        ev_rows = ev_by_entity.get(r["entity"], [])
        linked = [e for e in ev_rows if e["factor_id"] in adopted_ids]
        scope = "adopted" if linked else ("entity" if ev_rows else "none")
        chosen = linked if linked else ev_rows
        evidence = [_evidence_item(e, factor_names, decision_dt, news, disclosures, cal_ref)
                    for e in chosen]
        _sort_evidence(evidence)
        rows.append({
            "entity": r["entity"],
            "name": "시장 전체" if r["entity"] == MARKET_ENTITY else _name_of(names, r["entity"]),
            "layer": r["layer"],
            "layer_label": LAYER_LABELS.get(r["layer"], r["layer"]),
            "base_score": _f(r["base_score"]),
            "adj": _f(r["adj"]),
            "final_score": _f(r["final_score"]),
            "vetoed": bool(r["vetoed"]),
            "adopted": adopted,
            "rejected": rejected,
            "reason": r["reason"],
            "call_id": r["call_id"],
            # ---- 아래는 근거 표시용 추가 필드
            "decision_time": None if decision_dt is None else decision_dt.isoformat(timespec="seconds"),
            "adopted_factors": adopted_factors,
            "rejected_factors": _factor_list(rejected, factor_names, with_reason=True),
            "evidence_scope": scope,                    # adopted: 채택 요인의 근거 / entity: 자산 전체 / none
            "evidence_total": len(chosen),
            "evidence_other_count": len(ev_rows) - len(chosen),
            "evidence": evidence[:int(evidence_limit)],
        })
    rows.sort(key=lambda r: (not r["vetoed"], -abs(r["adj"] or 0.0)))
    return rows[:int(limit)]


def _decisions(store, run_id):
    out = {}
    for r in _rows(store, "SELECT * FROM decision WHERE run_id=?", (run_id,)):
        out[r["variant"]] = {
            "variant": r["variant"],
            "variant_label": VARIANT_LABELS.get(r["variant"], r["variant"]),
            "market_score": _f(r["market_score"]),
            "risk_weight": _f(r["risk_weight"]),
            "record_hash": r["record_hash"],
            "prev_hash": r["prev_hash"],
        }
    return out


# ---------------------------------------------------------------- 전일 대비 변화

def _prev_run(store, run):
    """같은 단계·같은 모드의 직전 판단. 재현과 실시간을 섞지 않는다 (설계 7.4)."""
    return _one(store,
                "SELECT r.* FROM run r WHERE r.stage=? AND r.mode=? "
                "AND (r.as_of < ? OR (r.as_of = ? AND r.run_id < ?)) "
                "AND EXISTS(SELECT 1 FROM decision d WHERE d.run_id=r.run_id) "
                "ORDER BY r.as_of DESC, r.run_id DESC LIMIT 1",
                (run["stage"], run["mode"], run["as_of"], run["as_of"], run["run_id"]))


def _pct(x):
    return None if x is None else round(100.0 * x, 1)


def diff_vs_previous(store, cfg, run, variant, names, top_factors=5):
    """"무엇이 달라졌고 왜" (설계 10.2 ①).

    비중이 얼마나 움직였는가(무엇이) + 점수가 가장 크게 움직인 요인(왜)을 한 쌍으로 준다.
    요인 쪽은 인과를 주장하지 않는다 — 같은 날 사이에 가장 크게 변한 입력이 무엇인지 보일 뿐이다.
    """
    prev = _prev_run(store, run)
    if prev is None:
        return _unavailable("직전 같은 단계의 판단이 아직 없습니다")

    cur_dec = _decisions(store, run["run_id"])
    prev_dec = _decisions(store, prev["run_id"])
    use = variant if variant in cur_dec and variant in prev_dec else "v0"
    cur_d, prev_d = cur_dec.get(use), prev_dec.get(use)

    cur_w = _weight_map(store, run["run_id"], use)
    prev_w = _weight_map(store, prev["run_id"], use)
    cur_tot, prev_tot = _role_totals(cur_w), _role_totals(prev_w)

    weights = []
    for asset in set(cur_w) | set(prev_w):
        a = _f(prev_w[asset]["weight"]) if asset in prev_w else None
        b = _f(cur_w[asset]["weight"]) if asset in cur_w else None
        row = cur_w.get(asset) or prev_w.get(asset)
        delta = (b or 0.0) - (a or 0.0)
        if a is None:
            change = "신규"
        elif b is None:
            change = "제외"
        elif abs(delta) < 1e-9:
            change = "유지"
        else:
            change = "증가" if delta > 0 else "감소"
        weights.append({"asset": asset, "name": _name_of(names, asset),
                        "role": row["role"], "role_label": ROLE_LABELS.get(row["role"], row["role"]),
                        "prev": a, "current": b, "delta": delta, "change": change})
    weights.sort(key=lambda r: -abs(r["delta"]))

    metas = {m["factor_id"]: m for m in factor_meta(cfg)}
    cur_fv = {(r["entity"], r["factor_id"]): r for r in _factor_rows(store, run["run_id"])}
    prev_fv = {(r["entity"], r["factor_id"]): r for r in _factor_rows(store, prev["run_id"])}
    moved = []
    for key, cur_row in cur_fv.items():
        prev_row = prev_fv.get(key)
        if prev_row is None or cur_row["missing"] or prev_row["missing"]:
            continue
        a, b = _f(prev_row["score"]), _f(cur_row["score"])
        if a is None or b is None:
            continue
        entity, fid = key
        meta = metas.get(fid) or {"name": FACTOR_LABELS.get(fid, fid), "layer": None, "weight": None}
        moved.append({"entity": entity,
                      "entity_name": "시장 전체" if entity == MARKET_ENTITY else _name_of(names, entity),
                      "factor_id": fid, "name": meta.get("name"), "layer": meta.get("layer"),
                      "weight": meta.get("weight"), "prev": a, "current": b, "delta": b - a})
    moved.sort(key=lambda r: -abs(r["delta"]))

    changed = [w for w in weights if w["change"] != "유지"]
    parts = []
    if cur_d and prev_d and cur_d["risk_weight"] is not None and prev_d["risk_weight"] is not None:
        parts.append(f"위험자산 {_pct(prev_d['risk_weight'])}% → {_pct(cur_d['risk_weight'])}%")
    added = sum(1 for w in changed if w["change"] == "신규")
    dropped = sum(1 for w in changed if w["change"] == "제외")
    if added or dropped:
        parts.append(f"신규 {added} · 제외 {dropped}")
    if moved:
        top = moved[0]
        parts.append(f"가장 크게 움직인 요인: {top['name']} {top['delta']:+.2f}")
    summary = " · ".join(parts) or "직전 판단과 비중·점수가 거의 같습니다"

    return {
        "available": True,
        "variant": use,
        "prev_run": run_info(prev),
        "market_score": {"prev": (prev_d or {}).get("market_score"),
                         "current": (cur_d or {}).get("market_score")},
        "risk_weight": {"prev": (prev_d or {}).get("risk_weight"),
                        "current": (cur_d or {}).get("risk_weight")},
        "role_totals": [{"role": role, "label": ROLE_LABELS[role], "prev": prev_tot[role],
                         "current": cur_tot[role], "delta": cur_tot[role] - prev_tot[role]}
                        for role in ROLE_ORDER],
        "weights": weights,
        "factors": moved[:int(top_factors)],
        "summary": summary,
    }


def _stage_compare(store, run, names):
    """같은 거래일의 예비·최종 비교 (설계 10.2 ③). 둘 다 있을 때만 값이 찬다.

    최종은 그날 07:40, 예비는 같은 날 18:30 이다. 두 판단은 같은 날짜 열(run.as_of)에 있고
    쓸 수 있었던 정보가 다르다 — 그 차이가 곧 밤사이 정보의 기여를 보는 자리다 (결정 11).
    """
    other = "prelim" if run["stage"] == "final" else "final"
    row = _one(store,
               "SELECT r.* FROM run r WHERE r.as_of=? AND r.mode=? AND r.stage=? "
               "AND EXISTS(SELECT 1 FROM decision d WHERE d.run_id=r.run_id) "
               "ORDER BY r.run_id DESC LIMIT 1",
               (run["as_of"], run["mode"], other))
    if row is None:
        return _unavailable(f"같은 날짜의 {STAGE_LABELS.get(other, other)} 판단이 없습니다")

    variants_a = _variants_of(store, run["run_id"])
    variants_b = _variants_of(store, row["run_id"])
    use = "llm" if "llm" in variants_a and "llm" in variants_b else "v0"
    a_dec, b_dec = _decisions(store, run["run_id"]).get(use), _decisions(store, row["run_id"]).get(use)
    a_w, b_w = _weight_map(store, run["run_id"], use), _weight_map(store, row["run_id"], use)

    rows = []
    for asset in set(a_w) | set(b_w):
        src = a_w.get(asset) or b_w.get(asset)
        cur = _f(a_w[asset]["weight"]) if asset in a_w else None
        oth = _f(b_w[asset]["weight"]) if asset in b_w else None
        rows.append({"asset": asset, "name": _name_of(names, asset), "role": src["role"],
                     "role_label": ROLE_LABELS.get(src["role"], src["role"]),
                     "current": cur, "other": oth,
                     "delta": (cur or 0.0) - (oth or 0.0)})
    rows.sort(key=lambda r: -abs(r["delta"]))
    return {
        "available": True,
        "variant": use,
        "current": {"run": run_info(run), "decision": a_dec},
        "other": {"run": run_info(row), "decision": b_dec},
        "weights": rows,
    }


# ---------------------------------------------------------------- 엔드포인트별 조립

def report(store, cfg, date=None, stage=None, mode="live", top_n=20, top_factors=5):
    """GET /advisor/report — 그날 판단 전체 (설계 10.1)."""
    run = resolve_run(store, date, stage, mode)
    if run is None:
        return _unavailable("아직 실행 기록이 없습니다", mode=mode,
                            dates=available_dates(store, mode))

    names = names_at(store, run["as_of"])
    variants = _variants_of(store, run["run_id"])
    decisions = _decisions(store, run["run_id"])
    primary = "llm" if "llm" in variants else ("v0" if "v0" in variants else None)

    weight_rows = _weight_table(store, run["run_id"], names, variants or ["v0"])
    totals = {v: _role_totals(_weight_map(store, run["run_id"], v)) for v in (variants or ["v0"])}

    out = {
        "available": True,
        "mode": mode,
        "date": run["as_of"],
        "stage": run["stage"],
        "run": run_info(run),
        "dates": available_dates(store, mode),
        "variants": variants,
        "primary_variant": primary,
        "llm_note": None if "llm" in variants else "LLM 조정 없음 (v0와 동일)",
        "decisions": [decisions[v] for v in variants if v in decisions],
        "market": {
            "score": (decisions.get(primary) or {}).get("market_score"),
            "score_v0": (decisions.get("v0") or {}).get("market_score"),
            "score_llm": (decisions.get("llm") or {}).get("market_score"),
            "risk_weight": (decisions.get(primary) or {}).get("risk_weight"),
            "factors": market_breakdown(store, cfg, run["run_id"]),
        },
        "allocation": {
            "role_totals": [
                {"role": role, "label": ROLE_LABELS[role],
                 **{f"weight_{v}": totals.get(v, {}).get(role) for v in (variants or ["v0"])}}
                for role in ROLE_ORDER],
            "weights": weight_rows,
        },
        "sectors": _score_table(store, cfg, run, "sector", names),
        "stocks": _score_table(store, cfg, run, "stock", names, top_n=top_n),
        "risk_flags": [
            {"entity": entity, "name": _name_of(names, entity), "flags": flags}
            for entity, flags in sorted(_flags_by_entity(store, run["run_id"]).items())],
        "llm_adjustments": llm_adjustments(store, run["run_id"], names, cfg=cfg, run=run),
        "diff": diff_vs_previous(store, cfg, run, primary or "v0", names, top_factors),
        "stage_compare": _stage_compare(store, run, names),
        "replay": str(mode) != "live",
        "replay_note": "재현 모드 — 성과 주장 아님" if str(mode) != "live" else None,
    }
    if not variants:
        out["available"] = bool(run)
        out["empty_reason"] = ("판단이 남지 않은 실행입니다 "
                               f"(상태: {run_info(run)['status_label']})")
    return out


def scores(store, cfg, date=None, layer=None, stage=None, mode="live", top_n=None):
    """GET /advisor/scores — 계층별 점수표 (요인별 원본 값·점수·결측)."""
    run = resolve_run(store, date, stage, mode)
    if run is None:
        return _unavailable("아직 실행 기록이 없습니다", mode=mode)
    names = names_at(store, run["as_of"])
    layers = [layer] if layer in LAYER_LABELS else list(LAYER_LABELS)
    return {
        "available": True,
        "mode": mode,
        "date": run["as_of"],
        "stage": run["stage"],
        "run": run_info(run),
        "layers": [_score_table(store, cfg, run, lay, names,
                                top_n=top_n if lay == "stock" else None)
                   for lay in layers],
    }


def asset(store, cfg, code, date=None, stage=None, mode="live", evidence_limit=20):
    """GET /advisor/asset/{code} — 한 자산의 점수 분해·근거·위험 표시·LLM 조정."""
    run = resolve_run(store, date, stage, mode)
    if run is None:
        return _unavailable("아직 실행 기록이 없습니다", code=code, mode=mode)
    run_id = run["run_id"]
    names = names_at(store, run["as_of"])
    meta = names.get(code) or {}

    comp = {r["variant"]: r for r in _rows(
        store, "SELECT * FROM composite WHERE run_id=? AND entity=?", (run_id, code))}
    layer = next((r["layer"] for r in comp.values() if r["layer"]), None)
    if layer is None:
        layer = "market" if code == MARKET_ENTITY else ("sector" if code in {
            m.get("sector") for m in names.values()} else "stock")

    values = {r["factor_id"]: r for r in _factor_rows(store, run_id, code)}
    if not comp and not values:
        return _unavailable(f"{code} 의 판단 기록이 그날에 없습니다", code=code,
                            date=run["as_of"], stage=run["stage"], mode=mode, run=run_info(run))

    breakdown = _breakdown(factor_meta(cfg, layer), values)
    layer_score = _layer_score_of(breakdown)

    # 종목 종합 점수 = 종목 계층 점수 + sector_tilt × 소속 섹터 점수 (설계 5.3).
    # composite.base_score 에는 이미 기울기 항이 들어 있으므로 여기서 다시 더하지 않는다 —
    # 화면은 "요인 기여의 합 + 섹터 기울기 = 종합 점수"로 읽는다.
    tilt = _f(((cfg or {}).get("combine") or {}).get("sector_tilt"))
    sector = meta.get("sector")
    sector_row = _one(store, "SELECT * FROM composite WHERE run_id=? AND variant='v0' AND entity=?",
                      (run_id, sector)) if sector else None
    sector_part = None
    if layer == "stock" and tilt is not None and sector_row is not None:
        sector_score = _f(sector_row["final_score"])
        sector_part = {"sector": sector, "score": sector_score, "tilt": tilt,
                       "contribution": None if sector_score is None else tilt * sector_score}

    evidence = [{"factor_id": r["factor_id"], "ref_type": r["ref_type"], "ref_id": r["ref_id"],
                 "summary": r["summary"]}
                for r in _rows(store, "SELECT * FROM factor_evidence WHERE run_id=? AND entity=? "
                                      "ORDER BY factor_id LIMIT ?",
                               (run_id, code, int(evidence_limit)))]
    for item in evidence:
        if item["ref_type"] == "disclosure" and item["ref_id"]:
            row = _one(store, "SELECT * FROM disclosure WHERE rcept_no=?", (item["ref_id"],))
            if row is not None:
                item["disclosure"] = {"report_nm": row["report_nm"], "kind": row["kind"],
                                      "rcept_dt": row["rcept_dt"], "ratio": _f(row["ratio"]),
                                      "first_seen_at": row["first_seen_at"],
                                      "first_seen_src": row["first_seen_src"]}

    llm = comp.get("llm")
    weights = {}
    for v in VARIANTS:
        row = _one(store, "SELECT * FROM target_weight WHERE run_id=? AND variant=? AND asset=?",
                   (run_id, v, code))
        weights[v] = None if row is None else {"role": row["role"],
                                               "role_label": ROLE_LABELS.get(row["role"], row["role"]),
                                               "weight": _f(row["weight"])}

    return {
        "available": True,
        "mode": mode,
        "date": run["as_of"],
        "stage": run["stage"],
        "run": run_info(run),
        "code": code,
        "name": meta.get("name") or ("시장 전체" if code == MARKET_ENTITY else code),
        "kind": meta.get("kind"),
        "sector": sector,
        "layer": layer,
        "layer_label": LAYER_LABELS.get(layer, layer),
        "factors": breakdown,
        "layer_score": layer_score,
        "base_score": None if "v0" not in comp else _f(comp["v0"]["base_score"]),
        "sector_part": sector_part,
        "score_v0": None if "v0" not in comp else _f(comp["v0"]["final_score"]),
        "score_llm": None if llm is None else _f(llm["final_score"]),
        "llm": None if llm is None else {
            "adj": _f(llm["adj"]),
            "vetoed": bool(llm["vetoed"]),
            "adopted": _json(llm["adopted_json"]),
            "rejected": _json(llm["rejected_json"]),
            "reason": llm["reason"],
            "call_id": llm["call_id"],
        },
        "llm_note": None if llm is not None else "LLM 조정 없음 (v0와 동일)",
        "flags": _flags_by_entity(store, run_id).get(code, []),
        "evidence": evidence,
        "weights": weights,
    }


# ---------------------------------------------------------------- 성과

def _summarise(dates, navs, turnovers, costs):
    """NAV 시계열 → 수익률·연율 변동성·최대 낙폭·회전율·비용 (설계 10.1 의 /performance).

    portfolio.py 의 요약과 같은 정의다. 리포트가 그 모듈을 import 하지 않는 이유는, 배치 쪽
    코드가 없거나 바뀌는 중이어도 화면은 DB 만으로 떠야 하기 때문이다.
    """
    if not navs:
        return {"n_days": 0, "nav": START_NAV, "total_return": 0.0, "ann_vol": None,
                "max_drawdown": 0.0, "turnover": 0.0, "cost": 0.0,
                "start_date": None, "end_date": None}
    series = [START_NAV] + list(navs)
    rets = [series[i] / series[i - 1] - 1.0 for i in range(1, len(series)) if series[i - 1]]
    vol = None
    if len(rets) >= 2:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        vol = math.sqrt(var) * math.sqrt(float(TRADING_DAYS_PER_YEAR))
    peak, mdd = series[0], 0.0
    for v in series:
        peak = max(peak, v)
        if peak:
            mdd = min(mdd, v / peak - 1.0)
    return {"n_days": len(navs), "nav": series[-1], "total_return": series[-1] / START_NAV - 1.0,
            "ann_vol": vol, "max_drawdown": mdd, "turnover": float(sum(turnovers)),
            "cost": float(sum(costs)), "start_date": dates[0], "end_date": dates[-1]}


def _portfolio_label(portfolio_id):
    base = portfolio_id[:-len(REPLAY_SUFFIX)] if portfolio_id.endswith(REPLAY_SUFFIX) else portfolio_id
    label, kind = PORTFOLIO_LABELS.get(base, (base, "baseline" if base.startswith("bl_") else "system"))
    return base, label, kind


def performance(store, cfg=None, mode="live"):
    """GET /advisor/performance — 포트폴리오별 NAV 시계열과 요약 지표.

    재현 모드 기록은 id 의 `@replay` 꼬리표로 갈라 담고 실시간 곡선과 절대 잇지 않는다 (설계 7.4).
    """
    rows = _rows(store, "SELECT portfolio_id, date, nav, turnover, cost FROM nav "
                        "ORDER BY portfolio_id, date")
    if not rows:
        return _unavailable("아직 NAV 기록이 없습니다", mode=mode, portfolios=[], dates=[])

    want_replay = str(mode) != "live"
    series = {}
    other_mode = False
    for r in rows:
        pid = r["portfolio_id"]
        is_replay = pid.endswith(REPLAY_SUFFIX)
        if is_replay != want_replay:
            other_mode = True
            continue
        series.setdefault(pid, []).append(r)

    if not series:
        return _unavailable(
            "이 모드의 NAV 기록이 아직 없습니다",
            mode=mode, portfolios=[], dates=[], other_mode_available=other_mode)

    out = []
    all_dates = set()
    for pid, items in series.items():
        base, label, kind = _portfolio_label(pid)
        dates = [i["date"] for i in items]
        navs = [_f(i["nav"]) or 0.0 for i in items]
        summary = _summarise(dates, navs,
                             [_f(i["turnover"]) or 0.0 for i in items],
                             [_f(i["cost"]) or 0.0 for i in items])
        all_dates |= set(dates)
        out.append({
            "portfolio_id": pid,
            "base_id": base,
            "label": label,
            "kind": kind,
            "mode": "replay" if pid.endswith(REPLAY_SUFFIX) else "live",
            "points": [{"date": d, "nav": n} for d, n in zip(dates, navs)],
            "summary": summary,
        })
    order = list(PORTFOLIO_LABELS)
    out.sort(key=lambda s: (order.index(s["base_id"]) if s["base_id"] in order else 99,
                            s["portfolio_id"]))
    return {
        "available": True,
        "mode": mode,
        "replay": want_replay,
        "replay_note": "재현 모드 — 성과 주장 아님" if want_replay else None,
        "other_mode_available": other_mode,
        "dates": sorted(all_dates),
        "portfolios": out,
        "costs": (cfg or {}).get("costs"),
    }


# ---------------------------------------------------------------- 요인 성적표

def metrics(store, cfg=None, mode="live"):
    """GET /advisor/metrics — 요인 지표 (단계·판단 버전별).

    같은 (단계, 버전, 요인, 기간) 조합은 가장 최근에 계산된 행만 본다. `n_eff` 가 설정의
    `reeval.min_n_eff` 보다 작으면 화면에 "판단 불가"로 둔다 (설계 7.2) — 표본이 모자란 지표를
    숫자로 보여 주면 그것부터 결론처럼 읽힌다.
    """
    rows = _rows(store,
                 "SELECT m.* FROM factor_metric m JOIN (SELECT stage, variant, factor_id, horizon, "
                 "  MAX(computed_at) AS c FROM factor_metric GROUP BY stage, variant, factor_id, horizon) x "
                 " ON x.stage IS m.stage AND x.variant IS m.variant AND x.factor_id IS m.factor_id "
                 "AND x.horizon IS m.horizon AND x.c IS m.computed_at "
                 "ORDER BY m.factor_id, m.stage, m.variant, m.horizon")
    min_n_eff = _f(((cfg or {}).get("reeval") or {}).get("min_n_eff")) or 0.0
    metas = {m["factor_id"]: m for m in factor_meta(cfg or {})}

    items = []
    for r in rows:
        n_eff = _f(r["n_eff"])
        meta = metas.get(r["factor_id"]) or {}
        judgable = n_eff is not None and n_eff >= min_n_eff
        items.append({
            "factor_id": r["factor_id"],
            "name": meta.get("name") or FACTOR_LABELS.get(r["factor_id"], r["factor_id"]),
            "layer": meta.get("layer"),
            "weight": meta.get("weight"),
            "source": meta.get("source"),
            "stage": r["stage"],
            "stage_label": STAGE_LABELS.get(r["stage"], r["stage"]),
            "variant": r["variant"],
            "variant_label": VARIANT_LABELS.get(r["variant"], r["variant"]),
            "horizon": r["horizon"],
            "n_days": r["n_days"],
            "n_eff": n_eff,
            "rank_ic_mean": _f(r["rank_ic_mean"]),
            "rank_ic_std": _f(r["rank_ic_std"]),
            "hit_rate": _f(r["hit_rate"]),
            "buckets": _json(r["bucket_json"]),
            "judgable": judgable,
            "verdict": None if judgable else "판단 불가",
            "computed_at": r["computed_at"],
        })
    if not items:
        return _unavailable("아직 요인 지표가 없습니다", mode=mode, min_n_eff=min_n_eff,
                            factors=[m for m in factor_meta(cfg or {})])
    return {
        "available": True,
        "mode": mode,
        "min_n_eff": min_n_eff,
        "note": ("재현 모드 기록은 요인 지표 집계에서 제외된다 (설계 7.4) — 아래 값은 실시간 기록으로 계산된 것이다."
                 if str(mode) != "live" else None),
        "metrics": items,
        "factors": factor_meta(cfg or {}),
    }


# ---------------------------------------------------------------- 상태 (DB 쪽)

def db_status(store, cfg=None, mode="live"):
    """GET /advisor/status 의 DB 부분. 스케줄러·폴러 상태는 서비스가 덧붙인다."""
    runs = {}
    for stage in STAGES:
        row = _one(store, "SELECT * FROM run WHERE stage=? AND mode=? "
                          "ORDER BY as_of DESC, run_id DESC LIMIT 1", (stage, str(mode)))
        runs[stage] = run_info(row)
    today = _today()
    cost = _one(store, "SELECT COUNT(*) AS n, SUM(COALESCE(cost_usd,0)) AS usd, "
                       "SUM(CASE WHEN cache_hit THEN 1 ELSE 0 END) AS hits "
                       "FROM llm_call WHERE created_at LIKE ?", (f"{today}%",))
    dates = available_dates(store, mode)
    counts = _one(store, "SELECT (SELECT COUNT(*) FROM run) AS runs, "
                         "(SELECT COUNT(*) FROM decision) AS decisions, "
                         "(SELECT COUNT(*) FROM nav) AS nav_rows")
    final_today = _one(store, "SELECT * FROM run WHERE stage='final' AND mode='live' AND as_of=? "
                              "ORDER BY run_id DESC LIMIT 1", (today,))
    return {
        "available": True,
        "mode": mode,
        "today": today,
        "runs": runs,
        "final_today": run_info(final_today),
        "dates": dates,
        "latest_date": dates[0]["date"] if dates else None,
        "llm_today": {
            "calls": (cost["n"] if cost else 0) or 0,
            "cost_usd": _f(cost["usd"]) if cost else None,
            "cache_hits": (cost["hits"] if cost else 0) or 0,
            "budget_usd": _f(((cfg or {}).get("llm") or {}).get("daily_budget_usd")),
        },
        "counts": {"runs": (counts["runs"] if counts else 0) or 0,
                   "decisions": (counts["decisions"] if counts else 0) or 0,
                   "nav_rows": (counts["nav_rows"] if counts else 0) or 0},
    }


__all__ = ["MARKET_ENTITY", "REPLAY_SUFFIX", "LAYER_LABELS", "ROLE_LABELS", "ROLE_ORDER",
           "STAGE_LABELS", "VARIANT_LABELS", "FLAG_LABELS", "FACTOR_LABELS", "PORTFOLIO_LABELS",
           "available_dates", "runs_on", "resolve_run", "run_info", "parse_note", "names_at",
           "factor_meta",
           "market_breakdown", "llm_adjustments", "diff_vs_previous", "report", "scores", "asset",
           "performance",
           "metrics", "db_status"]
