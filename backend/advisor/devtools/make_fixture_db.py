"""시연·개발용 가짜 advisor.db 생성기.

```
python -m backend.advisor.devtools.make_fixture_db --db data/advisor_fixture.db
ADVISOR_DB=data/advisor_fixture.db python -m uvicorn backend.app.main:app --port 8010
```

**왜 필요한가.** 실제 판단 기록은 마감까지 많아야 서너 벌이고(설계 11장), 그중 LLM 조정·재현
모드·채점 결과는 각각 담당이 다른 모듈이 들어와야 생긴다. API 와 화면을 그때까지 못 만들면
마지막 날에 세 개가 한꺼번에 처음 만난다. 그래서 **설계 8장의 테이블 모양 그대로** 가짜 기록을
만들어 두고 API·화면을 먼저 완성한다.

**규칙 세 가지.**
1. 값은 전부 가짜다. 이 DB 로 성과를 말하지 않는다 — 화면과 API 의 모양을 확인하는 용도다.
2. **같은 인자면 같은 DB** 가 나온다. 난수 대신 (대상, 날짜)의 해시를 쓰기 때문에 실행할 때마다
   숫자가 바뀌지 않는다. 그래야 테스트가 특정 값을 근거로 단언할 수 있다.
   (다만 `started_at`·`finished_at` 처럼 Store 가 벽시계로 채우는 열은 매번 달라진다.)
3. 기록은 `Store` 를 통해 넣는다. 저장 helper 가 아직 없는 표(nav·outcome·factor_metric 등)는
   같은 연결로 직접 INSERT 한다 — 그 표들은 포트폴리오·채점 담당이 쓰는 자리라 여기서
   Store 에 쓰기 helper 를 늘리지 않는다.

담는 것: 거래일 40일 × (예비·최종), 종목 30개 · 섹터 3개, v0 와 LLM 두 판단(조정·거부 포함),
위험 표시, 공시와 근거, 포트폴리오 6개(+`@replay` 한 벌), 사후 결과와 요인 지표(표본이 모자란 줄 포함).
"""
import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

from ..calendar import TradingCalendar, to_date
from ..config import config_hash, config_yaml_text, load_config
from ..store import KST, Store

# 가짜 종목 코드는 900001~ 을 쓴다. 실제 상장 코드와 겹치지 않아 시연 화면을 실제 기록으로
# 오해할 여지가 없다. ETF 코드만 설정의 실제 값을 그대로 쓴다 (비중 표의 역할이 설정과 맞아야 한다).
STOCK_CODE_BASE = 900000
N_STOCKS = 30
N_DAYS = 40
REPLAY_DAYS = 15
REPLAY_SUFFIX = "@replay"

MARKET_FACTORS = ("mkt_trend", "mkt_vol", "mkt_overnight", "mkt_credit")
SECTOR_FACTORS = ("sec_flow", "sec_trend", "news_risk")
STOCK_FACTORS = ("stk_high52", "stk_flow", "stk_disclosure")


# ---------------------------------------------------------------- 결정적 난수

def _u(*parts):
    """(0,1) 사이의 값. 같은 인자면 항상 같다 — 실행할 때마다 화면 숫자가 바뀌지 않게."""
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") / float(1 << 64)


def _s(*parts):
    """[-1, 1) 사이의 값."""
    return 2.0 * _u(*parts) - 1.0


