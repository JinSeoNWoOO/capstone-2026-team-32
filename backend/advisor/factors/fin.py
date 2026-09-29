"""정기보고서 값 → 분기 단독값 → 전년 동기 대비 변화 (2026-09-28, 관찰 요인 stk_earn_growth 의 계산부).

전부 **부수 효과 없는 순수 함수**다. 입력은 `asof.fin_reports` 가 as_of 로 이미 거른 행이므로
여기서는 시점을 다시 따지지 않는다 — 여기서 쓰는 것은 모두 그때 알 수 있던 값이다.

## 규칙

1. **같은 보고서가 여러 번(정정)** 있으면 접수번호가 가장 늦은 것을 쓴다 (as_of 이전에 알려진 것 중).
2. **분기 단독값(3개월)**: 1~3분기는 보고서의 thstrm_amount(3개월 값) 그대로.
   **4분기 = 사업보고서 연간 − 3분기 보고서 누적(thstrm_add_amount)**. 3분기 누적이 없으면
   1~3분기 단독값의 합으로 대신하고, 그것도 없으면 4분기는 결측이다.
   4분기 값은 사업보고서와 3분기 보고서가 **둘 다** 알려진 뒤에만 생긴다 (입력이 이미 as_of 로 걸러져 있다).
3. **연결/별도**: 같은 분기와 전년 동기가 **둘 다 연결(CFS)** 로 있으면 연결, 아니면 둘 다 별도(OFS)로
   있을 때 별도. 섞지 않는다 — 한쪽만 연결이면 자회사 편입 같은 범위 변화가 '이익 변화'로 둔갑한다.
4. **직전 분기**: 알려진 분기 중 가장 최근 것. 그 분기의 전년 동기가 없으면(신규 상장 등) 결측이다 —
   더 옛 분기로 물러서지 않는다 (물러서면 종목마다 다른 시점의 변화를 한 줄로 순위 매기게 된다).
5. **오래된 값**: 직전 분기의 기간 끝이 as_of 보다 `max_age_days` 넘게 앞이면(보고서를 안 냈다) 결측.
"""
from datetime import date

QUARTER_OF = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}
PERIOD_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
FS_ORDER = ("CFS", "OFS")                  # 연결 우선, 없으면 별도


def _get(row, key):
    return row[key]


def latest_by_report(rows, account):
    """[행] → {(연도, 분기, fs_div): (amount, cum_amount, rcept_no)}. 정정본이 있으면 늦은 접수번호."""
    out = {}
    for r in rows or ():
        if _get(r, "account") != account:
            continue
        q = QUARTER_OF.get(str(_get(r, "reprt_code")))
        if q is None:
            continue
        key = (int(_get(r, "bsns_year")), q, str(_get(r, "fs_div")))
        rec = (_get(r, "amount"), _get(r, "cum_amount"), str(_get(r, "rcept_no")))
        if key not in out or rec[2] > out[key][2]:
            out[key] = rec
    return out


def standalone_quarters(reports, fs_div):
    """{(연도, 분기): 3개월 값} — 한 가지 재무제표 구분(fs_div) 안에서만 계산한다 (규칙 2)."""
    out = {}
    years = sorted({y for (y, _, f) in reports if f == fs_div})
    for y in years:
        for q in (1, 2, 3):
            rec = reports.get((y, q, fs_div))
            if rec is not None and rec[0] is not None:
                out[(y, q)] = float(rec[0])
        annual = reports.get((y, 4, fs_div))
        if annual is None or annual[0] is None:
            continue
        q3 = reports.get((y, 3, fs_div))
        cum3 = None
        if q3 is not None and q3[1] is not None:
            cum3 = float(q3[1])
        elif all((y, q) in out for q in (1, 2, 3)):
            cum3 = out[(y, 1)] + out[(y, 2)] + out[(y, 3)]
        if cum3 is not None:
            out[(y, 4)] = float(annual[0]) - cum3
    return out


def period_end(year, quarter):
    m, d = PERIOD_END[int(quarter)]
    return date(int(year), m, d)


def yoy_change(rows, account, as_of_date, max_age_days=None):
    """직전 분기 단독값과 전년 동기 단독값. 없으면 None.

    반환 {"year", "quarter", "fs_div", "cur", "prev", "delta"} — delta = cur − prev.
    """
    reports = latest_by_report(rows, account)
    series = {fs: standalone_quarters(reports, fs) for fs in FS_ORDER}
    known = set(series["CFS"]) | set(series["OFS"])
    if not known:
        return None
    y, q = max(known)
    if max_age_days is not None and (as_of_date - period_end(y, q)).days > int(max_age_days):
        return None
    for fs in FS_ORDER:
        s = series[fs]
        if (y, q) in s and (y - 1, q) in s:
            cur, prev = s[(y, q)], s[(y - 1, q)]
            return {"year": y, "quarter": q, "fs_div": fs, "cur": cur, "prev": prev, "delta": cur - prev}
    return None


def growth_rate(change):
    """참고용 전년 동기 대비 증가율 (cur − prev) ÷ |prev|. 기준값이 0 이면 None.

    요인 값으로는 쓰지 않는다 (stock.stk_earn_growth 의 docstring 참고) — 재평가 보고서에서 두 정의를
    나란히 보여 주기 위해서만 둔다.
    """
    if not change or not change["prev"]:
        return None
    return (change["cur"] - change["prev"]) / abs(change["prev"])


__all__ = ["FS_ORDER", "QUARTER_OF", "growth_rate", "latest_by_report", "period_end",
           "standalone_quarters", "yoy_change"]
