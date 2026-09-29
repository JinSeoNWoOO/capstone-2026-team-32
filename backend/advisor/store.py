"""data/advisor.db 의 DDL 과 접근 (설계 8장). newsgap/store.py 와 같은 방식이다.

세 가지 규칙이 이 파일의 모양을 정한다.

1. **덧붙이기 위주**. 정정은 기존 행을 고치는 대신 새 행(새 run_id)으로 남긴다. 그래야 "그때 무엇을
   보고 그렇게 판단했는가"가 사후에 복원되고, 채점이 판단을 거슬러 고치는 일이 생기지 않는다.
2. **knowledge_time 은 뒤로 가지 않는다** (설계 2.3). 같은 값을 다시 받아 적을 때 수집 시각을 지금으로
   덮어쓰면 "언제 알 수 있었는가"가 실행할 때마다 늦어져 시점 규칙이 무의미해진다. 값이 실제로
   바뀐 정정일 때만 새 시각을 쓴다. 공시의 first_seen_at 도 같은 이유로 빠른 쪽(MIN)을 지킨다.
3. **읽기 전용 열기**. API 계층은 배치가 도는 중에도 DB를 읽어야 하므로 mode=ro 로 연다 —
   실수로 쓰는 코드가 들어가도 API 가 판단 기록을 건드릴 수 없다.

시각 문자열은 KST 의 naive ISO(예: 2026-09-22T18:30:00.123)다. 오프셋(+09:00)을 붙이지 않는 이유는
newsgap 수집기의 recv_wall 이 같은 형식이라, 공시의 first_seen_at 을 둘 중 빠른 쪽으로 고를 때
문자열 비교가 그대로 성립해야 하기 때문이다 (설계 5.4).
"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

# 실행 상태는 **소문자 네 가지**뿐이다. 스케줄러·API·리포트가 문자열로 비교하므로
# 쓰는 쪽이 대소문자를 섞으면 "오늘 최종 배치가 끝났는가" 같은 판정이 조용히 빗나간다.
# run.py 의 STATUS_* 상수가 같은 값을 다시 노출한다 (배치는 그쪽을 쓴다).
STATUS_RUNNING, STATUS_OK, STATUS_ERROR, STATUS_SKIPPED = "running", "ok", "error", "skipped"
RUN_STATUSES = (STATUS_RUNNING, STATUS_OK, STATUS_ERROR, STATUS_SKIPPED)

DDL = """
-- 실행과 설정
CREATE TABLE IF NOT EXISTS config_version(config_hash TEXT PRIMARY KEY, created_at TEXT, yaml_text TEXT);
CREATE TABLE IF NOT EXISTS run(run_id INTEGER PRIMARY KEY, stage TEXT, mode TEXT, as_of TEXT, decision_time TEXT,
  started_at TEXT, finished_at TEXT, status TEXT, config_hash TEXT, git_sha TEXT,
  llm_used INTEGER DEFAULT 0, fallback_used INTEGER DEFAULT 0, note TEXT);

-- 입력 (원본)
CREATE TABLE IF NOT EXISTS universe(as_of_date TEXT, code TEXT, name TEXT, kind TEXT, sector TEXT,
  PRIMARY KEY(as_of_date, code));
CREATE TABLE IF NOT EXISTS price_daily(code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
  volume REAL, value REAL, adj_close REAL, knowledge_time TEXT, PRIMARY KEY(code, date));
CREATE TABLE IF NOT EXISTS flow_daily(code TEXT, date TEXT, foreign_net REAL, inst_net REAL, mktcap REAL,
  knowledge_time TEXT, PRIMARY KEY(code, date));
CREATE TABLE IF NOT EXISTS market_daily(series TEXT, date TEXT, value REAL, knowledge_time TEXT,
  PRIMARY KEY(series, date));
