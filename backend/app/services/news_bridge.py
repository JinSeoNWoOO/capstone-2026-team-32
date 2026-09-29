"""주문 브리지. 수집기의 진입·청산 결정을 KIS 주문으로 옮긴다.

수집기(별도 프로세스)는 규칙과 AI 판정으로 BUY/SELL 을 결정해 newsgap.db 의 orders 테이블에
남긴다. 이 브리지는 그 행을 순번대로 읽어 kis_order.place_order 를 부른다. 주문 코드와
안전 모드(dry-run/paper, real 차단)는 기존 경로 하나만 쓴다.

**재시작 복구**: 백엔드는 개발 중 코드 저장마다 재시작된다. 그 사이에 나온 결정을 놓치면
5분짜리로 설계한 보유가 그대로 하룻밤이 된다 — 급증 중에 진입하는 전략이라 최악의 경우다.
그래서 처리한 마지막 주문 번호를 파일에 남기고, 시작할 때 밀린 것부터 따라잡는다.
따라잡기는 청산 먼저 한다(늦은 매도가 늦은 매수보다 위험하다).

포지션을 오래 들고 있을 일은 없으므로 포트폴리오 관리는 하지 않는다. 필요한 건
"놓친 결정 따라잡기"와 "주인 없는 보유분 정리" 둘뿐이다.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.app.services.kis_fill import get_order_fill
from backend.app.services.kis_order import KisOrderError, KisOrderValidationError, place_order
from backend.app.services.kis_read import KisReadError

SEOUL = timezone(timedelta(hours=9), name="Asia/Seoul")

BRIDGE_DDL = """
CREATE TABLE IF NOT EXISTS bridge_order(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  collector_order_id INTEGER UNIQUE,      -- 멱등성 키. 같은 결정을 두 번 주문하지 않는다.
  event_id INTEGER, code TEXT, side TEXT, qty INTEGER,
  mode TEXT, submitted INTEGER, order_no TEXT, status TEXT, message TEXT, created_at TEXT,
  filled_qty INTEGER, avg_price REAL, fill_status TEXT);
CREATE TABLE IF NOT EXISTS bridge_position(
  event_id INTEGER PRIMARY KEY, code TEXT, qty INTEGER, opened_at TEXT);
