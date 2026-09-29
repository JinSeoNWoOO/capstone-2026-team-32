"""국내 시세·수급 수집. 제공자를 갈아끼울 수 있게 **겉면 하나로 감싼 것**이 이 모듈의 전부다.

설계 12장이 미리 적어 둔 위험 — "pykrx 가 KRX 로그인 변경으로 동작하지 않음" — 이 실제로
일어났고(2026-01 변경), 지금은 `KRX_ID`/`KRX_PW` 로 로그인하면 된다. 로그인이 언제 다시 막힐지
모르므로 **두 경로를 모두 살려 둔다**.

| 데이터 | 로그인 있을 때 | 로그인 없을 때 |
|---|---|---|
| 종목 일봉 (뼈대) | 종목별 수정주가 (설정 `price_ticker_provider`) | 같음 — FinanceDataReader |
| 거래대금·시가총액 | 날짜별 전 종목 `get_market_ohlcv(date, market)` | **없음** (`value` NULL, `mktcap` 최근 일자만) |
| 코스피 지수 | pykrx `get_index_ohlcv("1001")` | KIS 지수 일봉 + FDR `KS11`(깊은 과거용, 이틀 지연) |
| 투자자 순매수 | pykrx 날짜별 전 종목 (원 단위) | KIS 종목별 매매동향 (백만원 → 원으로 환산) |

## 수정주가와 원주가를 나눠 받는 이유

2026-09-22 실측: pykrx 의 날짜별 전 종목 조회(`get_market_ohlcv(date, market)`)는 그날의
**원주가**다. 액면분할·병합이 있으면 `stk_high52`(250거래일 최고가 대비)와 200일 이동평균이
가짜로 깨진다. 그래서 **뼈대는 종목별 수정주가**로 받고(`close = adj_close`), 날짜별 전 종목
조회는 거래대금(`value`)·시가총액 보강에만 쓴다.

수정주가는 기업행위가 생기면 과거 값이 소급 변경된다. 증분 수집만 하면 그 변경이 영영 반영되지
않으므로, 이미 받아 둔 구간이라도 최근 `sources.adj_refresh_days` 만큼은 매 배치에서 다시 받는다.
값이 그대로인 행은 `knowledge_time` 이 유지되고 실제로 바뀐 행만 새 시각을 받는다 (store.py 규칙 2).

## 로그인 취급

pykrx 는 **import 시점에** 로그인을 시도하고, 실패하면 호출마다 다시 시도하며(계정 잠금 위험),
게다가 로그인 ID 를 stdout 에 찍는다. 그래서 자격 증명을 환경 변수에서 먼저 빼낸 뒤 import 하고,
우리 손으로 **한 번만** 로그인하고, 성공했을 때만 환경 변수를 되돌린다(세션 만료 시 자동 갱신용).
모든 pykrx 호출의 출력은 삼킨다.
"""
import contextlib
import io
import os
import socket
import time
from datetime import timedelta

from .base import (Report, SourceError, compact_date, env_value, guard_dated_rows, iso_date,
                   known_date_limit, load_env, missing_start, sources_cfg, to_date)
from .kis import KisClient, to_number

# market_daily 의 코스피 계열 이름. calendar.py 가 '그날 장이 열렸는가'의 근거로 쓰는 바로 그 계열이다.
from ..calendar import KOSPI_SERIES

# pykrx 지수 티커. 1001 = 코스피, 1028 = 코스피200.
PYKRX_KOSPI_INDEX = "1001"


@contextlib.contextmanager
def socket_timeout(seconds):
    """이 블록 안에서 새로 열리는 소켓에 기본 시간 제한을 건다.

    pykrx 는 요청에 timeout 을 받지 않는다. 그래서 KRX 가 SYN 에 응답하지 않으면 배치가
    **무한정 멈춘다** — 2026-09-22 실행에서 실제로 그랬다(210.89.168.x:443 이 SYN-SENT 에서
    10분 넘게 머물렀다). 07:40·18:30 에 끝나야 하는 배치에서 이것은 실패보다 나쁘다:
    실패는 대체 경로로 이어지지만(설계 3장) 멈춤은 그날 판단 자체를 없앤다.

    requests/urllib3 은 명시적 timeout 이 없을 때 소켓 기본값을 쓰므로, 호출을 감싸는 것만으로
    "연결이 안 되면 그 소스만 실패하고 배치는 계속된다"가 성립한다. 되돌리는 것까지가 이 함수다 —
    전역 값이라 그대로 두면 이후의 LLM·HTTP 호출에까지 남는다.
    """
    prev = socket.getdefaulttimeout()
    socket.setdefaulttimeout(float(seconds) if seconds else None)
    try:
        yield
    finally:
        socket.setdefaulttimeout(prev)


