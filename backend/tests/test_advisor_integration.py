"""advisor 모드 통합 테스트 — 훅을 꽂은 배치 한 벌이 끝까지 도는가. **네트워크를 쓰지 않는다.**

단위 테스트는 층마다 따로 있다 (요인·포트폴리오·채점·LLM). 여기서 보는 것은 그 층들이
`advisor/hooks.py` 로 묶였을 때 **서로의 계약을 지키는가**이고, 합성 시장과 가짜 모델만 쓴다.
합성 시장 생성기는 `test_advisor_pipeline` 의 것을 그대로 가져다 쓴다 — 같은 세계에서 돌아야
단위 테스트에서 본 값과 여기서 본 값을 견줄 수 있다.

확인하는 것:
  (a) 재현 구간 60거래일: 시스템 3 + 기준선 3 포트폴리오의 NAV, 만기 도래분 채점, 요인 지표,
      해시 사슬, 목표 비중 합 = 1, 남은 예약 없음, LLM 호출 0회 (설계 7.4)
  (b) 실시간 하루: 예비(T) → 최종(T+1) → 예비(T+1). v0 종합 점수는 `stk_disclosure` 를 **보지 않고**
      llm 판단만 그 값과 조정을 쓴다 (결정 4). 최종에 예약(pending) → 다음 예비에 체결(filled).
      위험 표시가 붙은 보유 종목을 거부하면 sys_final_llm 과 sys_final_v0 가 갈린다.
  (b-2) 섹터 조정은 모델에게 **ETF 코드**로 묻고 composite 에는 **섹터 이름**으로 얹힌다.
      키 변환이 끊기면 조정이 조용히 사라지므로 목표 비중이 실제로 바뀌는지까지 본다.
  (c) LLM 이 통째로 실패해도 실행 상태는 ok 이고 llm 판단은 v0 와 같은 내용으로 기록된다 (결정 15).
  (d) 대체 규칙(결정 11): `--promote-prelim` 이 전날 예비 판단을 오늘 최종으로 승격한다.
  (e) 미래 누출 회귀: 미래 행이 DB 에 있어도 같은 날 판단의 **해시가 한 글자도 달라지지 않는다**.
  (f) 설정 파일과 모듈 DEFAULTS 가 같은 값인가 / 환율 경계(`sources.fx_known_at`)를 읽기도 지키는가.
"""
from backend.tests import _safety  # noqa: F401  맨 먼저: 실제 주문·외부 접속 차단 (_safety.py 참고)
import contextlib
import copy
import io
import json
import logging
import tempfile
import unittest
from datetime import date
from pathlib import Path

from backend.advisor import baselines, combine, ledger, portfolio, reeval
from backend.advisor import risk_flags, run as run_mod, scoring
from backend.advisor.calendar import TradingCalendar
from backend.advisor.config import load_config
from backend.advisor.factors import asof, market, stock
from backend.advisor.factors.registry import load_specs
from backend.advisor.hooks import default_hooks
from backend.advisor.llm import client as llm_client
from backend.advisor.store import Store
from backend.tests.test_advisor_evaluation import CASH, CORE
from backend.tests.test_advisor_llm import FakeClient, body
from backend.tests.test_advisor_pipeline import MARKET_DATA

# 재현 구간: 합성 시장의 60거래일. 시작일에는 이미 200거래일이 넘는 과거가 쌓여 있어야
# 이동평균 기준선(bl_sma10m)과 mkt_trend 가 값을 낸다.
REPLAY_FROM, REPLAY_TO = 239, 298
SPIKER = MARKET_DATA.SPIKER          # 최근 5거래일 급등 → spike 표시가 붙는 종목


