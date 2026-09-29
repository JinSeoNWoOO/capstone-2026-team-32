"""가상 포트폴리오·기준선·채점·요인 지표·도전 안 재평가 단위 테스트. 네트워크를 쓰지 않는다.

확인하는 것:
  - 포트폴리오 회계: 손으로 계산되는 NAV(슬리피지·수수료·거래세), ETF 매도 면세, 리밸런싱 기준,
    체결 불가(일봉 없음)의 skipped 처리, 같은 날 재예약, 두 구간(전일 종가→시가→종가) 분리
  - 체결할 판단 고르기: llm 있음/없음, 전날 예비 판단 승격, live 와 replay 분리
  - 기준선: 이동평균 규칙이 판단 시각에 알 수 있던 값만 보는가, 60/40 의 월 1회 리밸런싱
  - 채점: 휴장일을 건너뛴 시작·종료일, 데이터 없음(unable), 만기 전 미채점, 미래 일봉을 넣어도
    이미 끝난 채점이 흔들리지 않음
  - 요인 지표: 완전 일치 순위 → IC 1, 뒤집으면 −1, n_eff = 관측 일수 ÷ 채점 기간, 시장 요인 경로
  - 재평가: 가중치 0 인 요인을 켜면 순위가 바뀐다, 살아 있는 표에는 한 줄도 쓰지 않는다
"""
from backend.tests import _safety  # noqa: F401  맨 먼저: 실제 주문·외부 접속 차단 (_safety.py 참고)
import copy
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from backend.advisor import baselines, portfolio, reeval, scoring
from backend.advisor.calendar import TradingCalendar, to_date
from backend.advisor.config import load_config
from backend.advisor.store import Store

CORE = "069500"          # KODEX 200 (핵심)
CASH = "459580"          # 현금 대용 ETF
SEC_ETF = "091160"       # 반도체 섹터 ETF
BANK_ETF = "091170"      # 은행 섹터 ETF

SLIP_FEE = 0.0010 + 0.00015      # 편도 체결 비용 + 수수료 (설정값과 같아야 한다)
TAX = 0.0020                     # 주식 매도 거래세


def weekdays(start, n, skip=()):
    """start 부터 평일 n 개 (skip 에 적힌 날짜는 휴장으로 빼고 센다)."""
    skip = {str(to_date(s)) for s in skip}
    out, d = [], to_date(start)
    while len(out) < n:
        if d.weekday() < 5 and str(d) not in skip:
            out.append(d)
        d += timedelta(days=1)
    return out


class Env:
    """임시 DB + 거래일이 데이터로 정해진 달력. 모든 테스트가 같은 방식으로 세계를 만든다."""

    def __init__(self, days, cfg=None, kospi=None):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "advisor.db"
        self.store = Store(self.path)
        self.cfg = copy.deepcopy(cfg or load_config())
        self.days = list(days)
        self.store.put_market([{"series": "KOSPI", "date": str(d),
                                "value": float((kospi or {}).get(str(d), 2500.0))}
                               for d in self.days])
        self.store.commit()
        self.cal = TradingCalendar(self.cfg, self.store)

    def price(self, code, day, open_, close=None, volume=100.0):
        close = open_ if close is None else close
        self.store.put_prices([{"code": code, "date": str(day), "open": open_,
                                "high": max(open_, close), "low": min(open_, close), "close": close,
                                "volume": volume, "value": 0.0, "adj_close": close}])

    def flat(self, code, value=100.0, days=None, volume=100.0):
        for d in (days or self.days):
            self.price(code, d, value, value, volume)

    def universe(self, rows, as_of=None):
        self.store.put_universe(str(as_of or self.days[0]), rows)

    def close(self):
        self.store.close()
        self.dir.cleanup()

    def nav(self, pid, day):
        row = self.store.conn.execute("SELECT nav FROM nav WHERE portfolio_id=? AND date=?",
                                      (pid, str(day))).fetchone()
        return None if row is None else float(row[0])

    def holdings(self, pid, day):
        return {r["asset"]: float(r["weight"]) for r in self.store.conn.execute(
            "SELECT asset, weight FROM holding WHERE portfolio_id=? AND date=?", (pid, str(day)))}

    def trades(self, pid, day=None):
        sql = "SELECT * FROM trade WHERE portfolio_id=?"
        params = [pid]
        if day is not None:
            sql += " AND date=?"
            params.append(str(day))
        return self.store.conn.execute(sql + " ORDER BY asset", params).fetchall()


# ---------------------------------------------------------------- 포트폴리오 회계

