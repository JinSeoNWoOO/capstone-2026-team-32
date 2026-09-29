"""시장 계층 코드 요인 (설계 5.2, 결정 12). 하루에 값이 하나이고 entity 는 항상 `'MARKET'` 이다.

네 요인 모두 **자기 과거 분포**나 **규칙**으로 점수를 만든다. 종목·섹터처럼 같은 날 비교 대상이
없기 때문이다 (결정 9). 그래서 이 계층에서 시점 규칙이 가장 위험하다 — 과거 분포를 만들 때
오늘 이후 값이 한 개만 섞여도 백분위가 통째로 틀어진다. 모든 읽기는 asof 모듈을 거친다.

각 함수는 `{entity: (raw_value, score, missing)}` 를 준다. 변환 전 원본 값을 함께 남기는 이유는
나중에 다른 변환(다른 창 길이, 다른 분포 기간)을 저장된 값에 사후 적용하기 위해서다 (결정 9).
"""
import statistics
from datetime import timedelta

from . import asof
from .normalize import hist_percentile_score, rule_score

MARKET = "MARKET"                       # 시장 계층의 유일한 entity id

# 코스피 지수 계열 이름과 밤사이 해외 계열. market_daily.series 의 값이다 (설계 8장).
KOSPI = "KOSPI"
# 밤사이 요인의 재료와 그 부호. 원/달러는 **오를수록 원화 약세**라 −를 붙여 평균에 넣는다 (결정 12).
OVERNIGHT_SERIES = (("SP500", 1.0), ("SOX", 1.0), ("USDKRW", -1.0))

# YAML 에 아직 없는 조정값 (config 소유자가 달라 여기서 못 넣는다 — asof.tunable 로 읽는다).
DEFAULTS = {
    # 과거 분포를 만들 때 쓸 최소 원자료 수. 이보다 짧으면 요인을 계산하지 않는다.
    "normalize.min_history": 60,
}


def _missing(entity=MARKET):
    return {entity: (None, 0.0, True)}


def _closes(rows):
    return [float(r["value"]) for r in rows if r["value"] is not None]


def _returns(rows):
    """[(날짜, 일간 수익률)] — 값이 비거나 0 이하인 구간은 건너뛴다 (지수·환율에 0 은 오류다)."""
    out = []
    prev = None
    for r in rows:
        v = r["value"]
        if v is None or float(v) <= 0:
            prev = None
            continue
        if prev is not None:
            out.append((r["date"], float(v) / prev - 1.0))
        prev = float(v)
    return out


def _hist_window_start(cfg, cutoff):
    """과거 분포의 시작 날짜. 설정의 normalize.hist_window_years 년 (결정 9: 최근 3~5년)."""
    years = float((cfg.get("normalize") or {}).get("hist_window_years") or 5)
    return asof.to_date_str(cutoff - timedelta(days=int(round(365.25 * years))))


def mkt_trend(store, cfg, spec, as_of, cal=None, uni=None):
    """코스피 종가 > 200일 이동평균 (규칙형, 부호 +). 결정 12의 근거 등급 2 요인.

    원본 값은 종가 ÷ 이동평균이다. 참·거짓보다 정보가 많아 나중에 '이격도' 형태의 도전 안을
    같은 저장 값에서 만들 수 있다.
    """
    ma_days = int((spec.params or {}).get("ma_days") or 200)
    cutoff = asof.last_kr_date(as_of, cfg)
    closes = _closes(asof.market_values(store, KOSPI, cutoff, ma_days))
    if len(closes) < ma_days:            # 200일치가 없으면 200일선이 아니다 — 지어내지 않는다
        return _missing()
    ma = sum(closes) / len(closes)
    if ma <= 0:
        return _missing()
    raw = closes[-1] / ma
    score, missing = rule_score(closes[-1] > ma, spec.sign)
    return {MARKET: (raw, score, missing)}