# 같은 배치 안에서만 사는 캐시 (목록 한 장, 날짜별 전 종목 몇 일치).
_LISTING_CACHE = {}
_BULK_CACHE = {}


class Providers:
    """어떤 제공자를 손에 들고 있는가. 테스트는 여기에 가짜 객체를 그대로 넣는다."""

    def __init__(self, pykrx=None, fdr=None, kis=None, krx_login=False, throttle=None,
                 timeout=None):
        self.pykrx = pykrx              # pykrx.stock 모듈 (로그인 성공했을 때만 채운다)
        self.fdr = fdr                  # FinanceDataReader 모듈
        self.kis = kis                  # KisClient
        self.krx_login = bool(krx_login)
        self._throttle = throttle       # pykrx 호출 간격 (초). None 이면 쉬지 않는다 (테스트)
        self._timeout = timeout         # pykrx 소켓 시간 제한 (초). None 이면 걸지 않는다 (테스트)
        self._last_krx = 0.0

    def require(self, name):
        got = getattr(self, name, None)
        if got is None:
            raise SourceError(f"제공자 {name} 를 쓸 수 없습니다")
        return got

    def krx_call(self, func, *args, **kwargs):
        """pykrx 호출 하나. 간격을 지키고, 시간 제한을 걸고, 출력을 삼킨다.

        (출력을 삼키는 것은 로그인 ID·휴일 안내가 로그에 남지 않게 하려는 것이고,
         시간 제한은 KRX 가 응답하지 않을 때 배치가 멈추지 않게 하려는 것이다 — `socket_timeout`.)
        """
        if self._throttle:
            gap = float(self._throttle) - (time.monotonic() - self._last_krx)
            if gap > 0:
                time.sleep(gap)
        sink = io.StringIO()
        try:
            with contextlib.redirect_stdout(sink), socket_timeout(self._timeout):
                return func(*args, **kwargs)
        finally:
            self._last_krx = time.monotonic()


def build_providers(cfg, env=None, report=None, try_krx_login=True, kis_transport=None):
    """실제 제공자를 만든다. 무거운 라이브러리는 전부 여기서 늦게 import 한다."""
    report = report if report is not None else Report()
    load_env(cfg)                       # .env 의 KRX_ID·KRX_PW·DART_API_KEY 를 올린다 (값은 찍지 않는다)
    pykrx_stock = _krx_login(cfg, env, report) if try_krx_login else None
    try:
        import FinanceDataReader as fdr
    except Exception as exc:                                    # 설치가 깨졌으면 대체 경로도 없다
        raise SourceError(f"FinanceDataReader 를 쓸 수 없습니다: {exc}") from exc
    return Providers(pykrx=pykrx_stock, fdr=fdr,
                     kis=KisClient(cfg, transport=kis_transport),
                     krx_login=pykrx_stock is not None,
                     throttle=float(sources_cfg(cfg, "krx_min_interval_sec")),
                     timeout=float(sources_cfg(cfg, "http_timeout_sec")))


