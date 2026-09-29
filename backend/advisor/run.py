"""배치 진입점 (설계 4장). 판단 한 번의 흐름을 **여기 한 곳에서만** 엮는다.

```
python -m backend.advisor.run --stage prelim|final [--as-of "2026-06-01" | "2026-06-01T07:40"]
                              [--mode live|replay] [--db PATH] [--no-ingest] [--config PATH]
                              [--no-llm] [--no-portfolio]
python -m backend.advisor.run --replay-range 2026-06-01 2026-06-30      # 최종 단계를 재현 모드로 반복
python -m backend.advisor.run --replay-range 2026-06-01 2026-06-30 --replay-stages final prelim
python -m backend.advisor.run --promote-prelim                          # 08:50 대체 규칙 (결정 11)
```

흐름은 설계 4장의 1~8번이고 **v0 판단까지**가 이 파일의 범위다.
5번(LLM 요인), 7번(LLM 조정 → llm 판단), 9번(체결 예약·확정), 10번(사후 채점)은 담당이 다르므로
`hooks` 라는 **평범한 함수 자리**로 비워 뒀다. 플러그인 체계를 만들지 않은 이유는, 자리가 네 개뿐이고
각자 자기 모듈을 만든 뒤 `run_once(..., hooks={...})` 로 넘기면 끝이기 때문이다.
실제 배치가 쓰는 훅 묶음은 `advisor/hooks.py` 의 `default_hooks(cfg)` 이고, CLI 는 그것을 쓴다.
`run_once(..., hooks=None)` 은 지금도 "훅 없음"(v0 만)이라는 뜻 그대로다.

설계 원칙 세 가지가 이 파일의 모양을 정한다.

1. **LLM 없이 끝까지 돈다** (결정 4). 수집이 실패해도, 요인 하나가 비어도, 훅이 하나도 없어도
   v0 판단과 목표 비중이 나온다.
2. **휴장일에는 아무것도 하지 않는다** (설계 4장). 다만 "돌았는데 휴장이라 건너뛰었다"는 사실은
   `run` 행으로 남긴다 — 기록이 없으면 배치가 죽은 것과 구분되지 않는다.
3. **판단 부분은 한 트랜잭션**이다. 중간에 죽어서 목표 비중 절반만 남으면 그날 기록은
   증빙으로도 채점으로도 못 쓴다. 예외가 나면 판단 행은 하나도 남기지 않고 실행 상태만 error 로 남긴다.
"""
import argparse
import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, time as dtime

import yaml

from . import allocate, combine, ledger
from .calendar import TradingCalendar
from .factors.normalize import clip
from .config import config_hash, config_yaml_text, load_config, resolve_path
from .factors import asof as asof_mod
from .factors.compute import MARKET, compute_all, scores_by_entity, universe_snapshot
from .factors.registry import load_specs
from .risk_flags import compute_risk_flags
from .store import (KST, STATUS_ERROR, STATUS_OK, STATUS_RUNNING, STATUS_SKIPPED, Store)

log = logging.getLogger("advisor.run")

STAGE_PRELIM, STAGE_FINAL = "prelim", "final"
MODE_LIVE, MODE_REPLAY = "live", "replay"
VARIANT_V0, VARIANT_LLM = "v0", "llm"

# 실행 상태(running|ok|error|skipped)는 store.py 가 유일한 출처다 — 기본 인자까지 같은 값이라
# 어느 쪽이 써도 대소문자가 갈리지 않는다. 여기서는 이름만 다시 내보낸다 (배치·테스트가 쓰던 자리).

# YAML 에 아직 없는 조정값 (설정 파일 소유자가 달라 여기 두고 asof.tunable 로 읽는다).
DEFAULTS = {
    # 진행 상황 출력에서 목표 비중을 몇 개까지 보여 줄 것인가. 기록이 아니라 화면 편의값이다.
    "run.log_top_n": 5,
}

# 훅 자리. 전부 `fn(ctx) -> 결과` 꼴의 평범한 함수다 (설계 4장 5·7·9·10번).
#   llm_factors(ctx)   -> [{entity, factor_id, raw_value, score, missing}]  : 결측 LLM 요인 행을 덮어쓴다
#                         **스스로 저장하는 훅은 빈 목록을 주고** `ctx.llm_scores` 만 채우면 된다.
#                         그러면 v0 점수는 코드 요인만으로 남고(결정 4) 그 표는 llm 판단에만 쓰인다.
#   llm_adjust(ctx)    -> {entity: {adj, veto, adopted, rejected, reason, call_id}}  : llm 판단을 만든다
#                         entity 는 composite 의 id (종목=코드, 섹터=섹터 이름).
#                         빈 dict 를 주면 '기권'이라 llm 판단이 조정 없이 기록된다 (설계 5.6).
#   holdings(ctx)      -> {자산 코드…}                                       : 실제 보유를 아는 쪽이 생기면 교체
#   book_trades(ctx)   -> 무엇이든 (기록은 훅이 직접)                         : 체결 예약·확정
#   score_outcomes(ctx)-> 무엇이든                                           : 만기 도래분 채점
# 실제 구현은 advisor/hooks.py 의 default_hooks(cfg) 이고, 훅은 `ctx.llm_used` 로 "오늘 LLM 을
# 실제로 썼는가"를 직접 정할 수 있다 (안 정하면 llm 판단이 있었는가로 본다).
HOOK_NAMES = ("llm_factors", "llm_adjust", "holdings", "book_trades", "score_outcomes")


