"""코드가 만드는 위험 표시 (설계 5.5). 점수와 **별개**이고, 붙었다고 해서 점수가 깎이지 않는다.

| 표시 | 기준 (여기서 만드는 것) |
|---|---|
| `halt` | 공시 유형 거래정지·상장폐지, 또는 마지막 일봉의 거래량 0 |
| `market_action` | 공시 유형 시장조치 (투자경고·위험·단기과열) |
| `governance` | 제목에 횡령·배임·감사의견·의견거절·한정 (LLM 몫은 나중에 같은 표에 덧붙인다) |
| `spike` | 최근 5거래일 누적 수익률 절댓값 ≥ 30% |

표시가 붙은 자산은 v0 에서 **그대로 후보에 남는다**. 표시만으로 빼면 "표시가 실제로 낙폭을
예고했는가"(결정 5의 위험 표시 채점)를 잴 수 없기 때문이다. 단 `halt` 는 체결 자체가 불가능해
v0 에서도 제외한다 — 그 판단은 allocate 가 한다.

**공시의 시점**은 `rcept_dt`(접수일, 시각 없음)가 아니라 `first_seen_at`(처음 본 시각)이다
(설계 2.3의 2번 규칙). 접수일만 보면 18:30 판단이 그날 19시에 접수될 공시를 미리 아는 셈이 된다.
first_seen_at 이 비어 있는 행(과거 재현 구간에는 있을 수 없다)은 **그날 18:00 에 알았다**고
보수적으로 친다 — DART 배포가 07:30~18:00 이므로 실제보다 늦으면 늦었지 빠르지 않다.
"""
import logging
import re

from .factors import asof
from .factors.stock import adj_close

log = logging.getLogger("advisor.risk_flags")

# 표시 종류와 출력 순서. 기록이 매일 같은 순서로 쌓여야 리포트·비교가 흔들리지 않는다.
FLAG_ORDER = ("halt", "market_action", "governance", "spike")

# YAML 에 아직 없는 조정값 (risk_flags 블록에는 spike_days·spike_abs_ret 만 있다).
DEFAULTS = {
    # 공시를 '최근'으로 볼 거래일 수. 거래정지·시장조치는 며칠 안에 해제되기도 해서 짧게 둔다.
    "risk_flags.recent_days": 5,
    # first_seen_at 이 없는 공시를 몇 시에 알았다고 볼 것인가 (DART 배포 종료 시각).
    "risk_flags.disclosure_fallback_hour": 18,
    # 코드가 판정하는 지배구조 위험 제목 패턴. LLM 판정은 나중에 같은 flag_type 으로 덧붙인다.
    "risk_flags.governance_pattern": "횡령|배임|감사의견|의견거절|한정",
    # 공시 유형 → 표시. newsgap.disclosure.kind_of 가 내는 이름과 같아야 한다.
    "risk_flags.halt_kinds": ["거래정지", "상장폐지"],
    "risk_flags.market_action_kinds": ["시장조치"],
}


def _kind_of(title):
    """공시 유형. 수집이 kind 를 안 채웠을 때만 제목으로 다시 분류한다 (newsgap 재사용, 결정 15)."""
    try:
        from ..newsgap.disclosure import kind_of
    except Exception:                      # newsgap 을 못 읽어도 위험 표시가 멈추지는 않는다
        return ""
    return kind_of(title or "")


def _known_at(row, fallback_hour):
    """그 공시를 알 수 있었던 시각 (KST naive ISO 문자열). 없으면 접수일 fallback_hour 시."""
    seen = row["first_seen_at"]
    if seen:
        return str(seen)
    dt = str(row["rcept_dt"] or "").strip()
    if len(dt) == 8 and dt.isdigit():
        return f"{dt[0:4]}-{dt[4:6]}-{dt[6:8]}T{int(fallback_hour):02d}:00:00.000"
    return None


