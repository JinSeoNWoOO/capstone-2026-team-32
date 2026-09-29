"""뉴스 기반 자동매매 모드.

기존 자동매매(auto_trading)는 KIS 시세를 폴링해 한 종목을 감시하지만, 이 모드는 LS증권
실시간 뉴스(NWS) 웹소켓 푸시를 받아 **뉴스가 오는 종목마다** 이벤트를 만들고 판단한다.
KIS 에는 실시간 뉴스 푸시가 없어 뉴스·체결 수신은 LS 로만 가능하고, 주문은 기존 KIS 경로
(dry-run/paper, real 은 코드 차단)를 그대로 쓴다.

구조: 수집기는 **별도 프로세스**다.
    uvicorn --reload 가 코드 저장마다 서버를 재시작하는데, 수집기가 서버 안에 있으면 그때마다
    웹소켓이 끊겨 장중 뉴스를 놓친다. 그래서 백엔드는 수집기를 자식 프로세스로 띄우고
    PID 로 살아있는지만 보며, 데이터는 수집기가 쓴 SQLite 를 읽는다.

    [LS WS] → 수집기 프로세스 → data/newsgap.db → (읽기) 백엔드 API → 프런트
                                       ↓ 진입 신호
                                  주문 브리지 → KIS 주문 (기본 꺼짐)
"""
from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.app.services.news_bridge import NewsBridge

SEOUL = timezone(timedelta(hours=9), name="Asia/Seoul")
ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data"
DB_PATH = DATA / "newsgap.db"
PID_PATH = DATA / "news_collector.pid"
LOG_PATH = DATA / "news_collector.log"
BRIDGE_DB_PATH = DATA / "news_bridge.db"
CONFIG_PATH = ROOT / "backend" / "newsgap.config.yaml"

START_TIMEOUT_SEC = 20          # 수집기가 토큰 발급·접속까지 가는 데 주는 시간
STOP_TIMEOUT_SEC = 15


class NewsTradingError(RuntimeError):
    pass


@dataclass(frozen=True)
class NewsSettings:
    """주문 브리지 설정. 진입·청산 규칙 자체는 수집기의 config(newsgap.config.yaml)에 있다."""
    order_enabled: bool = False     # 켜야 KIS 주문이 나간다. 꺼져 있으면 신호만 본다(shadow).
    quantity: int = 1
    max_trades: int = 3             # 하루 최대 진입 횟수
    min_ai_conf: float = 0.0        # AI 확신도 하한 (0이면 수집기 판정을 그대로 따른다)

    def validate(self) -> None:
        if not 1 <= self.quantity <= 1000:
            raise NewsTradingError("주문 수량은 1~1000주여야 합니다.")
        if not 1 <= self.max_trades <= 50:
            raise NewsTradingError("하루 최대 진입 횟수는 1~50회여야 합니다.")
        if not 0.0 <= self.min_ai_conf <= 1.0:
            raise NewsTradingError("AI 확신도 하한은 0.0~1.0이어야 합니다.")


def _today() -> str:
    return datetime.now(SEOUL).strftime("%Y-%m-%d")


def _alive(pid: int) -> bool:
    """살아 있는가. 좀비(Z)는 죽은 것으로 본다.

    수집기는 백엔드의 자식 프로세스라, 죽어도 부모가 거둬가기 전까지 좀비로 남는다.
    좀비에게도 os.kill(pid, 0) 은 성공하므로 그것만 보면 영원히 '가동 중'으로 보인다."""
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    try:
        state = (Path("/proc") / str(pid) / "stat").read_text().rsplit(") ", 1)[1][0]
    except (OSError, IndexError):
        return True                                  # /proc 이 없는 환경(맥·윈도우)에서는 kill 결과를 믿는다
    return state != "Z"