@dataclass
class RunContext:
    """훅이 받는 그날의 모든 것. 훅은 필요한 것만 꺼내 쓰고, 저장은 run.py 가 맡는다."""

    store: object
    cfg: dict
    cal: object
    specs: dict
    run_id: int
    stage: str
    mode: str
    as_of: datetime                 # 판단 시각 (시점 규칙의 기준)
    as_of_date: str                 # 'YYYY-MM-DD' — run.as_of 열에 들어가는 값
    config_hash: str
    universe: dict = field(default_factory=dict)
    factor_rows: list = field(default_factory=list)
    scores: dict = field(default_factory=dict)          # {entity: {factor_id: (score, missing)}}
    flags: dict = field(default_factory=dict)           # {자산: [위험 표시…]}
    market_score: float = None
    sector_scores: dict = field(default_factory=dict)
    stock_scores: dict = field(default_factory=dict)    # 종합 점수 (계층 점수 + 섹터 기울기)
    holdings: set = field(default_factory=set)
    weights: list = field(default_factory=list)         # v0 목표 비중
    hooks: dict = field(default_factory=dict)
    notes: dict = field(default_factory=dict)
    # --- llm 판단 전용 점수 (결정 4: "기본 점수는 LLM 없이 코드만으로 계산")
    # llm_factors 훅이 `llm_scores` 를 채워 두면 v0 는 코드 요인만으로 계산되고, LLM 이 채운
    # 요인 값은 llm 판단의 base_score 에만 들어간다. 훅이 비워 두면 두 판단이 같은 점수 표를 쓴다.
    llm_scores: dict = field(default_factory=dict)
    llm_market_score: float = None
    llm_sector_scores: dict = field(default_factory=dict)
    llm_stock_scores: dict = field(default_factory=dict)
    llm_used: int = None                                # 훅이 정하면 그 값이 run.llm_used 가 된다


# ------------------------------------------------------------------ 시각·환경

def resolve_as_of(cfg, stage, raw=None):
    """기준 시각을 정한다. 날짜만 주면 단계별 예정 시각(schedule)을 붙인다.

    "2026-06-01" 은 그날 00:00 이 아니라 **그 단계가 실제로 돌았을 시각**이어야 한다.
    00:00 으로 두면 전날 일봉조차 못 읽어(18:00 규칙) 재현이 통째로 결측이 된다.
    """
    if raw is None:
        return datetime.now(KST).replace(tzinfo=None)
    t = asof_mod.to_datetime(raw)
    bare_date = t.time() == dtime(0, 0) and (not isinstance(raw, str) or len(raw.strip()) <= 10)
    if bare_date:
        hh, mm = str((cfg.get("schedule") or {}).get(stage) or "00:00").split(":")[:2]
        t = t.replace(hour=int(hh), minute=int(mm))
    return t


