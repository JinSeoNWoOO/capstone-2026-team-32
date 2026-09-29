"""계층 점수와 종합 점수 (설계 5.3). 하위 점수를 더해 판단에 쓰는 세 개의 수로 만든다.

계층 점수 = Σ(가중치 × 점수) ÷ Σ(가중치)이고, **결측 요인은 분자와 분모에서 모두 뺀다**.
분모를 고정하면 자료가 하나 빠질 때마다 점수가 0 쪽으로 끌려가, "자료가 없다"가 "중립 판단"으로
둔갑한다. 쓸 수 있는 요인이 하나도 없으면 0 이 아니라 None 을 준다 — 비중 계산이 그 사실을
알아야 기본값(risk_base)으로 물러설 수 있다 (설계 6.1).

가중치 0 인 관찰 요인은 계산에서 빠진다. 점수는 저장되므로 나중에 가중치를 올린 도전 안으로
같은 데이터에서 다시 계산할 수 있다 (결정 6).
"""


def layer_score(scores, specs, layer=None):
    """{factor_id: (score, missing)} → 계층 점수. 쓸 수 있는 요인이 없으면 None.

    specs 에 없는 요인 id 는 무시한다 (설정에서 빠진 요인의 옛 점수가 섞여 들어와도
    현재 설정의 가중치로만 계산되게 하려는 것이다 — 재평가에서 중요하다).
    """
    num = den = 0.0
    for fid, pair in (scores or {}).items():
        spec = specs.get(fid)
        if spec is None or not spec.used_in_weighting:
            continue
        if layer is not None and spec.layer != layer:
            continue
        score, missing = pair
        if missing or score is None:
            continue
        num += spec.weight * float(score)
        den += spec.weight
    if den == 0:
        return None
    return num / den


def market_score(scores, specs):
    """시장 계층 점수 → 1단계 위험자산 비중에 쓴다 (결정 10)."""
    return layer_score(scores, specs, layer="market")


def sector_scores(scores_by_sector, specs):
    """{섹터: {factor_id: (score, missing)}} → {섹터: 점수|None}. 섹터 ETF 선택에 쓴다."""
    return {sector: layer_score(scores, specs, layer="sector")
            for sector, scores in (scores_by_sector or {}).items()}


def stock_composites(stock_layer_scores, stock_sector_map, sector_scores, sector_tilt):
    """종목 종합 점수 = 종목 계층 점수 + sector_tilt × 소속 섹터 점수 (설계 5.3).

    시장 점수는 더하지 않는다. 모든 종목에 같은 값이라 순위가 바뀌지 않고, 그 정보의 자리는
    1단계 위험자산 비중이다 (결정 3).
    소속 섹터가 없거나 섹터 점수가 None 이면 기울기 항은 0 이다.
    종목 계층 점수 자체가 None(쓸 수 있는 요인 없음)인 종목은 결과에서 뺀다 — 점수가 없는 종목을
    0 점으로 두면 "중립인 종목"과 섞여 후보 순위에 들어간다.
    """
    out = {}
    tilt = float(sector_tilt or 0.0)
    for code, layer in (stock_layer_scores or {}).items():
        if layer is None:
            continue
        sector = (stock_sector_map or {}).get(code)
        sec = (sector_scores or {}).get(sector) if sector else None
        out[code] = float(layer) + tilt * (float(sec) if sec is not None else 0.0)
    return out
