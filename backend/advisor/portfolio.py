"""가상 포트폴리오: 체결 예약·정산·비용·NAV (설계 6.2, 결정 10·11·14).

주문을 내지 않는 대신 "그날 시가에 그 비중으로 샀다면"을 **비중 기준으로** 회계한다.
초기 NAV 는 1.0 이고 주 수 반올림은 하지 않는다 — 평가 대상은 체결 기술이 아니라 점수의 품질이고
(결정 4·5), 주 수를 넣는 순간 종목별 최소 단위 때문에 비중 규칙과 결과가 어긋나기 때문이다.

이 파일의 모양을 정하는 규칙 넷.

1. **예약 → 확정의 두 단계.** 최종 판단은 T일 07:40 에 나오지만 그날 시가는 T일 18:00 이 지나야 알 수
   있다 (설계 2.3의 시점 규칙). 그래서 판단 시각에는 status='pending' 인 trade 행만 남기고, 같은 날
   저녁 예비 배치가 `settle()` 로 그날 일봉을 받아 시가로 체결을 확정한다. 판단 시각에 시가를 읽으면
   그것이 곧 미래 정보 누출이다.
2. **하루를 두 구간으로 나눈다.** ① 전일 종가 → 당일 시가(체결 전 표류) ② 당일 시가 → 종가(체결 후 평가).
   종가 대 종가 한 구간으로 뭉뚱그리면 "시가에 체결했다"는 가정이 회계에서 사라져, 비용과 새 비중이
   그날 수익에 잘못 실린다 (특히 비중을 크게 바꾼 날에 차이가 난다).
3. **live 와 replay 는 절대 섞이지 않는다.** 재현 기록은 성과 주장에 쓰지 않으므로 (설계 7.4),
   포트폴리오 id 에 '@replay' 를 붙여 상태(보유·NAV) 자체를 분리한다.
4. **없는 값을 지어내지 않는다.** 체결할 일봉이 없거나 거래량이 0 인 자산은 'skipped' 로 남기고,
   보유 자산의 시세가 그날 없으면 마지막 가격을 이어 쓰되 그 사실을 보고에 담는다.

가격 가정: 체결가는 `open`, 평가·채점은 `adj_close` 를 쓴다. 현재 수집 대체 경로에서는 close == adj_close
이고 open 도 같은 기준(수정 전후가 갈리지 않은 값)이라 두 값을 한 수익률 안에서 섞어도 일관된다.
액면분할·배당으로 adj_close 가 close 와 갈리는 구간을 쓰게 되면 open 도 같은 배수로 조정해 넣어야 한다.

turnover 와 cost 는 **체결 시점 NAV 대비 비율**로 남긴다(합이 아니라 비율이라야 날짜별로 비교된다).
"""
import logging
import math

from . import allocate
from .calendar import to_date
from .factors import asof

log = logging.getLogger("advisor.portfolio")

# 설정 파일(advisor.config.yaml)은 다른 담당자가 소유한다. 여기서 새로 필요해진 값은 YAML 에 넣지 않고
# cfg 에 있으면 그것을, 없으면 이 표의 값을 쓴다. 키는 설정에 넣을 때 쓸 경로 그대로 적는다.
DEFAULTS = {
    "portfolio.trading_days_per_year": 252,  # 변동성 연율화 계수
    "portfolio.start_nav": 1.0,              # 초기 NAV (설계 6.2)
    "portfolio.default_kind": "stock",       # universe 에 없는 자산의 종류 — 거래세를 무는 쪽으로 보수적으로
}

REPLAY_SUFFIX = "@replay"

# 시스템 포트폴리오 셋 (설계 6.3). stage 는 어느 단계의 판단을 체결하는가,
# variant 는 어느 판단 버전인가, offset 은 체결일 기준 며칠 전 실행인가(거래일).
SYSTEM_PORTFOLIOS = {
    "sys_final_llm":  {"stage": "final",  "variant": "llm", "offset": 0},
    "sys_final_v0":   {"stage": "final",  "variant": "v0",  "offset": 0},
    "sys_prelim_llm": {"stage": "prelim", "variant": "llm", "offset": -1},
}

_EPS = 1e-12
# 목표 비중의 합이 1 에서 이 정도까지 어긋난 것은 저장 과정의 부동소수점 오차로 보고 되맞춘다.
# 그보다 크게 어긋났으면 판단 기록 자체가 깨진 것이라 조용히 고치지 않고 멈춘다.
_TARGET_TOL = 1e-6


# ---------------------------------------------------------------- 설정·시점 보조

def opt_cfg(cfg, key, defaults=None):
    """설정에서 점 경로로 값을 찾고, 없으면 DEFAULTS 로 물러선다 (factors/asof.tunable 과 같은 규약)."""
    node = cfg or {}
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return (defaults or DEFAULTS)[key]
        node = node[part]
    return node