def git_sha():
    """현재 커밋 해시(짧게). git 이 없거나 저장소가 아니어도 배치는 계속돈다."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


def _hooks(hooks):
    return {name: (hooks or {}).get(name) for name in HOOK_NAMES}


# ------------------------------------------------------------------ 단계별 조각

def _ingest(ctx, ingest_fn, no_ingest):
    """수집 (설계 4장의 2번). 실시간 모드에서만 돈다.

    재현 모드는 이미 쌓인 데이터로 과거를 다시 도는 것이라 수집을 건너뛴다 (설계 7.4).
    소스 하나가 실패해도 판단은 나와야 하므로 예외를 삼키고 요약에 적는다 (결정 15의 기권 처리).
    """
    if ctx.mode != MODE_LIVE:
        return {"skipped": "replay"}
    if no_ingest:
        return {"skipped": "no-ingest"}
    try:
        fn = ingest_fn
        if fn is None:
            from .sources.ingest import run_ingest      # 지연 import: 수집 모듈 없이도 배치가 돈다
            fn = run_ingest
        return dict(fn(ctx.store, ctx.cfg, ctx.cal, ctx.as_of, ctx.stage, ctx.mode) or {})
    except Exception as exc:
        log.warning("수집 실패 → 있는 데이터로 계속합니다: %s", exc)
        return {"error": f"{type(exc).__name__}: {exc}"}


def _by_key(rows):
    return {(r["entity"], r["factor_id"]): r for r in rows}


def _factor_step(ctx):
    """코드 요인 → (있으면) LLM 요인 훅 → factor_value 저장 (설계 4장의 3·5번).

    **코드 요인 행을 먼저 저장하고 훅을 부른다.** 훅이 자기 값을 스스로 저장하는 구현(LLM 단계는
    근거·위험 표시까지 한 트랜잭션으로 쓴다)일 때, 나중에 코드 요인 행을 다시 쓰면 방금 채운
    LLM 값이 결측 자리로 되돌아가기 때문이다. 훅이 행을 돌려주면 그 행만 덮어쓴다.

    훅이 `ctx.llm_scores` 를 채우면 v0 점수는 **코드 요인만**으로 남는다 (결정 4). 채우지 않으면
    예전처럼 훅의 값이 v0 점수에도 들어간다 — 훅을 직접 넘기는 쪽(테스트·실험)의 뜻을 바꾸지 않는다.
    """
    rows = compute_all(ctx.store, ctx.cfg, ctx.specs, ctx.cal, ctx.as_of, ctx.stage, ctx.universe)
    ctx.factor_rows = rows
    ctx.store.put_factor_values(ctx.run_id, rows)
    hook = ctx.hooks.get("llm_factors")
    extra = []
    if hook:
        extra = list(hook(ctx) or [])
        if extra:
            # 같은 (entity, factor_id) 는 훅의 값이 이긴다 — v0 가 잡아 둔 결측 자리를 채우는 것이다
            ctx.store.put_factor_values(ctx.run_id, extra)
        ctx.notes.setdefault("llm_factors", len(extra))
    merged = _by_key(rows)
    merged.update(_by_key(extra))
    ctx.factor_rows = list(merged.values())
    ctx.scores = scores_by_entity(rows if ctx.llm_scores else ctx.factor_rows)
    return ctx.factor_rows


def _combine_scores(ctx, scores):
    """한 벌의 하위 점수 → (시장 점수, 섹터 점수, 종목 종합 점수) (설계 5.3)."""
    specs = ctx.specs
    market = combine.market_score(scores.get(MARKET, {}), specs)
    sectors = combine.sector_scores(
        {s: scores.get(s, {}) for s in ctx.universe.get("sectors") or []}, specs)
    layer = {c: combine.layer_score(scores.get(c, {}), specs, layer="stock")
             for c in (ctx.universe.get("stocks") or {})}
    stocks = combine.stock_composites(
        layer, ctx.universe.get("sector_of") or {}, sectors,
        (ctx.cfg.get("combine") or {}).get("sector_tilt"))
    return market, sectors, stocks


def _score_step(ctx):
    """계층 점수와 종목 종합 점수 (설계 5.3, 4장의 6번). 판단 버전마다 한 벌씩 만든다.

    v0 는 코드 요인만 본다 (결정 4: "LLM 없이도 판단이 나온다", 기준선 4 = LLM 없는 시스템).
    llm 판단은 LLM 이 채운 요인까지 넣은 점수를 base 로 쓰고 거기에 조정(adj)을 더한다.
    훅이 llm 전용 점수를 주지 않으면 두 벌은 같은 값이다.
    """
    ctx.market_score, ctx.sector_scores, ctx.stock_scores = _combine_scores(ctx, ctx.scores)
    if ctx.llm_scores:
        ctx.llm_market_score, ctx.llm_sector_scores, ctx.llm_stock_scores = _combine_scores(
            ctx, ctx.llm_scores)
    else:
        ctx.llm_market_score = ctx.market_score
        ctx.llm_sector_scores, ctx.llm_stock_scores = ctx.sector_scores, ctx.stock_scores
    return ctx.stock_scores


def previous_holdings(store, mode, as_of_date, exclude_run_id=None, variant=VARIANT_V0):
    """이력(exit_rank) 판정에 쓸 '현재 보유'. **직전 최종 단계 판단의 종목 비중**으로 본다.

    왜 최종 단계인가: 가상 포트폴리오가 실제로 체결하는 것은 최종 판단이다 (설계 6.3의 sys_final_*).
    예비 배치도 같은 보유를 기준으로 봐야 두 단계의 종목 교체 판단을 나란히 비교할 수 있다.
    왜 같은 mode 인가: 재현 기록과 실시간 기록이 서로의 보유를 물려받으면 둘 다 오염된다 (설계 7.4).
    왜 as_of 이하인가: 재현을 날짜 순서와 다르게 돌려도 미래 판단을 보유로 쓰지 않게 하려는 것이다.

    포트폴리오 담당이 실제 보유(체결 결과)를 갖게 되면 `hooks['holdings']` 로 이 함수를 대신한다.
    """
    row = store.conn.execute(
        "SELECT r.run_id FROM run r WHERE r.stage=? AND r.mode=? AND r.as_of<=? AND r.run_id<>? "
        "AND EXISTS(SELECT 1 FROM target_weight t WHERE t.run_id=r.run_id AND t.variant=?) "
        "ORDER BY r.as_of DESC, r.run_id DESC LIMIT 1",
        (STAGE_FINAL, str(mode), str(as_of_date), int(exclude_run_id or -1), variant)).fetchone()
    if not row:
        return set()
    return {r["asset"] for r in store.conn.execute(
        "SELECT asset FROM target_weight WHERE run_id=? AND variant=? AND role='stock'",
        (row[0], variant)).fetchall()}


def _composite_rows(ctx, market_score, sector_scores, stock_scores, extra=None):
    """composite 테이블 행. 계층 꼬리표를 붙여 시장·섹터·종목을 한 표에 담는다 (설계 8장).

    v0 에서는 조정이 없으므로 base_score = final_score, adj = 0 이다. LLM 조정은 같은 행 모양에
    adj 와 채택·기각 기록을 채워 넣는다 — 그래야 "조정 전후"를 한 행에서 비교할 수 있다 (결정 4).
    """
    extra = extra or {}

    def row(entity, layer, score):
        e = extra.get(entity) or {}
        adj = float(e.get("adj") or 0.0)
        base = None if score is None else float(score)
        return {"entity": entity, "layer": layer, "base_score": base, "adj": adj,
                "final_score": None if base is None else base + adj,
                "vetoed": bool(e.get("vetoed")), "adopted_json": e.get("adopted_json"),
                "rejected_json": e.get("rejected_json"), "reason": e.get("reason"),
                "call_id": e.get("call_id")}

    rows = [row(MARKET, "market", market_score)]
    rows += [row(s, "sector", sector_scores.get(s)) for s in sorted(sector_scores or {})]
    rows += [row(c, "stock", stock_scores.get(c)) for c in sorted(stock_scores or {})]
    return rows


def _write_decision(ctx, variant, market_score, weights, composite_rows):
    """composite → decision(해시 사슬) → target_weight. 호출자가 한 트랜잭션 안에서 부른다.

    위험자산 비중은 비중표에서 되읽는다(1 − 현금 비중). 같은 식을 여기서 다시 쓰면 allocate 와
    조용히 어긋날 수 있고, 판단 기록은 '실제로 낸 비중'과 일치해야 한다.
    """
    store, cfg = ctx.store, ctx.cfg
    weight_of = allocate.as_dict(weights)
    cash_etf = (cfg.get("universe") or {}).get("cash_etf")
    risk_weight = 1.0 - float(weight_of.get(cash_etf, 0.0))

    store.put_composites(ctx.run_id, variant, composite_rows)
    record = ledger.decision_record(ctx.run_id, ctx.stage, ctx.mode, ctx.as_of_date,
                                    ctx.config_hash, variant, market_score, risk_weight, weights)
    prev = ledger.prev_hash_from_store(store, ctx.mode)
    rec_hash = ledger.record_hash(record, prev)
    store.put_decision(ctx.run_id, variant, market_score, risk_weight, rec_hash, prev)
    store.put_target_weights(ctx.run_id, variant, weights)
    return record, prev, rec_hash


def _append_ledger(ctx, record, prev, rec_hash):
    """원장 파일은 실시간 기록만 남긴다 (ledger.py 의 설명). 파일 쓰기 실패가 판단을 무르지는 않는다."""
    if ctx.mode != MODE_LIVE:
        return False
    try:
        ledger.append_ledger(resolve_path(ctx.cfg, "ledger"), record, prev, rec_hash)
        return True
    except Exception as exc:
        log.warning("원장 파일 기록 실패 (DB 의 해시 사슬은 남았습니다): %s", exc)
        ctx.notes["ledger_error"] = f"{type(exc).__name__}: {exc}"
        return False


def _as_json(value):
    """훅이 목록으로 준 채택·기각 기록을 저장 형태(JSON 문자열)로. 이미 문자열이면 그대로."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _llm_variant(ctx):
    """설계 4장의 7번. 훅이 없으면 llm 판단을 만들지 않는다 (그날은 v0 만 기록된다).

    **LLM 의 권한 제한은 훅이 아니라 여기서 지킨다** (결정 4). 훅을 누가 구현하든 다음이 보장된다.
      1. 조정 폭은 설정의 `llm.adj_cap`(±0.2)으로 다시 자른다.
      2. 거부권은 **위험 표시가 붙은 자산에만** 유효하고, 표시 없는 자산의 거부는 무시한다 (설계 5.6).
      3. 점수만 움직이고 비중 규칙은 v0 와 같은 `allocate` 를 그대로 쓴다 — 편입 권한은 없다.
    훅이 빈 dict 를 주면 조정이 없는 llm 판단(기권)이 그날 llm 기본 점수 그대로 기록된다.

    base 는 `ctx.llm_*_score`(LLM 요인까지 넣은 점수)다. v0 와 llm 의 차이는 두 가지 — LLM 이 채운
    **요인 값**(v0 대 llm 비교)과 **조정**(같은 행의 base 대 final 비교)이고, 둘을 한 판단 안에서
    따로 읽을 수 있게 base 에는 조정 전 값을 남긴다 (결정 4의 LLM 기여 평가).
    """
    hook = ctx.hooks.get("llm_adjust")
    if not hook:
        return None
    adjust = dict(hook(ctx) or {})
    cap = abs(float((ctx.cfg.get("llm") or {}).get("adj_cap") or 0.0))
    etf_of = ctx.universe.get("sector_etf") or {}

    extra, vetoed = {}, set()
    for entity, info in adjust.items():
        info = dict(info or {})
        asset = etf_of.get(entity, entity)                      # 섹터는 이름 → 담을 ETF 코드로
        flagged = bool(ctx.flags.get(asset) or ctx.flags.get(entity))
        veto = bool(info.get("veto")) and flagged
        if veto:
            vetoed.add(asset)
        extra[entity] = {
            "adj": clip(float(info.get("adj") or 0.0), -cap, cap),
            "vetoed": veto,
            "adopted_json": _as_json(info.get("adopted_json", info.get("adopted"))),
            "rejected_json": _as_json(info.get("rejected_json", info.get("rejected"))),
            "reason": info.get("reason"),
            "call_id": info.get("call_id"),
        }

    def adjusted(base):
        return {k: (None if v is None else v + (extra.get(k, {}).get("adj") or 0.0))
                for k, v in (base or {}).items()}

    stock_scores = adjusted(ctx.llm_stock_scores)
    sector_scores = adjusted(ctx.llm_sector_scores)
    weights = allocate.target_weights(ctx.llm_market_score, sector_scores, stock_scores, ctx.holdings,
                                      ctx.flags, ctx.cfg, vetoed)
    # composite 에는 **조정 전 점수**를 base 로 남긴다 — final = base + adj 가 성립해야
    # "조정이 무엇을 바꿨는가"를 한 행에서 읽을 수 있다 (결정 4의 LLM 기여 평가)
    rows = _composite_rows(ctx, ctx.llm_market_score, ctx.llm_sector_scores, ctx.llm_stock_scores,
                           extra)
    record, prev, rec_hash = _write_decision(ctx, VARIANT_LLM, ctx.llm_market_score, weights, rows)
    return {"weights": weights, "record": record, "prev": prev, "hash": rec_hash,
            "vetoed": vetoed, "adjusted": len(extra)}