CREATE TABLE IF NOT EXISTS disclosure(rcept_no TEXT PRIMARY KEY, stock_code TEXT, corp_name TEXT, report_nm TEXT,
  rcept_dt TEXT, first_seen_at TEXT, first_seen_src TEXT, ls_realkey TEXT, kind TEXT, ratio REAL,
  ratio_ok INTEGER, body_src TEXT);

-- 정기보고서 재무 (2026-09-28 추가, sources/dart_fin.py). 기존 표는 건드리지 않고 덧붙이기만 한다.
-- fin_quarterly 는 보고서가 준 값 그대로(1~3분기 3개월 값·누적, 사업보고서 연간)이고 분기 단독값 계산은
-- factors/fin.py 가 읽을 때 한다. knowledge_time 은 수집 시각이 아니라 공시일 다음 날 00:00 이다.
-- 접수번호가 키에 들어가 정정본은 새 행으로 쌓인다 (읽는 쪽이 as_of 이전 마지막 접수분을 고른다).
CREATE TABLE IF NOT EXISTS fin_quarterly(stock_code TEXT, corp_code TEXT, bsns_year INTEGER, reprt_code TEXT,
  fs_div TEXT, account TEXT, amount REAL, cum_amount REAL, rcept_no TEXT, rcept_dt TEXT, rcept_dt_src TEXT,
  knowledge_time TEXT, fetched_at TEXT,
  PRIMARY KEY(stock_code, bsns_year, reprt_code, fs_div, account, rcept_no));
CREATE TABLE IF NOT EXISTS fin_filing(rcept_no TEXT PRIMARY KEY, stock_code TEXT, corp_code TEXT, report_nm TEXT,
  rcept_dt TEXT);
CREATE TABLE IF NOT EXISTS fin_fetch_log(corp_code TEXT, bsns_year INTEGER, reprt_code TEXT, last_checked TEXT,
  found INTEGER, PRIMARY KEY(corp_code, bsns_year, reprt_code));
CREATE TABLE IF NOT EXISTS dart_corp(stock_code TEXT PRIMARY KEY, corp_code TEXT, corp_name TEXT, modify_date TEXT,
  fetched_at TEXT);

-- 점수 (핵심)
CREATE TABLE IF NOT EXISTS factor_value(run_id INTEGER, entity TEXT, factor_id TEXT, raw_value REAL, score REAL,
  missing INTEGER, PRIMARY KEY(run_id, entity, factor_id));