def _krx_login(cfg, env, report):
    """KRX 로그인을 **한 번만** 시도하고, 성공하면 pykrx.stock 모듈을 준다. 실패하면 None.

    시간 제한을 걸고 부른다 (`socket_timeout`). 로그인 서버가 응답하지 않는 날에도 배치는
    대체 경로(`fallback_top_n`·KIS)로 끝까지 가야 한다 — 멈추면 그날 판단이 통째로 없어진다.
    """
    names = list(sources_cfg(cfg, "krx_login_env"))
    creds = [env_value(n, env) for n in names]
    if not all(creds):
        report.fallback("krx_login_missing")
        return None

    # 환경 변수를 먼저 빼낸다: 이 상태로 import 하면 pykrx 의 import 시점 자동 로그인이
    # "환경 변수 없음"으로 조용히 끝나고, 실제 로그인 시도는 아래 한 번뿐이 된다.
    saved = {n: os.environ.pop(n, None) for n in names}
    session, module = None, None
    sink = io.StringIO()
    timeout = float(sources_cfg(cfg, "http_timeout_sec"))
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink), \
                socket_timeout(timeout):
            from pykrx import stock as pykrx_stock
            from pykrx.website.comm import auth as krx_auth
            session = krx_auth.build_krx_session(creds[0], creds[1])
            if session is not None:
                krx_auth.set_auth_session(session)
                module = pykrx_stock
    except Exception as exc:
        report.fallback(f"krx_login_error:{type(exc).__name__}")
        session, module = None, None
    finally:
        if session is not None:
            # 세션이 만료되면 pykrx 가 스스로 갱신할 수 있게 되돌린다.
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value
    if module is None:
        report.fallback("krx_login_failed")
    return module


# ---------------------------------------------------------------- 목록·캐시

def kospi_listing(providers, refresh=False):
    """FDR 코스피 전 종목 목록 (**오늘 한 장**). 같은 배치 안에서는 한 번만 받는다.

    과거 날짜에 그대로 쓰면 안 된다 — 시가총액을 '가장 최근 일자'에만 붙이는 이유가 이것이다.
    """
    if refresh:
        _LISTING_CACHE.pop("KOSPI", None)
    if "KOSPI" not in _LISTING_CACHE:
        _LISTING_CACHE["KOSPI"] = providers.require("fdr").StockListing("KOSPI")
    return _LISTING_CACHE["KOSPI"]


def etf_listing(providers, refresh=False):
    """FDR 국내 ETF 목록 (이름을 얻으려고 쓴다)."""
    if refresh:
        _LISTING_CACHE.pop("ETF", None)
    if "ETF" not in _LISTING_CACHE:
        _LISTING_CACHE["ETF"] = providers.require("fdr").StockListing("ETF/KR")
    return _LISTING_CACHE["ETF"]


def clear_caches():
    """배치가 끝날 때·테스트 사이에 비운다."""
    _LISTING_CACHE.clear()
    _BULK_CACHE.clear()


def bulk_by_date(cfg, providers, day):
    """로그인 경로의 날짜별 전 종목: {코드: {"value": 거래대금, "mktcap": 시가총액}}.

    일봉 보강(거래대금)과 수급 보강(시가총액)이 같은 호출 하나를 쓰므로, 같은 배치 안에서는
    날짜당 한 번만 부른다. 캐시는 `sources.bulk_cache_days` 개까지만 들고 있는다.
    """
    key = iso_date(day)
    if key in _BULK_CACHE:
        return _BULK_CACHE[key]
    stock = providers.require("pykrx")
    frame = providers.krx_call(stock.get_market_ohlcv, compact_date(day),
                               sources_cfg(cfg, "krx_market"))
    out = {}
    for code, rec in _iter_frame(frame):
        out[code] = {"value": _num(rec.get("거래대금")), "mktcap": _num(rec.get("시가총액")),
                     "close_raw": _num(rec.get("종가"))}
    limit = int(sources_cfg(cfg, "bulk_cache_days"))
    while len(_BULK_CACHE) >= max(1, limit):
        _BULK_CACHE.pop(next(iter(_BULK_CACHE)))
    _BULK_CACHE[key] = out
    return out


# ---------------------------------------------------------------- 일봉

