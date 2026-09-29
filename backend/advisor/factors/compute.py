"""한 실행의 요인 표를 통째로 만든다 (설계 4장의 3번). 코드 요인 8개(+관찰 요인 stk_earn_growth) + LLM 요인 자리.

이 모듈의 계약은 **"표의 모양이 실행마다 같다"** 이다.
설정에 있는 모든 요인 × 그 계층의 모든 대상에 대해 행이 하나씩 나온다. 값을 못 구했으면
`missing=1` 행이 나오고, 예외는 밖으로 나가지 않는다. 이유는 두 가지다.

1. 자료가 모자란 요인 하나 때문에 그날 판단 전체가 없어지면 안 된다 (결정 4: LLM이 없어도,
   나아가 소스 하나가 비어도 판단은 나온다).
2. 사후 재평가(결정 6)와 채점은 `factor_value` 를 그대로 읽는다. 어떤 날은 행이 있고 어떤 날은
   아예 없으면 "그날 요인이 없었다"와 "값이 없었다"가 구분되지 않아 유효 표본이 틀어진다.

LLM 요인(`stk_disclosure`, `news_risk`)도 v0 에서는 **결측 행**으로 자리를 잡아 둔다.
나중에 LLM 모듈이 같은 (run_id, entity, factor_id) 로 `put_factor_values` 를 다시 부르면
INSERT OR REPLACE 로 그 행만 덮어쓴다 — 표의 모양은 그대로 두고 값만 채우는 구조다.
"""
import logging

from . import asof, market, sector, stock
from .asof import universe_snapshot          # run.py·risk_flags 가 같은 스냅샷을 쓰도록 재수출

log = logging.getLogger("advisor.factors")

# 요인 id → 계산 함수. 여기에 없는 코드 요인은 없다 (설정에만 있고 함수가 없으면 결측 행이 나간다).
CODE_FACTORS = {
    "mkt_trend": market.mkt_trend,
    "mkt_vol": market.mkt_vol,
    "mkt_overnight": market.mkt_overnight,
    "mkt_credit": market.mkt_credit,
    "sec_flow": sector.sec_flow,
    "sec_trend": sector.sec_trend,
    "stk_high52": stock.stk_high52,
    "stk_flow": stock.stk_flow,
    "stk_earn_growth": stock.stk_earn_growth,     # 2026-09-28 관찰 요인 (가중치 0, 결정 6)
}

MARKET = market.MARKET


def entities_for(layer, uni):
    """계층별 대상 목록. 시장은 항상 'MARKET' 하나, 섹터는 이름, 종목은 6자리 코드다."""
    if layer == "market":
        return [MARKET]
    if layer == "sector":
        return list((uni or {}).get("sectors") or [])
    return sorted((uni or {}).get("stocks") or {})


def missing_rows(factor_id, entities):
    """결측 행. 값이 없다는 사실도 기록이다 (결정 9의 공통 규칙 2)."""
    return [{"entity": e, "factor_id": factor_id, "raw_value": None, "score": 0.0, "missing": 1}
            for e in entities]


def _rows(factor_id, values):
    return [{"entity": e, "factor_id": factor_id, "raw_value": raw, "score": float(score),
             "missing": 1 if missing else 0}
            for e, (raw, score, missing) in values.items()]


def compute_all(store, cfg, specs, cal, as_of, stage, uni=None):
    """[{entity, factor_id, raw_value, score, missing}] — 그날 요인 표 전체.

    uni 를 주면 그 대상 목록 스냅샷을 쓴다 (run.py 가 위험 표시·비중 계산과 같은 스냅샷을
    공유하기 위해 한 번만 읽고 넘긴다).
    """
    uni = universe_snapshot(store, cfg, as_of) if uni is None else uni
    rows = []
    for fid, spec in specs.items():
        entities = entities_for(spec.layer, uni)
        fn = CODE_FACTORS.get(fid)
        if fn is None:
            # LLM 요인이거나 아직 구현이 없는 요인 — 자리만 잡아 둔다 (나중에 덮어쓴다)
            rows += missing_rows(fid, entities)
            continue
        if not spec.applies_to(stage):
            # 단계 제한이 걸린 요인. mkt_overnight 이 prelim 에서 결측인 것이 실험 설계다 (결정 11)
            rows += missing_rows(fid, entities)
            continue
        try:
            values = fn(store, cfg, spec, as_of, cal, uni)
        except asof.AsOfViolation:
            raise                                  # 시점 규칙 위반만은 삼키지 않는다
        except Exception as exc:                   # 자료 부족·손상은 그 요인만 결측으로 두고 계속한다
            log.warning("요인 %s 계산 실패 → 결측 처리: %s", fid, exc)
            rows += missing_rows(fid, entities)
            continue
        got = _rows(fid, values)
        seen = {r["entity"] for r in got}
        rows += got + missing_rows(fid, [e for e in entities if e not in seen])
    return rows


def scores_by_entity(rows):
    """요인 행 → {entity: {factor_id: (score, missing)}}. combine 이 바로 받는 형태다."""
    out = {}
    for r in rows:
        out.setdefault(r["entity"], {})[r["factor_id"]] = (float(r["score"]), bool(r["missing"]))
    return out


__all__ = ["CODE_FACTORS", "MARKET", "compute_all", "entities_for", "missing_rows",
           "scores_by_entity", "universe_snapshot"]