def setUpModule():
    """실패 경로를 일부러 밟으므로 경고 로그를 결과에 섞지 않는다."""
    for name in ("advisor", "advisor.llm", "advisor.hooks"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def make_world(root, name="advisor.db", upto=None, llm_enabled=True):
    """합성 시장을 올린 임시 DB 와 그 DB 를 가리키는 설정. (cfg, store) 를 준다."""
    cfg = copy.deepcopy(load_config())
    cfg["paths"]["db"] = str(root / name)
    cfg["paths"]["ledger"] = str(root / "ledger" / f"{name}.jsonl")
    cfg["paths"]["newsgap_db"] = str(root / "newsgap.db")     # 없는 파일 → 뉴스 분류는 NO_SOURCE
    cfg["llm"]["enabled"] = bool(llm_enabled)
    store = Store(root / name)
    MARKET_DATA.write(store, upto)
    return cfg, store


def disclosure_row(rcept_no, code, title, day, seen_at, kind):
    return {"rcept_no": rcept_no, "stock_code": code, "corp_name": f"회사{code}",
            "report_nm": title, "rcept_dt": day.strftime("%Y%m%d"), "first_seen_at": seen_at,
            "first_seen_src": "dart", "ls_realkey": None, "kind": kind, "ratio": None,
            "ratio_ok": None, "body_src": None}


def fake_model(veto_codes=(), adj=0.3, level=2):
    """가짜 모델 한 마리. 스키마 모양으로 어느 작업인지 가른다 (공시 채점 / 뉴스 / 조정).

    조정은 **후보로 보낸 코드 안에서만** 답한다 — 실제 모델과 같은 제약이라야 후처리
    (모르는 코드 버리기·거부권 제한)를 시험할 수 있다.
    """
    def handler(model, contents, config):
        schema = (config or {}).get("response_schema") or {}
        if schema.get("type") == "object":                    # 공시 한 건 채점
            return body({"level": level, "rubric_id": "other_material",
                         "evidence": "본문에서 인용한 문장", "confidence": 0.9})
        props = ((schema.get("items") or {}).get("properties") or {})
        if "code" not in props:                               # 뉴스 분류 (이 테스트에서는 빈 목록)
            return body([])
        codes = list(props["code"]["enum"])
        out = []
        for code in codes:
            if code in veto_codes:
                out.append({"code": code, "adj": -0.1, "veto": True, "adopted": [],
                            "rejected": [{"factor_id": "stk_high52", "reason": "급등 뒤 되돌림"}],
                            "reason": "위험 표시가 붙어 담지 않는다"})
        if codes and codes[0] not in veto_codes:
            out.append({"code": codes[0], "adj": adj, "veto": False,
                        "adopted": ["stk_high52"], "rejected": [], "reason": "상위 후보"})
        return body(out)
    return handler


def sector_model(bumps):
    """섹터 ETF 코드에만 답하는 가짜 모델. bumps 는 {ETF 코드: adj}.

    실제 모델과 같은 제약을 지킨다 — 후보로 보낸 코드 안에서만 답한다. 종목에는 손대지 않아
    "섹터 조정만" 흘렸을 때 무엇이 달라지는지가 그대로 보인다.
    """
    def handler(model, contents, config):
        schema = (config or {}).get("response_schema") or {}
        if schema.get("type") == "object":                    # 공시 채점 (이 세계에는 공시가 없다)
            return body({"level": 0, "rubric_id": "other_material", "evidence": "", "confidence": 0.5})
        props = ((schema.get("items") or {}).get("properties") or {})
        if "code" not in props:                               # 뉴스 분류
            return body([])
        return body([{"code": c, "adj": bumps[c], "veto": False, "adopted": ["sec_flow"],
                      "rejected": [], "reason": "섹터 순위를 뒤집는다"}
                     for c in props["code"]["enum"] if c in bumps])
    return handler


def weights_of(store, run_id, variant, role=None):
    rows = store.target_weights(run_id, variant)
    return {r["asset"]: float(r["weight"]) for r in rows if role is None or r["role"] == role}


def composites(store, run_id, variant):
    return {r["entity"]: r for r in store.conn.execute(
        "SELECT * FROM composite WHERE run_id=? AND variant=?", (run_id, variant))}


# ---------------------------------------------------------------- (a) 재현 구간

class ReplayIntegrationTest(unittest.TestCase):
    """`--replay-range` 한 번으로 NAV·사후 결과·요인 지표가 모두 나오는가 (설계 7.4).

    구간을 클래스 단위로 한 번만 돌린다 (60거래일 × 두 단계). 검사마다 다시 돌리면
    같은 계산을 열 번 반복하게 된다.
    """

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        root = Path(cls.dir.name)
        cls.cfg, cls.store = make_world(root)
        cls.days = MARKET_DATA.days
        cls.hooks = default_hooks(cls.cfg)
        out = io.StringIO()
        # 하루 안의 실제 순서대로 돈다: 최종 07:40 → 예비 18:30.
        # 예비 실행이 있어야 기준선 5번(sys_prelim_llm)이 다음 날 시가에 체결할 판단을 갖는다.
        cls.ok, cls.fail = run_mod.replay_range(
            cls.cfg, cls.store, cls.days[REPLAY_FROM], cls.days[REPLAY_TO],
            hooks=cls.hooks, out=out, stages=[run_mod.STAGE_FINAL, run_mod.STAGE_PRELIM])
        cls.printed = out.getvalue().splitlines()

    @classmethod
    def tearDownClass(cls):
        cls.hooks["close"]()
        cls.store.close()
        cls.dir.cleanup()

    def rows(self, sql, params=()):
        return self.store.conn.execute(sql, params).fetchall()

    def test_every_day_ran_and_was_recorded(self):
        self.assertEqual((self.ok, self.fail), (120, 0), "거래일 60일 × 두 단계")
        self.assertIn("재현 완료", self.printed[-1])
        runs = self.rows("SELECT stage, mode, status FROM run")
        self.assertEqual(len(runs), 120)
        self.assertTrue(all(r["mode"] == "replay" and r["status"] == run_mod.STATUS_OK
                            for r in runs))
        self.assertEqual({r["stage"] for r in runs}, {"final", "prelim"})

    def test_llm_is_skipped_in_replay(self):
        self.assertEqual(self.rows("SELECT * FROM llm_call"), [],
                         "재현 모드는 모델을 부르지도 기록하지도 않는다 (설계 7.4)")
        self.assertEqual(self.rows("SELECT COUNT(*) c FROM factor_value WHERE "
                                   "factor_id='stk_disclosure' AND missing=0")[0]["c"], 0)
        self.assertTrue(all(r["llm_used"] == 0 for r in self.rows("SELECT llm_used FROM run")))

    def test_six_portfolios_have_a_nav_series(self):
        got = {r["portfolio_id"]: r["n"] for r in self.rows(
            "SELECT portfolio_id, COUNT(*) n FROM nav GROUP BY portfolio_id")}
        expected = {portfolio.portfolio_id_for(p, "replay")
                    for p in list(portfolio.SYSTEM_PORTFOLIOS) + list(baselines.BASELINES)}
        self.assertEqual(set(got), expected, "시스템 3 + 기준선 3 (설계 6.3)")
        for pid, n in got.items():
            self.assertGreaterEqual(n, 55, f"{pid} 의 NAV 가 날짜마다 이어져야 한다")
            summary = portfolio.summary(self.store, pid, self.cfg)
            self.assertEqual(summary["mode"], "replay")
            self.assertGreater(summary["nav"], 0.0)
            self.assertIsNotNone(summary["ann_vol"])

    def test_no_booking_is_left_hanging(self):
        self.assertEqual(self.rows("SELECT COUNT(*) c FROM trade WHERE status='pending'")[0]["c"], 0,
                         "정산이 예약을 모두 닫았다 (체결 또는 skipped)")
        self.assertGreater(self.rows("SELECT COUNT(*) c FROM trade WHERE status='filled'")[0]["c"], 0)

    def test_target_weights_always_sum_to_one(self):
        rows = self.rows("SELECT run_id, variant, SUM(weight) s, COUNT(*) n FROM target_weight "
                         "GROUP BY run_id, variant")
        self.assertEqual(len(rows), 240, "실행마다 v0 와 llm 두 벌")
        for r in rows:
            self.assertAlmostEqual(r["s"], 1.0, places=9, msg=f"run {r['run_id']} {r['variant']}")

    def test_outcomes_matured_and_were_scored(self):
        counts = {r["eval_status"]: r["n"] for r in self.rows(
            "SELECT eval_status, COUNT(*) n FROM outcome GROUP BY eval_status")}
        self.assertGreater(counts.get("ok", 0), 1000, "만기가 된 조합이 채점됐다")
        composite = self.rows("SELECT variant, COUNT(*) n FROM outcome WHERE factor_id='composite' "
                              "GROUP BY variant")
        self.assertEqual({r["variant"] for r in composite}, {"v0", "llm"},
                         "종합 점수는 판단 버전마다 따로 채점된다 (설계 7.2)")
        # 채점 구간의 끝이 판단 시각에 알 수 있는 날짜를 넘지 않는다 (설계 2.3)
        latest = self.rows("SELECT MAX(end_date) d FROM outcome WHERE eval_status='ok'")[0]["d"]
        self.assertLessEqual(latest, str(self.days[REPLAY_TO]))

    def test_factor_metrics_are_written_with_n_eff(self):
        batch = self.rows("SELECT * FROM factor_metric WHERE computed_at="
                          "(SELECT MAX(computed_at) FROM factor_metric)")
        self.assertTrue(batch)
        by_id = {(r["stage"], r["variant"], r["factor_id"], r["horizon"]): r for r in batch}
        key = ("final", "v0", "stk_high52", 20)
        self.assertIn(key, by_id, "종목 요인의 순위 상관이 나와야 한다")
        row = by_id[key]
        self.assertGreater(row["n_days"], 0)
        self.assertAlmostEqual(row["n_eff"], row["n_days"] / 20.0)
        self.assertIsNotNone(row["rank_ic_mean"])
        self.assertEqual(len(json.loads(row["bucket_json"])["q"]), 5, "5분위 평균 초과 수익")
        # 채점 세 단계가 모두 돌았다는 사실은 run.note 에 남는다. 위험 표시 채점은 표시가 붙은
        # 실행이 이 합성 시장에서는 마지막 며칠뿐이라(급등·공시가 끝에 몰려 있다) 아직 만기가
        # 오지 않아 행이 없다 — '없음'과 '안 돌았음'을 구분하려고 건수를 함께 본다.
        note = json.loads(self.rows(
            "SELECT note FROM run WHERE stage='prelim' ORDER BY run_id DESC LIMIT 1")[0]["note"])
        self.assertEqual(set(note["score_outcomes"]),
                         {"outcome", "factor_metric", "risk_flag_metric"})
        self.assertGreater(note["score_outcomes"]["factor_metric"], 0)
        # 조정 기여는 조정 전후 종합 점수의 순위 상관 차이다 (결정 4). 재현에는 조정이 없어 0 이다.
        contrib = scoring.llm_adjust_contribution(
            [dict(r) for r in batch if r["variant"] is not None])
        self.assertTrue(contrib)
        self.assertTrue(all(abs(c["contribution"]) < 1e-12 for c in contrib))

    def test_ledger_chain_verifies_and_stays_out_of_the_file(self):
        self.assertEqual(ledger.verify_chain(self.store, "replay"), (True, None))
        self.assertFalse(Path(self.cfg["paths"]["ledger"]).exists(),
                         "재현 기록은 원장 파일에 섞지 않는다 (설계 7.4)")

    def test_reeval_runs_on_the_replay_records(self):
        """사후 재평가(결정 6)가 이 기록 위에서 실제로 돈다 — 재현 모드를 둔 이유의 절반이다."""
        cal = TradingCalendar(self.cfg, self.store)
        challenger = copy.deepcopy(self.cfg)
        challenger["factors"]["stk_flow"]["weight"] = 2
        got = reeval.run_reeval(self.store, self.cfg, cal, challenger, mode="replay")
        self.assertGreater(got["champion"]["n_runs"], 50)
        self.assertEqual(got["champion"]["n_runs"], got["challenger"]["n_runs"])
        self.assertIsNotNone(got["champion"]["nav"]["nav"])


# ---------------------------------------------------------------- (b) 실시간 하루

class LiveSequenceTest(unittest.TestCase):
    """예비(T) → 최종(T+1) → 예비(T+1). 가짜 모델로 LLM 단계를 돌린다."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        self.cfg, self.store = make_world(root)
        self.addCleanup(self.store.close)
        self.days = MARKET_DATA.days
        self.T, self.T1 = self.days[-2], self.days[-1]
        self.specs = load_specs(self.cfg)
        # LLM 이 수준을 정하는 공시 한 건 (decided_by=llm 인 '주요경영사항').
        # 이 종목의 종합 점수는 **llm 판단에서만** 달라져야 한다 (결정 4).
        self.disclosed = "100013"
        self.store.upsert_disclosure(disclosure_row(
            "INTEG-1", self.disclosed, "투자판단관련주요경영사항", self.T,
            f"{self.T}T09:00:00.000", "주요경영사항"))
        self.store.commit()
        self.fake = FakeClient(fake_model(veto_codes=(SPIKER,)))
        self.hooks = default_hooks(self.cfg, client=self.fake)
        self.addCleanup(self.hooks["close"])

    def run_once(self, stage, day):
        return run_mod.run_once(self.store, self.cfg, stage, day, hooks=self.hooks, no_ingest=True)

    def test_prelim_final_prelim(self):
        prelim = self.run_once("prelim", self.T)
        final = self.run_once("final", self.T1)

        # --- 1. 예약은 걸렸고 아직 체결되지 않았다 (설계 6.2의 예약 → 확정 두 단계)
        pending = self.store.conn.execute(
            "SELECT portfolio_id, COUNT(*) n FROM trade WHERE status='pending' AND date=? "
            "GROUP BY portfolio_id", (str(self.T1),)).fetchall()
        booked = {r["portfolio_id"] for r in pending}
        self.assertEqual(booked, {portfolio.portfolio_id_for(p, "live") for p in
                                  list(portfolio.SYSTEM_PORTFOLIOS) + list(baselines.BASELINES)},
                         "시스템 3 + 기준선 3 이 같은 날 시가에 체결을 예약한다")
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM nav").fetchone()[0], 0, "아직 그날 시가를 모른다")
        src = {r["src_run_id"] for r in self.store.conn.execute(
            "SELECT DISTINCT src_run_id FROM trade WHERE portfolio_id='sys_prelim_llm'")}
        self.assertEqual(src, {prelim}, "예비 포트폴리오는 전날 저녁 판단으로 체결한다 (기준선 5)")

        # --- 2. v0 는 LLM 요인을 보지 않는다 (결정 4)
        v0, llm = composites(self.store, final, "v0"), composites(self.store, final, "llm")
        row = self.store.conn.execute(
            "SELECT * FROM factor_value WHERE run_id=? AND entity=? AND factor_id='stk_disclosure'",
            (final, self.disclosed)).fetchone()
        self.assertEqual(row["missing"], 0, "LLM 단계가 결측 자리를 채웠다")
        self.assertGreater(row["score"], 0.0)
        self.assertAlmostEqual(v0[self.disclosed]["base_score"],
                               self.layer_score(final, self.disclosed, drop="stk_disclosure"),
                               msg="v0 종합 점수는 코드 요인만으로 만든다")
        self.assertAlmostEqual(llm[self.disclosed]["base_score"],
                               self.layer_score(final, self.disclosed),
                               msg="llm 종합 점수는 LLM 이 채운 요인까지 쓴다")
        self.assertNotAlmostEqual(v0[self.disclosed]["base_score"],
                                  llm[self.disclosed]["base_score"])
        untouched = next(c for c in v0 if c not in (self.disclosed, "MARKET")
                         and c in llm and llm[c]["adj"] == 0.0
                         and self.store.conn.execute(
                             "SELECT missing FROM factor_value WHERE run_id=? AND entity=? AND "
                             "factor_id='stk_disclosure'", (final, c)).fetchone()["missing"] == 1)
        self.assertAlmostEqual(v0[untouched]["base_score"], llm[untouched]["base_score"],
                               msg="공시가 없는 종목은 두 판단의 기본 점수가 같다")

        # --- 3. 조정과 거부는 llm 판단에만 얹힌다 (설계 5.6)
        self.assertTrue(llm[SPIKER]["vetoed"], "위험 표시가 붙은 자산의 거부는 받아들인다")
        self.assertEqual(v0[SPIKER]["vetoed"], 0, "v0 는 거부를 모른다")
        self.assertEqual(v0[SPIKER]["adj"], 0.0)
        self.assertAlmostEqual(llm[SPIKER]["final_score"] - llm[SPIKER]["base_score"],
                               llm[SPIKER]["adj"], msg="final = base + adj")
        self.assertIn("위험 표시", llm[SPIKER]["reason"])
        self.assertEqual(json.loads(llm[SPIKER]["rejected_json"])[0]["factor_id"], "stk_high52")

        # 섹터 조정은 **섹터 이름** 행에 얹혀야 한다. 모델에게는 담을 ETF 코드로 물었지만
        # composite 의 entity 는 섹터 이름이라, 키를 안 바꾸면 조정이 조용히 사라진다.
        adjusted_sectors = {e: r for e, r in llm.items()
                            if r["layer"] == "sector" and r["adj"] != 0.0}
        self.assertTrue(adjusted_sectors, "섹터 조정이 composite 에 남아야 한다")
        for code in self.cfg["universe"]["sector_etfs"].values():
            self.assertNotIn(code, llm, "섹터 행의 id 는 ETF 코드가 아니라 이름이다")
        for entity, row in adjusted_sectors.items():
            self.assertIn(entity, self.cfg["universe"]["sector_etfs"])
            self.assertAlmostEqual(row["final_score"] - row["base_score"], row["adj"])
            self.assertAlmostEqual(abs(row["adj"]), self.cfg["llm"]["adj_cap"],
                                   msg="±adj_cap 으로 다시 자른다")
            self.assertEqual(v0[entity]["adj"], 0.0)

        v0_stocks = set(weights_of(self.store, final, "v0", role="stock"))
        llm_stocks = set(weights_of(self.store, final, "llm", role="stock"))
        self.assertIn(SPIKER, v0_stocks)
        self.assertNotIn(SPIKER, llm_stocks, "거부된 종목은 llm 포트폴리오가 담지 않는다")
        self.assertNotEqual(v0_stocks, llm_stocks, "sys_final_llm 과 sys_final_v0 가 갈린다")
        for variant in ("v0", "llm"):
            self.assertAlmostEqual(sum(weights_of(self.store, final, variant).values()), 1.0,
                                   places=9)

        # --- 4. 실행 기록
        run = self.store.conn.execute("SELECT * FROM run WHERE run_id=?", (final,)).fetchone()
        self.assertEqual((run["status"], run["llm_used"]), (run_mod.STATUS_OK, 1))
        note = json.loads(run["note"])
        self.assertIn(note["llm_factors"]["status"], ("OK", "PARTIAL"))
        self.assertEqual(note["llm_adjust"]["status"], "OK")
        self.assertEqual(note["book_trades"]["fill_date"], str(self.T1))
        self.assertEqual(ledger.verify_chain(self.store, "live"), (True, None))
        self.assertEqual(len(Path(self.cfg["paths"]["ledger"]).read_text(
            encoding="utf-8").splitlines()), 4, "실행 둘 × 판단 두 벌")

        # --- 5. 다음 예비가 그날 시가로 체결을 확정한다
        self.run_once("prelim", self.T1)
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM trade WHERE status='pending'").fetchone()[0], 0)
        navs = {r["portfolio_id"]: r["nav"] for r in self.store.conn.execute(
            "SELECT portfolio_id, nav FROM nav WHERE date=?", (str(self.T1),))}
        self.assertEqual(set(navs), booked, "예약한 포트폴리오가 모두 정산됐다")
        held = self.store.conn.execute(
            "SELECT asset FROM holding WHERE portfolio_id='sys_final_llm' AND date=?",
            (str(self.T1),)).fetchall()
        self.assertNotIn(SPIKER, {r["asset"] for r in held})
        self.assertIn(CASH, {r["asset"] for r in held})
        self.assertIn(CORE, {r["asset"] for r in held})

    def test_holdings_come_from_the_settled_portfolio(self):
        """정산이 한 번이라도 돌면 이력(보유)은 **실제 보유**에서 온다 (run.py 의 기본값이 아니라)."""
        self.run_once("prelim", self.T)
        self.run_once("final", self.T1)
        self.run_once("prelim", self.T1)
        settled = {r["asset"] for r in self.store.conn.execute(
            "SELECT asset FROM holding WHERE portfolio_id='sys_final_v0' AND date=?",
            (str(self.T1),))}
        seen = {}

        def spy(ctx):
            seen["holdings"] = set(ctx.holdings)
            return {}

        hooks = dict(self.hooks, llm_adjust=spy)
        run_mod.run_once(self.store, self.cfg, "final", self.T1, hooks=hooks, no_ingest=True)
        self.assertTrue(seen["holdings"])
        self.assertEqual(seen["holdings"], settled - {CASH, CORE} - set(
            (self.cfg["universe"].get("sector_etfs") or {}).values()),
            "현금·핵심·섹터 ETF 는 '보유 종목'이 아니다")

    def layer_score(self, run_id, entity, drop=None):
        """저장된 요인 값으로 다시 만든 종합 점수 (섹터 기울기 포함). 계약을 손으로 검산한다."""
        scores = {}
        for r in self.store.conn.execute(
                "SELECT entity, factor_id, score, missing FROM factor_value WHERE run_id=?",
                (run_id,)):
            if r["factor_id"] == drop:
                continue
            scores.setdefault(r["entity"], {})[r["factor_id"]] = (r["score"], bool(r["missing"]))
        uni = asof.universe_snapshot(self.store, self.cfg, self.T1)
        sector_scores = combine.sector_scores(
            {s: scores.get(s, {}) for s in uni["sectors"]}, self.specs)
        layer = combine.layer_score(scores.get(entity, {}), self.specs, layer="stock")
        composite = combine.stock_composites({entity: layer}, uni["sector_of"], sector_scores,
                                             self.cfg["combine"]["sector_tilt"])
        return composite[entity]


# ---------------------------------------------------------------- (b-2) 섹터 조정의 키

class SectorAdjustmentTest(unittest.TestCase):
    """섹터 조정은 **ETF 코드로 묻고 섹터 이름으로 얹힌다**. 그 변환이 어디서 끊기는지 본다.

    `llm/adjust.py` 는 모델에게 담을 자산(ETF 코드)을 보여 주고 {코드: Adjustment} 를 돌려준다.
    그런데 `run.py` 의 훅 계약은 composite 의 entity 이고, 섹터 층의 entity 는 **섹터 이름**이다
    (점수·근거가 그 이름으로 저장돼 있다). 어댑터(`hooks.llm_adjust`)가 키를 되돌리지 않으면
    조정은 어느 composite 행에도 얹히지 않고 조용히 사라지고, 목표 비중도 v0 와 같아진다.
    둘 다 "조정이 0"으로 보이므로 기록만 봐서는 기권과 구분되지 않는다 — 그래서 여기서는
    **담는 섹터가 실제로 바뀌는가**까지 본다.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cfg, self.store = make_world(Path(self.dir.name))
        self.addCleanup(self.store.close)
        self.day = MARKET_DATA.days[-1]
        self.cap = float(self.cfg["llm"]["adj_cap"])
        self.etf_of = dict(self.cfg["universe"]["sector_etfs"])

    def sector_rows(self, run_id, variant):
        return {r["entity"]: r for r in self.store.conn.execute(
            "SELECT * FROM composite WHERE run_id=? AND variant=? AND layer='sector'",
            (run_id, variant))}

    def test_sector_adjustment_lands_on_the_name_and_moves_the_weights(self):
        # --- 1. 조정 없는 판단으로 "지금 담는 섹터"와 "바로 다음 섹터"를 고른다
        base_run = run_mod.run_once(
            self.store, self.cfg, "final", self.day, no_ingest=True,
            hooks=default_hooks(self.cfg, with_llm=False, with_portfolio=False))
        scored = {e: r["base_score"] for e, r in self.sector_rows(base_run, "v0").items()
                  if r["base_score"] is not None}
        ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
        (held, held_score), (rival, rival_score) = ranked[0], ranked[1]
        # 조정 상한(±adj_cap)만으로 순위가 뒤집히는 자리인지 먼저 확인한다. 합성 시장이 바뀌어
        # 이 전제가 깨지면 아래의 실패가 "조정이 안 먹혔다"로 잘못 읽히기 때문이다.
        self.assertTrue(0.0 < held_score <= self.cap, f"{held} 점수 {held_score}")
        self.assertTrue(-self.cap < rival_score <= 0.0, f"{rival} 점수 {rival_score}")

        # --- 2. 같은 날을 다시, 담는 섹터는 내리고 다음 섹터는 올리는 가짜 모델로 돈다
        seen = {}
        hooks = default_hooks(self.cfg, client=FakeClient(sector_model(
            {self.etf_of[held]: -self.cap, self.etf_of[rival]: self.cap})))
        self.addCleanup(hooks["close"])
        adapter = hooks["llm_adjust"]

        def spy(ctx):
            out = adapter(ctx)
            seen.update(out)
            return out

        run_id = run_mod.run_once(self.store, self.cfg, "final", self.day,
                                  hooks=dict(hooks, llm_adjust=spy), no_ingest=True)

        # --- 3. 어댑터가 run.py 에 넘기는 키는 섹터 **이름**이다 (코드가 아니다)
        self.assertEqual(set(seen), {held, rival}, "훅이 돌려주는 키가 섹터 이름이어야 한다")
        self.assertFalse(set(seen) & set(self.etf_of.values()), "ETF 코드가 그대로 새어 나왔다")

        # --- 4. 조정은 그 이름의 composite 행에 얹힌다 (llm 판단에만)
        v0, llm = self.sector_rows(run_id, "v0"), self.sector_rows(run_id, "llm")
        entities = set(composites(self.store, run_id, "llm"))
        self.assertFalse(entities & set(self.etf_of.values()),
                         "섹터 행의 id 는 ETF 코드가 아니라 이름이다")
        for sector, adj in ((held, -self.cap), (rival, self.cap)):
            self.assertAlmostEqual(llm[sector]["adj"], adj, msg=sector)
            self.assertAlmostEqual(llm[sector]["final_score"],
                                   llm[sector]["base_score"] + adj, msg=sector)
            self.assertEqual(v0[sector]["adj"], 0.0, "v0 는 조정을 모른다")
            self.assertAlmostEqual(v0[sector]["base_score"], llm[sector]["base_score"],
                                   msg="섹터에는 LLM 이 채우는 요인이 없어 기본 점수가 같다")

        # --- 5. 그 조정이 목표 비중까지 간다 — 담는 섹터 ETF 가 바뀐다
        v0_sectors = set(weights_of(self.store, run_id, "v0", role="sector"))
        llm_sectors = set(weights_of(self.store, run_id, "llm", role="sector"))
        self.assertEqual(v0_sectors, {self.etf_of[held]})
        self.assertEqual(llm_sectors, {self.etf_of[rival]},
                         "조정된 섹터 점수로 다시 고른다 (설계 6.1)")
        self.assertAlmostEqual(sum(weights_of(self.store, run_id, "llm").values()), 1.0, places=9)
        # 종목에는 손대지 않았으므로 종목 비중은 두 판단이 같다 (섹터 조정이 새어 나가지 않는다)
        self.assertEqual(weights_of(self.store, run_id, "v0", role="stock"),
                         weights_of(self.store, run_id, "llm", role="stock"))
        self.assertEqual(json.loads(self.store.conn.execute(
            "SELECT note FROM run WHERE run_id=?", (run_id,)).fetchone()[0])["llm_adjust"]["status"],
            "OK")


# ---------------------------------------------------------------- (c) LLM 실패

class LLMFailureTest(unittest.TestCase):
    """모델이 통째로 죽어도 그날 판단은 나온다 (결정 4·15). 기권은 '중립'이 아니라 '조정 없음'이다."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cfg, self.store = make_world(Path(self.dir.name))
        self.addCleanup(self.store.close)
        self.day = MARKET_DATA.days[-1]
        # 채점할 공시가 있어야 '요인 단계가 실패했다'를 시험할 수 있다 (전날 09시에 인지).
        self.store.upsert_disclosure(disclosure_row(
            "INTEG-F1", "100013", "투자판단관련주요경영사항", MARKET_DATA.days[-2],
            f"{MARKET_DATA.days[-2]}T09:00:00.000", "주요경영사항"))
        self.store.commit()

    def test_broken_client_abstains_and_records_why(self):
        def boom(model, contents, config):
            raise RuntimeError("모델이 응답하지 않는다")

        hooks = default_hooks(self.cfg, client=FakeClient(boom))
        self.addCleanup(hooks["close"])
        run_id = run_mod.run_once(self.store, self.cfg, "final", self.day, hooks=hooks,
                                  no_ingest=True)
        run = self.store.conn.execute("SELECT * FROM run WHERE run_id=?", (run_id,)).fetchone()
        self.assertEqual(run["status"], run_mod.STATUS_OK, "LLM 실패는 배치 실패가 아니다")
        self.assertEqual(run["llm_used"], 0, "성공한 호출이 하나도 없으면 LLM 을 쓴 것이 아니다")

        note = json.loads(run["note"])
        self.assertEqual(note["llm_adjust"]["status"], "ERROR", "run.note 에 상태가 남는다")
        # 요인 단계의 status 는 "무엇에 막혔는가"(DISABLED·예산)이고, 호출 하나하나의 실패는
        # 건수로 남는다 (llm/stage.py 의 규약). 둘을 합쳐 봐야 "그날 LLM 이 없었다"가 읽힌다.
        self.assertGreater(note["llm_factors"]["detail"]["disclosure"]["error"], 0)
        self.assertEqual(note["llm_factors"]["detail"]["disclosure"]["llm"], 0,
                         "실패한 건은 점수에 들어가지 않는다 (기권)")
        self.assertTrue(self.store.conn.execute(
            "SELECT COUNT(*) FROM llm_call WHERE status='ERROR'").fetchone()[0] > 0,
            "왜 값이 없는지는 llm_call 에 남는다")

        # llm 판단은 기록되지만 내용은 v0 와 같다 (설계 5.6의 기권)
        self.assertEqual(weights_of(self.store, run_id, "v0"),
                         weights_of(self.store, run_id, "llm"))
        v0, llm = composites(self.store, run_id, "v0"), composites(self.store, run_id, "llm")
        self.assertEqual(set(v0), set(llm))
        for entity, row in v0.items():
            self.assertEqual((row["base_score"], row["adj"]),
                             (llm[entity]["base_score"], llm[entity]["adj"]), entity)
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM factor_value WHERE run_id=? AND factor_id='stk_disclosure' "
            "AND missing=0", (run_id,)).fetchone()[0], 0, "채점하지 못한 공시는 결측으로 남는다")

    def test_disabled_llm_still_produces_a_v0_decision(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["llm"]["enabled"] = False
        hooks = default_hooks(cfg, client=None)
        self.addCleanup(hooks["close"])
        run_id = run_mod.run_once(self.store, cfg, "final", self.day, hooks=hooks, no_ingest=True)
        run = self.store.conn.execute("SELECT * FROM run WHERE run_id=?", (run_id,)).fetchone()
        self.assertEqual((run["status"], run["llm_used"]), (run_mod.STATUS_OK, 0))
        self.assertTrue(weights_of(self.store, run_id, "v0"))
        self.assertEqual(json.loads(run["note"])["llm_adjust"]["status"], "DISABLED")


# ---------------------------------------------------------------- (d) 대체 규칙

class PromotePrelimTest(unittest.TestCase):
    """결정 11: 08:50 까지 최종 판단이 없으면 전날 예비 판단을 그대로 승격한다."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cfg, self.store = make_world(Path(self.dir.name))
        self.addCleanup(self.store.close)
        self.days = MARKET_DATA.days
        self.T, self.T1 = self.days[-2], self.days[-1]
        self.hooks = default_hooks(self.cfg, client=FakeClient(fake_model()))
        self.addCleanup(self.hooks["close"])
        self.prelim = run_mod.run_once(self.store, self.cfg, "prelim", self.T, hooks=self.hooks,
                                       no_ingest=True)

    def promote(self, as_of=None):
        out = io.StringIO()
        code, info = run_mod.promote_prelim(self.store, self.cfg, as_of or self.T1,
                                            hooks=self.hooks, out=out)
        return code, info, out.getvalue()

    def test_promotes_yesterdays_prelim_and_books_it(self):
        code, info, printed = self.promote()
        self.assertEqual(code, 0)
        self.assertTrue(info["promoted"])
        self.assertIn("승격 완료", printed)

        run = self.store.conn.execute("SELECT * FROM run WHERE run_id=?",
                                      (info["run_id"],)).fetchone()
        self.assertEqual((run["stage"], run["as_of"], run["mode"]), ("final", str(self.T1), "live"))
        self.assertEqual((run["status"], run["fallback_used"]), (run_mod.STATUS_OK, 1))
        self.assertIn("대체 규칙", run["note"])

        # 두 버전 모두 전날 예비 판단 그대로다 (점수를 다시 계산하지 않는다)
        self.assertEqual(sorted(info["variants"]), ["llm", "v0"])
        for variant in ("v0", "llm"):
            self.assertEqual(weights_of(self.store, info["run_id"], variant),
                             weights_of(self.store, self.prelim, variant))
            src = composites(self.store, self.prelim, variant)
            got = composites(self.store, info["run_id"], variant)
            self.assertEqual(set(src), set(got))
            self.assertEqual([got[e]["final_score"] for e in sorted(got)],
                             [src[e]["final_score"] for e in sorted(src)])
        risk = self.store.conn.execute(
            "SELECT risk_weight FROM decision WHERE run_id=? AND variant='v0'",
            (info["run_id"],)).fetchone()[0]
        self.assertAlmostEqual(risk, self.store.conn.execute(
            "SELECT risk_weight FROM decision WHERE run_id=? AND variant='v0'",
            (self.prelim,)).fetchone()[0])

        # 사슬은 새로 잇고 원장에도 남는다 — 승격도 그날 실제로 낸 판단이다
        self.assertEqual(ledger.verify_chain(self.store, "live"), (True, None))
        self.assertEqual(len(Path(self.cfg["paths"]["ledger"]).read_text(
            encoding="utf-8").splitlines()), 4)

        # 체결 예약이 걸렸고, 그 출처가 승격된 실행이다
        booked = {r["portfolio_id"]: r["src_run_id"] for r in self.store.conn.execute(
            "SELECT portfolio_id, src_run_id FROM trade WHERE date=? AND status='pending'",
            (str(self.T1),))}
        self.assertEqual(booked.get("sys_final_v0"), info["run_id"])
        self.assertEqual(booked.get("sys_final_llm"), info["run_id"])
        self.assertIn("bl_kodex200", booked)

    def test_no_op_when_a_final_run_already_succeeded(self):
        run_mod.run_once(self.store, self.cfg, "final", self.T1, hooks=self.hooks, no_ingest=True)
        before = self.store.conn.execute("SELECT COUNT(*) FROM run").fetchone()[0]
        code, info, printed = self.promote()
        self.assertEqual(code, 0)
        self.assertFalse(info["promoted"])
        self.assertEqual(info["reason"], "final_ok")
        self.assertIn("승격하지 않습니다", printed)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM run").fetchone()[0], before,
                         "실행 기록을 새로 만들지 않는다")

    def test_a_failed_final_run_is_replaced(self):
        """상태가 ok 가 아닌 최종 실행은 '없는 것'으로 본다 (목표 비중을 못 남기고 죽은 실행)."""
        dead = self.store.start_run("final", str(self.T1), mode="live")
        self.store.finish_run(dead, status=run_mod.STATUS_ERROR)
        self.store.commit()
        code, info, _ = self.promote()
        self.assertEqual(code, 0)
        self.assertTrue(info["promoted"])
        self.assertNotEqual(info["run_id"], dead)

    def test_holiday_and_missing_prelim_are_reported(self):
        saturday = date(self.T1.year, self.T1.month, self.T1.day)
        while saturday.weekday() != 5:
            saturday = saturday.replace(day=saturday.day + 1)
        code, info, printed = self.promote(as_of=saturday)
        self.assertEqual((code, info["reason"]), (0, "holiday"))
        self.assertIn("휴장일", printed)

        code, info, printed = self.promote(as_of=self.days[10])   # 그 전날 예비 판단이 없다
        self.assertEqual((code, info["reason"]), (1, "no_prelim"))
        self.assertIn("승격할 수 없습니다", printed)

    def test_cli_flag(self):
        cfg_path = Path(self.dir.name) / "advisor.config.yaml"
        import yaml
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.cfg, f, allow_unicode=True, sort_keys=False)
        self.store.commit()
        self.store.close()
        with contextlib.redirect_stdout(io.StringIO()):
            code = run_mod.main(["--promote-prelim", "--as-of", str(self.T1),
                                 "--no-llm", "--config", str(cfg_path),
                                 "--db", self.cfg["paths"]["db"]])
        self.assertEqual(code, 0)
        with Store(self.cfg["paths"]["db"]) as store:
            row = store.conn.execute(
                "SELECT fallback_used, status FROM run WHERE stage='final' AND as_of=? "
                "ORDER BY run_id DESC LIMIT 1", (str(self.T1),)).fetchone()
            self.assertEqual((row["fallback_used"], row["status"]), (1, run_mod.STATUS_OK))