class PortfolioAccountingTest(unittest.TestCase):
    PID = "t_pf"

    def setUp(self):
        self.env = Env(weekdays("2026-06-01", 6))
        self.d = self.env.days
        self.env.universe([{"code": "A0001", "name": "가상주식", "kind": "stock", "sector": "반도체"},
                           {"code": CORE, "name": "KODEX200", "kind": "etf", "sector": None},
                           {"code": CASH, "name": "현금ETF", "kind": "cash_etf", "sector": None}])
        self.env.store.commit()

    def tearDown(self):
        self.env.close()

    def settle(self, day):
        return portfolio.settle(self.env.store, self.env.cfg, self.env.cal, day)

    def test_single_asset_nav_is_hand_computable(self):
        """한 자산·한 번 체결: NAV = (1 − 비용) × 종가/시가. 비용은 체결 금액 × (슬리피지+수수료)."""
        self.env.price("A0001", self.d[0], 100.0, 110.0)
        portfolio.book_trades(self.env.store, self.env.cfg, self.PID, self.d[0], {"A0001": 1.0}, 7)
        rows = self.env.trades(self.PID, self.d[0])
        self.assertEqual([(r["asset"], r["side"], r["status"], r["src_run_id"]) for r in rows],
                         [("A0001", "buy", "pending", 7)])
        self.assertAlmostEqual(rows[0]["weight_delta"], 1.0)

        self.settle(self.d[0])
        expected_cost = 1.0 * SLIP_FEE                      # 매수라 거래세는 없다
        self.assertAlmostEqual(self.env.nav(self.PID, self.d[0]), (1.0 - expected_cost) * 1.1, places=12)
        self.assertEqual(self.env.holdings(self.PID, self.d[0]), {"A0001": 1.0})
        filled = self.env.trades(self.PID, self.d[0])[0]
        self.assertEqual((filled["status"], filled["price"]), ("filled", 100.0))
        self.assertAlmostEqual(filled["cost"], expected_cost)
        nav_row = self.env.store.conn.execute(
            "SELECT turnover, cost FROM nav WHERE portfolio_id=? AND date=?",
            (self.PID, str(self.d[0]))).fetchone()
        self.assertAlmostEqual(nav_row["turnover"], 1.0)
        self.assertAlmostEqual(nav_row["cost"], expected_cost)

    def test_two_leg_drift_without_trades(self):
        """거래가 없는 날은 전일 종가 → 시가 → 종가가 이어져 결국 종가 대 종가와 같아야 한다."""
        self.env.price("A0001", self.d[0], 100.0, 100.0)
        self.env.price("A0001", self.d[1], 120.0, 110.0)     # 시가로 튀었다가 종가에 되돌아온 날
        portfolio.book_trades(self.env.store, self.env.cfg, self.PID, self.d[0], {"A0001": 1.0}, 1)
        self.settle(self.d[1])
        first = self.env.nav(self.PID, self.d[0])
        self.assertAlmostEqual(self.env.nav(self.PID, self.d[1]), first * (110.0 / 100.0), places=12)

    def _two_asset_switch(self, risky, kind_is_stock):
        """1일차 risky 100% → 2일차 risky 50% / 현금 50% 로 갈아탄다. 2일차 시가는 +20% 떠 있다."""
        self.env.price(risky, self.d[0], 100.0, 100.0)
        self.env.price(CASH, self.d[0], 100.0, 100.0)
        self.env.price(risky, self.d[1], 120.0, 132.0)
        self.env.price(CASH, self.d[1], 100.0, 101.0)
        portfolio.book_trades(self.env.store, self.env.cfg, self.PID, self.d[0], {risky: 1.0}, 1)
        self.settle(self.d[0])
        portfolio.book_trades(self.env.store, self.env.cfg, self.PID, self.d[1],
                              {risky: 0.5, CASH: 0.5}, 2)
        self.settle(self.d[1])

        nav1 = (1.0 - SLIP_FEE) * 1.0                        # 1일차: 100 에 사서 100 에 끝
        sell_rate = SLIP_FEE + (TAX if kind_is_stock else 0.0)
        cost2 = 0.5 * sell_rate + 0.5 * SLIP_FEE
        nav_open = nav1 * 1.2                                # 전일 종가 100 → 시가 120 (비중 100%)
        expected = nav_open * (1.0 - cost2) * (1.0 + 0.5 * 0.1 + 0.5 * 0.01)
        self.assertAlmostEqual(self.env.nav(self.PID, self.d[1]), expected, places=12)
        got = self.env.holdings(self.PID, self.d[1])
        self.assertAlmostEqual(got[risky], 0.5 * 1.1 / 1.055, places=12)
        self.assertAlmostEqual(got[CASH], 0.5 * 1.01 / 1.055, places=12)
        return cost2

    def test_stock_sell_pays_tax_and_etf_does_not(self):
        etf_cost = self._two_asset_switch(CORE, kind_is_stock=False)
        self.tearDown()
        self.setUp()
        stock_cost = self._two_asset_switch("A0001", kind_is_stock=True)
        self.assertAlmostEqual(stock_cost - etf_cost, 0.5 * TAX, places=12,
                               msg="거래세는 주식 매도에만 붙는다 (ETF 면제)")

    def test_rebalance_band_skips_small_moves(self):
        self.env.flat(CORE, 100.0)
        self.env.flat(CASH, 100.0)
        portfolio.book_trades(self.env.store, self.env.cfg, self.PID, self.d[0],
                              {CORE: 0.5, CASH: 0.5}, 1)
        self.settle(self.d[0])
        deltas = portfolio.book_trades(self.env.store, self.env.cfg, self.PID, self.d[1],
                                       {CORE: 0.52, CASH: 0.48}, 2)
        self.assertEqual(deltas, {}, "5%포인트 미만 차이는 거래하지 않는다")
        self.assertEqual(self.env.trades(self.PID, self.d[1]), [])

        big = portfolio.book_trades(self.env.store, self.env.cfg, self.PID, self.d[1],
                                    {CORE: 0.4, CASH: 0.6}, 2)
        self.assertAlmostEqual(big[CORE], -0.1)
        self.assertAlmostEqual(big[CASH], 0.1)

    def test_full_exit_leaves_nothing_behind_even_after_a_gap_up(self):
        """전량 매도는 시가가 뛰어도 0 으로 끝난다 (설계 6.2의 '전량 제외').

        예약은 전일 종가 비중에서 잰 차이인데 체결은 다음 시가라, 그 사이 표류만큼 변화량이
        모자란다. 잔량이 남으면 다음 날 '보유'로 읽혀 자리를 계속 차지하고(exit_rank), 목표와의
        차이가 리밸런싱 기준에 못 미쳐 제 비중으로 돌아오지도 못한다 — 먼지 하나가 포트폴리오를
        조용히 핵심 ETF 쪽으로 끌고 간다.
        """
        self.env.price("A0001", self.d[0], 100.0, 100.0)
        self.env.price(CASH, self.d[0], 100.0, 100.0)
        self.env.price("A0001", self.d[1], 110.0, 110.0)     # 시가가 +10% 떠서 열렸다
        self.env.price(CASH, self.d[1], 100.0, 100.0)
        self.env.price("A0001", self.d[2], 110.0, 110.0)
        self.env.price(CASH, self.d[2], 100.0, 100.0)
        portfolio.book_trades(self.env.store, self.env.cfg, self.PID, self.d[0],
                              {"A0001": 0.5, CASH: 0.5}, 1)
        self.settle(self.d[0])
        # 예약은 전일 종가 비중(0.5)에서 잰다. 시가에서는 표류로 0.5238 이 돼 있어, 변화량
        # −0.5 를 그대로 빼면 2.38%p 가 먼지로 남는다.
        portfolio.book_trades(self.env.store, self.env.cfg, self.PID, self.d[1], {CASH: 1.0}, 2)
        self.settle(self.d[1])

        self.assertEqual(self.env.holdings(self.PID, self.d[1]), {CASH: 1.0},
                         "판 종목이 먼지로 남으면 안 된다")
        sold = next(r for r in self.env.trades(self.PID, self.d[1]) if r["asset"] == "A0001")
        self.assertEqual(sold["status"], "filled")
        self.assertAlmostEqual(sold["weight_delta"], -0.5 * 1.1 / 1.05, places=12,
                               msg="표류 뒤 비중 전부를 판다 (예약 당시의 −0.5 가 아니다)")
        # 0 에서 다시 들어가는 것은 **신규 편입**이라 리밸런싱 기준(5%p)과 무관하게 체결된다.
        # 먼지 2.38% 가 남아 있었다면 목표 3% 와의 차이가 기준에 걸려 거래되지 않았을 자리다.
        deltas = portfolio.book_trades(self.env.store, self.env.cfg, self.PID, self.d[2],
                                       {"A0001": 0.03, CASH: 0.97}, 3)
        self.assertAlmostEqual(deltas["A0001"], 0.03)

    def test_missing_bar_skips_the_trade_and_renormalises(self):
        """일봉이 없는 자산은 체결하지 못한다 → skipped, 비중은 그대로, 나머지를 비례로 되맞춘다."""
        self.env.flat(CORE, 100.0)
        self.env.price(CASH, self.d[0], 100.0, 100.0)        # 2일차 현금 ETF 일봉은 없다
        portfolio.book_trades(self.env.store, self.env.cfg, self.PID, self.d[0], {CORE: 1.0}, 1)
        self.settle(self.d[0])
        portfolio.book_trades(self.env.store, self.env.cfg, self.PID, self.d[1],
                              {CORE: 0.5, CASH: 0.5}, 2)
        report = self.settle(self.d[1])
        rows = {r["asset"]: r["status"] for r in self.env.trades(self.PID, self.d[1])}
        self.assertEqual(rows, {CORE: "filled", CASH: "skipped"})
        self.assertEqual(self.env.holdings(self.PID, self.d[1]), {CORE: 1.0},
                         "체결하지 못한 몫은 이미 들고 있는 자산으로 되맞춘다")
        notes = [n for rep in report for n in rep["notes"]]
        self.assertTrue(any("skipped" in n for n in notes), notes)
        # 매도 비용은 실제로 냈다 (팔긴 팔았다)
        self.assertAlmostEqual(self.env.nav(self.PID, self.d[1]),
                               self.env.nav(self.PID, self.d[0]) * (1.0 - 0.5 * SLIP_FEE), places=12)

    def test_carry_last_price_when_a_holding_has_no_bar(self):
        self.env.price(CORE, self.d[0], 100.0, 100.0)
        self.env.price(CORE, self.d[2], 100.0, 120.0)        # 2일차 일봉이 통째로 없다
        self.env.price(CASH, self.d[1], 100.0, 100.0)        # 그날 시세가 아예 없지는 않게
        portfolio.book_trades(self.env.store, self.env.cfg, self.PID, self.d[0], {CORE: 1.0}, 1)
        report = self.settle(self.d[2])
        self.assertAlmostEqual(self.env.nav(self.PID, self.d[1]), self.env.nav(self.PID, self.d[0]),
                               places=12, msg="마지막 가격을 이어 쓰면 그날 수익은 0")
        notes = [n for rep in report for n in rep["notes"]]
        self.assertTrue(any("마지막 가격" in n for n in notes), notes)
        self.assertAlmostEqual(self.env.nav(self.PID, self.d[2]),
                               self.env.nav(self.PID, self.d[1]) * 1.2, places=12)

    def test_rebooking_replaces_pending_rows_only(self):
        self.env.flat(CORE, 100.0)
        self.env.flat(CASH, 100.0)
        portfolio.book_trades(self.env.store, self.env.cfg, self.PID, self.d[0], {CORE: 1.0}, 1)
        portfolio.book_trades(self.env.store, self.env.cfg, self.PID, self.d[0],
                              {CORE: 0.6, CASH: 0.4}, 2)
        rows = self.env.trades(self.PID, self.d[0])
        self.assertEqual(len(rows), 2, "같은 날 다시 예약하면 먼저 예약한 pending 은 지운다")
        self.assertEqual({r["src_run_id"] for r in rows}, {2})
        self.settle(self.d[0])
        self.assertEqual({r["status"] for r in self.env.trades(self.PID, self.d[0])}, {"filled"})

    def test_summary_numbers(self):
        self.env.price(CORE, self.d[0], 100.0, 100.0)
        self.env.price(CORE, self.d[1], 100.0, 90.0)
        self.env.price(CORE, self.d[2], 90.0, 99.0)
        portfolio.book_trades(self.env.store, self.env.cfg, self.PID, self.d[0], {CORE: 1.0}, 1)
        self.settle(self.d[2])
        got = portfolio.summary(self.env.store, self.PID, self.env.cfg)
        nav1 = 1.0 - SLIP_FEE
        self.assertEqual(got["n_days"], 3)
        self.assertAlmostEqual(got["nav"], nav1 * 0.99, places=12)
        self.assertAlmostEqual(got["total_return"], nav1 * 0.99 - 1.0, places=12)
        self.assertAlmostEqual(got["max_drawdown"], nav1 * 0.9 - 1.0, places=12)
        self.assertAlmostEqual(got["turnover"], 1.0, places=12)
        self.assertAlmostEqual(got["cost"], SLIP_FEE, places=12)
        self.assertEqual(got["mode"], "live")

    def test_settle_is_not_redone(self):
        self.env.flat(CORE, 100.0)
        portfolio.book_trades(self.env.store, self.env.cfg, self.PID, self.d[0], {CORE: 1.0}, 1)
        self.settle(self.d[0])
        nav_before = self.env.nav(self.PID, self.d[0])
        report = self.settle(self.d[0])
        self.assertEqual(report[0]["days"], [], "이미 정산한 날은 다시 계산하지 않는다")
        self.assertEqual(self.env.nav(self.PID, self.d[0]), nav_before)

    def test_replay_and_live_do_not_mix(self):
        self.env.flat(CORE, 100.0, volume=100.0)
        live = portfolio.portfolio_id_for("sys_final_v0", "live")
        replay = portfolio.portfolio_id_for("sys_final_v0", "replay")
        self.assertEqual(replay, "sys_final_v0@replay")
        portfolio.book_trades(self.env.store, self.env.cfg, live, self.d[0], {CORE: 1.0}, 1)
        portfolio.book_trades(self.env.store, self.env.cfg, replay, self.d[0], {CORE: 1.0}, 2)
        portfolio.settle(self.env.store, self.env.cfg, self.env.cal, self.d[0], mode="live")
        self.assertIsNotNone(self.env.nav(live, self.d[0]))
        self.assertIsNone(self.env.nav(replay, self.d[0]), "live 정산이 replay 기록을 건드리지 않는다")
        self.assertEqual(portfolio.portfolio_ids(self.env.store, "replay"), [replay])
        portfolio.settle(self.env.store, self.env.cfg, self.env.cal, self.d[0], mode="replay")
        self.assertIsNotNone(self.env.nav(replay, self.d[0]))


