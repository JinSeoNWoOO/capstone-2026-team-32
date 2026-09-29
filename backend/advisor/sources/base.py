"""시점 방어선 (설계 2.3) 과 수집 모듈이 공유하는 잡동사니.

> 판단에 쓰는 모든 데이터의 시각 < 판단 시각

이 불변식을 **수집 지점에서** 지킨다. 요인 계산 쪽에도 `store.prices(end_date=...)` 라는 2차
방어선이 있지만, 미래 값이 DB 에 한 번 들어가면 재현 모드·사후 채점이 전부 오염된다. 그래서
"저장하기 전에 거른다".

## 두 단계로 나누는 이유 — 거르기(drop)와 죽기(assert)

제공자는 우리가 묻지 않은 행을 끼워 준다. KIS 기간별 시세는 장중에도 **오늘 행**을 거래량 0 으로
돌려주고, yfinance 의 `KRW=X` 는 24시간 거래라 오늘 봉이 항상 미완성으로 딸려 온다. 이건 제공자의
정상 동작이지 버그가 아니므로 **조용히 버린다**.
반대로 거르고 난 뒤에도 허용 날짜보다 새 행이 남아 있으면 그건 우리 로직이 틀린 것이다. 그때는
`LookaheadError` 로 배치를 **죽인다** — 조사한 FinAgent 공식 코드가 플래그 기본값 실수로 미래
14거래일을 차트에 흘린 사고가 정확히 "거르기만 하고 검사는 안 해서" 생긴다.

## 일봉이 "알려지는" 시각

| 규칙 키 | 뜻 | 설정값 |
|---|---|---|
| `daily_bar_known_at` | 국내 일봉 D일자는 **D일 그 시각**에 확정 | 18:00 |
| `us_bar_known_at` | 미국 일봉 D일자는 **D+1일 그 시각**에 확정 (미국 장 마감이 한국 새벽) | 07:00 |
| `fx_known_at` | 24시간 거래 환율은 **D+1일 그 시각** = 날짜가 기준일보다 앞선 것만 | 00:00 |

그래서 18:30 예비 배치는 당일(T) 일봉까지, 07:40 최종 배치는 전날(T) 일봉까지 본다.
미국 지수는 예비에서 T-1, 최종에서 T 까지 — `mkt_overnight` 이 예비에서 결측인 것이 곧 실험
설계다 (결정 11). 환율은 어느 단계에서도 기준일 당일 봉을 쓰지 않는다.
"""
import os
import sqlite3
from datetime import date, datetime, time as dtime, timedelta, timezone

KST = timezone(timedelta(hours=9))

# 설정 sources 블록에서 읽는 "알려지는 시각" 규칙. (며칠 뒤, 설정 키) 짝이다.
# 며칠 뒤가 0 이면 그날 그 시각, 1 이면 다음 날 그 시각에 그 일자의 값이 확정된다.
KNOWN_AT_RULES = {
    "daily_bar": (0, "daily_bar_known_at"),
    "us_bar": (1, "us_bar_known_at"),
    "fx": (1, "fx_known_at"),
}


class SourceError(RuntimeError):
    """수집 중 복구 불가능한 오류. ingest 가 단계별로 잡아 배치를 이어 간다."""


class LookaheadError(AssertionError):
    """기준 시각 이후의 데이터가 반환값에 남았다 (설계 2.3의 assert).

    AssertionError 를 상속하는 이유: `python -O` 로 assert 가 꺼져도 이 검사는 꺼지지 않아야
    하지만, 잡는 쪽 입장에서는 여전히 "단정이 깨졌다"로 읽히는 편이 맞다.
    """


class ConfigKeyError(SourceError):
    """설정에 있어야 할 키가 없다. 숫자를 코드에 박지 않기로 했으므로 기본값으로 때우지 않는다."""


# ---------------------------------------------------------------- 설정 읽기

def cfg_get(cfg, *path):
    """cfg["a"]["b"] 를 안전하게 읽고, 없으면 어느 키가 없는지 말하며 죽는다.

    기본값을 주지 않는 것이 의도다 (설계 3장: 코드에 숫자를 박지 않는다). 설정에 없는 값을
    코드가 몰래 들고 있으면 config_hash 가 그 값을 담지 못해 "이 판단은 어느 설정으로 나왔는가"가
    깨진다.
    """
    node = cfg
    for i, key in enumerate(path):
        if not isinstance(node, dict) or key not in node:
            raise ConfigKeyError(f"설정에 필수 키가 없습니다: {'.'.join(path[:i + 1])}")
        node = node[key]
    return node


def sources_cfg(cfg, key):
    """sources 블록의 값 하나."""
    return cfg_get(cfg, "sources", key)


# ---------------------------------------------------------------- 날짜·시각 다루기

