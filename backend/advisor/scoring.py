"""사후 결과 채점과 요인 지표 (결정 5, 설계 7.1~7.2).

하위 점수는 곧 예측이다. 점수 +는 "이 자산이 정해진 기간 동안 **비교 대상보다 더 오른다**"이고,
이 파일은 그 예측이 맞았는지를 값으로 만든다.

| 계층 | 자산 수익 | 비교 대상 |
|---|---|---|
| 시장 | KODEX 200 | 현금 대용 ETF |
| 섹터 | 섹터 ETF | KODEX 200 |
| 종목 | 그 종목 | KODEX 200 (보조로 섹터 ETF) |

규칙 넷이 이 파일의 모양을 정한다.

1. **시작가는 판단을 실제로 실행할 수 있었던 첫 시가**다 (설계 7.1). 최종(T일 07:40) 판단은 T일 시가,
   예비(T일 18:30) 판단은 T+1일 시가. 종료가는 시작일부터 h거래일 뒤의 수정 종가다.
   시가로 시작해 종가로 끝나는 것은 가상 체결(설계 6.2)과 같은 가정이고, 현재 수집 경로에서는
   close == adj_close 이며 open 도 같은 기준이라 한 수익률 안에서 섞어도 일관된다.
2. **만기 판정은 데이터가 아니라 시점 규칙으로 한다** (설계 2.3). 종료일 일봉을 as_of 시점에
   알 수 있어야 채점한다. "값이 있으니 채점한다"로 하면 나중에 과거 일봉을 더 채워 넣는 순간
   이미 끝난 채점이 흔들리고, 미래 일봉이 섞여 들어와도 막을 수단이 없어진다.
3. **없으면 지어내지 않는다.** 데이터가 없으면 `eval_status='unable'` + 사유를 남긴다.
4. **이미 적은 결과는 다시 계산하지 않는다.** outcome 에 행이 있으면 건너뛴다. 기록을 뒤늦게
   고치지 않는 것이 이 시스템이 증명하려는 것 자체다 (결정 13).

**저장 규약 둘** (표 구조를 바꾸지 않고 두 가지를 담기 위한 선택):
- 보조 비교(종목 − 섹터)는 `factor_id` 에 `|vs_sector` 를 붙인 **별도 행**으로 남긴다.
  주 비교(결정 5의 종목 − KODEX 200)가 맨 이름을 그대로 가지므로 기존 조회·조인은 그대로 돌고,
  horizon 은 진짜 기간 값으로 남는다(기간 칸에 표식을 섞으면 유효 표본 계산이 깨진다).
- 요인 점수는 판단 버전(v0·LLM)과 무관하게 같은 값이므로 outcome 을 **v0 한 벌만** 남긴다.
  버전별 비교가 의미 있는 곳은 종합 점수뿐이고 (결정 4: LLM 기여는 조정 전후 종합 점수로 본다),
  `factor_id='composite'` 행은 버전마다 따로 남는다.
"""
import json
import math

from .factors.registry import load_specs
from .portfolio import known_bar_date, opt_cfg, start_day

# 설정 파일은 다른 담당자가 소유한다. 여기서 새로 필요해진 값만 이 표에 둔다 (cfg 에 있으면 그쪽이 이긴다).
DEFAULTS = {
    "scoring.min_cross_section": 5,      # 이보다 적은 날의 단면은 순위 상관을 내지 않는다 (섹터는 7개뿐)
    "scoring.n_buckets": 5,              # 점수 구간(5분위)별 평균 초과 수익
    "scoring.composite_horizons": None,  # None 이면 scoring.aux_horizons 를 그대로 쓴다
    "scoring.risk_flag_horizon": 20,     # 위험 표시 채점: 이후 며칠의 낙폭·변동성을 보는가
    "scoring.trading_days_per_year": 252,
}