def _clip(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


def _round(x, n=4):
    return None if x is None else round(float(x), n)


# ---------------------------------------------------------------- 대상 목록

def _assets(cfg):
    """시연용 대상. 섹터 3개 × 종목 10개 + 설정의 ETF 들."""
    uni = cfg["universe"]
    sector_etfs = list((uni.get("sector_etfs") or {}).items())[:3]
    sectors = [name for name, _ in sector_etfs]
    stocks = {}
    for i in range(N_STOCKS):
        code = str(STOCK_CODE_BASE + i + 1)
        sector = sectors[i % len(sectors)]
        stocks[code] = {"name": f"가상{sector}{i // len(sectors) + 1}", "sector": sector}
    return {
        "sectors": sectors,
        "sector_etfs": dict(sector_etfs),
        "core": uni["core_etf"],
        "cash": uni["cash_etf"],
        "stocks": stocks,
    }


def _universe_rows(assets):
    rows = [{"code": assets["core"], "name": "KODEX 200", "kind": "etf", "sector": None},
            {"code": assets["cash"], "name": "현금 대용 ETF", "kind": "cash_etf", "sector": None}]
    for sector, etf in assets["sector_etfs"].items():
        rows.append({"code": etf, "name": f"{sector} ETF", "kind": "etf", "sector": sector})
    for code, meta in assets["stocks"].items():
        rows.append({"code": code, "name": meta["name"], "kind": "stock", "sector": meta["sector"]})
    return rows


# ---------------------------------------------------------------- 시세·수급

def _price_path(code, dates, base):
    """결정적 가격 경로. 완만한 추세 + 잡음이라 NAV 곡선이 그럴듯하게 그려진다."""
    out = []
    px = float(base)
    for i, day in enumerate(dates):
        drift = 0.0012 * math.sin((i + _u(code) * 7) / 9.0)
        px *= 1.0 + drift + 0.010 * _s(code, day)
        out.append(round(px, 2))
    return out


def _input_rows(assets, dates):
    prices, flows, market = [], [], []
    codes = ([assets["core"], assets["cash"]] + list(assets["sector_etfs"].values())
             + list(assets["stocks"]))
    for code in codes:
        base = 10000 + 900 * _u("base", code)
        if code == assets["cash"]:
            base = 100000
        path = _price_path(code, dates, base)
        for day, close in zip(dates, path):
            spread = 1.0 + 0.004 * _u("hi", code, day)
            prices.append({"code": code, "date": day,
                           "open": round(close / (1.0 + 0.003 * _s("op", code, day)), 2),
                           "high": round(close * spread, 2),
                           "low": round(close / spread, 2), "close": close,
                           "volume": round(100000 * (1 + _u("vol", code, day)), 0),
                           "value": round(close * 100000 * (1 + _u("vol", code, day)), 0),
                           "adj_close": close})
    for code in assets["stocks"]:
        for day in dates:
            mktcap = 1e12 * (0.4 + _u("cap", code))
            flows.append({"code": code, "date": day,
                          "foreign_net": round(1e9 * _s("fn", code, day), 0),
                          "inst_net": round(8e8 * _s("in", code, day), 0),
                          "mktcap": round(mktcap, 0)})
    series_base = {"KOSPI": 2600.0, "SP500": 5400.0, "SOX": 5200.0, "USDKRW": 1340.0}
    for name, base in series_base.items():
        path = _price_path(name, dates, base)
        for day, value in zip(dates, path):
            market.append({"series": name, "date": day, "value": value})
    return prices, flows, market


# ---------------------------------------------------------------- 점수

def _factor_score(entity, factor_id, day, i):
    """요인 점수 [-1, 1]. 날짜에 따라 완만하게 움직여 '무엇이 달라졌고 왜'가 보이게 만든다."""
    wave = math.sin((i + 5.0 * _u(entity, factor_id)) / 6.0)
    return _round(_clip(0.75 * wave + 0.25 * _s(entity, factor_id, day), -1.0, 1.0), 3)


def _factor_rows(assets, day, i, stage):
    """그 실행의 factor_value 행. 결측 규칙은 설계 5.2 를 따른다."""
    rows = []
    for fid in MARKET_FACTORS:
        # mkt_overnight 은 최종 단계에만 값이 있다 (결정 11의 실험 설계)
        missing = (fid == "mkt_overnight" and stage == "prelim") or (fid == "mkt_credit" and i % 5 == 0)
        score = None if missing else _factor_score("MARKET", fid, day, i)
        if fid == "mkt_trend" and score is not None:
            score = 1.0 if score >= 0 else -1.0          # 규칙형은 ±1 뿐이다
        rows.append({"entity": "MARKET", "factor_id": fid,
                     "raw_value": None if missing else _round(100 * _u("raw", fid, day), 3),
                     "score": score, "missing": missing})
    for sector in assets["sectors"]:
        for fid in SECTOR_FACTORS:
            missing = fid == "news_risk" and i % 3 != 0     # 관찰 요인은 자주 비어 있다
            score = None if missing else _factor_score(sector, fid, day, i)
            if fid == "sec_trend" and score is not None:
                score = 1.0 if score >= 0 else -1.0
            rows.append({"entity": sector, "factor_id": fid,
                         "raw_value": None if missing else _round(_s("raw", sector, fid, day), 4),
                         "score": score, "missing": missing})
    for code in assets["stocks"]:
        for fid in STOCK_FACTORS:
            missing = fid == "stk_disclosure" and _u("disc", code, day) > 0.18
            score = None if missing else _factor_score(code, fid, day, i)
            rows.append({"entity": code, "factor_id": fid,
                         "raw_value": None if missing else _round(_u("raw", code, fid, day), 4),
                         "score": score, "missing": missing})
    return rows


def _layer_score(cfg, rows, entity, layer):
    """Σ(가중치 × 점수) ÷ Σ(가중치). 결측과 가중치 0 은 분자·분모에서 모두 뺀다 (설계 5.3)."""
    factors = cfg["factors"]
    num = den = 0.0
    for r in rows:
        if r["entity"] != entity or r["missing"]:
            continue
        meta = factors.get(r["factor_id"])
        if not meta or meta.get("layer") != layer or not meta.get("weight"):
            continue
        num += float(meta["weight"]) * float(r["score"])
        den += float(meta["weight"])
    return None if den == 0 else round(num / den, 4)


def _allocate(cfg, assets, market_score, sector_scores, stock_scores, blocked):
    """설계 6.1 의 비중 규칙을 그대로 옮긴 것 (가짜 기록용).

    배치의 allocate.py 를 부르지 않는 이유는, 시연용 DB 가 아직 바뀌는 중인 모듈에 묶이면
    그쪽 작업 중에 시연이 멈추기 때문이다. 규칙이 바뀌면 진짜 기록이 진실이고 이 표는 시연용이다.
    """
    al = cfg["allocate"]
    base = float(al["risk_base"])
    risk = _clip(base if market_score is None else base + float(al["risk_slope"]) * market_score,
                 float(al["risk_min"]), float(al["risk_max"]))
    cash = 1.0 - risk
    core = risk * float(al["core_share"])
    satellite = risk - core
    sector_pool = satellite * float(al["sector_share"])
    stock_pool = satellite - sector_pool

    n_sectors = int(al["n_sectors"])
    per_sector = sector_pool / n_sectors if n_sectors else 0.0
    rows = []
    for sector, score in sorted(sector_scores.items(), key=lambda kv: (-(kv[1] or -9), kv[0]))[:n_sectors]:
        etf = assets["sector_etfs"].get(sector)
        if score is None or score <= 0 or not etf or etf in blocked:
            continue
        rows.append({"asset": etf, "role": "sector", "weight": per_sector})

    n_stocks, cap = int(al["n_stocks"]), float(al["stock_cap"])
    per_stock = min(stock_pool / n_stocks, cap) if n_stocks else 0.0
    picked = [c for c, s in sorted(stock_scores.items(), key=lambda kv: (-kv[1], kv[0]))
              if s > 0 and c not in blocked][:n_stocks]
    rows += [{"asset": c, "role": "stock", "weight": per_stock} for c in picked]

    used = sum(r["weight"] for r in rows)
    out = [{"asset": assets["cash"], "role": "cash", "weight": cash},
           {"asset": assets["core"], "role": "core",
            "weight": core + sector_pool + stock_pool - used}] + rows
    return [r for r in out if r["weight"] > 1e-12], risk


# ---------------------------------------------------------------- 실행 한 벌

def _flag_rows(assets, day, i):
    """위험 표시 (설계 5.5). 며칠에 한 번 halt 를 넣어 'v0 에서도 빠지는' 경로를 만든다."""
    codes = list(assets["stocks"])
    rows = []
    spike = codes[(i * 3) % len(codes)]
    rows.append({"entity": spike, "flag_type": "spike", "src": "code",
                 "detail": f"최근 5거래일 누적 {35 + 10 * _u('sp', spike, day):.1f}%"})
    action = codes[(i * 7 + 5) % len(codes)]
    rows.append({"entity": action, "flag_type": "market_action", "src": "code",
                 "detail": "투자경고 지정 공시"})
    if i % 6 == 2:
        halted = codes[(i * 11 + 3) % len(codes)]
        rows.append({"entity": halted, "flag_type": "halt", "src": "code",
                     "detail": "거래정지 (가짜 기록)"})
    if i % 8 == 4:
        gov = codes[(i * 5 + 9) % len(codes)]
        rows.append({"entity": gov, "flag_type": "governance", "src": "code+llm",
                     "detail": "횡령·배임 혐의 제목 패턴"})
    return rows


def _composite_rows(scores_by_entity, extra=None):
    extra = extra or {}
    rows = []
    for entity, (layer, score) in scores_by_entity.items():
        e = extra.get(entity) or {}
        adj = float(e.get("adj") or 0.0)
        base = None if score is None else float(score)
        rows.append({"entity": entity, "layer": layer, "base_score": base, "adj": adj,
                     "final_score": None if base is None else round(base + adj, 4),
                     "vetoed": bool(e.get("vetoed")),
                     "adopted_json": e.get("adopted_json"), "rejected_json": e.get("rejected_json"),
                     "reason": e.get("reason"), "call_id": e.get("call_id")})
    return rows


def _llm_extra(assets, stock_scores, day, i, run_id):
    """LLM 조정·거부 (결정 4). 상위 몇 종목에만 ±0.2 안에서 조정하고 표시가 붙은 종목만 거부한다."""
    # 조정 대상은 상위 몇 종목이다 (설정의 llm.top_n 자리). 편입 경계 근처까지 걸치게 잡아야
    # '조정 때문에 종목이 바뀐 날'이 시연 화면에 나온다.
    top = [c for c, _ in sorted(stock_scores.items(), key=lambda kv: (-kv[1], kv[0]))[:14]]
    extra = {}
    for n, code in enumerate(top):
        if n % 2:
            continue
        adj = round(_clip(0.2 * _s("adj", code, day), -0.2, 0.2), 3)
        extra[code] = {
            "adj": adj,
            "adopted_json": json.dumps(["stk_high52", "stk_flow"], ensure_ascii=False),
            "rejected_json": json.dumps(
                [{"factor_id": "stk_disclosure", "reason": "공시가 오래돼 가격에 이미 반영된 것으로 봤다"}],
                ensure_ascii=False),
            "reason": ("수급과 52주 고가 신호가 같은 방향이라 소폭 올렸다" if adj > 0
                       else "같은 섹터에서 더 나은 후보가 있어 소폭 내렸다"),
            "call_id": f"call-{run_id}-{code}",
        }
    if i % 9 == 5:                                   # 가끔 한 종목을 거부한다
        code = top[-1]
        extra[code] = {"adj": -0.05, "vetoed": True,
                       "adopted_json": json.dumps([], ensure_ascii=False),
                       "rejected_json": json.dumps(
                           [{"factor_id": "stk_high52", "reason": "급등락 표시가 붙어 추세 신호를 믿지 않았다"}],
                           ensure_ascii=False),
                       "reason": "급등락 위험 표시가 붙어 이번 판단에서는 제외했다",
                       "call_id": f"call-{run_id}-{code}"}
    return extra


def _make_run(store, cfg, assets, day, i, stage, mode, chash, with_llm=True):
    """실행 한 벌을 통째로 기록한다. 돌려주는 값은 (run_id, v0 비중, llm 비중)."""
    hhmm = (cfg["schedule"]["final"] if stage == "final" else cfg["schedule"]["prelim"])
    decision_time = f"{day}T{hhmm}:00.000"
    run_id = store.start_run(stage, day, mode=mode, decision_time=decision_time,
                             config_hash=chash, git_sha="fixture", status="running")

    rows = _factor_rows(assets, day, i, stage)
    store.put_factor_values(run_id, rows)

    # 공시 근거 몇 줄 (자산 상세 화면의 '근거' 칸)
    evidence = []
    for code in list(assets["stocks"])[:6]:
        if any(r["entity"] == code and r["factor_id"] == "stk_disclosure" and not r["missing"]
               for r in rows):
            evidence.append({"entity": code, "factor_id": "stk_disclosure", "ref_type": "disclosure",
                             "ref_id": f"F{day.replace('-', '')}{code}",
                             "summary": "단일판매·공급계약 체결 — 최근매출액 대비 12.4%"})
    if evidence:
        store.put_factor_evidence(run_id, evidence)

    flags = _flag_rows(assets, day, i)
    store.put_risk_flags(run_id, flags)
    blocked = {f["entity"] for f in flags if f["flag_type"] == "halt"}

    market_score = _layer_score(cfg, rows, "MARKET", "market")
    sector_scores = {s: _layer_score(cfg, rows, s, "sector") for s in assets["sectors"]}
    tilt = float(cfg["combine"]["sector_tilt"])
    stock_scores = {}
    for code, meta in assets["stocks"].items():
        layer = _layer_score(cfg, rows, code, "stock")
        if layer is None:
            continue
        sec = sector_scores.get(meta["sector"])
        stock_scores[code] = round(layer + tilt * (sec or 0.0), 4)

    scores_by_entity = {"MARKET": ("market", market_score)}
    scores_by_entity.update({s: ("sector", sector_scores[s]) for s in assets["sectors"]})
    scores_by_entity.update({c: ("stock", s) for c, s in stock_scores.items()})

    weights, risk = _allocate(cfg, assets, market_score, sector_scores, stock_scores, blocked)
    store.put_composites(run_id, "v0", _composite_rows(scores_by_entity))
    store.put_decision(run_id, "v0", market_score, risk,
                       record_hash=hashlib.sha256(f"{run_id}v0".encode()).hexdigest()[:32],
                       prev_hash=None)
    store.put_target_weights(run_id, "v0", weights)

    llm_weights = None
    if with_llm:
        extra = _llm_extra(assets, stock_scores, day, i, run_id)
        vetoed = {c for c, e in extra.items() if e.get("vetoed")}
        adj_scores = {c: round(s + float((extra.get(c) or {}).get("adj") or 0.0), 4)
                      for c, s in stock_scores.items()}
        llm_weights, llm_risk = _allocate(cfg, assets, market_score, sector_scores, adj_scores,
                                          blocked | vetoed)
        llm_by_entity = dict(scores_by_entity)
        llm_by_entity.update({c: ("stock", s) for c, s in adj_scores.items()})
        store.put_composites(run_id, "llm", _composite_rows(llm_by_entity, extra))
        store.put_decision(run_id, "llm", market_score, llm_risk,
                           record_hash=hashlib.sha256(f"{run_id}llm".encode()).hexdigest()[:32],
                           prev_hash=None)
        store.put_target_weights(run_id, "llm", llm_weights)

    note = json.dumps({
        "ingest": {"providers": {"price": "fdr", "flow": "kis", "overnight": "yfinance",
                                 "disclosure": "dart"},
                   "fallbacks_used": (["universe: 시가총액 상위 200 (KRX 로그인 실패)"] if i % 11 == 0
                                      else []),
                   "rows": {"price": len(assets["stocks"]) + 5, "flow": len(assets["stocks"])}},
        "llm_factors": sum(1 for r in rows if r["factor_id"] == "stk_disclosure" and not r["missing"]),
    }, ensure_ascii=False)
    store.finish_run(run_id, status="ok", llm_used=1 if with_llm else 0, note=note)
    return run_id, weights, llm_weights


# ---------------------------------------------------------------- helper 가 없는 표

def _insert(store, table, cols, rows):
    """저장 helper 가 아직 없는 표에 직접 넣는다 (nav·trade·holding·outcome·factor_metric·llm_call).

    그 표들은 포트폴리오·채점 담당이 쓰는 자리라 시연용 도구 때문에 Store 에 쓰기 helper 를
    늘리지 않는다. DDL 은 Store 가 이미 만들어 둔 것을 그대로 쓴다.
    """
    if not rows:
        return 0
    sql = (f"INSERT OR REPLACE INTO {table}({','.join(cols)}) "
           f"VALUES({','.join('?' * len(cols))})")
    store.conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    return len(rows)


def _nav_series(store, portfolio_id, dates, seed, drift):
    """NAV 곡선 한 줄. 회전율·비용도 함께 넣어 요약 표가 채워지게 한다."""
    rows, nav = [], 1.0
    for i, day in enumerate(dates):
        nav *= 1.0 + drift + 0.006 * _s(seed, day)
        turnover = round(0.35 if i == 0 else max(0.0, 0.06 * _u("to", seed, day)), 4)
        rows.append({"portfolio_id": portfolio_id, "date": day, "nav": round(nav, 6),
                     "turnover": turnover, "cost": round(turnover * 0.0012, 6)})
    return _insert(store, "nav", ("portfolio_id", "date", "nav", "turnover", "cost"), rows)


def _portfolios(store, assets, dates, replay_dates):
    """포트폴리오 6개(실시간) + `@replay` 한 벌 (설계 6.3·7.4).

    실시간과 재현은 id 부터 갈라 둔다. 하나의 곡선으로 이으면 그 순간 성과 주장이 오염된다.
    """
    drifts = {"sys_final_llm": 0.0016, "sys_final_v0": 0.0013, "sys_prelim_llm": 0.0011,
              "bl_kodex200": 0.0009, "bl_sma10m": 0.0010, "bl_6040": 0.0007}
    n = 0
    for pid, drift in drifts.items():
        n += _nav_series(store, pid, dates, pid, drift)
        n += _nav_series(store, pid + REPLAY_SUFFIX, replay_dates, pid + "r", drift * 0.8)

    # 보유와 체결도 몇 줄 남긴다 (화면에는 안 쓰지만 표 모양을 확인할 수 있게)
    holdings, trades = [], []
    core, cash = assets["core"], assets["cash"]
    for day in dates[-3:]:
        for asset, weight in ((cash, 0.35), (core, 0.4), (list(assets["stocks"])[0], 0.05)):
            holdings.append({"portfolio_id": "sys_final_llm", "date": day, "asset": asset,
                             "weight": weight})
    trades.append({"portfolio_id": "sys_final_llm", "date": dates[-1], "asset": core, "side": "buy",
                   "weight_delta": 0.05, "price": 12345.0, "cost": 0.00006, "src_run_id": None,
                   "status": "filled"})
    _insert(store, "holding", ("portfolio_id", "date", "asset", "weight"), holdings)
    _insert(store, "trade", ("portfolio_id", "date", "asset", "side", "weight_delta", "price",
                             "cost", "src_run_id", "status"), trades)
    return n


def _outcomes(store, assets, runs, cfg):
    """사후 결과 (설계 7.1). 만기가 지난 판단만 채점된다는 사실을 날짜 수로 흉내 낸다."""
    aux = list(cfg["scoring"]["aux_horizons"])
    rows = []
    codes = list(assets["stocks"])[:12]
    for run_id, day, stage, i in runs[:-6]:               # 최근 며칠은 아직 만기가 안 됐다
        if stage != "final":
            continue
        for variant in ("v0", "llm"):
            for code in codes:
                for factor_id in ("stk_high52", "stk_flow", "composite"):
                    for horizon in aux:
                        asset_ret = round(0.02 * _s("ret", run_id, code, factor_id, horizon), 5)
                        bench_ret = round(0.01 * _s("bench", run_id, horizon), 5)
                        unable = _u("unable", run_id, code) > 0.97
                        rows.append({
                            "run_id": run_id, "variant": variant, "entity": code,
                            "factor_id": factor_id, "horizon": horizon,
                            "start_date": day, "end_date": day,
                            "asset_ret": None if unable else asset_ret,
                            "bench_ret": None if unable else bench_ret,
                            "excess_ret": None if unable else round(asset_ret - bench_ret, 5),
                            "eval_status": "unable" if unable else "ok",
                            "unable_reason": "거래정지로 종료가 없음" if unable else None,
                            "computed_at": f"{day}T18:40:00.000"})
    return _insert(store, "outcome",
                   ("run_id", "variant", "entity", "factor_id", "horizon", "start_date", "end_date",
                    "asset_ret", "bench_ret", "excess_ret", "eval_status", "unable_reason",
                    "computed_at"), rows)


def _factor_metrics(store, cfg, computed_at):
    """요인 지표 (설계 7.2). 관찰 요인은 표본이 모자라 '판단 불가'로 보이게 만든다."""
    rows = []
    small = {"news_risk", "mkt_overnight", "mkt_credit"}     # n_eff 가 min_n_eff 아래인 요인
    for fid, meta in cfg["factors"].items():
        horizons = sorted({int(meta["horizon"])} | set(cfg["scoring"]["aux_horizons"]))
        for stage in ("prelim", "final"):
            for variant in ("v0", "llm"):
                for horizon in horizons:
                    n_days = 8 if fid in small else 120
                    n_eff = round(n_days / float(horizon), 2)
                    is_market = meta["layer"] == "market"
                    rows.append({
                        "computed_at": computed_at, "stage": stage, "variant": variant,
                        "factor_id": fid, "horizon": horizon, "n_days": n_days, "n_eff": n_eff,
                        # 시장 요인은 하루에 값이 하나라 날짜 내 순위 상관이 없다 (설계 7.2)
                        "rank_ic_mean": None if is_market else _round(0.08 * _s("ic", fid, stage, variant, horizon), 4),
                        "rank_ic_std": None if is_market else _round(0.12 + 0.05 * _u("ics", fid, horizon), 4),
                        "hit_rate": _round(0.5 + 0.08 * _s("hit", fid, stage, variant, horizon), 4),
                        "bucket_json": json.dumps(
                            [{"bucket": b + 1, "mean_excess": _round(0.004 * (b - 2) + 0.002 * _s("b", fid, b), 5),
                              "n": max(1, n_days // 5)} for b in range(5)], ensure_ascii=False),
                    })
    return _insert(store, "factor_metric",
                   ("computed_at", "stage", "variant", "factor_id", "horizon", "n_days", "n_eff",
                    "rank_ic_mean", "rank_ic_std", "hit_rate", "bucket_json"), rows)


def _llm_calls(store, runs, cfg):
    """LLM 호출 기록. 상태 화면의 '오늘 LLM 비용'이 여기서 나온다."""
    rows = []
    models = cfg["llm"]["models"]
    for run_id, day, stage, _ in runs[-4:]:
        for task, model in (("disclosure_score", models["classify"]), ("adjust", models["adjust"])):
            rows.append({
                "call_id": f"call-{run_id}-{task}", "run_id": run_id, "task": task, "model": model,
                "prompt_ver": cfg["llm"]["prompt_ver"], "input_hash": f"h{run_id}{task}"[:16],
                "input_text": "(시연용 입력)", "output_text": "(시연용 출력)",
                "parsed_json": json.dumps({"ok": True}, ensure_ascii=False), "status": "OK",
                "tokens_in": 1800, "tokens_out": 320, "cost_usd": 0.0021,
                "latency_ms": 1450.0, "cache_hit": 0,
                "created_at": f"{day}T{'07:41' if stage == 'final' else '18:31'}:00.000"})
    return _insert(store, "llm_call",
                   ("call_id", "run_id", "task", "model", "prompt_ver", "input_hash", "input_text",
                    "output_text", "parsed_json", "status", "tokens_in", "tokens_out", "cost_usd",
                    "latency_ms", "cache_hit", "created_at"), rows)


def _disclosures(store, assets, dates):
    """공시. 근거(factor_evidence)가 가리킬 행이 항상 있도록 같은 조건으로 만든다."""
    n = 0
    for code in list(assets["stocks"])[:6]:
        for day in dates:
            if _u("disc", code, day) <= 0.18:
                store.upsert_disclosure({
                    "rcept_no": f"F{day.replace('-', '')}{code}", "stock_code": code,
                    "corp_name": assets["stocks"][code]["name"],
                    "report_nm": "단일판매ㆍ공급계약체결", "rcept_dt": day.replace("-", ""),
                    "first_seen_at": f"{day}T09:12:31.000", "first_seen_src": "ls",
                    "ls_realkey": None, "kind": "공급계약", "ratio": 0.124, "ratio_ok": 1,
                    "body_src": "ls"})
                n += 1
    return n


# ---------------------------------------------------------------- 진입점

def trading_days(cfg, end, count):
    """end 에서 거슬러 올라가는 거래일 목록 (오름차순). 설정의 휴장일만 본다."""
    cal = TradingCalendar(cfg, None)
    day, out = to_date(end), []
    while len(out) < count:
        if cal.is_trading_day(day):
            out.append(day.isoformat())
        day -= timedelta(days=1)
    return list(reversed(out))


def _last_closed_day(cfg, dates):
    """구간 안에서 가장 최근의 비거래일 (주말·휴장일). 없으면 None."""
    cal = TradingCalendar(cfg, None)
    day = to_date(dates[-1]) - timedelta(days=1)
    first = to_date(dates[0])
    while day >= first:
        if not cal.is_trading_day(day):
            return day.isoformat()
        day -= timedelta(days=1)
    return None


def build(path, cfg=None, end=None, days=N_DAYS, replay_days=REPLAY_DAYS, config_path=None):
    """가짜 advisor.db 를 만든다. 같은 인자면 같은 내용이 나온다. 요약 dict 를 돌려준다."""
    cfg = cfg or load_config(config_path)
    end = end or datetime.now(KST).strftime("%Y-%m-%d")
    dates = trading_days(cfg, end, int(days))
    replay_dates = dates[-int(replay_days):] if replay_days else []
    assets = _assets(cfg)
    chash = config_hash(cfg)

    path = Path(path)
    if path.exists():
        path.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)

    with Store(path) as store:
        try:
            yaml_text = config_yaml_text(config_path)
        except OSError:
            yaml_text = ""
        store.register_config(chash, yaml_text)

        for day in dates:
            store.put_universe(day, _universe_rows(assets))
        prices, flows, market = _input_rows(assets, dates)
        store.put_prices(prices)
        store.put_flows(flows)
        store.put_market(market)
        n_disclosure = _disclosures(store, assets, dates)
        store.commit()

        runs = []
        for i, day in enumerate(dates):
            # 최종(07:40) → 예비(18:30) 순서로 같은 날짜에 두 벌 남긴다 (설계 2.3)
            for stage in ("final", "prelim"):
                with_llm = not (stage == "final" and i % 13 == 7)    # 가끔 LLM 이 기권한다
                run_id, _, _ = _make_run(store, cfg, assets, day, i, stage, "live", chash,
                                         with_llm=with_llm)
                runs.append((run_id, day, stage, i))
            store.commit()

        # 재현 모드 기록 (설계 7.4). 코드 요인만 도므로 LLM 판단이 없다.
        for i, day in enumerate(replay_dates):
            _make_run(store, cfg, assets, day, i, "final", "replay", chash, with_llm=False)
        store.commit()

        # 실패한 실행과 휴장일 실행도 한 줄씩 — 화면이 그 상태를 그릴 수 있어야 한다.
        # 휴장일 줄은 구간 **안**의 비거래일에 넣는다 (사람이 주말에 수동으로 돌린 경우).
        # 구간 밖(미래)의 휴장일에 넣으면 '마지막 실행'이 영영 그 줄로 잡힌다.
        closed = _last_closed_day(cfg, dates)
        if closed:
            hid = store.start_run("final", closed, mode="live",
                                  decision_time=f"{closed}T07:40:00.000", config_hash=chash,
                                  git_sha="fixture", status="skipped",
                                  note="휴장일 (오늘이 거래일인가: 아니오) — 판단을 건너뜁니다")
            store.finish_run(hid, status="skipped")
        eid = store.start_run("prelim", dates[0], mode="live",
                              decision_time=f"{dates[0]}T18:30:00.000", config_hash=chash,
                              git_sha="fixture", status="running")
        store.finish_run(eid, status="error",
                         note="RuntimeError: 시세 제공자가 응답하지 않았습니다 (시연용 기록)")

        n_nav = _portfolios(store, assets, dates, replay_dates)
        n_outcome = _outcomes(store, assets, runs, cfg)
        n_metric = _factor_metrics(store, cfg, f"{dates[-1]}T18:45:00.000")
        n_llm = _llm_calls(store, runs, cfg)
        store.commit()

    return {"path": str(path), "dates": dates, "start": dates[0], "end": dates[-1],
            "runs": len(runs), "stocks": len(assets["stocks"]), "sectors": assets["sectors"],
            "nav_rows": n_nav, "outcomes": n_outcome, "metrics": n_metric,
            "llm_calls": n_llm, "disclosures": n_disclosure,
            "replay_dates": replay_dates}


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m backend.advisor.devtools.make_fixture_db",
                                description="시연·개발용 가짜 advisor.db 생성")
    p.add_argument("--db", default="data/advisor_fixture.db", help="만들 DB 경로")
    p.add_argument("--end", help="마지막 거래일 (기본: 오늘)")
    p.add_argument("--days", type=int, default=N_DAYS, help="거래일 수 (기본 40)")
    p.add_argument("--replay-days", dest="replay_days", type=int, default=REPLAY_DAYS)
    p.add_argument("--config", help="설정 파일 경로")
    args = p.parse_args(argv)

    root = Path(__file__).resolve().parents[3]
    db = Path(args.db)
    if not db.is_absolute():
        db = root / db
    info = build(db, end=args.end, days=args.days, replay_days=args.replay_days,
                 config_path=args.config)
    print(f"만들었습니다: {info['path']}")
    print(f"  거래일 {info['start']} ~ {info['end']} ({len(info['dates'])}일), 실행 {info['runs']}벌")
    print(f"  종목 {info['stocks']}개 · 섹터 {', '.join(info['sectors'])}")
    print(f"  NAV {info['nav_rows']}행 · 사후 결과 {info['outcomes']}행 · 요인 지표 {info['metrics']}행 "
          f"· LLM 호출 {info['llm_calls']}행 · 공시 {info['disclosures']}건")
    print(f"  재현 모드 구간: {info['replay_dates'][0] if info['replay_dates'] else '없음'} ~ "
          f"{info['replay_dates'][-1] if info['replay_dates'] else ''}")
    print(f"\n  ADVISOR_DB={info['path']} python -m uvicorn backend.app.main:app --port 8010")
    return 0


if __name__ == "__main__":
    sys.exit(main())
