"""뉴스·이벤트 분류 (결정 12의 LLM 요인 2, 설계 5.5의 `theme` 표시).

이 요인은 **가중치 0 의 관찰 요인**이다. 정치 테마주 반응이 이벤트마다 비일관적이라 점수로 쓰기
전에 기록부터 쌓는다는 결정이다 (결정 12). 그래서 여기서는 두 가지만 만든다.
  - 섹터별 `news_risk` 점수: Σ 방향/2 × 선형 감쇠, [-1,+1] 로 자름 (설계 5.2)
  - 종목별 `theme` 위험 표시: 정치 인맥으로 엮은 기사로 보이면 표시 (설계 5.5)

**호출을 아끼는 규칙이 이 모듈의 절반이다.** 국내 뉴스의 상당수는 "특징주·시황·마감" 같은
자동 기사라 부를 가치가 없다(단타 실험 첫날 규칙 BUY 13건 중 10건이 시황봇 기사였다).
그래서 ① 정규식으로 먼저 거르고 ② 제목이 같은 중복을 하나로 묶고 ③ 남은 것을 묶음으로 보낸다.
가중치 0 인 요인에 예산을 쓰지 않으려는 것이고, 그 대가로 진짜 재료를 놓칠 수 있다는 것은
`llm.news_noise_pattern` 에 적어 두었다.

뉴스 원본은 `sources/lsnews.recent_news` 가 준다(다른 담당). 계약은
`(realkey, recv_wall, code, source_id, title)` 의 제너레이터이고, **인자 이름으로 맞춰 부른다** —
그쪽 서명이 확정되기 전에 여기서 고정해 버리면 둘 중 하나는 반드시 고쳐야 하기 때문이다.
"""
import inspect
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

from ..factors.normalize import clip, linear_decay, rubric_score
from . import prompts, schemas
from .client import STATUS_BUDGET, STATUS_DISABLED, clip_text, to_datetime, tunable
from .disclosure_score import elapsed_trading_days, ref_day

log = logging.getLogger("advisor.llm.news")

# 공시 속보(LS source 15)는 공시 채점이 따로 본다. 여기서 또 세면 같은 사건이 두 번 점수가 된다.
EXCLUDE_SOURCES = ("15",)


@dataclass
class NewsOutcome:
    factor_rows: list = field(default_factory=list)
    evidence_rows: list = field(default_factory=list)
    flag_rows: list = field(default_factory=list)
    items: list = field(default_factory=list)      # 분류된 항목 (테스트·리포트용)
    n_seen: int = 0
    n_noise: int = 0
    n_sent: int = 0
    n_calls: int = 0
    n_error: int = 0
    status: str = "OK"


# ---------------------------------------------------------------- 입력

def previous_run_time(store, run_id=None, mode="live"):
    """직전 실행의 판단 시각. 없으면 None — 그때는 창(window)으로 물러선다."""
    sql = "SELECT MAX(decision_time) AS t FROM run WHERE mode=?"
    params = [mode]
    if run_id is not None:
        sql += " AND run_id<?"
        params.append(run_id)
    try:
        row = store.conn.execute(sql, params).fetchone()
    except Exception:
        return None
    return row["t"] if row else None


def since_time(store, cfg, as_of, run_id=None, mode="live"):
    """어디까지 거슬러 볼 것인가. 직전 실행 이후가 원칙이되 창보다 더 길게는 보지 않는다.

    창으로 한 번 더 자르는 이유: 며칠 쉬었다가 돌리면 직전 실행이 아주 옛날이라 그날 배치가
    수천 건을 분류하게 된다. 오래된 뉴스는 감쇠로 어차피 0 에 가깝다.
    """
    hours = float(tunable(cfg, "sources.news_window_hours"))
    floor = (to_datetime(as_of) - timedelta(hours=hours)).isoformat(timespec="milliseconds")
    prev = previous_run_time(store, run_id, mode)
    return max(str(prev), floor) if prev else floor


def _call_source(fn, pool):
    """서명에 있는 이름만 골라 넘긴다. 없는 이름을 요구하면 TypeError 로 떨어져 호출부가 기권한다."""
    sig = inspect.signature(fn)
    args, kwargs = [], {}
    for name, param in sig.parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if name in pool:
            if param.kind == param.POSITIONAL_ONLY:
                args.append(pool[name])
            else:
                kwargs[name] = pool[name]
        elif param.default is param.empty:
            raise TypeError(f"recent_news 가 모르는 인자를 요구합니다: {name}")
    return fn(*args, **kwargs)


