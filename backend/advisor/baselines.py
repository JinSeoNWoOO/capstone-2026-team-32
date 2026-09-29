"""기준선 포트폴리오 (결정 14, 설계 6.3). 시스템과 **같은 체결·비용 함수**로 매일 계산한다.

| id | 내용 | 무엇을 보여 주는가 |
|---|---|---|
| `bl_kodex200` | KODEX 200 매수 후 보유 | 코스피를 그대로 산 경우 (결정 1의 필수 비교 대상) |
| `bl_sma10m` | 코스피 > 200일 이동평균이면 KODEX 200, 아니면 현금 대용 ETF | 단순 규칙 하나의 효과 |
| `bl_6040` | KODEX 200 60% + 현금 대용 40%, 월 1회 리밸런싱 | 아무 판단도 하지 않는 보수적 배분 |

기준선 4·5(v0 단독, 예비 판단)는 판단 기록에서 나오므로 `portfolio.SYSTEM_PORTFOLIOS` 쪽에 있다.

두 가지를 특히 조심한다.

1. **`bl_sma10m` 은 판단 시각에 알 수 있었던 값만 본다.** 체결일 당일 지수 종가로 그날 시가에
   체결하면 그것이 곧 미래 정보다. 그래서 `known_bar_date(as_of)` 까지의 코스피 값만 쓴다
   (07:40 최종 판단이면 전 거래일까지). 이동평균을 채울 만큼 과거가 없으면 **예약하지 않는다** —
   짧은 표본으로 계산한 이동평균은 규칙을 흉내 낸 다른 규칙이다.
2. **`bl_6040` 은 리밸런싱 기준(band)을 쓰지 않는다.** 5%포인트 기준을 걸면 월 1회 리밸런싱이
   대부분의 달에 일어나지 않아 사실상 매수 후 보유가 된다. band 는 매일 흔들리는 점수 때문에
   회전율이 튀는 것을 막는 장치이고 (결정 10), 고정 비중 기준선의 정의와는 무관하다.
"""
from . import portfolio
from .calendar import to_date

# 설정 파일은 다른 담당자가 소유한다. 여기서 새로 필요해진 값만 이 표에 둔다 (cfg 에 있으면 그쪽이 이긴다).
DEFAULTS = {
    "baselines.sma_series": "KOSPI",     # 이동평균 규칙이 보는 시장 계열 (market_daily.series)
    "baselines.sma_ma_days": 200,        # factors.mkt_trend.params.ma_days 가 없을 때만 쓰는 값
    "baselines.core_weight_6040": 0.60,  # 60/40 의 주식 몫
}

BASELINES = ("bl_kodex200", "bl_sma10m", "bl_6040")


def _ma_days(cfg):
    """이동평균 기간. 시스템의 mkt_trend 와 같은 값을 써야 '같은 규칙'의 비교가 성립한다 (결정 14)."""
    params = ((cfg.get("factors") or {}).get("mkt_trend") or {}).get("params") or {}
    return int(params.get("ma_days") or portfolio.opt_cfg(cfg, "baselines.sma_ma_days", DEFAULTS))


def is_month_first_trading_day(cal, day):
    """그날이 그 달의 첫 거래일인가 (60/40 의 월 1회 리밸런싱 시점)."""
    day = to_date(day)
    days = cal.trading_days_between(day.replace(day=1), day)
    return bool(days) and days[0] == day


def sma_signal(store, cfg, cal, as_of):
    """(위에 있는가, 값, 이동평균). 판단 시각에 알 수 있었던 값만 쓴다.

    쓸 수 있는 과거가 모자라면 (None, 값, None) 을 준다 — 값을 지어내지 않는다.
    """
    series = portfolio.opt_cfg(cfg, "baselines.sma_series", DEFAULTS)
    ma_days = _ma_days(cfg)
    known = portfolio.known_bar_date(cal, as_of, cfg)
    rows = store.market_series(series, known, ma_days)
    values = [float(r["value"]) for r in rows if r["value"] is not None]
    if len(values) < ma_days:
        return None, (values[-1] if values else None), None
    ma = sum(values) / len(values)
    return values[-1] > ma, values[-1], ma


def baseline_target(store, cfg, cal, baseline_id, fill_date, as_of, portfolio_id=None):
    """그 기준선이 이 체결일에 들고 있으려는 비중 {자산: 비중}. 예약할 것이 없으면 (None, 사유)."""
    uni = cfg["universe"]
    core, cash = uni["core_etf"], uni["cash_etf"]
    if baseline_id == "bl_kodex200":
        return {core: 1.0}, "매수 후 보유"

    if baseline_id == "bl_sma10m":
        above, value, ma = sma_signal(store, cfg, cal, as_of)
        if above is None:
            return None, f"이동평균({_ma_days(cfg)}일)을 채울 과거가 없습니다"
        note = f"지수 {value:.2f} {'>' if above else '<='} 이동평균 {ma:.2f}"
        return ({core: 1.0} if above else {cash: 1.0}), note

    if baseline_id == "bl_6040":
        w = float(portfolio.opt_cfg(cfg, "baselines.core_weight_6040", DEFAULTS))
        started = store.conn.execute("SELECT 1 FROM trade WHERE portfolio_id=? LIMIT 1",
                                     (portfolio_id or baseline_id,)).fetchone()
        if started and not is_month_first_trading_day(cal, fill_date):
            return None, "월 첫 거래일이 아닙니다"
        return {core: w, cash: 1.0 - w}, "월 1회 리밸런싱"

    raise ValueError(f"알 수 없는 기준선입니다: {baseline_id}")


def book_baselines(store, cfg, cal, fill_date, as_of, mode="live"):
    """기준선 셋의 체결 예약. 시스템 판단을 예약한 직후에 부른다 (같은 날 같은 시가에 체결된다).

    as_of 는 판단 시각(가능하면 시각까지)이다. 날짜만 주면 00:00 으로 보아 전 거래일 일봉까지만
    쓴다 — 없는 데이터를 쓰는 쪽으로 틀리지 않게 하려는 것이다 (설계 2.3).
    """
    out = []
    for base in BASELINES:
        pid = portfolio.portfolio_id_for(base, mode)
        target, note = baseline_target(store, cfg, cal, base, fill_date, as_of, portfolio_id=pid)
        if target is None:
            out.append({"portfolio_id": pid, "booked": False, "note": note})
            continue
        band = 0.0 if base == "bl_6040" else None
        deltas = portfolio.book_trades(store, cfg, pid, fill_date, target, src_run_id=None, band=band)
        out.append({"portfolio_id": pid, "booked": True, "target": target, "deltas": deltas, "note": note})
    return out


__all__ = ["DEFAULTS", "BASELINES", "is_month_first_trading_day", "sma_signal", "baseline_target",
           "book_baselines"]
