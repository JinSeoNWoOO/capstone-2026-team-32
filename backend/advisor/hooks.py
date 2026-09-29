"""배치가 실제로 쓰는 훅 묶음 (설계 4장의 5·7·9·10번을 `run.py` 에 꽂는 자리).

`run.py` 는 훅 자리를 네 개 비워 두고 v0 판단까지만 책임진다. 이 파일은 그 자리에 LLM 단계
(`llm/stage.py`)와 포트폴리오·채점(`portfolio.py` · `baselines.py` · `scoring.py`)을 끼워
"하루치 전부"를 만든다. 배치 CLI 는 `default_hooks(cfg)` 하나만 부른다.

## 결정 넷 (이 파일의 모양을 정한다)

1. **v0 는 LLM 이 채운 요인을 보지 않는다** (결정 4: "기본 점수는 LLM 없이 코드만으로 계산",
   "LLM 없이도 판단이 나온다", 결정 14의 기준선 4 = LLM 없는 시스템).
   설정에서 `llm: true` 인 요인(v0 에서는 `stk_disclosure`·`news_risk`)은 **llm 판단에서만**
   점수에 들어가고, v0 composite 에서는 결측 자리로 남는다. 그래서 같은 날의 두 판단은
   ① LLM 이 채운 요인 값(v0 대 llm)과 ② 조정 adj(같은 행의 base 대 final) 두 가지로 갈린다.
   `stk_disclosure` 는 코드가 기준표로 확정하는 건(유상증자·전환사채·자기주식…)도 섞여 있지만
   **요인 하나를 통째로 llm 전용**으로 본다. 한 요인의 점수가 판단 버전마다 반쯤 다르면
   사후 채점(요인 점수는 버전과 무관하게 한 벌만 남긴다, scoring.py)이 성립하지 않기 때문이다.
   구현은 `ctx.llm_scores` 한 줄이다 — 훅이 그것을 채우면 run.py 가 판단 버전마다 점수를
   따로 합친다.
2. **저장은 한 번만.** `llm/stage.py` 는 점수·근거·위험 표시를 자기 트랜잭션에서
   INSERT OR REPLACE 로 쓴다. 그래서 이 훅은 행을 **돌려주지 않고**(빈 목록) 저장된 값을 도로
   읽어 in-memory 점수만 만든다. 돌려주면 run.py 가 같은 값을 한 번 더 쓴다.
3. **LLM 클라이언트는 실행 하나에 한 벌.** 요인 단계와 조정 단계가 같은 클라이언트를 쓰고
   (호출 수·비용·하루 예산이 한 벌로 집계돼야 한다) 실행이 끝나면 닫는다. 실패해도 닫는다.
4. **체결·채점의 호출 순서는 포트폴리오 담당이 정한 그대로.**
   - 최종(07:40): `start_day` → `book_system_portfolios` → `book_baselines`
   - 예비(18:30): `settle(오늘)` → 채점(`score_outcomes` → `factor_metrics` → `risk_flag_metrics`)
   - **재현 모드**에는 예비 배치가 없다. 그래서 최종 단계가 예약 **전에** 전 거래일까지 정산하고
     채점도 함께 돈다 — `--replay-range` 한 번으로 NAV·사후 결과·요인 지표가 다 나와야 한다.
     정산은 이미 NAV 가 있는 날을 건너뛰므로 몇 번 불러도 결과가 같다 (portfolio.settle_portfolio).

## 기권 (결정 15)

LLM 이 꺼져 있거나, 예산을 넘겼거나, 실패했거나, 재현 모드면 **조정 없음**이다. 그날 llm 판단은
v0 와 같은 내용으로 기록되고 그 사실(status)이 `run.note` 에 남는다 — "움직일 이유가 없었다"와
"물어보지 못했다"를 섞으면 결정 4의 비교가 오염된다. `run.llm_used` 도 그때 0 이 된다.
"""
import logging

from . import baselines, portfolio, scoring
from .factors.compute import scores_by_entity
from .llm import stage as llm_stage
from .llm.adjust import market_context
from .llm.client import STATUS_OK as LLM_STATUS_OK, LLMClient
from .llm.stage import LLM_FACTORS
from .run import MODE_REPLAY, STAGE_PRELIM, STATUS_OK, previous_holdings

log = logging.getLogger("advisor.hooks")