class KnownBarDateTest(unittest.TestCase):
    """시점 규칙의 경계는 한 곳(설정의 sources.daily_bar_known_at)에서만 나온다."""

    def setUp(self):
        self.env = Env(weekdays("2026-06-01", 5))
        self.d = self.env.days

    def tearDown(self):
        self.env.close()

    def known(self, as_of, cfg=None):
        return portfolio.known_bar_date(self.env.cal, as_of, cfg or self.env.cfg)

    def test_boundary_and_holidays(self):
        self.assertEqual(self.known(f"{self.d[2]}T07:40:00"), self.d[1], "아침에는 전 거래일까지")
        self.assertEqual(self.known(f"{self.d[2]}T18:30:00"), self.d[2], "18:00 이 지나면 당일까지")
        self.assertEqual(self.known(str(self.d[2])), self.d[1], "날짜만 주면 00:00 으로 본다")
        saturday = self.d[4] + timedelta(days=1)
        self.assertEqual(saturday.weekday(), 5)
        self.assertEqual(self.known(f"{saturday}T20:00:00"), self.d[4], "휴장일이면 직전 거래일")

    def test_boundary_follows_the_config(self):
        late = copy.deepcopy(self.env.cfg)
        late.setdefault("sources", {})["daily_bar_known_at"] = "20:00"
        self.assertEqual(self.known(f"{self.d[2]}T18:30:00", late), self.d[1],
                         "수집이 늦게 확정된다고 적히면 채점·기준선도 같이 늦게 본다")


# ---------------------------------------------------------------- 체결할 판단 고르기