def fetch_prices(store, cfg, as_of, codes, providers, report=None, backfill_days=None,
                 write=True):
    """종목·ETF 일봉을 price_daily 에 넣는다. 반환 {"rows", "provider", "codes"}.

    뼈대는 종목별 **수정주가**(close = adj_close)다. 로그인이 있고 빈 구간이 짧으면 날짜별 전 종목
    조회로 거래대금을 덧입힌다.
    """
    report = report if report is not None else Report()
    limit = known_date_limit(cfg, as_of, "daily_bar")
    depth = int(backfill_days if backfill_days is not None else sources_cfg(cfg, "backfill_days"))
    refresh_days = int(sources_cfg(cfg, "adj_refresh_days"))
    codes = [str(c) for c in codes if c]

    # 이미 받아 둔 구간이라도 최근 refresh_days 는 다시 받는다 (수정주가 소급 변경 반영).
    refresh_from = limit - timedelta(days=refresh_days)
    plan = {}
    for code in codes:
        start = missing_start(store, "price_daily", code, limit, depth)
        if start is None:
            start = refresh_from
        else:
            start = min(start, refresh_from)
        plan[code] = start
    if not plan:
        return {"rows": 0, "provider": report.provider_of("price") or "none", "codes": 0}

    use_pykrx = providers.krx_login and sources_cfg(cfg, "price_ticker_provider") == "pykrx"
    rows = _prices_by_ticker(providers, plan, limit, use_pykrx=use_pykrx)
    report.used("price", "pykrx_ticker" if use_pykrx else "fdr")

    # 거래대금은 **최근 krx_bulk_max_days 일치만** 채운다. 날짜 하나당 한 번씩 부르는 조회라
    # 긴 백필에서 수백 번이 되는데, v0 요인 어디에도 쓰이지 않는 열에 그만한 부담을 줄 이유가 없다.
    bulk_days = int(sources_cfg(cfg, "krx_bulk_max_days"))
    if providers.krx_login and bulk_days > 0:
        _fill_value_from_krx(cfg, providers, rows, report, bulk_days)
    elif providers.kis is not None and sources_cfg(cfg, "price_value_from_kis"):
        _fill_value_from_kis(cfg, providers, rows, report)
    if any(r["value"] is None for r in rows):
        report.fallback("price_value_missing")

    rows = guard_dated_rows(rows, cfg, as_of, "daily_bar", label="price_daily")
    if write and rows:
        store.put_prices(rows)
    return {"rows": len(rows), "provider": report.provider_of("price"), "codes": len(plan)}


def _prices_by_ticker(providers, plan, limit, use_pykrx):
    """종목별 한 번씩. pykrx(adjusted=True) 와 FDR 은 같은 수정주가를 열 이름만 달리 준다."""
    rows = []
    for code, start in plan.items():
        if start > limit:
            continue
        try:
            if use_pykrx:
                stock = providers.require("pykrx")
                frame = providers.krx_call(stock.get_market_ohlcv, compact_date(start),
                                           compact_date(limit), code)
                cols = {"open": "시가", "high": "고가", "low": "저가", "close": "종가",
                        "volume": "거래량"}
            else:
                fdr = providers.require("fdr")
                frame = fdr.DataReader(code, start.isoformat(), limit.isoformat())
                cols = {"open": "Open", "high": "High", "low": "Low", "close": "Close",
                        "volume": "Volume"}
        except Exception as exc:
            raise SourceError(f"{code} 일봉 조회 실패: {exc}") from exc
        for day, rec in _iter_frame(frame, index_is_date=True):
            close = _num(rec.get(cols["close"]))
            if not close:
                continue                                  # 상장 전 구간은 0 이나 NaN 으로 온다
            rows.append({
                "code": code, "date": iso_date(day),
                "open": _num(rec.get(cols["open"])), "high": _num(rec.get(cols["high"])),
                "low": _num(rec.get(cols["low"])), "close": close,
                "volume": _num(rec.get(cols["volume"])), "value": None,
                "adj_close": close,
            })
    return rows


def _fill_value_from_krx(cfg, providers, rows, report, max_days):
    """거래대금을 날짜별 전 종목 조회로 채운다 (로그인 경로, 최근 max_days 일치만)."""
    days = sorted({to_date(r["date"]) for r in rows})[-int(max_days):]
    tables = {}
    for day in days:
        try:
            tables[day] = bulk_by_date(cfg, providers, day)
        except Exception:
            report.fallback("krx_bulk_failed")
            return
    hit = 0
    for row in rows:
        rec = tables.get(to_date(row["date"]), {}).get(row["code"])
        if rec and rec.get("value") is not None:
            row["value"] = rec["value"]
            hit += 1
    if hit:
        report.used("price", "pykrx_bulk_value")