def known_bar_date(cal, as_of, cfg=None):
    """as_of 시점에 쓸 수 있는 **마지막 일봉의 거래일** (설계 2.3).

    경계 시각(국내 일봉은 D일 18:00)은 `factors.asof.last_kr_date` 에 맡긴다. 요인 계산이 보는
    경계와 채점·기준선이 보는 경계가 다르면 시점 규칙이 두 벌이 되고, 설정의
    `sources.daily_bar_known_at` 을 바꿨을 때 한쪽만 따라간다. 여기서는 그 날짜를 **거래일로
    당겨** 돌려준다 — 채점의 '만기가 됐는가'와 기준선의 '그때 알 수 있었는가'가 거래일 단위이기 때문이다.
    시각이 없는 as_of('YYYY-MM-DD')는 00:00 으로 보아 전 거래일까지만 쓴다.
    """
    d = to_date(asof.last_kr_date(as_of, cfg))
    return d if cal.is_trading_day(d) else cal.prev_trading_day(d)


def start_day(cal, stage, as_of):
    """그 판단을 실제로 실행할 수 있었던 첫 거래일 (설계 7.1의 '시작가' 날짜).

    최종(07:40)은 당일 시가, 예비(18:30)는 다음 거래일 시가가 첫 체결 기회다.
    """
    d = to_date(as_of)
    if str(stage) == "final":
        return d if cal.is_trading_day(d) else cal.next_trading_day(d)
    return cal.next_trading_day(d)


# ---------------------------------------------------------------- 포트폴리오 id

def mode_of(portfolio_id):
    """id 접미사로 모드를 읽는다. 기록이 섞이지 않게 하는 유일한 장치다 (설계 7.4)."""
    return "replay" if str(portfolio_id).endswith(REPLAY_SUFFIX) else "live"


def portfolio_id_for(base_id, mode="live"):
    """'sys_final_v0' + replay → 'sys_final_v0@replay'."""
    base = base_portfolio_id(base_id)
    return base + REPLAY_SUFFIX if mode == "replay" else base


def base_portfolio_id(portfolio_id):
    pid = str(portfolio_id)
    return pid[: -len(REPLAY_SUFFIX)] if pid.endswith(REPLAY_SUFFIX) else pid


def portfolio_ids(store, mode="live"):
    """기록이 있는 포트폴리오 id 목록 (해당 모드만)."""
    rows = store.conn.execute(
        "SELECT portfolio_id FROM trade UNION SELECT portfolio_id FROM holding").fetchall()
    return sorted({r[0] for r in rows if r[0] and mode_of(r[0]) == mode})


def asset_kinds(store, cfg):
    """{자산: stock|etf|cash_etf}. 거래세가 주식 매도에만 붙으므로 (설계 6.2) 이 구분이 비용을 가른다.

    설정의 ETF 목록으로 먼저 채우고 universe 스냅샷으로 덮는다 — 수집된 목록이 진실이지만,
    universe 가 아직 비어 있는 시점(기준선만 도는 초기)에도 ETF 가 주식으로 과세되면 안 된다.
    """
    uni = cfg.get("universe") or {}
    kinds = {}
    if uni.get("core_etf"):
        kinds[uni["core_etf"]] = "etf"
    if uni.get("cash_etf"):
        kinds[uni["cash_etf"]] = "cash_etf"
    for code in (uni.get("sector_etfs") or {}).values():
        kinds[code] = "etf"
    for code in (uni.get("other_etfs") or {}).values():
        kinds[code] = "etf"
    try:
        rows = store.conn.execute("SELECT code, kind FROM universe ORDER BY as_of_date").fetchall()
    except Exception:                       # 테이블이 비었거나 아직 없을 수 있다
        rows = []
    for r in rows:
        if r["kind"]:
            kinds[r["code"]] = r["kind"]
    return kinds


# ---------------------------------------------------------------- 하루치 회계 (순수 계산)

def _field(row, name, default=None):
    """dict 와 sqlite3.Row 를 같이 받기 위한 접근자."""
    try:
        v = row[name]
    except (KeyError, IndexError, TypeError):
        return default
    return default if v is None else v


def _normalised_target(target):
    """목표 비중의 합을 1 로 맞춘다. 크게 어긋났으면 조용히 고치지 않고 멈춘다."""
    tgt = {a: float(w) for a, w in (target or {}).items() if float(w) != 0.0}
    total = sum(tgt.values())
    if not tgt:
        raise ValueError("목표 비중이 비어 있습니다")
    if abs(total - 1.0) > _TARGET_TOL:
        raise ValueError(f"목표 비중의 합이 1이 아닙니다: {total!r}")
    if total != 1.0:
        tgt = {a: w / total for a, w in tgt.items()}
    return tgt