# 보조 비교(종목 − 섹터) 행의 표식. 주 비교는 맨 factor_id 를 그대로 쓴다.
VS_SECTOR = "|vs_sector"
# 요인 점수 outcome 을 적어 두는 판단 버전 (요인 점수는 버전과 무관하다 — 위 저장 규약 참고).
FACTOR_VARIANT = "v0"
MARKET_ENTITY = "MARKET"


# ---------------------------------------------------------------- 가격 읽기

class _Prices:
    """종목별 일봉 캐시. 채점은 같은 종목을 여러 요인·기간으로 반복해서 묻는다."""

    def __init__(self, store):
        self.store = store
        self._cache = {}

    def _series(self, code):
        if code not in self._cache:
            rows = self.store.conn.execute(
                "SELECT date, open, close, adj_close, volume FROM price_daily WHERE code=?",
                (code,)).fetchall()
            self._cache[code] = {r["date"]: r for r in rows}
        return self._cache[code]

    def row(self, code, day):
        return self._series(code).get(str(day))

    def open(self, code, day):
        r = self.row(code, day)
        return None if r is None or r["open"] is None else float(r["open"])

    def adj_close(self, code, day):
        """평가·채점은 수정 종가로 한다. 수집 대체 경로에서 비어 있으면 종가로 물러선다."""
        r = self.row(code, day)
        if r is None:
            return None
        v = r["adj_close"] if r["adj_close"] is not None else r["close"]
        return None if v is None else float(v)

    def volume(self, code, day):
        r = self.row(code, day)
        return None if r is None or r["volume"] is None else float(r["volume"])

    def ret(self, code, start, end):
        """시가(시작일) → 수정 종가(종료일) 수익률. 못 구하면 (None, 사유)."""
        o = self.open(code, start)
        if not o:
            return None, f"{code} {start} 시가 없음"
        vol = self.volume(code, start)
        if vol is not None and vol <= 0:
            return None, f"{code} {start} 거래량 0 (체결 불가)"
        c = self.adj_close(code, end)
        if c is None:
            return None, f"{code} {end} 종가 없음"
        return c / o - 1.0, None


# ---------------------------------------------------------------- 사후 결과

def _sector_map(store, as_of):
    """{종목 코드: 섹터}. 그 판단 시점의 universe 스냅샷을 본다 (나중에 바뀐 분류를 소급하지 않게)."""
    row = store.conn.execute("SELECT MAX(as_of_date) FROM universe WHERE as_of_date<=?",
                             (str(as_of),)).fetchone()
    snap = row[0] if row else None
    if snap is None:
        snap = (store.conn.execute("SELECT MIN(as_of_date) FROM universe").fetchone() or [None])[0]
    if snap is None:
        return {}
    return {r["code"]: r["sector"] for r in store.conn.execute(
        "SELECT code, sector FROM universe WHERE as_of_date=?", (snap,)).fetchall()}