# ------------------------------------------------------------------ 한 번 실행

def run_once(store, cfg, stage, as_of, mode=MODE_LIVE, ingest_fn=None, hooks=None,
             yaml_text=None, no_ingest=False):
    """판단 한 번. run_id 를 돌려준다. API·테스트·CLI 가 모두 이 함수를 부른다.

    as_of 는 datetime(판단 시각)이고 `run.as_of` 열에는 **날짜**가, `decision_time` 열에는
    시각까지가 들어간다. 채점·리포트가 "그날의 판단"을 날짜로 찾고, 시점 규칙은 시각으로 따지기
    때문이다.

    예외가 나면 마지막 커밋 이후의 쓰기를 되돌리고 실행 상태를 error 로 남긴 뒤 다시 던진다.
    다만 9·10번 훅(체결 예약·사후 채점)은 판단이 이미 확정(커밋 + 원장 기록)된 뒤에 돌기 때문에,
    거기서 실패하면 **판단은 남고 실행 상태만 error** 가 된다 — 다 만든 판단을 무르는 것보다
    "체결·채점이 밀렸다"를 드러내는 쪽이 낫다.
    """
    hook_map = _hooks(hooks)
    specs = load_specs(cfg)
    cal = TradingCalendar(cfg, store)
    t = resolve_as_of(cfg, stage, as_of)
    as_of_date = t.date().isoformat()
    chash = config_hash(cfg)
    store.register_config(chash, yaml_text or yaml.safe_dump(cfg, allow_unicode=True, sort_keys=True))

    if not cal.is_trading_day(t.date()):
        # 07:40 에는 "오늘이 거래일인가", 18:30 에는 "오늘 장이 열렸는가" — 같은 판정이다 (설계 4장)
        asked = "오늘이 거래일인가" if stage == STAGE_FINAL else "오늘 장이 열렸는가"
        note = f"휴장일 ({asked}: 아니오) — 판단을 건너뜁니다"
        run_id = store.start_run(stage, as_of_date, mode=mode, decision_time=t.isoformat(),
                                 config_hash=chash, git_sha=git_sha(), note=note,
                                 status=STATUS_SKIPPED)
        store.finish_run(run_id, status=STATUS_SKIPPED)
        store.commit()
        log.info("[%s/%s] %s %s", stage, mode, as_of_date, note)
        return run_id

    run_id = store.start_run(stage, as_of_date, mode=mode, decision_time=t.isoformat(),
                             config_hash=chash, git_sha=git_sha(), status=STATUS_RUNNING)
    store.commit()

    ctx = RunContext(store=store, cfg=cfg, cal=cal, specs=specs, run_id=run_id, stage=stage,
                     mode=mode, as_of=t, as_of_date=as_of_date, config_hash=chash, hooks=hook_map)
    try:
        # 2. 수집 → 요약을 run.note 에 남긴다 (무엇이 비어서 결측이 됐는지 사후에 보이게)
        ctx.notes["ingest"] = _ingest(ctx, ingest_fn, no_ingest)
        # 수집이 오늘 지수 일봉을 새로 넣었을 수 있다. 달력은 저장된 코스피 일자를 캐시하므로
        # 여기서 다시 읽지 않으면 요인의 '최근 N거래일' 창이 하루 어긋난다 (calendar.refresh 의 용도)
        cal.refresh()
        ctx.universe = universe_snapshot(store, cfg, t)

        # 3·5. 요인, 4. 위험 표시 — 여기까지는 판단이 아니라 입력이라 중간에 커밋해도 된다
        _factor_step(ctx)
        flag_rows, ctx.flags = compute_risk_flags(store, cfg, cal, t, ctx.universe)
        store.put_risk_flags(run_id, flag_rows)
        store.commit()

        # 6. 계층·종합 점수 → v0 비중
        _score_step(ctx)
        hold_hook = ctx.hooks.get("holdings")
        ctx.holdings = set(hold_hook(ctx) or ()) if hold_hook else previous_holdings(
            store, mode, as_of_date, exclude_run_id=run_id)
        ctx.weights = allocate.target_weights(ctx.market_score, ctx.sector_scores, ctx.stock_scores,
                                              ctx.holdings, ctx.flags, cfg)

        # 8. 판단 저장 + 해시 사슬. 여기부터 커밋 전까지가 '한 판단'이다
        v0_rows = _composite_rows(ctx, ctx.market_score, ctx.sector_scores, ctx.stock_scores)
        record, prev, rec_hash = _write_decision(ctx, VARIANT_V0, ctx.market_score, ctx.weights, v0_rows)
        llm = _llm_variant(ctx)
        store.commit()

        # 원장 파일은 커밋 뒤에 덧붙인다 — DB 에 없는 판단이 원장에 먼저 생기지 않게
        _append_ledger(ctx, record, prev, rec_hash)
        if llm:
            _append_ledger(ctx, llm["record"], llm["prev"], llm["hash"])

        # 9·10. 체결 예약·확정과 사후 채점은 판단이 확정된 뒤에 돈다
        for name in ("book_trades", "score_outcomes"):
            hook = ctx.hooks.get(name)
            if hook:
                ctx.notes[name] = hook(ctx)

        note = json.dumps({k: v for k, v in ctx.notes.items() if v is not None},
                          ensure_ascii=False, default=str)
        # llm_used 는 "오늘 LLM 을 실제로 썼는가"다. 훅이 기권·건너뜀을 알면 그쪽이 이긴다
        # (조정 없는 llm 판단이 기록된 것과 LLM 이 실제로 움직인 것은 다르다 — 결정 15).
        llm_used = ctx.llm_used if ctx.llm_used is not None else (1 if llm else 0)
        store.finish_run(run_id, status=STATUS_OK, llm_used=int(llm_used), note=note)
        store.commit()
        log.info("[%s/%s] %s 판단 완료 run_id=%s 시장점수=%s 비중 %s",
                 stage, mode, as_of_date, run_id,
                 "없음" if ctx.market_score is None else f"{ctx.market_score:+.3f}",
                 _weights_line(cfg, ctx.weights))
        return run_id
    except Exception as exc:
        # 판단 행을 절반만 남기지 않는다. 마지막 커밋 이후의 쓰기를 통째로 되돌린다
        store.conn.rollback()
        store.finish_run(run_id, status=STATUS_ERROR, note=f"{type(exc).__name__}: {exc}")
        store.commit()
        log.exception("[%s/%s] %s 판단 실패 run_id=%s", stage, mode, as_of_date, run_id)
        raise