def load_news(store, cfg, as_of, since, recent_news=None):
    """(realkey, recv_wall, code, source_id, title) 목록. 소스를 못 읽으면 빈 목록.

    `sources.lsnews` 는 늦게 import 한다 — 아직 없는 모듈 때문에 LLM 계층 전체가 import 실패하면
    안 된다 (다른 담당과 병행 작업 중이다).
    """
    fn = recent_news
    if fn is None:
        try:
            from ..sources import lsnews
            fn = lsnews.recent_news
        except Exception as exc:
            log.warning("뉴스 소스를 읽을 수 없습니다 → 뉴스 분류 건너뜀: %s", exc)
            return None
    # 시각은 **datetime 으로** 넘긴다. 수집 계층(sources/base.to_kst)은 문자열을 받지 않고,
    # 이쪽 함수들은 둘 다 받는다 — 더 까다로운 쪽에 맞추는 것이 맞다.
    end = to_datetime(as_of)
    start = to_datetime(since) if since else None
    window = float(tunable(cfg, "sources.news_window_hours"))
    hours = window
    if start is not None:                              # 필요한 만큼만 읽는다 (그쪽은 hours 로 창을 잡는다)
        span = (end - start).total_seconds() / 3600.0
        hours = min(window, max(1.0, span + 1.0))
    pool = {"store": store, "cfg": cfg, "as_of": end, "since": start, "until": end,
            "hours": hours, "window_hours": window, "exclude_disclosure": True}
    try:
        return list(_call_source(fn, pool))
    except Exception as exc:
        log.warning("뉴스 조회 실패 → 뉴스 분류 건너뜀: %s", exc)
        return None


def prefilter(rows, cfg, as_of, since):
    """시점·잡음·중복을 거른 항목 목록 [{id, key, time, code, title}] 과 (본 수, 잡음 수).

    시점 규칙이 먼저다: 수신 시각이 as_of 를 넘는 기사는 그 판단에 쓸 수 없다 (설계 2.3).
    """
    noise = re.compile(tunable(cfg, "llm.news_noise_pattern"))
    limit = to_datetime(as_of).isoformat(timespec="milliseconds")
    max_items = int(tunable(cfg, "llm.news_max_items"))
    seen_titles, kept, n_seen, n_noise = set(), [], 0, 0
    for row in rows or ():
        realkey, recv_wall, code, source_id, title = (list(row) + [None] * 5)[:5]
        title = (title or "").strip()
        when = str(recv_wall or "")
        if not title or not when:
            continue
        if when > limit or (since and when <= str(since)):
            continue
        if str(source_id or "") in EXCLUDE_SOURCES:
            continue
        n_seen += 1
        if noise.search(title):
            n_noise += 1
            continue
        norm = " ".join(title.split())
        if norm in seen_titles:                        # 같은 제목의 통신사 중복은 사건 하나다
            continue
        seen_titles.add(norm)
        kept.append({"key": realkey, "time": when, "code": (code or "") or None, "title": title})
    kept.sort(key=lambda it: it["time"])
    if len(kept) > max_items:                          # 넘치면 최근 것부터 남긴다 (감쇠 계수가 크다)
        kept = kept[-max_items:]
    for i, it in enumerate(kept, 1):
        it["id"] = f"n{i}"
    return kept, n_seen, n_noise


# ---------------------------------------------------------------- 분류·합산

