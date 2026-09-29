import csv, sqlite3, sys

HORIZONS = [5, 60, 120, 300, 600]

def run(db_path, out_csv):
    conn = sqlite3.connect(db_path)
    rows = []
    for ev_id, code, base, pre, pre60, market, pspk, ai_dec, ai_conf in conn.execute(
            """SELECT e.event_id, e.code, e.base_price, e.pre_price, e.pre60_price, e.market, e.pre_spike_mult,
                      n.ai_decision, n.ai_conf
               FROM event e LEFT JOIN news n ON n.realkey = e.realkey"""):
        ticks = conn.execute("SELECT sim_t, price, qty, side FROM tick WHERE event_id=? ORDER BY sim_t", (ev_id,)).fetchall()
        if not ticks or not base:
            continue
        r = {"event_id": ev_id, "code": code, "market": market, "base_price": base, "pre_price": pre, "pre60_price": pre60,
             "pre_spike_mult": pspk, "ai_decision": ai_dec, "ai_conf": ai_conf}
        # ①이 이미 올린 폭: 뉴스 직전 1분봉 종가 → 뉴스 후 첫 체결가
        r["r_pre60_to_base_pct"] = round((base - pre60) / pre60 * 100, 3) if pre60 else None
        for h in HORIZONS:
            last = [p for t, p, q, s in ticks if t <= h]
            r[f"r_{h}s_pct"] = round((last[-1] - base) / base * 100, 3) if last else None
            w = [(q, s) for t, p, q, s in ticks if t <= h]
            q = sum(x for x, _ in w)
            r[f"buy_ratio_{h}s"] = round(sum(x for x, s in w if s == "B") / q, 3) if q else None
        # 포지션은 실제·가상(counterfactual) 어느 쪽이든 붙인다. 구분은 counterfactual 컬럼.
        pos = conn.execute("""SELECT entry_price, exit_price, exit_reason, pnl_raw, pnl_after_cost,
                                     COALESCE(counterfactual,0), entry_reason FROM position WHERE event_id=?""", (ev_id,)).fetchone()
        r.update(dict(zip(["entry_price", "exit_price", "exit_reason", "pnl_raw", "pnl_after_cost",
                           "counterfactual", "entry_reason"], pos or [None] * 7)))
        rows.append(r)
    if rows:
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
    return rows

if __name__ == "__main__":
    for r in run(sys.argv[1], sys.argv[2]):
        print(r)
