"""제출용 수익률 비교 그래프 생성기.

```
python -m backend.advisor.devtools.plot_nav --db data/advisor.db --out docs/figures
python -m backend.advisor.devtools.plot_nav --db data/advisor_long.db --prefix replay_long   # 긴 재현 복사본
```

`--prefix` 는 출력 파일 이름의 앞부분이다 (`<prefix>_nav.png`, `<prefix>_summary.csv`, 기본 `replay`).
그림 제목의 기간은 DB 에 있는 재현 NAV 의 첫날·마지막 날에서 읽는다.

**왜 필요한가.** 졸업과제 제출물은 "시스템이 기준선보다 나았는가"를 그림 한 장으로 보여야 한다.
비교할 대상은 재현 모드(`%@replay`, 2026-04-01~09-21)뿐이다 — 실시간 기록은 이틀치뿐이라
곡선으로 그릴 표본이 못 된다 (README/설계 문서 참고). **실시간과 재현을 한 곡선으로 잇지 않는다**:
체결 방식·비용 함수는 같아도 재현은 사후에 한 번에 계산한 값이고 실시간은 그날그날 쌓인 값이라
이어 붙이면 마치 한 포트폴리오가 쭉 굴러간 것처럼 보인다.

**DB 는 읽기 전용으로 연다.** 다른 프로세스(뉴스 수집기·스케줄러)가 같은 파일에 쓰는 중일 수
있으므로 `mode=ro` 로만 열고, 이 스크립트는 아무것도 쓰지 않는다.

**정규화는 "첫 행으로 나누기"가 아니다.** 포트폴리오는 체결 전 NAV=`portfolio.start_nav`
(설정, 기본 1.0)에서 출발하고, `nav` 테이블의 첫 행은 이미 그 첫 거래일의 수익이 반영된 값이다
(`portfolio._summary_from_series` 와 같은 규약). 첫 행 값으로 나머지를 나누면 첫날 수익률 자체가
사라진다. 그래서 여기서는 raw NAV 를 그대로 쓰고, 곡선 맨 앞에 (그 계열의 첫 거래일 전날짜,
start_nav) 점을 하나 끼워 넣어 "1.0에서 출발해 첫날 수익이 반영된 곳까지 이어지는" 그림이 되게
한다. 요약표의 누적수익률·MDD 도 이 끼워 넣은 시작점을 포함해서 낸다.

**`sys_final_llm@replay` 를 그리지 않는 이유.** 재현 모드는 이미 벌어진 날짜를 그대로 다시
계산하는 것이라 그날의 LLM 을 다시 호출하지 않는다 (호출하면 그때그때 답이 달라져 "재현"이 아니게
된다). 그래서 `sys_final_llm@replay` 는 값이 `sys_final_v0@replay` 와 완전히 같다 — 그리면 선이
겹쳐 잉크만 낭비한다. 대신 그래프 캡션과 이 스크립트 출력에 "재현에서는 LLM 판단 = v0" 라고 적는다.
"""
import argparse
import csv
import math
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .. import portfolio as pf
from ..config import load_config

# 그릴 순서 그대로가 범례 순서다. 시스템 계열은 눈에 띄는 색(빨강 계열), 기준선은 차분한 회색조
# 점선으로 — 시스템과 기준선이 한눈에 구분되게 (결정 1: 기준선은 비교 대상이지 주인공이 아니다).
PLOT_SERIES = [
    # (portfolio_id, 범례 이름, 색, 선 모양, 굵기)
    ("sys_final_v0@replay",    "시스템 최종 (v0)",       "#e34948", "-",  2.2),
    ("sys_prelim_llm@replay",  "시스템 예비 (LLM)",      "#eb6834", "-",  1.8),
    ("bl_kodex200@replay",     "기준선: KODEX 200 보유", "#6b6b6b", "--", 1.4),
    ("bl_6040@replay",         "기준선: 60/40",          "#9a9a9a", "-.", 1.4),
    ("bl_sma10m@replay",       "기준선: 10개월 이동평균", "#4d4d4d", ":",  1.6),
]
# 재현에서 v0 와 완전히 같아 그리지 않지만, 표에는 참고로 남긴다.
SKIPPED_IDENTICAL = "sys_final_llm@replay"

# 실시간 기록은 곡선을 그릴 만큼 날짜가 없다 — 표에 참고 행으로만 남긴다.
LIVE_PORTFOLIOS = ["sys_final_v0", "sys_final_llm", "bl_kodex200", "bl_6040", "bl_sma10m"]


# ---------------------------------------------------------------- 읽기

