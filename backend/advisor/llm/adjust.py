"""종합 점수 조정과 거부권 (설계 5.6, 결정 4). LLM 에게 주는 권한은 딱 두 가지다.

  1. 종합 점수를 `llm.adj_cap`(v0 ±0.2) 안에서 움직인다.
  2. **위험 표시가 붙은 자산만** 후보에서 뺀다.

기본 점수가 제외한 종목을 새로 넣을 권한은 없다 (결정 4). 후보 범위는 검증 가능한 코드 규칙이
정하고 LLM 은 그 안에서만 움직인다 — 자율 판단의 레짐 의존적 실패를 막는 장치다.

호출은 자산 여러 개를 한 번에 묶어 **1~2회로 끝낸다**. 자산마다 부르면 비용도 비용이지만
"같은 날 같은 시장인데 자산마다 다른 전제로 판단"하는 일이 생긴다.

실패·예산 초과는 조정 없음(기권)이다. 그날 LLM 판단은 v0 와 같아지고, `status` 로 그 사실이
기록된다 — **중립 판단과 구분되어야 "LLM 이 도움이 됐는가"(결정 4) 비교가 오염되지 않는다.**
묶음 하나만 실패했으면 기권이 아니라 **PARTIAL** 이다 (`_status` 참고).
"""
import logging
from dataclasses import dataclass, field

from ..factors.normalize import clip
from . import prompts, schemas
from .client import STATUS_DISABLED, STATUS_OK, clip_text, tunable

log = logging.getLogger("advisor.llm.adjust")


@dataclass
class Adjustment:
    """자산 하나의 조정 결과. `composite` 테이블의 adj·vetoed·adopted_json·rejected_json 과 짝이 맞는다."""

    code: str
    adj: float = 0.0
    vetoed: bool = False
    adopted: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    reason: str = ""
    call_id: str = None


# ---------------------------------------------------------------- 후보 고르기

def select_assets(cfg, v0_composites, sector_scores=None, flags=None, holdings=None,
                  sector_etf=None, names=None):
    """조정 대상 [{code, entity, label, composite, held}] (설계 5.6의 대상 정의).

    대상 = v0 종합 점수 상위 top_n 종목 + 섹터 ETF 후보 + 위험 표시가 붙은 보유 종목.
    섹터 ETF 의 점수·근거는 **섹터 이름**으로 저장돼 있으므로 표를 만들 때 entity 를 따로 든다.
    """
    top_n = int((cfg.get("llm") or {}).get("top_n") or 20)
    flags = flags or {}
    held = set(holdings or ())
    out, seen = [], set()

    def add(code, entity, label, composite):
        if not code or code in seen:
            return
        seen.add(code)
        out.append({"code": code, "entity": entity, "label": label, "composite": composite,
                    "held": code in held, "flags": list(flags.get(code) or [])})

    ranked = sorted(((c, s) for c, s in (v0_composites or {}).items() if s is not None),
                    key=lambda cs: cs[1], reverse=True)
    for code, score in ranked[:top_n]:
        add(code, code, (names or {}).get(code, ""), score)
    for sector, etf in (sector_etf or {}).items():
        add(etf, sector, f"{sector} ETF", (sector_scores or {}).get(sector))
    for code in sorted(held):                        # 위험 표시가 붙은 보유 종목은 순위 밖이어도 본다
        if flags.get(code):
            add(code, code, (names or {}).get(code, ""), (v0_composites or {}).get(code))
    return out


def attach_context(store, cfg, run_id, assets):
    """자산마다 하위 점수와 최근 근거(공시·뉴스 요약)를 붙인다. 전부 저장소에서 읽는다.

    프롬프트에 넣을 값을 호출부가 따로 모아 넘기게 하면 "리포트에 보이는 근거"와 "모델이 본 근거"가
    갈라진다. 같은 `factor_value`·`factor_evidence` 를 읽는 것이 둘을 같게 유지하는 가장 싼 방법이다.
    """
    ev_max = int(tunable(cfg, "llm.evidence_max_chars"))
    entities = {a["entity"] for a in assets}
    subs, evid = {}, {}
    try:
        for r in store.conn.execute("SELECT * FROM factor_value WHERE run_id=?", (run_id,)):
            if r["entity"] in entities:
                subs.setdefault(r["entity"], []).append(
                    {"factor_id": r["factor_id"], "score": r["score"], "raw_value": r["raw_value"],
                     "missing": bool(r["missing"])})
        for r in store.conn.execute("SELECT * FROM factor_evidence WHERE run_id=?", (run_id,)):
            if r["entity"] in entities:
                evid.setdefault(r["entity"], []).append(clip_text(r["summary"], ev_max))
    except Exception as exc:                          # 표가 비어 있어도 조정 자체는 돌 수 있다
        log.warning("조정 입력 조회 실패: %s", exc)
    for a in assets:
        a["factors"] = subs.get(a["entity"], [])
        a["evidence"] = evid.get(a["entity"], [])[:3]
    return assets


def market_context(store, run_id, market_score=None, entity="MARKET"):
    """시장 점수와 그 구성 (설계 5.6: 시장 전체 맥락도 함께 준다)."""
    factors = []
    try:
        for r in store.conn.execute(
                "SELECT * FROM factor_value WHERE run_id=? AND entity=? ORDER BY factor_id",
                (run_id, entity)):
            factors.append({"factor_id": r["factor_id"], "score": r["score"],
                            "raw_value": r["raw_value"], "missing": bool(r["missing"])})
    except Exception as exc:
        log.warning("시장 맥락 조회 실패: %s", exc)
    return {"score": market_score, "factors": factors}