def _weights_line(cfg, weights):
    """진행 출력용 한 줄. 상위 몇 개만 보여 준다 (기록이 아니라 눈으로 보는 값)."""
    top_n = int(asof_mod.tunable(cfg, "run.log_top_n", DEFAULTS))
    parts = [f"{w['asset']}({w['role']}) {w['weight']:.1%}"
             for w in sorted(weights, key=lambda w: -w["weight"])[:top_n]]
    return " ".join(parts)


def replay_range(cfg, store, start, end, stage=STAGE_FINAL, hooks=None, out=None, stages=None):
    """[start, end] 의 모든 거래일을 재현 모드로 돈다 (설계 7.4). 실행마다 한 줄 출력.

    하루가 실패해도 나머지 날짜는 계속 돈다 — 재현의 목적이 "절차가 도는지 확인"이라
    중간에 멈추면 어디까지 되는지를 알 수 없다.

    `stages` 를 주면 **하루 안에서 그 순서대로** 여러 단계를 돈다. 기본은 한 단계(최종)뿐인데,
    예비 판단으로 다음 날 시가에 체결하는 기준선 5번(`sys_prelim_llm`)은 예비 실행이 있어야
    생기므로 그 포트폴리오까지 재현하려면 `["final", "prelim"]` 처럼 실제 하루 순서로 준다
    (최종 07:40 → 예비 18:30).
    """
    out = out or sys.stdout
    cal = TradingCalendar(cfg, store)
    days = cal.trading_days_between(start, end)
    stages = list(stages) if stages else [stage]
    ok = fail = 0
    for day in days:
        for stg in stages:
            label = f"{day}" if len(stages) == 1 else f"{day} {stg}"
            try:
                run_id = run_once(store, cfg, stg, day, mode=MODE_REPLAY, hooks=hooks,
                                  no_ingest=True)
                row = store.conn.execute("SELECT status FROM run WHERE run_id=?",
                                         (run_id,)).fetchone()
                dec = store.conn.execute(
                    "SELECT market_score, risk_weight FROM decision WHERE run_id=? AND variant=?",
                    (run_id, VARIANT_V0)).fetchone()
                detail = ("시장점수 %+.3f 위험자산 %.0f%%"
                          % (dec["market_score"] or 0.0, 100 * (dec["risk_weight"] or 0.0))
                          if dec else "판단 없음")
                print(f"{label} run_id={run_id} {row['status']} {detail}", file=out)
                ok += 1
            except Exception as exc:
                print(f"{label} 실패: {type(exc).__name__}: {exc}", file=out)
                fail += 1
    print(f"재현 완료: 거래일 {len(days)}일 중 성공 {ok}, 실패 {fail}", file=out)
    return ok, fail