def recent_disclosures(store, cfg, cal, as_of):
    """as_of 시점에 알 수 있었던 최근 공시 행 목록 (접수일 오름차순).

    접수일 하한은 거래일 기준이고, 상한은 **as_of 의 날짜**다 — 오늘 접수된 공시는 오늘 알 수
    있고, 정작 '언제 알았는가'는 아래 knowledge_time 비교가 따로 막는다.
    """
    recent = int(asof.tunable(cfg, "risk_flags.recent_days", DEFAULTS))
    fallback_hour = int(asof.tunable(cfg, "risk_flags.disclosure_fallback_hour", DEFAULTS))
    t = asof.to_datetime(as_of)
    cutoff = asof.last_kr_date(as_of, cfg)
    start = cal.add_trading_days(cutoff, -(recent - 1)) if cal is not None else cutoff
    lo = asof.to_date_str(start).replace("-", "")
    hi = asof.to_date_str(t.date()).replace("-", "")
    rows = store.conn.execute(
        "SELECT * FROM disclosure WHERE rcept_dt>=? AND rcept_dt<=? ORDER BY rcept_dt, rcept_no",
        (lo, hi)).fetchall()
    limit = t.isoformat(timespec="milliseconds")
    out = []
    for r in rows:
        known = _known_at(r, fallback_hour)
        if known is None or known > limit:         # 아직 알 수 없었던 공시는 없는 것과 같다
            continue
        out.append(r)
    return out


def _price_flags(store, cfg, cutoff, assets):
    """일봉으로 만드는 표시: 거래량 0(halt) 과 급등락(spike)."""
    rf = cfg.get("risk_flags") or {}
    spike_days = int(rf.get("spike_days") or 5)
    spike_abs = float(rf.get("spike_abs_ret") or 0.30)
    found = []
    for code in assets:
        bars = asof.price_bars(store, code, cutoff, spike_days + 1)
        if not bars:
            continue
        last = bars[-1]
        if last["volume"] is not None and float(last["volume"]) == 0.0:
            found.append((code, "halt", f"{last['date']} 거래량 0"))
        vals = [v for v in (adj_close(b) for b in bars) if v is not None and v > 0]
        if len(vals) >= spike_days + 1 and vals[0] > 0:
            ret = vals[-1] / vals[0] - 1.0
            if abs(ret) >= spike_abs:
                found.append((code, "spike", f"{spike_days}거래일 {ret:+.1%}"))
    return found


def _disclosure_flags(store, cfg, cal, as_of, stocks):
    """공시로 만드는 표시: halt·market_action·governance (코드 몫)."""
    halt_kinds = set(asof.tunable(cfg, "risk_flags.halt_kinds", DEFAULTS))
    action_kinds = set(asof.tunable(cfg, "risk_flags.market_action_kinds", DEFAULTS))
    pattern = re.compile(asof.tunable(cfg, "risk_flags.governance_pattern", DEFAULTS))
    found = []
    for r in recent_disclosures(store, cfg, cal, as_of):
        code = r["stock_code"]
        if not code or (stocks and code not in stocks):
            continue
        title = r["report_nm"] or ""
        kind = r["kind"] or _kind_of(title)
        detail = f"{r['rcept_dt']} {title[:40]}".strip()
        if kind in halt_kinds:
            found.append((code, "halt", detail))
        if kind in action_kinds:
            found.append((code, "market_action", detail))
        if pattern.search(title):
            found.append((code, "governance", detail))
    return found


def compute_risk_flags(store, cfg, cal, as_of, uni=None):
    """(저장할 행 목록, {자산: [표시…]}) — 설계 5.5 의 코드 몫 전부.

    행은 `store.put_risk_flags` 가 그대로 받고, dict 는 `allocate.target_weights` 의 flags 인자다.
    같은 (자산, 표시)가 여러 근거로 나오면 **행은 근거마다 남기고** dict 에서는 한 번만 센다 —
    근거를 지우면 리포트에서 "왜 붙었는가"를 못 보여 준다.
    """
    uni = asof.universe_snapshot(store, cfg, as_of) if uni is None else uni
    cutoff = asof.last_kr_date(as_of, cfg)
    stocks = uni.get("stocks") or {}
    assets = uni.get("assets") or []

    found = _price_flags(store, cfg, cutoff, assets)
    try:
        found += _disclosure_flags(store, cfg, cal, as_of, stocks)
    except Exception as exc:                        # 공시 표가 비어 있어도 일봉 표시는 남긴다
        log.warning("공시 위험 표시 실패 → 건너뜀: %s", exc)

    rows, flags = [], {}
    for entity, flag_type, detail in sorted(found, key=lambda x: (x[0], FLAG_ORDER.index(x[1]), x[2])):
        rows.append({"entity": entity, "flag_type": flag_type, "src": "code", "detail": detail})
        bucket = flags.setdefault(entity, [])
        if flag_type not in bucket:
            bucket.append(flag_type)
    return rows, flags


__all__ = ["DEFAULTS", "FLAG_ORDER", "compute_risk_flags", "recent_disclosures"]
