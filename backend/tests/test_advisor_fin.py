"""정기보고서 재무 수집과 관찰 요인 stk_earn_growth 테스트 (2026-09-28). **네트워크를 쓰지 않는다.**

확인하는 것:
  - 파서: 금액 문자열, corpCode.xml(ZIP), 주요계정 응답 → 행 (손익계산서만, 중복 당기순이익 한 줄)
  - 시점 처리: 인지 시각 = 공시일 다음 날 00:00, 공시일을 모르면 접수번호 날짜 + 대체 일수,
    as_of 이후에야 알 수 있는 행은 저장하지 않고 읽지도 않는다, 미래 행을 더해도 요인 값이 그대로
  - 증분 수집: 이미 받은 보고서는 다시 묻지 않고, 같은 날 두 번 묻지 않는다. 키 없음·오류 응답에서 죽지 않는다
  - 분기 단독값: 4분기 = 연간 − 3분기 누적, 연결 우선·혼합 금지, 정정본, 오래된 값 결측
  - 요인: (영업이익 − 전년 동기) ÷ 시가총액의 순위, 재무·시가총액 없는 종목은 결측
  - **가중치 0 이면 v0 판단·비중이 요인을 넣기 전과 완전히 같다**
"""
from backend.tests import _safety  # noqa: F401  맨 먼저: 실제 주문·외부 접속 차단 (_safety.py 참고)
import copy
import io
import logging
import tempfile
import unittest
import zipfile
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from backend.advisor import run as run_mod
from backend.advisor.calendar import TradingCalendar
from backend.advisor.config import config_hash, load_config
from backend.advisor.factors import asof, compute, fin, stock
from backend.advisor.factors.registry import load_specs
from backend.advisor.sources import dart_fin
from backend.advisor.sources.base import Report
from backend.advisor.store import Store
from backend.tests.test_advisor_pipeline import MARKET_DATA

FID = "stk_earn_growth"


def setUpModule():
    logging.getLogger("advisor").setLevel(logging.CRITICAL)


# ---------------------------------------------------------------- 고정 응답

def corp_zip(entries):
    """[(corp_code, 이름, 종목 코드)] → corpCode.xml 을 담은 ZIP 바이트 (OpenDART 형식)."""
    body = "".join(
        f"<list><corp_code>{c}</corp_code><corp_name>{n}</corp_name><corp_eng_name>x</corp_eng_name>"
        f"<stock_code>{s}</stock_code><modify_date>20250101</modify_date></list>" for c, n, s in entries)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><result>{body}</result>'.encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("CORPCODE.xml", xml)
    return buf.getvalue()


