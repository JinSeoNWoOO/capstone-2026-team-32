"""시점 규칙(설계 2.3)과 as_of 안전 읽기. 요인 계산이 저장소를 읽는 유일한 통로다.

> 판단에 쓰는 모든 데이터의 시각 < 판단 시각 < 가상 체결 시각

`store.prices(code, end_date, n)` 같은 foundation 의 읽기 함수는 "end_date 이후를 넘기지 않는다"까지만
보장한다. 그런데 **end_date 를 무엇으로 줄지**가 곧 시점 규칙이다. 판단 시각이 T일 07:40 인데
end_date 를 T일로 주면 아직 존재하지도 않는 그날 일봉을 읽게 된다 — 조사한 FinAgent 공식 코드가
플래그 기본값 실수로 차트에 미래 14거래일을 흘린 것이 정확히 이 자리다.

그래서 **as_of 에서 '마지막으로 쓸 수 있는 날짜'를 계산해** 그 날짜를 end_date 로 넘긴다.

| 계열 | 언제부터 쓸 수 있는가 | 근거 |
|---|---|---|
| 국내 일봉·수급·코스피 | D일 자료는 D일 18:00 이후 | 확정 수급이 18시 이후에 나온다 (결정 11) |
| 미국 지수 | D일 자료는 D+1일 07:00 이후 | 미국 장 마감이 한국 시간 05~06시, 07:40 최종 판단 전에 확보된다 |
| 원/달러 환율 | D일 자료는 D+1일 00:00 이후 | 24시간 거래라 당일 봉이 항상 미완성이다 (`sources.fx_known_at`) |

두 단계에 이 표를 대입하면 설계 2.3 의 표가 그대로 나온다.
  - 예비 T일 18:30 → 국내 T일까지, 미국 T−1일까지 (밤사이 요인은 애초에 prelim 에서 결측이다)
  - 최종 T+1일 07:40 → 국내 T일까지, 미국 T일까지

읽기 함수는 SQL 로 한 번 거르고 **반환 직전에 다시 assert** 한다. 조건을 빠뜨린 쿼리가 하나
섞여도 그날 기록 전체가 조용히 오염되는 대신 배치가 죽는 쪽이 낫다.
"""
import math
from datetime import date, datetime, time, timedelta, timezone

# 시각은 KST naive 문자열로 다룬다 (store.py 와 같은 형식). 오프셋이 붙은 값만 여기서 변환한다.
KST = timezone(timedelta(hours=9))

# 국내 일봉은 D일 18:00, 미국 일봉은 D+1일 07:00 부터 쓸 수 있다 (위 표).
# **수집 쪽과 같은 값을 봐야 한다** — 수집이 18:30 에 당일 봉을 받아 적는데 읽기가 19:00 를
# 기준으로 잡으면 그날 자료가 통째로 사라지고, 반대면 미래를 본다. 그래서 설정의
# sources.* 가 있으면 그 값이 이기고, 없을 때만 아래 기본값을 쓴다.
# 키 이름과 규칙(며칠 뒤 그 시각)은 `sources/base.py` 의 KNOWN_AT_RULES 와 한 벌이다.
KR_READY_HOUR = 18
US_READY_HOUR = 7
FX_READY_HOUR = 0
DEFAULTS = {
    "sources.daily_bar_known_at": f"{KR_READY_HOUR:02d}:00",
    "sources.us_bar_known_at": f"{US_READY_HOUR:02d}:00",
    "sources.fx_known_at": f"{FX_READY_HOUR:02d}:00",
}

# 24시간 거래라 '그날 봉'이 항상 미완성인 계열 (market_daily.series 의 이름).
# 수집은 이 계열만 fx 규칙으로 걸러 저장하므로, 읽기가 미국 지수와 같은 규칙을 쓰면
# 새벽 07:00 이전에 물었을 때 "수집은 넣었는데 읽기는 못 보는" 하루가 생긴다.
FX_SERIES = ("USDKRW",)


class AsOfViolation(AssertionError):
    """as_of 이후의 데이터가 읽혔다. 판단을 계속하지 않고 여기서 죽는다 (설계 2.3의 1번 규칙)."""


