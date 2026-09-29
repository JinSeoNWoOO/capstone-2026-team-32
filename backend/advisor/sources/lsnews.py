"""수집기 DB(`data/newsgap.db`) 읽기 — 공시 속보와 뉴스 (설계 2.1: **읽기만 한다**).

LS 실시간 뉴스에는 공시 원문 채널(`source_id='15'`)이 있고, 거래소 배포 직후에 도착한다.
DART 목록 폴러는 10분 간격이라, 같은 공시라도 **LS 쪽 수신 시각이 보통 더 빠르다**. 설계 5.4의
"둘 중 빠른 쪽"이 성립하려면 두 경로가 같은 공시를 같은 `rcept_no` 로 가리켜야 하는데, LS 속보에는
접수번호가 없다. 그래서 **제목으로 맞춘다**.

## 맞추는 규칙

같은 종목 코드 + 같은 날짜 + 정규화한 제목의 포함 관계. 정규화는 공백·문장부호를 모두 지우고
회사 이름을 떼는 것이다. LS 제목은 `"(주)브이씨 주식소각 결정"` 처럼 회사 이름이 앞에 붙고,
DART 의 `report_nm` 은 `"주식소각결정"` 처럼 이름이 없다.

- 맞는 DART 행이 있으면 그 행의 `first_seen_at` 을 LS 수신 시각으로 **당긴다**(빠른 쪽 규칙은
  `store.upsert_disclosure` 가 지킨다) 그리고 `ls_realkey` 를 붙인다.
- 못 맞추면 **합성 접수번호** `LS:<realkey>` 로 새 행을 만든다. 공시가 있었다는 사실 자체를 잃지
  않기 위해서다 (DART 키가 없을 때는 이쪽이 유일한 경로다).
- 나중에 DART 행이 도착해 맞춰지면 합성 행은 지운다 — 같은 공시가 두 줄이면 `stk_disclosure`
  점수가 두 번 더해진다.

## 본문에 대해

2026-09-22 확인: `news.body` 는 공시 행에서도 **전부 비어 있다**. 그래서 공시 채점은 제목만으로
하는 것이 정상 경로다 (설계 5.4의 4번: 본문이 없으면 제목과 추출 수치로 채점).
"""
import os
import re
import sqlite3

from datetime import timedelta

from .base import (Report, iso_date, sources_cfg, to_date, to_kst, wall)
from ..config import resolve_path
from ...newsgap.disclosure import kind_of

# LS 실시간 뉴스의 공시 원문 채널.
DISCLOSURE_SOURCE_ID = "15"
FIRST_SEEN_SRC = "ls_news"
SYNTHETIC_PREFIX = "LS:"

# 제목 대조용 정규화: 공백과 문장부호를 전부 지운다. 같은 공시라도 채널마다 가운뎃점·괄호·
# 공백이 다르게 들어오기 때문이다 ("단일판매ㆍ공급계약체결" vs "단일판매·공급계약 체결").
_PUNCT = re.compile(r"[\s·ㆍ・ㆍ·,.\-_/\\()\[\]{}<>「」『』【】〔〕"
                    r"\"'‘’“”:;!?~`@#$%^&*+=|]+")
_CORP_MARKS = re.compile(r"㈜|\(주\)|\(유\)|주식회사")


def normalise_title(text):
    """제목 대조용 문자열. 공백·문장부호·법인 표기를 지우고 소문자로."""
    cleaned = _CORP_MARKS.sub("", str(text or ""))
    return _PUNCT.sub("", cleaned).lower()


def strip_company(title, corp_name):
    """정규화한 제목에서 회사 이름을 뗀다. 이름을 모르면 그대로 둔다."""
    norm = normalise_title(title)
    corp = normalise_title(corp_name)
    if corp and corp in norm:
        return norm.replace(corp, "", 1)
    return norm


def titles_match(ls_title, report_nm, corp_name=None):
    """LS 속보 제목과 DART 보고서명이 같은 공시를 가리키는가.

    포함 관계로 본다. LS 제목은 괄호 안 부연이 더 붙는 일이 많고("...(종속회사의 주요경영사항)"),
    DART 쪽이 더 긴 경우도 있어 양쪽 방향을 다 본다. 너무 짧은 제목은 우연히 겹치므로 제외한다.
    """
    left = strip_company(ls_title, corp_name)
    right = normalise_title(report_nm)
    if len(left) < 4 or len(right) < 4:
        return False
    return right in left or left in right


# ---------------------------------------------------------------- newsgap.db 열기

def open_newsgap(cfg):
    """수집기 DB 를 읽기 전용으로 연다. 파일이 없으면 None (수집기를 안 돌린 개발 PC)."""
    path = str(resolve_path(cfg, "newsgap_db"))
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _window(cfg, as_of, hours, key):
    """[시작, 끝] KST 벽시계 문자열. 끝은 기준 시각 자신이다 (설계 2.3)."""
    end = to_kst(as_of)
    span = float(hours if hours is not None else sources_cfg(cfg, key))
    return wall(end - timedelta(hours=span)), wall(end)


# ---------------------------------------------------------------- (a) 공시 속보