def multi_items(corp, code, year, reprt, rcept_no, op, cum=None, sales=1000, fs=("CFS", "OFS")):
    """fnlttMultiAcnt 응답 항목 (2026-09-28 실측 응답의 모양 그대로: 금액은 쉼표 문자열)."""
    out = []
    for fs_div in fs:
        def item(sj, name, amount, add=None):
            return {"rcept_no": rcept_no, "reprt_code": reprt, "bsns_year": str(year), "corp_code": corp,
                    "stock_code": code, "fs_div": fs_div, "fs_nm": "x", "sj_div": sj, "sj_nm": "x",
                    "account_nm": name, "thstrm_nm": "x", "thstrm_dt": "x",
                    "thstrm_amount": None if amount is None else f"{amount:,}",
                    "thstrm_add_amount": None if add is None else f"{add:,}", "ord": "1",
                    "currency": "KRW"}
        out += [item("BS", "자산총계", 99999),
                item("IS", "매출액", sales, sales),
                item("IS", "영업이익", op, cum),
                item("IS", "당기순이익(손실)", op // 2),
                item("IS", "당기순이익(손실)", op // 2),       # 실제 응답처럼 두 번 온다
                item("IS", "총포괄손익", 1)]
    return out


def fin_row(code, year, reprt, amount, kt, cum=None, fs="CFS", rcept_no=None, account="op_income"):
    return {"stock_code": code, "corp_code": "C" + code, "bsns_year": year, "reprt_code": reprt,
            "fs_div": fs, "account": account, "amount": amount, "cum_amount": cum,
            "rcept_no": rcept_no or f"{kt[:4]}{kt[5:7]}{kt[8:10]}000001", "rcept_dt": kt[:10],
            "rcept_dt_src": "list", "knowledge_time": kt, "fetched_at": kt}


class FakeDart:
    """corpCode / fnlttMultiAcnt / list.json 을 흉내 낸다. 부른 횟수를 URL 별로 센다."""

    def __init__(self, corps, reports, filings, status="000"):
        self.corps, self.reports, self.filings, self.status = corps, reports, filings, status
        self.calls = {"corp": 0, "multi": 0, "list": 0}
        self.asked = []

    def __call__(self, url, params):
        if url.endswith("corpCode.xml"):
            self.calls["corp"] += 1
            return corp_zip(self.corps)
        if url.endswith("fnlttMultiAcnt.json"):
            self.calls["multi"] += 1
            if self.status != "000":
                return {"status": self.status, "message": "사용한도 초과"}
            corps = params["corp_code"].split(",")
            self.asked.append((params["bsns_year"], params["reprt_code"], tuple(corps)))
            items = [it for (y, r, c), rows in self.reports.items()
                     if str(y) == params["bsns_year"] and r == params["reprt_code"] and c in corps
                     for it in rows]
            return {"status": "000", "list": items} if items else {"status": "013", "message": "없음"}
        if url.endswith("list.json"):
            self.calls["list"] += 1
            lo, hi = params["bgn_de"], params["end_de"]
            rows = [{"rcept_no": no, "rcept_dt": dt, "stock_code": "", "corp_code": "", "report_nm": "보고서"}
                    for no, dt in self.filings.items() if lo <= dt <= hi]
            return {"status": "000", "total_page": 1, "list": rows} if rows else {"status": "013"}
        raise AssertionError(f"예상하지 못한 URL: {url}")


def base_cfg():
    return copy.deepcopy(load_config())


# ---------------------------------------------------------------- 파서

class ParserTest(unittest.TestCase):
    def setUp(self):
        self.cfg = base_cfg()

    def test_amounts(self):
        self.assertEqual(dart_fin.parse_amount("1,234,000"), 1234000.0)
        self.assertEqual(dart_fin.parse_amount("-5,000"), -5000.0)
        self.assertEqual(dart_fin.parse_amount("(5,000)"), -5000.0)
        self.assertIsNone(dart_fin.parse_amount(""))
        self.assertIsNone(dart_fin.parse_amount("-"))
        self.assertIsNone(dart_fin.parse_amount(None))

    def test_corp_codes_from_zip_skip_unlisted(self):
        raw = corp_zip([("00126380", "삼성전자", "005930"), ("00434003", "다코", " ")])
        got = dart_fin.parse_corp_codes(raw)
        self.assertEqual(set(got), {"005930"}, "종목 코드가 빈 비상장 회사는 뺀다")
        self.assertEqual(got["005930"][0], "00126380")

    def test_rows_keep_income_statement_only_and_dedupe(self):
        items = multi_items("00126380", "005930", 2025, "11012", "20250814003156", 4676, cum=11361)
        rows = dart_fin.to_fin_rows(self.cfg, items)
        keys = sorted((r["fs_div"], r["account"]) for r in rows)
        self.assertEqual(keys, sorted((fs, a) for fs in ("CFS", "OFS")
                                      for a in ("revenue", "op_income", "net_income")),
                         "자산총계·총포괄손익은 빼고, 두 번 오는 당기순이익은 한 줄")
        op = [r for r in rows if r["account"] == "op_income" and r["fs_div"] == "CFS"][0]
        self.assertEqual((op["amount"], op["cum_amount"]), (4676.0, 11361.0), "반기 3개월 값과 누적을 그대로")
        self.assertIsNone(op["knowledge_time"], "인지 시각은 공시일을 찾은 뒤에 붙인다")

    def test_account_names_ignore_spaces(self):
        self.assertEqual(dart_fin.account_key(self.cfg, "영업이익(손실)"), "op_income")
        self.assertEqual(dart_fin.account_key(self.cfg, "당기순이익 (손실)"), "net_income")
        self.assertIsNone(dart_fin.account_key(self.cfg, "법인세차감전 순이익"))


# ---------------------------------------------------------------- 시점 규칙

class KnowledgeTimeTest(unittest.TestCase):
    def setUp(self):
        self.cfg = base_cfg()

    def test_known_the_day_after_the_filing_date(self):
        kt, day, src = dart_fin.knowledge_time_of(self.cfg, "20250515001922", "2025-05-15")
        self.assertEqual((kt, day, src), ("2025-05-16T00:00:00.000", "2025-05-15", None))

    def test_after_hours_filing_uses_the_later_publication_date(self):
        # 금요일 밤 접수 → 월요일 공시. 접수번호 날짜(금)가 아니라 공시일(월)이 기준이다.
        kt, _, _ = dart_fin.knowledge_time_of(self.cfg, "20260918000900", "2026-09-21")
        self.assertEqual(kt, "2026-09-22T00:00:00.000")

    def test_unknown_filing_date_falls_back_conservatively(self):
        kt, day, src = dart_fin.knowledge_time_of(self.cfg, "20250515001922", None)
        self.assertEqual((day, src), ("2025-05-22", dart_fin.SRC_FALLBACK), "접수번호 날짜 + 7일")
        self.assertEqual(kt, "2025-05-23T00:00:00.000")

    def test_list_windows_respect_the_three_month_limit(self):
        days = [date(2025, 5, 15), date(2025, 5, 30), date(2025, 8, 14), date(2025, 11, 14)]
        wins = dart_fin.list_windows(days, date(2026, 1, 1))
        self.assertTrue(all((e - b).days <= 89 for b, e in wins))
        self.assertTrue(all(any(b <= d <= e for b, e in wins) for d in days))
        self.assertTrue(all(any(b <= d and (e - d).days >= dart_fin.LIST_TAIL_DAYS for b, e in wins)
                            for d in days), "장 마감 뒤 접수분(다음 영업일 공시)까지 창에 든다")
        last = dart_fin.list_windows([date(2025, 12, 30)], date(2026, 1, 1))
        self.assertEqual(last, [(date(2025, 12, 30), date(2026, 1, 1))], "기준일을 넘지 않는다")

    def test_reader_never_returns_rows_known_after_as_of(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "a.db")
            store.put_fin_rows([fin_row("A", 2025, "11013", 10.0, "2025-05-16T00:00:00.000"),
                                fin_row("A", 2025, "11012", 20.0, "2025-08-15T00:00:00.000")])
            prelim = asof.fin_reports(store, "2025-08-14T18:30")      # 공시일 당일 저녁 예비 판단
            final = asof.fin_reports(store, "2025-08-15T07:40")       # 다음 날 아침 최종 판단
            self.assertEqual([r["reprt_code"] for r in prelim["A"]], ["11013"])
            self.assertEqual(sorted(r["reprt_code"] for r in final["A"]), ["11012", "11013"])
            store.close()


# ---------------------------------------------------------------- 증분 수집

class SyncTest(unittest.TestCase):
    CORPS = [("C1", "가", "000010"), ("C2", "나", "000020")]

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.dir.name) / "advisor.db")
        self.cfg = base_cfg()
        self.cfg["sources"]["dart_fin_start_year"] = 2025
        reports, filings = {}, {}
        for corp, _, code in self.CORPS:
            for reprt, no, op in (("11013", "20250515000100", 100), ("11012", "20250814000200", 150)):
                rcept_no = no[:-1] + corp[-1]              # 20250515000101, 20250515000102, …
                reports[(2025, reprt, corp)] = multi_items(corp, code, 2025, reprt, rcept_no, op)
                filings[rcept_no] = no[:8]
        # C2 의 반기 보고서는 금요일(8/8) 밤 접수 → 월요일(8/11) 공시
        reports[(2025, "11012", "C2")] = multi_items("C2", "000020", 2025, "11012", "20250808000900", 150)
        filings["20250808000900"] = "20250811"
        filings.pop("20250814000202", None)
        self.http = FakeDart(self.CORPS, reports, filings)
        self.codes = ["000010", "000020"]

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def sync(self, as_of, http=None, **kw):
        return dart_fin.sync(self.store, self.cfg, as_of, self.codes, http=http or self.http,
                             api_key="dummy", **kw)

    def kt(self, code, reprt):
        return self.store.conn.execute(
            "SELECT DISTINCT knowledge_time FROM fin_quarterly WHERE stock_code=? AND reprt_code=?",
            (code, reprt)).fetchall()

    def test_first_sync_stores_only_what_was_known(self):
        got = self.sync(datetime(2025, 8, 14, 18, 30))
        self.assertEqual(self.http.calls["corp"], 1)
        self.assertEqual(self.http.calls["multi"], 2, "기간이 끝난 1분기·반기 보고서, 100개씩 한 번")
        self.assertEqual([tuple(r) for r in self.kt("000010", "11013")], [("2025-05-16T00:00:00.000",)])
        self.assertEqual([tuple(r) for r in self.kt("000020", "11012")], [("2025-08-12T00:00:00.000",)],
                         "금요일 밤 접수분은 월요일 공시 → 화요일 0시부터")
        self.assertEqual(self.kt("000010", "11012"), [],
                         "8/14 공시분은 8/15 0시에야 알 수 있으므로 8/14 저녁 수집에서는 저장하지 않는다")
        self.assertEqual(got["fallback_dates"], 0)

    def test_second_sync_asks_only_what_is_missing(self):
        self.sync(datetime(2025, 8, 14, 18, 30))
        before = dict(self.http.calls)
        self.sync(datetime(2025, 8, 14, 18, 40))
        self.assertEqual(self.http.calls["multi"], before["multi"], "같은 날에는 다시 묻지 않는다")
        self.sync(datetime(2025, 8, 15, 7, 40))
        asked = self.http.asked[-1]
        self.assertEqual(asked[:2], ("2025", "11012"))
        self.assertEqual(asked[2], ("C1",), "행이 이미 있는 종목(C2)은 묻지 않는다")
        self.assertEqual(len(self.kt("000010", "11012")), 1, "다음 날 아침에는 저장된다")
        self.assertEqual(self.http.calls["corp"], 1, "고유번호 목록은 모르는 종목이 없으면 다시 받지 않는다")

    def test_unknown_filing_date_uses_the_fallback(self):
        self.http.filings = {}
        got = self.sync(datetime(2025, 9, 30, 18, 30))
        self.assertGreater(got["fallback_dates"], 0)
        src = {r[0] for r in self.store.conn.execute("SELECT rcept_dt_src FROM fin_quarterly")}
        self.assertEqual(src, {dart_fin.SRC_FALLBACK})

    def test_disclosure_table_is_used_before_the_list_api(self):
        for no in ("20250515000101", "20250515000102", "20250814000201", "20250808000900"):
            self.store.upsert_disclosure({"rcept_no": no, "stock_code": "x", "corp_name": None,
                                          "report_nm": "분기보고서", "rcept_dt": f"{no[:4]}-{no[4:6]}-{no[6:8]}",
                                          "first_seen_at": None, "first_seen_src": None, "ls_realkey": None,
                                          "kind": None, "ratio": None, "ratio_ok": None, "body_src": None})
        self.sync(datetime(2025, 9, 30, 18, 30))
        self.assertEqual(self.http.calls["list"], 0, "공시 폴러가 받아 둔 공시일이 있으면 목록을 부르지 않는다")

    def test_no_key_and_error_status_do_not_raise(self):
        got = dart_fin.sync(self.store, self.cfg, datetime(2025, 9, 30, 18, 30), self.codes,
                            http=self.http, env={}, report=Report())
        self.assertEqual((got["rows"], got["calls"]), (0, 0))
        broken = FakeDart(self.CORPS, {}, {}, status="020")
        report = Report()
        got = self.sync(datetime(2025, 9, 30, 18, 30), http=broken, report=report)
        self.assertEqual(got["rows"], 0)
        self.assertIn("status=020", got["note"])
        self.assertIn("dart_fin_status", report.fallbacks)

    def test_call_cap_is_respected(self):
        self.cfg["sources"]["dart_fin_max_calls"] = 2        # 고유번호 1 + 주요계정 1
        got = self.sync(datetime(2025, 9, 30, 18, 30))
        self.assertLessEqual(got["calls"], 2)
        self.assertIn("호출 상한", got["note"])