def score_outcomes(store, cfg, cal, as_of, mode="live"):
    """만기가 된 (실행, 버전, 자산, 요인, 기간)의 사후 결과를 outcome 에 넣는다 (설계 7.1).

    최종 배치가 하루 한 번 부른다. 아직 만기가 안 된 조합은 **행을 만들지 않는다** — 'unable' 로
    적어 두면 나중에 만기가 왔을 때 이미 적힌 행 때문에 채점을 못 한다.
    """
    specs = load_specs(cfg)
    uni = cfg["universe"]
    core, cash = uni["core_etf"], uni["cash_etf"]
    sector_etf = dict(uni.get("sector_etfs") or {})
    etf_to_sector = {v: k for k, v in sector_etf.items()}
    aux = sorted({int(h) for h in ((cfg.get("scoring") or {}).get("aux_horizons") or [])})
    comp_horizons = opt_cfg(cfg, "scoring.composite_horizons", DEFAULTS) or aux
    comp_horizons = sorted({int(h) for h in comp_horizons})
    known = known_bar_date(cal, as_of, cfg)
    px = _Prices(store)
    now = store.now_wall()
    counts = {"ok": 0, "unable": 0, "immature": 0, "existing": 0, "runs": 0}
    payload = []

    runs = store.conn.execute(
        "SELECT run_id, stage, as_of FROM run WHERE mode=? ORDER BY as_of, run_id", (mode,)).fetchall()
    for run in runs:
        run_id = int(run["run_id"])
        start = start_day(cal, run["stage"], run["as_of"])
        if start > known:
            counts["immature"] += 1
            continue                                     # 체결도 아직 일어나지 않은 판단
        existing = {(r[0], r[1], r[2], int(r[3])) for r in store.conn.execute(
            "SELECT variant, entity, factor_id, horizon FROM outcome WHERE run_id=?", (run_id,))}
        sector_of = _sector_map(store, run["as_of"])
        ends, mature = {}, {}

        def end_of(h):
            if h not in ends:
                ends[h] = cal.add_trading_days(start, h)
                mature[h] = ends[h] <= known
            return ends[h]

        jobs = []                                        # (variant, entity, factor_id, horizon, layer)
        for r in store.conn.execute(
                "SELECT entity, factor_id FROM factor_value WHERE run_id=? AND "
                "(missing IS NULL OR missing=0)", (run_id,)):
            spec = specs.get(r["factor_id"])
            if spec is None:
                continue                                 # 설정에서 빠진 옛 요인은 현재 기준으로 채점하지 않는다
            for h in sorted({int(spec.horizon)} | set(aux)):
                jobs.append((FACTOR_VARIANT, r["entity"], r["factor_id"], h, spec.layer))
        for r in store.conn.execute(
                "SELECT variant, entity, layer FROM composite WHERE run_id=?", (run_id,)):
            for h in comp_horizons:
                jobs.append((r["variant"], r["entity"], "composite", h, r["layer"]))

        for variant, entity, factor_id, horizon, layer in jobs:
            end = end_of(horizon)
            if not mature[horizon]:
                counts["immature"] += 1
                continue
            for fid, asset, bench, reason in _targets(layer, entity, core, cash, sector_etf,
                                                      etf_to_sector, sector_of, factor_id):
                key = (variant, entity, fid, horizon)
                if key in existing:
                    counts["existing"] += 1
                    continue
                existing.add(key)
                if reason is not None:
                    payload.append((run_id, variant, entity, fid, horizon, str(start), str(end),
                                    None, None, None, "unable", reason, now))
                    counts["unable"] += 1
                    continue
                a_ret, a_why = px.ret(asset, start, end)
                b_ret, b_why = px.ret(bench, start, end)
                if a_ret is None or b_ret is None:
                    payload.append((run_id, variant, entity, fid, horizon, str(start), str(end),
                                    a_ret, b_ret, None, "unable", a_why or b_why, now))
                    counts["unable"] += 1
                else:
                    payload.append((run_id, variant, entity, fid, horizon, str(start), str(end),
                                    a_ret, b_ret, a_ret - b_ret, "ok", None, now))
                    counts["ok"] += 1
        counts["runs"] += 1

    if payload:
        store.conn.executemany(
            "INSERT OR IGNORE INTO outcome(run_id,variant,entity,factor_id,horizon,start_date,end_date,"
            "asset_ret,bench_ret,excess_ret,eval_status,unable_reason,computed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", payload)
        store.commit()
    return counts


def _targets(layer, entity, core, cash, sector_etf, etf_to_sector, sector_of, factor_id):
    """(factor_id, 자산, 비교 대상, 불가 사유) 목록. 종목 계층은 보조 비교(|vs_sector) 행을 하나 더 낸다."""
    if layer == "market":
        return [(factor_id, core, cash, None)]
    if layer == "sector":
        name = entity if entity in sector_etf else etf_to_sector.get(entity)
        etf = sector_etf.get(name)
        if not etf:
            return [(factor_id, None, None, f"섹터 ETF 를 찾을 수 없습니다: {entity}")]
        return [(factor_id, etf, core, None)]
    rows = [(factor_id, entity, core, None)]
    etf = sector_etf.get(sector_of.get(entity))
    if etf and etf != entity:
        rows.append((factor_id + VS_SECTOR, entity, etf, None))
    return rows