def mkt_vol(store, cfg, spec, as_of, cal=None, uni=None):
    """코스피 일간 수익률의 20일 표준편차 → 자기 과거 분포 백분위 (부호 −).

    '지금 변동성이 과거에 비해 얼마나 높은가'를 재는 요인이라, 비교 대상인 과거 분포도 **그날
    기준으로 계산할 수 있었던 값들**이어야 한다. 그래서 과거 분포를 미리 계산해 두지 않고
    매번 as_of 이전 구간에서 같은 방식으로 다시 만든다 (저장된 요인 값을 쓰면 실행 이력이
    없는 재현 모드에서 분포가 비어 버린다).
    """
    window = int((spec.params or {}).get("window") or 20)
    cutoff = asof.last_kr_date(as_of, cfg)
    rows = asof.market_values(store, KOSPI, cutoff)
    rets = _returns(rows)
    if window < 2 or len(rets) < window:   # 표준편차는 두 점부터 정의된다
        return _missing()

    vols = []                            # [(날짜, 그날까지의 window 일 표준편차)]
    for i in range(window, len(rets) + 1):
        chunk = [r for _, r in rets[i - window:i]]
        vols.append((rets[i - 1][0], statistics.stdev(chunk)))
    raw = vols[-1][1]

    start = _hist_window_start(cfg, cutoff)
    history = [v for d, v in vols[:-1] if asof.to_date_str(d) >= start]
    min_history = asof.tunable(cfg, "normalize.min_history", DEFAULTS)
    score, missing = hist_percentile_score(raw, history, spec.sign, min_history)
    return {MARKET: (raw, score, missing)}


def mkt_overnight(store, cfg, spec, as_of, cal=None, uni=None):
    """전일 S&P500·SOX 수익률과 −(원/달러 변화율)의 평균 → 과거 분포 백분위 (부호 +).

    **final 단계에서만 값이 있다.** prelim 에서 결측인 것이 곧 "밤사이 정보의 기여"를 재는
    실험 설계이고 (결정 11·14의 기준선 5번), 그 단계 판정은 compute.py 가 spec.stages 로 한다.

    세 계열은 휴장일이 서로 달라 날짜가 완전히 겹치지 않는다. 그래서 날짜의 합집합에서
    **그날 값이 있는 계열만의 평균**을 쓴다 — 하나라도 빠지면 결측으로 두면 미국 휴일마다
    요인이 통째로 사라진다.

    경계는 계열마다 따로 묻는다 (`asof.last_series_date`). 원/달러는 24시간 거래라 확정 시각이
    미국 지수와 다르고, 한 경계로 뭉뚱그리면 수집과 읽기가 어긋나는 시간대가 생긴다.
    """
    limits = {s: asof.last_series_date(s, as_of, cfg) for s, _ in OVERNIGHT_SERIES}
    cutoff = max(limits.values())                   # 과거 분포 창의 기준 (가장 늦은 경계)
    by_date = {}
    for series, sign in OVERNIGHT_SERIES:
        for day, ret in _returns(asof.market_values(store, series, limits[series])):
            by_date.setdefault(asof.to_date_str(day), []).append(sign * ret)
    if not by_date:
        return _missing()

    composites = [(d, sum(v) / len(v)) for d, v in sorted(by_date.items())]
    raw = composites[-1][1]
    start = _hist_window_start(cfg, cutoff)
    history = [v for d, v in composites[:-1] if d >= start]
    min_history = asof.tunable(cfg, "normalize.min_history", DEFAULTS)
    score, missing = hist_percentile_score(raw, history, spec.sign, min_history)
    return {MARKET: (raw, score, missing)}


def mkt_credit(store, cfg, spec, as_of, cal=None, uni=None):
    """신용잔고 ÷ 예탁금 (관찰 요인, 가중치 0).

    금융투자협회 FreeSIS 수집이 1차 범위에서 빠져 **항상 결측**이다 (설계 12장의 위험 대비).
    그래도 행은 남긴다 — 요인 표의 모양이 실행마다 달라지면 사후 재평가(결정 6)에서
    "그날 이 요인이 없었는가, 값이 없었는가"를 구분할 수 없다.
    """
    return _missing()


__all__ = ["MARKET", "DEFAULTS", "mkt_trend", "mkt_vol", "mkt_overnight", "mkt_credit"]