# ---------------------------------------------------------------- 분기 단독값

class StandaloneTest(unittest.TestCase):
    def test_q4_is_annual_minus_q3_cumulative(self):
        rows = [fin_row("A", 2024, "11014", 30.0, "2024-11-15T00:00:00.000", cum=60.0),
                fin_row("A", 2024, "11011", 100.0, "2025-03-15T00:00:00.000")]
        s = fin.standalone_quarters(fin.latest_by_report(rows, "op_income"), "CFS")
        self.assertEqual(s[(2024, 4)], 40.0)
        self.assertEqual(s[(2024, 3)], 30.0, "3분기는 3개월 값을 그대로")

    def test_q4_falls_back_to_the_sum_of_quarters(self):
        rows = [fin_row("A", 2024, r, v, "2024-11-15T00:00:00.000") for r, v in
                (("11013", 10.0), ("11012", 20.0), ("11014", 30.0))]
        rows.append(fin_row("A", 2024, "11011", 100.0, "2025-03-15T00:00:00.000"))
        s = fin.standalone_quarters(fin.latest_by_report(rows, "op_income"), "CFS")
        self.assertEqual(s[(2024, 4)], 40.0)

    def test_q4_missing_without_q3(self):
        rows = [fin_row("A", 2024, "11011", 100.0, "2025-03-15T00:00:00.000")]
        s = fin.standalone_quarters(fin.latest_by_report(rows, "op_income"), "CFS")
        self.assertNotIn((2024, 4), s)

    def test_consolidated_first_and_never_mixed(self):
        rows = [fin_row("A", 2024, "11013", 10.0, "2024-05-16T00:00:00.000", fs="CFS"),
                fin_row("A", 2024, "11013", 8.0, "2024-05-16T00:00:00.000", fs="OFS"),
                fin_row("A", 2025, "11013", 15.0, "2025-05-16T00:00:00.000", fs="CFS"),
                fin_row("A", 2025, "11013", 9.0, "2025-05-16T00:00:00.000", fs="OFS")]
        got = fin.yoy_change(rows, "op_income", date(2025, 6, 1))
        self.assertEqual((got["fs_div"], got["delta"]), ("CFS", 5.0))
        # 전년에는 별도만 있었다 → 연결과 별도를 섞지 않고 별도끼리 비교한다
        got = fin.yoy_change([r for r in rows if not (r["bsns_year"] == 2024 and r["fs_div"] == "CFS")],
                             "op_income", date(2025, 6, 1))
        self.assertEqual((got["fs_div"], got["delta"]), ("OFS", 1.0))

    def test_latest_correction_wins_and_stale_is_missing(self):
        rows = [fin_row("A", 2024, "11013", 10.0, "2024-05-16T00:00:00.000"),
                fin_row("A", 2025, "11013", 15.0, "2025-05-16T00:00:00.000", rcept_no="20250515000001"),
                fin_row("A", 2025, "11013", 12.0, "2025-07-02T00:00:00.000", rcept_no="20250701000001")]
        self.assertEqual(fin.yoy_change(rows, "op_income", date(2025, 7, 10))["delta"], 2.0, "정정본")
        self.assertIsNone(fin.yoy_change(rows, "op_income", date(2026, 1, 10), max_age_days=200),
                          "직전 분기가 200일 넘게 묵으면 결측")

    def test_no_prior_year_is_missing(self):
        rows = [fin_row("A", 2025, "11013", 15.0, "2025-05-16T00:00:00.000")]
        self.assertIsNone(fin.yoy_change(rows, "op_income", date(2025, 6, 1)))

    def test_growth_rate_is_reference_only(self):
        self.assertAlmostEqual(fin.growth_rate({"cur": -50.0, "prev": -100.0}), 0.5,
                               "적자 축소는 + (|기준값| 으로 나눈다)")
        self.assertIsNone(fin.growth_rate({"cur": 5.0, "prev": 0.0}))