def to_datetime(x):
    """date | datetime | 'YYYY-MM-DD[THH:MM[:SS]]' → KST naive datetime.

    시각이 없는 날짜는 그날 00:00 으로 본다. 그래야 '날짜만 준 as_of'가 그날 어떤 자료도
    쓰지 않는 가장 보수적인 값이 된다 (run.py 가 단계별 기본 시각을 채워 넣는다).
    """
    if isinstance(x, datetime):
        return x.replace(tzinfo=None) if x.tzinfo is None else x.astimezone(KST).replace(tzinfo=None)
    if isinstance(x, date):
        return datetime(x.year, x.month, x.day)
    if isinstance(x, str):
        s = x.strip().replace(" ", "T")
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
    raise TypeError(f"시각으로 볼 수 없습니다: {x!r}")


def _ready_time(cfg, key):
    """설정의 "HH:MM" → time. 값이 없거나 이상하면 기본 경계 시각으로 물러선다."""
    raw = str(tunable(cfg or {}, key, DEFAULTS))
    try:
        hh, mm = raw.split(":")[:2]
        return time(int(hh), int(mm))
    except (ValueError, TypeError):
        hh, mm = DEFAULTS[key].split(":")
        return time(int(hh), int(mm))


def _known_before(as_of, cfg, key, days_after):
    """'D일 자료는 D+days_after 일 그 시각에 확정' 규칙을 as_of 에 대입한 마지막 날짜."""
    t = to_datetime(as_of)
    base = t.date() - timedelta(days=int(days_after))
    return base if t.time() >= _ready_time(cfg, key) else base - timedelta(days=1)


def last_kr_date(as_of, cfg=None):
    """as_of 시점에 쓸 수 있는 마지막 **국내** 일봉 날짜. 18:00 이 경계다."""
    return _known_before(as_of, cfg, "sources.daily_bar_known_at", 0)


def last_us_date(as_of, cfg=None):
    """as_of 시점에 쓸 수 있는 마지막 **미국** 일봉 날짜. D+1일 07:00 이 경계다."""
    return _known_before(as_of, cfg, "sources.us_bar_known_at", 1)


def last_fx_date(as_of, cfg=None):
    """as_of 시점에 쓸 수 있는 마지막 **환율** 날짜. D+1일 `sources.fx_known_at` 이 경계다.

    24시간 거래라 기준일 당일 봉은 어느 단계에서도 쓰지 않는다 (수집 쪽 규칙과 같다).
    """
    return _known_before(as_of, cfg, "sources.fx_known_at", 1)


def last_series_date(series, as_of, cfg=None):
    """밤사이 계열(미국 지수·환율)의 **계열별** 경계.

    같은 as_of 라도 환율과 미국 지수는 확정 시각이 다르다. 한 경계로 뭉뚱그리면 07:00 이전에
    돌린 배치에서 환율만 하루 사라지거나(수집은 넣었는데 못 읽는다) 미국 지수가 하루 새어
    들어온다. 수집(`sources/base.py`)이 계열마다 다른 규칙으로 거르므로 읽기도 그대로 따른다.
    """
    return (last_fx_date(as_of, cfg) if str(series).upper() in FX_SERIES
            else last_us_date(as_of, cfg))


def to_date_str(d):
    """date | datetime | str → 'YYYY-MM-DD' (저장소의 date 열이 문자열이라 비교도 문자열로 한다)."""
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    return str(d).strip()[:10]


def assert_not_after(dates, cutoff, what):
    """읽어 온 날짜가 하나라도 cutoff 를 넘으면 AsOfViolation. 시점 규칙의 2차 방어선이다.

    dates 는 날짜 문자열(또는 date)의 모음이거나 None 을 포함할 수 있다. None 은 넘어간다 —
    날짜가 비어 있는 행은 애초에 요인 계산에 못 쓰고, 여기서 막을 문제도 아니다.
    """
    limit = to_date_str(cutoff)
    for d in dates or ():
        if d is None:
            continue
        ds = to_date_str(d)
        if ds > limit:
            raise AsOfViolation(f"{what}: 시점 규칙 위반 — {ds} 는 기준일 {limit} 이후입니다 (설계 2.3)")
    return True


