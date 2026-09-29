"""그날의 대상 목록 스냅샷 (결정 8, 설계 8장의 `universe` 테이블).

날마다 다시 만들어 저장하는 이유는 **지수 편입·편출이 사후에 보이게** 하기 위해서다. 오늘의
코스피200 으로 과거를 돌리면 생존 편향이 생기는데(설계 7.4), 적어도 오늘 이후로는 그날의 목록이
그대로 남아 "그때 후보가 무엇이었나"를 되짚을 수 있다.

## 세 가지를 정한다

1. **종목**: 코스피200 구성 종목. pykrx 로그인이 되면 `get_index_portfolio_deposit_file("1028")`,
   안 되면 코스피 시가총액 상위 `universe.fallback_top_n` 으로 대신한다. 대체 경로는 결정 8 에서
   벗어난 것이라 요약의 `fallbacks_used` 에 반드시 남는다.
2. **ETF**: 설정의 `core_etf`·`cash_etf`·`sector_etfs`·`other_etfs`. 이름은 FDR ETF 목록에서 가져온다.
   `kind` 는 현금 대용만 `cash_etf`, 나머지 ETF 는 `etf`, 주식은 `stock`.
3. **섹터**: 섹터 ETF 구성 종목(PDF) ∩ 우리 종목 범위. 멤버가 `universe.min_sector_members`
   보다 적은 섹터는 설정의 수작업 표로 보강한다 — 2026-09-22 확인 결과 KODEX 반도체 22종목 중
   코스피200 에 드는 것은 4개뿐이었다(나머지는 코스닥). 두세 종목으로 섹터 점수를 내면 순위가 잡음이 된다.
   한 종목은 **한 섹터에만** 들어간다 (설정의 등장 순서가 이긴다).

## 우선주·리츠를 빼는 규칙 (대체 경로에서만)

2026-09-22 확인: 코스피 942종목 중 **코드 끝자리가 '0' 이 아닌 111종목이 정확히 이름에 '우'가 든
우선주 집합과 일치**했다. 그래서 끝자리 규칙 하나로 우선주가 걸러진다. 여기에 스팩·리츠·인프라
펀드를 이름 패턴(`universe.exclude_name_patterns`)으로 뺀다 — '메리츠'가 '리츠'에 걸리지 않게
앞 글자를 확인하는 정규식이다.
"""
import re

from .sources.base import Report, cfg_get, compact_date, iso_date, to_date

PYKRX_KOSPI200 = "1028"

KIND_STOCK = "stock"
KIND_ETF = "etf"
KIND_CASH_ETF = "cash_etf"


def configured_etfs(cfg):
    """설정의 ETF 를 {코드: (kind, sector)} 로 편다. 섹터 ETF 는 자기 섹터 이름을 갖는다."""
    uni = cfg_get(cfg, "universe")
    out = {}
    for sector, code in (uni.get("sector_etfs") or {}).items():
        out[str(code)] = (KIND_ETF, sector)
    for code in (uni.get("other_etfs") or {}).values():
        out.setdefault(str(code), (KIND_ETF, None))
    out[str(uni["core_etf"])] = (KIND_ETF, None)
    out[str(uni["cash_etf"])] = (KIND_CASH_ETF, None)
    return out


# ---------------------------------------------------------------- 종목 목록

def kospi200_codes(cfg, providers, as_of_date, report):
    """코스피200 구성 종목. 로그인이 없으면 시가총액 상위로 대신한다."""
    if providers.krx_login:
        try:
            stock = providers.require("pykrx")
            codes = providers.krx_call(stock.get_index_portfolio_deposit_file,
                                       PYKRX_KOSPI200, compact_date(as_of_date))
            codes = [str(c).zfill(6) for c in (codes or [])]
            if codes:
                report.used("universe_stocks", "pykrx_kospi200")
                return codes
            report.fallback("kospi200_empty")
        except Exception as exc:
            report.fallback(f"kospi200_failed:{type(exc).__name__}")
    else:
        report.fallback("kospi200_no_login")
    return fallback_top_codes(cfg, providers, report)