def _fill_value_from_kis(cfg, providers, rows, report):
    """거래대금을 KIS 일봉으로 채운다 (설정으로 켤 때만 — 호출 수가 두 배가 된다)."""
    need = {}
    for row in rows:
        if row.get("value") is None:
            need.setdefault(row["code"], []).append(row)
    if not need:
        return
    max_rows = int(sources_cfg(cfg, "kis_chart_max_rows"))
    max_pages = int(sources_cfg(cfg, "dart_max_pages"))
    for code, group in need.items():
        days = [to_date(r["date"]) for r in group]
        try:
            fetched = _kis_chart_pages(
                lambda s, e, c=code: providers.kis.daily_chart(c, s, e),
                min(days), max(days), max_rows, max_pages)
        except Exception:
            report.fallback("kis_value_unavailable")
            continue
        by_date = {to_date(r["stck_bsop_date"]): r for r in fetched}
        for row in group:
            rec = by_date.get(to_date(row["date"]))
            if rec is not None:
                row["value"] = to_number(rec.get("acml_tr_pbmn"))
    report.used("price", "kis_value")


def _kis_chart_pages(fetch, start, end, max_rows, max_pages):
    """KIS 기간별 시세를 뒤에서부터 페이지로 훑는다.

    구간을 아무리 넓게 줘도 최근 max_rows 행만 오므로(2026-09-22 실측 50행), 받은 것 중 가장
    이른 날짜 하루 앞을 새 끝으로 삼아 되풀이한다. 더 못 줄이면 멈춘다 (무한 루프 방지).
    """
    start, end = to_date(start), to_date(end)
    seen, cursor = {}, end
    for _ in range(int(max_pages)):
        if cursor < start:
            break
        rows = fetch(start, cursor) or []
        dates = []
        for row in rows:
            key = row.get("stck_bsop_date")
            if not key:
                continue
            day = to_date(key)
            if start <= day <= end:
                seen.setdefault(day, row)
            dates.append(day)
        if not dates:
            break
        earliest = min(dates)
        if earliest <= start or len(rows) < max_rows:
            break
        cursor = earliest - timedelta(days=1)
    return [seen[d] for d in sorted(seen)]


# ---------------------------------------------------------------- 코스피 지수

def fetch_index(store, cfg, as_of, providers, report=None, years=None, write=True):
    """코스피 지수 종가를 market_daily(series='KOSPI') 에 넣는다.

    지수는 시장 요인의 **과거 대비 백분위**(설계 5.1) 재료라 5년 이상이 필요하다. pykrx 의 긴
    구간 조회는 느리므로(2026-09-22 실측: 5년치 9.2초) 백필은 한 번, 이후는 증분만 받는다.
    로그인이 없으면 깊은 과거를 FDR `KS11` 로 받고, FDR 이 이틀쯤 늦는 꼬리를 KIS 지수 일봉으로 덮는다.
    """
    report = report if report is not None else Report()
    limit = known_date_limit(cfg, as_of, "daily_bar")
    depth_years = int(years if years is not None else sources_cfg(cfg, "index_backfill_years"))
    start = missing_start(store, "market_daily", KOSPI_SERIES, limit,
                          depth_years * 365, code_col="series")
    if start is None:
        return {"rows": 0, "provider": report.provider_of("index") or "none"}

    values = {}
    if providers.krx_login:
        try:
            stock = providers.require("pykrx")
            frame = providers.krx_call(stock.get_index_ohlcv, compact_date(start),
                                       compact_date(limit), PYKRX_KOSPI_INDEX)
            for day, rec in _iter_frame(frame, index_is_date=True):
                close = _num(rec.get("종가"))
                if close:
                    values[to_date(day)] = close
            report.used("index", "pykrx")
        except Exception as exc:
            report.fallback(f"index_pykrx_failed:{type(exc).__name__}")

    if not values:
        try:
            fdr = providers.require("fdr")
            frame = fdr.DataReader("KS11", start.isoformat(), limit.isoformat())
            for day, rec in _iter_frame(frame, index_is_date=True):
                close = _num(rec.get("Close"))
                if close:
                    values[to_date(day)] = close
            report.used("index", "fdr")
        except Exception as exc:
            report.fallback(f"index_fdr_failed:{type(exc).__name__}")
        if providers.kis is not None:
            try:
                tail_start = min(max(values) + timedelta(days=1), limit) if values else start
                fetched = _kis_chart_pages(
                    lambda s, e: providers.kis.index_chart(sources_cfg(cfg, "kis_index_code"), s, e),
                    tail_start, limit, int(sources_cfg(cfg, "kis_chart_max_rows")),
                    int(sources_cfg(cfg, "dart_max_pages")))
                for rec in fetched:
                    close = to_number(rec.get("bstp_nmix_prpr"))
                    volume = to_number(rec.get("acml_vol"))
                    if close and volume:      # 거래량 0 인 당일 미완성 행은 지수 값이 아니다
                        values[to_date(rec["stck_bsop_date"])] = close
                report.used("index", "kis")
            except Exception as exc:
                report.fallback(f"index_kis_failed:{type(exc).__name__}")

    rows = [{"series": KOSPI_SERIES, "date": iso_date(d), "value": v}
            for d, v in values.items() if start <= d <= limit]
    rows = guard_dated_rows(rows, cfg, as_of, "daily_bar", label="market_daily:KOSPI")
    if write and rows:
        store.put_market(rows)
    return {"rows": len(rows), "provider": report.provider_of("index")}


