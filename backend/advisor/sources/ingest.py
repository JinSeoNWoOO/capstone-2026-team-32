"""수집 단계의 단일 진입점 (설계 4장의 2번 상자). 배치는 이 함수 하나만 부른다.

    run_ingest(store, cfg, cal, as_of, stage, mode="live") -> dict

## 한 소스가 죽어도 배치는 산다

단계마다 try/except 로 감싼다. 공시 API 키가 만료되거나 yfinance 가 한 번 튕겼다고 그날 판단이
통째로 없어지면, "휴장일을 빼면 마감까지 실시간 기록이 서너 번"인 일정에서 그 하루가 회복되지 않는다
(설계 11장). 실패한 단계는 요약에 사유를 남기고 나머지는 그대로 진행한다 — 그 요인이 결측이 되는 것은
설계가 이미 다루는 경우다 (설계 5.3: 결측 요인은 분자·분모에서 함께 뺀다).

## 반환 계약 (다른 모듈이 이걸 보고 코딩한다)

```python
{
  "mode": "live" | "replay",
  "stage": "prelim" | "final",
  "as_of": "2026-09-22T18:30:00.000",
  "ok": bool,                      # 모든 단계가 성공했는가
  "steps": {                       # 단계 이름 → 결과
      "universe": {"ok": True, "provider": "pykrx_kospi200", "rows": 215, "error": None, ...},
      ...
  },
  "fallbacks_used": ["krx_login_failed", ...],
  "elapsed_sec": 31.4,
  "note": "한 줄 요약 — run.note 에 그대로 넣으면 된다",
}
```

단계 이름은 `universe · price · index · flow · overnight · dart · fin · lsnews` 로 고정이다.
`fin`(정기보고서 재무, 2026-09-28 추가)은 `dart` 다음에 돈다 — 방금 받은 공시 목록의 공시일을
재무 값의 인지 시각에 쓰기 때문이다 (sources/dart_fin.py). 새 보고서가 없으면 호출이 0 회다.

## 재현 모드

`mode="replay"` 는 **아무것도 하지 않는다**. 재현은 이미 채워 둔 데이터 위에서 도는 것이고
(설계 7.4), 과거 시점에 네트워크로 되받으면 "그때 알 수 있었던 값"이 아니라 "지금 보이는 값"이
들어와 시점 규칙이 무너진다. 과거 구간은 `backfill()` 로 한 번 채운다.
"""
import argparse
import json
import sys
import time
from datetime import datetime

from . import dart, dart_fin, krx, lsnews, overnight
from .base import (Report, iso_date, now_kst, parse_hhmm, sources_cfg, to_date, to_kst, wall)
from .. import universe as universe_mod
from ..calendar import TradingCalendar
from ..config import load_config, resolve_path
from ..store import Store

STEP_NAMES = ("universe", "price", "index", "flow", "overnight", "dart", "fin", "lsnews")


def _step(summary, name, func):
    """단계 하나를 돌리고 결과를 요약에 넣는다. 예외는 여기서 끊는다."""
    started = time.monotonic()
    try:
        result = func() or {}
        entry = {"ok": True, "provider": result.get("provider"), "rows": int(result.get("rows") or 0),
                 "error": None}
        for key, value in result.items():
            if key not in entry:
                entry[key] = value
    except Exception as exc:
        entry = {"ok": False, "provider": None, "rows": 0,
                 "error": f"{type(exc).__name__}: {exc}"}
        summary["ok"] = False
    entry["sec"] = round(time.monotonic() - started, 2)
    summary["steps"][name] = entry
    return entry


def _note(summary):
    """run.note 한 줄. 어느 제공자가 무엇을 채웠고 무엇이 실패했는지만 담는다."""
    parts = []
    for name, entry in summary["steps"].items():
        if entry["ok"]:
            parts.append(f"{name}={entry['rows']}@{entry.get('provider') or '-'}")
        else:
            parts.append(f"{name}=FAIL({entry['error'][:60]})")
    if summary["fallbacks_used"]:
        parts.append("fallback:" + ",".join(summary["fallbacks_used"]))
    return " ".join(parts)