def classify_news(store, cfg, cal, as_of, llm=None, sectors=None, run_id=None, recent_news=None,
                  since=None):
    """헤드라인을 묶음으로 분류해 섹터 점수·근거·테마 표시를 만든다. 예외를 내보내지 않는다."""
    out = NewsOutcome()
    sectors = list(sectors or [])
    meta = (cfg.get("factors") or {}).get("news_risk") or {}
    decay_days = int((meta.get("params") or {}).get("decay_days") or 5)
    sign = int(meta.get("sign") or 1)
    batch_size = max(1, int(tunable(cfg, "llm.news_batch_size")))
    max_calls = int(tunable(cfg, "llm.news_max_calls"))
    ev_max = int(tunable(cfg, "llm.evidence_max_chars"))
    ref = ref_day(cal, as_of)

    if since is None:
        since = since_time(store, cfg, as_of, run_id)
    rows = load_news(store, cfg, as_of, since, recent_news)
    if rows is None:
        out.status = "NO_SOURCE"
        return out
    items, out.n_seen, out.n_noise = prefilter(rows, cfg, as_of, since)
    if not items or not sectors:
        log.info("뉴스 분류: 대상 %d건(잡음 %d) → 부를 것이 없다", out.n_seen, out.n_noise)
        return out
    if llm is None:
        out.status = STATUS_DISABLED
        return out

    system = prompts.news_system(sectors, llm.prompt_ver)
    by_id = {it["id"]: it for it in items}
    classified = []
    for start in range(0, len(items), batch_size):
        if out.n_calls >= max_calls:
            log.warning("뉴스 분류 호출 수 상한 %d회 → 남은 %d건은 분류하지 않는다",
                        max_calls, len(items) - start)
            break
        chunk = items[start:start + batch_size]
        user = prompts.news_user(chunk, llm.prompt_ver)
        res = llm.call(schemas.TASK_NEWS, system, user, schemas.news_schema(sectors), "classify")
        if res.status in (STATUS_DISABLED, STATUS_BUDGET):
            out.status = res.status
            break
        if not res.cache_hit:
            out.n_calls += 1
        out.n_sent += len(chunk)
        if not res.ok:
            out.n_error += 1                           # 그 묶음만 기권한다 (다음 묶음은 계속)
            continue
        for rec in res.data:
            item = by_id.get(rec.get("id"))
            if item is None:                           # 모르는 id 는 버린다 (입력과 1:1 이 아니면 근거가 없다)
                continue
            rec["_item"] = item
            rec["call_id"] = res.call_id
            classified.append(rec)

    # ---- 섹터별 합산 (설계 5.2: Σ 방향/2 × 감쇠, [-1,+1])
    per_sector, raw_sum, seen_pairs = {}, {}, set()
    for rec in classified:
        item = rec["_item"]
        day = to_datetime(item["time"]).date()
        elapsed = elapsed_trading_days(cal, day, ref)
        decay = linear_decay(elapsed, decay_days)
        direction = int(rec.get("direction", 0))
        unit, _missing = rubric_score(direction, sign)
        out.items.append(rec)
        if rec.get("theme_suspect") and item.get("code"):
            out.flag_rows.append({
                "entity": item["code"], "flag_type": "theme", "src": "llm",
                "detail": clip_text(f"{item['time'][:16]} {item['title']} — "
                                    f"{rec.get('reason', '')}", 200)})
        if rec.get("category") == "noise" or direction == 0:
            continue
        for sector in rec.get("sectors") or ():
            if sector not in sectors:
                continue
            per_sector[sector] = per_sector.get(sector, 0.0) + unit * decay
            raw_sum[sector] = raw_sum.get(sector, 0.0) + unit
            pair = (sector, item["key"])
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            out.evidence_rows.append({
                "entity": sector, "factor_id": "news_risk", "ref_type": "news",
                "ref_id": str(item["key"] or item["id"]),
                "summary": clip_text(f"{rec.get('category')} 방향 {direction:+d} "
                                     f"심각도 {rec.get('severity')} 감쇠 {decay:.2f}"
                                     f"({elapsed}거래일) {item['title']} — "
                                     f"{clip_text(rec.get('reason'), ev_max)}", 300)})
    for sector, total in per_sector.items():
        out.factor_rows.append({"entity": sector, "factor_id": "news_risk",
                                "raw_value": raw_sum.get(sector), "score": clip(total, -1.0, 1.0),
                                "missing": 0})
    log.info("뉴스 분류: 수신 %d건 · 잡음 %d · 보냄 %d · 호출 %d · 기권 %d → 섹터 %d개, 테마 표시 %d건",
             out.n_seen, out.n_noise, out.n_sent, out.n_calls, out.n_error,
             len(out.factor_rows), len(out.flag_rows))
    return out


__all__ = ["EXCLUDE_SOURCES", "NewsOutcome", "classify_news", "load_news", "prefilter",
           "previous_run_time", "since_time"]