class DecisionForFillTest(unittest.TestCase):
    def setUp(self):
        self.env = Env(weekdays("2026-06-01", 5))
        self.d = self.env.days

    def tearDown(self):
        self.env.close()

    def put_run(self, stage, day, variants, mode="live"):
        rid = self.env.store.start_run(stage, str(day), mode=mode)
        for variant, weights in variants.items():
            self.env.store.put_decision(rid, variant, 0.0, 0.5)
            self.env.store.put_target_weights(rid, variant, [
                {"asset": a, "role": "core" if a == CORE else "cash", "weight": w}
                for a, w in weights.items()])
        self.env.store.commit()
        return rid

    def pick(self, base, day, mode="live"):
        return portfolio.decision_for_fill(self.env.store, base, day, mode=mode, cal=self.env.cal)

    def test_final_prefers_llm_and_falls_back_to_v0(self):
        rid = self.put_run("final", self.d[1], {"v0": {CORE: 1.0}, "llm": {CORE: 0.4, CASH: 0.6}})
        got = self.pick("sys_final_llm", self.d[1])
        self.assertEqual((got["run_id"], got["variant"], got["promoted"]), (rid, "llm", False))
        self.assertAlmostEqual(got["weights"][CASH], 0.6)
        self.assertFalse(got["llm_abstained"])
        v0 = self.pick("sys_final_v0", self.d[1])
        self.assertEqual(v0["variant"], "v0")
        self.assertEqual(v0["weights"], {CORE: 1.0})

    def test_llm_abstention_falls_back_to_v0(self):
        self.put_run("final", self.d[1], {"v0": {CORE: 1.0}})
        got = self.pick("sys_final_llm", self.d[1])
        self.assertEqual(got["variant"], "v0")
        self.assertTrue(got["llm_abstained"], "llm 판단이 없으면 v0 로 체결한다 (= LLM 기권)")

    def test_promoted_prelim_when_final_is_missing(self):
        rid = self.put_run("prelim", self.d[0], {"v0": {CORE: 0.3, CASH: 0.7}})
        got = self.pick("sys_final_v0", self.d[1])
        self.assertEqual((got["run_id"], got["stage"], got["promoted"]), (rid, "prelim", True))
        self.assertAlmostEqual(got["weights"][CASH], 0.7)

        # 스케줄러가 대체 실행(fallback_used=1)을 따로 만들어 둔 경우에도 같은 함수가 그것을 찾는다
        rid2 = self.put_run("final", self.d[1], {"v0": {CORE: 1.0}})
        self.env.store.finish_run(rid2, status="OK", fallback_used=1)
        self.env.store.commit()
        got = self.pick("sys_final_v0", self.d[1])
        self.assertEqual((got["run_id"], got["promoted"]), (rid2, False))

    def test_final_without_weights_is_treated_as_missing(self):
        rid = self.env.store.start_run("final", str(self.d[1]))      # 목표 비중을 남기지 못하고 죽은 실행
        self.env.store.finish_run(rid, status="ERROR")
        prelim = self.put_run("prelim", self.d[0], {"v0": {CORE: 1.0}})
        got = self.pick("sys_final_v0", self.d[1])
        self.assertEqual((got["run_id"], got["promoted"]), (prelim, True))

    def test_prelim_portfolio_uses_the_previous_evening(self):
        rid = self.put_run("prelim", self.d[0], {"llm": {CORE: 0.2, CASH: 0.8}})
        got = self.pick("sys_prelim_llm", self.d[1])
        self.assertEqual((got["run_id"], got["stage"], got["variant"]), (rid, "prelim", "llm"))
        self.assertIsNone(self.pick("sys_prelim_llm", self.d[0]),
                          "전 거래일 예비 판단이 없으면 예약하지 않는다")

    def test_live_and_replay_are_isolated(self):
        live = self.put_run("final", self.d[1], {"v0": {CORE: 1.0}}, mode="live")
        rep = self.put_run("final", self.d[1], {"v0": {CORE: 0.1, CASH: 0.9}}, mode="replay")
        self.assertEqual(self.pick("sys_final_v0", self.d[1], mode="live")["run_id"], live)
        self.assertEqual(self.pick("sys_final_v0", self.d[1], mode="replay")["run_id"], rep)
        self.assertAlmostEqual(self.pick("sys_final_v0", self.d[1], mode="replay")["weights"][CASH], 0.9)

    def test_latest_run_of_the_day_wins(self):
        self.put_run("final", self.d[1], {"v0": {CORE: 1.0}})
        second = self.put_run("final", self.d[1], {"v0": {CORE: 0.5, CASH: 0.5}})
        self.assertEqual(self.pick("sys_final_v0", self.d[1])["run_id"], second)

    def test_book_system_portfolios(self):
        self.env.flat(CORE, 100.0)
        self.env.flat(CASH, 100.0)
        self.put_run("final", self.d[1], {"v0": {CORE: 1.0}, "llm": {CORE: 0.5, CASH: 0.5}})
        self.put_run("prelim", self.d[0], {"v0": {CORE: 0.7, CASH: 0.3}})
        out = portfolio.book_system_portfolios(self.env.store, self.env.cfg, self.env.cal, self.d[1])
        booked = {r["portfolio_id"]: r for r in out}
        self.assertEqual(set(booked), {"sys_final_llm", "sys_final_v0", "sys_prelim_llm"})
        self.assertTrue(all(r["booked"] for r in out))
        self.assertAlmostEqual(booked["sys_final_llm"]["deltas"][CASH], 0.5)
        self.assertAlmostEqual(booked["sys_prelim_llm"]["deltas"][CASH], 0.3)
        self.assertTrue(booked["sys_prelim_llm"]["variant"] == "v0")


# ---------------------------------------------------------------- 기준선

class BaselineTest(unittest.TestCase):
    def setUp(self):
        self.days = weekdays("2026-06-22", 12)
        kospi = {str(d): 100.0 for d in self.days}
        kospi[str(self.days[4])] = 130.0                 # 5일차에만 크게 튄 지수
        cfg = copy.deepcopy(load_config())
        cfg["factors"]["mkt_trend"]["params"]["ma_days"] = 3
        self.env = Env(self.days, cfg=cfg, kospi=kospi)
        self.d = self.days
        for code in (CORE, CASH):
            self.env.flat(code, 100.0)
        self.env.store.commit()

    def tearDown(self):
        self.env.close()

    def target(self, baseline_id, fill_date, as_of):
        return baselines.baseline_target(self.env.store, self.env.cfg, self.env.cal,
                                         baseline_id, fill_date, as_of,
                                         portfolio_id=baseline_id)[0]

    def test_kodex200_is_buy_and_hold(self):
        self.assertEqual(self.target("bl_kodex200", self.d[0], f"{self.d[0]}T07:40:00"), {CORE: 1.0})
        out = baselines.book_baselines(self.env.store, self.env.cfg, self.env.cal,
                                       self.d[0], f"{self.d[0]}T07:40:00")
        pid = "bl_kodex200"
        self.assertTrue(next(r for r in out if r["portfolio_id"] == pid)["booked"])
        portfolio.settle(self.env.store, self.env.cfg, self.env.cal, self.d[0])
        baselines.book_baselines(self.env.store, self.env.cfg, self.env.cal,
                                 self.d[1], f"{self.d[1]}T07:40:00")
        self.assertEqual(self.env.trades(pid, self.d[1]), [],
                         "이미 100% 들고 있으면 더 거래하지 않는다 (매수 후 보유)")

    def test_sma_uses_only_data_known_at_decision_time(self):
        """5일차 07:40 판단은 5일차 지수(130)를 알 수 없다 → 현금. 같은 날 18:30 에는 알 수 있다 → 주식."""
        morning = self.target("bl_sma10m", self.d[4], f"{self.d[4]}T07:40:00")
        self.assertEqual(morning, {CASH: 1.0}, "판단 시각에 확정된 일봉은 전 거래일까지다")
        evening = self.target("bl_sma10m", self.d[5], f"{self.d[4]}T18:30:00")
        self.assertEqual(evening, {CORE: 1.0}, "18:00 이 지나면 그날 지수를 쓴다")
        # 날짜만 준 as_of 는 00:00 으로 본다 → 아침 판단과 같은 자료만 본다 (늦게 아는 쪽으로 틀린다)
        self.assertEqual(self.target("bl_sma10m", self.d[4], str(self.d[4])),
                         self.target("bl_sma10m", self.d[4], f"{self.d[4]}T07:40:00"))

    def test_sma_does_not_invent_a_moving_average(self):
        target, note = baselines.baseline_target(self.env.store, self.env.cfg, self.env.cal,
                                                 "bl_sma10m", self.d[1], f"{self.d[1]}T07:40:00",
                                                 portfolio_id="bl_sma10m")
        self.assertIsNone(target, "과거가 3일치도 없으면 예약하지 않는다")
        self.assertIn("이동평균", note)

    def test_sma_switch_is_a_full_exit_and_entry(self):
        baselines.book_baselines(self.env.store, self.env.cfg, self.env.cal,
                                 self.d[4], f"{self.d[4]}T07:40:00")
        portfolio.settle(self.env.store, self.env.cfg, self.env.cal, self.d[4])
        self.assertEqual(self.env.holdings("bl_sma10m", self.d[4]), {CASH: 1.0})
        baselines.book_baselines(self.env.store, self.env.cfg, self.env.cal,
                                 self.d[5], f"{self.d[5]}T07:40:00")
        portfolio.settle(self.env.store, self.env.cfg, self.env.cal, self.d[5])
        self.assertEqual(self.env.holdings("bl_sma10m", self.d[5]), {CORE: 1.0},
                         "규칙이 바뀌면 전량 교체한다 (리밸런싱 기준과 무관)")

    def test_6040_rebalances_on_the_first_trading_day_of_a_month(self):
        june = [d for d in self.days if d.month == 6]
        july = [d for d in self.days if d.month == 7]
        self.assertTrue(july, "7월 거래일이 있어야 하는 표본이다")
        first_july = july[0]
        self.assertTrue(baselines.is_month_first_trading_day(self.env.cal, first_july))
        self.assertFalse(baselines.is_month_first_trading_day(self.env.cal, june[1]))

        # 첫 예약은 달의 첫 거래일이 아니어도 한다 (그래야 포트폴리오가 시작된다)
        baselines.book_baselines(self.env.store, self.env.cfg, self.env.cal,
                                 june[1], f"{june[1]}T07:40:00")
        self.assertEqual({r["asset"] for r in self.env.trades("bl_6040", june[1])}, {CORE, CASH})
        portfolio.settle(self.env.store, self.env.cfg, self.env.cal, june[1])
        held = self.env.holdings("bl_6040", june[1])
        self.assertAlmostEqual(held[CORE], 0.6, places=12)

        out = baselines.book_baselines(self.env.store, self.env.cfg, self.env.cal,
                                       june[2], f"{june[2]}T07:40:00")
        note = next(r for r in out if r["portfolio_id"] == "bl_6040")
        self.assertFalse(note["booked"])
        self.assertIn("월 첫 거래일", note["note"])

    def test_6040_monthly_rebalance_ignores_the_band(self):
        june = [d for d in self.days if d.month == 6]
        july = [d for d in self.days if d.month == 7]
        self.env.price(CORE, june[-1], 103.0, 103.0)     # 마지막 날 주식만 3% 오른 상태로 정산된다
        self.env.store.commit()
        baselines.book_baselines(self.env.store, self.env.cfg, self.env.cal,
                                 june[-2], f"{june[-2]}T07:40:00")
        portfolio.settle(self.env.store, self.env.cfg, self.env.cal, june[-1])
        held = self.env.holdings("bl_6040", june[-1])
        self.assertLess(abs(held[CORE] - 0.6), 0.05, "표류가 리밸런싱 기준 안에 있다")
        self.assertGreater(held[CORE], 0.6)
        baselines.book_baselines(self.env.store, self.env.cfg, self.env.cal,
                                 july[0], f"{july[0]}T07:40:00")
        rows = self.env.trades("bl_6040", july[0])
        self.assertTrue(rows, "표류가 5%포인트 미만이어도 월 1회 리밸런싱은 한다")
        self.assertLess(abs(rows[0]["weight_delta"]), 0.05)


