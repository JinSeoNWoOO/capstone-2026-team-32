"""data/newsgap.db 의 스키마와 쓰기. 수집기만 쓰고, 판단 지원은 읽기 전용으로 연다.

event·tick·signal_log·orders·position·daily_risk·snapshot 표와 news 의 ai_* 열은 없앤 뉴스 자동매매가
쓰던 것이다. 지우지 않고 그대로 만든다 — 운영 DB 에 이미 쌓인 기록과 스키마가 같아야 옛 DB 를
그대로 이어 쓰고 옛 기록도 읽을 수 있다. 지금 수집기는 raw·news·session_log 만 채운다.
"""
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

# 기존 DB에 컬럼을 덧붙이기 위한 마이그레이션 (없으면 추가, 있으면 무시). 스키마를 옛 DB 와 맞추려고 남긴다
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
        self.conn.execute("PRAGMA journal_mode=WAL")   # collector가 쓰는 동안 판단 지원·status.py가 읽을 수 있게
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

    def log(self, level, msg):
        self.conn.execute("INSERT INTO session_log(wall,level,msg) VALUES(?,?,?)", (self.now_wall(), level, msg))

    def commit(self):
        self.conn.commit()