CREATE TABLE IF NOT EXISTS bridge_state(key TEXT PRIMARY KEY, value TEXT);
"""

# 이미 만들어진 표에 열을 덧붙인다 (없으면 추가, 있으면 무시).
MIGRATIONS = [("bridge_order", "filled_qty", "INTEGER"), ("bridge_order", "avg_price", "REAL"),
              ("bridge_order", "fill_status", "TEXT"), ("bridge_position", "avg_price", "REAL")]

# 수집기가 남긴 실제(가상 아님) 주문만 본다. counterfactual=1 은 AI 가 거절했거나
# RiskGuard 에 막힌 가상 진입이라 측정용 기록일 뿐이다.
# 지금은 처리할 수 없지만 곧 가능해질 수 있는 경우. 기록하지 않고 다음 주기에 다시 본다.
DEFER = "defer:"

PENDING_SQL = """
SELECT o.order_id, o.event_id, o.side, o.qty, o.fill_price, e.code, n.ai_conf
FROM orders o
JOIN event e ON e.event_id = o.event_id
LEFT JOIN news n ON n.realkey = e.realkey
WHERE COALESCE(o.counterfactual, 0) = 0 AND o.order_id > ?
ORDER BY o.order_id
"""


class NewsBridge:
    """수집기 결정 → KIS 주문. settings.order_enabled 가 켜져 있을 때만 주문한다."""

    def __init__(self, db_path: Path, bridge_db_path: Path, log) -> None:
        self.db_path = db_path
        self.bridge_db_path = bridge_db_path
        self._log = log
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._settings = None
        self._poll_seconds = 1.0
        self._last_error: str | None = None
        self._deferred: dict[int, float] = {}      # 수집기 주문번호 -> 처음 미룬 시각(monotonic)
        self._defer_limit_sec = 60.0               # 이만큼 지나도 조건이 안 되면 포기하고 넘어간다

    # ------------------------------------------------------------------ 저장
    def _bridge_conn(self) -> sqlite3.Connection:
        self.bridge_db_path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.bridge_db_path, timeout=10)
        c.row_factory = sqlite3.Row
        c.executescript(BRIDGE_DDL)
        for table, column, decl in MIGRATIONS:
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        return c

    def _watermark(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM bridge_state WHERE key='last_order_id'").fetchone()
        return int(row["value"]) if row else 0

    def _set_watermark(self, conn: sqlite3.Connection, value: int) -> None:
        conn.execute("INSERT OR REPLACE INTO bridge_state VALUES('last_order_id',?)", (str(value),))

    # ------------------------------------------------------------------ 실행
    def start(self, settings) -> None:
        with self._lock:
            self._settings = settings
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="news-bridge", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=5)
        with self._lock:
            self._thread = None

    def update_settings(self, settings) -> None:
        with self._lock:
            self._settings = settings

    def _run(self) -> None:
        self._log("주문 브리지를 시작했습니다. 밀린 결정을 먼저 따라잡습니다.")
        try:
            self._catch_up()
        except Exception as exc:                     # 따라잡기 실패가 루프를 막지 않게 한다
            self._last_error = str(exc)
            self._log(f"밀린 결정 따라잡기 실패: {exc}", "error")
        while not self._stop.is_set():
            try:
                self._cycle()
                self._last_error = None
            except Exception as exc:
                self._last_error = str(exc)
                self._log(f"주문 브리지 오류: {exc}", "error")
            self._stop.wait(self._poll_seconds)
        self._log("주문 브리지를 중지했습니다.")

    def _catch_up(self) -> None:
        """재시작 사이에 나온 결정을 처리한다. 청산(SELL)을 먼저 한다."""
        rows = self._pending()
        if not rows:
            return
        sells = [r for r in rows if r["side"] == "SELL"]
        buys = [r for r in rows if r["side"] != "SELL"]
        self._log(f"밀린 결정 {len(rows)}건 (청산 {len(sells)}, 진입 {len(buys)}). 청산부터 처리합니다.",
                  "warning")
        for row in sells:
            self._execute(row)
        # 진입은 이미 시간이 지났으면 의미가 없다. 기록만 남기고 주문하지 않는다.
        with closing(self._bridge_conn()) as conn, conn:
            for row in buys:
                self._record(conn, row, mode="skipped", submitted=0, order_no=None,
                             status="stale", message="재시작 중 놓친 진입 — 시점이 지나 주문하지 않음")
            self._set_watermark(conn, max(r["order_id"] for r in rows))

    def _pending(self) -> list[sqlite3.Row]:
        if not self.db_path.exists():
            return []
        with closing(self._bridge_conn()) as bc:
            mark = self._watermark(bc)
        with closing(sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)) as c:
            c.row_factory = sqlite3.Row
            return c.execute(PENDING_SQL, (mark,)).fetchall()

    def _cycle(self) -> None:
        for row in self._pending():
            self._execute(row)
        self._reconcile_fills()

    def _reconcile_fills(self) -> None:
        """접수된 주문의 실제 체결 수량·평균가를 채운다.

        주문 접수 성공은 체결이 아니다. 익절·손절·손익이 모두 진입가에서 나오므로
        추정가가 아니라 증권사가 알려주는 체결 평균가를 쓴다. 조회가 몇 초 늦을 수 있어
        주문 직후 한 번에 끝내지 않고 매 주기 아직 안 채워진 것만 다시 본다."""
        with closing(self._bridge_conn()) as conn:
            pending = conn.execute(
                "SELECT id, collector_order_id, event_id, code, side, qty, order_no "
                "FROM bridge_order WHERE submitted=1 "
                "AND (fill_status IS NULL OR fill_status IN ('pending','partial','not_found')) "
                "ORDER BY id").fetchall()
        for row in pending:
            try:
                fill = get_order_fill(row["order_no"], row["code"])
            except KisReadError as exc:
                self._last_error = str(exc)
                continue
            if not fill["found"]:
                status = "not_found"
            elif fill["filled"]:
                status = "filled"
            elif fill["partial"]:
                status = "partial"
            else:
                status = "pending"
            with closing(self._bridge_conn()) as conn, conn:
                conn.execute("UPDATE bridge_order SET filled_qty=?, avg_price=?, fill_status=? WHERE id=?",
                             (fill["filled_qty"], fill["avg_price"], status, row["id"]))
                if row["side"] == "BUY" and fill["filled_qty"] > 0:
                    # 체결된 수량만 보유로 잡는다. 부분 체결이면 그만큼만 팔아야 한다.
                    conn.execute("UPDATE bridge_position SET qty=?, avg_price=? WHERE event_id=?",
                                 (fill["filled_qty"], fill["avg_price"], row["event_id"]))
                elif row["side"] == "SELL" and status == "filled":
                    conn.execute("DELETE FROM bridge_position WHERE event_id=?", (row["event_id"],))
            if status in ("filled", "partial"):
                self._log(f"{row['code']} {row['side']} 체결 {fill['filled_qty']}/{row['qty']}주 "
                          f"평균 {fill['avg_price']:,.0f}원"
                          + (" (부분 체결)" if status == "partial" else ""))

    # ------------------------------------------------------------------ 주문
    def _execute(self, row: sqlite3.Row) -> None:
        with self._lock:
            settings = self._settings
        with closing(self._bridge_conn()) as conn, conn:
            if conn.execute("SELECT 1 FROM bridge_order WHERE collector_order_id=?",
                            (row["order_id"],)).fetchone():
                self._set_watermark(conn, row["order_id"])
                return

            skip = self._skip_reason(conn, row, settings)
            if skip and skip.startswith(DEFER):
                # 매수 체결을 기다리는 청산 같은 경우다. 무한정 미루면 뒤 결정이 막히므로
                # 제한 시간을 두고, 넘으면 포기하고 기록한 뒤 넘어간다.
                first = self._deferred.setdefault(row["order_id"], time.monotonic())
                if time.monotonic() - first < self._defer_limit_sec:
                    return
                skip = skip[len(DEFER):] + f" ({int(self._defer_limit_sec)}초 대기 후 포기)"
            self._deferred.pop(row["order_id"], None)
            if skip:
                self._record(conn, row, mode="skipped", submitted=0, order_no=None,
                             status="skipped", message=skip)
                self._set_watermark(conn, row["order_id"])
                return

            side = "buy" if row["side"] == "BUY" else "sell"
            qty = self._quantity(conn, row, settings)
            try:
                result = place_order(side, row["code"], qty)
            except (KisOrderError, KisOrderValidationError) as exc:
                self._record(conn, row, mode="error", submitted=0, order_no=None,
                             status="error", message=str(exc), qty=qty)
                self._set_watermark(conn, row["order_id"])
                self._log(f"{row['code']} {side} 주문 실패: {exc}", "error")
                return

            self._record(conn, row, mode=result["mode"], submitted=int(bool(result["submitted"])),
                         order_no=result.get("order_no"), status=result.get("status", "-"),
                         message=result.get("message", ""), qty=qty)
            if side == "buy":
                # dry-run 은 체결 조회가 없으므로 주문 수량을 그대로 잡는다.
                # paper 는 0 으로 두고 _reconcile_fills 가 실제 체결 수량으로 채운다.
                held = qty if not result["submitted"] else 0
                conn.execute(
                    "INSERT OR REPLACE INTO bridge_position(event_id,code,qty,opened_at,avg_price)"
                    " VALUES(?,?,?,?,?)",
                    (row["event_id"], row["code"], held,
                     datetime.now(SEOUL).strftime("%Y-%m-%d %H:%M:%S"),
                     row["fill_price"] if not result["submitted"] else None))
            elif not result["submitted"]:
                conn.execute("DELETE FROM bridge_position WHERE event_id=?", (row["event_id"],))
            self._set_watermark(conn, row["order_id"])
            self._log(f"{row['code']} {side} {qty}주 — {result['mode']} "
                      f"{result.get('order_no') or result.get('status')}")

    def _skip_reason(self, conn, row, settings) -> str | None:
        if settings is None or not settings.order_enabled:
            return "주문 실행이 꺼져 있음"
        if row["side"] == "SELL":
            held = conn.execute("SELECT qty FROM bridge_position WHERE event_id=?",
                                (row["event_id"],)).fetchone()
            if not held:
                return "브리지가 사지 않은 포지션의 청산"
            if int(held["qty"] or 0) <= 0:
                return DEFER + "매수가 아직 체결되지 않음"
            return None
        # 이하 진입에만 적용
        if settings.min_ai_conf > 0 and (row["ai_conf"] or 0) < settings.min_ai_conf:
            return f"AI 확신도 {row['ai_conf']} < 하한 {settings.min_ai_conf}"
        today = datetime.now(SEOUL).strftime("%Y-%m-%d")
        done = conn.execute("SELECT COUNT(*) FROM bridge_order WHERE side='BUY' "
                            "AND mode NOT IN ('skipped','error') AND created_at LIKE ?",
                            (f"{today}%",)).fetchone()[0]
        if done >= settings.max_trades:
            return f"하루 최대 진입 {settings.max_trades}회 도달"
        return None

    def _quantity(self, conn, row, settings) -> int:
        """매도는 브리지가 산 수량 그대로, 매수는 설정 수량."""
        if row["side"] == "SELL":
            held = conn.execute("SELECT qty FROM bridge_position WHERE event_id=?",
                                (row["event_id"],)).fetchone()
            if held:
                return int(held["qty"])
        return int(settings.quantity)

    def _record(self, conn, row, *, mode, submitted, order_no, status, message, qty=None) -> None:
        """qty 는 실제로 주문한 수량. 안 주면 수집기가 계산한 수량을 남긴다(주문 안 한 경우)."""
        conn.execute(
            "INSERT OR REPLACE INTO bridge_order"
            "(collector_order_id,event_id,code,side,qty,mode,submitted,order_no,status,message,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (row["order_id"], row["event_id"], row["code"], row["side"],
             row["qty"] if qty is None else qty, mode,
             submitted, order_no, status, message,
             datetime.now(SEOUL).strftime("%Y-%m-%d %H:%M:%S")))

    # ------------------------------------------------------------------ 조회
    def orphans(self) -> list[dict]:
        """청산되지 않은 브리지 보유분. 백엔드가 죽어 있는 동안 청산을 놓쳤을 때 남는다."""
        with closing(self._bridge_conn()) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM bridge_position ORDER BY opened_at").fetchall()]

    def close_orphans(self) -> list[dict]:
        """주인 없는 보유분을 시장가로 정리한다. 화면에서 눌러야 실행된다."""
        out = []
        for pos in self.orphans():
            try:
                result = place_order("sell", pos["code"], int(pos["qty"]))
                ok, message = True, result.get("message") or result.get("status", "-")
            except (KisOrderError, KisOrderValidationError) as exc:
                ok, message = False, str(exc)
            if ok:
                with closing(self._bridge_conn()) as conn, conn:
                    conn.execute("DELETE FROM bridge_position WHERE event_id=?", (pos["event_id"],))
            out.append({**pos, "closed": ok, "message": message})
            self._log(f"주인 없는 보유분 정리 {pos['code']} {pos['qty']}주: {message}",
                      "info" if ok else "error")
        return out

    def recent(self, limit: int = 30) -> list[dict]:
        with closing(self._bridge_conn()) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM bridge_order ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def status(self) -> dict:
        with self._lock:
            running = bool(self._thread and self._thread.is_alive())
        with closing(self._bridge_conn()) as conn:
            mark = self._watermark(conn)
            today = datetime.now(SEOUL).strftime("%Y-%m-%d")
            submitted = conn.execute(
                "SELECT COUNT(*) FROM bridge_order WHERE mode NOT IN ('skipped','error') "
                "AND created_at LIKE ?", (f"{today}%",)).fetchone()[0]
        return {"running": running, "last_order_id": mark, "attempted_today": submitted,
                "open_positions": len(self.orphans()), "last_error": self._last_error}
