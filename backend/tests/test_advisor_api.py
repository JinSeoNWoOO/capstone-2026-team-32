"""판단 지원(advisor) 모드의 리포트 API·서비스 단위 테스트. 네트워크를 쓰지 않는다.

기존 테스트와 같은 방식이다: unittest + 임시 디렉터리 + 실제 SQLite. FastAPI 를 띄우지 않고
서비스 함수를 직접 부른다 (backend/app/main.py 를 import 하면 운영 경로를 가리키는 수집기 감독
싱글턴까지 따라온다 — 이 파일이 확인하려는 것과 무관하다). 라우트는 얇은 껍데기라 여기서
확인하는 서비스 계약이 그대로 응답이 된다.

확인하는 것:
  - 시연용 DB 생성기: 같은 인자면 같은 내용
  - 리포트: 기본값(최근 날짜·최근 단계), 요인 분해, v0·LLM 비중 병렬, 전일 대비 변화, 단계 비교
  - 재현 모드와 실시간 모드가 섞이지 않는다 (설계 7.4)
  - 요인 지표의 '판단 불가' 문턱 (설계 7.2)
  - 기록이 없는 DB 에서 500 대신 available=False
  - 스케줄러 판단(무엇을 언제 띄우는가)을 순수 함수로, 프로세스를 띄우지 않고
  - 배치가 도는 중의 수동 실행 거절
"""
from backend.tests import _safety  # noqa: F401  맨 먼저: 실제 주문·외부 접속 차단 (_safety.py 참고)
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import date, datetime
from unittest import mock

from backend.advisor import report as R
from backend.advisor.config import load_config
from backend.advisor.devtools.make_fixture_db import build as build_fixture
from backend.advisor.store import Store
from backend.app.services.advisor import (
    AdvisorError,
    AdvisorService,
    fallback_state,
    is_due,
    next_occurrence,
    plan_launch,
    poll_due,
)

FIXTURE_END = "2026-09-22"          # 설계 11장의 확인된 거래일. 고정해야 단언이 안정적이다
FIXTURE_DAYS = 20
SCHEDULE = {"prelim": "18:30", "final": "07:40", "fallback_deadline": "08:50",
            "poll_interval_min": 10, "poll_window": ["07:30", "18:10"]}


def _tmpdir():
    path = tempfile.mkdtemp(prefix="advisor-test-")
    return path


class FixtureDbTest(unittest.TestCase):
    """시연용 DB 생성기 자체. 여기가 깨지면 아래 모든 테스트의 전제가 무너진다."""

    def setUp(self):
        self.tmp = _tmpdir()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = load_config()

    def test_builds_expected_shape(self):
        info = build_fixture(os.path.join(self.tmp, "a.db"), end=FIXTURE_END, days=FIXTURE_DAYS,
                             replay_days=5)
        self.assertEqual(len(info["dates"]), FIXTURE_DAYS)
        self.assertEqual(info["end"], FIXTURE_END)
        self.assertEqual(info["runs"], FIXTURE_DAYS * 2)          # 날짜마다 예비·최종 두 벌
        self.assertEqual(info["stocks"], 30)
        self.assertEqual(len(info["sectors"]), 3)
        self.assertGreater(info["nav_rows"], 0)
        self.assertGreater(info["metrics"], 0)

    def test_same_arguments_give_same_content(self):
        """같은 인자면 같은 숫자가 나와야 테스트가 특정 값을 근거로 단언할 수 있다."""
        a = os.path.join(self.tmp, "a.db")
        b = os.path.join(self.tmp, "b.db")
        build_fixture(a, end=FIXTURE_END, days=FIXTURE_DAYS, replay_days=5)
        build_fixture(b, end=FIXTURE_END, days=FIXTURE_DAYS, replay_days=5)
        with Store(a, readonly=True) as sa, Store(b, readonly=True) as sb:
            sql = "SELECT entity, factor_id, raw_value, score, missing FROM factor_value ORDER BY 1,2"
            self.assertEqual([tuple(r) for r in sa.conn.execute(sql)],
                             [tuple(r) for r in sb.conn.execute(sql)])
            nav = "SELECT portfolio_id, date, nav FROM nav ORDER BY 1,2"
            self.assertEqual([tuple(r) for r in sa.conn.execute(nav)],
                             [tuple(r) for r in sb.conn.execute(nav)])