def _codes(store, cfg, as_of, summary, limit=None):
    """이번 수집이 다룰 코드. 목록 만들기가 실패했으면 가장 최근 저장 목록으로 물러선다."""
    as_of_date = to_kst(as_of).date()
    day = universe_mod.latest_universe_date(store, as_of_date)
    stocks = universe_mod.stored_codes(store, day, kinds=(universe_mod.KIND_STOCK,)) if day else []
    etfs = universe_mod.stored_codes(
        store, day, kinds=(universe_mod.KIND_ETF, universe_mod.KIND_CASH_ETF)) if day else []
    if not stocks and not etfs:
        # 저장된 목록이 하나도 없으면 설정의 ETF 만이라도 챙긴다 (첫 실행).
        etfs = list(universe_mod.configured_etfs(cfg))
    if limit:
        stocks = stocks[:int(limit)]
    summary.setdefault("counts", {})["stocks"] = len(stocks)
    summary["counts"]["etfs"] = len(etfs)
    return stocks, etfs


def run_ingest(store, cfg, cal, as_of, stage, mode="live", providers=None, report=None,
               limit=None, http=None, downloader=None):
    """수집 한 바퀴. 계약은 모듈 머리말 참고."""
    started = time.monotonic()
    summary = {"mode": mode, "stage": stage, "as_of": wall(as_of), "ok": True,
               "steps": {}, "fallbacks_used": [], "elapsed_sec": 0.0, "note": ""}
    if mode == "replay":
        summary["skipped"] = True
        summary["note"] = "재현 모드: 수집을 건너뛴다 (이미 채워 둔 데이터로 돈다, 설계 7.4)"
        return summary

    report = report if report is not None else Report()
    if providers is None:
        providers = krx.build_providers(cfg, report=report)

    _step(summary, "universe",
          lambda: universe_mod.snapshot(store, cfg, providers, as_of, report=report))
    stocks, etfs = _codes(store, cfg, as_of, summary, limit=limit)

    _step(summary, "price",
          lambda: krx.fetch_prices(store, cfg, as_of, stocks + etfs, providers, report=report))
    _step(summary, "index", lambda: krx.fetch_index(store, cfg, as_of, providers, report=report))
    _step(summary, "flow",
          lambda: krx.fetch_flows(store, cfg, as_of, stocks, providers, report=report))
    # 밤사이 시장은 예비 단계에서도 받아 둔다. 시점 규칙이 "언제까지 쓸 수 있는가"를 이미 막으므로
    # 미리 받아 두는 것이 해롭지 않고, 최종 배치가 실패했을 때 데이터가 남아 있다.
    _step(summary, "overnight",
          lambda: overnight.fetch_overnight(store, cfg, as_of, downloader=downloader, report=report))
    _step(summary, "dart", lambda: dart.sync(store, cfg, as_of, http=http, report=report))
    _step(summary, "fin", lambda: dart_fin.sync(store, cfg, as_of, stocks, http=http, report=report))
    _step(summary, "lsnews", lambda: lsnews.sync_disclosures(store, cfg, as_of, report=report))

    store.commit()
    summary["fallbacks_used"] = list(report.fallbacks)
    summary["providers"] = dict(report.providers)
    summary["elapsed_sec"] = round(time.monotonic() - started, 2)
    summary["note"] = _note(summary)
    return summary


