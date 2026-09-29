"""도전 가중치 안 사후 재평가 (결정 6, 설계 7.3).

    python -m backend.advisor.reeval --weights challenger.yaml [--from 날짜 --to 날짜]
                                     [--mode live|replay] [--db 경로]

저장된 `factor_value` 에 **새 가중치를 사후 적용**해 계층 점수·종합 점수·목표 비중을 다시 만들고,
순위 상관과 비용 반영 NAV 를 현재 안과 나란히 출력한다. 이것이 가능한 이유는 가중치 0 인 관찰
요인까지 매일 원본 점수를 저장해 두기 때문이다 (결정 6) — 다시 수집할 것도, LLM 을 다시 부를 것도 없다.

지키는 선 셋.

1. **살아 있는 기록에 한 줄도 쓰지 않는다.** DB 는 읽기 전용으로 열고 NAV 재현은 메모리 안에서만 한다.
   도전 안의 판단이 실제 판단 기록에 섞이면 그날 무엇을 판단했는지가 사후에 흐려진다.
2. **설정 파일을 고치지 않는다.** 출력은 판정문까지다. 교체는 사람이 `advisor.config.yaml` 을 바꿔
   커밋해야 일어나고, 그러면 config_hash 가 바뀌어 모든 실행 기록에 남는다 (결정 6).
3. **LLM 조정 단계는 재현하지 않는다.** 저장된 LLM 요인 점수는 그대로 쓰지만 조정·거부는 다시 부르지
   않고 v0 수준에서 비교한다 (설계 7.3).

보유 이력(exit_rank)은 **도전 안 자신의 경로**를 따라 흐른다. 챔피언이 들고 있던 종목을 도전 안의
보유로 치면, 도전 안이 한 번도 담은 적 없는 종목을 "보유라서 남긴다"고 판단하게 된다.
"""
import argparse
import copy
import sys
import unicodedata

import yaml

from . import allocate, combine, portfolio
from .calendar import TradingCalendar, to_date
from .config import config_hash, load_config, resolve_path, validate_config
from .factors.registry import load_specs
from .scoring import MARKET_ENTITY, VS_SECTOR, spearman
from .store import Store

# 설정 파일은 다른 담당자가 소유한다. 여기서 새로 필요해진 값만 이 표에 둔다 (cfg 에 있으면 그쪽이 이긴다).
DEFAULTS = {
    "reeval.horizon": 20,        # 비교에 쓰는 채점 기간(거래일). 종합 점수의 주 기간에 해당한다
    "reeval.min_cross_section": 5,   # 그날 단면이 이보다 적으면 순위 상관을 내지 않는다
    "reeval.nav_tolerance": 0.0,     # '비용 반영 NAV 가 나쁘지 않을 것' 의 허용 폭
}


# ---------------------------------------------------------------- 도전 안 읽기

def load_challenger(path, base_cfg):
    """도전 안 YAML 을 현재 설정 위에 얹는다. 부분 덮어쓰기이고 원본은 건드리지 않는다.

    형식: {factors: {factor_id: 가중치 | {필드: 값}}, combine: {...}, allocate: {...}, costs: {...}}
    없는 요인 id 는 오타로 보고 멈춘다 — 조용히 무시하면 "가중치를 바꿨는데 결과가 같다"가 된다.
    """
    with open(path, encoding="utf-8") as f:
        patch = yaml.safe_load(f) or {}
    if not isinstance(patch, dict):
        raise ValueError(f"도전 안 파일이 매핑이 아닙니다: {path}")
    cfg = copy.deepcopy(base_cfg)
    for fid, val in (patch.get("factors") or {}).items():
        if fid not in cfg["factors"]:
            raise ValueError(f"설정에 없는 요인입니다: {fid}")
        if isinstance(val, dict):
            cfg["factors"][fid].update(val)
        else:
            cfg["factors"][fid]["weight"] = val
    for section in ("combine", "allocate", "costs", "scoring", "reeval", "normalize"):
        if section in patch:
            cfg[section] = dict(cfg.get(section) or {}, **(patch[section] or {}))
    validate_config(cfg)
    return cfg, patch


# ---------------------------------------------------------------- 판단 재계산