# 조정을 받아들일 상태. 나머지(ERROR·SKIPPED_BUDGET·DISABLED·SKIPPED_REPLAY·NO_ASSETS)는
# 전부 기권이라 조정 없이 간다 (결정 15). 상태는 run.note 에 그대로 남는다.
ADJUST_OK_STATUSES = ("OK", "PARTIAL")

# 이력(보유)을 읽어 올 포트폴리오. LLM 조정이 들어간 주 포트폴리오가 아니라 v0 를 쓰는 이유는,
# 보유 여부가 두 판단의 **공통 입력**이어야 하기 때문이다 (결정 4: 한쪽의 보유가 다른 쪽의
# 후보 순위를 바꾸면 v0 와 llm 의 차이가 조정 때문인지 보유 때문인지 갈라낼 수 없다).
HOLDINGS_PORTFOLIO = "sys_final_v0"

# run.note 에 남길 예약 요약의 길이 상한. 원장·리포트가 읽는 칸이라 한 줄로 읽히는 편이 낫다.
_MAX_NOTES = 10


def _configured_etfs(cfg):
    """설정이 아는 ETF 코드 모음. 보유 비중에서 '종목'만 골라내는 데 쓴다."""
    uni = cfg.get("universe") or {}
    out = {uni.get("core_etf"), uni.get("cash_etf")}
    out |= set((uni.get("sector_etfs") or {}).values())
    out |= set((uni.get("other_etfs") or {}).values())
    return {c for c in out if c}


def _llm_factor_ids(ctx):
    """설정에서 `llm: true` 인 요인 id. 설정이 비어 있으면 LLM 단계가 채우는 두 개로 물러선다."""
    ids = tuple(fid for fid, spec in (ctx.specs or {}).items() if getattr(spec, "llm", False))
    return ids or LLM_FACTORS


def _brief_llm(result, before):
    """LLM 단계 결과에서 run.note 에 남길 만큼만. 호출 수·비용은 **이 단계 몫**만 센다.

    클라이언트를 두 단계가 나눠 쓰므로 누적 통계에서 단계 시작 시점의 값을 뺀다.
    """
    out = {"status": result.get("status"), "detail": result.get("detail")}
    if before is None:
        out["n_calls"] = result.get("n_calls")
        out["cost"] = result.get("cost")
        out["n_cache"] = result.get("n_cache")
    else:
        out["n_calls"] = (result.get("n_calls") or 0) - before[0]
        out["cost"] = round((result.get("cost") or 0.0) - before[1], 6)
        out["n_cache"] = (result.get("n_cache") or 0) - before[2]
    if result.get("cost_unknown"):
        out["cost_unknown"] = result["cost_unknown"]
    return {k: v for k, v in out.items() if v is not None}


def _brief_booking(rows):
    """예약 결과 → run.note 용 요약. 자산별 변화량까지 넣으면 한 줄이 수십 줄이 된다."""
    out = []
    for r in rows or ():
        item = {"id": r.get("portfolio_id"), "booked": bool(r.get("booked")),
                "n": len(r.get("deltas") or {})}
        for key in ("variant", "promoted", "llm_abstained", "note"):
            if r.get(key):
                item[key] = r[key]
        out.append(item)
    return out