# ---------------------------------------------------------------- 투자자 순매수·시가총액

def fetch_flows(store, cfg, as_of, codes, providers, report=None, backfill_days=None,
                write=True):
    """외국인·기관 순매수(원)와 시가총액을 flow_daily 에 넣는다.

    순매수는 **금액(원)** 이다. pykrx 의 `순매수거래대금` 은 이미 원 단위이고, KIS 의 `*_tr_pbmn`
    은 백만원 단위라 `sources.kis_net_value_unit_krw` 배수로 환산한다 (2026-09-22 검산:
    삼성전자 9/21 외국인 4,080,410주 × 274,000원 ≒ 1.118e12원, KIS 응답 1,106,291 → 배수 1e6).
    """
    report = report if report is not None else Report()
    limit = known_date_limit(cfg, as_of, "daily_bar")
    depth = int(backfill_days if backfill_days is not None else sources_cfg(cfg, "backfill_days"))
    codes = [str(c) for c in codes if c]

    plan = {}
    for code in codes:
        start = missing_start(store, "flow_daily", code, limit, depth)
        if start is not None:
            plan[code] = start
    if not plan:
        return {"rows": 0, "provider": report.provider_of("flow") or "none", "codes": 0}

    if providers.krx_login:
        rows = _flows_pykrx(cfg, providers, plan, limit, report)
        report.used("flow", "pykrx")
    else:
        rows = _flows_kis(cfg, providers, plan, limit, report)
        report.used("flow", "kis")
        _attach_latest_mktcap(providers, rows, limit, report)

    rows = guard_dated_rows(rows, cfg, as_of, "daily_bar", label="flow_daily")
    if write and rows:
        store.put_flows(rows)
    return {"rows": len(rows), "provider": report.provider_of("flow"), "codes": len(plan)}


def _flows_pykrx(cfg, providers, plan, limit, report):
    """로그인 경로: 날짜마다 외국인·기관 두 번.

    `get_market_net_purchases_of_equities` 는 **기간 합계**를 주므로 하루 단위 행을 만들려면
    from=to 로 날짜마다 불러야 한다 (백필 300거래일 ≒ 600회). 시가총액은 날짜별 전 종목 조회를
    같이 쓰는데, 이미 일봉 보강에서 불렀다면 캐시가 답한다.
    """
    stock = providers.require("pykrx")
    market = sources_cfg(cfg, "krx_market")
    investors = (("foreign_net", sources_cfg(cfg, "krx_investor_foreign")),
                 ("inst_net", sources_cfg(cfg, "krx_investor_institution")))
    wanted = set(plan)
    rows = []
    day = min(plan.values())
    while day <= limit:
        stamp = compact_date(day)
        nets = {}
        for label, investor in investors:
            frame = providers.krx_call(stock.get_market_net_purchases_of_equities,
                                       stamp, stamp, market, investor)
            for code, rec in _iter_frame(frame):
                if code in wanted and plan[code] <= day:
                    nets.setdefault(code, {})[label] = _num(rec.get("순매수거래대금"))
        if nets:
            try:
                caps = bulk_by_date(cfg, providers, day)
            except Exception:
                report.fallback("mktcap_bulk_failed")
                caps = {}
            for code, net in nets.items():
                rows.append({"code": code, "date": iso_date(day),
                             "foreign_net": net.get("foreign_net"),
                             "inst_net": net.get("inst_net"),
                             "mktcap": (caps.get(code) or {}).get("mktcap")})
        day += timedelta(days=1)
    return rows