def recompute_path(store, cfg, cal, mode="live", date_from=None, date_to=None, stage="final"):
    """저장된 하위 점수로 그 설정의 판단을 처음부터 다시 만든다.

    반환 {"runs": [...], "targets": {체결일: {자산: 비중}}, "scores": {run_id: {종목: 종합 점수}}}.
    비중 계산은 `combine`·`allocate` 를 그대로 부른다 — 비교 대상이 다른 코드로 계산되면
    그 비교는 규칙의 차이가 아니라 구현의 차이를 재게 된다.
    """
    specs = load_specs(cfg)
    tilt = (cfg.get("combine") or {}).get("sector_tilt")
    sql = "SELECT run_id, stage, as_of FROM run WHERE mode=? AND stage=?"
    params = [mode, stage]
    if date_from:
        sql += " AND as_of>=?"
        params.append(str(to_date(date_from)))
    if date_to:
        sql += " AND as_of<=?"
        params.append(str(to_date(date_to)))
    runs = store.conn.execute(sql + " ORDER BY as_of, run_id", params).fetchall()

    out = {"runs": [], "targets": {}, "scores": {}}
    holdings = set()
    for run in runs:
        run_id = int(run["run_id"])
        by_entity = {}
        for r in store.conn.execute(
                "SELECT entity, factor_id, score, missing FROM factor_value WHERE run_id=?", (run_id,)):
            by_entity.setdefault(r["entity"], {})[r["factor_id"]] = (
                r["score"], bool(r["missing"]))
        if not by_entity:
            continue
        sector_rows, stock_rows = {}, {}
        for entity, scores in by_entity.items():
            if entity == MARKET_ENTITY:
                continue
            layers = {specs[f].layer for f in scores if f in specs}
            if "sector" in layers:
                sector_rows[entity] = scores
            elif "stock" in layers:
                stock_rows[entity] = scores
        market = combine.market_score(by_entity.get(MARKET_ENTITY, {}), specs)
        sector_scores = combine.sector_scores(sector_rows, specs)
        stock_layer = {c: combine.layer_score(s, specs, layer="stock") for c, s in stock_rows.items()}
        sector_of = _sector_of(store, run["as_of"])
        stock_scores = combine.stock_composites(stock_layer, sector_of, sector_scores, tilt)
        flags = {}
        for r in store.conn.execute("SELECT entity, flag_type FROM risk_flag WHERE run_id=?", (run_id,)):
            flags.setdefault(r["entity"], []).append(r["flag_type"])
        rows = allocate.target_weights(market, sector_scores, stock_scores, holdings, flags, cfg)
        holdings = {r["asset"] for r in rows if r["role"] == "stock"}
        fill = portfolio.start_day(cal, run["stage"], run["as_of"])
        out["targets"][str(fill)] = allocate.as_dict(rows)
        out["scores"][run_id] = stock_scores
        out["runs"].append({"run_id": run_id, "as_of": run["as_of"], "fill_date": str(fill),
                            "market_score": market, "n_stocks": len(stock_scores)})
    return out


def _sector_of(store, as_of):
    """{종목: 섹터} — 그 판단 시점의 universe 스냅샷 기준."""
    row = store.conn.execute("SELECT MAX(as_of_date) FROM universe WHERE as_of_date<=?",
                             (str(as_of),)).fetchone()
    snap = row[0] if row else None
    if snap is None:
        return {}
    return {r["code"]: r["sector"] for r in store.conn.execute(
        "SELECT code, sector FROM universe WHERE as_of_date=?", (snap,)).fetchall()}


def composite_rank_ic(store, path, mode="live", horizon=20, min_n=5):
    """재계산한 종합 점수 × **저장된** 초과 수익의 날짜별 순위 상관 (설계 7.2).

    초과 수익은 채점이 이미 남긴 값을 그대로 쓴다. 같은 (실행, 종목, 기간)의 초과 수익은 어느
    요인 행에서 읽어도 같은 값이라(자산도 비교 대상도 같다) 요인 행을 함께 받아 표본을 넓힌다.
    보조 비교(|vs_sector) 행은 비교 대상이 달라 섞지 않는다.
    """
    ics, days = [], 0
    for run_id, scores in sorted(path["scores"].items()):
        excess = {}
        for r in store.conn.execute(
                "SELECT entity, factor_id, excess_ret FROM outcome WHERE run_id=? AND horizon=? "
                "AND eval_status='ok' AND excess_ret IS NOT NULL", (run_id, int(horizon))):
            if r["factor_id"].endswith(VS_SECTOR) or r["entity"] not in scores:
                continue
            excess.setdefault(r["entity"], float(r["excess_ret"]))
        pairs = sorted((e, scores[e], x) for e, x in excess.items())
        if len(pairs) < min_n:
            continue
        days += 1
        ic = spearman([p[1] for p in pairs], [p[2] for p in pairs])
        if ic is not None:
            ics.append(ic)
    mean = sum(ics) / len(ics) if ics else None
    return {"rank_ic_mean": mean, "n_days": days, "n_eff": days / float(horizon) if horizon else None,
            "n_ic": len(ics)}


