import sqlite3, json, os, time
from datetime import datetime

DDL = """
CREATE TABLE IF NOT EXISTS raw (id INTEGER PRIMARY KEY, recv_mono REAL, recv_wall TEXT, tr_cd TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS news (realkey TEXT PRIMARY KEY, ls_datetime TEXT, recv_wall TEXT, recv_mono REAL,
  code TEXT, source_id TEXT, title TEXT, body TEXT, body_status TEXT DEFAULT 'PENDING',
  ai_decision TEXT, ai_conf REAL, ai_model TEXT, ai_prompt_ver TEXT, ai_latency_ms REAL);
CREATE TABLE IF NOT EXISTS event (event_id INTEGER PRIMARY KEY, realkey TEXT, code TEXT, t0_mono REAL,
  base_price INTEGER, baseline_amount_1m REAL, sub_end_mono REAL, vi_flag INTEGER DEFAULT 0, prev_event_id INTEGER,
  pre_price INTEGER, pre60_price INTEGER, market TEXT,
  t0_wall TEXT, baseline_vol_1m REAL, pre_spike_mult REAL, ai_decision TEXT);
CREATE TABLE IF NOT EXISTS tick (id INTEGER PRIMARY KEY, event_id INTEGER, code TEXT, exch_time TEXT, sim_t REAL,
  recv_mono REAL, price INTEGER, qty INTEGER, side TEXT, venue TEXT);
CREATE TABLE IF NOT EXISTS signal_log (id INTEGER PRIMARY KEY, event_id INTEGER, sim_t REAL, amount_1m REAL,
  baseline REAL, buy_ratio REAL, price INTEGER, decision TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS orders (order_id INTEGER PRIMARY KEY, event_id INTEGER, side TEXT, qty INTEGER,
  state TEXT, sent_mono REAL, ls_order_no TEXT, fill_price INTEGER, counterfactual INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS position (event_id INTEGER PRIMARY KEY, code TEXT, entry_price INTEGER, entry_t REAL,
  exit_price INTEGER, exit_t REAL, exit_reason TEXT, qty INTEGER, pnl_raw REAL, pnl_after_cost REAL,
  counterfactual INTEGER DEFAULT 0, entry_reason TEXT);
CREATE TABLE IF NOT EXISTS daily_risk (date TEXT PRIMARY KEY, realized_pnl REAL DEFAULT 0, trade_count INTEGER DEFAULT 0, halted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY, event_id INTEGER, kind TEXT, recv_wall TEXT, recv_mono REAL,
  price INTEGER, offerho1 INTEGER, bidho1 INTEGER, offerrem1 INTEGER, bidrem1 INTEGER, volume INTEGER, payload TEXT);
CREATE TABLE IF NOT EXISTS session_log (id INTEGER PRIMARY KEY, wall TEXT, level TEXT, msg TEXT);
"""

# 기존 DB에 컬럼을 덧붙이기 위한 마이그레이션 (없으면 추가, 있으면 무시)
MIGRATIONS = [
    ("news", "ai_decision", "TEXT"), ("news", "ai_conf", "REAL"), ("news", "ai_model", "TEXT"),
    ("news", "ai_prompt_ver", "TEXT"), ("news", "ai_latency_ms", "REAL"),
    ("event", "pre_price", "INTEGER"), ("event", "pre60_price", "INTEGER"), ("event", "market", "TEXT"),
    ("event", "t0_wall", "TEXT"), ("event", "baseline_vol_1m", "REAL"), ("event", "pre_spike_mult", "REAL"),
    ("event", "ai_decision", "TEXT"),
    ("orders", "counterfactual", "INTEGER DEFAULT 0"),
    ("position", "counterfactual", "INTEGER DEFAULT 0"), ("position", "entry_reason", "TEXT"),
    ("tick", "venue", "TEXT"),
]