def plan_trades(current, target, band, core_asset):
    """현재 비중 → 목표 비중의 실제 거래량 {자산: 비중 변화}. 변화량의 합은 0 이다.

    리밸런싱 기준(band)은 `allocate.apply_rebalance_band` 가 그대로 적용한다 — 비중 규칙은 한 곳에만
    있어야 도전 안 재평가가 같은 규칙으로 돌아간다 (결정 6).
    """
    current = {a: float(w) for a, w in (current or {}).items()}
    banded = allocate.apply_rebalance_band(current, _normalised_target(target), float(band), core_asset)
    deltas = {}
    for asset in set(banded) | set(current):
        d = float(banded.get(asset, 0.0)) - float(current.get(asset, 0.0))
        if abs(d) > _EPS:
            deltas[asset] = d
    return deltas


def advance_day(state, bars, deltas=None, *, cfg, kinds=None):
    """하루를 한 걸음 진행한다. (새 state, 기록) 을 돌려준다. DB 를 보지 않는 순수 계산이다.

    state: {"nav": float, "weights": {자산: 비중}, "prices": {자산: 마지막 가격}}
    bars:  {자산: {"open","adj_close","volume"}} — 그날 일봉이 있는 자산만
    deltas: 그날 체결할 비중 변화 {자산: ±비중} (예약된 trade 행에서 온다)

    회계 순서 (설계 6.2):
      1) 전일 종가 → 당일 시가로 **어제 비중 그대로** 표류시킨다. 체결은 이 비중 위에서 일어난다.
      2) 시가에 체결한다. 비용은 체결 금액(|비중 변화|) × (슬리피지 + 수수료 + 주식 매도 거래세).
         비용은 NAV 를 깎고 비중은 건드리지 않는다 — 비용은 포트폴리오 전체에서 빠져나간 돈이다.
      3) 시가 → 종가로 **체결 후 비중**으로 평가한다.

    체결이 불가능한 자산(일봉 없음·거래량 0)의 변화량은 적용하지 않고 'skipped' 로 남긴다.
    그러면 비중의 합이 1 에서 어긋나므로, **건너뛴 자산의 비중은 그대로 두고 나머지를 비례 조정**해
    합을 1 로 되맞춘다. 건너뛴 자산까지 같이 줄이면 "거래하지 못했다"가 "조금 팔았다"가 된다.

    **전량 매도는 표류와 무관하게 0 으로 끝난다** (설계 6.2의 "전량 제외는 차이와 무관하게 실행").
    예약은 마지막 정산일 종가 비중에서 잰 차이인데 체결은 다음 시가라, 1구간 표류만큼 변화량이
    모자라 먼지 같은 잔량이 남는다. 그 잔량이 남으면 ① 다음 날 '보유'로 읽혀 exit_rank 가 자리를
    계속 내주고 ② 목표와의 차이가 리밸런싱 기준(5%p)에 못 미쳐 제 비중으로 돌아오지도 못한다
    (2026-04~09 재현에서 실제로 그랬다: 목표 2.47% 인 종목이 0.34% 로 눌린 채 남고 그 몫이
    핵심 ETF 로 쏠렸다). 그래서 **예약이 0 으로 비우려던 자산은 여기서 0 으로 만든다.**
    판정에 쓰는 것은 표류 전 비중(`state["weights"]`)이다 — 예약이 차이를 잰 바로 그 값이라
    `weights[자산] + 변화량 <= 0` 이 곧 "전량 매도였다"가 된다.
    """
    costs = cfg["costs"]
    kinds = kinds or {}
    default_kind = opt_cfg(cfg, "portfolio.default_kind")
    prices = dict(state.get("prices") or {})
    weights = {a: float(w) for a, w in (state.get("weights") or {}).items()}
    nav = float(state.get("nav", opt_cfg(cfg, "portfolio.start_nav")))
    deltas = {a: float(d) for a, d in (deltas or {}).items()}

    # --- 가격 정리: 일봉이 있으면 그것, 없으면 마지막 가격을 이어 쓴다(그 사실을 기록한다)
    opens, closes, tradable, carried = {}, {}, set(), []
    for asset in set(weights) | set(deltas):
        bar = bars.get(asset)
        o, c = (_field(bar, "open"), _field(bar, "adj_close")) if bar is not None else (None, None)
        if c is None and bar is not None:
            c = _field(bar, "close")                     # adj_close 가 비면 close 로 (수집 대체 경로)
        if o and c:
            opens[asset], closes[asset] = float(o), float(c)
            if float(_field(bar, "volume", 0.0) or 0.0) > 0:
                tradable.add(asset)
        else:
            last = prices.get(asset)
            if weights.get(asset):
                carried.append(asset)                    # 들고 있는데 그날 시세가 없다 → 보고에 남긴다
            if last is None:
                continue                                 # 처음 보는 자산인데 시세도 없다 → 체결 불가
            opens[asset] = closes[asset] = float(last)

    # --- 1구간: 전일 종가 → 당일 시가
    growth = 0.0
    legs = {}
    for asset, w in weights.items():
        p0, p1 = prices.get(asset), opens.get(asset)
        r = (p1 / p0 - 1.0) if (p0 and p1) else 0.0
        legs[asset] = r
        growth += w * r
    nav_open = nav * (1.0 + growth)
    if 1.0 + growth <= 0:
        raise ValueError(f"시가까지의 표류로 NAV 가 0 이하가 됐습니다: {growth!r}")
    post = {a: w * (1.0 + legs[a]) / (1.0 + growth) for a, w in weights.items()}

    # --- 2구간 앞: 시가 체결
    filled, skipped = {}, {}
    turnover = cost_frac = 0.0
    rate_base = float(costs["slippage"]) + float(costs["fee"])
    for asset, d in sorted(deltas.items()):
        if asset not in tradable:
            skipped[asset] = d
            continue
        cur = post.get(asset, 0.0)
        # 예약이 0 으로 비우려던 자산은 표류와 무관하게 0 으로 (위 머리말의 '전량 매도').
        # 나머지는 표류 뒤 비중에 변화량을 그대로 얹고 음수만 막는다.
        new = 0.0 if weights.get(asset, 0.0) + d <= _EPS else max(0.0, cur + d)
        applied = new - cur
        post[asset] = new
        if abs(applied) <= _EPS:
            filled[asset] = {"delta": 0.0, "price": opens.get(asset), "cost": 0.0,
                             "side": "buy" if d > 0 else "sell"}
            continue
        rate = rate_base
        if applied < 0 and kinds.get(asset, default_kind) == "stock":
            rate += float(costs["tax_stock_sell"])       # 거래세는 주식 매도에만 (ETF 면제)
        cost = abs(applied) * rate
        turnover += abs(applied)
        cost_frac += cost
        filled[asset] = {"delta": applied, "price": opens.get(asset), "cost": cost,
                         "side": "buy" if applied > 0 else "sell"}

    post = {a: w for a, w in post.items() if w > _EPS}
    total = sum(post.values())
    if abs(total - 1.0) > _EPS and total > 0:
        fixed = sum(w for a, w in post.items() if a in skipped)
        movable = total - fixed
        if movable > _EPS and (1.0 - fixed) > 0:
            k = (1.0 - fixed) / movable
            post = {a: (w if a in skipped else w * k) for a, w in post.items()}
        else:
            post = {a: w / total for a, w in post.items()}

    nav_traded = nav_open * (1.0 - cost_frac)

    # --- 2구간: 시가 → 종가
    growth2 = 0.0
    legs2 = {}
    for asset, w in post.items():
        p1, p2 = opens.get(asset), closes.get(asset)
        r = (p2 / p1 - 1.0) if (p1 and p2) else 0.0
        legs2[asset] = r
        growth2 += w * r
    if 1.0 + growth2 <= 0:
        raise ValueError(f"종가 평가로 NAV 가 0 이하가 됐습니다: {growth2!r}")
    nav_close = nav_traded * (1.0 + growth2)
    w_close = {a: w * (1.0 + legs2[a]) / (1.0 + growth2) for a, w in post.items()}

    new_prices = dict(prices)
    new_prices.update(closes)
    new_state = {"nav": nav_close, "weights": w_close, "prices": new_prices}
    record = {"nav": nav_close, "nav_open": nav_open, "turnover": turnover, "cost": cost_frac,
              "weights": w_close, "post_trade_weights": post, "filled": filled, "skipped": skipped,
              "carried": sorted(carried)}
    return new_state, record