class DefaultHooks:
    """훅 다섯 개와 그들이 공유하는 상태(LLM 클라이언트 한 벌)를 담는다.

    `client` 를 주면 그것을 SDK 자리에 끼운다 — 테스트가 가짜 모델을 넣는 통로다
    (`LLMClient(cfg, store, run_id, client=…)` 와 같은 규약).
    """

    def __init__(self, cfg, with_llm=True, with_portfolio=True, client=None):
        self.cfg = cfg
        self.with_llm = bool(with_llm)
        self.with_portfolio = bool(with_portfolio)
        self._sdk = client
        self._llm = None
        self._llm_run_id = None

    # ---------------------------------------------------------------- LLM 클라이언트

    def client(self, ctx):
        """이 실행의 LLM 클라이언트. 실행이 바뀌면 앞의 것을 닫고 새로 만든다.

        `llm_call.run_id` 가 클라이언트에 박히므로 실행마다 새로 만들어야 하고, 한 실행 안에서는
        요인 단계와 조정 단계가 같은 것을 써야 하루 예산·호출 수가 한 벌로 집계된다.
        """
        key = (id(ctx.store), ctx.run_id)
        if self._llm is None or self._llm_run_id != key:
            self.close()
            self._llm = LLMClient(self.cfg, ctx.store, ctx.run_id, client=self._sdk)
            self._llm_run_id = key
        return self._llm

    def close(self):
        """소유한 클라이언트를 닫는다. 실패해도 배치를 막지 않는다."""
        llm, self._llm, self._llm_run_id = self._llm, None, None
        if llm is None:
            return
        try:
            llm.close()
        except Exception as exc:                       # 닫기 실패가 판단을 무르지는 않는다
            log.warning("LLM 클라이언트 닫기 실패: %s", exc)

    def _client_for(self, ctx):
        """재현 모드에서는 만들지 않는다 (LLM 단계가 부르기 전에 돌아선다, 설계 7.4)."""
        return None if ctx.mode == MODE_REPLAY else self.client(ctx)

    def _stats(self, ctx):
        """지금까지 이 실행에서 쌓인 (API 호출 수, 비용, 캐시 적중). 단계별 몫을 빼기 위한 기준점."""
        if self._llm is None or self._llm_run_id != (id(ctx.store), ctx.run_id):
            return None
        s = self._llm.stats
        return (s.n_api, s.cost, s.n_cache)

    def _mark_used(self, ctx):
        """run.llm_used = 이 실행에 **성공한 호출이 하나라도 있었는가**.

        상태 문자열로 정하지 않는 이유는, 공시가 전부 코드로 확정된 날(호출 0회)이나 전부 실패한
        날(상태는 PARTIAL)에도 status 만 보면 '썼다'가 되기 때문이다. 기록(llm_call)이 진실이다.
        """
        try:
            row = ctx.store.conn.execute(
                "SELECT 1 FROM llm_call WHERE run_id=? AND status=? LIMIT 1",
                (ctx.run_id, LLM_STATUS_OK)).fetchone()
        except Exception:                                  # 기록을 못 읽어도 판단은 계속된다
            return
        ctx.llm_used = 1 if row else 0

    # ---------------------------------------------------------------- 5. LLM 요인

    def llm_factors(self, ctx):
        """설계 4장의 5번. 저장은 `llm/stage.py` 가 하고, 여기서는 **llm 판단용 점수**만 만든다.

        돌려주는 값이 빈 목록인 것이 중요하다 — 같은 행을 run.py 가 한 번 더 쓰지 않게 한다.
        `ctx.llm_scores` 를 채우는 순간 v0 는 코드 요인만으로 계산된다 (결정 4).
        """
        before = self._stats(ctx)
        result = llm_stage.llm_factors(ctx.store, ctx.cfg, ctx.cal, ctx.run_id, ctx.as_of,
                                       ctx.stage, llm=self._client_for(ctx))
        ctx.notes["llm_factors"] = _brief_llm(result, before)
        self._mark_used(ctx)

        merged = {(r["entity"], r["factor_id"]): r for r in ctx.factor_rows}
        merged.update({(r["entity"], r["factor_id"]): r for r in self._stored_llm_rows(ctx)})
        ctx.llm_scores = scores_by_entity(list(merged.values()))
        return []

    def _stored_llm_rows(self, ctx):
        """LLM 단계가 방금 저장한 요인 행을 도로 읽는다 (채우지 못한 자리는 결측 그대로)."""
        ids = _llm_factor_ids(ctx)
        rows = ctx.store.conn.execute(
            "SELECT entity, factor_id, raw_value, score, missing FROM factor_value "
            "WHERE run_id=? AND factor_id IN (%s)" % ",".join("?" * len(ids)),
            (ctx.run_id,) + tuple(ids)).fetchall()
        return [{"entity": r["entity"], "factor_id": r["factor_id"], "raw_value": r["raw_value"],
                 "score": r["score"], "missing": r["missing"]} for r in rows]

    # ---------------------------------------------------------------- 7. LLM 조정

    def llm_adjust(self, ctx):
        """설계 4장의 7번. {entity: {adj, veto, adopted, rejected, reason, call_id}} 를 돌려준다.

        넘기는 점수는 **llm 판단의 기본 점수**(LLM 요인까지 반영된 값)다. 모델이 보는 점수와
        조정이 얹히는 점수가 다르면 "무엇을 보고 얼마를 움직였는가"가 기록과 어긋난다.
        기권 상태면 빈 dict 를 주고 그 사실을 `ctx.notes` 에 남긴다 (결정 15).

        **키를 바꿔 준다.** `llm/adjust.py` 는 {자산 코드: Adjustment} 를 주는데 (모델에게는 담을
        자산을 코드로 보여 준다), `run.py` 의 훅 계약은 **composite 의 entity**다 — 섹터 ETF 의
        점수·근거는 섹터 **이름**으로 저장돼 있기 때문이다. 그대로 넘기면 섹터 조정이 어느 행에도
        얹히지 않고 조용히 사라진다.
        """
        before = self._stats(ctx)
        names = {code: (meta or {}).get("name") or ""
                 for code, meta in (ctx.universe.get("stocks") or {}).items()}
        result = llm_stage.llm_adjust(
            ctx.store, ctx.cfg, ctx.run_id, ctx.llm_stock_scores,
            sector_scores=ctx.llm_sector_scores,
            market_ctx=market_context(ctx.store, ctx.run_id, ctx.llm_market_score),
            flags=ctx.flags, holdings=ctx.holdings, names=names, llm=self._client_for(ctx))
        status = result.get("status")
        ctx.notes["llm_adjust"] = _brief_llm(result, before)
        self._mark_used(ctx)
        if status not in ADJUST_OK_STATUSES:
            return {}                                  # 기권: 조정 없는 llm 판단이 그대로 기록된다
        sector_of_etf = {etf: sector
                         for sector, etf in (ctx.universe.get("sector_etf") or {}).items()}
        return {sector_of_etf.get(code, code):
                {"adj": a.adj, "veto": a.vetoed, "adopted": list(a.adopted or ()),
                 "rejected": list(a.rejected or ()), "reason": a.reason, "call_id": a.call_id}
                for code, a in (result.get("adjustments") or {}).items()}

    # ---------------------------------------------------------------- 보유

    def holdings(self, ctx):
        """이력(exit_rank) 판정에 쓸 현재 보유 종목. **정산된 실제 보유**가 있으면 그쪽이다.

        마지막 정산일의 `holding` 행에서 설정이 아는 ETF(현금 대용·핵심·섹터)를 뺀 것이
        '지금 들고 있는 종목'이다. 아직 한 번도 정산하지 않았으면(첫 실행) run.py 의 기본값인
        직전 최종 판단의 종목 비중으로 물러선다.
        """
        pid = portfolio.portfolio_id_for(HOLDINGS_PORTFOLIO, ctx.mode)
        weights = portfolio.current_weights(ctx.store, pid)
        if weights:
            etfs = _configured_etfs(ctx.cfg)
            return {a for a, w in weights.items() if w > 0 and a not in etfs}
        return previous_holdings(ctx.store, ctx.mode, ctx.as_of_date, exclude_run_id=ctx.run_id)

    # ---------------------------------------------------------------- 9. 체결 예약·정산

    def book_trades(self, ctx):
        """설계 4장의 9번. 최종은 예약, 예비는 정산 (설계 6.2의 예약 → 확정 두 단계)."""
        out = {"stage": ctx.stage, "mode": ctx.mode}
        if ctx.stage == STAGE_PRELIM:
            out["settle"] = self._settle(ctx)
            return out
        if ctx.mode == MODE_REPLAY:
            # 재현에는 예비 배치가 없다 → 예약 전에 전 거래일까지 정산해 둔다 (그래야 현재 비중이
            # 최신이고, `--replay-range` 한 번으로 NAV 가 날짜마다 이어진다)
            out["settle"] = self._settle(ctx)
        fill = portfolio.start_day(ctx.cal, ctx.stage, ctx.as_of)
        out["fill_date"] = str(fill)
        out["system"] = _brief_booking(
            portfolio.book_system_portfolios(ctx.store, ctx.cfg, ctx.cal, fill, mode=ctx.mode))
        out["baselines"] = _brief_booking(
            baselines.book_baselines(ctx.store, ctx.cfg, ctx.cal, fill, ctx.as_of, mode=ctx.mode))
        return out

    def _settle(self, ctx):
        """판단 시각에 **일봉이 확정된 마지막 거래일**까지 정산한다 (설계 2.3).

        예비 18:30 이면 오늘까지, 최종 07:40 이면 전 거래일까지다. 경계는 요인 계산과 같은
        `sources.daily_bar_known_at` 에서 나온다 — 두 벌이 되면 하루가 어긋난다.
        되돌리지 않으므로(이미 NAV 가 있는 날은 건너뛴다) 같은 날 몇 번 불려도 결과가 같다.
        """
        upto = portfolio.known_bar_date(ctx.cal, ctx.as_of, ctx.cfg)
        reports = portfolio.settle(ctx.store, ctx.cfg, ctx.cal, upto, mode=ctx.mode)
        notes = [n for r in reports for n in r["notes"]]
        return {"upto": str(upto), "portfolios": len(reports),
                "days": sum(len(r["days"]) for r in reports),
                "notes": notes[:_MAX_NOTES] or None}

    # ---------------------------------------------------------------- 10. 사후 채점

    def score_outcomes(self, ctx):
        """설계 4장의 10번. 만기 도래분 채점 → 요인 지표 → 위험 표시 지표.

        **정산 다음에** 돈다. 채점은 정산된 NAV 가 아니라 일봉을 직접 보지만, 그날 일봉을
        읽을 수 있는 시점이 곧 정산할 수 있는 시점이라 예비 배치가 둘을 함께 하는 것이 자연스럽다.
        최종 배치는 전날 예비가 이미 채점했으면 건너뛴다 — 지표 표는 덧붙이기라 하루 두 번 쌓으면
        같은 날의 묶음이 두 벌이 된다.
        """
        if not self._should_score(ctx):
            return {"skipped": "직전 예비 배치가 이미 채점했습니다"}
        counts = scoring.score_outcomes(ctx.store, ctx.cfg, ctx.cal, ctx.as_of, mode=ctx.mode)
        metrics = scoring.factor_metrics(ctx.store, ctx.cfg, mode=ctx.mode)
        flags = scoring.risk_flag_metrics(ctx.store, ctx.cfg, ctx.cal, ctx.as_of, mode=ctx.mode)
        return {"outcome": counts, "factor_metric": len(metrics), "risk_flag_metric": len(flags)}

    def _should_score(self, ctx):
        """예비 단계는 언제나. 최종 단계는 직전 거래일의 예비 배치가 없거나 실패했을 때만.

        재현 모드에는 예비 실행이 아예 없으므로 최종이 매일 채점하게 된다 — 같은 규칙 하나로
        "예비가 채점한다"와 "재현에서는 최종이 대신한다"가 둘 다 나온다.
        """
        if ctx.stage == STAGE_PRELIM:
            return True
        prev = ctx.cal.prev_trading_day(ctx.as_of_date)
        row = ctx.store.conn.execute(
            "SELECT 1 FROM run WHERE stage=? AND mode=? AND as_of=? AND status=? LIMIT 1",
            (STAGE_PRELIM, ctx.mode, str(prev), STATUS_OK)).fetchone()
        return row is None

    # ---------------------------------------------------------------- 묶기

    def as_dict(self):
        """`run_once(hooks=…)` 에 넘길 dict. 꺼 둔 자리는 **키 자체를 넣지 않는다**."""
        hooks = {"close": self.close}
        if self.with_llm:
            hooks["llm_factors"] = self.llm_factors
            hooks["llm_adjust"] = self.llm_adjust
        if self.with_portfolio:
            hooks["holdings"] = self.holdings
            hooks["book_trades"] = self.book_trades
            hooks["score_outcomes"] = self.score_outcomes
        return hooks


def default_hooks(cfg, with_llm=True, with_portfolio=True, client=None):
    """배치가 쓰는 훅 묶음 (설계 4장의 5·7·9·10번). `run_once(..., hooks=default_hooks(cfg))`.

    돌려주는 dict 에는 훅 말고 `close` 가 하나 더 들어 있다 — LLM 클라이언트를 닫는 자리이고
    `run_once` 는 자기가 아는 이름만 꺼내 쓰므로 그냥 지나간다. CLI 는 배치가 끝날 때 부른다.
    """
    return DefaultHooks(cfg, with_llm=with_llm, with_portfolio=with_portfolio,
                        client=client).as_dict()


__all__ = ["ADJUST_OK_STATUSES", "DefaultHooks", "HOLDINGS_PORTFOLIO", "default_hooks"]