class ReportReadTest(unittest.TestCase):
    """리포트 조립 함수 (설계 10.1). 읽기 전용 Store 로만 본다."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = _tmpdir()
        cls.cfg = load_config()
        cls.db = os.path.join(cls.tmp, "advisor.db")
        cls.info = build_fixture(cls.db, end=FIXTURE_END, days=FIXTURE_DAYS, replay_days=5)
        cls.store = Store(cls.db, readonly=True)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---------------------------------------------------------- 기본값

    def test_report_defaults_to_latest_date_and_stage(self):
        rep = R.report(self.store, self.cfg)
        self.assertTrue(rep["available"])
        self.assertEqual(rep["date"], FIXTURE_END)
        self.assertIn(rep["stage"], ("prelim", "final"))
        self.assertEqual(rep["mode"], "live")
        self.assertEqual(rep["dates"][0]["date"], self.info["dates"][-1])

    def test_explicit_date_and_stage_win(self):
        day = self.info["dates"][-3]
        rep = R.report(self.store, self.cfg, date=day, stage="final")
        self.assertEqual((rep["date"], rep["stage"]), (day, "final"))
        self.assertEqual(rep["run"]["decision_time"][:10], day)

    def test_unknown_date_degrades_without_raising(self):
        rep = R.report(self.store, self.cfg, date="1999-01-01")
        self.assertFalse(rep["available"])
        self.assertIn("실행 기록", rep["reason"])

    # ---------------------------------------------------------- 시장 점수 분해

    def test_market_breakdown_covers_every_configured_market_factor(self):
        rep = R.report(self.store, self.cfg, stage="final")
        ids = [f["factor_id"] for f in rep["market"]["factors"]]
        want = [fid for fid, m in self.cfg["factors"].items() if m["layer"] == "market"]
        self.assertEqual(ids, want)
        for row in rep["market"]["factors"]:
            self.assertIn("raw_value", row)
            self.assertIn("weight", row)
            self.assertIn("missing", row)
        self.assertIsNotNone(rep["market"]["risk_weight"])

    def test_overnight_factor_is_missing_in_prelim_only(self):
        """mkt_overnight 이 예비에서 결측인 것이 곧 '밤사이 정보의 기여' 실험이다 (결정 11)."""
        def overnight(stage):
            rep = R.report(self.store, self.cfg, stage=stage)
            return next(f for f in rep["market"]["factors"] if f["factor_id"] == "mkt_overnight")
        self.assertTrue(overnight("prelim")["missing"])
        self.assertFalse(overnight("final")["missing"])

    # ---------------------------------------------------------- 비중과 판단 버전

    def test_weights_show_v0_and_llm_side_by_side(self):
        rep = R.report(self.store, self.cfg, stage="final")
        self.assertEqual(rep["variants"], ["v0", "llm"])
        rows = rep["allocation"]["weights"]
        self.assertTrue(rows)
        for row in rows:
            self.assertIn("weight_v0", row)
            self.assertIn("weight_llm", row)
            self.assertIn(row["role"], R.ROLE_ORDER)
            self.assertTrue(row["name"])
        total = sum(r["weight_v0"] or 0.0 for r in rows)
        self.assertAlmostEqual(total, 1.0, places=6)

    def test_run_without_llm_variant_says_so(self):
        """LLM 이 기권한 날은 llm 판단이 아예 없다 (결정 15). 화면 문구가 거기서 나온다."""
        rows = self.store.conn.execute(
            "SELECT r.as_of, r.stage FROM run r WHERE r.mode='live' "
            "AND EXISTS(SELECT 1 FROM decision d WHERE d.run_id=r.run_id AND d.variant='v0') "
            "AND NOT EXISTS(SELECT 1 FROM decision d WHERE d.run_id=r.run_id AND d.variant='llm')"
        ).fetchall()
        self.assertTrue(rows, "시연용 DB 에 LLM 기권 사례가 있어야 한다")
        rep = R.report(self.store, self.cfg, date=rows[0][0], stage=rows[0][1])
        self.assertEqual(rep["variants"], ["v0"])
        self.assertEqual(rep["llm_note"], "LLM 조정 없음 (v0와 동일)")

    def test_llm_adjustments_carry_adopt_and_reject_reasons(self):
        rep = R.report(self.store, self.cfg, stage="final")
        adj = rep["llm_adjustments"]
        self.assertTrue(adj)
        row = adj[0]
        for key in ("entity", "name", "adj", "adopted", "rejected", "reason", "vetoed"):
            self.assertIn(key, row)
        self.assertTrue(all(r["adj"] or r["vetoed"] for r in adj))

    def test_llm_adjustments_add_readable_factor_names_and_evidence_fields(self):
        """근거 표시용 필드는 **추가만** 한다. 요인 id 대신 이름, 근거 목록과 그 범위가 붙는다."""
        rep = R.report(self.store, self.cfg, stage="final")
        names = {m["factor_id"]: m["name"] for m in R.factor_meta(self.cfg)}
        for row in rep["llm_adjustments"]:
            for key in ("decision_time", "adopted_factors", "rejected_factors", "evidence",
                        "evidence_scope", "evidence_total", "evidence_other_count"):
                self.assertIn(key, row)
            self.assertIn(row["evidence_scope"], ("adopted", "entity", "none"))
            self.assertEqual([f["factor_id"] for f in row["adopted_factors"]], row["adopted"] or [])
            for f in row["rejected_factors"]:
                self.assertEqual(f["name"], names.get(f["factor_id"], f["factor_id"]))
                self.assertTrue(f["reason"])
            for ev in row["evidence"]:
                self.assertEqual(ev["ref_type"], "disclosure")      # 시연용 DB 의 근거는 공시뿐이다
                self.assertEqual(ev["ref_label"], "공시")
                if ev.get("disclosure"):                          # 공시 행이 있으면 접수일·경과가 붙는다
                    self.assertIsNotNone(ev["trading_days_before"])
                    self.assertTrue(ev["age_label"].startswith("접수 "))
                    self.assertRegex(ev["published_at"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(any(ev.get("disclosure") for row in rep["llm_adjustments"]
                            for ev in row["evidence"]), "시연용 DB 에 공시 근거가 딸린 조정이 있어야 한다")
        rejected = [f for row in rep["llm_adjustments"] for f in row["rejected_factors"]]
        self.assertTrue(rejected)
        self.assertTrue(any(f["name"] != f["factor_id"] for f in rejected))

    # ---------------------------------------------------------- 전일 대비·단계 비교

    def test_diff_compares_the_previous_same_stage_run(self):
        day = self.info["dates"][-1]
        rep = R.report(self.store, self.cfg, date=day, stage="final")
        diff = rep["diff"]
        self.assertTrue(diff["available"])
        self.assertEqual(diff["prev_run"]["stage"], "final")           # 같은 단계끼리만 견준다
        self.assertLess(diff["prev_run"]["as_of"], day)
        self.assertEqual([r["role"] for r in diff["role_totals"]], list(R.ROLE_ORDER))
        self.assertTrue(diff["summary"])
        for row in diff["factors"]:
            self.assertAlmostEqual(row["delta"], row["current"] - row["prev"], places=9)
        deltas = [abs(r["delta"]) for r in diff["factors"]]
        self.assertEqual(deltas, sorted(deltas, reverse=True))

    def test_first_run_has_no_previous_decision(self):
        rep = R.report(self.store, self.cfg, date=self.info["dates"][0], stage="final")
        self.assertFalse(rep["diff"]["available"])
        self.assertIn("직전", rep["diff"]["reason"])

    def test_stage_compare_pairs_prelim_and_final_of_the_same_day(self):
        rep = R.report(self.store, self.cfg, date=self.info["dates"][-1], stage="final")
        cmp = rep["stage_compare"]
        self.assertTrue(cmp["available"])
        self.assertEqual(cmp["current"]["run"]["stage"], "final")
        self.assertEqual(cmp["other"]["run"]["stage"], "prelim")
        self.assertEqual(cmp["other"]["run"]["as_of"], cmp["current"]["run"]["as_of"])

    # ---------------------------------------------------------- 실행 기록과 수집 요약

    def test_run_note_is_split_into_providers_and_fallbacks(self):
        rep = R.report(self.store, self.cfg, stage="final")
        note = rep["run"]["note"]
        self.assertIsNotNone(note["providers"])
        self.assertIsInstance(note["fallbacks_used"], list)
        self.assertIsNone(note["text"])

    def test_error_run_note_stays_readable_text(self):
        row = self.store.conn.execute(
            "SELECT run_id, note FROM run WHERE status='error' LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        note = R.parse_note(row["note"])
        self.assertIn("RuntimeError", note["text"])
        self.assertIsNone(note["providers"])

    # ---------------------------------------------------------- 점수표·자산 상세

    def test_scores_returns_three_layers_by_default(self):
        out = R.scores(self.store, self.cfg)
        self.assertEqual([l["layer"] for l in out["layers"]], ["market", "sector", "stock"])
        stock = out["layers"][2]
        self.assertEqual(stock["total"], 30)
        self.assertEqual(len(stock["rows"]), 30)                  # top_n 없이 부르면 전부

    def test_scores_layer_filter(self):
        out = R.scores(self.store, self.cfg, layer="sector")
        self.assertEqual([l["layer"] for l in out["layers"]], ["sector"])
        self.assertEqual(len(out["layers"][0]["rows"]), 3)

    def test_report_limits_the_stock_table(self):
        rep = R.report(self.store, self.cfg, top_n=20)
        self.assertEqual(len(rep["stocks"]["rows"]), 20)
        self.assertEqual(rep["stocks"]["total"], 30)
        scores = [r["score_v0"] for r in rep["stocks"]["rows"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_asset_detail_decomposes_the_score(self):
        """요인 기여의 합 + sector_tilt × 섹터 점수 = 저장된 종합 점수 (설계 5.3)."""
        rep = R.report(self.store, self.cfg, stage="final")
        code = rep["stocks"]["rows"][0]["entity"]
        out = R.asset(self.store, self.cfg, code, date=rep["date"], stage="final")
        self.assertTrue(out["available"])
        self.assertEqual(out["layer"], "stock")
        self.assertTrue(out["factors"])
        self.assertIsNotNone(out["sector_part"])
        self.assertAlmostEqual(out["sector_part"]["contribution"],
                               out["sector_part"]["tilt"] * out["sector_part"]["score"], places=9)
        self.assertAlmostEqual(
            out["score_v0"], out["layer_score"] + out["sector_part"]["contribution"], places=3)
        self.assertIn("weights", out)

    def test_asset_detail_for_market_entity(self):
        out = R.asset(self.store, self.cfg, R.MARKET_ENTITY)
        self.assertTrue(out["available"])
        self.assertEqual(out["layer"], "market")
        self.assertEqual(len(out["factors"]),
                         len([f for f, m in self.cfg["factors"].items() if m["layer"] == "market"]))

    def test_unknown_asset_degrades(self):
        out = R.asset(self.store, self.cfg, "000000")
        self.assertFalse(out["available"])
        self.assertIn("000000", out["reason"])

    def test_asset_evidence_joins_the_disclosure_row(self):
        row = self.store.conn.execute(
            "SELECT run_id, entity FROM factor_evidence WHERE ref_type='disclosure' LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        run = self.store.conn.execute("SELECT as_of, stage FROM run WHERE run_id=?",
                                      (row["run_id"],)).fetchone()
        out = R.asset(self.store, self.cfg, row["entity"], date=run["as_of"], stage=run["stage"])
        found = [e for e in out["evidence"] if e["ref_type"] == "disclosure"]
        self.assertTrue(found)
        self.assertIn("report_nm", found[0]["disclosure"])

    # ---------------------------------------------------------- 성과 (실시간 대 재현)

    def test_performance_lists_every_portfolio_with_a_summary(self):
        out = R.performance(self.store, self.cfg, mode="live")
        self.assertTrue(out["available"])
        ids = [p["portfolio_id"] for p in out["portfolios"]]
        self.assertEqual(set(ids), set(R.PORTFOLIO_LABELS))
        self.assertEqual(ids[0], "sys_final_llm")                  # 주 포트폴리오가 먼저
        for p in out["portfolios"]:
            self.assertEqual(len(p["points"]), FIXTURE_DAYS)
            for key in ("total_return", "ann_vol", "max_drawdown", "turnover", "cost"):
                self.assertIn(key, p["summary"])
            self.assertAlmostEqual(p["summary"]["nav"], p["points"][-1]["nav"], places=9)

    def test_live_and_replay_are_never_merged(self):
        """재현 기록이 실시간 곡선에 섞이면 그 순간 성과 주장이 오염된다 (설계 7.4)."""
        live = R.performance(self.store, self.cfg, mode="live")
        replay = R.performance(self.store, self.cfg, mode="replay")
        self.assertTrue(all(not p["portfolio_id"].endswith(R.REPLAY_SUFFIX)
                            for p in live["portfolios"]))
        self.assertTrue(all(p["portfolio_id"].endswith(R.REPLAY_SUFFIX)
                            for p in replay["portfolios"]))
        self.assertTrue(all(p["mode"] == "replay" for p in replay["portfolios"]))
        self.assertEqual(replay["replay_note"], "재현 모드 — 성과 주장 아님")
        self.assertIsNone(live["replay_note"])
        live_points = {(p["base_id"], q["date"]) for p in live["portfolios"] for q in p["points"]}
        replay_points = {(p["base_id"], q["date"]) for p in replay["portfolios"] for q in p["points"]}
        self.assertTrue(live_points & replay_points, "두 모드가 같은 날짜를 덮어야 비교가 의미 있다")
        for p in live["portfolios"]:
            other = [q for q in replay["portfolios"] if q["base_id"] == p["base_id"]]
            self.assertTrue(other)
            self.assertNotEqual(p["points"], other[0]["points"])

    def test_replay_report_is_labelled(self):
        rep = R.report(self.store, self.cfg, mode="replay")
        self.assertTrue(rep["available"])
        self.assertEqual(rep["run"]["mode"], "replay")
        self.assertEqual(rep["replay_note"], "재현 모드 — 성과 주장 아님")
        self.assertEqual(rep["variants"], ["v0"])          # 재현은 코드 요인만 돈다 (설계 7.4)

    def test_replay_diff_never_points_at_a_live_run(self):
        rep = R.report(self.store, self.cfg, mode="replay")
        if rep["diff"]["available"]:
            self.assertEqual(rep["diff"]["prev_run"]["mode"], "replay")

    # ---------------------------------------------------------- 요인 지표

    def test_metrics_marks_small_samples_unjudgable(self):
        out = R.metrics(self.store, self.cfg)
        self.assertTrue(out["available"])
        limit = self.cfg["reeval"]["min_n_eff"]
        self.assertEqual(out["min_n_eff"], float(limit))
        small = [m for m in out["metrics"] if m["n_eff"] < limit]
        big = [m for m in out["metrics"] if m["n_eff"] >= limit]
        self.assertTrue(small and big, "두 경우가 다 있어야 문턱을 확인할 수 있다")
        self.assertTrue(all(m["verdict"] == "판단 불가" and not m["judgable"] for m in small))
        self.assertTrue(all(m["verdict"] is None and m["judgable"] for m in big))

    def test_market_factor_metrics_have_no_rank_ic(self):
        """시장 요인은 하루에 값이 하나라 날짜 내 순위 상관이 없다 (설계 7.2)."""
        out = R.metrics(self.store, self.cfg)
        market = [m for m in out["metrics"] if m["layer"] == "market"]
        self.assertTrue(market)
        self.assertTrue(all(m["rank_ic_mean"] is None for m in market))
        self.assertTrue(all(m["hit_rate"] is not None for m in market))

    def test_metrics_keeps_only_the_latest_computation(self):
        keys = [(m["stage"], m["variant"], m["factor_id"], m["horizon"])
                for m in R.metrics(self.store, self.cfg)["metrics"]]
        self.assertEqual(len(keys), len(set(keys)))


class LlmEvidenceTimingTest(unittest.TestCase):
    """LLM 조정의 근거와 그 시각 (③). "옛날 뉴스를 보고 판단했나?" 를 화면에서 가릴 수 있어야 한다.

    실제 운영 기록과 같은 모양(run 239: 자동차 뉴스 06:13·06:32, KT&G 9/22 접수 공시 2건)을
    손으로 넣은 작은 DB 와 newsgap 뉴스 DB 로 확인한다. 뉴스 DB 는 읽기 전용으로만 열어야 한다.
    """

    NEWS_A = "2026092806130535HOC23R0N"
    NEWS_B = "202609280632282600000103"

    def setUp(self):
        self.tmp = _tmpdir()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.newsgap = os.path.join(self.tmp, "newsgap.db")
        conn = sqlite3.connect(self.newsgap)
        conn.execute("CREATE TABLE news (realkey TEXT PRIMARY KEY, ls_datetime TEXT, recv_wall TEXT, "
                     "code TEXT, title TEXT)")
        conn.executemany("INSERT INTO news VALUES(?,?,?,?,?)", [
            (self.NEWS_A, "20260928061305", "2026-09-28T06:13:41.857", "",
             "트럼프 “전기차 의무화 없애…GM, 포드 미국내 생산 늘릴 것”"),
            (self.NEWS_B, "20260928063228", "2026-09-28T06:32:29.299", "",
             "[단독] 현대차 — 미국 판매 신기록"),
        ])
        conn.commit()
        conn.close()

        self.cfg = load_config()
        self.cfg = dict(self.cfg, paths=dict(self.cfg.get("paths") or {}, newsgap_db=self.newsgap))
        self.db = os.path.join(self.tmp, "advisor.db")
        with Store(self.db) as st:
            self.run_id = st.start_run("final", "2026-09-28",
                                       decision_time="2026-09-28T07:40:01.259041")
            st.put_composites(self.run_id, "llm", [
                {"entity": "자동차", "layer": "sector", "base_score": -0.93, "adj": -0.1,
                 "final_score": -1.03, "vetoed": 0, "adopted_json": '["news_risk"]',
                 "rejected_json": '[{"factor_id": "sec_flow", "reason": "수급은 이미 반영"}]',
                 "reason": "전기차 의무화 폐지 우려", "call_id": "c1"},
                {"entity": "033780", "layer": "stock", "base_score": 0.69, "adj": 0.15,
                 "final_score": 0.84, "vetoed": 0, "adopted_json": '["stk_disclosure"]',
                 "rejected_json": "[]", "reason": "자기주식 취득·소각", "call_id": "c2"},
                {"entity": "005930", "layer": "stock", "base_score": 0.5, "adj": 0.05,
                 "final_score": 0.55, "vetoed": 0, "adopted_json": '["stk_high52"]',
                 "rejected_json": "[]", "reason": "고가 근처", "call_id": "c3"},
                {"entity": "000660", "layer": "stock", "base_score": 0.4, "adj": 0.0,
                 "final_score": 0.4, "vetoed": 0, "adopted_json": "[]",
                 "rejected_json": "[]", "reason": None, "call_id": "c4"},
            ])
            st.put_factor_evidence(self.run_id, [
                {"entity": "자동차", "factor_id": "news_risk", "ref_type": "news", "ref_id": self.NEWS_A,
                 "summary": "policy 방향 +1 심각도 2 감쇠 1.00(0거래일) 트럼프 “전기차 의무화 없애…GM, "
                            "포드 미국내 생산 늘릴 것” — 전기차 의무화 폐지 언급"},
                {"entity": "자동차", "factor_id": "news_risk", "ref_type": "news", "ref_id": self.NEWS_B,
                 "summary": "company 방향 +1 심각도 1 감쇠 1.00(0거래일) [단독] 현대차 — 미국 판매 신기록"
                            " — 누적 판매 돌파"},
                {"entity": "033780", "factor_id": "stk_disclosure", "ref_type": "disclosure",
                 "ref_id": "20260922000256",
                 "summary": "buyback level=+1 감쇠 0.90(2거래일) [code] 주요사항보고서(자기주식취득결정) — "
                            "자기주식 취득·소각"},
                {"entity": "033780", "factor_id": "stk_disclosure", "ref_type": "disclosure",
                 "ref_id": "20260922800292",
                 "summary": "buyback level=+1 감쇠 0.90(2거래일) [llm] 주식소각결정 — 주식소각결정"},
                {"entity": "005930", "factor_id": "stk_disclosure", "ref_type": "disclosure",
                 "ref_id": "20260923000001",
                 "summary": "routine level=+0 감쇠 0.95(1거래일) [llm] 임원ㆍ주요주주특정증권등소유상황보고서 — "
                            "임원ㆍ주요주주특정증권등소유상황보고서"},
            ])
            for no, nm, day in (("20260922000256", "주요사항보고서(자기주식취득결정)", "2026-09-22"),
                                ("20260922800292", "주식소각결정", "2026-09-22"),
                                ("20260923000001", "임원ㆍ주요주주특정증권등소유상황보고서", "2026-09-23")):
                st.upsert_disclosure({"rcept_no": no, "stock_code": "033780", "corp_name": "케이티앤지",
                                      "report_nm": nm, "rcept_dt": day,
                                      "first_seen_at": f"{day}T07:40:00.000", "first_seen_src": "dart_poll"})
            st.finish_run(self.run_id, status="ok", llm_used=1)
        self.store = Store(self.db, readonly=True)
        self.addCleanup(self.store.close)

    def _rows(self, cfg=None):
        rows = R.llm_adjustments(self.store, self.run_id, {"033780": {"name": "KT&G"}},
                                 cfg=self.cfg if cfg is None else cfg)
        return {r["entity"]: r for r in rows}

    def test_news_evidence_carries_article_time_and_minutes_before_decision(self):
        car = self._rows()["자동차"]
        self.assertEqual(car["decision_time"], "2026-09-28T07:40:01")
        self.assertEqual(car["evidence_scope"], "adopted")
        self.assertEqual([f["name"] for f in car["adopted_factors"]],
                         [R.factor_meta(self.cfg)[[m["factor_id"] for m in R.factor_meta(self.cfg)]
                                                 .index("news_risk")]["name"]])
        self.assertEqual(car["rejected_factors"][0]["reason"], "수급은 이미 반영")
        ev = car["evidence"]
        self.assertEqual([e["ref_id"] for e in ev], [self.NEWS_B, self.NEWS_A])     # 최근 것이 먼저
        a = ev[1]
        self.assertEqual(a["ref_label"], "뉴스")
        self.assertEqual(a["published_at"], "2026-09-28T06:13:05")
        self.assertEqual(a["received_at"], "2026-09-28T06:13:41.857")
        self.assertEqual(a["minutes_before"], 86)
        self.assertEqual(a["age_label"], "판단 1시간 26분 전")
        self.assertEqual(a["time_label"], "9/28 06:13")
        self.assertFalse(a["after_decision"])
        self.assertEqual(a["head"], "policy 방향 +1 심각도 2 감쇠 1.00(0거래일)")
        self.assertEqual(a["title"], "트럼프 “전기차 의무화 없애…GM, 포드 미국내 생산 늘릴 것”")
        self.assertEqual(a["detail"], "전기차 의무화 폐지 언급")
        self.assertEqual(a["elapsed_recorded"], 0)
        # 제목에 '—' 가 들어 있어도 뉴스 DB 의 제목으로 자르므로 어긋나지 않는다
        self.assertEqual(ev[0]["title"], "[단독] 현대차 — 미국 판매 신기록")
        self.assertEqual(ev[0]["detail"], "누적 판매 돌파")

    def test_disclosure_evidence_counts_trading_days_across_the_holiday(self):
        """9/22 접수 → 9/28 판단은 추석 휴장(9/24·25)과 주말을 빼면 2거래일 (감쇠에 쓴 값과 같다)."""
        ktg = self._rows()["033780"]
        self.assertEqual(ktg["name"], "KT&G")
        self.assertEqual(len(ktg["evidence"]), 2)
        for ev in ktg["evidence"]:
            self.assertEqual(ev["ref_label"], "공시")
            self.assertEqual(ev["published_at"], "2026-09-22")
            self.assertEqual(ev["received_at"], "2026-09-22T07:40:00.000")
            self.assertEqual(ev["trading_days_before"], 2)
            self.assertEqual(ev["trading_days_before"], ev["elapsed_recorded"])
            self.assertEqual(ev["age_label"], "접수 9/22 · 2거래일 전")
            self.assertEqual(ev["disclosure"]["rcept_dt"], "2026-09-22")
        by_id = {e["ref_id"]: e for e in ktg["evidence"]}
        self.assertEqual(by_id["20260922000256"]["head"], "buyback level=+1 감쇠 0.90(2거래일) [code]")
        self.assertEqual(by_id["20260922000256"]["title"], "주요사항보고서(자기주식취득결정)")
        self.assertIsNone(by_id["20260922800292"]["detail"])          # 제목의 되풀이는 떨군다

    def test_falls_back_to_all_evidence_when_adopted_factor_has_none(self):
        row = self._rows()["005930"]
        self.assertEqual(row["evidence_scope"], "entity")
        self.assertEqual(len(row["evidence"]), 1)
        self.assertNotIn("000660", self._rows())         # 조정·거부가 없으면 빠진다 (기존 규칙)

    def test_missing_news_db_keeps_summary_without_times(self):
        cfg = dict(self.cfg, paths=dict(self.cfg["paths"], newsgap_db=os.path.join(self.tmp, "없음.db")))
        car = self._rows(cfg)["자동차"]
        self.assertEqual(len(car["evidence"]), 2)
        for ev in car["evidence"]:
            self.assertIsNone(ev["published_at"])
            self.assertIsNone(ev["age_label"])
            self.assertTrue(ev["title"])                  # 요약에서 제목은 여전히 뽑힌다
            self.assertTrue(ev["summary"])

    def test_news_db_is_opened_read_only(self):
        """뉴스 DB 는 수집기가 쓰는 중이다. 리포트가 열어도 파일이 바뀌면 안 된다."""
        before = os.path.getmtime(self.newsgap), os.path.getsize(self.newsgap)
        with mock.patch.object(R.sqlite3, "connect", wraps=sqlite3.connect) as spy:
            self._rows()
        uris = [c.args[0] for c in spy.call_args_list]
        self.assertTrue(uris and all(str(u).endswith("?mode=ro") for u in uris))
        self.assertTrue(all(c.kwargs.get("uri") for c in spy.call_args_list))
        self.assertEqual((os.path.getmtime(self.newsgap), os.path.getsize(self.newsgap)), before)


class EmptyDbTest(unittest.TestCase):
    """배치가 한 번도 안 돈 상태. 개발 중에는 이게 정상이라 500 을 내면 안 된다."""

    def setUp(self):
        self.tmp = _tmpdir()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = load_config()
        self.path = os.path.join(self.tmp, "empty.db")
        Store(self.path).close()                    # DDL 만 있는 빈 DB
        self.store = Store(self.path, readonly=True)
        self.addCleanup(self.store.close)

    def test_every_report_function_degrades(self):
        for name, out in (
            ("report", R.report(self.store, self.cfg)),
            ("scores", R.scores(self.store, self.cfg)),
            ("asset", R.asset(self.store, self.cfg, "005930")),
            ("performance", R.performance(self.store, self.cfg)),
            ("metrics", R.metrics(self.store, self.cfg)),
        ):
            with self.subTest(name):
                self.assertFalse(out["available"])
                self.assertTrue(out["reason"])
                json.dumps(out, ensure_ascii=False)          # JSON 으로 나갈 수 있어야 한다

    def test_status_still_answers(self):
        out = R.db_status(self.store, self.cfg)
        self.assertTrue(out["available"])
        self.assertEqual(out["counts"]["runs"], 0)
        self.assertIsNone(out["latest_date"])

    def test_missing_tables_do_not_raise(self):
        """테이블이 아예 없는 DB (다른 파일을 가리킨 경우)에서도 물러서야 한다."""
        path = os.path.join(self.tmp, "other.db")
        import sqlite3
        sqlite3.connect(path).execute("CREATE TABLE junk(x)")
        with Store(path, readonly=True) as store:
            self.assertFalse(R.report(store, self.cfg)["available"])
            self.assertFalse(R.performance(store, self.cfg)["available"])
            self.assertEqual(R.available_dates(store), [])


class SchedulerDecisionTest(unittest.TestCase):
    """무엇을 언제 띄우는가. 프로세스를 띄우지 않고 시각 판단만 확인한다."""

    def test_is_due_only_inside_the_window(self):
        self.assertFalse(is_due(datetime(2026, 9, 22, 18, 29), "18:30", 30))
        self.assertTrue(is_due(datetime(2026, 9, 22, 18, 30), "18:30", 30))
        self.assertTrue(is_due(datetime(2026, 9, 22, 18, 59), "18:30", 30))
        self.assertFalse(is_due(datetime(2026, 9, 22, 19, 1), "18:30", 30))
        self.assertFalse(is_due(datetime(2026, 9, 22, 18, 30), "엉터리", 30))

    def test_holiday_launches_nothing(self):
        plan = plan_launch(datetime(2026, 9, 24, 7, 41), SCHEDULE, trading_day=False,
                           runs_today={}, batch_running=False)
        self.assertIsNone(plan["stage"])
        self.assertEqual(plan["reason"], "휴장일")

    def test_launches_final_at_its_time(self):
        plan = plan_launch(datetime(2026, 9, 22, 7, 41), SCHEDULE, trading_day=True,
                           runs_today={}, batch_running=False)
        self.assertEqual(plan["stage"], "final")

    def test_launches_prelim_in_the_evening(self):
        plan = plan_launch(datetime(2026, 9, 22, 18, 31), SCHEDULE, trading_day=True,
                           runs_today={"final": "ok"}, batch_running=False)
        self.assertEqual(plan["stage"], "prelim")

    def test_never_launches_the_same_stage_twice_a_day(self):
        """실패한 실행도 '이미 띄웠다'로 본다 — 자동 재시도는 그날 판단을 여러 벌로 만든다."""
        for status in ("ok", "error", "running", "skipped"):
            plan = plan_launch(datetime(2026, 9, 22, 7, 45), SCHEDULE, trading_day=True,
                               runs_today={"final": status}, batch_running=False)
            self.assertIsNone(plan["stage"], status)
            self.assertIn("이미 실행", plan["reason"])

    def test_never_launches_two_batches_at_once(self):
        plan = plan_launch(datetime(2026, 9, 22, 7, 41), SCHEDULE, trading_day=True,
                           runs_today={}, batch_running=True)
        self.assertIsNone(plan["stage"])
        self.assertIn("실행 중", plan["reason"])

    def test_nothing_outside_the_scheduled_times(self):
        plan = plan_launch(datetime(2026, 9, 22, 12, 0), SCHEDULE, trading_day=True,
                           runs_today={}, batch_running=False)
        self.assertIsNone(plan["stage"])

    def test_late_start_does_not_backfill_the_day(self):
        """한참 뒤에 백엔드를 켰을 때 뒤늦게 띄우면 판단 시각과 기록이 어긋난다 (설계 12장)."""
        plan = plan_launch(datetime(2026, 9, 22, 11, 0), SCHEDULE, trading_day=True,
                           runs_today={}, batch_running=False)
        self.assertIsNone(plan["stage"])

    def test_fallback_needed_when_final_did_not_finish(self):
        state = fallback_state(datetime(2026, 9, 22, 8, 51), SCHEDULE, None, trading_day=True)
        self.assertTrue(state["due"])
        self.assertTrue(state["needed"])
        state = fallback_state(datetime(2026, 9, 22, 8, 51), SCHEDULE, "error", trading_day=True)
        self.assertTrue(state["needed"])
        state = fallback_state(datetime(2026, 9, 22, 8, 51), SCHEDULE, "ok", trading_day=True)
        self.assertFalse(state["needed"])
        state = fallback_state(datetime(2026, 9, 22, 8, 49), SCHEDULE, None, trading_day=True)
        self.assertFalse(state["due"])
        self.assertFalse(state["needed"])
        state = fallback_state(datetime(2026, 9, 24, 9, 0), SCHEDULE, None, trading_day=False)
        self.assertFalse(state["needed"])

    def test_poll_window_and_interval(self):
        now = datetime(2026, 9, 22, 10, 0)
        self.assertTrue(poll_due(now, None, SCHEDULE, trading_day=True))
        self.assertFalse(poll_due(now, None, SCHEDULE, trading_day=False))
        self.assertFalse(poll_due(now, datetime(2026, 9, 22, 9, 55), SCHEDULE, trading_day=True))
        self.assertTrue(poll_due(now, datetime(2026, 9, 22, 9, 49), SCHEDULE, trading_day=True))
        self.assertFalse(poll_due(datetime(2026, 9, 22, 7, 0), None, SCHEDULE, trading_day=True))
        self.assertFalse(poll_due(datetime(2026, 9, 22, 19, 0), None, SCHEDULE, trading_day=True))

    def test_next_occurrence_skips_closed_days(self):
        trading = lambda d: d.weekday() < 5 and d != date(2026, 9, 24)
        self.assertEqual(next_occurrence(datetime(2026, 9, 22, 8, 0), "18:30", trading),
                         "2026-09-22T18:30")
        self.assertEqual(next_occurrence(datetime(2026, 9, 22, 19, 0), "18:30", trading),
                         "2026-09-23T18:30")
        self.assertEqual(next_occurrence(datetime(2026, 9, 23, 19, 0), "07:40", trading),
                         "2026-09-25T07:40")
        self.assertIsNone(next_occurrence(datetime(2026, 9, 22, 8, 0), None, trading))


class AdvisorServiceTest(unittest.TestCase):
    """서비스 계층. 라우트가 얇은 껍데기라 여기 계약이 그대로 응답이 된다."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = _tmpdir()
        cls.db = os.path.join(cls.tmp, "advisor.db")
        build_fixture(cls.db, end=FIXTURE_END, days=FIXTURE_DAYS, replay_days=5)
        # 대체 규칙 경고는 일부러 내는 것이라 테스트 출력에서만 가린다 (상태·로그로는 그대로 확인한다)
        cls._log_level = logging.getLogger("advisor.service").level
        logging.getLogger("advisor.service").setLevel(logging.CRITICAL)


    @classmethod
    def tearDownClass(cls):
        logging.getLogger("advisor.service").setLevel(cls._log_level)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def service(self, db=None):
        # root 를 임시 디렉터리로 두면 PID·로그 파일도 거기 생겨 저장소를 건드리지 않는다
        svc = AdvisorService(db_path=db or self.db, root=self.tmp)
        svc._promote_supported = False          # 배치 --help 를 띄우지 않는다 (느리고 불필요)
        self._isolate(svc)
        self.addCleanup(svc.stop)
        return svc

    def _isolate(self, svc):
        """운영 프로세스·운영 파일에 닿는 두 갈래를 기본으로 끊는다.

        - 수집기: 진짜 `_collector_engine` 은 운영 data/ 의 PID 파일을 보는 news_collector
          싱글턴이라, 수집기가 꺼져 있으면 `_scheduler_tick`·`start` 가 실제 LS 수집기 프로세스를
          띄우려 한다 (안전망이 막지만 그 시도 자체가 실패로 남는다). 그래서 '이미 돌고 있는'
          가짜를 기본으로 준다 (감시 동작을 보려는 테스트는 `_with_collector` 로 바꿔 끼운다).
        - 배치: `_spawn` 을 갈아 끼우지 않은 테스트가 배치를 띄우려 하면 막고 실패로 남긴다.
          스케줄러는 _spawn 예외를 로그로 삼키므로 예외만으로는 안 드러나 정리 단계에서 본다."""
        fake = self._FakeCollector(running=True)
        svc._collector_engine = lambda: fake
        attempts = []

        def no_spawn(*a, **kw):
            attempts.append((a, kw))
            raise RuntimeError("테스트에서 배치 프로세스를 띄우지 않는다")
        svc._spawn = no_spawn

        def check():
            if attempts and svc._spawn is no_spawn:
                self.fail(f"테스트가 _spawn 을 가짜로 주지 않았는데 배치를 띄우려 했다: {attempts}")
        self.addCleanup(check)
        return fake

    def test_reads_go_through_to_the_report_layer(self):
        svc = self.service()
        self.assertTrue(svc.report()["available"])
        self.assertTrue(svc.scores(layer="market")["available"])
        self.assertTrue(svc.performance()["available"])
        self.assertTrue(svc.metrics()["available"])
        rep = svc.report()
        self.assertEqual(len(rep["stocks"]["rows"]), 20)        # DEFAULTS 의 report_top_n
        self.assertTrue(svc.asset(rep["stocks"]["rows"][0]["entity"])["available"])

    def test_missing_db_file_degrades(self):
        svc = self.service(db=os.path.join(self.tmp, "does-not-exist.db"))
        out = svc.report()
        self.assertFalse(out["available"])
        self.assertIn("advisor.db", out["reason"])
        self.assertFalse(svc.status()["available"])

    def test_env_overrides_the_db_path(self):
        os.environ["ADVISOR_DB"] = self.db
        self.addCleanup(os.environ.pop, "ADVISOR_DB", None)
        svc = AdvisorService(root=self.tmp)
        self._isolate(svc)
        self.addCleanup(svc.stop)
        self.assertEqual(str(svc.db_path()), self.db)
        self.assertTrue(svc.report()["available"])

    def test_status_reports_thread_and_schedule_state(self):
        svc = self.service()
        st = svc.status()
        self.assertTrue(st["available"])
        self.assertFalse(st["scheduler"]["running"])
        self.assertFalse(st["scheduler"]["enabled"])            # ADVISOR_SCHEDULER 없이는 꺼짐
        self.assertEqual(st["scheduler"]["schedule"]["final"], "07:40")
        self.assertFalse(st["batch"]["running"])
        self.assertIn("fallback_needed", st)
        self.assertEqual(st["data"]["llm_today"]["budget_usd"], 1.0)
        json.dumps(st, ensure_ascii=False)

    def test_threads_stay_off_unless_the_env_says_so(self):
        svc = self.service()
        self.assertFalse(svc.scheduler_enabled())
        self.assertFalse(svc.start())
        self.assertFalse(svc.status()["scheduler"]["running"])

    def test_manual_run_rejects_bad_arguments(self):
        svc = self.service()
        with self.assertRaises(AdvisorError):
            svc.run_batch("nightly")
        with self.assertRaises(AdvisorError):
            svc.run_batch("final", mode="backtest")

    def test_manual_run_is_refused_while_a_batch_is_running(self):
        """돌고 있는 배치를 프로세스로 흉내 내지 않고 PID 파일로만 확인한다."""
        svc = self.service()
        svc._pid_file().parent.mkdir(parents=True, exist_ok=True)
        svc._pid_file().write_text(json.dumps(
            {"pid": os.getpid(), "stage": "final", "mode": "live",
             "started_at": datetime.now().isoformat(timespec="seconds")}))
        self.addCleanup(svc._pid_file().unlink, True)
        with self.assertRaises(AdvisorError) as ctx:
            svc.run_batch("final")
        self.assertIn("이미 실행 중", str(ctx.exception))
        self.assertTrue(svc.status()["batch"]["running"])

    def test_dead_pid_file_does_not_block_forever(self):
        svc = self.service()
        svc._pid_file().parent.mkdir(parents=True, exist_ok=True)
        svc._pid_file().write_text(json.dumps({"pid": 2 ** 31 - 1, "stage": "final"}))
        self.assertIsNone(svc._read_batch())
        self.assertFalse(svc._pid_file().exists())


    # ---------------------------------------------------------- 실시간 수집기 감시

    class _FakeCollector:
        """수집기 감독 객체(news_collector)의 흉내. 실제 LS 웹소켓 프로세스는 절대 띄우지 않는다."""

        def __init__(self, running=False, fail=False):
            self.running, self.fail, self.starts = running, fail, 0

        def status(self):
            return {"collector": {"running": self.running, "pid": 1 if self.running else None,
                                  "log_path": "x"}}

        def start_collector(self, mock=False):
            self.starts += 1
            if self.fail:
                raise RuntimeError("키 없음")
            self.running = True
            return self.status()

    def _with_collector(self, svc, fake):
        svc._collector_engine = lambda: fake
        svc._spawn = lambda *a, **kw: {"pid": 1}
        return fake

    def test_tick_starts_the_collector_when_it_is_down(self):
        svc = self.service()
        fake = self._with_collector(svc, self._FakeCollector(running=False))
        svc._scheduler_tick(now=datetime(2026, 9, 22, 18, 35))
        self.assertEqual(fake.starts, 1)
        self.assertTrue(svc.status()["collector"]["running"])

    def test_tick_leaves_a_running_collector_alone(self):
        svc = self.service()
        fake = self._with_collector(svc, self._FakeCollector(running=True))
        svc._scheduler_tick(now=datetime(2026, 9, 22, 18, 35))
        self.assertEqual(fake.starts, 0)

    def test_manual_stop_disables_auto_restart_until_started_again(self):
        svc = self.service()
        fake = self._with_collector(svc, self._FakeCollector(running=False))
        svc.note_collector_manual_stop(True)
        svc._scheduler_tick(now=datetime(2026, 9, 22, 18, 35))
        self.assertEqual(fake.starts, 0)
        self.assertTrue(svc.status()["collector"]["manual_stop"])
        svc.note_collector_manual_stop(False)
        svc._scheduler_tick(now=datetime(2026, 9, 22, 18, 36))
        self.assertEqual(fake.starts, 1)

    def test_failed_start_is_not_retried_every_tick(self):
        svc = self.service()
        fake = self._with_collector(svc, self._FakeCollector(running=False, fail=True))
        svc._scheduler_tick(now=datetime(2026, 9, 22, 18, 35))
        svc._scheduler_tick(now=datetime(2026, 9, 22, 18, 35, 20))
        self.assertEqual(fake.starts, 1)               # 재시도 간격(기본 300초) 안에서는 다시 안 띄운다
        self.assertFalse(svc.status()["collector"]["running"])

    # ---------------------------------------------------------- 스케줄러 한 틱

    def _tick(self, svc, when):
        launched = []
        svc._spawn = lambda *a, **kw: launched.append((a, kw)) or {"pid": 1}
        plan = svc._scheduler_tick(now=when)
        return plan, launched

    def test_tick_does_not_relaunch_a_stage_already_in_the_run_table(self):
        svc = self.service()
        plan, launched = self._tick(svc, datetime(2026, 9, 22, 18, 35))
        self.assertIsNone(plan["stage"])
        self.assertEqual(launched, [])

    def test_tick_launches_when_the_day_has_no_run_yet(self):
        svc = self.service()
        plan, launched = self._tick(svc, datetime(2026, 9, 23, 18, 35))
        self.assertEqual(plan["stage"], "prelim")
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0][0][0], "prelim")

    def test_tick_launches_each_stage_at_most_once_per_day(self):
        """배치가 run 행을 쓰기 전에 다음 틱이 와도 두 번 띄우지 않는다 (메모리 보조 잠금)."""
        svc = self.service()
        _, launched = self._tick(svc, datetime(2026, 9, 23, 7, 45))
        self.assertEqual(len(launched), 1)
        _, again = self._tick(svc, datetime(2026, 9, 23, 7, 50))
        self.assertEqual(again, [], "같은 (단계, 날짜)를 두 번 띄우면 안 된다")

    def test_tick_is_quiet_on_a_holiday(self):
        svc = self.service()
        plan, launched = self._tick(svc, datetime(2026, 9, 24, 7, 45))   # 설정의 휴장일
        self.assertEqual(plan["reason"], "휴장일")
        self.assertEqual(launched, [])

    def test_fallback_is_exposed_when_the_final_batch_is_missing(self):
        """대체 규칙(결정 11). 승격 자체는 배치의 일이라 여기서는 드러내고 경고만 남긴다."""
        svc = self.service()
        self._tick(svc, datetime(2026, 9, 23, 8, 55))         # 그날 최종 실행이 없는 날
        st = svc.status()
        self.assertTrue(st["fallback_needed"])
        self.assertIn("완료되지 않았습니다", st["fallback"]["reason"])
        self.assertTrue(any("대체 규칙" in log["message"] for log in st["logs"]))

    def test_fallback_is_clear_when_the_final_batch_finished(self):
        svc = self.service()
        self._tick(svc, datetime(2026, 9, 22, 8, 55))         # 시연용 DB 에 최종 ok 가 있는 날
        st = svc.status()
        self.assertFalse(st["fallback_needed"])

    # ---------------------------------------------------------- 폴러

    def test_poller_survives_a_missing_module(self):
        """공시 폴러 모듈이 아직 없어도 스레드가 죽지 않고 사유만 남긴다.

        실제 모듈을 부르지 않는다 — 공시 목록 조회는 바깥으로 나가는 HTTP 호출이라
        테스트가 네트워크에 묶이면 안 된다. sys.modules 에 None 을 끼워 import 만 실패시킨다.
        """
        svc = self.service()
        with mock.patch.dict(sys.modules, {"backend.advisor.poller": None}):
            out = svc._poll_once(datetime(2026, 9, 22, 10, 0))
        state = svc.status()["poller"]
        self.assertIsNone(out)
        self.assertFalse(state["available"])
        self.assertIn("폴러 모듈", state["last_error"])

    def test_poller_records_the_count_and_commits(self):
        """폴러는 쓰기 가능한 Store 를 열어 poll_once 에 넘기고 결과 수를 상태에 남긴다."""
        svc = self.service()
        seen = {}
        fake = types.ModuleType("backend.advisor.poller")

        def poll_once(store, cfg, now):
            seen["readonly"] = store.readonly
            seen["now"] = now
            return 7

        fake.poll_once = poll_once
        with mock.patch.dict(sys.modules, {"backend.advisor.poller": fake}):
            out = svc._poll_once(datetime(2026, 9, 22, 10, 0))
        self.assertEqual(out, 7)
        self.assertFalse(seen["readonly"])            # 공시는 DB 에 쓴다 (설계 2.1)
        state = svc.status()["poller"]
        self.assertTrue(state["available"])
        self.assertEqual(state["last_count"], 7)
        self.assertIsNone(state["last_error"])

    def test_poller_failure_is_recorded_but_not_raised(self):
        svc = self.service()
        fake = types.ModuleType("backend.advisor.poller")

        def poll_once(store, cfg, now):
            raise RuntimeError("DART 응답 없음")

        fake.poll_once = poll_once
        with mock.patch.dict(sys.modules, {"backend.advisor.poller": fake}):
            self.assertIsNone(svc._poll_once(datetime(2026, 9, 22, 10, 0)))
        self.assertIn("DART 응답 없음", svc.status()["poller"]["last_error"])

    def test_poller_tick_stays_quiet_outside_the_window(self):
        svc = self.service()
        calls = []
        svc._poll_once = lambda now=None: calls.append(now)
        svc._poller_tick(now=datetime(2026, 9, 22, 6, 0))
        self.assertEqual(calls, [])
        svc._poller_tick(now=datetime(2026, 9, 24, 10, 0))    # 휴장일
        self.assertEqual(calls, [])
        svc._poller_tick(now=datetime(2026, 9, 22, 10, 0))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