def fallback_top_codes(cfg, providers, report):
    """코스피 시가총액 상위 N — 결정 8 의 코스피200 을 대신하는 것이라 반드시 기록으로 남긴다."""
    from .sources import krx
    uni = cfg_get(cfg, "universe")
    top_n = int(uni["fallback_top_n"])
    listing = krx.kospi_listing(providers)
    etfs = set(configured_etfs(cfg))
    patterns = [re.compile(p) for p in (uni.get("exclude_name_patterns") or [])]
    drop_nonzero = bool(uni.get("exclude_name_suffix_nonzero_code", True))

    rows = []
    for code, name, cap in zip(listing["Code"], listing["Name"], listing["Marcap"]):
        code = str(code).zfill(6)
        name = str(name)
        if code in etfs:
            continue
        if drop_nonzero and not code.endswith("0"):
            continue                                  # 우선주 (2026-09-22 확인: 끝자리 규칙과 일치)
        if any(p.search(name) for p in patterns):
            continue                                  # 스팩·리츠·인프라 펀드
        if cap != cap or cap is None:
            continue
        rows.append((float(cap), code))
    rows.sort(reverse=True)
    codes = [code for _, code in rows[:top_n]]
    report.used("universe_stocks", "fdr_top_marcap")
    report.fallback("universe_fallback_top_marcap")
    return codes


def stock_names(providers, codes):
    """코드 → 이름. FDR 코스피 목록에서 가져오고, 없으면 코드를 그대로 이름으로 쓴다."""
    from .sources import krx
    try:
        listing = krx.kospi_listing(providers)
        table = {str(c).zfill(6): str(n) for c, n in zip(listing["Code"], listing["Name"])}
    except Exception:
        table = {}
    return {code: table.get(code, code) for code in codes}


def etf_names(providers, codes):
    """ETF 코드 → 이름."""
    from .sources import krx
    try:
        listing = krx.etf_listing(providers)
        table = {str(s).zfill(6): str(n) for s, n in zip(listing["Symbol"], listing["Name"])}
    except Exception:
        table = {}
    return {code: table.get(code, code) for code in codes}


# ---------------------------------------------------------------- 섹터 매핑

def sector_map(cfg, providers, codes, as_of_date, report):
    """{종목코드: 섹터}. 섹터 ETF 구성 종목을 먼저 쓰고 모자라면 수작업 표로 보강한다.

    반환은 (매핑, 섹터별 멤버 수) — 멤버 수는 요약에 남겨, 섹터 점수가 몇 종목으로 나온 것인지
    사후에 알 수 있게 한다.
    """
    uni = cfg_get(cfg, "universe")
    sectors = list((uni.get("sector_etfs") or {}).keys())
    manual = uni.get("sector_map_manual") or {}
    min_members = int(uni.get("min_sector_members", 0) or 0)
    pool = set(codes)

    from_pdf = {}
    if providers.krx_login:
        stock = providers.require("pykrx")
        for sector in sectors:
            etf_code = str(uni["sector_etfs"][sector])
            try:
                frame = providers.krx_call(stock.get_etf_portfolio_deposit_file,
                                           etf_code, compact_date(as_of_date))
                members = [str(c).zfill(6) for c, _ in _iter_index(frame)]
            except Exception:
                report.fallback(f"sector_pdf_failed:{sector}")
                members = []
            from_pdf[sector] = [c for c in members if c in pool]
        if any(from_pdf.values()):
            report.used("universe_sector", "etf_pdf")
    else:
        report.fallback("sector_pdf_no_login")

    mapping, counts = {}, {}
    used_manual = []
    for sector in sectors:
        members = list(from_pdf.get(sector) or [])
        if len(members) < min_members:
            extra = [str(c).zfill(6) for c in (manual.get(sector) or []) if str(c).zfill(6) in pool]
            if extra:
                used_manual.append(sector)
            members = list(dict.fromkeys(members + extra))
        assigned = 0
        for code in members:
            if code in mapping:
                continue                              # 한 종목은 한 섹터에만 (설정 순서가 이긴다)
            mapping[code] = sector
            assigned += 1
        counts[sector] = assigned
    if used_manual:
        report.used("universe_sector", "manual_table")
        report.fallback("sector_manual_table:" + ",".join(used_manual))
    return mapping, counts


