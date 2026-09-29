"""판단 지원(advisor) 모드 기반 계층 단위 테스트. 네트워크를 쓰지 않는다.

확인하는 것:
  - 설정: 로드·해시 안정성·계약 위반 검출 (결정 6의 '설정 버전'이 성립하려면 해시가 안정적이어야 한다)
  - 저장: 왕복, knowledge_time 이 뒤로 가지 않음, 공시 first_seen_at 은 빠른 쪽, 읽기 전용 열기
  - 달력: 설정 휴장일과 데이터로 판정한 과거 거래일 (데이터가 규칙을 이긴다)
  - 눈금 변환: 동점·결측·대상 1개, 백분위 최소 표본
  - 계층 점수: 결측·가중치 0 요인을 분모에서 뺀다
  - 비중: 합 1, 상한 초과분 core 로, 0 이하 점수 제외, 보유 이력(exit_rank), halt·거부 제외, 리밸런싱 기준
"""
from backend.tests import _safety  # noqa: F401  맨 먼저: 실제 주문·외부 접속 차단 (_safety.py 참고)
import copy
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from backend.advisor import allocate, combine
from backend.advisor.calendar import TradingCalendar
from backend.advisor.config import (ConfigError, config_hash, config_yaml_text, factor_ids,
                                    llm_cost_usd, load_config, resolve_path, validate_config)
from backend.advisor.factors import normalize as nz
from backend.advisor.factors.registry import load_specs, specs_for
from backend.advisor.store import Store

PRICE_COLS = ("code", "date", "open", "high", "low", "close", "volume", "value", "adj_close")


