"""종목 계층 코드 요인 (설계 5.2, 결정 12). entity 는 6자리 종목 코드다.

둘 다 **같은 날 전체 순위**로 점수를 만든다 (결정 9: 섹터 안 순위가 아니라 코스피200 전체 순위).
순위는 극단값에 흔들리지 않고 조정할 파라미터가 없어 "임의로 정한 값" 시비가 가장 적고,
채점의 주 지표가 순위 상관이라 예측과 채점의 방식이 일치한다.

결측은 순위에서 빼고 0점 + missing 으로 둔다. 자료가 없는 종목을 "중간 순위"로 채워 넣으면
그 종목이 실제로 중간이었던 날과 구분이 안 돼 채점이 오염된다 (결정 9의 공통 규칙 2).
"""
from . import asof, fin
from .normalize import rank_scores

# YAML 에 아직 없는 조정값 — 설정 소유자가 달라 여기 DEFAULTS 로 두고 asof.tunable 로 읽는다.
# 자리를 normalize 아래로 잡은 이유: 설정의 factors 블록은 요인 메타데이터 표라 스칼라 키를
# 넣으면 validate_config 가 "매핑이어야 합니다"로 거부한다. min_history 와 같은 성격이기도 하다.
DEFAULTS = {
    # 창 길이 대비 최소 관측 비율. 250거래일 중 30일치 일봉만 있는 신규 상장 종목의 '52주 고가
    # 대비 위치'는 다른 종목과 같은 뜻이 아니다 — 지어내지 않고 결측으로 둔다.
    "normalize.min_obs_ratio": 0.6,
}


def adj_close(row):
    """수정 종가. 없으면 종가로 물러선다 (무료 대체 경로에서는 close == adj_close 다)."""
    v = row["adj_close"]
    return float(v) if v is not None else (float(row["close"]) if row["close"] is not None else None)


def high52_raw(store, cfg, lookback, cutoff, codes):
    """{code: 수정 종가 ÷ 최근 lookback 거래일 최고가}. 값이 없으면 키의 값이 None."""
    need = asof.min_obs(lookback, asof.tunable(cfg, "normalize.min_obs_ratio", DEFAULTS))
    out = {}
    for code in codes:
        bars = asof.price_bars(store, code, cutoff, lookback)
        vals = [v for v in (adj_close(b) for b in bars) if v is not None and v > 0]
        out[code] = (vals[-1] / max(vals)) if len(vals) >= need and max(vals) > 0 else None
    return out


def flow_raw(store, cfg, days, cutoff, cal, codes, window_sums=None, mktcaps=None):
    """{code: (외국인+기관 순매수 days 거래일 합) ÷ 시가총액}. 섹터 요인도 이 값을 재사용한다.

    분모는 **as_of 이전 마지막으로 알려진 시가총액**이다. 무료 대체 경로에서 과거 mktcap 이
    비어 있기 때문이고(데이터 계약), 그 값이 아예 없으면 순매수 '강도'를 만들 수 없으므로 결측이다.
    """
    start = cal.add_trading_days(cutoff, -(int(days) - 1)) if cal is not None else cutoff
    sums = asof.flow_window(store, start, cutoff) if window_sums is None else window_sums
    caps = asof.latest_mktcaps(store, cutoff) if mktcaps is None else mktcaps
    need = asof.min_obs(days, asof.tunable(cfg, "normalize.min_obs_ratio", DEFAULTS))
    out = {}
    for code in codes:
        net, n = sums.get(code, (None, 0))
        cap = caps.get(code)
        out[code] = (float(net) / cap) if (net is not None and n >= need and cap) else None
    return out


def _scored(raws, sign):
    scores = rank_scores(raws, sign)
    return {code: (raws[code], scores[code][0], scores[code][1]) for code in raws}


def stk_high52(store, cfg, spec, as_of, cal=None, uni=None):
    """현재가 ÷ 52주(250거래일) 최고가의 전체 순위 (부호 +). 결정 12의 근거 등급 2 요인."""
    codes = sorted((uni or {}).get("stocks") or {})
    if not codes:
        return {}
    lookback = int((spec.params or {}).get("lookback") or 250)
    raws = high52_raw(store, cfg, lookback, asof.last_kr_date(as_of, cfg), codes)
    return _scored(raws, spec.sign)