CREATE TABLE IF NOT EXISTS factor_evidence(run_id INTEGER, entity TEXT, factor_id TEXT, ref_type TEXT,
  ref_id TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS risk_flag(run_id INTEGER, entity TEXT, flag_type TEXT, src TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS composite(run_id INTEGER, variant TEXT, entity TEXT, layer TEXT, base_score REAL,
  adj REAL, final_score REAL, vetoed INTEGER, adopted_json TEXT, rejected_json TEXT, reason TEXT, call_id TEXT,
  PRIMARY KEY(run_id, variant, entity));

-- 판단과 포트폴리오
CREATE TABLE IF NOT EXISTS decision(run_id INTEGER, variant TEXT, market_score REAL, risk_weight REAL,
  record_hash TEXT, prev_hash TEXT, PRIMARY KEY(run_id, variant));
CREATE TABLE IF NOT EXISTS target_weight(run_id INTEGER, variant TEXT, asset TEXT, role TEXT, weight REAL,
  PRIMARY KEY(run_id, variant, asset));
CREATE TABLE IF NOT EXISTS trade(portfolio_id TEXT, date TEXT, asset TEXT, side TEXT, weight_delta REAL,
  price REAL, cost REAL, src_run_id INTEGER, status TEXT);
CREATE TABLE IF NOT EXISTS holding(portfolio_id TEXT, date TEXT, asset TEXT, weight REAL,
  PRIMARY KEY(portfolio_id, date, asset));
CREATE TABLE IF NOT EXISTS nav(portfolio_id TEXT, date TEXT, nav REAL, turnover REAL, cost REAL,
  PRIMARY KEY(portfolio_id, date));

-- 채점
CREATE TABLE IF NOT EXISTS outcome(run_id INTEGER, variant TEXT, entity TEXT, factor_id TEXT, horizon INTEGER,
  start_date TEXT, end_date TEXT, asset_ret REAL, bench_ret REAL, excess_ret REAL, eval_status TEXT,
  unable_reason TEXT, computed_at TEXT, PRIMARY KEY(run_id, variant, entity, factor_id, horizon));
CREATE TABLE IF NOT EXISTS factor_metric(computed_at TEXT, stage TEXT, variant TEXT, factor_id TEXT,
  horizon INTEGER, n_days INTEGER, n_eff REAL, rank_ic_mean REAL, rank_ic_std REAL, hit_rate REAL,
  bucket_json TEXT);

-- LLM
CREATE TABLE IF NOT EXISTS llm_call(call_id TEXT PRIMARY KEY, run_id INTEGER, task TEXT, model TEXT,
  prompt_ver TEXT, input_hash TEXT, input_text TEXT, output_text TEXT, parsed_json TEXT, status TEXT,
  tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL, latency_ms REAL, cache_hit INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS model_registry(model TEXT PRIMARY KEY, training_cutoff TEXT, source_url TEXT,
  noted_at TEXT);

-- 색인. 채점·지표 집계는 (요인, 기간)으로, 리포트는 날짜·실행으로 훑는다.
CREATE INDEX IF NOT EXISTS ix_run_stage ON run(stage, as_of);
CREATE INDEX IF NOT EXISTS ix_price_date ON price_daily(date);
CREATE INDEX IF NOT EXISTS ix_flow_date ON flow_daily(date);
CREATE INDEX IF NOT EXISTS ix_market_date ON market_daily(date);
CREATE INDEX IF NOT EXISTS ix_disclosure_code ON disclosure(stock_code, rcept_dt);
CREATE INDEX IF NOT EXISTS ix_factor_value_factor ON factor_value(factor_id);
CREATE INDEX IF NOT EXISTS ix_factor_evidence ON factor_evidence(run_id, entity, factor_id);
CREATE INDEX IF NOT EXISTS ix_risk_flag ON risk_flag(run_id, entity);
CREATE INDEX IF NOT EXISTS ix_outcome_factor ON outcome(factor_id, horizon);
CREATE INDEX IF NOT EXISTS ix_factor_metric ON factor_metric(factor_id, horizon, stage, variant);
CREATE INDEX IF NOT EXISTS ix_trade_pf ON trade(portfolio_id, date);
CREATE INDEX IF NOT EXISTS ix_llm_call_hash ON llm_call(input_hash);
CREATE INDEX IF NOT EXISTS ix_llm_call_created ON llm_call(created_at);
CREATE INDEX IF NOT EXISTS ix_fin_known ON fin_quarterly(knowledge_time);
"""

# 기존 DB에 컬럼을 덧붙이기 위한 마이그레이션 (없으면 추가, 있으면 무시).
# DB를 지우지 않고 이어 쓰므로, 열을 늘릴 때는 DDL 과 여기에 함께 적는다.
MIGRATIONS = [
    # ("table", "column", "TYPE"),
]

# 대량 upsert 가 쓰는 열 목록. (테이블, 키 열, 값 열) — 값이 그대로면 knowledge_time 을 유지한다.
_PRICE_COLS = ("open", "high", "low", "close", "volume", "value", "adj_close")
_FLOW_COLS = ("foreign_net", "inst_net", "mktcap")
_MARKET_COLS = ("value",)


def _row(rec, keys):
    """dict 든 순서 있는 시퀀스든 같은 순서의 튜플로 만든다 (수집기마다 만들기 쉬운 쪽을 쓰게)."""
    if isinstance(rec, dict):
        return tuple(rec.get(k) for k in keys)
    rec = tuple(rec)
    if len(rec) != len(keys):
        raise ValueError(f"행의 길이가 {len(keys)}이 아닙니다: {rec!r} (기대 열 {keys})")
    return rec


class Store:
    """advisor.db 접근. readonly=True 면 mode=ro 로 열고 DDL·마이그레이션을 건드리지 않는다."""

    def __init__(self, path, readonly=False):
        self.path = str(path)
        self.readonly = bool(readonly)
        if self.readonly:
            self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        else:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            self.conn = sqlite3.connect(self.path)
            # 배치가 쓰는 동안 API 가 읽을 수 있게 (newsgap 과 같은 이유)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(DDL)
            for table, col, typ in MIGRATIONS:
                try:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass
            self.conn.commit()
        self.conn.row_factory = sqlite3.Row

    # ---------------------------------------------------------------- 공통

    def now_wall(self):
        """KST 벽시계 시각. newsgap 의 recv_wall 과 같은 형식이라 서로 비교할 수 있다."""
        return datetime.now(KST).replace(tzinfo=None).isoformat(timespec="milliseconds")

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if not self.readonly:
            self.conn.commit()
        self.conn.close()
        return False

    # ---------------------------------------------------------------- 실행과 설정

    def register_config(self, config_hash, yaml_text):
        """설정 버전을 기록한다. 같은 해시를 다시 넣어도 최초 기록을 지키려고 OR IGNORE."""
        self.conn.execute("INSERT OR IGNORE INTO config_version(config_hash,created_at,yaml_text) VALUES(?,?,?)",
                          (config_hash, self.now_wall(), yaml_text))
        return config_hash

    def start_run(self, stage, as_of, mode="live", decision_time=None, config_hash=None,
                  git_sha=None, note=None, status=STATUS_RUNNING):
        """실행 기록을 열고 run_id 를 준다. 판단 시각(decision_time)은 시점 규칙의 기준이다 (설계 2.3)."""
        cur = self.conn.execute(
            "INSERT INTO run(stage,mode,as_of,decision_time,started_at,status,config_hash,git_sha,note) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (stage, mode, str(as_of), decision_time or self.now_wall(), self.now_wall(), status,
             config_hash, git_sha, note))
        return int(cur.lastrowid)

    def finish_run(self, run_id, status=STATUS_OK, llm_used=None, fallback_used=None, note=None):
        """실행을 닫는다. None 으로 넘긴 열은 건드리지 않는다 (중간에 적어 둔 값을 지우지 않게)."""
        sets = ["finished_at=?", "status=?"]
        vals = [self.now_wall(), status]
        for col, val in (("llm_used", llm_used), ("fallback_used", fallback_used), ("note", note)):
            if val is not None:
                sets.append(f"{col}=?")
                vals.append(int(val) if col != "note" else val)
        vals.append(run_id)
        self.conn.execute(f"UPDATE run SET {','.join(sets)} WHERE run_id=?", vals)

    def latest_run(self, stage, as_of=None, mode="live"):
        """단계별 마지막 실행 행. as_of 를 주면 그 기준일의 실행만 본다. 없으면 None."""
        sql = "SELECT * FROM run WHERE stage=? AND mode=?"
        params = [stage, mode]
        if as_of is not None:
            sql += " AND as_of=?"
            params.append(str(as_of))
        sql += " ORDER BY as_of DESC, run_id DESC LIMIT 1"
        return self.conn.execute(sql, params).fetchone()

    # ---------------------------------------------------------------- 입력 (대량 upsert)

    def _upsert_timed(self, table, key_cols, val_cols, rows, knowledge_time=None):
        """시점이 붙은 입력 행을 밀어 넣는다.

        값이 하나도 안 바뀌었으면 knowledge_time 을 그대로 둔다 — 같은 일봉을 예비·최종 두 번
        받아 적는다고 해서 "알게 된 시각"이 저녁에서 아침으로 미뤄지면 안 된다 (설계 2.3).
        값이 실제로 바뀐 정정일 때만 새 시각을 쓴다.
        """
        if self.readonly:
            raise sqlite3.OperationalError("읽기 전용으로 연 Store 입니다")
        kt = knowledge_time or self.now_wall()
        cols = tuple(key_cols) + tuple(val_cols) + ("knowledge_time",)
        placeholders = ",".join("?" * len(cols))
        same = " AND ".join(f"{table}.{c} IS excluded.{c}" for c in val_cols)
        sets = ",".join(f"{c}=excluded.{c}" for c in val_cols)
        sql = (f"INSERT INTO {table}({','.join(cols)}) VALUES({placeholders}) "
               f"ON CONFLICT({','.join(key_cols)}) DO UPDATE SET {sets}, "
               f"knowledge_time=CASE WHEN {same} "
               f"THEN COALESCE({table}.knowledge_time, excluded.knowledge_time) "
               f"ELSE excluded.knowledge_time END")
        payload = []
        for rec in rows:
            values = _row(rec, cols[:-1])
            rec_kt = rec.get("knowledge_time") if isinstance(rec, dict) else None
            payload.append(values + (rec_kt or kt,))
        if payload:
            self.conn.executemany(sql, payload)
        return len(payload)

    def put_prices(self, rows, knowledge_time=None):
        """일봉. rows 는 {code,date,open,high,low,close,volume,value,adj_close} 또는 같은 순서의 시퀀스."""
        return self._upsert_timed("price_daily", ("code", "date"), _PRICE_COLS, rows, knowledge_time)

    def put_flows(self, rows, knowledge_time=None):
        """투자자별 순매수와 시가총액. rows 는 {code,date,foreign_net,inst_net,mktcap}."""
        return self._upsert_timed("flow_daily", ("code", "date"), _FLOW_COLS, rows, knowledge_time)

    def put_market(self, rows, knowledge_time=None):
        """시장 계열 값 (KOSPI, SP500, SOX, USDKRW, 신용잔고…). rows 는 {series,date,value}."""
        return self._upsert_timed("market_daily", ("series", "date"), _MARKET_COLS, rows, knowledge_time)

    def put_universe(self, as_of_date, rows):
        """그날의 대상 목록 스냅샷. rows 는 {code,name,kind,sector} (kind: stock|etf|cash_etf)."""
        payload = [(str(as_of_date),) + _row(r, ("code", "name", "kind", "sector")) for r in rows]
        self.conn.executemany(
            "INSERT OR REPLACE INTO universe(as_of_date,code,name,kind,sector) VALUES(?,?,?,?,?)", payload)
        return len(payload)

    def upsert_disclosure(self, rec):
        """공시 한 건. first_seen_at 은 절대 뒤로 가지 않는다.

        LS 공시 속보의 수신 시각과 DART 폴러의 최초 조회 시각 중 **빠른 쪽**이 그 공시를 처음 알 수
        있었던 시각이다 (설계 5.4). 어느 쪽이 먼저 들어올지 모르므로 여기서 MIN 을 지킨다.
        나머지 열은 COALESCE 로 채우기만 하고 이미 있는 값을 NULL 로 지우지 않는다.
        """
        if self.readonly:
            raise sqlite3.OperationalError("읽기 전용으로 연 Store 입니다")
        cols = ("rcept_no", "stock_code", "corp_name", "report_nm", "rcept_dt", "first_seen_at",
                "first_seen_src", "ls_realkey", "kind", "ratio", "ratio_ok", "body_src")
        values = _row(rec, cols)
        keep = [c for c in cols[1:] if c not in ("first_seen_at", "first_seen_src")]
        sets = ",".join(f"{c}=COALESCE(excluded.{c}, disclosure.{c})" for c in keep)
        earlier = ("excluded.first_seen_at IS NOT NULL AND (disclosure.first_seen_at IS NULL "
                   "OR excluded.first_seen_at < disclosure.first_seen_at)")
        self.conn.execute(
            f"INSERT INTO disclosure({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
            f"ON CONFLICT(rcept_no) DO UPDATE SET {sets}, "
            f"first_seen_at=CASE WHEN {earlier} THEN excluded.first_seen_at ELSE disclosure.first_seen_at END, "
            f"first_seen_src=CASE WHEN {earlier} THEN excluded.first_seen_src ELSE disclosure.first_seen_src END",
            values)
        return values[0]

    # ---------------------------------------------------------------- 정기보고서 재무 (dart_fin.py)

    def put_dart_corps(self, mapping, fetched_at=None):
        """{종목 코드: (고유번호, 회사명, 변경일)} → dart_corp. 매핑은 바뀔 수 있어 덮어쓴다."""
        at = fetched_at or self.now_wall()
        payload = [(code, rec[0], rec[1], rec[2], at) for code, rec in (mapping or {}).items()]
        self.conn.executemany(
            "INSERT OR REPLACE INTO dart_corp(stock_code,corp_code,corp_name,modify_date,fetched_at) "
            "VALUES(?,?,?,?,?)", payload)
        return len(payload)

    def dart_corp_map(self):
        """{종목 코드: 고유번호}."""
        return {r[0]: r[1] for r in self.conn.execute("SELECT stock_code, corp_code FROM dart_corp")}

    def dart_corp_fetched_at(self):
        return self.conn.execute("SELECT MAX(fetched_at) FROM dart_corp").fetchone()[0]

    def put_fin_rows(self, rows):
        """정기보고서 주요계정 행. 같은 접수번호의 같은 값은 다시 넣지 않는다 (OR IGNORE).

        접수번호가 키에 들어 있어 정정본은 새 행이 되고, 원본 행과 그 인지 시각은 그대로 남는다.
        """
        if self.readonly:
            raise sqlite3.OperationalError("읽기 전용으로 연 Store 입니다")
        cols = ("stock_code", "corp_code", "bsns_year", "reprt_code", "fs_div", "account", "amount",
                "cum_amount", "rcept_no", "rcept_dt", "rcept_dt_src", "knowledge_time", "fetched_at")
        payload = [_row(r, cols) for r in rows]
        if payload:
            self.conn.executemany(
                f"INSERT OR IGNORE INTO fin_quarterly({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                payload)
        return len(payload)

    def fin_codes_with(self, bsns_year, reprt_code):
        """이 보고서의 행이 하나라도 있는 종목 코드 집합."""
        return {r[0] for r in self.conn.execute(
            "SELECT DISTINCT stock_code FROM fin_quarterly WHERE bsns_year=? AND reprt_code=?",
            (int(bsns_year), str(reprt_code)))}

    def fin_fetch_log(self, bsns_year, reprt_code):
        """{고유번호: 마지막 조회 시각} — 이 보고서를 언제 마지막으로 물었는가."""
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT corp_code, last_checked FROM fin_fetch_log WHERE bsns_year=? AND reprt_code=?",
            (int(bsns_year), str(reprt_code)))}

    def log_fin_fetch(self, rows):
        """(고유번호, 연도, 보고서, 조회 시각, 찾았나) 목록을 남긴다. 같은 키는 마지막 조회로 덮는다."""
        self.conn.executemany(
            "INSERT OR REPLACE INTO fin_fetch_log(corp_code,bsns_year,reprt_code,last_checked,found) "
            "VALUES(?,?,?,?,?)", [(c, int(y), str(r), t, int(f)) for c, y, r, t, f in rows])
        return len(rows)

    def put_fin_filings(self, rows):
        """정기공시 목록 행 {rcept_no, stock_code, corp_code, report_nm, rcept_dt}."""
        cols = ("rcept_no", "stock_code", "corp_code", "report_nm", "rcept_dt")
        payload = [_row(r, cols) for r in rows or ()]
        if payload:
            self.conn.executemany(
                "INSERT OR REPLACE INTO fin_filing(rcept_no,stock_code,corp_code,report_nm,rcept_dt) "
                "VALUES(?,?,?,?,?)", payload)
        return len(payload)

    def _dates_by_rcept(self, table, rcept_nos):
        out = {}
        nos = [str(n) for n in rcept_nos or ()]
        for i in range(0, len(nos), 500):                 # SQLite 변수 한도를 넘지 않게 쪼갠다
            chunk = nos[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for r in self.conn.execute(
                    f"SELECT rcept_no, rcept_dt FROM {table} WHERE rcept_no IN ({marks}) "
                    "AND rcept_dt IS NOT NULL", chunk):
                out[r[0]] = str(r[1])[:10]
        return out

    def fin_filing_dates(self, rcept_nos):
        """{접수번호: 공시일} — fin_filing 에서."""
        return self._dates_by_rcept("fin_filing", rcept_nos)

    def disclosure_dates(self, rcept_nos):
        """{접수번호: 공시일} — 공시 폴러가 받아 둔 disclosure 에서."""
        return self._dates_by_rcept("disclosure", rcept_nos)

    # ---------------------------------------------------------------- 점수·판단

    def put_factor_values(self, run_id, rows):
        """하위 점수. 가중치 0인 관찰 요인도 전부 저장한다 (결정 6: 사후 재평가의 재료).

        rows 는 {entity,factor_id,raw_value,score,missing} 또는 같은 순서의 시퀀스.
        변환 전 raw_value 를 함께 남겨야 나중에 다른 변환(섹터 안 순위 등)을 사후 적용할 수 있다.
        """
        cols = ("entity", "factor_id", "raw_value", "score", "missing")
        payload = [(run_id,) + _row(r, cols) for r in rows]
        self.conn.executemany(
            "INSERT OR REPLACE INTO factor_value(run_id,entity,factor_id,raw_value,score,missing) "
            "VALUES(?,?,?,?,?,?)",
            [(rid, e, f, rv, sc, int(bool(ms))) for rid, e, f, rv, sc, ms in payload])
        return len(payload)

    def put_factor_evidence(self, run_id, rows):
        """점수의 근거 (공시 접수번호·뉴스 키 등). 리포트의 '왜 이 판단인가'가 여기서 나온다."""
        cols = ("entity", "factor_id", "ref_type", "ref_id", "summary")
        payload = [(run_id,) + _row(r, cols) for r in rows]
        self.conn.executemany(
            "INSERT INTO factor_evidence(run_id,entity,factor_id,ref_type,ref_id,summary) VALUES(?,?,?,?,?,?)",
            payload)
        return len(payload)

    def put_risk_flags(self, run_id, rows):
        """위험 표시. 점수와 별개로 기록하고 halt 만 v0 에서도 후보에서 뺀다 (설계 5.5)."""
        cols = ("entity", "flag_type", "src", "detail")
        payload = [(run_id,) + _row(r, cols) for r in rows]
        self.conn.executemany(
            "INSERT INTO risk_flag(run_id,entity,flag_type,src,detail) VALUES(?,?,?,?,?)", payload)
        return len(payload)

    def put_composites(self, run_id, variant, rows):
        """계층·종합 점수. variant 는 v0|llm — 같은 실행에서 두 판단을 나란히 남긴다 (결정 4)."""
        cols = ("entity", "layer", "base_score", "adj", "final_score", "vetoed",
                "adopted_json", "rejected_json", "reason", "call_id")
        payload = [(run_id, variant) + _row(r, cols) for r in rows]
        self.conn.executemany(
            "INSERT OR REPLACE INTO composite(run_id,variant,entity,layer,base_score,adj,final_score,"
            "vetoed,adopted_json,rejected_json,reason,call_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            [p[:7] + (int(bool(p[7])),) + p[8:] for p in payload])
        return len(payload)

    def put_decision(self, run_id, variant, market_score, risk_weight, record_hash=None, prev_hash=None):
        """판단 한 줄. record_hash 는 판단 JSON + 직전 해시로 만든 사슬이다 (결정 13)."""
        self.conn.execute(
            "INSERT OR REPLACE INTO decision(run_id,variant,market_score,risk_weight,record_hash,prev_hash) "
            "VALUES(?,?,?,?,?,?)", (run_id, variant, market_score, risk_weight, record_hash, prev_hash))

    def put_target_weights(self, run_id, variant, weights):
        """목표 비중. weights 는 allocate.target_weights() 의 반환 그대로 ({asset,role,weight})."""
        cols = ("asset", "role", "weight")
        payload = [(run_id, variant) + _row(w, cols) for w in weights]
        self.conn.executemany(
            "INSERT OR REPLACE INTO target_weight(run_id,variant,asset,role,weight) VALUES(?,?,?,?,?)", payload)
        return len(payload)

    # ---------------------------------------------------------------- 읽기

    def prices(self, code, end_date, n):
        """end_date 까지의 마지막 n 거래일 일봉을 날짜 오름차순으로.

        end_date **이후**를 절대 넘기지 않는 것이 시점 규칙의 1차 방어선이다 (설계 2.3).
        """
        rows = self.conn.execute(
            "SELECT * FROM price_daily WHERE code=? AND date<=? ORDER BY date DESC LIMIT ?",
            (code, str(end_date), int(n))).fetchall()
        return list(reversed(rows))

    def price_panel(self, date):
        """그날 전 종목 일봉 {code: row}. 순위 변환은 같은 날 단면을 통째로 필요로 한다."""
        rows = self.conn.execute("SELECT * FROM price_daily WHERE date=?", (str(date),)).fetchall()
        return {r["code"]: r for r in rows}

    def market_series(self, series, end_date, n=None):
        """시장 계열 값을 날짜 오름차순으로. n 을 주면 end_date 까지의 마지막 n 개만."""
        sql = "SELECT * FROM market_daily WHERE series=? AND date<=? ORDER BY date DESC"
        params = [series, str(end_date)]
        if n is not None:
            sql += " LIMIT ?"
            params.append(int(n))
        return list(reversed(self.conn.execute(sql, params).fetchall()))

    def factor_values(self, run_id, factor_id=None, entity=None):
        """한 실행의 하위 점수 행. factor_value + outcome 조인이 곧 분석용 데이터셋이다 (설계 8장)."""
        sql = "SELECT * FROM factor_value WHERE run_id=?"
        params = [run_id]
        if factor_id is not None:
            sql += " AND factor_id=?"
            params.append(factor_id)
        if entity is not None:
            sql += " AND entity=?"
            params.append(entity)
        return self.conn.execute(sql + " ORDER BY entity, factor_id", params).fetchall()

    def target_weights(self, run_id, variant):
        return self.conn.execute(
            "SELECT * FROM target_weight WHERE run_id=? AND variant=? ORDER BY role, asset",
            (run_id, variant)).fetchall()

    def disclosure(self, rcept_no):
        return self.conn.execute("SELECT * FROM disclosure WHERE rcept_no=?", (rcept_no,)).fetchone()

    def drop_disclosure(self, rcept_no):
        """공시 한 건을 지운다. 반환은 지워진 행 수.

        덧붙이기 위주라는 이 파일의 원칙에서 **유일하게 예외**인 자리다. LS 공시 속보를 DART 행에
        못 맞춰 임시로 만든 합성 접수번호(`LS:<realkey>`)가, 나중에 DART 행이 도착해 맞춰졌을 때
        남아 있으면 같은 공시가 두 줄이 되어 stk_disclosure 점수가 두 번 더해진다 (설계 5.2).
        판단 기록이 아니라 **입력의 중복**을 없애는 것이라 정정이 아니다.
        """
        if self.readonly:
            raise sqlite3.OperationalError("읽기 전용으로 연 Store 입니다")
        return self.conn.execute("DELETE FROM disclosure WHERE rcept_no=?", (rcept_no,)).rowcount