def price(code, day, close, **over):
    row = dict(zip(PRICE_COLS, (code, day, close, close, close, close, 100.0, 100.0, close)))
    row.update(over)
    return row


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_loads_contract_keys(self):
        for key in ("paths", "schedule", "calendar", "universe", "factors", "normalize", "combine",
                    "allocate", "costs", "scoring", "reeval", "llm", "disclosure_rubric", "risk_flags"):
            self.assertIn(key, self.cfg)
        self.assertEqual(self.cfg["config_note"], "v0")
        self.assertEqual(len(self.cfg["factors"]), 11,
                         "코드 요인 8개 + LLM 요인 2개 (결정 12) + 관찰 요인 stk_earn_growth (2026-09-28)")
        self.assertEqual(self.cfg["factors"]["stk_earn_growth"]["weight"], 0,
                         "재무 요인은 관찰 요인으로만 들어왔다 — 가중치는 재평가 뒤 사람이 정한다 (결정 6)")
        self.assertEqual(sum(1 for f in self.cfg["factors"].values() if f["weight"] == 2), 2,
                         "가중치 2는 근거가 '강함'인 두 요인에만")
        self.assertTrue(all(f.get("source") for f in self.cfg["factors"].values()),
                        "요인마다 출처를 한 줄씩 남긴다 (결정 6)")

    def test_hash_is_stable_and_sensitive(self):
        self.assertEqual(config_hash(self.cfg), config_hash(load_config()))
        self.assertEqual(len(config_hash(self.cfg)), 12)
        changed = copy.deepcopy(self.cfg)
        changed["allocate"]["risk_slope"] = 0.5
        self.assertNotEqual(config_hash(self.cfg), config_hash(changed))
        # 키 순서만 다른 같은 설정은 같은 해시여야 한다 (주석·순서 변경으로 실행 기록이 쪼개지지 않게)
        reordered = {k: self.cfg[k] for k in reversed(list(self.cfg))}
        self.assertEqual(config_hash(self.cfg), config_hash(reordered))

    def test_yaml_text_and_path(self):
        self.assertIn("config_note", config_yaml_text())
        self.assertTrue(str(resolve_path(self.cfg, "db")).endswith("advisor.db"))
        self.assertTrue(Path(resolve_path(self.cfg, "ledger")).is_absolute())

    def test_factor_ids_honours_stage_and_layer(self):
        self.assertIn("mkt_overnight", factor_ids(self.cfg, layer="market", stage="final"))
        self.assertNotIn("mkt_overnight", factor_ids(self.cfg, layer="market", stage="prelim"),
                         "밤사이 요인은 예비 단계에서 결측이다 (결정 11)")
        self.assertEqual(factor_ids(self.cfg, layer="stock"),
                         ["stk_high52", "stk_flow", "stk_earn_growth", "stk_disclosure"])

    def test_missing_key_raises(self):
        broken = copy.deepcopy(self.cfg)
        del broken["allocate"]["stock_cap"]
        with self.assertRaises(ConfigError) as ctx:
            validate_config(broken)
        self.assertIn("stock_cap", str(ctx.exception))

        broken = copy.deepcopy(self.cfg)
        del broken["costs"]
        with self.assertRaises(ConfigError):
            validate_config(broken)

    def test_bad_weight_transform_layer_raise(self):
        for field, value in (("weight", 3), ("transform", "zscore"), ("layer", "macro"), ("sign", 0)):
            broken = copy.deepcopy(self.cfg)
            broken["factors"]["stk_flow"][field] = value
            with self.assertRaises(ConfigError, msg=f"{field}={value} 는 거부돼야 한다"):
                validate_config(broken)

    def test_unknown_stage_raises(self):
        broken = copy.deepcopy(self.cfg)
        broken["factors"]["mkt_overnight"]["stages"] = ["intraday"]
        with self.assertRaises(ConfigError):
            validate_config(broken)

    def test_llm_cost_tolerates_missing_price(self):
        blank = copy.deepcopy(self.cfg)
        blank["llm"]["price_per_mtok"]["gemini-3.5-flash"] = {"in": None, "out": None}
        self.assertIsNone(llm_cost_usd(blank, "gemini-3.5-flash", 1000, 1000),
                          "단가가 비어 있으면 0 이 아니라 None (예산 검사가 거짓말하지 않게)")
        self.assertIsNone(llm_cost_usd(self.cfg, "없는모델", 10, 10))
        filled = copy.deepcopy(self.cfg)
        filled["llm"]["price_per_mtok"]["gemini-3.5-flash"] = {"in": 1.0, "out": 2.0}
        self.assertAlmostEqual(llm_cost_usd(filled, "gemini-3.5-flash", 1_000_000, 500_000), 2.0)
        # 저장소 설정의 단가는 채워져 있다 (2026-09-22 공식 가격표: 입력 1.50 / 출력 9.00)
        self.assertAlmostEqual(llm_cost_usd(self.cfg, "gemini-3.5-flash", 1_000_000, 1_000_000), 10.5)

    def test_load_config_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config("/tmp/advisor-없는파일.yaml")

    def test_registry_specs(self):
        specs = load_specs(self.cfg)
        self.assertEqual(specs["stk_high52"].weight, 2)
        self.assertEqual(specs["stk_high52"].params["lookback"], 250)
        self.assertTrue(specs["stk_disclosure"].llm)
        self.assertFalse(specs["mkt_credit"].used_in_weighting, "관찰 요인은 가중 합산에서 빠진다")
        self.assertTrue(specs["mkt_overnight"].applies_to("final"))
        self.assertFalse(specs["mkt_overnight"].applies_to("prelim"))
        self.assertEqual(set(specs_for(specs, layer="sector")), {"sec_flow", "sec_trend", "news_risk"})


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "sub" / "advisor.db"     # 디렉터리도 만들어야 한다
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_run_roundtrip(self):
        self.store.register_config("cafe12345678", "config_note: v0")
        self.store.register_config("cafe12345678", "덮어쓰지 않는다")
        row = self.store.conn.execute("SELECT * FROM config_version").fetchone()
        self.assertEqual(row["yaml_text"], "config_note: v0")

        rid = self.store.start_run("final", "2026-09-22", config_hash="cafe12345678", note="첫 실행")
        self.store.finish_run(rid, status="OK", llm_used=1)
        got = self.store.latest_run("final")
        self.assertEqual(got["run_id"], rid)
        self.assertEqual(got["status"], "OK")
        self.assertEqual(got["llm_used"], 1)
        self.assertEqual(got["note"], "첫 실행", "finish_run 에 note 를 안 주면 기존 값을 지우지 않는다")
        self.assertIsNotNone(got["finished_at"])

        self.assertIsNone(self.store.latest_run("prelim"))
        self.store.start_run("final", "2026-06-01", mode="replay")
        self.assertEqual(self.store.latest_run("final")["run_id"], rid,
                         "재현 모드 실행은 live 조회에 섞이지 않는다 (설계 7.4)")
        self.assertEqual(self.store.latest_run("final", mode="replay")["as_of"], "2026-06-01")
        self.assertIsNone(self.store.latest_run("final", as_of="2020-01-01"))

    def test_knowledge_time_is_not_pushed_later(self):
        evening, morning = "2026-09-22T18:30:00.000", "2026-09-23T07:40:00.000"
        self.store.put_prices([price("005930", "2026-09-22", 70000.0)], knowledge_time=evening)
        self.store.put_prices([price("005930", "2026-09-22", 70000.0)], knowledge_time=morning)
        row = self.store.conn.execute("SELECT * FROM price_daily").fetchone()
        self.assertEqual(row["knowledge_time"], evening, "같은 값을 다시 받아도 인지 시각은 앞선 것을 지킨다")

        self.store.put_prices([price("005930", "2026-09-22", 70500.0)], knowledge_time=morning)
        row = self.store.conn.execute("SELECT * FROM price_daily").fetchone()
        self.assertEqual((row["close"], row["knowledge_time"]), (70500.0, morning),
                         "값이 실제로 바뀐 정정은 새 인지 시각을 쓴다")

        # 수급·시장 계열도 같은 규칙
        self.store.put_flows([{"code": "005930", "date": "2026-09-22", "foreign_net": 1.0,
                               "inst_net": 2.0, "mktcap": 3.0}], knowledge_time=evening)
        self.store.put_flows([{"code": "005930", "date": "2026-09-22", "foreign_net": 1.0,
                               "inst_net": 2.0, "mktcap": 3.0}], knowledge_time=morning)
        self.assertEqual(self.store.conn.execute("SELECT knowledge_time FROM flow_daily").fetchone()[0], evening)
        self.store.put_market([{"series": "KOSPI", "date": "2026-09-22", "value": 2500.0}], knowledge_time=evening)
        self.store.put_market([{"series": "KOSPI", "date": "2026-09-22", "value": 2500.0}], knowledge_time=morning)
        self.assertEqual(self.store.conn.execute("SELECT knowledge_time FROM market_daily").fetchone()[0], evening)

    def test_disclosure_first_seen_keeps_the_earliest(self):
        dart = {"rcept_no": "20260922000001", "stock_code": "005930", "corp_name": "삼성전자",
                "report_nm": "단일판매ㆍ공급계약체결", "rcept_dt": "20260922",
                "first_seen_at": "2026-09-22T15:00:00.000", "first_seen_src": "dart",
                "ls_realkey": None, "kind": "공급계약", "ratio": 12.5, "ratio_ok": 1, "body_src": None}
        ls = dict(dart, first_seen_at="2026-09-22T14:00:00.000", first_seen_src="ls",
                  ls_realkey="RK1", corp_name=None, kind=None, ratio=None, ratio_ok=None, body_src="ls")
        self.store.upsert_disclosure(dart)
        self.store.upsert_disclosure(ls)
        row = self.store.disclosure("20260922000001")
        self.assertEqual(row["first_seen_at"], "2026-09-22T14:00:00.000", "빠른 쪽이 이긴다 (설계 5.4)")
        self.assertEqual(row["first_seen_src"], "ls")
        self.assertEqual(row["corp_name"], "삼성전자", "나중 행의 NULL 이 기존 값을 지우지 않는다")
        self.assertEqual(row["ls_realkey"], "RK1")

        self.store.upsert_disclosure(dict(dart, first_seen_at="2026-09-22T09:00:00.000", first_seen_src="dart"))
        self.assertEqual(self.store.disclosure("20260922000001")["first_seen_at"], "2026-09-22T09:00:00.000")
        # 늦은 시각으로 다시 들어와도 움직이지 않는다
        self.store.upsert_disclosure(dict(dart, first_seen_at="2026-09-23T07:40:00.000", first_seen_src="dart"))
        self.assertEqual(self.store.disclosure("20260922000001")["first_seen_at"], "2026-09-22T09:00:00.000")

    def test_scores_and_decision_roundtrip(self):
        rid = self.store.start_run("prelim", "2026-09-22")
        self.store.put_factor_values(rid, [
            {"entity": "005930", "factor_id": "stk_high52", "raw_value": 0.98, "score": 0.9, "missing": 0},
            ("000660", "stk_high52", None, 0.0, True),                 # 시퀀스로도 받는다
        ])
        rows = self.store.factor_values(rid)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["entity"], "000660")
        self.assertEqual(rows[0]["missing"], 1, "missing 은 0/1 정수로 저장한다")
        self.assertEqual(self.store.factor_values(rid, entity="005930")[0]["raw_value"], 0.98)

        self.store.put_factor_evidence(rid, [("005930", "stk_disclosure", "disclosure", "2026001", "공급계약 12.5%")])
        self.store.put_risk_flags(rid, [("005930", "spike", "code", "5일 +32%")])
        self.store.put_composites(rid, "v0", [
            {"entity": "005930", "layer": "stock", "base_score": 0.5, "adj": 0.0, "final_score": 0.5,
             "vetoed": False, "adopted_json": None, "rejected_json": None, "reason": None, "call_id": None}])
        self.store.put_decision(rid, "v0", 0.5, 0.7, record_hash="h1", prev_hash=None)
        self.store.put_target_weights(rid, "v0", [
            {"asset": "459580", "role": "cash", "weight": 0.3},
            {"asset": "069500", "role": "core", "weight": 0.7}])
        self.store.commit()

        comp = self.store.conn.execute("SELECT * FROM composite").fetchone()
        self.assertEqual((comp["run_id"], comp["variant"], comp["entity"], comp["layer"]),
                         (rid, "v0", "005930", "stock"))
        self.assertEqual((comp["base_score"], comp["final_score"], comp["vetoed"]), (0.5, 0.5, 0))
        self.assertEqual(self.store.conn.execute("SELECT flag_type FROM risk_flag").fetchone()[0], "spike")
        self.assertEqual(self.store.conn.execute("SELECT summary FROM factor_evidence").fetchone()[0],
                         "공급계약 12.5%")
        dec = self.store.conn.execute("SELECT * FROM decision").fetchone()
        self.assertEqual((dec["variant"], dec["risk_weight"], dec["record_hash"]), ("v0", 0.7, "h1"))
        weights = self.store.target_weights(rid, "v0")
        self.assertEqual({w["asset"]: w["role"] for w in weights}, {"459580": "cash", "069500": "core"})
        # 같은 실행을 다시 써도 행이 늘지 않는다 (재실행이 기록을 중복시키지 않게)
        self.store.put_target_weights(rid, "v0", [{"asset": "459580", "role": "cash", "weight": 0.25}])
        self.assertEqual(len(self.store.target_weights(rid, "v0")), 2)

    def test_price_reads(self):
        for i, day in enumerate(["2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23"]):
            self.store.put_prices([price("005930", day, 1000.0 + i), price("000660", day, 2000.0 + i)])
        rows = self.store.prices("005930", "2026-09-22", 2)
        self.assertEqual([r["date"] for r in rows], ["2026-09-21", "2026-09-22"],
                         "기준일 이후는 절대 넘기지 않고 날짜 오름차순으로 준다")
        self.assertEqual(len(self.store.prices("005930", "2026-09-22", 10)), 3)
        panel = self.store.price_panel("2026-09-22")
        self.assertEqual(set(panel), {"005930", "000660"})
        self.assertEqual(panel["000660"]["close"], 2002.0)
        self.assertEqual(self.store.price_panel("2026-09-24"), {})

    def test_readonly_open(self):
        rid = self.store.start_run("prelim", "2026-09-22")
        self.store.commit()
        ro = Store(self.path, readonly=True)
        try:
            self.assertEqual(ro.latest_run("prelim")["run_id"], rid)
            with self.assertRaises(sqlite3.OperationalError):
                ro.conn.execute("INSERT INTO run(stage) VALUES('x')")
            with self.assertRaises(sqlite3.OperationalError):
                ro.put_prices([price("005930", "2026-09-22", 1.0)])
        finally:
            ro.close()

    def test_bad_row_length(self):
        rid = self.store.start_run("prelim", "2026-09-22")
        with self.assertRaises(ValueError):
            self.store.put_factor_values(rid, [("005930", "stk_flow")])


class CalendarTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.dir.name) / "advisor.db")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_rule_based_days(self):
        cal = TradingCalendar(self.cfg)
        self.assertTrue(cal.is_trading_day("2026-09-22"))              # 화요일
        self.assertTrue(cal.is_trading_day(date(2026, 9, 23)))         # date 객체도 받는다
        self.assertFalse(cal.is_trading_day("2026-09-24"), "추석 휴장")
        self.assertFalse(cal.is_trading_day("2026-09-25"), "추석 휴장")
        self.assertFalse(cal.is_trading_day("2026-09-26"), "토요일")
        self.assertFalse(cal.is_trading_day("2026-09-27"), "일요일")
        self.assertFalse(cal.is_trading_day("2026-10-05"), "개천절 대체공휴일")
        self.assertFalse(cal.is_trading_day("2026-10-09"), "한글날")
        self.assertEqual(cal.next_trading_day("2026-09-23"), date(2026, 9, 28),
                         "연휴를 건너뛴다 (9/28 은 정상 개장으로 확인됨)")
        self.assertEqual(cal.prev_trading_day("2026-09-28"), date(2026, 9, 23))
        self.assertEqual(cal.add_trading_days("2026-09-23", 2), date(2026, 9, 29))
        self.assertEqual(cal.add_trading_days("2026-09-29", -2), date(2026, 9, 23))
        self.assertEqual(cal.add_trading_days("2026-09-23", 0), date(2026, 9, 23))
        self.assertEqual(cal.trading_days_between("2026-09-23", "2026-09-29"),
                         [date(2026, 9, 23), date(2026, 9, 28), date(2026, 9, 29)])
        self.assertEqual(cal.trading_days_between("2026-09-29", "2026-09-23"), [])

    def test_data_beats_the_rule_for_past_days(self):
        # 수집된 코스피 일봉: 9/24(설정상 휴장)에는 장이 열렸고 9/23 에는 열리지 않은 것으로 기록됐다
        self.store.put_market([{"series": "KOSPI", "date": d, "value": 2500.0}
                               for d in ("2026-09-21", "2026-09-22", "2026-09-24")])
        self.store.commit()
        cal = TradingCalendar(self.cfg, self.store)
        self.assertTrue(cal.is_trading_day("2026-09-24"), "데이터가 있으면 설정 휴장일보다 데이터가 이긴다")
        self.assertFalse(cal.is_trading_day("2026-09-23"), "데이터 구간 안에서 값이 없으면 휴장으로 본다")
        self.assertEqual(cal.next_trading_day("2026-09-22"), date(2026, 9, 24))
        self.assertEqual(cal.prev_trading_day("2026-09-24"), date(2026, 9, 22))
        # 데이터 구간 밖(미래)은 다시 규칙으로 판정한다 — 없는 휴장일을 만들지 않는다
        self.assertTrue(cal.is_trading_day("2026-09-29"))
        self.assertFalse(cal.is_trading_day("2026-10-09"))
        self.assertTrue(cal.is_trading_day("2026-09-18"), "수집 시작 전 과거도 규칙으로")

        cal.refresh()
        self.assertTrue(cal.is_trading_day("2026-09-24"), "refresh 후에도 같은 판정")

    def test_calendar_without_store_ignores_data(self):
        self.store.put_market([{"series": "KOSPI", "date": "2026-09-24", "value": 2500.0}])
        self.store.commit()
        self.assertFalse(TradingCalendar(self.cfg).is_trading_day("2026-09-24"))