def tunable(cfg, dotted, defaults):
    """설정값을 점 표기로 읽고, 없으면 모듈의 DEFAULTS 를 쓴다.

    advisor.config.yaml 은 수집 담당이 소유해 이 작업에서 고칠 수 없다. 그래서 아직 YAML 에 없는
    조정값은 모듈마다 DEFAULTS 한 곳에 모아 두고 여기로만 읽는다 — 나중에 YAML 에 같은 키를
    넣으면 코드를 안 고쳐도 그쪽이 이긴다.
    """
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return defaults[dotted]
        node = node[part]
    return defaults[dotted] if node is None else node


def min_obs(window, ratio):
    """창 길이 대비 최소 관측 수. 올림이라 ratio 가 0 보다 크면 최소 1 개는 요구한다."""
    return max(1, int(math.ceil(float(window) * float(ratio))))


# ------------------------------------------------------------------ 읽기 (전부 cutoff 로 자른다)

def price_bars(store, code, cutoff, n):
    """cutoff 까지의 마지막 n 거래일 일봉 (날짜 오름차순). 수익률·이동평균·고가는 adj_close 를 쓴다."""
    rows = store.prices(code, to_date_str(cutoff), int(n))
    assert_not_after((r["date"] for r in rows), cutoff, f"price_daily({code})")
    return rows


def market_values(store, series, cutoff, n=None):
    """cutoff 까지의 시장 계열 값 (날짜 오름차순). n 을 주면 마지막 n 개만."""
    rows = store.market_series(series, to_date_str(cutoff), n)
    assert_not_after((r["date"] for r in rows), cutoff, f"market_daily({series})")
    return rows


def flow_window(store, start_date, cutoff):
    """[start_date, cutoff] 구간의 종목별 순매수 합 {code: (합, 관측일수)}.

    외국인·기관 중 한쪽만 있는 행도 관측으로 세고 없는 쪽은 0 으로 본다 — 무료 대체 경로에서
    한쪽이 비는 일이 있는데 그 행을 통째로 버리면 합이 더 크게 왜곡된다.
    """
    lo, hi = to_date_str(start_date), to_date_str(cutoff)
    rows = store.conn.execute(
        "SELECT code, SUM(COALESCE(foreign_net,0)+COALESCE(inst_net,0)) AS net, "
        "SUM(CASE WHEN foreign_net IS NOT NULL OR inst_net IS NOT NULL THEN 1 ELSE 0 END) AS n, "
        "MAX(date) AS last_date FROM flow_daily WHERE date>=? AND date<=? GROUP BY code",
        (lo, hi)).fetchall()
    assert_not_after((r["last_date"] for r in rows), cutoff, "flow_daily")
    return {r["code"]: (r["net"], int(r["n"] or 0)) for r in rows}


def latest_mktcaps(store, cutoff):
    """cutoff 이전(포함)의 **마지막 비어 있지 않은** 시가총액 {code: mktcap}.

    무료 대체 경로에서는 과거 날짜의 mktcap 이 비고 최신 하루만 채워진다. 그래서 '그날의
    시가총액'이 아니라 '그때까지 알려진 마지막 시가총액'을 분모로 쓴다. 하나도 없으면 키가 없다
    (요인은 결측이 된다 — 값을 지어내지 않는다).
    """
    hi = to_date_str(cutoff)
    rows = store.conn.execute(
        "SELECT code, MAX(date) AS date, mktcap FROM flow_daily "
        "WHERE date<=? AND mktcap IS NOT NULL GROUP BY code", (hi,)).fetchall()
    assert_not_after((r["date"] for r in rows), cutoff, "flow_daily.mktcap")
    return {r["code"]: float(r["mktcap"]) for r in rows if r["mktcap"] is not None}