def _iter_index(frame):
    """DataFrame 의 (인덱스, 행) 또는 (키, dict) 목록을 같은 모양으로 흘린다."""
    if frame is None:
        return
    if isinstance(frame, (list, tuple)):
        for key, rec in frame:
            yield key, rec
        return
    if getattr(frame, "empty", False):
        return
    for key in frame.index:
        yield key, None


# ---------------------------------------------------------------- 스냅샷

def build_universe(cfg, providers, as_of_date, report=None):
    """그날의 목록 행과 요약을 만든다 (저장은 하지 않는다 — 테스트가 쉬워진다)."""
    report = report if report is not None else Report()
    as_of_date = to_date(as_of_date)
    codes = kospi200_codes(cfg, providers, as_of_date, report)
    mapping, counts = sector_map(cfg, providers, codes, as_of_date, report)
    names = stock_names(providers, codes)

    etfs = configured_etfs(cfg)
    enames = etf_names(providers, list(etfs))
    report.used("universe_etf", "config+fdr_etf_listing")

    rows = [{"code": code, "name": names.get(code, code), "kind": KIND_STOCK,
             "sector": mapping.get(code)} for code in codes]
    rows += [{"code": code, "name": enames.get(code, code), "kind": kind, "sector": sector}
             for code, (kind, sector) in etfs.items()]
    summary = {"stocks": len(codes), "etfs": len(etfs), "sector_members": counts,
               "provider": report.provider_of("universe_stocks")}
    return rows, summary


def snapshot(store, cfg, providers, as_of, report=None, write=True):
    """목록을 만들어 universe 테이블에 넣는다. 반환 {"rows", "provider", "stocks", ...}."""
    report = report if report is not None else Report()
    as_of_date = to_date(as_of)
    rows, summary = build_universe(cfg, providers, as_of_date, report)
    if write and rows:
        store.put_universe(iso_date(as_of_date), rows)
    summary["rows"] = len(rows)
    return summary


def stored_codes(store, as_of_date, kinds=None):
    """저장된 그날 목록에서 코드를 읽는다 (수집 단계들이 대상 코드를 여기서 받는다)."""
    rows = store.conn.execute(
        "SELECT code, kind FROM universe WHERE as_of_date=?", (iso_date(as_of_date),)).fetchall()
    if kinds is None:
        return [r["code"] for r in rows]
    wanted = set(kinds)
    return [r["code"] for r in rows if r["kind"] in wanted]


def latest_universe_date(store, as_of_date):
    """기준일 이하의 가장 최근 목록 날짜. 없으면 None.

    목록 만들기가 실패한 날에도 어제 목록으로 수집을 이어 가려고 쓴다 — 목록 하나 때문에
    그날 시세·수급을 통째로 잃는 것이 더 나쁘다.
    """
    row = store.conn.execute(
        "SELECT MAX(as_of_date) FROM universe WHERE as_of_date<=?",
        (iso_date(as_of_date),)).fetchone()
    return row[0] if row and row[0] else None


__all__ = ["PYKRX_KOSPI200", "KIND_STOCK", "KIND_ETF", "KIND_CASH_ETF", "configured_etfs",
           "kospi200_codes", "fallback_top_codes", "sector_map", "build_universe", "snapshot",
           "stored_codes", "stock_names", "etf_names"]
