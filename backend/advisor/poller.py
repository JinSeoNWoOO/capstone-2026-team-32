"""DART 공시 목록 주기 조회 (설계 2.1의 `advisor-poller` 스레드, 12장: 영업일 07:30~18:10, 10분 간격).

폴러가 하는 일은 하나다 — **공시를 언제 알 수 있었는지를 기록**한다. 판단은 하지 않는다.
하루 두 번의 배치에서만 공시를 받으면 `first_seen_at` 이 18:30·07:40 둘 중 하나로만 찍혀,
"장중 11시에 나온 공시를 우리가 11시에 알 수 있었다"는 사실이 사라진다. 그 시각이 곧 설계 5.4의
"둘 중 빠른 쪽" 비교 대상이고, 나중에 LS 속보와의 시차를 재는 자료이기도 하다.

상태가 없는 HTTP 호출이라 백엔드가 재시작돼도 잃는 것이 없다 (설계 2.1). 그래서 웹소켓 수집기와
달리 별도 프로세스가 아니라 백엔드 스레드로 둔다 — 스레드 배선은 `app/services/advisor.py` 의 몫이고,
여기서는 **순수 함수 한 개와 얇은 루프 한 개**만 둔다.
"""
import logging

from .calendar import TradingCalendar
from .sources import dart
from .sources.base import Report, parse_hhmm, now_kst, to_kst

log = logging.getLogger("advisor.poller")


def in_poll_window(cfg, now):
    """지금이 조회 창 안인가 (설정 `schedule.poll_window` 의 [시작, 끝])."""
    window = (cfg.get("schedule") or {}).get("poll_window") or []
    if len(window) != 2:
        return False
    start, end = parse_hhmm(window[0]), parse_hhmm(window[1])
    return start <= to_kst(now).time() <= end


def should_poll(cfg, now, cal=None):
    """거래일이면서 조회 창 안인가. 휴장일에는 아무것도 하지 않는다 (설계 4장)."""
    moment = to_kst(now)
    cal = cal or TradingCalendar(cfg)
    return bool(cal.is_trading_day(moment.date()) and in_poll_window(cfg, moment))


def poll_once(store, cfg, now=None, cal=None, http=None, report=None):
    """조회 창 안이면 오늘 공시 목록을 받아 upsert 한다. 반환은 **새로 본 접수번호 수**.

    `as_of` 를 지금으로 준다 — 폴러는 언제나 현재를 본다. 과거 재현에는 쓰지 않는다.
    실패해도 예외를 밖으로 내보내지 않는다. 스레드가 죽으면 다음 조회가 없어지는데, 공시 하나
    놓치는 것보다 그쪽이 훨씬 나쁘다.
    """
    moment = to_kst(now or now_kst())
    if not should_poll(cfg, moment, cal=cal):
        return 0
    report = report if report is not None else Report()
    try:
        result = dart.sync(store, cfg, moment, bgn_de=moment.date(), end_de=moment.date(),
                           http=http, report=report, now=moment)
        store.commit()
    except Exception as exc:                       # 키 만료·네트워크 단절 등
        log.warning("공시 목록 조회 실패: %s", exc)
        return 0
    if result.get("note"):
        log.info("공시 목록: %s", result["note"])
    return int(result.get("new") or 0)


def run_forever(store, cfg, stop_event, now_fn=None, sleep=None, http=None):
    """조회 간격마다 `poll_once` 를 부른다. `stop_event.wait` 로 자므로 종료가 즉시 먹힌다.

    스레드를 만드는 쪽(백엔드)이 `threading.Event` 를 주고 종료할 때 set 한다.
    """
    interval = float((cfg.get("schedule") or {}).get("poll_interval_min", 10)) * 60.0
    now_fn = now_fn or now_kst
    waiter = sleep or stop_event.wait
    total = 0
    while not stop_event.is_set():
        total += poll_once(store, cfg, now=now_fn(), http=http)
        if waiter(interval):
            break
    return total


__all__ = ["in_poll_window", "should_poll", "poll_once", "run_forever"]
