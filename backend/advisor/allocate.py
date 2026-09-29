"""점수 → 목표 비중 (결정 10, 설계 6.1·6.2). 부수 효과가 없는 순수 함수다.

DB도 시각도 보지 않는다. 같은 입력이면 항상 같은 비중이 나와야 판단을 사후에 재현·재평가할 수
있고 (결정 6), 도전 가중치 안으로 같은 날을 다시 계산하는 일이 단순 함수 호출이 된다.

구조는 "1단계 현금 대 위험자산 → 2단계 핵심 대 위성"이다.
  risk_weight = clip(risk_base + risk_slope × 시장 점수, risk_min, risk_max)
  현금 = 1 − risk_weight                      → 현금 대용 ETF
  핵심 = risk_weight × core_share             → KODEX 200
  위성 = 나머지 → 섹터 몫(sector_share)과 종목 몫으로 나눈다
**담을 것이 없으면 그 몫은 전부 핵심(core)으로 간다** — "끌리는 종목이 없으면 지수를 든다"가
'주식·ETF·현금 중 선택'에서 ETF 선택지가 하는 일이다 (결정 10).
"""
from .factors.normalize import clip

# 비중의 합은 1 이어야 한다. 부동소수점 오차만 허용한다.
_SUM_TOL = 1e-9


def _as_set(x):
    return set(x) if x else set()


def _blocked(asset, flags, vetoed):
    """체결 자체가 불가능하거나(halt) LLM 이 거부한 자산인가.

    halt 는 v0 에서도 빼고(설계 5.5), 그 밖의 위험 표시는 후보에 남긴다 — 표시만으로 빼면
    '표시가 실제로 낙폭을 예고했는가'를 채점할 수 없다.
    """
    return "halt" in _as_set((flags or {}).get(asset)) or asset in (vetoed or set())


def target_weights(market_score, sector_scores, stock_scores, holdings, flags, cfg, vetoed=None):
    """목표 비중 [{asset, role, weight}] (role: cash|core|sector|stock). 합은 정확히 1.

    market_score 가 None 이면 risk_base 를 쓴다 — 시장 요인을 하나도 못 구한 날에 0점(중립)으로
    치면 "자료 없음"이 "중립 판단"이 되고, 그 차이가 위험자산 비중에 그대로 남는다.
    holdings 는 현재 보유 자산(코드) 모음이고 순위 이력(exit_rank)에만 쓴다.
    """
    al, uni = cfg["allocate"], cfg["universe"]
    vetoed = _as_set(vetoed)
    flags = flags or {}

    base = float(al["risk_base"])
    raw_risk = base if market_score is None else base + float(al["risk_slope"]) * float(market_score)
    risk = clip(raw_risk, float(al["risk_min"]), float(al["risk_max"]))
    cash = 1.0 - risk
    core = risk * float(al["core_share"])
    satellite = risk - core
    sector_pool = satellite * float(al["sector_share"])
    stock_pool = satellite - sector_pool

    # 섹터 몫: 점수 상위 n_sectors 개 슬롯에 균등. 점수가 0 이하이거나 담을 ETF 가 없는 슬롯은 core 로.
    n_sectors = int(al["n_sectors"])
    per_sector = sector_pool / n_sectors if n_sectors > 0 else 0.0
    etf_of = uni.get("sector_etfs") or {}
    ranked_sectors = sorted(((s, sc) for s, sc in (sector_scores or {}).items() if sc is not None),
                            key=lambda kv: (-kv[1], kv[0]))
    sector_rows = []
    for sector, score in ranked_sectors:
        if len(sector_rows) >= n_sectors:
            break
        if score <= 0:
            break                                   # 내림차순이므로 아래는 전부 0 이하다
        etf = etf_of.get(sector)
        if not etf or _blocked(etf, flags, vetoed):
            continue
        sector_rows.append({"asset": etf, "role": "sector", "weight": per_sector})

    # 종목 몫: 상위 n_stocks 슬롯에 균등, 종목당 stock_cap(전체 대비) 상한.
    # 슬롯을 못 채우거나 상한을 넘는 몫은 core 로 간다 (설계 6.1).
    n_stocks = int(al["n_stocks"])
    cap = float(al["stock_cap"])
    per_stock = min(stock_pool / n_stocks, cap) if n_stocks > 0 else 0.0
    stock_rows = [{"asset": code, "role": "stock", "weight": per_stock}
                  for code in select_stocks(stock_scores, holdings, flags, cfg, vetoed)]

    # 쓰지 못한 몫(빈 슬롯 + 상한 초과분)은 전부 핵심으로
    used = sum(r["weight"] for r in sector_rows + stock_rows)
    rows = [{"asset": uni["cash_etf"], "role": "cash", "weight": cash},
            {"asset": uni["core_etf"], "role": "core", "weight": core + sector_pool + stock_pool - used}]
    rows += sector_rows + stock_rows

    rows = [r for r in rows if r["weight"] > 0]
    total = sum(r["weight"] for r in rows)
    assert abs(total - 1.0) < _SUM_TOL, f"목표 비중의 합이 1이 아닙니다: {total!r}"
    # 같은 자산이 두 역할로 들어오면 (asset 이 키인) target_weight 저장에서 한 줄이 조용히 사라진다.
    assert len({r["asset"] for r in rows}) == len(rows), f"자산이 중복됐습니다: {[r['asset'] for r in rows]}"
    return rows