# ---------------------------------------------------------------- (e) 미래 누출

class LookAheadSystemTest(unittest.TestCase):
    """단위 테스트가 요인 값으로 보는 것을 여기서는 **판단 해시**로 본다.

    파이프라인 전체(LLM 단계·체결·채점 포함)를 돌려도, DB 에 미래 행이 있고 없고가
    그날 판단을 한 글자도 바꾸지 않아야 한다 (설계 2.3).
    """

    UPTO = 219

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.day = MARKET_DATA.days[self.UPTO]
        # 두 세계가 **같은 설정**을 써야 한다 — config_hash 가 판단 줄에 들어가므로 경로 하나만
        # 달라도 해시가 달라진다. 저장소 파일만 따로 두고 설정은 한 벌이다.
        self.cfg, _ = make_world(self.root, name="unused.db", upto=0)

    def decisions(self, name, upto):
        store = Store(self.root / name)
        self.addCleanup(store.close)
        MARKET_DATA.write(store, upto)
        hooks = default_hooks(self.cfg, client=FakeClient(fake_model(veto_codes=(SPIKER,))))
        self.addCleanup(hooks["close"])
        run_id = run_mod.run_once(store, self.cfg, "final", self.day, hooks=hooks, no_ingest=True)
        rows = store.conn.execute(
            "SELECT variant, market_score, risk_weight, record_hash, prev_hash FROM decision "
            "WHERE run_id=? ORDER BY variant", (run_id,)).fetchall()
        weights = {v: weights_of(store, run_id, v) for v in ("v0", "llm")}
        return [tuple(r) for r in rows], weights

    def test_future_rows_do_not_change_the_decision(self):
        known, known_w = self.decisions("known.db", self.UPTO)
        allrows, all_w = self.decisions("all.db", None)
        self.assertEqual(len(known), 2)
        self.assertEqual(known, allrows, "판단 해시가 같다 = 판단에 미래가 섞이지 않았다")
        self.assertEqual(known_w, all_w)