# ------------------------------------------------------------------ 대체 규칙 (결정 11)

def promote_prelim(store, cfg, as_of=None, mode=MODE_LIVE, hooks=None, yaml_text=None, out=None):
    """최종 배치가 제 시각에 끝나지 않았을 때 전날 예비 판단을 승격한다 (설계 4장, 결정 11).

    스케줄러가 08:50(`schedule.fallback_deadline`)에 부른다. 하는 일은 네 가지뿐이다.

      1. 오늘이 거래일이고 오늘자 최종 실행이 **상태 ok 로** 없는지 본다. 있으면 아무것도 안 한다.
      2. 직전 거래일의 **마지막 예비 실행**의 판단(두 버전 모두)을 그대로 베껴
         `fallback_used=1` 인 오늘자 최종 실행 행으로 남긴다. 점수를 다시 계산하지 않는 이유는,
         승격은 "어제 저녁의 판단을 오늘 시가에 그대로 낸다"이지 새 판단이 아니기 때문이다.
      3. 해시 사슬을 잇고 원장에 덧붙인다 — 승격도 그날 실제로 낸 판단이라 증빙에 들어가야 한다.
      4. 체결 예약 훅을 돌린다 (그 판단으로 오늘 시가에 체결한다).

    돌려주는 것은 (종료 코드, 요약 dict). 종료 코드 0 은 '할 일이 없었다'와 '승격했다' 둘 다다.
    """
    out = out or sys.stdout
    cal = TradingCalendar(cfg, store)
    t = resolve_as_of(cfg, STAGE_FINAL, as_of)
    day = t.date()
    if not cal.is_trading_day(day):
        print(f"{day} 휴장일 — 승격할 것이 없습니다", file=out)
        return 0, {"promoted": False, "reason": "holiday"}

    done = store.conn.execute(
        "SELECT run_id FROM run WHERE stage=? AND mode=? AND as_of=? AND status=? "
        "ORDER BY run_id DESC LIMIT 1", (STAGE_FINAL, mode, str(day), STATUS_OK)).fetchone()
    if done:
        print(f"{day} 최종 판단이 이미 있습니다 (run_id={done[0]}) — 승격하지 않습니다", file=out)
        return 0, {"promoted": False, "reason": "final_ok", "run_id": int(done[0])}

    prev_day = cal.prev_trading_day(day)
    src = store.conn.execute(
        "SELECT * FROM run WHERE stage=? AND mode=? AND as_of=? "
        "AND EXISTS(SELECT 1 FROM target_weight t WHERE t.run_id=run.run_id) "
        "ORDER BY run_id DESC LIMIT 1", (STAGE_PRELIM, mode, str(prev_day))).fetchone()
    if src is None:
        print(f"{prev_day} 예비 판단이 없어 승격할 수 없습니다", file=out)
        return 1, {"promoted": False, "reason": "no_prelim", "prev_day": str(prev_day)}

    chash = config_hash(cfg)
    store.register_config(chash, yaml_text or yaml.safe_dump(cfg, allow_unicode=True, sort_keys=True))
    note = f"대체 규칙(결정 11): {prev_day} 예비 판단(run_id={src['run_id']})을 승격"
    run_id = store.start_run(STAGE_FINAL, str(day), mode=mode, decision_time=t.isoformat(),
                             config_hash=chash, git_sha=git_sha(), note=note, status=STATUS_RUNNING)
    store.commit()

    ctx = RunContext(store=store, cfg=cfg, cal=cal, specs=load_specs(cfg), run_id=run_id,
                     stage=STAGE_FINAL, mode=mode, as_of=t, as_of_date=str(day), config_hash=chash,
                     hooks=_hooks(hooks))
    try:
        variants = _copy_decisions(ctx, int(src["run_id"]))
        store.finish_run(run_id, status=STATUS_OK, fallback_used=1, note=note)
        store.commit()
        for record, prev, rec_hash in variants:
            _append_ledger(ctx, record, prev, rec_hash)
        hook = ctx.hooks.get("book_trades")
        if hook:
            hook(ctx)
        print(f"{day} 승격 완료 run_id={run_id} (원본 {prev_day} run_id={src['run_id']}, "
              f"버전 {','.join(v[0]['variant'] for v in variants)})", file=out)
        return 0, {"promoted": True, "run_id": run_id, "src_run_id": int(src["run_id"]),
                   "variants": [v[0]["variant"] for v in variants], "as_of": str(day)}
    except Exception as exc:
        store.conn.rollback()
        store.finish_run(run_id, status=STATUS_ERROR, fallback_used=1,
                         note=f"{note} / 실패: {type(exc).__name__}: {exc}")
        store.commit()
        log.exception("승격 실패 run_id=%s", run_id)
        print(f"{day} 승격 실패: {type(exc).__name__}: {exc}", file=out)
        return 1, {"promoted": False, "reason": "error", "error": f"{type(exc).__name__}: {exc}"}