# ---------------------------------------------------------------- 통계 보조

def _ranks(values):
    """평균 순위 (동점은 평균 순위). 스피어만 상관은 이 순위 위의 피어슨 상관이다."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for t in range(i, j + 1):
            out[order[t]] = avg
        i = j + 1
    return out


def _pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None                                      # 한쪽이 전부 같은 값이면 상관이 정의되지 않는다
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def spearman(xs, ys):
    """스피어만 순위 상관 (결정 5의 주 지표). 정의되지 않으면 None."""
    return _pearson(_ranks(list(xs)), _ranks(list(ys)))


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _std(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


# ---------------------------------------------------------------- 요인 지표

def factor_metrics(store, cfg, mode="live", stage=None, variant=None, persist=True):
    """요인별 지표 행 목록 (설계 7.2). persist=True 면 factor_metric 에 덧붙인다.

    - 종목·섹터 요인: 날짜별 단면의 **스피어만 순위 상관(rank IC)** 평균·표준편차, 5분위 평균 초과 수익,
      부호 적중률. 단면이 `scoring.min_cross_section` 개보다 적은 날은 건너뛴다.
    - 시장 요인: 하루에 값이 하나라 날짜 안 상관이 없다 → 점수 부호별 평균 초과 수익과 적중률,
      순위 상관 칸은 NULL.
    - 언제나 `n_days` 와 **`n_eff = n_days ÷ horizon`** 을 함께 낸다. 매일 판단 × 20일 기간이면
      관측 60건이 독립 표본 3건이라는 사실을 숨기지 않기 위해서다 (결정 5).
    - 단계별·판단 버전별로 따로 낸다. LLM 조정의 기여는 `composite` 와 `composite_base` 의
      순위 상관 차이로 본다 (`llm_adjust_contribution`).
    - 결측(missing=1)으로 적힌 요인 값은 들어가지 않는다.
    """
    specs = load_specs(cfg)
    min_n = int(opt_cfg(cfg, "scoring.min_cross_section", DEFAULTS))
    n_buckets = int(opt_cfg(cfg, "scoring.n_buckets", DEFAULTS))

    scores = {}
    for r in store.conn.execute(
            "SELECT fv.run_id, fv.entity, fv.factor_id, fv.score FROM factor_value fv "
            "JOIN run r ON r.run_id=fv.run_id WHERE r.mode=? AND (fv.missing IS NULL OR fv.missing=0)",
            (mode,)):
        if r[3] is not None:
            scores[(int(r[0]), r[1], r[2])] = float(r[3])
    composites = {}
    for r in store.conn.execute(
            "SELECT c.run_id, c.variant, c.entity, c.layer, c.base_score, c.final_score FROM composite c "
            "JOIN run r ON r.run_id=c.run_id WHERE r.mode=?", (mode,)):
        composites[(int(r[0]), r[1], r[2])] = (r[3], r[4], r[5])

    sql = ("SELECT r.stage AS stage, o.run_id, o.variant, o.entity, o.factor_id, o.horizon, "
           "o.start_date, o.excess_ret FROM outcome o JOIN run r ON r.run_id=o.run_id "
           "WHERE r.mode=? AND o.eval_status='ok' AND o.excess_ret IS NOT NULL")
    params = [mode]
    if stage is not None:
        sql += " AND r.stage=?"
        params.append(stage)
    if variant is not None:
        sql += " AND o.variant=?"
        params.append(variant)

    groups = {}
    for o in store.conn.execute(sql, params):
        fid = o["factor_id"]
        aux_row = fid.endswith(VS_SECTOR)
        base_fid = fid[: -len(VS_SECTOR)] if aux_row else fid
        run_id, entity = int(o["run_id"]), o["entity"]
        if base_fid == "composite":
            comp = composites.get((run_id, o["variant"], entity))
            if comp is None:
                continue
            layer, base_score, final_score = comp
            layer = layer or "stock"
            pairs = (("composite", final_score), ("composite_base", base_score))
        else:
            spec = specs.get(base_fid)
            if spec is None:
                continue
            layer = spec.layer
            pairs = ((base_fid, scores.get((run_id, entity, base_fid))),)
        for name, score in pairs:
            if score is None:
                continue
            # 종합 점수는 계층마다 뜻이 다르다. 종목 계층이 '종합 점수' 본래 뜻이라 맨 이름을 쓰고,
            # 시장·섹터 계층은 |market·|sector 를 붙여 한 지표 행에 섞이지 않게 한다.
            metric_fid = name if (base_fid != "composite" or layer == "stock") else f"{name}|{layer}"
            if aux_row:
                metric_fid += VS_SECTOR
            key = (o["stage"], o["variant"], metric_fid, int(o["horizon"]))
            g = groups.setdefault(key, {"layer": layer, "days": {}})
            day = g["days"].setdefault(o["start_date"], {})
            prev = day.get(entity)
            if prev is None or prev[0] <= run_id:        # 같은 날 여러 실행이면 마지막 실행을 쓴다
                day[entity] = (run_id, float(score), float(o["excess_ret"]))

    rows = []
    for (stg, var, fid, horizon), g in sorted(groups.items()):
        if g["layer"] == "market":
            metrics = _market_metrics(g["days"])
        else:
            metrics = _cross_metrics(g["days"], min_n, n_buckets)
        if not metrics["n_days"]:
            continue
        rows.append({"stage": stg, "variant": var, "factor_id": fid, "horizon": horizon,
                     "layer": g["layer"], "n_days": metrics["n_days"],
                     "n_eff": metrics["n_days"] / float(horizon) if horizon else None,
                     "rank_ic_mean": metrics["rank_ic_mean"], "rank_ic_std": metrics["rank_ic_std"],
                     "hit_rate": metrics["hit_rate"],
                     "bucket_json": json.dumps(metrics["buckets"], ensure_ascii=False)})
    if persist and rows:
        _persist(store, rows)
    return rows


def _cross_metrics(by_day, min_n, n_buckets):
    """날짜별 단면의 순위 상관·구간별 평균 초과 수익·적중률 (종목·섹터 계층)."""
    ics, hits, total, days = [], 0, 0, 0
    buckets = {q: [0, 0.0] for q in range(1, n_buckets + 1)}
    for day in sorted(by_day):
        items = sorted(by_day[day].items())               # 동점을 개체 이름으로 갈라 매번 같은 결과가 나오게
        if len(items) < min_n:
            continue
        days += 1
        ss = [v[1] for _, v in items]
        xs = [v[2] for _, v in items]
        ic = spearman(ss, xs)
        if ic is not None:
            ics.append(ic)
        for s, x in zip(ss, xs):
            if s == 0 or x == 0:
                continue
            total += 1
            hits += 1 if s * x > 0 else 0
        order = sorted(range(len(items)), key=lambda i: (ss[i], items[i][0]))
        n = len(order)
        for pos, i in enumerate(order):
            q = min(n_buckets, int(pos * n_buckets / n) + 1)   # q1 = 점수가 가장 낮은 구간
            buckets[q][0] += 1
            buckets[q][1] += xs[i]
    return {"n_days": days, "rank_ic_mean": _mean(ics), "rank_ic_std": _std(ics),
            "hit_rate": (hits / total) if total else None,
            "buckets": {"kind": "quantile",
                        "q": [{"bucket": q, "n": buckets[q][0],
                               "mean_excess": (buckets[q][1] / buckets[q][0]) if buckets[q][0] else None}
                              for q in sorted(buckets)]}}


def _market_metrics(by_day):
    """시장 계층: 점수 부호별 평균 초과 수익과 부호 적중률 (날짜 안 단면이 없다)."""
    acc = {"pos": [0, 0.0], "neg": [0, 0.0], "zero": [0, 0.0]}
    hits = total = days = 0
    for day in sorted(by_day):
        obs = sorted(by_day[day].items())
        if not obs:
            continue
        days += 1
        for _, (_, score, excess) in obs:
            key = "pos" if score > 0 else ("neg" if score < 0 else "zero")
            acc[key][0] += 1
            acc[key][1] += excess
            if score and excess:
                total += 1
                hits += 1 if score * excess > 0 else 0
    return {"n_days": days, "rank_ic_mean": None, "rank_ic_std": None,
            "hit_rate": (hits / total) if total else None,
            "buckets": {"kind": "sign",
                        "sign": {k: {"n": v[0], "mean_excess": (v[1] / v[0]) if v[0] else None}
                                 for k, v in acc.items()}}}


def _persist(store, rows):
    """factor_metric 은 덧붙이기 표다. 리포트는 가장 최근 computed_at 묶음을 읽는다."""
    now = store.now_wall()
    store.conn.executemany(
        "INSERT INTO factor_metric(computed_at,stage,variant,factor_id,horizon,n_days,n_eff,"
        "rank_ic_mean,rank_ic_std,hit_rate,bucket_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(now, r["stage"], r.get("variant"), r["factor_id"], r["horizon"], r["n_days"], r.get("n_eff"),
          r.get("rank_ic_mean"), r.get("rank_ic_std"), r.get("hit_rate"), r.get("bucket_json"))
         for r in rows])
    store.commit()
    return now


def llm_adjust_contribution(rows):
    """LLM 조정의 기여 = 순위 상관(조정 후 종합 점수) − 순위 상관(조정 전) (결정 4·설계 7.2).

    포트폴리오 수익이 아니라 자산별 점수 수준에서 재는 이유는, 조정 폭이 ±0.2 로 작아 단기간의
    수익 차이로는 구분이 되지 않기 때문이다.
    """
    idx = {(r["stage"], r["variant"], r["factor_id"], r["horizon"]): r for r in rows}
    out = []
    for (stg, var, fid, horizon), row in sorted(idx.items()):
        if var != "llm" or not fid.startswith("composite") or fid.startswith("composite_base"):
            continue
        base = idx.get((stg, var, fid.replace("composite", "composite_base", 1), horizon))
        if base is None or row["rank_ic_mean"] is None or base["rank_ic_mean"] is None:
            continue
        out.append({"stage": stg, "factor_id": fid, "horizon": horizon,
                    "rank_ic_adjusted": row["rank_ic_mean"], "rank_ic_base": base["rank_ic_mean"],
                    "contribution": row["rank_ic_mean"] - base["rank_ic_mean"],
                    "n_eff": row.get("n_eff")})
    return out


# ---------------------------------------------------------------- 위험 표시 채점

def _forward_risk(px, code, days, days_per_year):
    """이후 구간의 (최대 낙폭, 연율 변동성). 값이 모자라면 (None, None)."""
    prices = [px.adj_close(code, d) for d in days]
    prices = [p for p in prices if p]
    if len(prices) < 2:
        return None, None
    peak, mdd = prices[0], 0.0
    for p in prices:
        peak = max(peak, p)
        mdd = min(mdd, p / peak - 1.0)
    rets = [prices[i] / prices[i - 1] - 1.0 for i in range(1, len(prices))]
    vol = _std(rets)
    return mdd, (vol * math.sqrt(float(days_per_year)) if vol is not None else None)


def risk_flag_metrics(store, cfg, cal, as_of, mode="live", horizon=None, persist=True):
    """위험 표시 채점: 표시가 붙은 자산이 실제로 낙폭·변동성이 컸는가 (결정 5, 설계 7.2).

    표시는 점수와 별개라 순위 상관으로 잴 수 없다. 대신 표시가 붙은 무리와 아닌 무리의 이후
    최대 낙폭·변동성 평균을 나란히 남긴다. factor_metric 표를 그대로 쓰되 factor_id 를
    `risk_flag|<유형>` 으로 두고 비교 결과를 bucket_json 에 담는다 (표 구조를 바꾸지 않기 위해).
    """
    specs = load_specs(cfg)
    stock_factors = {fid for fid, s in specs.items() if s.layer == "stock"}
    horizon = int(horizon or opt_cfg(cfg, "scoring.risk_flag_horizon", DEFAULTS))
    dpy = opt_cfg(cfg, "scoring.trading_days_per_year", DEFAULTS)
    known = known_bar_date(cal, as_of, cfg)
    px = _Prices(store)
    acc = {}                                              # (stage, flag_type) → 누적

    for run in store.conn.execute(
            "SELECT run_id, stage, as_of FROM run WHERE mode=? ORDER BY as_of, run_id", (mode,)):
        run_id = int(run["run_id"])
        start = start_day(cal, run["stage"], run["as_of"])
        end = cal.add_trading_days(start, horizon)
        if end > known:
            continue
        # 채점 대상은 그 실행의 종목 계층 대상 전부다 (표시가 붙지 않은 쪽이 비교군이 된다)
        entities = sorted({r["entity"] for r in store.conn.execute(
            "SELECT entity, factor_id FROM factor_value WHERE run_id=?", (run_id,))
            if r["factor_id"] in stock_factors})
        if not entities:
            continue
        flags = {}
        for r in store.conn.execute("SELECT entity, flag_type FROM risk_flag WHERE run_id=?", (run_id,)):
            flags.setdefault(r["entity"], set()).add(r["flag_type"])
        types = {"any"} | {t for s in flags.values() for t in s}
        days = cal.trading_days_between(start, end)
        risk = {}
        for code in entities:
            mdd, vol = _forward_risk(px, code, days, dpy)
            if mdd is not None:
                risk[code] = (mdd, vol)
        if not risk:
            continue
        for flag_type in types:
            slot = acc.setdefault((run["stage"], flag_type),
                                  {"flagged": [], "unflagged": [], "days": set()})
            slot["days"].add(str(start))
            for code, (mdd, vol) in risk.items():
                marked = bool(flags.get(code)) if flag_type == "any" else (flag_type in flags.get(code, ()))
                slot["flagged" if marked else "unflagged"].append((mdd, vol))

    rows = []
    for (stg, flag_type), slot in sorted(acc.items()):
        if not slot["flagged"]:
            continue                                      # 표시가 한 번도 안 붙었으면 비교할 것이 없다
        bucket = {"kind": "risk_flag", "flag_type": flag_type, "groups": {}}
        for group in ("flagged", "unflagged"):
            vals = slot[group]
            bucket["groups"][group] = {
                "n": len(vals),
                "mean_max_drawdown": _mean([v[0] for v in vals]),
                "mean_vol": _mean([v[1] for v in vals])}
        n_days = len(slot["days"])
        rows.append({"stage": stg, "variant": None, "factor_id": f"risk_flag|{flag_type}",
                     "horizon": horizon, "layer": "stock", "n_days": n_days,
                     "n_eff": n_days / float(horizon), "rank_ic_mean": None, "rank_ic_std": None,
                     "hit_rate": None, "bucket_json": json.dumps(bucket, ensure_ascii=False)})
    if persist and rows:
        _persist(store, rows)
    return rows


__all__ = ["DEFAULTS", "VS_SECTOR", "FACTOR_VARIANT", "MARKET_ENTITY", "score_outcomes",
           "factor_metrics", "risk_flag_metrics", "llm_adjust_contribution", "spearman"]
