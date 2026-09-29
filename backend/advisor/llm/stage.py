"""배치(run.py)가 부르는 두 개의 함수 (설계 4장의 5번·7번 단계).

`run.py` 는 LLM 의 사정을 몰라야 한다. 그래서 이 모듈이 다음을 전부 흡수한다.
  - 재현 모드면 **부르지 않는다** (설계 7.4: 과거 뉴스·공시에 대한 LLM 판단은 기억 누출 위험이
    있고 그때의 first_seen_at 도 없다). 상태만 `SKIPPED_REPLAY` 로 돌려준다.
  - 꺼져 있거나 예산을 넘겼거나 실패해도 **예외 없이 상태만** 돌려준다. 그날은 v0 만 기록된다 (결정 15).
  - 저장(점수·근거·위험 표시)은 여기서 한 번에 한다. 중간에 실패해도 절반만 저장된 표가 남지 않게
    한 실행의 쓰기를 한곳에 모았다.

`llm_factors` 는 `factors/compute.py` 가 깔아 둔 **결측 자리(stk_disclosure·news_risk)를 덮어쓴다**
(INSERT OR REPLACE). 표의 모양은 그대로 두고 값만 채우는 구조라, LLM 이 없는 날에도 표의 행 수가
같아 채점·재평가의 유효 표본이 흔들리지 않는다.
"""
import logging

from .adjust import market_context, run_adjust
from .client import STATUS_BUDGET, STATUS_DISABLED, STATUS_OK, LLMClient
from .disclosure_score import score_disclosures
from .news_risk import classify_news

log = logging.getLogger("advisor.llm.stage")

# LLM 이 값을 채우는 요인. 재기록할 때 지울 근거 행의 범위이기도 하다.
LLM_FACTORS = ("stk_disclosure", "news_risk")
STATUS_REPLAY = "SKIPPED_REPLAY"


def run_mode(store, run_id):
    """그 실행의 mode (live|replay). 실행 기록이 없으면 live 로 본다."""
    if run_id is None:
        return "live"
    try:
        row = store.conn.execute("SELECT mode FROM run WHERE run_id=?", (run_id,)).fetchone()
    except Exception:
        return "live"
    return (row["mode"] if row and row["mode"] else "live")


def _universe(store, cfg, as_of):
    """요인 표와 **같은** 대상 목록 스냅샷. entity 이름이 어긋나면 덮어쓰기가 빗나간다.

    같은 이유로 `factors/asof.universe_snapshot` 을 그대로 쓰고, 그 모듈을 아직 읽을 수 없을 때만
    (병행 작업 중이다) 설정과 universe 표만 보는 최소 경로로 물러선다.
    """
    try:
        from ..factors.asof import universe_snapshot
        return universe_snapshot(store, cfg, as_of)
    except Exception as exc:
        log.warning("대상 목록 스냅샷을 읽지 못해 설정만으로 물러섭니다: %s", exc)
        day = str(as_of)[:10]
        stocks, sector_of = {}, {}
        try:
            snap = store.conn.execute(
                "SELECT MAX(as_of_date) FROM universe WHERE as_of_date<=?", (day,)).fetchone()[0]
            if snap:
                for r in store.conn.execute("SELECT * FROM universe WHERE as_of_date=?", (snap,)):
                    if r["kind"] == "stock":
                        stocks[r["code"]] = dict(r)
                        if r["sector"]:
                            sector_of[r["code"]] = r["sector"]
        except Exception:
            pass
        sector_etf = dict((cfg.get("universe") or {}).get("sector_etfs") or {})
        sectors = sorted(set(sector_of.values()) | set(sector_etf))
        return {"stocks": stocks, "sector_of": sector_of, "sector_etf": sector_etf,
                "sectors": sectors, "etfs": {}, "assets": sorted(stocks)}


def _dedupe_flags(store, run_id, rows):
    """같은 실행에 이미 있는 (자산, 표시, 내용) 은 다시 넣지 않는다.

    risk_flag 는 덧붙이기 표라 지우지 않는다 — 코드가 만든 표시(risk_flags.py)를 LLM 단계가
    지워 버리면 "왜 붙었는가"의 근거가 사라진다. 대신 똑같은 줄만 걸러 낸다.
    """
    try:
        have = {(r["entity"], r["flag_type"], r["detail"]) for r in store.conn.execute(
            "SELECT entity,flag_type,detail FROM risk_flag WHERE run_id=?", (run_id,))}
    except Exception:
        have = set()
    out = []
    for r in rows:
        key = (r["entity"], r["flag_type"], r["detail"])
        if key in have:
            continue
        have.add(key)
        out.append(r)
    return out


