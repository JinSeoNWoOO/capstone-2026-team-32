"""가동 상태 요약 (포스터/발표용 건수, 운영 점검용).
사용: python -m newsgap.status [--db data/newsgap.db] [--date YYYY-MM-DD]  (기본: 오늘)"""
import argparse, sqlite3
from datetime import date

def run(db, day):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    like = f"{day}%"
    q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
    print(f"== {day}  db={db}")
    print("news total     :", q("SELECT COUNT(*) FROM news WHERE recv_wall LIKE ?", like))
    print("news with code :", q("SELECT COUNT(*) FROM news WHERE recv_wall LIKE ? AND code != ''", like))
    ev_ids = [r[0] for r in c.execute("SELECT event_id FROM event WHERE realkey IN (SELECT realkey FROM news WHERE recv_wall LIKE ?)", (like,))]
    print("events         :", len(ev_ids))
    if ev_ids:
        ph = ",".join("?" * len(ev_ids))
        print("ticks          :", q(f"SELECT COUNT(*) FROM tick WHERE event_id IN ({ph})", *ev_ids))
        print("events w/ base :", q(f"SELECT COUNT(*) FROM event WHERE event_id IN ({ph}) AND baseline_amount_1m IS NOT NULL", *ev_ids))
        print("events w/ pspk :", q(f"SELECT COUNT(*) FROM event WHERE event_id IN ({ph}) AND pre_spike_mult IS NOT NULL", *ev_ids))
        for d, n in c.execute(f"SELECT decision, COUNT(*) FROM signal_log WHERE event_id IN ({ph}) GROUP BY decision", ev_ids):
            print(f"signal {d:8s}:", n)
        # AI 판정 (news.ai_decision). 라벨은 뉴스 단위로 남는다.
        ai = list(c.execute("SELECT COALESCE(ai_decision,'(none)'), COUNT(*) FROM news WHERE recv_wall LIKE ? GROUP BY 1", (like,)))
        print("ai decisions   :", ", ".join(f"{d}={n}" for d, n in ai) or "-")
        lat = c.execute("SELECT COUNT(*), AVG(ai_latency_ms) FROM news WHERE recv_wall LIKE ? AND ai_latency_ms IS NOT NULL", (like,)).fetchone()
        if lat[0]:
            print("ai latency ms  :", f"n={lat[0]} avg={lat[1]:.0f}")
        # 포지션: 실제(counterfactual=0) 와 가상(=1, AI 거절·RiskGuard 차단) 분리
        for label, cf in (("positions real ", 0), ("positions cf   ", 1)):
            r = c.execute(f"SELECT COUNT(*), COALESCE(SUM(pnl_after_cost),0) FROM position "
                          f"WHERE event_id IN ({ph}) AND COALESCE(counterfactual,0)=?", (*ev_ids, cf)).fetchone()
            print(f"{label}:", r[0], " pnl_after_cost:", round(r[1]))
    print("-- last news")
    for row in c.execute("SELECT recv_wall, code, substr(title,1,50) FROM news WHERE recv_wall LIKE ? ORDER BY recv_wall DESC LIMIT 5", (like,)):
        print("  ", *row)
    print("-- last session_log")
    for row in c.execute("SELECT wall, level, substr(msg,1,100) FROM session_log ORDER BY id DESC LIMIT 6"):
        print("  ", *row)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/newsgap.db")
    ap.add_argument("--date", default=date.today().isoformat())
    a = ap.parse_args()
    run(a.db, a.date)
