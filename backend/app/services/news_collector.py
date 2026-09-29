"""LS 실시간 뉴스·공시 속보 수집기 감독 (시작·중지·상태).

판단 지원(advisor)이 공시 속보·뉴스를 data/newsgap.db 에서 읽으므로 수집기는 계속 돌아야 한다.
뉴스 자동매매(주문 브리지·AI 판정·진입 신호)는 효과가 없어 없앴고, 여기에는 수집기 프로세스
관리만 남겼다. advisor 스케줄러가 생존을 보고, 판단 지원 탭의 버튼이 시작·중지를 부른다.

수집기는 **별도 프로세스**다. uvicorn 이 재시작될 때마다 웹소켓이 끊기면 장중 뉴스를 놓친다.
그래서 백엔드는 수집기를 띄우고 PID 파일로 살아 있는지만 보며, 데이터는 수집기가 쓴 SQLite 를 읽는다.

    [LS WS] → 수집기 프로세스 → data/newsgap.db → (읽기 전용) 판단 지원 배치·리포트

이 모듈은 import 만으로는 아무것도 띄우지 않는다 (스레드·프로세스 없음).
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEOUL = timezone(timedelta(hours=9), name="Asia/Seoul")
ROOT = Path(__file__).resolve().parents[3]
CONFIG_REL = Path("backend") / "newsgap.config.yaml"
DISCLOSURE_SOURCE_ID = "15"     # LS 공시 속보. advisor/sources/lsnews.py 와 같은 값

START_TIMEOUT_SEC = 20          # 수집기가 토큰 발급·접속까지 가는 데 주는 시간
STOP_TIMEOUT_SEC = 15


class NewsCollectorError(RuntimeError):
    pass


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


class NewsCollectorService:
    """수집기 프로세스 감독 + 오늘 수신 건수. root 를 바꾸면 PID·로그·DB 가 모두 그 아래로 간다 (테스트용)."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else ROOT
        self.data = self.root / "data"
        self.db_path = self.data / "newsgap.db"
        self.pid_path = self.data / "news_collector.pid"
        self.log_path = self.data / "news_collector.log"
        self.config_path = self.root / CONFIG_REL
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._logs: deque[dict] = deque(maxlen=100)

    # ---------------------------------------------------------------- 로그
    def _log(self, message: str, level: str = "info") -> None:
        with self._lock:
            self._logs.appendleft({
                "time": datetime.now(SEOUL).strftime("%H:%M:%S"),
                "level": level,
                "message": message,
            })

    def _log_tail(self, lines: int) -> str:
        try:
            return " / ".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return "(로그 없음)"

    # ------------------------------------------------------- 수집기 프로세스
    def _read_pid(self) -> int | None:
        try:
            pid = int(self.pid_path.read_text().strip())
        except (OSError, ValueError):
            return None
        return pid if _alive(pid) else None

    def _spawn(self, mock: bool = False) -> subprocess.Popen:
        self.data.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "backend.newsgap.main", "--config", str(CONFIG_REL)]
        if mock:                                     # 키 없이 목업 웹소켓에 붙는다 (시연·점검용)
            cmd.append("--mock")
        with open(self.log_path, "ab", buffering=0) as log:
            # 자식이 파일 기술자를 물려받으므로 부모 쪽은 바로 닫아도 된다
            proc = subprocess.Popen(cmd, cwd=str(self.root), stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        self.pid_path.write_text(str(proc.pid))
        return proc

    def start_collector(self, mock: bool = False) -> dict:
        if self._read_pid():
            raise NewsCollectorError("수집기가 이미 실행 중입니다.")
        if not self.config_path.exists():
            raise NewsCollectorError(f"수집기 설정이 없습니다: {self.config_path}")
        proc = self._spawn(mock)
        deadline = time.monotonic() + START_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                tail = self._log_tail(8)
                self.pid_path.unlink(missing_ok=True)
                raise NewsCollectorError(f"수집기가 즉시 종료됐습니다 (코드 {proc.returncode}). 로그: {tail}")
            if self.db_path.exists():
                break
            time.sleep(0.5)
        with self._lock:
            self._proc = proc
        self._log(f"수집기를 시작했습니다 (pid {proc.pid}{', 목업' if mock else ''}).")
        return self.status()

    def stop_collector(self) -> dict:
        pid = self._read_pid()
        if not pid:
            self.pid_path.unlink(missing_ok=True)
            self._log("수집기가 실행 중이 아닙니다.", "warning")
            return self.status()
        os.kill(pid, signal.SIGTERM)                    # 수집기는 SIGTERM 에 commit 하고 끝난다
        deadline = time.monotonic() + STOP_TIMEOUT_SEC
        while time.monotonic() < deadline and _alive(pid):
            time.sleep(0.3)
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)
            self._log("수집기가 응답하지 않아 강제 종료했습니다.", "warning")
        self.pid_path.unlink(missing_ok=True)
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
            self.pid_path.unlink(missing_ok=True)
            self._log(f"수집기가 종료됐습니다 (코드 {proc.returncode}).",
                      "info" if proc.returncode in (0, -signal.SIGTERM) else "error")

    # ------------------------------------------------------------ 상태
    def counts(self, day: str | None = None) -> dict:
        """오늘 받은 뉴스·공시 속보 건수와 마지막 수신 시각. 수집이 실제로 되고 있는지 보는 숫자다."""
        day = day or _today()
        empty = {"date": day, "news": 0, "news_with_code": 0, "disclosures": 0,
                 "last_recv": None, "ready": False}
        if not self.db_path.exists():
            return empty
        try:
            c = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            return empty
        like = f"{day}%"
        try:
            news, coded, disc, last = c.execute(
                "SELECT COUNT(*), SUM(code != ''), SUM(source_id = ?), MAX(recv_wall) "
                "FROM news WHERE recv_wall LIKE ?", (DISCLOSURE_SOURCE_ID, like)).fetchone()
        except sqlite3.Error:
            return empty
        finally:
            c.close()
        return {"date": day, "news": news or 0, "news_with_code": coded or 0, "disclosures": disc or 0,
                "last_recv": last, "ready": True}

    def status(self) -> dict:
        self._reap()
        pid = self._read_pid()
        with self._lock:
            logs = list(self._logs)
        return {
            "collector": {
                "running": pid is not None,
                "pid": pid,
                "db_path": str(self.db_path),
                "log_path": str(self.log_path),
                "log_tail": self._log_tail(5) if self.log_path.exists() else "",
            },
            "counts": self.counts(),
            "logs": logs,
        }


news_collector = NewsCollectorService()