# ---------------------------------------------------------------- 체결 예약

def current_weights(store, portfolio_id, before=None):
    """마지막으로 정산된 날의 보유 비중 {자산: 비중}. before 를 주면 그 날짜 **이전**만 본다.

    예약은 '마지막 정산일 종가 기준 비중'과 목표를 견준다. 체결일 당일 시가 비중은 판단 시각에
    알 수 없기 때문이다 (설계 2.3).
    """
    sql = "SELECT date FROM holding WHERE portfolio_id=?"
    params = [portfolio_id]
    if before is not None:
        sql += " AND date<?"
        params.append(str(to_date(before)))
    row = store.conn.execute(sql + " ORDER BY date DESC LIMIT 1", params).fetchone()
    if row is None:
        return {}
    rows = store.conn.execute(
        "SELECT asset, weight FROM holding WHERE portfolio_id=? AND date=?",
        (portfolio_id, row[0])).fetchall()
    return {r["asset"]: float(r["weight"]) for r in rows}


def book_trades(store, cfg, portfolio_id, fill_date, target, src_run_id=None, band=None):
    """체결 예약. 목표 비중과 현재 비중의 차이를 status='pending' 인 trade 행으로 남긴다.

    같은 (포트폴리오, 체결일)로 다시 부르면 먼저 예약된 pending 행을 지우고 다시 쓴다 — 07:40 최종
    배치가 재시도되거나 08:50 대체 규칙으로 다른 판단이 올라와도 그날 예약은 한 벌이어야 한다.
    이미 정산된(filled/skipped) 행은 건드리지 않는다. 기록은 고치지 않고 덧붙이는 것이 원칙이다.

    band 를 따로 주면 그 값으로 리밸런싱 기준을 적용한다 (기준선 60/40 의 월 1회 리밸런싱처럼
    '기준을 두지 않는 것이 정의'인 포트폴리오를 위해서다).
    """
    fill = str(to_date(fill_date))
    core = (cfg.get("universe") or {}).get("core_etf")
    band = float(cfg["allocate"]["rebalance_band"]) if band is None else float(band)
    settled = store.conn.execute("SELECT 1 FROM nav WHERE portfolio_id=? AND date=?",
                                 (portfolio_id, fill)).fetchone()
    if settled:
        log.warning("이미 정산된 날짜에 예약이 들어왔습니다 (체결되지 않는다): %s %s", portfolio_id, fill)
    current = current_weights(store, portfolio_id, before=fill)
    deltas = plan_trades(current, target, band, core)
    store.conn.execute("DELETE FROM trade WHERE portfolio_id=? AND date=? AND status='pending'",
                       (portfolio_id, fill))
    store.conn.executemany(
        "INSERT INTO trade(portfolio_id,date,asset,side,weight_delta,price,cost,src_run_id,status) "
        "VALUES(?,?,?,?,?,?,?,?,'pending')",
        [(portfolio_id, fill, asset, "buy" if d > 0 else "sell", d, None, None, src_run_id)
         for asset, d in sorted(deltas.items())])
    store.commit()
    return deltas