class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")   # collector가 쓰는 동안 status.py가 읽을 수 있게
        self.conn.executescript(DDL)
        for table, col, typ in MIGRATIONS:
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        self.conn.commit()

    def now_wall(self):
        return datetime.now().isoformat(timespec="milliseconds")

    def raw(self, tr_cd, payload):
        self.conn.execute("INSERT INTO raw(recv_mono,recv_wall,tr_cd,payload) VALUES(?,?,?,?)",
                          (time.monotonic(), self.now_wall(), tr_cd, json.dumps(payload, ensure_ascii=False)))

    def news(self, n):
        self.conn.execute("INSERT OR IGNORE INTO news(realkey,ls_datetime,recv_wall,recv_mono,code,source_id,title) VALUES(?,?,?,?,?,?,?)",
                          (n.realkey, n.ls_datetime, n.recv_wall, n.recv_mono, n.code, n.source_id, n.title))

    def news_update(self, realkey, **cols):
        sets = ",".join(f"{k}=?" for k in cols)
        self.conn.execute(f"UPDATE news SET {sets} WHERE realkey=?", (*cols.values(), realkey))

    def next_order_id(self):
        """주문 번호를 DB에서 이어 매긴다. 1부터 다시 시작하면 INSERT OR REPLACE 가
        지난 실행의 주문 행을 덮어써 기록이 사라진다."""
        return self.conn.execute("SELECT COALESCE(MAX(order_id),0)+1 FROM orders").fetchone()[0]

    def next_event_id(self):
        """이어 쓰는 DB(실수집)에서 event_id가 1부터 다시 시작해 충돌하지 않도록 마지막 id 다음을 준다."""
        return self.conn.execute("SELECT COALESCE(MAX(event_id),0)+1 FROM event").fetchone()[0]

    def event(self, e):
        self.conn.execute("INSERT INTO event(event_id,realkey,code,t0_mono,base_price,baseline_amount_1m,sub_end_mono,market,t0_wall) VALUES(?,?,?,?,?,?,?,?,?)",
                          (e.event_id, e.realkey, e.code, e.t0_mono, e.base_price, e.baseline_amount_1m, e.sub_end_mono,
                           e.market, e.t0_wall))

    def event_update(self, event_id, **cols):
        sets = ",".join(f"{k}=?" for k in cols)
        self.conn.execute(f"UPDATE event SET {sets} WHERE event_id=?", (*cols.values(), event_id))

    def tick(self, event_id, t):
        self.conn.execute("INSERT INTO tick(event_id,code,exch_time,sim_t,recv_mono,price,qty,side,venue) VALUES(?,?,?,?,?,?,?,?,?)",
                          (event_id, t.code, t.exch_time, t.sim_t, t.recv_mono, t.price, t.qty, t.side, t.venue))

    def snapshot(self, event_id, kind, recv_mono, price, offerho1, bidho1, offerrem1, bidrem1, volume, payload):
        self.conn.execute("INSERT INTO snapshot(event_id,kind,recv_wall,recv_mono,price,offerho1,bidho1,offerrem1,bidrem1,volume,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (event_id, kind, self.now_wall(), recv_mono, price, offerho1, bidho1, offerrem1, bidrem1, volume,
                           json.dumps(payload, ensure_ascii=False)))

    def signal(self, event_id, sim_t, amount_1m, baseline, buy_ratio, price, decision, reason):
        self.conn.execute("INSERT INTO signal_log(event_id,sim_t,amount_1m,baseline,buy_ratio,price,decision,reason) VALUES(?,?,?,?,?,?,?,?)",
                          (event_id, sim_t, amount_1m, baseline, buy_ratio, price, decision, reason))

    def order(self, o):
        self.conn.execute("INSERT OR REPLACE INTO orders(order_id,event_id,side,qty,state,sent_mono,ls_order_no,fill_price,counterfactual) VALUES(?,?,?,?,?,?,?,?,?)",
                          (o.order_id, o.event_id, o.side, o.qty, o.state, o.sent_mono, o.ls_order_no, o.fill_price,
                           int(o.counterfactual)))

    def position(self, **p):
        cols = ",".join(p.keys()); qs = ",".join("?" * len(p))
        self.conn.execute(f"INSERT OR REPLACE INTO position({cols}) VALUES({qs})", tuple(p.values()))

    def log(self, level, msg):
        self.conn.execute("INSERT INTO session_log(wall,level,msg) VALUES(?,?,?)", (self.now_wall(), level, msg))

    def commit(self):
        self.conn.commit()