def _copy_decisions(ctx, src_run_id):
    """원본 실행의 composite·decision·target_weight 를 이 실행으로 베낀다 (버전별로 한 벌씩).

    해시 사슬은 **새로 잇는다** — 같은 내용이라도 오늘 낸 판단은 오늘 자리의 한 줄이다.
    """
    store = ctx.store
    out = []
    variants = [r[0] for r in store.conn.execute(
        "SELECT DISTINCT variant FROM target_weight WHERE run_id=? ORDER BY variant",
        (src_run_id,)).fetchall()]
    for variant in variants:
        comp = [dict(r) for r in store.conn.execute(
            "SELECT entity, layer, base_score, adj, final_score, vetoed, adopted_json, "
            "rejected_json, reason, call_id FROM composite WHERE run_id=? AND variant=?",
            (src_run_id, variant)).fetchall()]
        weights = [{"asset": r["asset"], "role": r["role"], "weight": float(r["weight"])}
                   for r in store.conn.execute(
                       "SELECT asset, role, weight FROM target_weight WHERE run_id=? AND variant=? "
                       "ORDER BY asset", (src_run_id, variant)).fetchall()]
        dec = store.conn.execute(
            "SELECT market_score FROM decision WHERE run_id=? AND variant=?",
            (src_run_id, variant)).fetchone()
        market_score = dec["market_score"] if dec else None
        out.append(_write_decision(ctx, variant, market_score, weights, comp))
    store.commit()
    return out


