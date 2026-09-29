"""밤사이 해외 시장 (설계 5.2의 `mkt_overnight`). yfinance → market_daily.

받는 것은 **종가 수준**뿐이다. 수익률은 요인 코드가 저장된 값으로 계산한다 — 수집이 수익률을
미리 계산해 버리면 "as_of 이전 데이터만 썼는가"를 사후에 검산할 수 없고, 기간을 바꿔 다시 볼 수도 없다.

| 계열 이름 | 티커 | 시점 규칙 |
|---|---|---|
| `SP500` | `^GSPC` | D일자 봉은 D+1일 07:00 KST 에 확정 (미국 장 마감이 한국 새벽 5~6시) |
| `SOX` | `^SOX` | 위와 같음 |
| `USDKRW` | `KRW=X` | 24시간 거래라 당일 봉이 항상 미완성 → **기준일보다 앞선 날짜만** |

코스피는 여기서 받지 않는다. `^KS11` 은 하루 늦게 갱신돼 예비 배치(18:30)가 당일 지수를 못 본다
(2026-09-22 확인). 코스피는 `krx.fetch_index` 가 KIS·pykrx 에서 받는다.
"""
from datetime import timedelta

from .base import (Report, SourceError, guard_dated_rows, iso_date, known_date_limit,
                   missing_start, sources_cfg, to_date)


def fetch_overnight(store, cfg, as_of, downloader=None, report=None, years=None, write=True):
    """세 계열을 빈 구간만 받아 market_daily 에 넣는다.

    `downloader` 는 (티커, 시작일, 끝일) → [(date, close)] 인 함수다. 기본은 yfinance 이고,
    테스트는 가짜를 넣어 네트워크 없이 돈다.
    """
    report = report if report is not None else Report()
    tickers = dict(sources_cfg(cfg, "overnight_tickers"))
    depth_years = int(years if years is not None else sources_cfg(cfg, "overnight_backfill_years"))
    download = downloader or _yfinance_downloader(cfg)

    total = 0
    for series, ticker in tickers.items():
        rule = "fx" if series.upper() == "USDKRW" else "us_bar"
        limit = known_date_limit(cfg, as_of, rule)
        start = missing_start(store, "market_daily", series, limit, depth_years * 365,
                              code_col="series")
        if start is None:
            continue
        try:
            # 끝일은 넉넉히 하루 더 준다 (제공자가 끝일을 배타적으로 다루는 경우가 있다).
            pairs = download(ticker, start, limit + timedelta(days=1))
        except Exception as exc:
            raise SourceError(f"{series}({ticker}) 조회 실패: {exc}") from exc
        rows = [{"series": series, "date": iso_date(day), "value": float(close)}
                for day, close in pairs
                if close is not None and close == close and start <= to_date(day) <= limit]
        rows = guard_dated_rows(rows, cfg, as_of, rule, label=f"market_daily:{series}")
        if write and rows:
            store.put_market(rows)
        report.used("overnight", "yfinance")
        total += len(rows)
    return {"rows": total, "provider": report.provider_of("overnight") or "yfinance",
            "series": sorted(tickers)}


def _yfinance_downloader(cfg):
    """기본 다운로더. yfinance 는 무거워서 여기서 늦게 import 한다."""
    timeout = int(sources_cfg(cfg, "http_timeout_sec"))

    def download(ticker, start, end):
        import yfinance as yf
        frame = yf.download(ticker, start=to_date(start).isoformat(), end=to_date(end).isoformat(),
                            progress=False, auto_adjust=False, timeout=timeout)
        if frame is None or getattr(frame, "empty", True):
            return []
        close = frame["Close"]
        if hasattr(close, "columns"):          # 티커 하나여도 열이 2단이 되는 경우가 있다
            close = close.iloc[:, 0]
        return [(day, value) for day, value in zip(close.index, close.to_numpy().ravel())]

    return download


__all__ = ["fetch_overnight"]