def matches_stored(store, path, variant="v0", tol=1e-9):
    """재계산한 판단이 그때 저장된 판단과 같은가 (재계산 경로가 옳은지 검산하는 용도)."""
    same = total = 0
    for run in path["runs"]:
        rows = store.conn.execute("SELECT asset, weight FROM target_weight WHERE run_id=? AND variant=?",
                                  (run["run_id"], variant)).fetchall()
        if not rows:
            continue
        total += 1
        stored = {r["asset"]: float(r["weight"]) for r in rows}
        got = path["targets"].get(run["fill_date"], {})
        if set(stored) == set(got) and all(abs(stored[a] - got[a]) <= tol for a in stored):
            same += 1
    return same, total


# ---------------------------------------------------------------- 비교와 판정

def run_reeval(store, cfg, cal, challenger_cfg, mode="live", date_from=None, date_to=None, horizon=None):
    """챔피언(현재 설정)과 도전 안을 같은 절차로 계산해 나란히 돌려준다. DB 에는 쓰지 않는다."""
    horizon = int(horizon or portfolio.opt_cfg(cfg, "reeval.horizon", DEFAULTS))
    min_n = int(portfolio.opt_cfg(cfg, "reeval.min_cross_section", DEFAULTS))
    out = {"horizon": horizon, "mode": mode, "from": date_from, "to": date_to,
           "config_hash": {"champion": config_hash(cfg), "challenger": config_hash(challenger_cfg)},
           "weight_diff": _weight_diff(cfg, challenger_cfg)}
    paths = {}
    for name, conf in (("champion", cfg), ("challenger", challenger_cfg)):
        path = recompute_path(store, conf, cal, mode=mode, date_from=date_from, date_to=date_to)
        paths[name] = path
        ic = composite_rank_ic(store, path, mode=mode, horizon=horizon, min_n=min_n)
        sim = portfolio.simulate(store, conf, cal, path["targets"])
        out[name] = {"ic": ic, "nav": sim["summary"], "n_runs": len(path["runs"]),
                     "days_simulated": len(sim["records"])}
    # 챔피언을 현재 설정으로 다시 계산한 결과가 그때 저장된 v0 판단과 같은지 검산한다 —
    # 재계산 경로가 실제 파이프라인과 어긋나 있으면 비교 자체가 무의미해지기 때문이다.
    out["reproduced"] = matches_stored(store, paths["champion"])
    out["verdict"] = _verdict(cfg, out)
    return out


def _weight_diff(base, challenger):
    """가중치·주요 설정에서 실제로 달라진 항목만 뽑는다 (출력에 그대로 싣는다)."""
    diff = []
    for fid, meta in (challenger.get("factors") or {}).items():
        old = ((base.get("factors") or {}).get(fid) or {}).get("weight")
        if meta.get("weight") != old:
            diff.append({"key": f"factors.{fid}.weight", "champion": old, "challenger": meta.get("weight")})
    for section in ("combine", "allocate", "costs"):
        for key, val in (challenger.get(section) or {}).items():
            old = (base.get(section) or {}).get(key)
            if val != old:
                diff.append({"key": f"{section}.{key}", "champion": old, "challenger": val})
    return diff


def _verdict(cfg, out):
    """교체 조건 판정 (설계 7.3). 조건을 넘어도 **교체는 사람이 커밋해야** 일어난다."""
    min_n_eff = float((cfg.get("reeval") or {}).get("min_n_eff", 12))
    tol = float(portfolio.opt_cfg(cfg, "reeval.nav_tolerance", DEFAULTS))
    ch, cp = out["challenger"], out["champion"]
    ic_ch, ic_cp = ch["ic"]["rank_ic_mean"], cp["ic"]["rank_ic_mean"]
    n_eff = ch["ic"]["n_eff"] or 0.0
    nav_ch, nav_cp = ch["nav"]["nav"], cp["nav"]["nav"]
    checks = [
        {"name": f"유효 표본 n_eff ≥ {min_n_eff:g}", "ok": n_eff >= min_n_eff,
         "detail": f"n_eff={n_eff:.2f}"},
        {"name": "종합 점수 순위 상관 개선", "ok": ic_ch is not None and ic_cp is not None and ic_ch > ic_cp,
         "detail": f"{_fmt(ic_cp)} → {_fmt(ic_ch)}"},
        {"name": "비용 반영 NAV 비열위", "ok": nav_ch is not None and nav_cp is not None and nav_ch >= nav_cp - tol,
         "detail": f"{_fmt(nav_cp, 4)} → {_fmt(nav_ch, 4)}"},
    ]
    passed = all(c["ok"] for c in checks)
    text = ("교체 조건을 모두 충족했다. 다만 자동 교체는 하지 않는다 — "
            "사람이 advisor.config.yaml 을 바꿔 커밋해야 교체된다 (결정 6)."
            if passed else
            "교체 조건 미달: " + ", ".join(c["name"] for c in checks if not c["ok"]) + ". 현재 안을 유지한다.")
    return {"passed": passed, "checks": checks, "text": text}


def _fmt(v, digits=4):
    return "-" if v is None else f"{v:.{digits}f}"