def decision_for_fill(store, portfolio_id, fill_date, mode="live", cal=None):
    """이 포트폴리오가 이 날 시가에 **실제로 체결할 판단**을 고른다 (설계 6.3, 결정 11의 대체 규칙).

    고르는 순서는 세 갈래다.
      1. 단계: sys_final_* 는 체결일 당일의 최종 실행, sys_prelim_llm 은 직전 거래일 저녁의 예비 실행.
      2. 대체: 최종 실행이 없거나 목표 비중을 남기지 못했으면 **직전 거래일의 예비 판단을 승격**한다.
         (스케줄러가 08:50 에 fallback_used=1 로 최종 실행을 만들어 두는 경우에도 같은 함수가
          그 실행을 그냥 찾아낸다 — 여기에 대체 규칙 전용 분기를 두지 않는 이유다.)
      3. 버전: llm 판단이 없으면 v0 로 물러선다. 그것이 곧 "LLM 이 기권했다"이고 (결정 15),
         LLM 조정 포트폴리오가 그날 v0 와 같아지는 것이 정확한 기록이다.

    같은 (단계, 기준일)에 실행이 여러 번 있으면 마지막 run_id 를 쓴다. 상태(status)로 거르지 않는 이유는
    최종 배치가 자기 실행 안에서 예약을 걸 때 아직 RUNNING 이기 때문이다. 목표 비중 행의 유무로 판정한다.
    """
    base = base_portfolio_id(portfolio_id)
    spec = SYSTEM_PORTFOLIOS.get(base)
    if spec is None:
        raise ValueError(f"시스템 포트폴리오가 아닙니다: {portfolio_id}")
    fill = to_date(fill_date)
    prev = cal.prev_trading_day(fill) if cal is not None else None

    def pick(stage, as_of):
        sql = ("SELECT run_id FROM run WHERE stage=? AND mode=?")
        params = [stage, mode]
        if as_of is not None:
            sql += " AND as_of=?"
            params.append(str(as_of))
        else:
            sql += " AND as_of<?"
            params.append(str(fill))
        for row in store.conn.execute(sql + " ORDER BY as_of DESC, run_id DESC", params).fetchall():
            run_id = int(row[0])
            for variant in (spec["variant"], "v0"):
                rows = store.conn.execute(
                    "SELECT asset, weight FROM target_weight WHERE run_id=? AND variant=?",
                    (run_id, variant)).fetchall()
                if rows:
                    return {"run_id": run_id, "stage": stage, "as_of": str(as_of) if as_of else None,
                            "variant": variant, "requested_variant": spec["variant"],
                            "llm_abstained": variant != spec["variant"],
                            "weights": {r["asset"]: float(r["weight"]) for r in rows}}
        return None

    if spec["stage"] == "final":
        got = pick("final", fill)
        if got is not None:
            got["promoted"] = False
            return got
        # 대체 규칙: 전날 예비 판단을 승격한다 (결정 11). run.fallback_used 는 파이프라인이 남긴다.
        got = pick("prelim", prev)
        if got is not None:
            got["promoted"] = True
        return got

    got = pick("prelim", prev)
    if got is not None:
        got["promoted"] = False
    return got