def stock_ranks(stock_scores):
    """종합 점수 내림차순 순위 {code: 1..N}. 동점은 코드 순으로 갈라 매일 같은 결과가 나오게 한다.

    순위는 **거르기 전 전체 대상**에서 매긴다. halt·거부로 빠진 종목 때문에 남은 종목의 순위가
    올라가면 보유 종목의 exit_rank 판정이 그날의 제외 건수에 따라 흔들린다.
    """
    ordered = sorted((stock_scores or {}).items(), key=lambda kv: (-kv[1], kv[0]))
    return {code: i + 1 for i, (code, _) in enumerate(ordered)}


def select_stocks(stock_scores, holdings, flags, cfg, vetoed=None):
    """담을 종목 목록 (순위 순). 보유 종목이 먼저 자리를 잡는다 (설계 6.2의 이력 규칙).

    - 보유 종목은 순위가 exit_rank 밖으로 밀리거나 점수가 0 이하가 되거나 halt·거부될 때만 뺀다.
      매일 순위가 한두 칸 흔들릴 때마다 갈아타면 회전율이 급증하고 거래세 0.20%에 진다 (결정 10).
    - 남은 자리는 점수 순으로 채운다. 점수가 0 이하인 종목은 채우지 않고 그 몫을 core 로 보낸다
      ("끌리는 종목이 없으면 지수를 든다"). halt·거부로 빠진 자리는 **다음 순위로 채운다** (결정 10).
    """
    al = cfg["allocate"]
    n_stocks, exit_rank = int(al["n_stocks"]), int(al["exit_rank"])
    vetoed = _as_set(vetoed)
    held = _as_set(holdings)
    scores = stock_scores or {}
    ranks = stock_ranks(scores)
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))

    def ok(code, rank_limit=None):
        if scores[code] <= 0 or _blocked(code, flags, vetoed):
            return False
        return rank_limit is None or ranks[code] <= rank_limit

    selected = [c for c, _ in ordered if c in held and ok(c, exit_rank)][:n_stocks]
    for code, _ in ordered:
        if len(selected) >= n_stocks:
            break
        if code not in selected and ok(code):
            selected.append(code)
    return selected


def apply_rebalance_band(current, target, band, core_asset=None):
    """리밸런싱 기준을 적용한 실제 목표 비중 {asset: weight} (설계 6.2).

    자산별 (목표 − 현재) 차이가 band 미만이면 거래하지 않는다. 단 신규 편입(현재 0)과
    전량 제외(목표 0)는 차이와 무관하게 실행한다 — 담거나 빼는 결정 자체가 판단이기 때문이다.
    거래하지 않은 만큼 비중의 합이 1에서 어긋나므로 그 잔여분을 핵심 ETF 로 몰아 맞춘다.
    잔여분 흡수에는 band 를 적용하지 않는다(적용하면 합이 다시 1이 아니게 된다).
    """
    current, target = dict(current or {}), dict(target or {})
    out = {}
    for asset in set(current) | set(target):
        cur, tgt = float(current.get(asset, 0.0)), float(target.get(asset, 0.0))
        new_entry, full_exit = cur == 0.0, tgt == 0.0
        out[asset] = tgt if (new_entry or full_exit or abs(tgt - cur) >= band) else cur

    out = {a: w for a, w in out.items() if w != 0.0}
    leftover = 1.0 - sum(out.values())
    if abs(leftover) > _SUM_TOL:
        if not core_asset:
            raise ValueError("거래하지 않은 잔여분을 담을 core_asset 이 필요합니다")
        out[core_asset] = out.get(core_asset, 0.0) + leftover
        if out[core_asset] == 0.0:
            out.pop(core_asset)
    total = sum(out.values())
    assert abs(total - 1.0) < _SUM_TOL, f"리밸런싱 후 비중의 합이 1이 아닙니다: {total!r}"
    return out


def as_dict(rows):
    """[{asset, role, weight}] → {asset: weight}. 포트폴리오 계산이 쓰는 형태."""
    return {r["asset"]: r["weight"] for r in rows}