def stk_flow(store, cfg, spec, as_of, cal=None, uni=None):
    """(외국인+기관 순매수 20일 합) ÷ 시가총액의 전체 순위 (부호 +)."""
    codes = sorted((uni or {}).get("stocks") or {})
    if not codes:
        return {}
    days = int((spec.params or {}).get("days") or 20)
    raws = flow_raw(store, cfg, days, asof.last_kr_date(as_of, cfg), cal, codes)
    return _scored(raws, spec.sign)


def earn_growth_raw(store, cfg, spec, as_of, cutoff, codes, reports=None, mktcaps=None):
    """{code: (직전 분기 영업이익 − 전년 동기) ÷ 시가총액}. 못 구하면 None. 정의의 이유는 아래 요인 참고."""
    params = spec.params or {}
    account = str(params.get("account") or "op_income")
    max_age = params.get("max_age_days")
    as_of_date = asof.to_datetime(as_of).date()
    reports = asof.fin_reports(store, as_of, codes) if reports is None else reports
    caps = asof.latest_mktcaps(store, cutoff) if mktcaps is None else mktcaps
    out = {}
    for code in codes:
        change = fin.yoy_change(reports.get(code) or [], account, as_of_date, max_age)
        cap = caps.get(code)
        out[code] = (change["delta"] / cap) if (change is not None and cap) else None
    return out


def stk_earn_growth(store, cfg, spec, as_of, cal=None, uni=None):
    """직전 분기 영업이익의 전년 동기 대비 변화 ÷ 시가총액의 전체 순위 (부호 +). **관찰 요인(가중치 0)**.

    2026-09-28 추가 (결정 12 의 재무 요인, 결정 6: 관찰 요인으로 넣고 가중치는 재평가 뒤 사람이 정한다).

    **왜 증가율이 아니라 '변화 ÷ 시가총액'인가.** 결정 12 의 글자 그대로의 정의는 증가율
    (당분기 − 전년 동기) ÷ 전년 동기인데, 기준값이 0 에 가까우면 값이 폭주하고(영업이익 1억 → 50억 이
    +4,900%) 음수이면 부호가 뒤집힌다(−100억 → −50억 은 개선인데 증가율 −50%). 순위 변환이 폭주의 '크기'는
    눌러 주지만 '순서'는 못 고친다 — 기준값이 작은 종목이 구조적으로 양 끝에 몰린다.
    |기준값| 으로 나누는 흔한 땜질도 0 근처 폭주는 그대로다.
    변화의 크기를 시가총액으로 나누면 (1) 기준값의 부호·크기와 무관하게 정의되고 (2) "시장가치에 비해
    이익이 얼마나 늘었나"라는, 실적 발표 후 드리프트(PEAD) 연구의 가격 척도 이익 변화(계절 랜덤워크
    기대 대비 서프라이즈를 주가로 나눈 값)와 같은 형태가 된다. 분모는 stk_flow 와 같은
    '그때까지 알려진 마지막 시가총액'이다.
    참고용 증가율은 `fin.growth_rate` 로 따로 낼 수 있다 (재평가 보고서에서 두 정의를 비교한다).

    분기 단독값·4분기 계산·연결/별도 규칙은 `factors/fin.py`, 공시일 기준 시점 처리는 `sources/dart_fin.py`.
    재무가 없는 종목은 결측이다 (순위에서 빠진다, 결정 9 공통 규칙 2).
    """
    codes = sorted((uni or {}).get("stocks") or {})
    if not codes:
        return {}
    raws = earn_growth_raw(store, cfg, spec, as_of, asof.last_kr_date(as_of, cfg), codes)
    return _scored(raws, spec.sign)


__all__ = ["DEFAULTS", "adj_close", "earn_growth_raw", "flow_raw", "high52_raw", "stk_earn_growth",
           "stk_flow", "stk_high52"]