def book_system_portfolios(store, cfg, cal, fill_date, mode="live"):
    """시스템 포트폴리오 셋의 체결 예약 (최종 배치가 판단을 저장한 직후에 부른다)."""
    out = []
    for base in SYSTEM_PORTFOLIOS:
        pid = portfolio_id_for(base, mode)
        dec = decision_for_fill(store, base, fill_date, mode=mode, cal=cal)
        if dec is None:
            out.append({"portfolio_id": pid, "booked": False, "note": "체결할 판단이 없습니다"})
            continue
        try:
            deltas = book_trades(store, cfg, pid, fill_date, dec["weights"], dec["run_id"])
        except ValueError as exc:
            # 한 판단의 목표 비중이 깨졌다고 나머지 포트폴리오의 예약까지 막지 않는다.
            log.error("체결 예약 실패 %s: %s", pid, exc)
            out.append({"portfolio_id": pid, "booked": False, "note": f"예약 실패: {exc}"})
            continue
        out.append({"portfolio_id": pid, "booked": True, "run_id": dec["run_id"],
                    "variant": dec["variant"], "promoted": dec.get("promoted", False),
                    "llm_abstained": dec.get("llm_abstained", False), "deltas": deltas})
    return out


# ---------------------------------------------------------------- 정산

def _bars(store, assets, day):
    """그날 일봉 {자산: row}. 없는 자산은 빠진다."""
    if not assets:
        return {}
    assets = list(assets)
    out = {}
    chunk = 400                                          # SQLite 변수 한도를 넘지 않게 나눠 묻는다
    for i in range(0, len(assets), chunk):
        part = assets[i:i + chunk]
        rows = store.conn.execute(
            "SELECT * FROM price_daily WHERE date=? AND code IN (%s)" % ",".join("?" * len(part)),
            [str(day)] + part).fetchall()
        for r in rows:
            out[r["code"]] = r
    return out


def _load_state(store, cfg, portfolio_id, day):
    """정산된 날의 상태 복원: 보유 비중·NAV·마지막 가격."""
    nav_row = store.conn.execute("SELECT nav FROM nav WHERE portfolio_id=? AND date=?",
                                 (portfolio_id, str(day))).fetchone()
    weights = {r["asset"]: float(r["weight"]) for r in store.conn.execute(
        "SELECT asset, weight FROM holding WHERE portfolio_id=? AND date=?",
        (portfolio_id, str(day))).fetchall()}
    prices = {}
    for asset in weights:
        row = store.conn.execute(
            "SELECT adj_close, close FROM price_daily WHERE code=? AND date<=? "
            "AND (adj_close IS NOT NULL OR close IS NOT NULL) ORDER BY date DESC LIMIT 1",
            (asset, str(day))).fetchone()
        if row is not None:
            prices[asset] = float(row["adj_close"] if row["adj_close"] is not None else row["close"])
    nav = float(nav_row[0]) if nav_row else float(opt_cfg(cfg, "portfolio.start_nav"))
    return {"nav": nav, "weights": weights, "prices": prices}