def _flows_kis(cfg, providers, plan, limit, report):
    """무로그인 경로: 종목마다 한 번 (응답에 최근 30영업일이 통째로 들어 있다)."""
    unit = float(sources_cfg(cfg, "kis_net_value_unit_krw"))
    kis = providers.require("kis")
    rows = []
    for code, start in plan.items():
        try:
            records = kis.investor_daily(code)
        except Exception as exc:
            report.fallback(f"flow_kis_failed:{code}")
            raise SourceError(f"{code} 투자자 매매동향 조회 실패: {exc}") from exc
        for rec in records:
            day = to_date(rec["stck_bsop_date"])
            if day < start or day > limit:
                continue
            foreign = to_number(rec.get("frgn_ntby_tr_pbmn"))
            inst = to_number(rec.get("orgn_ntby_tr_pbmn"))
            if foreign is None and inst is None:
                continue                                  # 장중 조회 시의 오늘 행은 값이 '' 다
            rows.append({"code": code, "date": iso_date(day),
                         "foreign_net": None if foreign is None else foreign * unit,
                         "inst_net": None if inst is None else inst * unit,
                         "mktcap": None})
    return rows


def _attach_latest_mktcap(providers, rows, limit, report):
    """무로그인 경로의 시가총액: FDR 목록의 오늘치를 **가장 최근 일자에만** 붙인다.

    과거 날짜에 오늘 시가총액을 붙이면 과거 수급 비율이 조용히 틀어진다. 없는 값은 없는 채로 둔다 —
    요인 코드는 "그 종목의 가장 최근 시가총액"을 분모로 쓰면 된다.
    """
    if not rows:
        return
    latest = max(to_date(r["date"]) for r in rows)
    if latest != to_date(limit):
        return
    try:
        listing = kospi_listing(providers)
        caps = {str(c).zfill(6): float(m)
                for c, m in zip(listing["Code"], listing["Marcap"]) if m == m}
    except Exception:
        report.fallback("mktcap_unavailable")
        return
    hit = 0
    for row in rows:
        if to_date(row["date"]) == latest and row["code"] in caps:
            row["mktcap"] = caps[row["code"]]
            hit += 1
    if hit:
        report.used("mktcap", "fdr_listing_today")
        report.fallback("mktcap_latest_date_only")


# ---------------------------------------------------------------- 표 다루기

def _num(value):
    """pandas 값 → float | None. NaN 과 파싱 불가를 결측으로 본다."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


def _iter_frame(frame, index_is_date=False):
    """DataFrame → (키, {열: 값}) 반복. 키는 티커거나 날짜다.

    pandas 를 이 함수 안에만 가둔다 — 제공자가 바뀌어도 위 로직이 그대로다.
    가짜 제공자는 (키, dict) 목록을 그대로 돌려줘도 된다.
    """
    if frame is None:
        return
    if isinstance(frame, (list, tuple)):
        for key, rec in frame:
            yield (to_date(key) if index_is_date else str(key).zfill(6)), dict(rec)
        return
    if getattr(frame, "empty", False):
        return
    columns = list(frame.columns)
    for key, values in zip(frame.index, frame.to_numpy()):
        rec = dict(zip(columns, values))
        yield (to_date(key) if index_is_date else str(key).zfill(6)), rec


__all__ = ["Providers", "build_providers", "fetch_prices", "fetch_index", "fetch_flows",
           "bulk_by_date", "kospi_listing", "etf_listing", "clear_caches", "KOSPI_SERIES",
           "PYKRX_KOSPI_INDEX"]