def _read_pid() -> int | None:
    try:
        pid = int(PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


class NewsTradingEngine:
    """수집기 프로세스 감독 + 수집 결과 조회. 주문 브리지는 settings.order_enabled 로 켠다."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._settings = NewsSettings()
        self._proc: subprocess.Popen | None = None
        self._logs: deque[dict] = deque(maxlen=100)
        self._trades_today = 0
        self._bridge = NewsBridge(DB_PATH, BRIDGE_DB_PATH, self._log)
        # 브리지는 항상 돈다. 주문 여부는 settings.order_enabled 가 가른다 — 꺼져 있어도
        # 결정을 읽고 "주문 안 함"으로 기록해 둬야 재시작 후 옛 결정을 뒤늦게 주문하지 않는다.
        self._bridge.start(self._settings)

    # ---------------------------------------------------------------- 로그
    def _log(self, message: str, level: str = "info") -> None:
        with self._lock:
            self._logs.appendleft({
                "time": datetime.now(SEOUL).strftime("%H:%M:%S"),
                "level": level,
                "message": message,
            })

    # ------------------------------------------------------- 수집기 프로세스
    def _spawn(self, mock: bool = False) -> subprocess.Popen:
        DATA.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "backend.newsgap.main", "--mode", "live",
               "--config", str(CONFIG_PATH.relative_to(ROOT))]
        if mock:                                     # 키 없이 목업 웹소켓에 붙는다 (시연·점검용)
            cmd.append("--mock")
        log = open(LOG_PATH, "ab", buffering=0)
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        PID_PATH.write_text(str(proc.pid))
        return proc

    def start_collector(self, mock: bool = False) -> dict:
        if _read_pid():
            raise NewsTradingError("수집기가 이미 실행 중입니다.")
        if not CONFIG_PATH.exists():
            raise NewsTradingError(f"수집기 설정이 없습니다: {CONFIG_PATH}")
        proc = self._spawn(mock)
        deadline = time.monotonic() + START_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                tail = self._log_tail(8)
                PID_PATH.unlink(missing_ok=True)
                raise NewsTradingError(f"수집기가 즉시 종료됐습니다 (코드 {proc.returncode}). 로그: {tail}")
            if self._db_ready():
                break
            time.sleep(0.5)
        with self._lock:
            self._proc = proc
        self._log(f"수집기를 시작했습니다 (pid {proc.pid}{', 목업' if mock else ''}).")
        return self.status()

    def stop_collector(self) -> dict:
        pid = _read_pid()
        if not pid:
            PID_PATH.unlink(missing_ok=True)
            self._log("수집기가 실행 중이 아닙니다.", "warning")
            return self.status()
        os.kill(pid, signal.SIGTERM)                    # 수집기는 SIGTERM 에 구독 해제 후 commit 한다
        deadline = time.monotonic() + STOP_TIMEOUT_SEC
        while time.monotonic() < deadline and _alive(pid):
            time.sleep(0.3)
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)
            self._log("수집기가 응답하지 않아 강제 종료했습니다.", "warning")
        PID_PATH.unlink(missing_ok=True)
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None:
            proc.poll()                              # 좀비로 남지 않게 거둔다
        self._log("수집기를 중지했습니다.")
        return self.status()

    def _reap(self) -> None:
        """죽은 자식을 거둔다. 안 하면 좀비가 쌓이고 PID 가 재사용될 때 오판한다."""
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is not None:
            with self._lock:
                self._proc = None
            PID_PATH.unlink(missing_ok=True)
            self._log(f"수집기가 종료됐습니다 (코드 {proc.returncode}).",
                      "info" if proc.returncode in (0, -signal.SIGTERM) else "error")

    def _db_ready(self) -> bool:
        return DB_PATH.exists()

    def _log_tail(self, lines: int) -> str:
        try:
            return " / ".join(LOG_PATH.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return "(로그 없음)"

    # ------------------------------------------------------------ DB 조회
    def _conn(self) -> sqlite3.Connection:
        if not DB_PATH.exists():
            raise NewsTradingError("수집 데이터베이스가 아직 없습니다. 수집기를 먼저 시작하세요.")
        c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        return c

    def counts(self, day: str | None = None) -> dict:
        """오늘 수신·판정 건수. 발표용 가동 상태 그대로."""
        day = day or _today()
        like = f"{day}%"
        try:
            c = self._conn()
        except NewsTradingError:
            return {"date": day, "news": 0, "news_with_code": 0, "events": 0,
                    "ai": {}, "signals": {}, "ready": False}
        with c:
            one = lambda sql, *a: c.execute(sql, a).fetchone()[0]
            news = one("SELECT COUNT(*) FROM news WHERE recv_wall LIKE ?", like)
            coded = one("SELECT COUNT(*) FROM news WHERE recv_wall LIKE ? AND code != ''", like)
            events = one("SELECT COUNT(*) FROM event WHERE t0_wall LIKE ?", like)
            ai = {(d or "미판정"): n for d, n in c.execute(
                "SELECT ai_decision, COUNT(*) FROM news WHERE recv_wall LIKE ? AND code != '' GROUP BY 1", (like,))}
            sig = {d: n for d, n in c.execute(
                "SELECT s.decision, COUNT(*) FROM signal_log s JOIN event e ON e.event_id=s.event_id "
                "WHERE e.t0_wall LIKE ? GROUP BY 1", (like,))}
        return {"date": day, "news": news, "news_with_code": coded, "events": events,
                "ai": ai, "signals": sig, "ready": True}

    def feed(self, limit: int = 40) -> list[dict]:
        """최근 이벤트 피드. 뉴스 제목 + AI 판정·근거 + 규칙 판단 + 포지션."""
        try:
            c = self._conn()
        except NewsTradingError:
            return []
        with c:
            rows = c.execute("""
                SELECT e.event_id, e.code, e.t0_wall, e.pre_spike_mult, e.baseline_amount_1m, e.vi_flag,
                       n.title, n.source_id, n.ai_decision, n.ai_conf, n.ai_latency_ms,
                       (SELECT decision FROM signal_log s WHERE s.event_id=e.event_id
                          AND s.decision IN ('BUY','SKIP') ORDER BY s.id LIMIT 1) AS signal_decision,
                       (SELECT reason FROM signal_log s WHERE s.event_id=e.event_id
                          AND s.decision IN ('BUY','SKIP') ORDER BY s.id LIMIT 1) AS signal_reason,
                       p.entry_price, p.exit_price, p.exit_reason, p.pnl_after_cost, p.counterfactual
                FROM event e
                JOIN news n ON n.realkey = e.realkey
                LEFT JOIN position p ON p.event_id = e.event_id
                ORDER BY e.event_id DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def positions(self, day: str | None = None) -> list[dict]:
        day = day or _today()
        try:
            c = self._conn()
        except NewsTradingError:
            return []
        with c:
            rows = c.execute("""
                SELECT p.*, e.t0_wall, n.title FROM position p
                JOIN event e ON e.event_id = p.event_id
                JOIN news n ON n.realkey = e.realkey
                WHERE e.t0_wall LIKE ? ORDER BY p.event_id DESC""", (f"{day}%",)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ 설정·상태
    def update_settings(self, settings: NewsSettings) -> dict:
        settings.validate()
        with self._lock:
            self._settings = settings
        self._bridge.update_settings(settings)
        self._log(f"설정을 바꿨습니다. 주문 {'켬' if settings.order_enabled else '끔'}, "
                  f"{settings.quantity}주, 하루 최대 {settings.max_trades}회")
        return self.status()

    def status(self) -> dict:
        self._reap()
        pid = _read_pid()
        with self._lock:
            settings = asdict(self._settings)
            logs = list(self._logs)
            trades = self._trades_today
        return {
            "collector": {
                "running": pid is not None,
                "pid": pid,
                "db_path": str(DB_PATH),
                "log_path": str(LOG_PATH),
                "log_tail": self._log_tail(5) if LOG_PATH.exists() else "",
            },
            "counts": self.counts(),
            "bridge": self._bridge.status(),
            "settings": settings,
            "trades_today": trades,
            "logs": logs,
        }


    # -------------------------------------------------------------- 주문 브리지
    def bridge_orders(self, limit: int = 30) -> list[dict]:
        return self._bridge.recent(limit)

    def bridge_orphans(self) -> list[dict]:
        return self._bridge.orphans()

    def close_orphans(self) -> list[dict]:
        return self._bridge.close_orphans()


news_trading_engine = NewsTradingEngine()