# ---------------------------------------------------------------- 채점 (사후 결과)

class OutcomeTest(unittest.TestCase):
    """휴장일이 하나 끼어 있는 15거래일 위에서 채점 산수를 확인한다."""

    def setUp(self):
        raw = weekdays("2026-06-01", 16)
        self.holiday = raw[3]                            # 이 평일은 휴장으로 뺀다
        self.days = [d for d in raw if d != self.holiday]
        self.env = Env(self.days)
        self.env.universe([{"code": "A0001", "name": "가상주식", "kind": "stock", "sector": "반도체"},
                           {"code": "A0002", "name": "가상주식2", "kind": "stock", "sector": None}])
        for code in (CORE, CASH, SEC_ETF):
            self.env.flat(code, 100.0)
        for i, d in enumerate(self.days):
            self.env.price("A0001", d, 100.0 + i, 100.0 + i)
        self.env.store.commit()

    def tearDown(self):
        self.env.close()

    def make_run(self, stage, day, entities, mode="live"):
        rid = self.env.store.start_run(stage, str(day), mode=mode)
        self.env.store.put_factor_values(rid, entities)
        self.env.store.commit()
        return rid

    def outcomes(self, run_id=None):
        sql = "SELECT * FROM outcome"
        params = []
        if run_id is not None:
            sql += " WHERE run_id=?"
            params.append(run_id)
        return self.env.store.conn.execute(sql + " ORDER BY entity, factor_id, horizon", params).fetchall()

    def test_start_and_end_skip_the_holiday(self):
        rid = self.make_run("final", self.days[0], [
            {"entity": "A0001", "factor_id": "stk_flow", "raw_value": 1.0, "score": 0.5, "missing": 0}])
        scoring.score_outcomes(self.env.store, self.env.cfg, self.env.cal,
                               f"{self.days[14]}T18:30:00")
        rows = {r["horizon"]: r for r in self.outcomes(rid)}
        self.assertEqual(set(rows), {5, 10, 20} & set(rows), "주 기간(10) + 보조 기간(5·20)")
        self.assertEqual(sorted(rows), [5, 10])          # 20거래일 뒤는 아직 오지 않았다
        five = rows[5]
        self.assertEqual(five["start_date"], str(self.days[0]))
        self.assertEqual(five["end_date"], str(self.days[5]))
        self.assertGreater((self.days[5] - self.days[0]).days, 5, "휴장일이 끼어 달력일로는 더 멀다")
        self.assertEqual(five["eval_status"], "ok")
        # 종목 계층: 종목 수익 − KODEX 200 수익 (KODEX 는 내내 100 이라 0)
        self.assertAlmostEqual(five["asset_ret"], 105.0 / 100.0 - 1.0)
        self.assertAlmostEqual(five["bench_ret"], 0.0)
        self.assertAlmostEqual(five["excess_ret"], 0.05)

    def test_prelim_starts_on_the_next_trading_day(self):
        rid = self.make_run("prelim", self.days[0], [
            {"entity": "A0001", "factor_id": "stk_flow", "raw_value": 1.0, "score": 0.5, "missing": 0}])
        scoring.score_outcomes(self.env.store, self.env.cfg, self.env.cal, f"{self.days[14]}T18:30:00")
        row = [r for r in self.outcomes(rid) if r["horizon"] == 5][0]
        self.assertEqual(row["start_date"], str(self.days[1]), "예비 판단은 다음 날 시가부터다")
        self.assertEqual(row["end_date"], str(self.days[6]))

    def test_market_and_sector_benchmarks(self):
        for i, d in enumerate(self.days):                 # 핵심 ETF 가 매일 1 씩 오르게 바꾼다
            self.env.price(CORE, d, 100.0 + i, 100.0 + i)
        self.env.store.commit()
        rid = self.make_run("final", self.days[0], [
            {"entity": "MARKET", "factor_id": "mkt_vol", "raw_value": 0.1, "score": -0.5, "missing": 0},
            {"entity": "반도체", "factor_id": "sec_flow", "raw_value": 1.0, "score": 0.5, "missing": 0},
            {"entity": "A0001", "factor_id": "stk_flow", "raw_value": 1.0, "score": 0.5, "missing": 0}])
        scoring.score_outcomes(self.env.store, self.env.cfg, self.env.cal, f"{self.days[14]}T18:30:00")
        rows = {(r["entity"], r["factor_id"], r["horizon"]): r for r in self.outcomes(rid)}
        market = rows[("MARKET", "mkt_vol", 5)]
        self.assertAlmostEqual(market["asset_ret"], 0.05, msg="시장 계층은 KODEX 200 수익")
        self.assertAlmostEqual(market["bench_ret"], 0.0, msg="비교 대상은 현금 대용 ETF")
        sector = rows[("반도체", "sec_flow", 5)]
        self.assertAlmostEqual(sector["asset_ret"], 0.0, msg="섹터 ETF 수익")
        self.assertAlmostEqual(sector["excess_ret"], -0.05, msg="섹터는 KODEX 200 대비")
        stock = rows[("A0001", "stk_flow", 5)]
        self.assertAlmostEqual(stock["excess_ret"], 0.0, msg="종목 − KODEX 200")
        aux = rows[("A0001", "stk_flow" + scoring.VS_SECTOR, 5)]
        self.assertAlmostEqual(aux["excess_ret"], 0.05, msg="보조 비교: 종목 − 섹터 ETF")
        self.assertNotIn(("A0002", "stk_flow" + scoring.VS_SECTOR, 5), rows)

    def test_unable_is_recorded_without_inventing_numbers(self):
        rid = self.make_run("final", self.days[0], [
            {"entity": "A0002", "factor_id": "stk_flow", "raw_value": 1.0, "score": 0.5, "missing": 0},
            {"entity": "없는섹터", "factor_id": "sec_flow", "raw_value": 1.0, "score": 0.1, "missing": 0}])
        scoring.score_outcomes(self.env.store, self.env.cfg, self.env.cal, f"{self.days[14]}T18:30:00")
        rows = {(r["entity"], r["horizon"]): r for r in self.outcomes(rid)}
        missing_price = rows[("A0002", 5)]
        self.assertEqual(missing_price["eval_status"], "unable")
        self.assertIsNone(missing_price["excess_ret"])
        self.assertIn("A0002", missing_price["unable_reason"])
        no_sector = rows[("없는섹터", 5)]
        self.assertEqual(no_sector["eval_status"], "unable")
        self.assertIn("섹터 ETF", no_sector["unable_reason"])

    def test_missing_scores_are_not_scored(self):
        rid = self.make_run("final", self.days[0], [
            {"entity": "A0001", "factor_id": "stk_flow", "raw_value": None, "score": 0.0, "missing": 1}])
        scoring.score_outcomes(self.env.store, self.env.cfg, self.env.cal, f"{self.days[14]}T18:30:00")
        self.assertEqual(self.outcomes(rid), [], "결측으로 적힌 점수는 예측이 아니다")

    def test_immature_windows_and_no_look_ahead(self):
        rid = self.make_run("final", self.days[0], [
            {"entity": "A0001", "factor_id": "stk_flow", "raw_value": 1.0, "score": 0.5, "missing": 0}])
        # 5거래일이 막 지난 시점: 5일 기간만 채점된다
        scoring.score_outcomes(self.env.store, self.env.cfg, self.env.cal, f"{self.days[5]}T18:30:00")
        first = {r["horizon"]: (r["excess_ret"], r["computed_at"]) for r in self.outcomes(rid)}
        self.assertEqual(sorted(first), [5])

        # 미래 일봉을 더 채워 넣어도 as_of 가 그대로면 만기가 앞당겨지지 않는다
        for i, d in enumerate(self.days):
            self.env.price("A0001", d, 500.0 + i, 500.0 + i)
        self.env.store.commit()
        scoring.score_outcomes(self.env.store, self.env.cfg, self.env.cal, f"{self.days[5]}T18:30:00")
        again = {r["horizon"]: (r["excess_ret"], r["computed_at"]) for r in self.outcomes(rid)}
        self.assertEqual(again, first, "이미 끝난 채점도, 만기 전 조합도 움직이지 않는다")

        # as_of 가 실제로 지나가면 그제서야 10일 기간이 채점된다
        scoring.score_outcomes(self.env.store, self.env.cfg, self.env.cal, f"{self.days[10]}T18:30:00")
        later = {r["horizon"] for r in self.outcomes(rid)}
        self.assertEqual(sorted(later), [5, 10])
        self.assertEqual({r["horizon"]: r["excess_ret"] for r in self.outcomes(rid)}[5], first[5][0])

    def test_composite_rows_are_scored_per_variant(self):
        rid = self.env.store.start_run("final", str(self.days[0]))
        self.env.store.put_composites(rid, "v0", [
            {"entity": "A0001", "layer": "stock", "base_score": 0.4, "adj": 0.0, "final_score": 0.4,
             "vetoed": False, "adopted_json": None, "rejected_json": None, "reason": None, "call_id": None}])
        self.env.store.put_composites(rid, "llm", [
            {"entity": "A0001", "layer": "stock", "base_score": 0.4, "adj": 0.2, "final_score": 0.6,
             "vetoed": False, "adopted_json": None, "rejected_json": None, "reason": None, "call_id": None}])
        self.env.store.commit()
        scoring.score_outcomes(self.env.store, self.env.cfg, self.env.cal, f"{self.days[14]}T18:30:00")
        rows = self.outcomes(rid)
        variants = {r["variant"] for r in rows}
        self.assertEqual(variants, {"v0", "llm"})
        self.assertEqual({r["factor_id"] for r in rows},
                         {"composite", "composite" + scoring.VS_SECTOR})
        self.assertEqual({r["horizon"] for r in rows}, {5},
                         "보조 기간(5·20) 중 만기가 온 5일만 (표본이 15거래일뿐이다)")

    def test_replay_records_are_scored_separately(self):
        live = self.make_run("final", self.days[0], [
            {"entity": "A0001", "factor_id": "stk_flow", "raw_value": 1.0, "score": 0.5, "missing": 0}])
        rep = self.make_run("final", self.days[0], [
            {"entity": "A0001", "factor_id": "stk_flow", "raw_value": 1.0, "score": 0.5, "missing": 0}],
            mode="replay")
        scoring.score_outcomes(self.env.store, self.env.cfg, self.env.cal, f"{self.days[14]}T18:30:00",
                               mode="live")
        self.assertTrue(self.outcomes(live))
        self.assertEqual(self.outcomes(rep), [], "재현 기록은 live 채점에 섞이지 않는다 (설계 7.4)")