def settle_portfolio(store, cfg, cal, portfolio_id, date, kinds=None):
    """한 포트폴리오를 date 까지 정산한다. 아직 정산하지 않은 거래일을 순서대로 지나간다.

    정산은 되돌리지 않는다(이미 NAV 행이 있는 날은 건너뛴다). 그날 시세가 하나도 없으면 그날부터
    멈춘다 — 수집이 아직 안 된 날을 '아무 일도 없던 날'로 적으면 NAV 가 조용히 거짓말을 한다.
    """
    kinds = kinds if kinds is not None else asset_kinds(store, cfg)
    end = to_date(date)
    last = store.conn.execute("SELECT date FROM nav WHERE portfolio_id=? ORDER BY date DESC LIMIT 1",
                              (portfolio_id,)).fetchone()
    if last is not None:
        state = _load_state(store, cfg, portfolio_id, last[0])
        start = cal.next_trading_day(last[0])
    else:
        first = store.conn.execute("SELECT MIN(date) FROM trade WHERE portfolio_id=?",
                                   (portfolio_id,)).fetchone()[0]
        if not first:
            return {"portfolio_id": portfolio_id, "days": [], "notes": ["예약된 체결이 없습니다"]}
        state = {"nav": float(opt_cfg(cfg, "portfolio.start_nav")), "weights": {}, "prices": {}}
        start = to_date(first)

    notes, done = [], []
    for day in cal.trading_days_between(start, end):
        ds = str(day)
        pending = store.conn.execute(
            "SELECT asset, weight_delta FROM trade WHERE portfolio_id=? AND date=? AND status='pending'",
            (portfolio_id, ds)).fetchall()
        deltas = {r["asset"]: float(r["weight_delta"]) for r in pending}
        assets = set(state["weights"]) | set(deltas)
        if not assets:
            continue                                     # 첫 예약 전의 날 — 포트폴리오가 아직 없다
        bars = _bars(store, assets, ds)
        if not bars and not store.conn.execute(
                "SELECT 1 FROM price_daily WHERE date=? LIMIT 1", (ds,)).fetchone():
            # 그날 일봉이 시장 전체에 하나도 없다 = 아직 수집 전이다. 아무 일도 없던 날로 적으면
            # NAV 가 조용히 거짓말을 하므로 여기서 멈추고 다음 정산 때 이어 간다.
            notes.append(f"{ds}: 그날 시세가 하나도 없어 정산을 멈춥니다")
            break
        state, rec = advance_day(state, bars, deltas, cfg=cfg, kinds=kinds)

        for asset, info in rec["filled"].items():
            store.conn.execute(
                "UPDATE trade SET status='filled', price=?, cost=?, weight_delta=? "
                "WHERE portfolio_id=? AND date=? AND asset=? AND status='pending'",
                (info["price"], info["cost"], info["delta"], portfolio_id, ds, asset))
        for asset in rec["skipped"]:
            store.conn.execute(
                "UPDATE trade SET status='skipped' WHERE portfolio_id=? AND date=? AND asset=? "
                "AND status='pending'", (portfolio_id, ds, asset))
            notes.append(f"{ds}: {asset} 체결 불가(일봉 없음·거래량 0) → skipped")
        for asset in rec["carried"]:
            notes.append(f"{ds}: {asset} 시세 없음 → 마지막 가격을 이어 씀")

        store.conn.execute("DELETE FROM holding WHERE portfolio_id=? AND date=?", (portfolio_id, ds))
        store.conn.executemany(
            "INSERT INTO holding(portfolio_id,date,asset,weight) VALUES(?,?,?,?)",
            [(portfolio_id, ds, a, w) for a, w in sorted(rec["weights"].items())])
        store.conn.execute(
            "INSERT OR REPLACE INTO nav(portfolio_id,date,nav,turnover,cost) VALUES(?,?,?,?,?)",
            (portfolio_id, ds, rec["nav"], rec["turnover"], rec["cost"]))
        done.append({"date": ds, "nav": rec["nav"], "turnover": rec["turnover"], "cost": rec["cost"],
                     "filled": len(rec["filled"]), "skipped": len(rec["skipped"])})

    # 지나간 날짜에 남은 예약은 체결될 수 없다 (휴장일에 예약됐거나 정산 시작 전의 행).
    # 그대로 두면 한참 뒤의 정산이 엉뚱한 날 시가로 체결해 버리므로 여기서 닫는다.
    if done:
        cur = store.conn.execute(
            "UPDATE trade SET status='skipped' WHERE portfolio_id=? AND date<=? AND status='pending'",
            (portfolio_id, done[-1]["date"]))
        if cur.rowcount:
            notes.append(f"{done[-1]['date']} 까지 남아 있던 예약 {cur.rowcount}건을 skipped 로 닫았습니다")
    return {"portfolio_id": portfolio_id, "days": done, "notes": notes}


def settle(store, cfg, cal, date, mode="live"):
    """그 모드의 모든 포트폴리오를 date 까지 정산한다 (예비 배치가 당일 18:30 에 부른다).

    date 는 '일봉이 확정된 날'이어야 한다. 시점 규칙의 판정은 부르는 쪽이 하고(설계 2.3),
    여기서는 시세가 없는 날을 만나면 그 포트폴리오의 정산을 멈추는 것으로 방어한다.
    """
    kinds = asset_kinds(store, cfg)
    reports = [settle_portfolio(store, cfg, cal, pid, date, kinds) for pid in portfolio_ids(store, mode)]
    store.commit()
    return reports


# ---------------------------------------------------------------- 요약 지표