def _width(text):
    """터미널에서 차지하는 칸 수. 한글은 두 칸이라 글자 수로 맞추면 표가 어긋난다."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text))


def _cell(text, width, right=False):
    pad = " " * max(0, width - _width(text))
    return (pad + str(text)) if right else (str(text) + pad)


def render(out):
    """사람이 읽는 나란한 표. 숫자를 못 구한 칸은 0 이 아니라 '-' 로 둔다."""
    lines = []
    lines.append(f"도전 가중치 안 사후 재평가 (모드 {out['mode']}, 채점 기간 {out['horizon']}거래일, "
                 f"기간 {out['from'] or '처음'} ~ {out['to'] or '끝'})")
    lines.append(f"설정 지문: 챔피언 {out['config_hash']['champion']} / 도전안 {out['config_hash']['challenger']}")
    if out["weight_diff"]:
        lines.append("바뀐 값:")
        for d in out["weight_diff"]:
            lines.append(f"  - {d['key']}: {d['champion']} → {d['challenger']}")
    else:
        lines.append("바뀐 값: 없음 (도전 안이 현재 설정과 같다)")
    same, total = out["reproduced"]
    lines.append(f"재계산 검산: 저장된 v0 판단 {total}건 중 {same}건이 그대로 재현됨")
    lines.append("")
    w = (30, 16, 16)
    head = _cell("항목", w[0]) + _cell("챔피언(v0)", w[1], True) + _cell("도전안", w[2], True)
    lines.append(head)
    lines.append("-" * _width(head))
    cp, ch = out["champion"], out["challenger"]
    rows = [
        ("판단 일수", f"{cp['n_runs']}", f"{ch['n_runs']}"),
        ("종합 점수 순위 상관", _fmt(cp["ic"]["rank_ic_mean"]), _fmt(ch["ic"]["rank_ic_mean"])),
        ("관측 일수 / 유효 표본", f"{cp['ic']['n_days']} / {_fmt(cp['ic']['n_eff'], 2)}",
         f"{ch['ic']['n_days']} / {_fmt(ch['ic']['n_eff'], 2)}"),
        ("NAV (비용 반영)", _fmt(cp["nav"]["nav"]), _fmt(ch["nav"]["nav"])),
        ("누적 수익률", _fmt(cp["nav"]["total_return"]), _fmt(ch["nav"]["total_return"])),
        ("최대 낙폭", _fmt(cp["nav"]["max_drawdown"]), _fmt(ch["nav"]["max_drawdown"])),
        ("연율 변동성", _fmt(cp["nav"]["ann_vol"]), _fmt(ch["nav"]["ann_vol"])),
        ("회전율 합 / 비용 합", f"{_fmt(cp['nav']['turnover'], 3)} / {_fmt(cp['nav']['cost'], 4)}",
         f"{_fmt(ch['nav']['turnover'], 3)} / {_fmt(ch['nav']['cost'], 4)}"),
        ("정산 일수", f"{cp['days_simulated']}", f"{ch['days_simulated']}"),
    ]
    for name, a, b in rows:
        lines.append(_cell(name, w[0]) + _cell(a, w[1], True) + _cell(b, w[2], True))
    lines.append("")
    for c in out["verdict"]["checks"]:
        lines.append(f"  [{'O' if c['ok'] else 'X'}] {c['name']} ({c['detail']})")
    lines.append("")
    lines.append("판정: " + out["verdict"]["text"])
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m backend.advisor.reeval",
        description="저장된 하위 점수에 도전 가중치 안을 사후 적용해 현재 안과 비교한다 (설계 7.3)")
    ap.add_argument("--weights", required=True, help="도전 안 YAML 경로")
    ap.add_argument("--from", dest="date_from", help="기준일 시작 (YYYY-MM-DD)")
    ap.add_argument("--to", dest="date_to", help="기준일 끝 (YYYY-MM-DD)")
    ap.add_argument("--mode", default="live", choices=["live", "replay"])
    ap.add_argument("--db", help="advisor.db 경로 (기본값은 설정의 paths.db)")
    ap.add_argument("--horizon", type=int, help="비교에 쓸 채점 기간(거래일)")
    ap.add_argument("--config", help="설정 파일 경로 (기본값은 저장소의 advisor.config.yaml)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    challenger_cfg, _ = load_challenger(args.weights, cfg)
    db = args.db or resolve_path(cfg, "db")
    store = Store(db, readonly=True)          # 살아 있는 표에 한 줄도 쓰지 않는다 (읽기 전용으로 연다)
    try:
        cal = TradingCalendar(cfg, store)
        out = run_reeval(store, cfg, cal, challenger_cfg, mode=args.mode,
                         date_from=args.date_from, date_to=args.date_to, horizon=args.horizon)
        print(render(out))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