# ---------------------------------------------------------------- (f) 설정·시점 규칙

class ConfigDefaultsTest(unittest.TestCase):
    """모듈의 DEFAULTS 는 "YAML 에 아직 없는 값"의 임시 거처다. 옮겨 적은 값이 어긋나면
    설정을 고쳐도 동작이 안 바뀌거나(코드가 이김) 조용히 다른 값으로 돈다."""

    MODULES = {"baselines": baselines, "portfolio": portfolio, "reeval": reeval,
               "risk_flags": risk_flags, "run": run_mod, "scoring": scoring, "asof": asof,
               "market": market, "stock": stock, "llm.client": llm_client}

    def setUp(self):
        self.cfg = load_config()

    def lookup(self, dotted):
        node = self.cfg
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return KeyError
            node = node[part]
        return node

    def test_every_module_default_is_in_the_config_file(self):
        for name, module in self.MODULES.items():
            for key, value in getattr(module, "DEFAULTS", {}).items():
                got = self.lookup(key)
                self.assertIsNot(got, KeyError, f"{name}.DEFAULTS 의 {key} 가 설정에 없습니다")
                self.assertEqual(got, value, f"{key} 가 코드 기본값과 다릅니다")

    def test_no_scalar_slipped_under_factors(self):
        for fid, meta in self.cfg["factors"].items():
            self.assertIsInstance(meta, dict, f"factors.{fid}")

    def test_llm_price_is_filled_so_the_budget_check_is_real(self):
        for model in self.cfg["llm"]["models"].values():
            price = self.cfg["llm"]["price_per_mtok"][model]
            self.assertIsNotNone(price["in"], model)
            self.assertIsNotNone(price["out"], model)
            self.assertGreater(price["out"], price["in"], "출력 단가가 더 비싸다 (사고 토큰 포함)")