def _summary_from_series(dates, navs, turnovers, costs, start_nav, days_per_year):
    """NAV 시계열 → 수익률·연율 변동성·최대 낙폭·회전율·비용."""
    if not navs:
        return {"n_days": 0, "nav": start_nav, "total_return": 0.0, "ann_vol": None,
                "max_drawdown": 0.0, "turnover": 0.0, "cost": 0.0,
                "start_date": None, "end_date": None}
    series = [start_nav] + list(navs)
    rets = [series[i] / series[i - 1] - 1.0 for i in range(1, len(series)) if series[i - 1]]
    vol = None
    if len(rets) >= 2:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        vol = math.sqrt(var) * math.sqrt(float(days_per_year))
    peak, mdd = series[0], 0.0
    for v in series:
        peak = max(peak, v)
        if peak:
            mdd = min(mdd, v / peak - 1.0)
    return {"n_days": len(navs), "nav": series[-1], "total_return": series[-1] / start_nav - 1.0,
            "ann_vol": vol, "max_drawdown": mdd, "turnover": float(sum(turnovers)),
            "cost": float(sum(costs)), "start_date": dates[0] if dates else None,
            "end_date": dates[-1] if dates else None}


def summary(store, portfolio_id, cfg=None):
    """포트폴리오 성과 요약 (설계 10.1 의 /advisor/performance 가 쓰는 형태).

    회전율·비용은 체결 시점 NAV 대비 비율의 합이다(금액이 아니다). NAV 가 1 근처에 있는 동안
    둘은 사실상 같지만, 비율이라야 '하루 회전율 12%' 처럼 날짜별로 견줄 수 있다.
    """
    rows = store.conn.execute(
        "SELECT date, nav, turnover, cost FROM nav WHERE portfolio_id=? ORDER BY date",
        (portfolio_id,)).fetchall()
    out = _summary_from_series([r["date"] for r in rows], [float(r["nav"]) for r in rows],
                               [float(r["turnover"] or 0.0) for r in rows],
                               [float(r["cost"] or 0.0) for r in rows],
                               float(opt_cfg(cfg, "portfolio.start_nav")),
                               opt_cfg(cfg, "portfolio.trading_days_per_year"))
    out["portfolio_id"] = portfolio_id
    out["mode"] = mode_of(portfolio_id)
    return out


# ---------------------------------------------------------------- 메모리 안 재현 (재평가용)

def simulate(store, cfg, cal, targets, end=None, band=None, kinds=None):
    """DB 에 쓰지 않고 같은 체결·비용 모델로 NAV 를 만든다 (설계 7.3의 도전 안 비교).

    targets 는 {체결일: {자산: 목표 비중}}. 예약·정산을 거치지 않을 뿐, 하루치 회계는 settle() 과
    **같은 advance_day** 를 쓴다 — 비교 대상이 다른 코드로 계산되면 그 비교는 의미가 없다.
    """
    targets = {str(to_date(d)): t for d, t in (targets or {}).items()}
    start_nav = float(opt_cfg(cfg, "portfolio.start_nav"))
    dpy = opt_cfg(cfg, "portfolio.trading_days_per_year")
    if not targets:
        return {"records": [], "summary": _summary_from_series([], [], [], [], start_nav, dpy)}
    kinds = kinds if kinds is not None else asset_kinds(store, cfg)
    core = (cfg.get("universe") or {}).get("core_etf")
    band = float(cfg["allocate"]["rebalance_band"]) if band is None else float(band)
    start = to_date(min(targets))
    if end is None:
        end = store.conn.execute("SELECT MAX(date) FROM price_daily").fetchone()[0]
    end = to_date(end) if end else start
    state = {"nav": start_nav, "weights": {}, "prices": {}}
    records = []
    for day in cal.trading_days_between(start, end):
        ds = str(day)
        target = targets.get(ds)
        deltas = plan_trades(state["weights"], target, band, core) if target else {}
        assets = set(state["weights"]) | set(deltas)
        if not assets:
            continue
        bars = _bars(store, assets, ds)
        if not bars:
            break
        state, rec = advance_day(state, bars, deltas, cfg=cfg, kinds=kinds)
        records.append({"date": ds, "nav": rec["nav"], "turnover": rec["turnover"], "cost": rec["cost"],
                        "weights": rec["weights"], "skipped": sorted(rec["skipped"])})
    return {"records": records,
            "summary": _summary_from_series([r["date"] for r in records], [r["nav"] for r in records],
                                            [r["turnover"] for r in records],
                                            [r["cost"] for r in records], start_nav, dpy)}


__all__ = ["DEFAULTS", "REPLAY_SUFFIX", "SYSTEM_PORTFOLIOS", "opt_cfg", "known_bar_date", "start_day",
           "mode_of", "portfolio_id_for", "base_portfolio_id", "portfolio_ids", "asset_kinds",
           "plan_trades", "advance_day", "current_weights", "book_trades", "decision_for_fill",
           "book_system_portfolios", "settle", "settle_portfolio", "summary", "simulate"]
