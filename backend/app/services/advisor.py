"""판단 지원(advisor) 모드의 백엔드 서비스 — 스케줄러·폴러 스레드, 배치 실행, DB 읽기.

구조는 뉴스 수집기(services/news_collector.py)와 같다 (설계 2.1). **배치는 별도 프로세스**다.

    스레드 advisor-scheduler ─(subprocess)→ python -m backend.advisor.run --stage prelim|final
    스레드 advisor-poller    ─(함수 호출)→ backend.advisor.poller.poll_once  (DART 공시 목록)
    API                      ─(읽기 전용)→ data/advisor.db

왜 배치를 프로세스로 띄우는가: `uvicorn --reload` 가 코드 저장마다 백엔드를 재시작해도 돌고 있는
배치가 죽지 않는다. 같은 명령을 터미널이나 cron 에서 직접 실행해도 결과가 같다.
왜 공시 조회는 스레드인가: 상태 없는 HTTP 호출이라 재시작돼도 잃는 것이 없다 (다음 조회에서 이어짐).

**스레드는 기본으로 뜨지 않는다.** 환경 변수 `ADVISOR_SCHEDULER=1` 일 때만 시작한다.
그래서 테스트나 평소 개발용 실행이 18:30·07:40 을 지나도 배치가 저절로 뜨지 않는다.
상태 엔드포인트가 지금 켜져 있는지 아닌지를 그대로 보여 준다.

환경 변수
    ADVISOR_SCHEDULER=1     스케줄러·폴러 스레드를 켜고, 실시간 뉴스·공시 속보 수집기
                            (LS 웹소켓)도 함께 띄운다. 스케줄러 틱마다 수집기 생존을 확인해
                            죽어 있으면 다시 띄운다 (기본 꺼짐)
    ADVISOR_DB=<경로>       advisor.db 경로를 설정(paths.db)보다 우선해서 쓴다.
                            시연용 fixture DB 로 API 를 띄울 때 쓰고, 이 값이 있으면 배치를
                            띄울 때도 같은 경로를 `--db` 로 넘긴다 (읽는 DB 와 쓰는 DB 가 갈라지지 않게).
    ADVISOR_CONFIG=<경로>   advisor.config.yaml 경로 (기본: backend/advisor.config.yaml)

중복 실행 방지는 두 겹이다. ① PID 파일 — 같은 배치가 둘 돌지 않는다. ② `run` 테이블 —
(단계, 날짜)마다 한 번만 띄운다. ②를 메모리가 아니라 DB 로 보는 이유는 `uvicorn --reload` 가
백엔드를 재시작해도 "오늘 이미 띄웠다"가 남아야 하기 때문이다.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path

from backend.advisor import report as report_mod
from backend.advisor.calendar import TradingCalendar
from backend.advisor.config import ConfigError, load_config, resolve_path
from backend.advisor.store import Store

log = logging.getLogger("advisor.service")

KST = timezone(timedelta(hours=9), name="Asia/Seoul")
ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data"

# advisor.config.yaml 은 다른 담당이 소유한다. 아직 설정에 없는 조정값은 여기 한 곳에 모아 두고
# cfg 에 같은 경로의 키가 생기면 그쪽이 이긴다 (`_tunable`). 키는 설정에 넣을 때 쓸 경로 그대로 적는다.
DEFAULTS = {
    "advisor.tick_sec": 20,                 # 스케줄러가 시계를 보는 주기(초)
    "advisor.collector_retry_sec": 300,     # 실시간 수집기가 죽었을 때 다시 띄우기까지 기다리는 시간(초)
    "advisor.launch_window_min": 30,        # 예정 시각 뒤 이 분 안에만 배치를 띄운다 (늦게 켠 PC 가 한밤중에 배치를 띄우지 않게)
    "advisor.batch_log": "data/advisor_batch.log",
    "advisor.batch_pid": "data/advisor_batch.pid",
    "advisor.batch_stale_sec": 7200,        # 이만큼 지나도 안 끝난 PID 기록은 죽은 것으로 본다
    "advisor.report_top_n": 20,             # 리포트의 종목 표 줄 수 (설계 10.2 의 '종목 상위 20')
    "advisor.diff_top_factors": 5,          # '무엇이 달라졌고 왜'에 보일 요인 수
    "advisor.evidence_limit": 20,           # 자산 상세의 근거 줄 수
    "advisor.log_lines": 60,                # 서비스 로그 보관 줄 수
    "advisor.log_tail_lines": 12,           # 상태에 실어 보내는 배치 로그 꼬리
    "advisor.promote_flag": "--promote-prelim",   # 대체 규칙(결정 11)을 배치에 넘길 때 쓸 인자
}

STAGES = ("prelim", "final")
MODES = ("live", "replay")

ENV_SCHEDULER = "ADVISOR_SCHEDULER"
ENV_DB = "ADVISOR_DB"
ENV_CONFIG = "ADVISOR_CONFIG"


class AdvisorError(RuntimeError):
    """요청이 지금 상태에서 성립하지 않는다 (배치 중복 실행 등). API 는 422 로 바꾼다."""


# ---------------------------------------------------------------- 순수 함수 (시각 판단)
# 스레드 없이도 단위 테스트할 수 있게, "지금 무엇을 해야 하는가"는 전부 인자만 보는 함수로 뺐다.

def parse_hhmm(text):
    """'18:30' → time(18,30). 형식이 틀리면 None (설정 오타가 스레드를 죽이지 않게)."""
    try:
        hh, mm = str(text).strip().split(":")[:2]
        return clock_time(int(hh), int(mm))
    except (TypeError, ValueError):
        return None


def _combine(day, t):
    return datetime.combine(day, t)


def is_due(now, hhmm, window_min):
    """예정 시각을 지났고 아직 창 안인가. 창을 두는 이유는 설계 12장의 '07:40에 PC가 꺼져 있음' 이다.

    한참 뒤에 백엔드를 켰을 때 그날치 배치를 뒤늦게 띄우면 판단 시각과 기록이 어긋난다.
    그런 날은 아예 띄우지 않고 대체 규칙(결정 11)과 `run.note` 의 누락 기록에 맡긴다.
    """
    t = parse_hhmm(hhmm)
    if t is None:
        return False
    start = _combine(now.date(), t)
    return start <= now < start + timedelta(minutes=int(window_min))


def plan_launch(now, schedule, *, trading_day, runs_today, batch_running, window_min=30):
    """지금 띄울 배치가 있는가. `{"stage": …, "reason": …}` 를 돌려준다 (stage=None 이면 안 띄운다).

    runs_today 는 오늘 날짜의 `run` 행 {단계: 상태} 다. 상태와 무관하게 행이 있으면 다시 띄우지
    않는다 — 실패한 배치를 스케줄러가 자동으로 재시도하면 같은 날에 판단이 여러 벌 생겨
    어느 것이 '그날의 판단'인지 사후에 가릴 수 없다. 재시도는 사람이 수동 실행으로 한다.
    """
    if not trading_day:
        return {"stage": None, "reason": "휴장일"}
    if batch_running:
        return {"stage": None, "reason": "배치가 이미 실행 중"}
    for stage in ("final", "prelim"):           # 같은 틱에 둘이 겹치면 최종을 먼저 본다
        if not is_due(now, (schedule or {}).get(stage), window_min):
            continue
        if stage in (runs_today or {}):
            return {"stage": None, "reason": f"{stage} 는 오늘 이미 실행됨 (상태 {runs_today[stage]})"}
        return {"stage": stage, "reason": f"{stage} 예정 시각 {(schedule or {}).get(stage)} 도달"}
    return {"stage": None, "reason": "예정 시각 아님"}


def fallback_state(now, schedule, final_status, *, trading_day):
    """대체 규칙(결정 11)의 판정. 08:50 에 최종 배치가 완료됐는가.

    완료되지 않았으면 전날 예비 판단을 체결 예약으로 승격해야 한다. 승격 자체는 배치(파이프라인)의
    일이라 여기서 지어내지 않고, `fallback_needed` 로 드러내고 경고를 남긴다.
    """
    deadline = (schedule or {}).get("fallback_deadline")
    t = parse_hhmm(deadline)
    if not trading_day or t is None:
        return {"due": False, "needed": False, "deadline": deadline,
                "reason": "휴장일" if not trading_day else "대체 마감 시각 설정 없음"}
    due = now >= _combine(now.date(), t)
    status = (final_status or "").lower() or None
    needed = bool(due and status != "ok")
    return {"due": due, "needed": needed, "deadline": deadline, "final_status": status,
            "reason": ("아직 대체 마감 시각 전" if not due else
                       ("최종 배치 완료" if not needed else
                        f"최종 배치가 완료되지 않았습니다 (상태 {status or '기록 없음'})"))}


def poll_due(now, last_at, schedule, *, trading_day, interval_min=10):
    """DART 공시 목록을 지금 조회할 때인가 (영업일의 poll_window 안, interval_min 간격)."""
    if not trading_day:
        return False
    window = (schedule or {}).get("poll_window") or []
    start = parse_hhmm(window[0]) if len(window) > 0 else None
    end = parse_hhmm(window[1]) if len(window) > 1 else None
    if start is None or end is None or not (start <= now.time() <= end):
        return False
    if last_at is None:
        return True
    return (now - last_at) >= timedelta(minutes=float(interval_min))


def next_occurrence(now, hhmm, is_trading_day):
    """다음 예정 시각 (거래일만). 오늘 시각이 남았으면 오늘, 아니면 다음 거래일."""
    t = parse_hhmm(hhmm)
    if t is None:
        return None
    day = now.date()
    for _ in range(14):
        if is_trading_day(day):
            when = _combine(day, t)
            if when > now:
                return when.isoformat(timespec="minutes")
        day = day + timedelta(days=1)
    return None


# ---------------------------------------------------------------- 자식 프로세스

def _alive(pid):
    """살아 있는가. 좀비(Z)는 죽은 것으로 본다 (news_collector 와 같은 이유: 자식이라 거둬야 사라진다)."""
    try:
        os.kill(int(pid), 0)
    except (OSError, ProcessLookupError, TypeError, ValueError):
        return False
    try:
        state = (Path("/proc") / str(int(pid)) / "stat").read_text().rsplit(") ", 1)[1][0]
    except (OSError, IndexError):
        return True                    # /proc 이 없는 환경(맥·윈도우)에서는 kill 결과를 믿는다
    return state != "Z"


# ---------------------------------------------------------------- 서비스

class AdvisorService:
    """스케줄러·폴러 스레드 + 배치 실행 + 리포트 조회. 백엔드에 하나만 둔다."""

    def __init__(self, db_path=None, config_path=None, cfg=None, root=None):
        self._lock = threading.RLock()
        self._root = Path(root) if root else ROOT
        self._db_override = str(db_path) if db_path else (os.getenv(ENV_DB) or None)
        self._config_path = str(config_path) if config_path else (os.getenv(ENV_CONFIG) or None)
        self._cfg = cfg
        self._cfg_error = None
        self._logs = deque(maxlen=DEFAULTS["advisor.log_lines"])
        self._threads = {}
        self._stop = threading.Event()
        self._proc = None
        self._launched = set()                 # (단계, 날짜) — DB 확인을 못 하는 순간의 보조 잠금
        self._collector_manual_stop = False    # 사람이 수집기 중지 버튼으로 끈 상태면 자동 재시작하지 않는다
        self._collector_last_try = None
        self._poll = {"last_at": None, "last_count": None, "last_error": None, "available": None}
        self._fallback = {"needed": False, "reason": None, "checked_at": None, "warned_on": None}
        self._promote_supported = None

    # ------------------------------------------------------------ 설정·경로

    def cfg(self):
        """설정. 읽지 못해도 서비스는 살아 있어야 하므로 빈 dict 로 물러서고 오류를 상태에 남긴다."""
        with self._lock:
            if self._cfg is None:
                try:
                    self._cfg = load_config(self._config_path)
                    self._cfg_error = None
                except (ConfigError, OSError, ValueError) as exc:
                    self._cfg = {}
                    self._cfg_error = f"{type(exc).__name__}: {exc}"
            return self._cfg

    def _tunable(self, key):
        node = self.cfg()
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return DEFAULTS[key]
            node = node[part]
        return DEFAULTS[key] if node is None else node

    def _path(self, key):
        raw = str(self._tunable(key))
        p = Path(raw)
        return p if p.is_absolute() else (self._root / p)

    def db_path(self):
        """읽고 쓸 advisor.db. ADVISOR_DB 가 설정(paths.db)을 이긴다."""
        if self._db_override:
            return Path(self._db_override)
        try:
            return Path(resolve_path(self.cfg(), "db", root=self._root))
        except (ConfigError, KeyError, TypeError):
            return DATA / "advisor.db"

    def _open(self, readonly=True):
        """읽기 전용 Store. DB 파일이 없으면 None (배치가 한 번도 안 돈 상태는 정상이다)."""
        path = self.db_path()
        if readonly and not path.exists():
            return None
        try:
            return Store(path, readonly=readonly)
        except Exception as exc:                # 잠김·권한·깨진 파일 — 화면은 떠야 한다
            self._log(f"DB 를 열지 못했습니다 ({path}): {exc}", "error")
            return None

    def calendar(self, store=None):
        return TradingCalendar(self.cfg(), store)

    # ------------------------------------------------------------ 로그

    def _log(self, message, level="info"):
        with self._lock:
            self._logs.appendleft({"time": datetime.now(KST).strftime("%H:%M:%S"),
                                   "level": level, "message": message})
        getattr(log, "error" if level == "error" else ("warning" if level == "warning" else "info"))(
            "%s", message)

    def _log_tail(self, lines=None):
        path = self._path("advisor.batch_log")
        try:
            n = int(lines or self._tunable("advisor.log_tail_lines"))
            return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
        except OSError:
            return ""

    # ------------------------------------------------------------ 배치 프로세스

    def _pid_file(self):
        return self._path("advisor.batch_pid")

    def _read_batch(self):
        """PID 파일 → 지금 돌고 있는 배치 정보. 죽었거나 너무 오래됐으면 지우고 None."""
        path = self._pid_file()
        try:
            raw = path.read_text().strip()
        except OSError:
            return None
        try:
            info = json.loads(raw)
            if not isinstance(info, dict):
                raise ValueError
        except (TypeError, ValueError):
            info = {"pid": raw}
        pid = info.get("pid")
        if not _alive(pid):
            path.unlink(missing_ok=True)
            return None
        started = info.get("started_at")
        if started:
            try:
                age = (datetime.now(KST).replace(tzinfo=None)
                       - datetime.fromisoformat(started)).total_seconds()
                if age > float(self._tunable("advisor.batch_stale_sec")):
                    self._log(f"배치가 {age / 3600:.1f}시간째 끝나지 않았습니다 (pid {pid}).", "warning")
            except (TypeError, ValueError):
                pass
        return info

    def _spawn(self, stage, as_of=None, mode="live", extra_args=()):
        """배치를 자식 프로세스로 띄운다. 표준 출력·오류는 배치 로그 파일로 모은다."""
        DATA.mkdir(parents=True, exist_ok=True)
        log_path = self._path("advisor.batch_log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "backend.advisor.run", "--stage", str(stage),
               "--mode", str(mode)]
        if as_of:
            cmd += ["--as-of", str(as_of)]
        if self._db_override:
            cmd += ["--db", str(self.db_path())]
        if self._config_path:
            cmd += ["--config", str(self._config_path)]
        cmd += [str(a) for a in extra_args]

        handle = open(log_path, "ab", buffering=0)
        try:
            handle.write((f"\n===== {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')} "
                          f"{' '.join(cmd[1:])}\n").encode("utf-8"))
            proc = subprocess.Popen(cmd, cwd=str(self._root), stdout=handle,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    start_new_session=True)
        finally:
            handle.close()
        info = {"pid": proc.pid, "stage": stage, "as_of": as_of, "mode": mode,
                "started_at": datetime.now(KST).replace(tzinfo=None).isoformat(timespec="seconds"),
                "cmd": " ".join(cmd[1:])}
        self._pid_file().write_text(json.dumps(info, ensure_ascii=False))
        with self._lock:
            self._proc = proc
        self._log(f"배치를 시작했습니다: {stage}/{mode}"
                  f"{f' as-of {as_of}' if as_of else ''} (pid {proc.pid})")
        return info

    def _reap(self):
        """죽은 자식을 거둔다. 안 하면 좀비가 쌓이고 PID 재사용 때 오판한다."""
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is not None:
            with self._lock:
                self._proc = None
            self._pid_file().unlink(missing_ok=True)
            self._log(f"배치가 종료됐습니다 (코드 {proc.returncode}).",
                      "info" if proc.returncode == 0 else "error")

    def run_batch(self, stage, as_of=None, mode="live"):
        """POST /advisor/run — 수동 실행. 배치가 돌고 있으면 거절한다."""
        if stage not in STAGES:
            raise AdvisorError(f"단계는 {' 또는 '.join(STAGES)} 여야 합니다: {stage}")
        if mode not in MODES:
            raise AdvisorError(f"모드는 {' 또는 '.join(MODES)} 여야 합니다: {mode}")
        self._reap()
        running = self._read_batch()
        if running:
            raise AdvisorError(
                f"배치가 이미 실행 중입니다 ({running.get('stage')}/{running.get('mode')}, "
                f"pid {running.get('pid')}). 끝난 뒤에 다시 실행하세요.")
        info = self._spawn(stage, as_of=as_of, mode=mode)
        with self._lock:
            self._launched.add((stage, str(as_of or datetime.now(KST).strftime("%Y-%m-%d"))))
        return {"started": True, "batch": info, "status": self.status()}

    # ------------------------------------------------------------ 스케줄러 스레드

    def _runs_today(self, store, day):
        """오늘 날짜의 실시간 실행 {단계: 상태}. '이미 띄웠는가'를 메모리가 아니라 DB 로 본다."""
        if store is None:
            return {}
        return report_mod.runs_on(store, day, mode="live")

    def _promote_flag_ok(self):
        """배치가 대체 규칙 인자를 받는가. `--help` 한 번으로 확인하고 결과를 기억한다.

        파이프라인 담당이 `--promote-prelim` 을 아직 넣지 않았을 수 있다. 없는 인자를 넘기면
        배치가 인자 오류로 죽으므로, 있을 때만 넘기고 없으면 경고만 남긴다.
        """
        with self._lock:
            if self._promote_supported is not None:
                return self._promote_supported
        flag = str(self._tunable("advisor.promote_flag"))
        ok = False
        try:
            out = subprocess.run([sys.executable, "-m", "backend.advisor.run", "--help"],
                                 cwd=str(self._root), capture_output=True, text=True, timeout=60)
            ok = flag in (out.stdout or "")
        except Exception as exc:
            self._log(f"배치 인자 확인에 실패했습니다: {exc}", "warning")
        with self._lock:
            self._promote_supported = ok
        return ok

    def _check_fallback(self, now, store, trading_day):
        """08:50 의 대체 규칙 확인 (결정 11). 승격 자체는 배치의 일이다."""
        schedule = (self.cfg().get("schedule") or {})
        today = now.strftime("%Y-%m-%d")
        runs = self._runs_today(store, today) if store is not None else {}
        state = fallback_state(now, schedule, runs.get("final"), trading_day=trading_day)
        with self._lock:
            self._fallback.update({"needed": state["needed"], "reason": state["reason"],
                                   "deadline": state["deadline"],
                                   "checked_at": now.isoformat(timespec="seconds")})
            warned = self._fallback.get("warned_on")
        if not state["needed"] or warned == today:
            return state
        with self._lock:
            self._fallback["warned_on"] = today
        if self._promote_flag_ok() and not self._read_batch():
            self._log(f"대체 규칙: {state['reason']} → 예비 판단 승격 배치를 띄웁니다.", "warning")
            try:
                self._spawn("final", mode="live",
                            extra_args=[str(self._tunable("advisor.promote_flag"))])
            except Exception as exc:
                self._log(f"승격 배치를 띄우지 못했습니다: {exc}", "error")
        else:
            self._log(
                f"대체 규칙: {state['reason']}. 배치에 "
                f"{self._tunable('advisor.promote_flag')} 인자가 아직 없어 승격을 자동으로 하지 못합니다 — "
                "전날 예비 판단을 체결 예약으로 승격해야 합니다 (결정 11).", "warning")
        return state

    def _scheduler_tick(self, now=None):
        """스케줄러 한 번. 테스트가 직접 부를 수 있게 루프와 분리했다."""
        now = now or datetime.now(KST).replace(tzinfo=None)
        store = self._open(readonly=True)
        try:
            cal = self.calendar(store)
            trading_day = cal.is_trading_day(now.date())
            runs = self._runs_today(store, now.strftime("%Y-%m-%d")) if store is not None else {}
            self._reap()
            plan = plan_launch(now, self.cfg().get("schedule") or {}, trading_day=trading_day,
                               runs_today=runs, batch_running=bool(self._read_batch()),
                               window_min=self._tunable("advisor.launch_window_min"))
            stage = plan["stage"]
            if stage and (stage, now.strftime("%Y-%m-%d")) not in self._launched:
                with self._lock:
                    self._launched.add((stage, now.strftime("%Y-%m-%d")))
                self._log(f"스케줄러: {plan['reason']}")
                try:
                    self._spawn(stage, mode="live")
                except Exception as exc:
                    self._log(f"배치를 띄우지 못했습니다: {exc}", "error")
            self._check_fallback(now, store, trading_day)
            self._ensure_collector()
            return plan
        finally:
            if store is not None:
                store.close()

    # ------------------------------------------------------------ 실시간 수집기

    def _collector_engine(self):
        """수집기 감독 객체 (services/news_collector.py). 지연 import — 테스트가 이 메서드를
        가짜로 바꿔 끼워 운영 PID 파일·운영 수집기에 닿지 않게 하는 이음매다."""
        from backend.app.services.news_collector import news_collector
        return news_collector

    def _ensure_collector(self):
        """수집기가 안 돌고 있으면 띄운다. 뉴스·공시 속보는 지나가면 다시 받을 수 없어
        "켜는 걸 잊은 날"이 곧 영구 결손이다 — 그래서 운영 스위치 하나에 묶어 두고
        스케줄러가 20초마다 생존을 본다. 사람이 판단 지원 탭에서 직접 끈 경우는 존중한다.

        재시도 간격(`advisor.collector_retry_sec`)을 두는 이유: LS 키가 없거나 서버가
        막혀 즉시 죽는 상태에서 틱마다 다시 띄우면 로그만 쌓인다."""
        with self._lock:
            manual_stop = self._collector_manual_stop
            last_try = self._collector_last_try
        if manual_stop:
            return False
        now = datetime.now(KST).replace(tzinfo=None)
        retry = float(self._tunable("advisor.collector_retry_sec"))
        if last_try and (now - last_try).total_seconds() < retry:
            return False
        try:
            engine = self._collector_engine()
        except Exception as exc:
            self._log(f"수집기 모듈을 불러오지 못했습니다: {exc}", "error")
            return False
        if engine.status()["collector"]["running"]:
            return True
        with self._lock:
            self._collector_last_try = now
        try:
            engine.start_collector()
            self._log("실시간 수집기를 띄웠습니다 (뉴스·공시 속보).")
            return True
        except Exception as exc:
            self._log(f"실시간 수집기를 띄우지 못했습니다: {exc}", "error")
            return False

    def note_collector_manual_stop(self, stopped: bool):
        """판단 지원 탭의 수집기 시작/중지 버튼(/news/collector/*)이 부른다. 사람이 껐으면 자동 재시작을 멈추고, 다시 켰으면 감시를 재개한다."""
        with self._lock:
            self._collector_manual_stop = bool(stopped)
            self._collector_last_try = None

    def _collector_state(self):
        try:
            full = self._collector_engine().status()
            st = full["collector"]
        except Exception as exc:
            return {"running": False, "error": f"{type(exc).__name__}: {exc}"}
        with self._lock:
            manual_stop = self._collector_manual_stop
        # counts: 오늘 받은 뉴스·공시 건수. 판단 지원 탭 카드가 "돌고는 있는데 안 받는" 상태를 보이게 한다
        return {"running": bool(st.get("running")), "pid": st.get("pid"),
                "log_path": st.get("log_path"), "manual_stop": manual_stop,
                "enabled": self.scheduler_enabled(), "counts": full.get("counts")}

    def _scheduler_loop(self):
        tick = float(self._tunable("advisor.tick_sec"))
        while not self._stop.is_set():
            try:
                self._scheduler_tick()
            except Exception as exc:                 # 스레드는 어떤 예외로도 죽지 않는다
                self._log(f"스케줄러 오류: {type(exc).__name__}: {exc}", "error")
            self._stop.wait(tick)

    # ------------------------------------------------------------ 폴러 스레드

    def _poll_once(self, now=None):
        """DART 공시 목록 한 번 (설계 12장: 영업일 07:30~18:10, 10분 간격).

        폴러 모듈이 아직 없을 수 있어 지연 import 하고 ImportError 를 상태로만 남긴다 —
        모듈이 들어오면 백엔드를 다시 띄우지 않아도 다음 조회부터 붙는다.
        """
        now = now or datetime.now(KST).replace(tzinfo=None)
        try:
            from backend.advisor.poller import poll_once
        except ImportError as exc:
            with self._lock:
                self._poll.update({"available": False,
                                   "last_error": f"공시 폴러 모듈이 아직 없습니다 ({exc})"})
            return None
        store = self._open(readonly=False)
        if store is None:
            with self._lock:
                self._poll["last_error"] = "DB 를 열지 못했습니다"
            return None
        try:
            count = poll_once(store, self.cfg(), now)
            store.commit()
            with self._lock:
                self._poll.update({"available": True, "last_at": now, "last_count": count,
                                   "last_error": None})
            return count
        except Exception as exc:
            with self._lock:
                self._poll.update({"available": True, "last_at": now,
                                   "last_error": f"{type(exc).__name__}: {exc}"})
            self._log(f"공시 조회 실패: {type(exc).__name__}: {exc}", "warning")
            return None
        finally:
            store.close()

    def _poller_tick(self, now=None):
        now = now or datetime.now(KST).replace(tzinfo=None)
        schedule = self.cfg().get("schedule") or {}
        store = self._open(readonly=True)
        try:
            trading_day = self.calendar(store).is_trading_day(now.date())
        finally:
            if store is not None:
                store.close()
        with self._lock:
            last = self._poll["last_at"]
        if not poll_due(now, last, schedule, trading_day=trading_day,
                        interval_min=schedule.get("poll_interval_min") or 10):
            return None
        return self._poll_once(now)

    def _poller_loop(self):
        tick = float(self._tunable("advisor.tick_sec"))
        while not self._stop.is_set():
            try:
                self._poller_tick()
            except Exception as exc:
                self._log(f"폴러 오류: {type(exc).__name__}: {exc}", "error")
            self._stop.wait(tick)

    # ------------------------------------------------------------ 스레드 수명

    def scheduler_enabled(self):
        return str(os.getenv(ENV_SCHEDULER, "")).strip().lower() in ("1", "true", "yes", "on")

    def start(self, force=False):
        """스레드를 띄운다. `ADVISOR_SCHEDULER=1` 이 아니면 아무것도 하지 않는다."""
        if not force and not self.scheduler_enabled():
            return False
        with self._lock:
            if self._threads:
                return True
            self._stop.clear()
            for name, target in (("advisor-scheduler", self._scheduler_loop),
                                 ("advisor-poller", self._poller_loop)):
                thread = threading.Thread(target=target, name=name, daemon=True)
                thread.start()
                self._threads[name] = thread
        self._log("스케줄러·폴러 스레드를 시작했습니다.")
        self._ensure_collector()
        return True

    def start_if_enabled(self):
        return self.start(force=False)

    def stop(self, timeout=5.0):
        self._stop.set()
        with self._lock:
            threads, self._threads = self._threads, {}
        for thread in threads.values():
            thread.join(timeout=timeout)
        return True

    def _thread_state(self, name):
        with self._lock:
            thread = self._threads.get(name)
        return {"name": name, "running": bool(thread and thread.is_alive())}

    # ------------------------------------------------------------ 조회 (읽기 전용)

    def _read(self, fn, *args, **kwargs):
        """읽기 전용 Store 를 열어 리포트 함수를 부른다. DB 가 없으면 available=False."""
        store = self._open(readonly=True)
        if store is None:
            return {"available": False, "reason": "advisor.db 가 아직 없습니다. 배치를 한 번 실행하세요.",
                    "db_path": str(self.db_path())}
        try:
            return fn(store, *args, **kwargs)
        except Exception as exc:                # 화면이 통째로 죽지 않게 사유를 담아 돌려준다
            self._log(f"리포트 조회 실패: {type(exc).__name__}: {exc}", "error")
            return {"available": False, "reason": f"{type(exc).__name__}: {exc}",
                    "db_path": str(self.db_path())}
        finally:
            store.close()

    def report(self, date=None, stage=None, mode="live"):
        return self._read(report_mod.report, self.cfg(), date=date, stage=stage, mode=mode,
                          top_n=int(self._tunable("advisor.report_top_n")),
                          top_factors=int(self._tunable("advisor.diff_top_factors")))

    def scores(self, date=None, layer=None, stage=None, mode="live"):
        return self._read(report_mod.scores, self.cfg(), date=date, layer=layer, stage=stage,
                          mode=mode, top_n=None)

    def asset(self, code, date=None, stage=None, mode="live"):
        return self._read(report_mod.asset, self.cfg(), code, date=date, stage=stage, mode=mode,
                          evidence_limit=int(self._tunable("advisor.evidence_limit")))

    def performance(self, mode="live"):
        return self._read(report_mod.performance, self.cfg(), mode=mode)

    def metrics(self, mode="live"):
        return self._read(report_mod.metrics, self.cfg(), mode=mode)

    def status(self, mode="live"):
        """GET /advisor/status — 마지막 실행, 다음 예정 시각, 스레드 상태, 오늘 LLM 비용."""
        now = datetime.now(KST).replace(tzinfo=None)
        schedule = self.cfg().get("schedule") or {}
        path = self.db_path()
        data = self._read(report_mod.db_status, self.cfg(), mode=mode)

        store = self._open(readonly=True)
        cal = self.calendar(store)
        try:
            trading_day = cal.is_trading_day(now.date())
            runs_today = self._runs_today(store, now.strftime("%Y-%m-%d"))
        except Exception:
            trading_day, runs_today = None, {}
        finally:
            if store is not None:
                store.close()
        cal = self.calendar()          # 닫힌 store 를 보지 않게 설정만으로 다시 만든다

        self._reap()
        batch = self._read_batch()
        with self._lock:
            poll = dict(self._poll)
            fallback = dict(self._fallback)
            logs = list(self._logs)
        poll["last_at"] = poll["last_at"].isoformat(timespec="seconds") if poll["last_at"] else None

        return {
            "available": bool(data.get("available")),
            "now": now.isoformat(timespec="seconds"),
            "trading_day": trading_day,
            "db": {"path": str(path), "exists": path.exists()},
            "config": {"path": self._config_path, "hash_source": "advisor.config.yaml",
                       "error": self._cfg_error},
            "data": data,
            "runs_today": runs_today,
            "scheduler": {
                **self._thread_state("advisor-scheduler"),
                "enabled": self.scheduler_enabled(),
                "env": f"{ENV_SCHEDULER}=1 이면 켜집니다",
                "schedule": {"prelim": schedule.get("prelim"), "final": schedule.get("final"),
                             "fallback_deadline": schedule.get("fallback_deadline")},
                "next": {stage: next_occurrence(now, schedule.get(stage), cal.is_trading_day)
                         for stage in STAGES},
                "launch_window_min": self._tunable("advisor.launch_window_min"),
            },
            "poller": {
                **self._thread_state("advisor-poller"),
                "enabled": self.scheduler_enabled(),
                "interval_min": schedule.get("poll_interval_min"),
                "window": schedule.get("poll_window"),
                **poll,
            },
            "batch": {
                "running": bool(batch),
                **(batch or {}),
                "log_path": str(self._path("advisor.batch_log")),
                "log_tail": self._log_tail(),
            },
            "collector": self._collector_state(),
            "fallback": fallback,
            "fallback_needed": bool(fallback.get("needed")),
            "logs": logs,
        }


advisor_service = AdvisorService()