class FxKnownAtTest(unittest.TestCase):
    """환율은 24시간 거래라 확정 시각이 미국 지수와 다르다 (`sources.fx_known_at`).

    수집(`sources/base.py`)이 계열마다 다른 규칙으로 거르므로 읽기도 같은 규칙이어야 한다 —
    어긋나면 새벽에 돌린 배치에서 환율만 하루 사라지거나 미국 지수가 하루 새어 들어온다.
    """

    def setUp(self):
        self.cfg = load_config()

    def test_fx_boundary_is_the_previous_day_at_any_clock_time(self):
        for at in ("2026-09-23T00:00", "2026-09-23T06:59", "2026-09-23T07:40",
                   "2026-09-23T18:30"):
            self.assertEqual(asof.last_fx_date(at, self.cfg), date(2026, 9, 22), at)

    def test_fx_and_us_differ_before_the_us_boundary(self):
        early = "2026-09-23T06:59"
        self.assertEqual(asof.last_us_date(early, self.cfg), date(2026, 9, 21))
        self.assertEqual(asof.last_series_date("USDKRW", early, self.cfg), date(2026, 9, 22))
        self.assertEqual(asof.last_series_date("SP500", early, self.cfg), date(2026, 9, 21))
        after = "2026-09-23T07:40"
        self.assertEqual(asof.last_series_date("USDKRW", after, self.cfg),
                         asof.last_series_date("SOX", after, self.cfg),
                         "07:00 을 넘기면 두 규칙이 같은 날짜를 가리킨다")

    def test_the_setting_wins(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["sources"]["fx_known_at"] = "09:00"
        self.assertEqual(asof.last_fx_date("2026-09-23T08:59", cfg), date(2026, 9, 21))
        self.assertEqual(asof.last_fx_date("2026-09-23T09:00", cfg), date(2026, 9, 22))

    def test_overnight_factor_uses_the_fx_rule(self):
        """06:59 에 돌려도 전날 환율은 쓴다 — 수집이 이미 넣어 둔 값이다."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "fx.db")
        self.addCleanup(store.close)
        store.put_market([{"series": "USDKRW", "date": "2026-09-21", "value": 1000.0},
                          {"series": "USDKRW", "date": "2026-09-22", "value": 990.0}])
        store.commit()
        spec = load_specs(self.cfg)["mkt_overnight"]
        raw, _, missing = market.mkt_overnight(store, self.cfg, spec, "2026-09-23T06:59")["MARKET"]
        self.assertAlmostEqual(raw, 0.01, msg="원/달러 하락(원화 강세)은 + 로 들어간다")
        self.assertTrue(missing, "비교할 과거 분포가 없으면 점수는 결측")


if __name__ == "__main__":
    unittest.main()