def fin_reports(store, as_of, codes=None):
    """as_of 시점에 **이미 알 수 있던** 정기보고서 주요계정 행 {code: [row…]} (2026-09-28 추가).

    일봉과 달리 재무 값은 '어느 날짜의 값'이 아니라 '언제 공시됐는가'가 시점이다. 그래서 날짜 경계가
    아니라 `knowledge_time`(공시일 다음 날 00:00, sources/dart_fin.py) 을 as_of 시각과 직접 비교한다.
    SQL 로 한 번 거르고 반환 직전에 다시 확인한다 (이 파일의 공통 규칙).
    표가 없는 옛 DB(읽기 전용으로 연 경우)는 빈 결과 — 요인이 결측이 될 뿐 판단은 계속된다.
    """
    import sqlite3
    t = to_datetime(as_of).isoformat(timespec="milliseconds")
    try:
        rows = store.conn.execute(
            "SELECT stock_code, bsns_year, reprt_code, fs_div, account, amount, cum_amount, rcept_no, "
            "knowledge_time FROM fin_quarterly WHERE knowledge_time IS NOT NULL AND knowledge_time<=?",
            (t,)).fetchall()
    except sqlite3.OperationalError:
        return {}
    want = set(codes) if codes is not None else None
    out = {}
    for r in rows:
        if str(r["knowledge_time"]) > t:
            raise AsOfViolation(f"fin_quarterly({r['stock_code']}): 시점 규칙 위반 — 인지 시각 "
                                f"{r['knowledge_time']} 가 판단 시각 {t} 이후입니다 (설계 2.3)")
        if want is None or r["stock_code"] in want:
            out.setdefault(r["stock_code"], []).append(r)
    return out


def universe_snapshot(store, cfg, as_of):
    """as_of 날짜 이하의 **가장 최근** 대상 목록 스냅샷을 쓰기 좋은 형태로.

    목록은 날짜마다 새로 쌓이므로(덧붙이기) 기준일 이후 스냅샷을 읽으면 그날 알 수 없었던
    편입·편출이 새어 들어온다. 그래서 여기서도 as_of 로 자른다.

    섹터 → ETF 매핑은 **설정이 먼저**다. 비중 계산(allocate)이 설정의 sector_etfs 로 담을 ETF 를
    고르므로, 점수의 섹터 이름과 설정의 키가 어긋나면 점수만 있고 담지 못하는 섹터가 생긴다.
    설정에 없는 섹터만 스냅샷의 ETF 행으로 채운다.
    """
    day = to_date_str(to_datetime(as_of).date())
    snap = store.conn.execute(
        "SELECT MAX(as_of_date) FROM universe WHERE as_of_date<=?", (day,)).fetchone()[0]
    rows = []
    if snap is not None:
        rows = store.conn.execute("SELECT * FROM universe WHERE as_of_date=? ORDER BY code",
                                  (snap,)).fetchall()
        assert_not_after([snap], day, "universe")

    stocks, etfs, sector_of, from_snapshot = {}, {}, {}, {}
    for r in rows:
        rec = {"code": r["code"], "name": r["name"], "kind": r["kind"], "sector": r["sector"]}
        if r["kind"] == "stock":
            stocks[r["code"]] = rec
            if r["sector"]:
                sector_of[r["code"]] = r["sector"]
        else:
            etfs[r["code"]] = rec
            if r["sector"] and r["sector"] not in from_snapshot:
                from_snapshot[r["sector"]] = r["code"]

    uni_cfg = cfg.get("universe") or {}
    sector_etf = dict(from_snapshot)
    sector_etf.update({s: c for s, c in (uni_cfg.get("sector_etfs") or {}).items()})
    sectors = sorted(set(sector_of.values()) | set(sector_etf))
    return {
        "snapshot_date": snap,
        "stocks": stocks,
        "etfs": etfs,
        "sector_of": sector_of,
        "sector_etf": sector_etf,
        "sectors": sectors,
        "members": {s: sorted(c for c, sec in sector_of.items() if sec == s) for s in sectors},
        "assets": sorted(set(stocks) | set(etfs)),
    }


__all__ = ["AsOfViolation", "DEFAULTS", "FX_READY_HOUR", "FX_SERIES", "KR_READY_HOUR", "US_READY_HOUR",
           "assert_not_after", "fin_reports", "flow_window", "last_fx_date", "last_kr_date", "last_series_date",
           "last_us_date", "latest_mktcaps", "market_values", "min_obs",
           "price_bars", "to_date_str", "to_datetime", "tunable", "universe_snapshot"]