# ---------------------------------------------------------------- 호출·후처리

def _normalise_ctx(market_ctx):
    """호출부가 어떤 모양으로 주든 프롬프트가 아는 모양으로 맞춘다 (score, factors[], note)."""
    ctx = dict(market_ctx or {})
    if "score" not in ctx:
        ctx["score"] = ctx.get("market_score")
    factors = ctx.get("factors")
    if isinstance(factors, dict):                     # {factor_id: {...}} 로 줘도 받는다
        ctx["factors"] = [dict(v, factor_id=k) for k, v in factors.items()]
    elif factors is None:
        ctx["factors"] = []
    return ctx


def postprocess(records, assets, cfg, flags=None, call_id=None):
    """모델 출력 → {code: Adjustment}. 세 가지를 코드가 다시 강제한다 (설계 5.6).

      - 모르는 code 는 버린다 (후보 밖 자산을 만들어 낼 수 없다).
      - adj 는 ±adj_cap 으로 다시 자른다 (스키마를 지나쳐 온 값도 있다).
      - 위험 표시가 없는 자산의 veto 는 무시한다.
    """
    cap = abs(float((cfg.get("llm") or {}).get("adj_cap") or 0.2))
    known = {a["code"] for a in assets}
    flags = flags or {}
    out = {}
    for rec in records or ():
        code = str(rec.get("code") or "")
        if code not in known:
            log.warning("조정에서 모르는 코드를 버린다: %s", clip_text(code, 20))
            continue
        try:
            adj = clip(float(rec.get("adj") or 0.0), -cap, cap)
        except (TypeError, ValueError):
            adj = 0.0
        veto = bool(rec.get("veto"))
        if veto and not flags.get(code):
            log.info("위험 표시가 없는 %s 의 거부는 무시한다 (결정 4)", code)
            veto = False
        rejected = [{"factor_id": r.get("factor_id"), "reason": clip_text(r.get("reason"), 120)}
                    for r in (rec.get("rejected") or ()) if r.get("factor_id")]
        out[code] = Adjustment(code, adj, veto, list(rec.get("adopted") or ()), rejected,
                               clip_text(rec.get("reason"), 200), call_id)
    return out


def run_adjust(store, cfg, llm, v0_composites, sector_scores=None, market_ctx=None, flags=None,
               holdings=None, run_id=None, sector_etf=None, names=None):
    """조정 한 판. ({code: Adjustment}, status) 를 준다. 예외를 내보내지 않는다."""
    if sector_etf is None:
        sector_etf = dict((cfg.get("universe") or {}).get("sector_etfs") or {})
    assets = select_assets(cfg, v0_composites, sector_scores, flags, holdings, sector_etf, names)
    if not assets:
        return {}, "NO_ASSETS"
    if llm is None:
        return {}, STATUS_DISABLED
    attach_context(store, cfg, run_id, assets)
    ctx = _normalise_ctx(market_ctx if market_ctx is not None else market_context(store, run_id))

    cap = abs(float((cfg.get("llm") or {}).get("adj_cap") or 0.2))
    per_call = max(1, int(tunable(cfg, "llm.adjust_max_assets_per_call")))
    max_calls = max(1, int(tunable(cfg, "llm.adjust_max_calls")))
    factor_ids = list((cfg.get("factors") or {}).keys())
    system = prompts.adjust_system(cap, llm.prompt_ver)

    chunks = [assets[i:i + per_call] for i in range(0, len(assets), per_call)][:max_calls]
    if len(assets) > per_call * max_calls:
        log.warning("조정 대상 %d개 중 %d개만 본다 (호출 %d회 상한, 설계 5.6)",
                    len(assets), per_call * max_calls, max_calls)
    out, failed = {}, []
    for chunk in chunks:
        user = prompts.adjust_user(ctx, chunk, llm.prompt_ver)
        schema = schemas.adjust_schema([a["code"] for a in chunk], factor_ids, cap)
        res = llm.call(schemas.TASK_ADJUST, system, user, schema, "adjust")
        if not res.ok:
            failed.append(res.status)
            continue                                  # 이 묶음만 기권한다 (다른 묶음은 살린다)
        out.update(postprocess(res.data, chunk, cfg, flags, res.call_id))
    status = _status(out, failed)
    log.info("LLM 조정: 대상 %d개 → 조정 %d건 (status=%s%s)", len(assets), len(out), status,
             (" 실패 " + ",".join(failed)) if failed else "")
    return out, status


def _status(out, failed):
    """묶음별 결과 → 한 덩어리 상태 (설계 5.6의 기권 판정).

    **일부만 실패했으면 PARTIAL 이다.** 호출부(`advisor/hooks.py`)는 OK·PARTIAL 만 조정으로
    받아들이므로, 한 묶음이 실패했다고 상태를 ERROR 로 올리면 성공한 묶음의 조정까지 통째로
    버려진다 — "이 묶음만 기권한다(다른 묶음은 살린다)"는 위 반복문의 뜻과 어긋난다.
    (2026-09-21 실행에서 실제로 그랬다: 첫 묶음이 400 으로 죽자 둘째 묶음의 조정 7건이 사라졌다.)
    전부 실패했으면 **첫 실패 사유를 그대로** 올린다 — 예산 초과와 호출 실패는 구분돼야 한다.
    한 건도 못 받았지만 실패도 없었으면 "움직일 이유가 없었다"(EMPTY)이고 조정은 없다.
    """
    if failed:
        return "PARTIAL" if out else failed[0]
    return STATUS_OK if out else "EMPTY"


__all__ = ["Adjustment", "attach_context", "market_context", "postprocess", "run_adjust",
           "select_assets"]
