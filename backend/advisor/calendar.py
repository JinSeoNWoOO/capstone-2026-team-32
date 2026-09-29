"""거래일 판정 (설계 4장: 휴장일에는 아무것도 하지 않는다, 결정 11: 채점 기간은 거래일로 센다).

네트워크를 쓰지 않는다. 거래소 달력 API 에 의존하면 배치가 외부 장애에 묶이고, 과거 재현 모드에서
그날의 달력을 다시 받아 올 수도 없다. 대신 두 가지 근거를 쓰고 **데이터가 규칙을 이긴다**.

  1. 규칙: 평일에서 설정의 calendar.holidays 를 뺀다. 미래 날짜는 이것밖에 쓸 수 없다.
  2. 데이터: market_daily 에 KOSPI 값이 있는 날은 실제로 장이 열린 날이다. 지수 값이 남은 구간에서는
     이쪽이 진실이다 — 임시 휴장·대체공휴일처럼 설정에 못 적어 둔 날을 데이터가 알려 준다.
     (2026-09-28 대체공휴일 여부가 아직 확인되지 않은 것이 정확히 이 경우다. 설계 11장)

데이터가 이기는 범위는 **KOSPI 값이 실제로 있는 날짜 구간 안**으로 한정한다. 수집이 안 된 미래나
수집 시작 전 과거까지 "값이 없으니 휴장"이라고 하면 없는 휴장일을 만들어 낸다.
"""
from datetime import date, datetime, timedelta

from .config import load_config

# market_daily 의 계열 이름. 코스피 지수 일봉이 곧 '그날 장이 열렸는가'의 증거다 (설계 8장).
KOSPI_SERIES = "KOSPI"

# 거래일을 찾아 앞뒤로 움직일 때의 안전 한계(일). 휴장일 설정이 잘못돼 무한 루프가 되는 것을 막는다.
_MAX_SCAN_DAYS = 3650


def to_date(d):
    """date | datetime | 'YYYY-MM-DD' → date. 다른 것은 오류."""
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        return datetime.strptime(d.strip()[:10], "%Y-%m-%d").date()
    raise TypeError(f"날짜로 볼 수 없습니다: {d!r}")


class TradingCalendar:
    """설정의 휴장일 + (있으면) 저장된 KOSPI 일자로 거래일을 판정한다."""

    def __init__(self, cfg=None, store=None):
        self.cfg = cfg if cfg is not None else load_config()
        self.store = store
        self.holidays = {to_date(h) for h in ((self.cfg.get("calendar") or {}).get("holidays") or [])}
        self._data_days = None
        self._data_range = None

    def refresh(self):
        """저장된 KOSPI 일자를 다시 읽는다 (수집이 더 진행된 뒤 다시 물을 때)."""
        self._data_days = None
        self._data_range = None
        return self

    def _data(self):
        """{거래일} 과 (최초, 최종) 날짜. store 가 없거나 행이 없으면 빈 집합."""
        if self._data_days is None:
            days = set()
            if self.store is not None:
                try:
                    rows = self.store.conn.execute(
                        "SELECT date FROM market_daily WHERE series=?", (KOSPI_SERIES,)).fetchall()
                except Exception:          # 테이블이 아직 없거나 읽을 수 없어도 규칙으로 답한다
                    rows = []
                for r in rows:
                    try:
                        days.add(to_date(r[0]))
                    except (TypeError, ValueError):
                        pass
            self._data_days = days
            self._data_range = (min(days), max(days)) if days else None
        return self._data_days, self._data_range

    def is_trading_day(self, d):
        """그날 장이 열리는가(열렸는가)."""
        d = to_date(d)
        days, rng = self._data()
        if rng is not None and rng[0] <= d <= rng[1]:
            return d in days               # 데이터가 있는 구간에서는 데이터가 진실이다
        return d.weekday() < 5 and d not in self.holidays

    def next_trading_day(self, d):
        """d 다음 거래일 (d 자신은 포함하지 않는다)."""
        return self._step(to_date(d), 1)

    def prev_trading_day(self, d):
        """d 직전 거래일 (d 자신은 포함하지 않는다)."""
        return self._step(to_date(d), -1)

    def _step(self, d, direction):
        cur = d
        for _ in range(_MAX_SCAN_DAYS):
            cur = cur + timedelta(days=direction)
            if self.is_trading_day(cur):
                return cur
        raise ValueError(f"{_MAX_SCAN_DAYS}일 안에 거래일을 찾지 못했습니다: {d} 방향 {direction}")

    def add_trading_days(self, d, n):
        """d 에서 n 거래일 뒤(n<0 이면 앞). n=0 은 d 를 그대로 준다.

        채점의 '시작일부터 h거래일 뒤'가 이 함수다 (설계 7.1). 시작일 자신은 세지 않는다.
        """
        d = to_date(d)
        if n == 0:
            return d
        step = 1 if n > 0 else -1
        for _ in range(abs(int(n))):
            d = self._step(d, step)
        return d

    def trading_days_between(self, a, b):
        """a 부터 b 까지의 거래일 목록 (양 끝 포함). a > b 면 빈 목록."""
        a, b = to_date(a), to_date(b)
        out = []
        cur = a
        while cur <= b:
            if self.is_trading_day(cur):
                out.append(cur)
            cur += timedelta(days=1)
        return out


_default = None


def default_calendar(cfg=None, store=None):
    """설정 파일 하나로 만드는 공용 달력. cfg 나 store 를 주면 새로 만든다."""
    global _default
    if cfg is not None or store is not None:
        return TradingCalendar(cfg, store)
    if _default is None:
        _default = TradingCalendar()
    return _default


def is_trading_day(d, cal=None):
    return (cal or default_calendar()).is_trading_day(d)


def next_trading_day(d, cal=None):
    return (cal or default_calendar()).next_trading_day(d)


def prev_trading_day(d, cal=None):
    return (cal or default_calendar()).prev_trading_day(d)


def add_trading_days(d, n, cal=None):
    return (cal or default_calendar()).add_trading_days(d, n)


def trading_days_between(a, b, cal=None):
    return (cal or default_calendar()).trading_days_between(a, b)