# ---------------------------------------------------------------- 요인 지표

class FactorMetricsTest(unittest.TestCase):
    """점수 순위와 초과 수익 순위가 완전히 일치하면 IC = 1, 뒤집히면 −1 이어야 한다."""

    N_STOCKS = 6

    def setUp(self):
        self.days = weekdays("2026-06-01", 30)
        self.env = Env(self.days)
        self.codes = [f"S{i:04d}" for i in range(1, self.N_STOCKS + 1)]
        self.env.universe([{"code": c, "name": c, "kind": "stock", "sector": None} for c in self.codes])
        self.env.flat(CASH, 100.0)
        for i, d in enumerate(self.days):
            # 핵심 ETF 도 조금씩 오른다 — 초과 수익이 '종목 − KODEX 200' 이라는 것을 확인하기 위해서다
            self.env.price(CORE, d, 100.0 * (1.0 + 0.0001 * i), 100.0 * (1.0 + 0.0001 * i))
            for k, code in enumerate(self.codes):
                px = 100.0 * (1.0 + 0.001 * (k + 1) * i)      # 뒤 종목일수록 빨리 오른다
                self.env.price(code, d, px, px)
        self.env.store.commit()

    def tearDown(self):
        self.env.close()

    def make_run(self, day, scores, stage="final"):
        rid = self.env.store.start_run(stage, str(day))
        self.env.store.put_factor_values(rid, [
            {"entity": code, "factor_id": "stk_flow", "raw_value": s, "score": s, "missing": 0}
            for code, s in scores.items()])
        self.env.store.commit()
        return rid

    def metrics(self, as_of=None, **kw):
        scoring.score_outcomes(self.env.store, self.env.cfg, self.env.cal,
                               as_of or f"{self.days[-1]}T18:30:00")
        return scoring.factor_metrics(self.env.store, self.env.cfg, **kw)

    def rows_by_horizon(self, rows, factor_id="stk_flow"):
        return {r["horizon"]: r for r in rows if r["factor_id"] == factor_id}

    def test_perfect_and_reversed_rank_ic(self):
        aligned = {code: (k + 1) / 10.0 for k, code in enumerate(self.codes)}
        self.make_run(self.days[0], aligned)
        self.make_run(self.days[1], aligned)
        rows = self.rows_by_horizon(self.metrics())
        self.assertAlmostEqual(rows[5]["rank_ic_mean"], 1.0, places=12,
                               msg="점수 순위와 초과 수익 순위가 같으면 IC = 1")
        self.assertEqual(rows[5]["n_days"], 2)
        self.assertAlmostEqual(rows[5]["n_eff"], 2 / 5.0, msg="n_eff = 관측 일수 ÷ 채점 기간")
        self.assertAlmostEqual(rows[10]["n_eff"], 2 / 10.0)
        self.assertAlmostEqual(rows[5]["rank_ic_std"], 0.0, places=12)
        self.assertAlmostEqual(rows[5]["hit_rate"], 1.0, places=12)
        buckets = json.loads(rows[5]["bucket_json"])["q"]
        means = [b["mean_excess"] for b in buckets]
        self.assertEqual(means, sorted(means), "점수 구간이 높을수록 평균 초과 수익이 커야 한다")

    def test_reversed_scores_give_minus_one(self):
        reversed_scores = {code: -(k + 1) / 10.0 for k, code in enumerate(self.codes)}
        self.make_run(self.days[0], reversed_scores)
        rows = self.rows_by_horizon(self.metrics())
        self.assertAlmostEqual(rows[5]["rank_ic_mean"], -1.0, places=12)
        self.assertAlmostEqual(rows[5]["hit_rate"], 0.0, places=12)

    def test_missing_values_do_not_enter(self):
        aligned = {code: (k + 1) / 10.0 for k, code in enumerate(self.codes)}
        rid = self.make_run(self.days[0], aligned)
        self.env.store.put_factor_values(rid, [
            {"entity": self.codes[0], "factor_id": "stk_high52", "raw_value": None,
             "score": 99.0, "missing": 1}])
        self.env.store.commit()
        rows = self.metrics()
        self.assertAlmostEqual(self.rows_by_horizon(rows)[5]["rank_ic_mean"], 1.0, places=12)
        self.assertEqual([r for r in rows if r["factor_id"] == "stk_high52"], [],
                         "결측 점수만 있는 요인은 지표가 없다")

    def test_small_cross_section_is_skipped(self):
        cfg = copy.deepcopy(self.env.cfg)
        cfg["scoring"]["min_cross_section"] = self.N_STOCKS + 1
        self.make_run(self.days[0], {code: (k + 1) / 10.0 for k, code in enumerate(self.codes)})
        scoring.score_outcomes(self.env.store, self.env.cfg, self.env.cal, f"{self.days[-1]}T18:30:00")
        rows = scoring.factor_metrics(self.env.store, cfg)
        self.assertEqual(self.rows_by_horizon(rows), {},
                         "이름이 모자란 날의 단면은 순위 상관을 내지 않는다")

    def test_market_factor_path(self):
        rid = self.env.store.start_run("final", str(self.days[0]))
        self.env.store.put_factor_values(rid, [
            {"entity": "MARKET", "factor_id": "mkt_trend", "raw_value": 1.0, "score": 1.0, "missing": 0}])
        self.env.store.commit()
        rows = {r["factor_id"]: r for r in self.metrics()}
        market = rows["mkt_trend"]
        self.assertIsNone(market["rank_ic_mean"], "시장 요인은 날짜 안 단면이 없어 순위 상관이 없다")
        self.assertIsNone(market["rank_ic_std"])
        self.assertAlmostEqual(market["hit_rate"], 1.0, msg="점수 +, 실제로도 주식이 현금보다 나았다")
        sign = json.loads(market["bucket_json"])["sign"]
        self.assertEqual(sign["pos"]["n"], 1)
        self.assertGreater(sign["pos"]["mean_excess"], 0)
        self.assertEqual(sign["neg"]["n"], 0)

    def test_metrics_are_persisted_and_split_by_stage(self):
        aligned = {code: (k + 1) / 10.0 for k, code in enumerate(self.codes)}
        self.make_run(self.days[0], aligned, stage="final")
        self.make_run(self.days[0], aligned, stage="prelim")
        rows = self.metrics()
        stages = {r["stage"] for r in rows}
        self.assertEqual(stages, {"final", "prelim"}, "단계별로 따로 낸다 (결정 11)")
        stored = self.env.store.conn.execute(
            "SELECT DISTINCT stage, factor_id, horizon FROM factor_metric").fetchall()
        self.assertTrue(stored)
        only_final = scoring.factor_metrics(self.env.store, self.env.cfg, stage="final", persist=False)
        self.assertEqual({r["stage"] for r in only_final}, {"final"})

    def test_llm_contribution_is_the_ic_difference(self):
        rid = self.env.store.start_run("final", str(self.days[0]))
        rows_v0, rows_llm = [], []
        for k, code in enumerate(self.codes):
            base = (k + 1) / 10.0
            rows_v0.append({"entity": code, "layer": "stock", "base_score": base, "adj": 0.0,
                            "final_score": base, "vetoed": False, "adopted_json": None,
                            "rejected_json": None, "reason": None, "call_id": None})
            # 조정 후 점수는 순위를 완전히 뒤집는다 → 기여는 음수여야 한다
            rows_llm.append({"entity": code, "layer": "stock", "base_score": base, "adj": -0.2,
                             "final_score": -base, "vetoed": False, "adopted_json": None,
                             "rejected_json": None, "reason": None, "call_id": None})
        self.env.store.put_composites(rid, "v0", rows_v0)
        self.env.store.put_composites(rid, "llm", rows_llm)
        self.env.store.commit()
        rows = self.metrics()
        contrib = scoring.llm_adjust_contribution(rows)
        self.assertTrue(contrib)
        for c in contrib:
            self.assertAlmostEqual(c["rank_ic_base"], 1.0, places=12)
            self.assertAlmostEqual(c["rank_ic_adjusted"], -1.0, places=12)
            self.assertAlmostEqual(c["contribution"], -2.0, places=12)

    def test_risk_flag_metrics(self):
        aligned = {code: (k + 1) / 10.0 for k, code in enumerate(self.codes)}
        rid = self.make_run(self.days[0], aligned)
        self.env.store.put_risk_flags(rid, [(self.codes[0], "spike", "code", "5일 +32%")])
        self.env.store.commit()
        rows = scoring.risk_flag_metrics(self.env.store, self.env.cfg, self.env.cal,
                                         f"{self.days[-1]}T18:30:00")
        by_id = {r["factor_id"]: r for r in rows}
        self.assertEqual(set(by_id), {"risk_flag|any", "risk_flag|spike"})
        groups = json.loads(by_id["risk_flag|spike"]["bucket_json"])["groups"]
        self.assertEqual(groups["flagged"]["n"], 1)
        self.assertEqual(groups["unflagged"]["n"], self.N_STOCKS - 1)
        self.assertIsNotNone(groups["flagged"]["mean_max_drawdown"])
        self.assertAlmostEqual(by_id["risk_flag|spike"]["n_eff"], 1 / 20.0)
        self.assertIsNone(by_id["risk_flag|spike"]["rank_ic_mean"])