# ---------------------------------------------------------------- 요인

class FactorTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.dir.name) / "advisor.db")
        self.cfg = base_cfg()
        self.spec = load_specs(self.cfg)[FID]
        self.uni = {"stocks": {c: {} for c in ("A", "B", "C", "D")}}
        rows = []
        for code, prev, q3, annual, cum3 in (("A", 10.0, 30.0, 100.0, 60.0), ("B", 10.0, 30.0, 50.0, 60.0),
                                              ("C", 10.0, 30.0, 70.0, 60.0)):
            rows += [fin_row(code, 2024, "11014", prev, "2024-11-15T00:00:00.000", cum=prev * 3),
                     fin_row(code, 2024, "11011", prev * 4, "2025-03-15T00:00:00.000"),
                     fin_row(code, 2025, "11014", q3, "2025-11-15T00:00:00.000", cum=cum3),
                     fin_row(code, 2025, "11011", annual, "2026-03-21T00:00:00.000")]
        self.store.put_fin_rows(rows)
        self.store.put_flows([{"code": c, "date": "2026-03-19", "foreign_net": 0, "inst_net": 0,
                               "mktcap": 1000.0} for c in ("A", "B", "D")])
        self.store.commit()

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def factor(self, as_of):
        return stock.stk_earn_growth(self.store, self.cfg, self.spec, as_of, None, self.uni)

    def test_value_rank_and_missing(self):
        got = self.factor("2026-03-21T07:40")
        # Q4 2025 = 연간 − 3분기 누적: A 40, B −10 / Q4 2024 = 40 − 30 = 10
        self.assertAlmostEqual(got["A"][0], (40.0 - 10.0) / 1000.0)
        self.assertAlmostEqual(got["B"][0], (-10.0 - 10.0) / 1000.0)
        self.assertEqual(got["C"], (None, 0.0, True), "시가총액을 모르면 결측")
        self.assertEqual(got["D"], (None, 0.0, True), "재무가 없으면 결측")
        self.assertAlmostEqual(got["A"][1], 0.5)
        self.assertAlmostEqual(got["B"][1], -0.5)

    def test_before_the_annual_report_it_uses_q3(self):
        got = self.factor("2026-03-20T18:30")          # 사업보고서 공시일 당일 저녁 → 아직 3분기
        self.assertAlmostEqual(got["A"][0], (30.0 - 10.0) / 1000.0)
        self.assertAlmostEqual(got["B"][0], (30.0 - 10.0) / 1000.0)

    def test_future_rows_do_not_change_the_value(self):
        before = self.factor("2026-03-21T07:40")
        self.store.put_fin_rows([fin_row("A", 2026, "11013", 999.0, "2026-05-16T00:00:00.000"),
                                 fin_row("B", 2025, "11011", 5.0, "2026-04-02T00:00:00.000",
                                         rcept_no="20260401000009")])
        self.store.put_flows([{"code": "A", "date": "2026-03-25", "foreign_net": 0, "inst_net": 0,
                               "mktcap": 1.0}])
        self.assertEqual(self.factor("2026-03-21T07:40"), before, "나중 공시·나중 시가총액이 새면 안 된다")

    def test_compute_all_has_a_row_per_stock(self):
        rows = compute.compute_all(self.store, self.cfg, {FID: self.spec}, None, "2026-03-21T07:40", "final",
                                   {"stocks": self.uni["stocks"], "sectors": []})
        self.assertEqual(sorted(r["entity"] for r in rows), ["A", "B", "C", "D"])