def _connect(db_path):
    """읽기 전용 연결. 다른 프로세스가 쓰고 있어도 안전하다."""
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _load_nav(conn, portfolio_id):
    """(dates, nav, turnover, cost) — 날짜순 정렬. nav 는 raw 값(정규화하지 않는다)."""
    rows = conn.execute(
        "SELECT date, nav, turnover, cost FROM nav WHERE portfolio_id = ? ORDER BY date",
        (portfolio_id,),
    ).fetchall()
    dates = [r[0] for r in rows]
    nav = [r[1] for r in rows]
    turnover = [r[2] or 0.0 for r in rows]
    cost = [r[3] or 0.0 for r in rows]
    return dates, nav, turnover, cost


def _prev_day(date_str):
    """그 날짜의 전날 (달력일 기준 — 그림의 시작점을 찍을 자리일 뿐 거래일일 필요는 없다)."""
    return (date.fromisoformat(date_str) - timedelta(days=1)).isoformat()


def _with_start(dates, nav, start_nav):
    """맨 앞에 (첫 거래일 전날, start_nav) 를 끼워 넣은 (dates, nav). 그림·MDD 계산에 함께 쓴다."""
    if not dates:
        return [], []
    return [_prev_day(dates[0])] + dates, [start_nav] + list(nav)


# ---------------------------------------------------------------- 지표

def _drawdown(nav_series):
    """낙폭 곡선 (running max 대비 하락률, 0 이하). nav_series 는 시작점이 이미 끼워진 것이어야 한다."""
    peak = nav_series[0]
    out = []
    for v in nav_series:
        peak = max(peak, v)
        out.append(v / peak - 1.0)
    return out


def _metrics(dates, nav, turnover, cost, start_nav, days_per_year):
    """누적 수익률·MDD·연율 변동성·샤프(무위험 0)·누적 회전율·누적 비용.

    `portfolio._summary_from_series` 와 같은 규약: series 맨 앞에 start_nav 를 끼워 넣고 그
    변화율로 계산한다 (첫 행을 기준으로 나누면 첫날 수익률이 지워진다). 분산은 표본분산
    (n-1) — 역시 그 함수와 같은 값이 나오게 맞춘 것이다.
    """
    if not nav:
        return {"구간": "", "표본일수": 0, "누적수익률": None, "MDD": None,
                "연율변동성": None, "샤프": None, "누적회전율": 0.0, "누적비용": 0.0}
    series = [start_nav] + list(nav)
    rets = [series[i] / series[i - 1] - 1.0 for i in range(1, len(series)) if series[i - 1]]
    mean_r = sum(rets) / len(rets) if rets else 0.0
    std_r = None
    if len(rets) >= 2:
        var_r = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)
        std_r = math.sqrt(var_r)
    ann_vol = std_r * math.sqrt(days_per_year) if std_r is not None else None
    sharpe = (mean_r / std_r * math.sqrt(days_per_year)) if std_r else None
    dd = _drawdown(series)
    return {
        "구간": f"{dates[0]}~{dates[-1]}",
        "표본일수": len(dates),
        "누적수익률": series[-1] / start_nav - 1.0,
        "MDD": min(dd),
        "연율변동성": ann_vol,
        "샤프": sharpe,
        "누적회전율": sum(turnover),
        "누적비용": sum(cost),
    }


# ---------------------------------------------------------------- 그림

def _style_axes(ax):
    ax.grid(True, axis="y", linewidth=0.5, alpha=0.35)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


def _period_label(dates):
    """그림 제목에 쓸 기간 'YYYY-MM-DD ~ YYYY-MM-DD'. 계열이 비어 있으면 빈 문자열."""
    return f"{dates[0]} ~ {dates[-1]}" if dates else ""