# ---------------------------------------------------------------- 도전 안 재평가

class ReevalTest(unittest.TestCase):
    """가중치 0 인 관찰 요인(news_risk)을 켜면 섹터 점수가 뒤집혀 종목 순위까지 달라져야 한다."""

    def setUp(self):
        self.days = weekdays("2026-06-01", 30)
        self.env = Env(self.days)
        self.sectors = {"반도체": SEC_ETF, "은행": BANK_ETF}
        self.codes = {"반도체": ["S0001", "S0002", "S0003"], "은행": ["S0004", "S0005", "S0006"]}
        rows = []
        for sector, codes in self.codes.items():
            for c in codes:
                rows.append({"code": c, "name": c, "kind": "stock", "sector": sector})
        self.env.universe(rows)
        self.env.flat(CASH, 100.0)
        for code in (CORE, SEC_ETF, BANK_ETF):
            self.env.flat(code, 100.0)
        for i, d in enumerate(self.days):
            for sector, codes in self.codes.items():
                for k, c in enumerate(codes):
                    px = 100.0 * (1.0 + 0.001 * (k + 1) * i * (2 if sector == "은행" else 1))
                    self.env.price(c, d, px, px)
        self.env.store.commit()
        self.run_ids = [self.make_run(self.days[i]) for i in (0, 1, 2)]
        scoring.score_outcomes(self.env.store, self.env.cfg, self.env.cal, f"{self.days[-1]}T18:30:00")
        self.challenger, _ = self._challenger({"factors": {"news_risk": 2}})

    def tearDown(self):
        self.env.close()

    def _challenger(self, patch):
        path = Path(self.env.dir.name) / "challenger.yaml"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(patch, f)                   # JSON 은 YAML 의 부분집합이라 그대로 읽힌다
        return reeval.load_challenger(str(path), self.env.cfg)

    def make_run(self, day):
        rid = self.env.store.start_run("final", str(day))
        rows = [{"entity": "MARKET", "factor_id": "mkt_trend", "raw_value": 1.0, "score": 1.0,
                 "missing": 0}]
        # 섹터: 코드 요인은 반도체가 앞서지만, 관찰 요인(news_risk)은 은행을 강하게 민다
        for sector, sign in (("반도체", 1.0), ("은행", -1.0)):
            rows += [{"entity": sector, "factor_id": "sec_trend", "raw_value": sign, "score": sign,
                      "missing": 0},
                     {"entity": sector, "factor_id": "news_risk", "raw_value": -sign, "score": -sign,
                      "missing": 0}]
        for sector, codes in self.codes.items():
            for k, c in enumerate(codes):
                score = 0.30 - 0.05 * k if sector == "반도체" else 0.25 - 0.05 * k
                rows.append({"entity": c, "factor_id": "stk_high52", "raw_value": score,
                             "score": score, "missing": 0})
        self.env.store.put_factor_values(rid, rows)
        weights = reeval.recompute_path(self.env.store, self.env.cfg, self.env.cal,
                                        date_from=day, date_to=day)
        self.env.store.put_decision(rid, "v0", 1.0, 0.9)
        self.env.store.put_target_weights(rid, "v0", [
            {"asset": a, "role": "core", "weight": w}
            for a, w in weights["targets"][str(day)].items()])
        self.env.store.commit()
        return rid

    def test_zero_weight_factor_changes_the_ranking(self):
        champ = reeval.recompute_path(self.env.store, self.env.cfg, self.env.cal)
        chall = reeval.recompute_path(self.env.store, self.challenger, self.env.cal)
        day = str(self.days[0])
        champ_top = max(champ["scores"][self.run_ids[0]].items(), key=lambda kv: kv[1])[0]
        chall_top = max(chall["scores"][self.run_ids[0]].items(), key=lambda kv: kv[1])[0]
        self.assertIn(champ_top, self.codes["반도체"])
        self.assertIn(chall_top, self.codes["은행"],
                      "가중치 0 이던 요인을 켜면 섹터 기울기가 뒤집혀 종목 순위가 달라진다")
        self.assertNotEqual(champ["targets"][day], chall["targets"][day])
        # 섹터 ETF 선택도 달라진다
        self.assertIn(SEC_ETF, champ["targets"][day])
        self.assertIn(BANK_ETF, chall["targets"][day])

    def test_run_reeval_reports_and_writes_nothing(self):
        before = self._counts()
        ro = Store(self.env.path, readonly=True)          # 읽기 전용이라 쓰면 예외가 난다
        try:
            cal = TradingCalendar(self.env.cfg, ro)
            out = reeval.run_reeval(ro, self.env.cfg, cal, self.challenger)
        finally:
            ro.close()
        self.assertEqual(self._counts(), before, "살아 있는 표에 한 줄도 쓰지 않는다")
        self.assertEqual(out["champion"]["n_runs"], 3)
        self.assertEqual(out["challenger"]["n_runs"], 3)
        self.assertNotEqual(out["config_hash"]["champion"], out["config_hash"]["challenger"])
        self.assertEqual(out["weight_diff"][0]["key"], "factors.news_risk.weight")
        self.assertIsNotNone(out["champion"]["nav"]["nav"])
        self.assertIsNotNone(out["challenger"]["ic"]["rank_ic_mean"])
        self.assertEqual(out["reproduced"][0], out["reproduced"][1],
                         "현재 설정으로 다시 계산하면 그때 저장된 v0 판단이 그대로 나와야 한다")
        # 유효 표본이 3일 ÷ 20일 = 0.15 라 교체 조건은 어차피 미달이어야 한다
        self.assertFalse(out["verdict"]["passed"])
        self.assertIn("유효 표본", out["verdict"]["text"])
        text = reeval.render(out)
        self.assertIn("도전안", text)
        self.assertIn("판정:", text)

    def test_challenger_file_rejects_unknown_factor(self):
        with self.assertRaises(ValueError):
            self._challenger({"factors": {"없는요인": 2}})
        with self.assertRaises(Exception):
            self._challenger({"factors": {"news_risk": 3}})     # 근거 등급 밖의 가중치

    def _counts(self):
        tables = [r[0] for r in self.env.store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        return {t: self.env.store.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in tables}


class PlotNavOptionsTest(unittest.TestCase):
    """plot_nav: --db 로 준 DB 를 읽고 --prefix 이름으로 그림·요약표를 쓰며, 제목 기간은 데이터에서 읽는다."""

    def test_prefix_and_period_from_data(self):
        import csv
        import io
        import sqlite3
        from contextlib import redirect_stdout
        from backend.advisor.devtools import plot_nav

        self.assertEqual(plot_nav._period_label(["2021-01-04", "2026-09-21"]), "2021-01-04 ~ 2026-09-21")
        self.assertEqual(plot_nav._period_label([]), "")
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "long.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE nav(portfolio_id TEXT, date TEXT, nav REAL, turnover REAL, cost REAL)")
            for pid, *_ in plot_nav.PLOT_SERIES + [(plot_nav.SKIPPED_IDENTICAL,)]:
                for i, d in enumerate(["2021-01-04", "2021-01-05", "2021-01-06"]):
                    conn.execute("INSERT INTO nav VALUES(?,?,?,?,?)", (pid, d, 1.0 + 0.01 * i, 0.1, 0.001))
            conn.commit()
            conn.close()
            with redirect_stdout(io.StringIO()):
                code = plot_nav.main(["--db", str(db), "--out", tmp, "--prefix", "replay_long"])
            self.assertEqual(code, 0)
            self.assertTrue((Path(tmp) / "replay_long_nav.png").exists())
            with open(Path(tmp) / "replay_long_summary.csv", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["구간"], "2021-01-04~2021-01-06")
            self.assertAlmostEqual(float(rows[0]["누적수익률"]), 0.02)
            self.assertFalse((Path(tmp) / "replay_nav.png").exists())


if __name__ == "__main__":
    unittest.main()