# ---------------------------------------------------------------- 가중치 0 → 비중 불변

class WeightZeroTest(unittest.TestCase):
    """관찰 요인을 넣어도(가중치 0) v0 판단이 한 자리도 바뀌지 않는다 — 결정 6 의 전제."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.m = MARKET_DATA
        self.day = self.m.days[-1]

    def tearDown(self):
        self.dir.cleanup()

    def cfg(self, name, with_factor=True, weight=0):
        cfg = base_cfg()
        cfg["paths"]["db"] = str(self.root / f"{name}.db")
        cfg["paths"]["ledger"] = str(self.root / name / "ledger.jsonl")
        cfg["paths"]["newsgap_db"] = str(self.root / "newsgap.db")
        if not with_factor:
            cfg["factors"].pop(FID)
        else:
            cfg["factors"][FID]["weight"] = weight
        return cfg

    def decide(self, cfg):
        store = Store(cfg["paths"]["db"])
        self.addCleanup(store.close)
        self.m.write(store)
        # 합성 종목 전부에 재무를 넣는다 (종목마다 다른 이익 변화 → 순위가 생긴다)
        rows = []
        for i, code in enumerate(self.m.codes):
            rows += [fin_row(code, 2025, "11012", 100.0, "2025-08-15T00:00:00.000"),
                     fin_row(code, 2026, "11012", 100.0 + 37.0 * ((i * 7) % 11) - 150.0,
                             "2026-08-15T00:00:00.000")]
        store.put_fin_rows(rows)
        store.commit()
        run_id = run_mod.run_once(store, cfg, "final", self.day, no_ingest=True)
        conn = store.conn
        weights = sorted(tuple(r) for r in conn.execute(
            "SELECT asset, role, weight FROM target_weight WHERE run_id=? AND variant='v0'", (run_id,)))
        decision = tuple(conn.execute(
            "SELECT market_score, risk_weight FROM decision WHERE run_id=? AND variant='v0'",
            (run_id,)).fetchone())
        comps = sorted(tuple(r) for r in conn.execute(
            "SELECT entity, layer, base_score, final_score FROM composite WHERE run_id=? AND variant='v0'",
            (run_id,)))
        fv = conn.execute("SELECT COUNT(*) FROM factor_value WHERE run_id=? AND factor_id=? AND missing=0",
                          (run_id, FID)).fetchone()[0]
        return weights, decision, comps, fv

    def test_weight_zero_leaves_the_decision_unchanged(self):
        base_w, base_d, base_c, base_fv = self.decide(self.cfg("without", with_factor=False))
        w, d, c, fv = self.decide(self.cfg("with", with_factor=True, weight=0))
        self.assertEqual(base_fv, 0)
        self.assertGreater(fv, 0, "관찰 요인 점수는 저장된다 (결정 6: 사후 재평가의 재료)")
        self.assertEqual(w, base_w, "목표 비중이 한 자리도 바뀌면 안 된다")
        self.assertEqual(d, base_d)
        self.assertEqual(c, base_c, "종합 점수도 같다")
        self.assertNotEqual(config_hash(self.cfg("x", False)), config_hash(self.cfg("x", True)),
                            "설정 지문은 바뀐다 (의도된 변경)")

    def test_weight_one_would_change_scores(self):
        """대조군: 가중치를 1로 올리면 종합 점수가 달라진다 — 위 검사가 헛돌지 않는다는 증거."""
        _, _, base_c, _ = self.decide(self.cfg("zero", with_factor=True, weight=0))
        _, _, c, _ = self.decide(self.cfg("one", with_factor=True, weight=1))
        self.assertNotEqual(c, base_c)


if __name__ == "__main__":
    unittest.main()