def disclosure_flashes(cfg, as_of, hours=None, conn=None):
    """창 안의 공시 속보 행 목록. 종목 코드가 빈 것은 버린다 (종목에 붙일 수 없으면 점수에 못 쓴다)."""
    owned = conn is None
    conn = conn or open_newsgap(cfg)
    if conn is None:
        return []
    start, end = _window(cfg, as_of, hours, "lsnews_window_hours")
    try:
        rows = conn.execute(
            "SELECT realkey, recv_wall, code, source_id, title FROM news "
            "WHERE source_id=? AND code IS NOT NULL AND code!='' "
            "AND recv_wall>=? AND recv_wall<=? ORDER BY recv_wall",
            (DISCLOSURE_SOURCE_ID, start, end)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if owned:
            conn.close()
    return [dict(r) for r in rows]


def match_dart_row(store, code, day, title):
    """같은 종목·같은 날짜의 DART 행 중 제목이 맞는 것의 접수번호. 없으면 None."""
    candidates = store.conn.execute(
        "SELECT rcept_no, corp_name, report_nm FROM disclosure "
        "WHERE stock_code=? AND rcept_dt=? AND rcept_no NOT LIKE ?",
        (code, iso_date(day), SYNTHETIC_PREFIX + "%")).fetchall()
    for row in candidates:
        if titles_match(title, row["report_nm"], row["corp_name"]):
            return row["rcept_no"]
    return None


def sync_disclosures(store, cfg, as_of, hours=None, conn=None, report=None):
    """공시 속보를 disclosure 에 반영한다. 반환 {"rows", "matched", "synthetic", "merged"}.

    맞춘 행은 `first_seen_at` 을 LS 수신 시각으로 당기고(MIN 규칙은 store 가 지킨다) `ls_realkey`
    를 붙인다. 못 맞춘 것만 합성 접수번호로 새로 만든다.
    """
    report = report if report is not None else Report()
    flashes = disclosure_flashes(cfg, as_of, hours=hours, conn=conn)
    matched = synthetic = merged = 0
    for flash in flashes:
        code = str(flash["code"]).strip()
        recv = str(flash["recv_wall"])
        day = to_date(recv[:10])
        title = flash.get("title") or ""
        rcept_no = match_dart_row(store, code, day, title)
        if rcept_no:
            store.upsert_disclosure({
                "rcept_no": rcept_no, "stock_code": code, "corp_name": None,
                "report_nm": None, "rcept_dt": None,
                "first_seen_at": recv, "first_seen_src": FIRST_SEEN_SRC,
                "ls_realkey": flash["realkey"], "kind": None, "ratio": None,
                "ratio_ok": None, "body_src": None})
            matched += 1
            # 앞선 실행에서 못 맞춰 만들어 둔 합성 행이 있으면 지운다 (같은 공시가 두 줄이면
            # stk_disclosure 점수가 두 번 더해진다).
            if _drop_synthetic(store, flash["realkey"]):
                merged += 1
        else:
            store.upsert_disclosure({
                "rcept_no": SYNTHETIC_PREFIX + str(flash["realkey"]),
                "stock_code": code, "corp_name": None,
                "report_nm": " ".join(title.split()) or None, "rcept_dt": iso_date(day),
                "first_seen_at": recv, "first_seen_src": FIRST_SEEN_SRC,
                "ls_realkey": flash["realkey"], "kind": kind_of(title), "ratio": None,
                "ratio_ok": None, "body_src": FIRST_SEEN_SRC})
            synthetic += 1
    if flashes:
        report.used("lsnews", "newsgap_db")
    if synthetic:
        report.fallback("disclosure_synthetic_rcept_no")
    return {"rows": len(flashes), "matched": matched, "synthetic": synthetic, "merged": merged,
            "provider": "newsgap_db"}


def _drop_synthetic(store, realkey):
    """합성 행을 지운다. store 에 도우미가 있으면 그것을, 없으면 직접 지운다."""
    rcept_no = SYNTHETIC_PREFIX + str(realkey)
    dropper = getattr(store, "drop_disclosure", None)
    if callable(dropper):
        return bool(dropper(rcept_no))
    cur = store.conn.execute("DELETE FROM disclosure WHERE rcept_no=?", (rcept_no,))
    return bool(cur.rowcount)


# ---------------------------------------------------------------- (b) 뉴스 읽기

def recent_news(cfg, as_of, hours=None, conn=None, exclude_disclosure=False):
    """창 안의 뉴스를 (realkey, recv_wall, code, source_id, title) 로 흘려 준다.

    나중에 붙을 LLM 뉴스 분류기(`llm/news_risk.py`)가 쓴다. 건수가 많을 수 있어 목록이 아니라
    발생자다 — 호출자가 예산에 맞춰 끊어 쓰면 된다.
    """
    owned = conn is None
    conn = conn or open_newsgap(cfg)
    if conn is None:
        return
    start, end = _window(cfg, as_of, hours, "news_window_hours")
    sql = ("SELECT realkey, recv_wall, code, source_id, title FROM news "
           "WHERE recv_wall>=? AND recv_wall<=?")
    params = [start, end]
    if exclude_disclosure:
        sql += " AND source_id!=?"
        params.append(DISCLOSURE_SOURCE_ID)
    sql += " ORDER BY recv_wall"
    try:
        for row in conn.execute(sql, params):
            yield (row["realkey"], row["recv_wall"], row["code"], row["source_id"], row["title"])
    except sqlite3.Error:
        return
    finally:
        if owned:
            conn.close()


__all__ = ["DISCLOSURE_SOURCE_ID", "FIRST_SEEN_SRC", "SYNTHETIC_PREFIX", "normalise_title",
           "strip_company", "titles_match", "open_newsgap", "disclosure_flashes",
           "match_dart_row", "sync_disclosures", "recent_news"]