def llm_factors(store, cfg, cal, run_id, as_of, stage=None, llm=None):
    """LLM 요인 두 개를 채운다 (설계 4장의 5번). → {status, n_calls, cost, …}

    쓰는 곳:
      - `factor_value` : stk_disclosure(종목별), news_risk(섹터별) — 결측 자리를 덮어쓴다
      - `factor_evidence` : 공시(ref_type='disclosure', ref_id=접수번호), 뉴스(ref_type='news', ref_id=realkey)
      - `risk_flag` : governance(공시), theme(뉴스)

    `llm` 을 주면 그 클라이언트를 쓰고 **닫지 않는다** — 배치 한 번은 클라이언트 하나를 쓰고
    (호출 통계·이벤트 루프가 한 벌이어야 한다) 그 수명은 만든 쪽(`advisor/hooks.py`)이 진다.
    주지 않으면 예전처럼 여기서 만들고 여기서 닫는다.
    """
    mode = run_mode(store, run_id)
    if mode == "replay":
        log.info("재현 모드 → LLM 요인 건너뜀 (설계 7.4)")
        return {"status": STATUS_REPLAY, "n_calls": 0, "cost": 0.0, "mode": mode}

    uni = _universe(store, cfg, as_of)
    owned = llm is None
    llm = LLMClient(cfg, store, run_id) if owned else llm
    detail = {}
    try:
        # 꺼져 있어도 클라이언트를 그대로 넘긴다 — 첫 호출이 DISABLED 행 하나를 남겨
        # "그날 왜 LLM 값이 없는가"가 llm_call 에 기록된다 (그 뒤로는 부르지 않는다).
        disc = score_disclosures(store, cfg, cal, as_of, llm, stocks=uni.get("stocks") or None)
        news = classify_news(store, cfg, cal, as_of, llm, sectors=uni.get("sectors") or [],
                             run_id=run_id)
        rows = list(disc.factor_rows) + list(news.factor_rows)
        evidence = list(disc.evidence_rows) + list(news.evidence_rows)
        flags = list(disc.flag_rows) + list(news.flag_rows)
        if rows:
            store.put_factor_values(run_id, rows)
        if evidence:
            # 같은 실행에서 다시 불렸을 때 근거가 두 번 쌓이지 않게 내 몫만 지우고 다시 쓴다.
            store.conn.execute(
                f"DELETE FROM factor_evidence WHERE run_id=? AND factor_id IN "
                f"({','.join('?' * len(LLM_FACTORS))})", (run_id,) + LLM_FACTORS)
            store.put_factor_evidence(run_id, evidence)
        flags = _dedupe_flags(store, run_id, flags)
        if flags:
            store.put_risk_flags(run_id, flags)
        store.commit()
        detail = {"disclosure": {"status": disc.status, "code": disc.n_code, "llm": disc.n_llm,
                                 "error": disc.n_error, "skipped": disc.n_skipped,
                                 "stocks": len(disc.factor_rows)},
                  "news": {"status": news.status, "seen": news.n_seen, "noise": news.n_noise,
                           "sent": news.n_sent, "error": news.n_error,
                           "sectors": len(news.factor_rows), "theme": len(news.flag_rows)},
                  "flags": len(flags), "evidence": len(evidence)}
        status = _combine_status(llm, disc.status, news.status)
    except Exception as exc:                           # 여기서 죽으면 그날 판단 전체가 없어진다
        log.exception("LLM 요인 단계 실패 → v0 만 기록합니다: %s", exc)
        status, detail = "ERROR", {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        if owned:
            llm.close()
    stats = llm.stats
    return {"status": status, "n_calls": stats.n_api, "cost": stats.cost,
            "cost_unknown": stats.cost_unknown, "n_cache": stats.n_cache,
            "n_error": stats.n_error, "mode": mode, "stage": stage, "detail": detail}


def _combine_status(llm, *parts):
    """부분 상태를 하나로. 막힌 이유가 있으면 그것을 그대로 올린다 (기권과 중립을 구분하기 위해)."""
    if not llm.enabled:
        return STATUS_DISABLED
    if STATUS_BUDGET in parts:
        return STATUS_BUDGET
    if all(p == STATUS_OK for p in parts):
        return STATUS_OK
    return "PARTIAL"


def llm_adjust(store, cfg, run_id, v0_composites, sector_scores=None, market_ctx=None, flags=None,
               holdings=None, names=None, llm=None):
    """종합 점수 조정 (설계 4장의 7번). → {adjustments, status, n_calls, cost}

    `adjustments` 는 {code: Adjustment} 이고, 비어 있으면 그날 LLM 판단은 v0 와 같다.
    호출부는 status 를 `composite`(variant='llm') 나 `run.note` 에 남겨 "조정 없음"이 기권인지
    "움직일 이유가 없었다"인지 구분할 수 있게 한다 (결정 4).

    `llm` 의 뜻은 `llm_factors` 와 같다 — 주면 쓰고 닫지 않는다.
    """
    mode = run_mode(store, run_id)
    if mode == "replay":
        log.info("재현 모드 → LLM 조정 건너뜀 (설계 7.4)")
        return {"adjustments": {}, "status": STATUS_REPLAY, "n_calls": 0, "cost": 0.0, "mode": mode}

    owned = llm is None
    llm = LLMClient(cfg, store, run_id) if owned else llm
    try:
        ctx = market_ctx if market_ctx is not None else market_context(store, run_id)
        adjustments, status = run_adjust(store, cfg, llm, v0_composites, sector_scores, ctx,
                                         flags, holdings, run_id, names=names)
        store.commit()
    except Exception as exc:
        log.exception("LLM 조정 단계 실패 → 조정 없음으로 기록합니다: %s", exc)
        adjustments, status = {}, "ERROR"
    finally:
        if owned:
            llm.close()
    stats = llm.stats
    return {"adjustments": adjustments, "status": status, "n_calls": stats.n_api,
            "cost": stats.cost, "cost_unknown": stats.cost_unknown, "n_cache": stats.n_cache,
            "mode": mode}


__all__ = ["LLM_FACTORS", "STATUS_REPLAY", "llm_adjust", "llm_factors", "run_mode"]