class NormalizeTest(unittest.TestCase):
    def test_rank_scores_basic(self):
        got = nz.rank_scores({"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0}, 1)
        self.assertEqual([round(got[k][0], 6) for k in "abcd"], [-0.75, -0.25, 0.25, 0.75])
        self.assertTrue(all(not got[k][1] for k in "abcd"))
        flipped = nz.rank_scores({"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0}, -1)
        self.assertEqual([round(flipped[k][0], 6) for k in "abcd"], [0.75, 0.25, -0.25, -0.75])

    def test_rank_scores_ties_and_missing(self):
        got = nz.rank_scores({"a": 1.0, "b": 2.0, "c": 2.0, "d": None}, 1)
        self.assertEqual(got["d"], (0.0, True), "결측은 0 점 + 표시")
        self.assertAlmostEqual(got["a"][0], 2 * (1 - 0.5) / 3 - 1)
        self.assertAlmostEqual(got["b"][0], got["c"][0], msg="동점은 평균 순위로 같은 점수")
        self.assertAlmostEqual(got["b"][0], 2 * (2.5 - 0.5) / 3 - 1)
        self.assertEqual(sum(1 for v in got.values() if v[1]), 1)

    def test_rank_scores_edge_counts(self):
        self.assertEqual(nz.rank_scores({"a": None, "b": None}, 1), {"a": (0.0, True), "b": (0.0, True)})
        self.assertEqual(nz.rank_scores({}, 1), {})
        one = nz.rank_scores({"a": 5.0, "b": None}, 1)
        self.assertEqual(one["a"], (0.0, False), "대상이 하나면 우열이 없어 0 점 (결측은 아니다)")
        all_tied = nz.rank_scores({"a": 1.0, "b": 1.0, "c": 1.0}, 1)
        self.assertEqual([round(all_tied[k][0], 6) for k in "abc"], [0.0, 0.0, 0.0])

    def test_hist_percentile(self):
        hist = [float(i) for i in range(10)]                     # 0..9
        self.assertEqual(nz.hist_percentile_score(9.5, hist, 1), (1.0, False))
        self.assertEqual(nz.hist_percentile_score(-1.0, hist, 1), (-1.0, False))
        self.assertAlmostEqual(nz.hist_percentile_score(5.0, hist, 1)[0], 2 * ((5 + 0.5) / 10) - 1,
                               msg="동점은 중간 순위로 센다")
        self.assertAlmostEqual(nz.hist_percentile_score(5.0, hist, -1)[0], -(2 * ((5 + 0.5) / 10) - 1))
        self.assertEqual(nz.hist_percentile_score(5.0, [], 1), (0.0, True))
        self.assertEqual(nz.hist_percentile_score(None, hist, 1), (0.0, True))
        self.assertEqual(nz.hist_percentile_score(5.0, hist, 1, min_history=60), (0.0, True),
                         "표본이 모자라면 점수를 지어내지 않는다")
        self.assertEqual(nz.hist_percentile_score(5.0, hist, 1, min_history=10)[1], False)
        self.assertEqual(nz.hist_percentile_score(5.0, [None, 1.0, None], 1)[1], False)

    def test_rule_and_rubric(self):
        self.assertEqual(nz.rule_score(True, 1), (1.0, False))
        self.assertEqual(nz.rule_score(False, 1), (-1.0, False))
        self.assertEqual(nz.rule_score(True, -1), (-1.0, False))
        self.assertEqual(nz.rule_score(None, 1), (0.0, True))
        self.assertEqual(nz.rubric_score(2, 1), (1.0, False))
        self.assertEqual(nz.rubric_score(-2, 1), (-1.0, False))
        self.assertEqual(nz.rubric_score(1, 1), (0.5, False))
        self.assertEqual(nz.rubric_score(0, 1), (0.0, False), "수준 0 은 결측이 아니다")
        self.assertEqual(nz.rubric_score(None, 1), (0.0, True))
        self.assertEqual(nz.rubric_score(5, 1), (1.0, False), "눈금 밖 수준은 잘린다")

    def test_decay_and_clip(self):
        self.assertEqual(nz.linear_decay(0, 20), 1.0)
        self.assertEqual(nz.linear_decay(10, 20), 0.5)
        self.assertEqual(nz.linear_decay(20, 20), 0.0)
        self.assertEqual(nz.linear_decay(30, 20), 0.0)
        self.assertEqual(nz.linear_decay(-5, 20), 1.0)
        self.assertEqual(nz.linear_decay(1, 0), 0.0)
        self.assertEqual((nz.clip(5, 0, 1), nz.clip(-5, 0, 1), nz.clip(0.5, 0, 1)), (1, 0, 0.5))


class CombineTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.specs = load_specs(self.cfg)

    def test_layer_score_weights_and_missing(self):
        # 결측 처리를 보려면 가중치가 있는 요인이 결측이어야 한다. 운영 설정의 mkt_overnight 는
        # 2026-09-29 부터 가중치 0(결정 17)이라 이 테스트에서만 1 로 둔다.
        cfg = copy.deepcopy(self.cfg)
        cfg["factors"]["mkt_overnight"]["weight"] = 1
        specs = load_specs(cfg)
        scores = {"mkt_trend": (1.0, False),      # 가중치 2
                  "mkt_vol": (-0.5, False),       # 가중치 1
                  "mkt_overnight": (1.0, True),   # 결측 → 분자·분모에서 뺀다
                  "mkt_credit": (1.0, False)}     # 가중치 0 → 무시
        self.assertAlmostEqual(combine.market_score(scores, specs), (2 * 1.0 + 1 * -0.5) / 3)

        no_missing = dict(scores, mkt_overnight=(1.0, False))
        self.assertAlmostEqual(combine.market_score(no_missing, specs), (2 * 1.0 + 1 * -0.5 + 1 * 1.0) / 4)

    def test_overnight_is_observation_only(self):
        """결정 17: 운영 설정에서 밤사이 요인은 관찰 요인이다 — 값이 있어도 시장 점수에 들어가지 않는다."""
        scores = {"mkt_trend": (1.0, False), "mkt_vol": (-0.5, False), "mkt_overnight": (1.0, False)}
        self.assertAlmostEqual(combine.market_score(scores, self.specs), (2 * 1.0 + 1 * -0.5) / 3)

    def test_layer_score_none_when_nothing_usable(self):
        self.assertIsNone(combine.market_score({}, self.specs))
        self.assertIsNone(combine.market_score({"mkt_trend": (1.0, True)}, self.specs))
        self.assertIsNone(combine.market_score({"mkt_credit": (1.0, False)}, self.specs),
                          "관찰 요인만 있으면 계층 점수는 없다")
        self.assertIsNone(combine.market_score({"없는요인": (1.0, False)}, self.specs))
        self.assertIsNone(combine.layer_score({"stk_flow": (1.0, False)}, self.specs, layer="market"),
                          "다른 계층 점수는 섞이지 않는다")

    def test_sector_scores(self):
        got = combine.sector_scores({
            "반도체": {"sec_flow": (0.8, False), "sec_trend": (1.0, False), "news_risk": (-1.0, False)},
            "은행": {"sec_flow": (0.2, True), "sec_trend": (-1.0, False)},
            "조선": {"news_risk": (1.0, False)},
        }, self.specs)
        self.assertAlmostEqual(got["반도체"], 0.9, msg="관찰 요인(news_risk)은 빠진다")
        self.assertAlmostEqual(got["은행"], -1.0)
        self.assertIsNone(got["조선"])

    def test_stock_composites(self):
        got = combine.stock_composites(
            {"A": 0.4, "B": 0.4, "C": 0.4, "D": None},
            {"A": "반도체", "B": "없는섹터", "C": None},
            {"반도체": 0.8, "없는섹터": None},
            self.cfg["combine"]["sector_tilt"])
        self.assertAlmostEqual(got["A"], 0.4 + 0.25 * 0.8)
        self.assertAlmostEqual(got["B"], 0.4, msg="섹터 점수가 없으면 기울기 항은 0")
        self.assertAlmostEqual(got["C"], 0.4, msg="소속 섹터가 없으면 기울기 항은 0")
        self.assertNotIn("D", got, "계층 점수가 없는 종목은 종합 점수도 없다")


def stock_universe(n=40):
    """S01 이 1위인 점수표. 점수는 전부 양수."""
    return {f"S{i:02d}": (n + 1 - i) / 100.0 for i in range(1, n + 1)}


class AllocateTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.uni = self.cfg["universe"]
        self.cash, self.core = self.uni["cash_etf"], self.uni["core_etf"]
        self.sectors = {"반도체": 0.8, "조선": 0.4, "은행": -0.2, "자동차": 0.1}

    def weights(self, rows):
        return {r["asset"]: r["weight"] for r in rows}

    def roles(self, rows):
        return {r["asset"]: r["role"] for r in rows}

    def run_alloc(self, market=0.0, sectors=None, stocks=None, holdings=(), flags=None, vetoed=None, cfg=None):
        return allocate.target_weights(market, self.sectors if sectors is None else sectors,
                                       stock_universe() if stocks is None else stocks,
                                       holdings, flags or {}, cfg or self.cfg, vetoed)

    def test_sums_to_one_and_structure(self):
        for market in (-1.0, -0.5, 0.0, 0.37, 1.0, None):
            rows = self.run_alloc(market=market)
            self.assertAlmostEqual(sum(r["weight"] for r in rows), 1.0, places=12)
            self.assertTrue(all(r["weight"] > 0 for r in rows))
            self.assertEqual(set(self.roles(rows).values()), {"cash", "core", "sector", "stock"})

    def test_risk_weight_follows_market_score(self):
        al = self.cfg["allocate"]
        w = self.weights(self.run_alloc(market=0.5))
        self.assertAlmostEqual(w[self.cash], 1 - (0.5 + 0.4 * 0.5))
        self.assertAlmostEqual(self.weights(self.run_alloc(market=1.0))[self.cash], 1 - al["risk_max"])
        self.assertAlmostEqual(self.weights(self.run_alloc(market=-1.0))[self.cash], 1 - al["risk_min"])
        self.assertAlmostEqual(self.weights(self.run_alloc(market=None))[self.cash], 1 - al["risk_base"],
                               msg="시장 점수를 못 구하면 기본값으로 물러선다 (0점 중립과 구분)")

    def test_sector_slots_and_spill_to_core(self):
        rows = self.run_alloc(market=1.0)
        sector_assets = [r["asset"] for r in rows if r["role"] == "sector"]
        self.assertEqual(sector_assets, [self.uni["sector_etfs"]["반도체"], self.uni["sector_etfs"]["조선"]],
                         "점수 상위 2개 섹터만 (결정 10)")
        satellite = 0.9 * 0.5
        self.assertAlmostEqual(self.weights(rows)[sector_assets[0]], satellite * 0.3 / 2)

        # 양수 섹터가 하나뿐이면 남은 슬롯의 몫은 core 로 간다
        one = self.run_alloc(market=1.0, sectors={"반도체": 0.8, "은행": -0.2, "조선": None})
        self.assertEqual(len([r for r in one if r["role"] == "sector"]), 1)
        self.assertAlmostEqual(self.weights(one)[self.core],
                               self.weights(rows)[self.core] + satellite * 0.3 / 2)
        self.assertAlmostEqual(sum(r["weight"] for r in one), 1.0, places=12)

        # 섹터 ETF 가 없는 섹터는 담을 수 없다 → 다음 섹터가 그 자리를 쓴다
        unknown = self.run_alloc(market=1.0, sectors={"알수없는테마": 0.9, "반도체": 0.8, "조선": 0.4})
        self.assertEqual([r["asset"] for r in unknown if r["role"] == "sector"],
                         [self.uni["sector_etfs"]["반도체"], self.uni["sector_etfs"]["조선"]])

    def test_stock_cap_spills_to_core(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["allocate"]["n_stocks"] = 2                       # 슬롯당 몫이 상한(5%)을 넘게 만든다
        rows = self.run_alloc(market=1.0, sectors={}, cfg=cfg)
        stock_rows = [r for r in rows if r["role"] == "stock"]
        self.assertEqual(len(stock_rows), 2)
        self.assertTrue(all(r["weight"] == cfg["allocate"]["stock_cap"] for r in stock_rows),
                        "종목당 전체의 5% 상한")
        satellite = 0.9 * 0.5
        expected_core = 0.45 + satellite * 0.3 + (satellite * 0.7 - 2 * 0.05)
        self.assertAlmostEqual(self.weights(rows)[self.core], expected_core, places=12)
        self.assertAlmostEqual(sum(r["weight"] for r in rows), 1.0, places=12)

    def test_non_positive_scores_are_not_held(self):
        stocks = {"S01": 0.5, "S02": 0.1, "S03": 0.0, "S04": -0.3}
        rows = self.run_alloc(market=0.0, stocks=stocks)
        self.assertEqual([r["asset"] for r in rows if r["role"] == "stock"], ["S01", "S02"],
                         "0 이하인 종목은 상위 안이어도 담지 않는다")
        self.assertAlmostEqual(sum(r["weight"] for r in rows), 1.0, places=12)

        none_positive = self.run_alloc(market=0.0, stocks={"S01": -0.1}, sectors={})
        self.assertEqual([r for r in none_positive if r["role"] in ("stock", "sector")], [])
        self.assertAlmostEqual(self.weights(none_positive)[self.core], 0.5,
                               msg="끌리는 종목이 없으면 전부 지수(core)로")

    def test_hysteresis_keeps_a_holding_inside_exit_rank(self):
        stocks = stock_universe()
        rows = self.run_alloc(stocks=stocks, holdings={"S25"})
        picked = [r["asset"] for r in rows if r["role"] == "stock"]
        self.assertIn("S25", picked, "보유 종목은 exit_rank(30) 안이면 남는다")
        self.assertEqual(len(picked), self.cfg["allocate"]["n_stocks"])
        self.assertEqual(picked[0], "S25", "보유 종목이 먼저 자리를 잡는다")
        self.assertNotIn("S10", picked, "보유가 자리를 차지한 만큼 신규는 순위대로 잘린다")

        dropped = [r["asset"] for r in self.run_alloc(stocks=stocks, holdings={"S31"})]
        self.assertNotIn("S31", dropped, "순위가 30위 밖으로 밀린 보유는 뺀다")
        self.assertIn("S10", dropped)

        # 보유 종목이라도 점수가 0 이하가 되면 뺀다
        weak = dict(stocks, S25=-0.1)
        self.assertNotIn("S25", [r["asset"] for r in self.run_alloc(stocks=weak, holdings={"S25"})])

    def test_halt_and_veto_exclusion(self):
        stocks = stock_universe()
        rows = self.run_alloc(stocks=stocks, flags={"S01": ["halt"], "S02": ["spike"]},
                              vetoed={"S03"}, holdings={"S01"})
        picked = [r["asset"] for r in rows if r["role"] == "stock"]
        self.assertNotIn("S01", picked, "halt 는 체결이 불가능하므로 v0 에서도 뺀다")
        self.assertNotIn("S03", picked, "거부된 종목은 담지 않는다")
        self.assertIn("S02", picked, "halt 가 아닌 위험 표시는 후보에 남는다 (표시만 기록)")
        self.assertIn("S12", picked, "빠진 자리는 다음 순위로 채운다 (결정 10)")
        self.assertEqual(len(picked), self.cfg["allocate"]["n_stocks"])

        # 섹터 ETF 도 같은 규칙
        halted_etf = self.uni["sector_etfs"]["반도체"]
        sector_rows = self.run_alloc(stocks=stocks, flags={halted_etf: ["halt"]})
        self.assertNotIn(halted_etf, [r["asset"] for r in sector_rows if r["role"] == "sector"])

    def test_empty_inputs(self):
        rows = self.run_alloc(market=None, sectors={}, stocks={})
        self.assertEqual(self.roles(rows), {self.cash: "cash", self.core: "core"})
        self.assertAlmostEqual(sum(r["weight"] for r in rows), 1.0, places=12)

    def test_as_dict(self):
        rows = self.run_alloc()
        self.assertEqual(allocate.as_dict(rows), self.weights(rows))


class RebalanceBandTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.core = self.cfg["universe"]["core_etf"]
        self.band = self.cfg["allocate"]["rebalance_band"]

    def test_small_difference_is_not_traded(self):
        current = {self.core: 0.5, "A": 0.3, "B": 0.2}
        target = {self.core: 0.5, "A": 0.32, "B": 0.18}
        got = allocate.apply_rebalance_band(current, target, self.band, self.core)
        self.assertAlmostEqual(got["A"], 0.3, msg="5%포인트 미만이면 거래하지 않는다")
        self.assertAlmostEqual(got["B"], 0.2)
        self.assertAlmostEqual(sum(got.values()), 1.0, places=12)

    def test_large_difference_is_traded(self):
        current = {self.core: 0.5, "A": 0.3, "B": 0.2}
        target = {self.core: 0.4, "A": 0.4, "B": 0.2}
        got = allocate.apply_rebalance_band(current, target, self.band, self.core)
        self.assertAlmostEqual(got["A"], 0.4)
        self.assertAlmostEqual(got[self.core], 0.4)
        self.assertAlmostEqual(sum(got.values()), 1.0, places=12)

    def test_new_entry_and_full_exit_ignore_the_band(self):
        current = {self.core: 0.9, "OLD": 0.1}
        target = {self.core: 0.88, "NEW": 0.02}          # 둘 다 기준 미만의 차이
        got = allocate.apply_rebalance_band(current, target, self.band, self.core)
        self.assertNotIn("OLD", got, "전량 제외는 차이와 무관하게 실행한다")
        self.assertAlmostEqual(got["NEW"], 0.02, msg="신규 편입은 차이와 무관하게 실행한다")
        self.assertAlmostEqual(got[self.core], 0.98, msg="거래하지 않은 잔여분은 핵심 ETF 로")
        self.assertAlmostEqual(sum(got.values()), 1.0, places=12)

    def test_leftover_goes_to_core(self):
        current = {self.core: 0.4, "A": 0.6}
        target = {self.core: 0.42, "A": 0.58}            # 둘 다 기준 미만 → 아무것도 거래하지 않는다
        got = allocate.apply_rebalance_band(current, target, self.band, self.core)
        self.assertEqual(got, {self.core: 0.4, "A": 0.6})

        current = {"A": 0.5, "B": 0.5}
        target = {"A": 0.52, "B": 0.4, self.core: 0.08}
        got = allocate.apply_rebalance_band(current, target, self.band, self.core)
        self.assertAlmostEqual(got["A"], 0.5, msg="A 는 기준 미만이라 유지")
        self.assertAlmostEqual(got["B"], 0.4, msg="B 는 기준 이상이라 거래")
        self.assertAlmostEqual(got[self.core], 0.1, msg="0.08 + 거래하지 않아 남은 0.02")
        self.assertAlmostEqual(sum(got.values()), 1.0, places=12)

    def test_core_asset_required_when_leftover(self):
        with self.assertRaises(ValueError):
            allocate.apply_rebalance_band({"A": 1.0}, {"A": 0.98, "B": 0.02}, self.band)
        # 잔여분이 없으면 core_asset 없이도 된다
        got = allocate.apply_rebalance_band({"A": 1.0}, {"A": 0.5, "B": 0.5}, self.band)
        self.assertEqual(got, {"A": 0.5, "B": 0.5})

    def test_end_to_end_with_target_weights(self):
        """목표 비중 → 리밸런싱 기준 적용까지 합이 1로 유지되는지 (다음 모듈이 기대하는 계약)."""
        cfg = self.cfg
        rows = allocate.target_weights(0.5, {"반도체": 0.8, "조선": 0.4}, stock_universe(),
                                       holdings=(), flags={}, cfg=cfg)
        target = allocate.as_dict(rows)
        current = dict(target)
        current[self.core] = current[self.core] + 0.01
        current[list(target)[-1]] -= 0.01
        got = allocate.apply_rebalance_band(current, target, cfg["allocate"]["rebalance_band"], self.core)
        self.assertAlmostEqual(sum(got.values()), 1.0, places=12)


if __name__ == "__main__":
    unittest.main()