def backfill(store, cfg, cal, start, end, providers=None, report=None, limit=None,
             with_universe=True, with_flows=None, downloader=None):
    """재현 모드를 돌리기 전에 과거 구간을 한 번 채운다 (설계 7.4).

    일봉·지수·밤사이가 기본이고, 여기에 두 가지를 더 한다.
      - `with_universe`: 구간의 모든 거래일에 **오늘 목록**으로 universe 스냅샷을 찍는다.
        재현 실행이 그날의 후보를 읽을 수 있어야 하기 때문이다. 오늘 목록을 과거에 쓰는 것은
        생존 편향이고, 설계 7.4 가 이미 README 에 적기로 한 알려진 한계다.
      - `with_flows`: 수급. KRX 로그인이 있을 때만 의미가 있다 — 무로그인 경로(KIS)는 최근
        30영업일까지만 준다. 기본은 '로그인이 있으면 켠다'.
    """
    started = time.monotonic()
    start, end = to_date(start), to_date(end)
    summary = {"mode": "backfill", "range": [iso_date(start), iso_date(end)], "ok": True,
               "steps": {}, "fallbacks_used": [], "elapsed_sec": 0.0, "note": ""}
    report = report if report is not None else Report()
    if providers is None:
        providers = krx.build_providers(cfg, report=report)
    if with_flows is None:
        with_flows = bool(providers.krx_login)

    # 끝일의 일봉이 확정되는 순간을 기준 시각으로 삼는다 (미래를 보지 않으면서 끝일까지 받는다).
    as_of = min(datetime.combine(end, parse_hhmm(sources_cfg(cfg, "daily_bar_known_at"))), now_kst())
    depth = (end - start).days + 1

    _step(summary, "universe", lambda: _backfill_universe(
        store, cfg, cal, providers, start, end, report, enabled=with_universe))
    stocks, etfs = _codes(store, cfg, as_of, summary, limit=limit)

    _step(summary, "price", lambda: krx.fetch_prices(
        store, cfg, as_of, stocks + etfs, providers, report=report, backfill_days=depth))
    _step(summary, "index", lambda: krx.fetch_index(store, cfg, as_of, providers, report=report))
    _step(summary, "overnight", lambda: overnight.fetch_overnight(
        store, cfg, as_of, downloader=downloader, report=report))
    if with_flows:
        _step(summary, "flow", lambda: krx.fetch_flows(
            store, cfg, as_of, stocks, providers, report=report, backfill_days=depth))

    store.commit()
    summary["fallbacks_used"] = list(report.fallbacks)
    summary["providers"] = dict(report.providers)
    summary["elapsed_sec"] = round(time.monotonic() - started, 2)
    summary["note"] = _note(summary)
    return summary


def _backfill_universe(store, cfg, cal, providers, start, end, report, enabled=True):
    """구간의 거래일마다 오늘 목록을 찍는다. 끄면 끝일 하루만 찍는다."""
    rows, _ = universe_mod.build_universe(cfg, providers, end, report)
    days = cal.trading_days_between(start, end) if enabled else [end]
    for day in days or [end]:
        store.put_universe(iso_date(day), rows)
    return {"rows": len(rows) * max(len(days or [end]), 1), "provider": report.provider_of("universe_stocks"),
            "days": len(days or [end]), "codes": len(rows)}


# ---------------------------------------------------------------- CLI

def _open_store(cfg, db_path=None):
    return Store(db_path or resolve_path(cfg, "db"))


def main(argv=None):
    """`python -m backend.advisor.sources.ingest --once` / `--backfill 시작 끝`."""
    parser = argparse.ArgumentParser(description="advisor 수집기 (설계 3장 sources/)")
    parser.add_argument("--once", action="store_true", help="지금 기준으로 수집 한 바퀴")
    parser.add_argument("--backfill", nargs=2, metavar=("시작", "끝"),
                        help="과거 구간을 채운다 (재현 모드 전에 한 번)")
    parser.add_argument("--stage", default="prelim", choices=("prelim", "final"))
    parser.add_argument("--as-of", dest="as_of", default=None,
                        help="기준 시각 (YYYY-MM-DD 또는 ISO). 생략하면 지금")
    parser.add_argument("--db", default=None, help="advisor.db 경로 (스모크 테스트용)")
    parser.add_argument("--limit", type=int, default=None, help="종목 수 상한 (개발용)")
    parser.add_argument("--no-krx-login", action="store_true", help="KRX 로그인을 시도하지 않는다")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    with _open_store(cfg, args.db) as store:
        cal = TradingCalendar(cfg, store)
        report = Report()
        providers = krx.build_providers(cfg, report=report,
                                        try_krx_login=not args.no_krx_login)
        if args.backfill:
            summary = backfill(store, cfg, cal, args.backfill[0], args.backfill[1],
                               providers=providers, report=report, limit=args.limit)
        elif args.once:
            as_of = _parse_as_of(cfg, args.as_of, args.stage)
            summary = run_ingest(store, cfg, cal, as_of, args.stage, providers=providers,
                                 report=report, limit=args.limit)
        else:
            parser.error("--once 또는 --backfill 중 하나가 필요합니다")
            return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary.get("ok") else 1


def _parse_as_of(cfg, text, stage):
    """--as-of 해석. 날짜만 주면 그 단계의 예정 시각을 붙인다 (설정 schedule)."""
    if not text:
        return now_kst()
    text = text.strip()
    if len(text) == 10:
        at = parse_hhmm((cfg.get("schedule") or {})[stage])
        return datetime.combine(to_date(text), at)
    return datetime.fromisoformat(text)


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["run_ingest", "backfill", "main", "STEP_NAMES"]
