"""섹터 계층 코드 요인 (설계 5.2, 결정 12). entity 는 **섹터 이름**(예: '반도체')이다.

코드가 아니라 이름을 entity 로 쓰는 이유는, 섹터를 대표하는 ETF 가 바뀌거나(운용사 변경·상장폐지)
설정에서 다른 종목으로 교체돼도 **같은 섹터의 점수 이력이 끊기지 않아야** 채점이 이어지기
때문이다 (결정 5의 표본 수 문제). 담을 ETF 코드는 비중 계산이 설정에서 다시 찾는다.

섹터 구성 종목은 섹터 ETF 의 구성 종목으로 대신한다 (결정 8) — 수집 쪽이 universe.sector 에
적어 둔 값을 그대로 읽는다.
"""
from . import asof
from . import stock as stock_factors
from .normalize import rank_scores, rule_score


def sec_flow(store, cfg, spec, as_of, cal=None, uni=None):
    """섹터 구성 종목의 (외국인+기관 순매수 20일 합) 합 ÷ 시가총액 합 → 섹터 간 순위 (부호 +).

    종목별 비율의 평균이 아니라 **합 ÷ 합**이다. 평균으로 내면 시가총액이 작은 구성 종목 하나의
    극단값이 섹터 점수를 끌고 간다 — 순위 변환이 종목 수준에서 해 주던 극단값 방어가 섹터
    수준에서는 없기 때문이다. 합 ÷ 합은 "그 섹터에 돈이 얼마나 들어왔는가"의 정의에도 맞는다.

    시가총액을 모르거나 관측일이 모자란 구성 종목은 분자·분모에서 **함께** 뺀다. 한쪽에만
    남기면 비율이 그 종목 때문에 커지거나 작아진다.
    """
    sectors = (uni or {}).get("sectors") or []
    members = (uni or {}).get("members") or {}
    if not sectors:
        return {}
    days = int((spec.params or {}).get("days") or 20)
    cutoff = asof.last_kr_date(as_of, cfg)
    start = cal.add_trading_days(cutoff, -(days - 1)) if cal is not None else cutoff
    sums = asof.flow_window(store, start, cutoff)
    caps = asof.latest_mktcaps(store, cutoff)

    raws = {}
    for sector in sectors:
        codes = members.get(sector) or []
        per_code = stock_factors.flow_raw(store, cfg, days, cutoff, cal, codes,
                                          window_sums=sums, mktcaps=caps)
        net_sum = cap_sum = 0.0
        used = 0
        for code, ratio in per_code.items():
            if ratio is None:                     # 시가총액이 없거나 관측일이 모자란 구성 종목
                continue
            net_sum += float(sums.get(code, (0.0, 0))[0] or 0.0)
            cap_sum += caps[code]
            used += 1
        raws[sector] = (net_sum / cap_sum) if used and cap_sum > 0 else None

    scores = rank_scores(raws, spec.sign)
    return {s: (raws[s], scores[s][0], scores[s][1]) for s in raws}


def sec_trend(store, cfg, spec, as_of, cal=None, uni=None):
    """섹터 ETF 종가 > 자신의 200일 이동평균 (규칙형, 부호 +).

    추세 신호는 개별 종목보다 지수·ETF 단위에서 맞는다는 조사 근거(결정 2)를 섹터에 그대로
    적용한 것이다. 원본 값은 종가 ÷ 이동평균 (mkt_trend 와 같은 이유).
    """
    sectors = (uni or {}).get("sectors") or []
    etf_of = (uni or {}).get("sector_etf") or {}
    if not sectors:
        return {}
    ma_days = int((spec.params or {}).get("ma_days") or 200)
    cutoff = asof.last_kr_date(as_of, cfg)

    out = {}
    for sector in sectors:
        etf = etf_of.get(sector)
        raw = cond = None
        if etf:
            bars = asof.price_bars(store, etf, cutoff, ma_days)
            vals = [v for v in (stock_factors.adj_close(b) for b in bars) if v is not None and v > 0]
            if len(vals) >= ma_days:
                ma = sum(vals) / len(vals)
                if ma > 0:
                    raw, cond = vals[-1] / ma, vals[-1] > ma
        score, missing = rule_score(cond, spec.sign)
        out[sector] = (raw, score, missing)
    return out


__all__ = ["sec_flow", "sec_trend"]