def plot_replay(conn, out_path, start_nav):
    plt.rcParams["font.family"] = "NanumGothic"
    plt.rcParams["axes.unicode_minus"] = False

    fig, (ax_nav, ax_dd) = plt.subplots(
        2, 1, figsize=(10, 7), dpi=150, sharex=True, layout="constrained",
        gridspec_kw={"height_ratios": [3, 1]},
    )

    ref_dates = None
    span = []
    for pid, label, color, ls, lw in PLOT_SERIES:
        dates, nav, _, _ = _load_nav(conn, pid)
        ext_dates, ext_nav = _with_start(dates, nav, start_nav)
        span += dates[:1] + dates[-1:]
        ax_nav.plot(ext_dates, ext_nav, label=label, color=color, linestyle=ls, linewidth=lw)
        ax_dd.plot(ext_dates, _drawdown(ext_nav), color=color, linestyle=ls, linewidth=lw)
        if ref_dates is None or len(ext_dates) > len(ref_dates):
            ref_dates = ext_dates  # 눈금은 날짜 범위가 가장 넓은 계열(시작점 포함) 기준으로 뽑는다

    ax_nav.axhline(start_nav, color="#c8c8c8", linewidth=0.8, zorder=0)
    ax_nav.set_ylabel(f"NAV (시작={start_nav:.1f})")
    ax_nav.set_title(f"재현 모드 수익률 비교 ({_period_label(sorted(span))})")
    ax_nav.legend(loc="upper left", fontsize=9, frameon=False)
    _style_axes(ax_nav)

    ax_dd.set_ylabel("낙폭")
    ax_dd.yaxis.set_major_formatter(lambda v, _pos: f"{v:.0%}")
    _style_axes(ax_dd)

    # 눈금이 촘촘해지지 않게 x축은 몇 개만 남긴다.
    ticks = ref_dates[::max(1, len(ref_dates) // 8)]
    ax_dd.set_xticks(ticks)
    ax_dd.tick_params(axis="x", rotation=30)

    fig.supxlabel(
        "재현에서는 LLM 판단 = v0 여서 sys_final_llm@replay 는 겹쳐 그리지 않음(요약표에는 참고로 남김).\n"
        "실시간 기록(2026-09-22~23)은 이틀뿐이라 곡선으로 그리지 않음. 맨 앞 점은 체결 전 시작 NAV(설정값).",
        fontsize=7.5, color="#666666", ha="left", x=0.01,
    )
    fig.savefig(out_path)
    plt.close(fig)


# ---------------------------------------------------------------- 표

def write_summary(conn, out_path, start_nav, days_per_year):
    """replay 계열 지표 + (참고) sys_final_llm@replay + (참고) 실시간 마지막 NAV."""
    rows = []
    for pid, label, *_ in PLOT_SERIES:
        dates, nav, turnover, cost = _load_nav(conn, pid)
        m = _metrics(dates, nav, turnover, cost, start_nav, days_per_year)
        rows.append({"portfolio_id": pid, "이름": label, "구분": "재현", **m})

    # v0 와 값이 같은지 확인해서 참고 행으로 남긴다 (다르면 원인을 알아야 하므로 표에 그대로 적는다).
    dates_llm, nav_llm, turnover_llm, cost_llm = _load_nav(conn, SKIPPED_IDENTICAL)
    dates_v0, nav_v0, _, _ = _load_nav(conn, "sys_final_v0@replay")
    identical = (dates_llm == dates_v0 and nav_llm == nav_v0)
    m = _metrics(dates_llm, nav_llm, turnover_llm, cost_llm, start_nav, days_per_year)
    rows.append({
        "portfolio_id": SKIPPED_IDENTICAL,
        "이름": "시스템 최종 (LLM, 참고 — v0와 " + ("동일" if identical else "다름! 원인 확인 필요") + ")",
        "구분": "재현(참고, 미표시)",
        **m,
    })

    for pid in LIVE_PORTFOLIOS:
        dates, nav, turnover, cost = _load_nav(conn, pid)
        if not dates:
            continue
        rows.append({
            "portfolio_id": pid,
            "이름": pid + " (실시간, 참고)",
            "구분": f"실시간·{len(dates)}일뿐(곡선 생략)",
            "구간": f"{dates[0]}~{dates[-1]}",
            "표본일수": len(dates),
            "누적수익률": nav[-1] / start_nav - 1.0,
            "MDD": None, "연율변동성": None, "샤프": None,
            "누적회전율": sum(turnover), "누적비용": sum(cost),
        })

    fields = ["portfolio_id", "이름", "구분", "구간", "표본일수", "누적수익률", "MDD",
              "연율변동성", "샤프", "누적회전율", "누적비용"]
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return rows


# ---------------------------------------------------------------- main

def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m backend.advisor.devtools.plot_nav",
                                description="재현 모드 NAV 비교 그래프·요약표 생성")
    p.add_argument("--db", default="data/advisor.db", help="advisor.db 경로 (읽기 전용으로 엽니다)")
    p.add_argument("--out", default="docs/figures", help="출력 디렉터리")
    p.add_argument("--prefix", default="replay",
                   help="출력 파일 이름 앞부분: <prefix>_nav.png, <prefix>_summary.csv (기본 replay)")
    args = p.parse_args(argv)

    root = Path(__file__).resolve().parents[3]
    db = Path(args.db)
    if not db.is_absolute():
        db = root / db
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = root / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = load_config()
    start_nav = float(pf.opt_cfg(cfg, "portfolio.start_nav"))
    days_per_year = float(pf.opt_cfg(cfg, "portfolio.trading_days_per_year"))

    conn = _connect(db)
    try:
        png_path = out_dir / f"{args.prefix}_nav.png"
        csv_path = out_dir / f"{args.prefix}_summary.csv"
        plot_replay(conn, png_path, start_nav)
        rows = write_summary(conn, csv_path, start_nav, days_per_year)
    finally:
        conn.close()

    print(f"그렸습니다: {png_path}")
    print(f"요약표: {csv_path}")
    for r in rows:
        if r["구분"] == "재현":
            print(f"  {r['이름']:22s} 누적수익률 {r['누적수익률']:+.4f}  MDD {r['MDD']:+.4f}  "
                  f"연율변동성 {r['연율변동성']:.4f}  샤프 {r['샤프']:.3f}  "
                  f"회전율 {r['누적회전율']:.2f}  비용 {r['누적비용']:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
