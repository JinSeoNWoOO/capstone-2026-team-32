"""수집 상태 요약 (운영 점검용).
사용: python -m backend.newsgap.status [--db data/newsgap.db] [--date YYYY-MM-DD]  (기본: 오늘)"""
import argparse, sqlite3
from datetime import date

DISCLOSURE_SOURCE_ID = "15"     # LS 공시 속보. advisor/sources/lsnews.py 와 같은 값


def run(db, day):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    like = f"{day}%"
    q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
    print(f"== {day}  db={db}")
    print("news total     :", q("SELECT COUNT(*) FROM news WHERE recv_wall LIKE ?", like))
    print("news with code :", q("SELECT COUNT(*) FROM news WHERE recv_wall LIKE ? AND code != ''", like))
    print("disclosures    :", q("SELECT COUNT(*) FROM news WHERE recv_wall LIKE ? AND source_id = ?",
                                like, DISCLOSURE_SOURCE_ID))
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