# ------------------------------------------------------------------ CLI

def build_parser():
    p = argparse.ArgumentParser(prog="python -m backend.advisor.run",
                                description="판단 지원(advisor) 배치")
    p.add_argument("--stage", choices=[STAGE_PRELIM, STAGE_FINAL],
                   help="예비(18:30) 또는 최종(07:40). --replay-range 에서는 기본 final")
    p.add_argument("--as-of", dest="as_of",
                   help='기준 시각. "2026-06-01" 이면 단계별 예정 시각, "2026-06-01T07:40" 이면 그 시각')
    p.add_argument("--mode", choices=[MODE_LIVE, MODE_REPLAY], default=MODE_LIVE)
    p.add_argument("--db", help="advisor.db 경로 (기본: 설정의 paths.db)")
    p.add_argument("--config", help="설정 파일 경로 (기본: backend/advisor.config.yaml)")
    p.add_argument("--no-ingest", action="store_true", help="수집을 건너뛰고 쌓인 데이터로만 판단")
    p.add_argument("--replay-range", dest="replay_range", nargs=2, metavar=("START", "END"),
                   help="재현 모드로 구간의 모든 거래일에 최종 단계를 실행")
    p.add_argument("--replay-stages", dest="replay_stages", nargs="+",
                   choices=[STAGE_PRELIM, STAGE_FINAL], metavar="STAGE",
                   help="재현에서 하루에 돌릴 단계와 순서 (예: final prelim). 기본은 --stage 하나")
    p.add_argument("--promote-prelim", dest="promote_prelim", action="store_true",
                   help="오늘 최종 판단이 없으면 전날 예비 판단을 승격한다 (08:50 대체 규칙, 결정 11)")
    p.add_argument("--no-llm", dest="no_llm", action="store_true",
                   help="LLM 단계를 빼고 돈다 (v0 판단만)")
    p.add_argument("--no-portfolio", dest="no_portfolio", action="store_true",
                   help="체결 예약·정산·채점을 빼고 돈다 (판단만)")
    return p


def main(argv=None):
    """종료 코드를 돌려준다. 0=성공·휴장, 1=실패 (스케줄러가 이 값을 본다)."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = load_config(args.config)
        yaml_text = config_yaml_text(args.config)
    except Exception as exc:
        print(f"설정을 읽지 못했습니다: {exc}", file=sys.stderr)
        return 1

    db_path = args.db or resolve_path(cfg, "db")
    stage = args.stage or (STAGE_FINAL if args.replay_range or args.promote_prelim else None)
    if not stage:
        print("--stage 가 필요합니다 (prelim 또는 final)", file=sys.stderr)
        return 1

    # 실제 배치의 훅 묶음. 깃발로 끈 자리는 아예 넘기지 않는다 — 훅이 없다는 것이 곧
    # "그 단계를 하지 않는다"이고, run_once 는 그 상태에서도 v0 판단을 끝까지 낸다 (결정 4).
    from .hooks import default_hooks                      # 지연 import: LLM·포트폴리오 없이도 임포트된다
    hooks = default_hooks(cfg, with_llm=not args.no_llm, with_portfolio=not args.no_portfolio)

    with Store(db_path) as store:
        try:
            if args.promote_prelim:
                code, _ = promote_prelim(store, cfg, args.as_of, mode=args.mode, hooks=hooks,
                                         yaml_text=yaml_text)
                return code
            if args.replay_range:
                _, fail = replay_range(cfg, store, args.replay_range[0], args.replay_range[1],
                                       stage=stage, hooks=hooks, stages=args.replay_stages)
                return 1 if fail else 0
            run_once(store, cfg, stage, args.as_of, mode=args.mode, hooks=hooks,
                     yaml_text=yaml_text, no_ingest=args.no_ingest)
            return 0
        except Exception as exc:
            print(f"배치 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        finally:
            close = (hooks or {}).get("close")
            if close:
                close()


if __name__ == "__main__":
    sys.exit(main())