def to_date(value):
    """date | datetime | 'YYYY-MM-DD' | 'YYYYMMDD' | pandas Timestamp → date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "to_pydatetime"):                 # pandas.Timestamp
        return value.to_pydatetime().date()
    if isinstance(value, str):
        text = value.strip()
        if len(text) == 8 and text.isdigit():
            return date(int(text[:4]), int(text[4:6]), int(text[6:]))
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    raise TypeError(f"날짜로 볼 수 없습니다: {value!r}")


def iso_date(value):
    """저장 포맷 'YYYY-MM-DD'. price_daily.date 등 모든 날짜 열이 이 모양이다."""
    return to_date(value).isoformat()


def compact_date(value):
    """제공자들이 쓰는 'YYYYMMDD' (KIS·DART·pykrx 가 전부 이 형식)."""
    return to_date(value).strftime("%Y%m%d")


def to_kst(moment):
    """tz 가 붙어 있으면 KST 로 옮기고, 없으면 이미 KST 벽시계로 본다.

    수집기의 recv_wall 이 오프셋 없는 KST 문자열이라 (advisor/store.py 머리말) 시스템 전체가
    naive-KST 를 쓴다. 여기서 한 번만 맞춰 두면 뒤에서 비교가 전부 성립한다.
    """
    if isinstance(moment, datetime):
        return moment.astimezone(KST).replace(tzinfo=None) if moment.tzinfo else moment
    raise TypeError(f"시각으로 볼 수 없습니다: {moment!r}")


def now_kst():
    return datetime.now(KST).replace(tzinfo=None)


def wall(moment):
    """저장·비교용 KST naive ISO 문자열 (밀리초). store.now_wall() 과 같은 형식."""
    return to_kst(moment).isoformat(timespec="milliseconds")


def parse_hhmm(text):
    """'18:00' → time(18, 0)."""
    parts = str(text).strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ConfigKeyError(f"HH:MM 형식이 아닙니다: {text!r}")
    return dtime(int(parts[0]), int(parts[1]))


# ---------------------------------------------------------------- 시점 방어선

def known_date_limit(cfg, as_of, rule="daily_bar"):
    """as_of 시각에 값을 알 수 있는 **가장 최근 일자**.

    "D일자 값은 (D + 며칠) 날의 HH:MM 에 확정된다"를 뒤집어, 그 확정 시각이 as_of 이하인 최대 D 를 찾는다.
    """
    if rule not in KNOWN_AT_RULES:
        raise ConfigKeyError(f"알 수 없는 시점 규칙: {rule} (가능: {sorted(KNOWN_AT_RULES)})")
    day_offset, key = KNOWN_AT_RULES[rule]
    at = parse_hhmm(sources_cfg(cfg, key))
    moment = to_kst(as_of)
    limit = moment.date() - timedelta(days=day_offset)
    if moment.time() < at:
        limit -= timedelta(days=1)
    return limit


def guard_dated_rows(rows, cfg, as_of, rule="daily_bar", key="date", label=""):
    """일자가 붙은 행에서 아직 알 수 없는 것을 버리고, 남은 것을 다시 검사한다.

    제공자가 끼워 주는 미완성 행(오늘의 KIS 일봉, 24시간 거래 환율의 오늘 봉)은 조용히 버리고,
    그래도 남아 있으면 LookaheadError 로 죽는다. 반환은 일자 오름차순.
    """
    limit = known_date_limit(cfg, as_of, rule)
    kept = []
    for row in rows:
        try:
            row_date = to_date(row[key])
        except (TypeError, ValueError, KeyError) as exc:
            raise SourceError(f"{label or '행'}의 날짜를 읽을 수 없습니다: {row!r}") from exc
        if row_date <= limit:
            kept.append(row)
    assert_max_date(kept, limit, key=key, label=label)
    return sorted(kept, key=lambda r: to_date(r[key]))


def assert_max_date(rows, limit, key="date", label=""):
    """반환 직전의 단정: 어떤 행도 허용 일자를 넘지 않는다 (설계 2.3 구현 규칙 1)."""
    for row in rows:
        row_date = to_date(row[key])
        if row_date > limit:
            raise LookaheadError(
                f"{label or '수집 결과'}에 기준 시각 이후 데이터가 있습니다: "
                f"{row_date} > 허용 {limit}")
    return rows


def assert_not_after(moments, as_of, label=""):
    """분 단위 시각이 기준 시각을 넘지 않는지 (공시 first_seen_at·뉴스 수신 시각용)."""
    limit = to_kst(as_of)
    for moment in moments:
        if moment is None:
            continue
        got = to_kst(moment) if isinstance(moment, datetime) else datetime.fromisoformat(str(moment))
        if got > limit:
            raise LookaheadError(
                f"{label or '수집 결과'}에 기준 시각 이후 시각이 있습니다: {got} > 허용 {limit}")
    return moments


# ---------------------------------------------------------------- 증분 수집

def missing_start(store, table, code, as_of_limit, backfill_days, code_col="code"):
    """이 코드에 대해 **어느 날부터** 받아 와야 하는가.

    저장된 마지막 일자의 다음 날부터 받는다. 한 건도 없으면 backfill_days 만큼 거슬러 올라간다.
    이미 있는 구간을 다시 받지 않는 것은 속도 문제이기도 하지만, 같은 값을 다시 써서
    knowledge_time 을 흔들지 않기 위해서이기도 하다 (store.py 머리말 2).
    반환이 None 이면 받을 것이 없다.
    """
    limit = to_date(as_of_limit)
    last = None
    try:
        row = store.conn.execute(
            f"SELECT MAX(date) FROM {table} WHERE {code_col}=?", (code,)).fetchone()
        last = row[0] if row else None
    except sqlite3.Error:                                  # 테이블이 아직 없으면 전체 backfill
        last = None
    if last:
        start = to_date(last) + timedelta(days=1)
    else:
        start = limit - timedelta(days=int(backfill_days))
    return None if start > limit else start


def missing_dates(store, table, codes, as_of_limit, backfill_days, code_col="code"):
    """여러 코드의 빈 구간을 합친 (시작일, 끝일). 전 종목을 날짜 단위로 한 번에 받는 경로용."""
    starts = [missing_start(store, table, c, as_of_limit, backfill_days, code_col) for c in codes]
    starts = [s for s in starts if s is not None]
    if not starts:
        return None
    return min(starts), to_date(as_of_limit)


# ---------------------------------------------------------------- 단계 보고

def step_result(ok=True, provider=None, rows=0, error=None, **extra):
    """ingest 요약의 한 칸. run.note 에 그대로 실리므로 짧고 사실만 담는다."""
    out = {"ok": bool(ok), "provider": provider, "rows": int(rows or 0), "error": error}
    out.update(extra)
    return out


class Report:
    """어느 데이터셋을 어느 제공자가 채웠는지, 어떤 대체 경로를 탔는지 모은다.

    대체 경로는 결정 8(코스피200)·설계 12장의 대비책에서 벗어난 것이라 **기록이 남아야** 한다.
    나중에 "그날 섹터 분류가 왜 이상한가"를 물었을 때 run.note 만 보고 답할 수 있게 한다.
    """

    def __init__(self):
        self.providers = {}
        self.fallbacks = []

    def used(self, dataset, provider):
        """dataset 을 provider 가 채웠다. 여러 제공자가 나눠 채우면 '+' 로 잇는다."""
        prev = self.providers.get(dataset)
        if prev and provider not in prev.split("+"):
            self.providers[dataset] = f"{prev}+{provider}"
        elif not prev:
            self.providers[dataset] = provider
        return provider

    def fallback(self, reason):
        """기본 경로를 못 써서 대체 경로를 탔다."""
        if reason not in self.fallbacks:
            self.fallbacks.append(reason)
        return reason

    def provider_of(self, dataset):
        return self.providers.get(dataset)

    def merge(self, other):
        for dataset, provider in (other.providers or {}).items():
            self.used(dataset, provider)
        for reason in other.fallbacks or []:
            self.fallback(reason)
        return self


# ---------------------------------------------------------------- 환경 변수

def load_env(cfg=None):
    """저장소 .env 를 os.environ 에 올린다 (이미 있는 값은 덮지 않는다).

    newsgap 수집기와 같은 로더를 쓴다 — 키가 두 곳에 흩어지면 한쪽만 고치는 사고가 난다.
    값은 절대 로그에 찍지 않는다.
    """
    from ...newsgap.ls_client import load_env_file
    from ..config import DEFAULT_CONFIG_PATH
    root = DEFAULT_CONFIG_PATH.parent.parent
    return load_env_file(str(root / ".env"))


def env_value(name, env=None):
    """환경 변수 하나. 주입된 dict 가 있으면 그쪽을 본다 (테스트가 네트워크·비밀 없이 돌게)."""
    src = os.environ if env is None else env
    value = src.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def has_env(*names, env=None):
    return all(env_value(n, env) for n in names)


__all__ = ["KST", "KNOWN_AT_RULES", "SourceError", "LookaheadError", "ConfigKeyError",
           "cfg_get", "sources_cfg", "to_date", "iso_date", "compact_date", "to_kst", "now_kst",
           "wall", "parse_hhmm", "known_date_limit", "guard_dated_rows", "assert_max_date",
           "assert_not_after", "missing_start", "missing_dates", "step_result", "Report",
           "load_env", "env_value", "has_env"]
