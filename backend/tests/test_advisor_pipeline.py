"""advisor 코드 요인·위험 표시·증빙·배치 단위 테스트. 네트워크를 쓰지 않고 임시 DB 로만 돈다.

확인하는 것:
  - 시점 규칙(설계 2.3): 18:00/07:00 경계, assert 헬퍼
  - 요인별 원본 값과 부호·변환: 손으로 검산되는 작은 사례
  - **미래 누출**: 나중 날짜 행을 덧붙여도 이전 as_of 의 요인 값·위험 표시가 한 개도 바뀌지 않는다
    (이 파일에서 가장 중요한 검사다 — 조사한 FinAgent 의 미래 누출 사고가 정확히 이 자리에서 났다)
  - 단계 차이: prelim 에는 mkt_overnight 이 결측, final 에는 값이 있다 (결정 11의 실험 설계)
  - 위험 표시: 공시 유형·제목·거래량 0·급등락, 그리고 공시는 접수일이 아니라 '처음 본 시각'으로 판정
  - 증빙: 해시 사슬 검증과 변조 탐지 (DB·원장 파일 양쪽)
  - 배치: 휴장일 건너뜀, 전 테이블 기록, 비중 합 1, 재실행, 이력(보유) 인계, CLI 종료 코드
"""
from backend.tests import _safety  # noqa: F401  맨 먼저: 실제 주문·외부 접속 차단 (_safety.py 참고)
import contextlib
import copy
import io
import json
import logging
import random
import statistics
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from backend.advisor import allocate, ledger, run as run_mod
from backend.advisor.calendar import TradingCalendar
from backend.advisor.config import load_config
from backend.advisor.factors import asof, compute, market, sector, stock
from backend.advisor.factors.registry import load_specs
from backend.advisor.risk_flags import compute_risk_flags
from backend.advisor.store import Store

PRICE_COLS = ("code", "date", "open", "high", "low", "close", "volume", "value", "adj_close")


def setUpModule():
    """배치 진행 로그는 테스트 출력에 섞지 않는다 (CLI 테스트가 로깅을 켜기 때문에 필요하다)."""
    logging.getLogger("advisor").setLevel(logging.CRITICAL)


def price(code, day, close, volume=1000.0, **over):
    row = dict(zip(PRICE_COLS, (code, str(day), close, close, close, close, volume, None, close)))
    row.update(over)
    return row


def flow(code, day, foreign_net=0.0, inst_net=0.0, mktcap=None):
    return {"code": code, "date": str(day), "foreign_net": foreign_net, "inst_net": inst_net,
            "mktcap": mktcap}


def series(name, day, value):
    return {"series": name, "date": str(day), "value": value}


def spec_of(cfg, fid, **params):
    """설정의 요인 메타데이터에서 params 만 갈아 끼운다 (작은 사례를 손으로 검산하려고 창을 줄인다)."""
    spec = load_specs(cfg)[fid]
    return replace(spec, params={**spec.params, **params}) if params else spec


def tiny_cfg(min_history=2):
    """작은 사례용 설정. 과거 분포 최소 표본만 낮춘다 — 나머지는 실제 v0 설정 그대로."""
    cfg = copy.deepcopy(load_config())
    cfg["normalize"]["min_history"] = min_history
    return cfg


class TempStoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name)
        self.store = Store(self.path / "advisor.db")
        self.cfg = tiny_cfg()

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def cal(self, cfg=None):
        return TradingCalendar(cfg or self.cfg, self.store)


# ---------------------------------------------------------------- 시점 규칙

class AsOfRuleTest(unittest.TestCase):
    """설계 2.3: 국내 일봉은 D일 18:00, 미국 일봉은 D+1일 07:00 부터 쓸 수 있다."""

    def test_kr_boundary_is_18_00(self):
        self.assertEqual(asof.last_kr_date("2026-09-22T17:59"), date(2026, 9, 21))
        self.assertEqual(asof.last_kr_date("2026-09-22T18:00"), date(2026, 9, 22))
        self.assertEqual(asof.last_kr_date("2026-09-22T18:30"), date(2026, 9, 22), "예비 판단")
        self.assertEqual(asof.last_kr_date("2026-09-23T07:40"), date(2026, 9, 22), "최종 판단")
        self.assertEqual(asof.last_kr_date(date(2026, 9, 22)), date(2026, 9, 21),
                         "시각 없는 날짜는 00:00 — 가장 보수적인 값")

    def test_us_boundary_is_07_00(self):
        self.assertEqual(asof.last_us_date("2026-09-23T06:59"), date(2026, 9, 21))
        self.assertEqual(asof.last_us_date("2026-09-23T07:00"), date(2026, 9, 22))
        self.assertEqual(asof.last_us_date("2026-09-23T07:40"), date(2026, 9, 22),
                         "최종 판단은 전날 미국 장 마감을 쓴다")
        self.assertEqual(asof.last_us_date("2026-09-22T18:30"), date(2026, 9, 21),
                         "예비 판단 시각에는 그날 미국 장이 아직 열리지도 않았다")

    def test_boundary_follows_the_collector_setting(self):
        """수집(sources.*)과 읽기가 같은 경계를 봐야 한다 — 어긋나면 자료가 사라지거나 미래가 샌다."""
        cfg = {"sources": {"daily_bar_known_at": "19:00", "us_bar_known_at": "08:30"}}
        self.assertEqual(asof.last_kr_date("2026-09-22T18:30", cfg), date(2026, 9, 21))
        self.assertEqual(asof.last_kr_date("2026-09-22T19:00", cfg), date(2026, 9, 22))
        self.assertEqual(asof.last_us_date("2026-09-23T07:40", cfg), date(2026, 9, 21))
        self.assertEqual(asof.last_us_date("2026-09-23T08:30", cfg), date(2026, 9, 22))
        self.assertEqual(asof.last_kr_date("2026-09-22T18:30", {"sources": {"daily_bar_known_at": "엉터리"}}),
                         date(2026, 9, 22), "값이 이상하면 기본 경계로 물러선다")
        # 저장소의 실제 설정이 기본 경계와 같은지 (같아야 설계 2.3 의 표가 그대로 성립한다)
        repo = load_config()
        self.assertEqual(asof.last_kr_date("2026-09-22T18:00", repo), date(2026, 9, 22))
        self.assertEqual(asof.last_us_date("2026-09-23T07:00", repo), date(2026, 9, 22))

    def test_assert_helper_catches_future_rows(self):
        self.assertTrue(asof.assert_not_after(["2026-09-21", "2026-09-22"], "2026-09-22", "x"))
        self.assertTrue(asof.assert_not_after([None], "2026-09-22", "x"))
        self.assertTrue(asof.assert_not_after([], "2026-09-22", "x"))
        with self.assertRaises(asof.AsOfViolation) as ctx:
            asof.assert_not_after(["2026-09-23"], "2026-09-22", "price_daily(005930)")
        self.assertIn("2026-09-23", str(ctx.exception))
        self.assertIn("price_daily", str(ctx.exception))

    def test_to_datetime_forms(self):
        self.assertEqual(asof.to_datetime("2026-09-22"), datetime(2026, 9, 22, 0, 0))
        self.assertEqual(asof.to_datetime("2026-09-22T18:30"), datetime(2026, 9, 22, 18, 30))
        self.assertEqual(asof.to_datetime("2026-09-22 18:30:05"), datetime(2026, 9, 22, 18, 30, 5))
        self.assertEqual(asof.to_datetime(date(2026, 9, 22)), datetime(2026, 9, 22))
        with self.assertRaises(TypeError):
            asof.to_datetime(20260922)

    def test_min_obs(self):
        self.assertEqual(asof.min_obs(250, 0.6), 150)
        self.assertEqual(asof.min_obs(20, 0.6), 12)
        self.assertEqual(asof.min_obs(3, 0.0), 1, "0 이어도 최소 한 개는 요구한다")

    def test_tunable_prefers_config(self):
        defaults = {"risk_flags.recent_days": 5}
        self.assertEqual(asof.tunable({}, "risk_flags.recent_days", defaults), 5)
        self.assertEqual(asof.tunable({"risk_flags": {"recent_days": 9}},
                                      "risk_flags.recent_days", defaults), 9)
        self.assertEqual(asof.tunable({"risk_flags": {"recent_days": None}},
                                      "risk_flags.recent_days", defaults), 5)


# ---------------------------------------------------------------- 요인 하나씩 (손으로 검산)

class MarketFactorTest(TempStoreCase):
    def test_mkt_trend_rule_and_raw(self):
        for day, close in (("2026-09-16", 100.0), ("2026-09-17", 110.0), ("2026-09-18", 120.0)):
            self.store.put_market([series("KOSPI", day, close)])
        spec = spec_of(self.cfg, "mkt_trend", ma_days=3)
        got = market.mkt_trend(self.store, self.cfg, spec, "2026-09-18T18:30")
        raw, score, missing = got["MARKET"]
        self.assertAlmostEqual(raw, 120.0 / 110.0, msg="원본 값은 종가 ÷ 이동평균")
        self.assertEqual((score, missing), (1.0, False), "이동평균 위 → 규칙형 +1")

        # 기준 시각을 한 시간 앞당기면 18:00 규칙에 걸려 9/17 이 마지막 일봉이다 → 자료 부족
        self.assertEqual(market.mkt_trend(self.store, self.cfg, spec, "2026-09-18T17:59")["MARKET"],
                         (None, 0.0, True), "200일치가 없으면 지어내지 않는다")

    def test_mkt_trend_below_ma_is_minus_one(self):
        for day, close in (("2026-09-16", 120.0), ("2026-09-17", 110.0), ("2026-09-18", 90.0)):
            self.store.put_market([series("KOSPI", day, close)])
        spec = spec_of(self.cfg, "mkt_trend", ma_days=3)
        raw, score, missing = market.mkt_trend(self.store, self.cfg, spec, "2026-09-18T18:30")["MARKET"]
        self.assertAlmostEqual(raw, 90.0 / (320.0 / 3))
        self.assertEqual((score, missing), (-1.0, False))

    def test_mkt_vol_is_stdev_and_sign_is_negative(self):
        closes = [100.0, 101.0, 102.0, 103.0, 110.0]
        for i, close in enumerate(closes):
            self.store.put_market([series("KOSPI", f"2026-09-{14 + i:02d}", close)])
        spec = spec_of(self.cfg, "mkt_vol", window=2)
        raw, score, missing = market.mkt_vol(self.store, self.cfg, spec, "2026-09-18T18:30")["MARKET"]
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        self.assertAlmostEqual(raw, statistics.stdev(rets[-2:]), msg="원본 값은 20일 표준편차")
        self.assertEqual(missing, False)
        self.assertAlmostEqual(score, -1.0, msg="과거 분포의 최상단 변동성 → 부호 − 이므로 −1")

    def test_mkt_vol_missing_without_enough_history(self):
        cfg = tiny_cfg(min_history=60)
        for i, close in enumerate([100.0, 101.0, 102.0, 110.0]):
            self.store.put_market([series("KOSPI", f"2026-09-{14 + i:02d}", close)])
        spec = spec_of(cfg, "mkt_vol", window=2)
        self.assertEqual(market.mkt_vol(self.store, cfg, spec, "2026-09-17T18:30")["MARKET"][2], True)

    def test_mkt_overnight_composite(self):
        self.store.put_market([series("SP500", "2026-09-21", 100.0), series("SP500", "2026-09-22", 101.0),
                               series("SOX", "2026-09-21", 100.0), series("SOX", "2026-09-22", 102.0),
                               series("USDKRW", "2026-09-21", 1000.0), series("USDKRW", "2026-09-22", 990.0)])
        spec = spec_of(self.cfg, "mkt_overnight")
        raw, _, missing = market.mkt_overnight(self.store, self.cfg, spec, "2026-09-23T07:40")["MARKET"]
        self.assertAlmostEqual(raw, (0.01 + 0.02 + 0.01) / 3,
                               msg="원/달러 하락(원화 강세)은 + 로 들어간다")
        self.assertTrue(missing, "비교할 과거 분포가 없으면 점수는 결측이다")

    def test_mkt_overnight_respects_the_07_00_rule(self):
        self.store.put_market([series("SP500", "2026-09-21", 100.0), series("SP500", "2026-09-22", 101.0)])
        spec = spec_of(self.cfg, "mkt_overnight")
        early = market.mkt_overnight(self.store, self.cfg, spec, "2026-09-23T06:59")["MARKET"]
        self.assertEqual(early, (None, 0.0, True), "07:00 전에는 전날 미국 종가를 쓸 수 없다")

    def test_mkt_credit_is_always_missing_but_recorded(self):
        spec = spec_of(self.cfg, "mkt_credit")
        self.assertEqual(market.mkt_credit(self.store, self.cfg, spec, "2026-09-22T18:30"),
                         {"MARKET": (None, 0.0, True)})


class StockFactorTest(TempStoreCase):
    def setUp(self):
        super().setUp()
        self.uni = {"stocks": {c: {"sector": None} for c in ("A", "B", "C")}, "sectors": [],
                    "members": {}, "sector_etf": {}, "sector_of": {}}

    def test_stk_high52_ratio_and_rank(self):
        for code, closes in (("A", [100.0, 90.0]), ("B", [100.0, 100.0]), ("C", [100.0, 80.0])):
            for day, close in zip(("2026-09-21", "2026-09-22"), closes):
                self.store.put_prices([price(code, day, close)])
        spec = spec_of(self.cfg, "stk_high52", lookback=2)
        got = stock.stk_high52(self.store, self.cfg, spec, "2026-09-22T18:30", self.cal(), self.uni)
        self.assertAlmostEqual(got["A"][0], 0.9)
        self.assertAlmostEqual(got["B"][0], 1.0)
        self.assertAlmostEqual(got["C"][0], 0.8)
        self.assertAlmostEqual(got["B"][1], 2 * (3 - 0.5) / 3 - 1, msg="고가에 붙을수록 + (부호 +)")
        self.assertAlmostEqual(got["A"][1], 0.0)
        self.assertAlmostEqual(got["C"][1], 2 * (1 - 0.5) / 3 - 1)
        self.assertFalse(any(v[2] for v in got.values()))

    def test_stk_high52_missing_when_history_is_short(self):
        self.store.put_prices([price("A", "2026-09-22", 100.0)])
        spec = spec_of(self.cfg, "stk_high52", lookback=2)       # 최소 관측 2개
        got = stock.stk_high52(self.store, self.cfg, spec, "2026-09-22T18:30", self.cal(),
                               {"stocks": {"A": {}}})
        self.assertEqual(got["A"], (None, 0.0, True))

    def test_stk_flow_divides_by_the_last_known_mktcap(self):
        for day in ("2026-09-21", "2026-09-22"):
            self.store.put_prices([price(c, day, 100.0) for c in ("A", "B", "C")])
        self.store.put_flows([
            flow("A", "2026-09-21", 10.0, 0.0, 1000.0), flow("A", "2026-09-22", 0.0, 20.0, None),
            flow("B", "2026-09-21", 5.0, 0.0, 1000.0), flow("B", "2026-09-22", 0.0, 5.0, None),
            flow("C", "2026-09-21", 99.0, 0.0, None), flow("C", "2026-09-22", 0.0, 99.0, None)])
        spec = spec_of(self.cfg, "stk_flow", days=2)
        got = stock.stk_flow(self.store, self.cfg, spec, "2026-09-22T18:30", self.cal(), self.uni)
        self.assertAlmostEqual(got["A"][0], 30.0 / 1000.0, msg="(외국인+기관) 2일 합 ÷ 마지막 시가총액")
        self.assertAlmostEqual(got["B"][0], 10.0 / 1000.0)
        self.assertEqual(got["C"], (None, 0.0, True), "시가총액을 모르면 강도를 만들 수 없다")
        self.assertAlmostEqual(got["A"][1], 0.5, msg="결측은 순위에서 빠져 둘 중 위가 +0.5")
        self.assertAlmostEqual(got["B"][1], -0.5)


class SectorFactorTest(TempStoreCase):
    def setUp(self):
        super().setUp()
        self.uni = {"stocks": {"A": {}, "B": {}, "C": {}},
                    "sector_of": {"A": "반도체", "B": "반도체", "C": "은행"},
                    "members": {"반도체": ["A", "B"], "은행": ["C"]},
                    "sectors": ["반도체", "은행"],
                    "sector_etf": {"반도체": "091160", "은행": "091170"}}

    def test_sec_flow_is_sum_over_sum(self):
        for day in ("2026-09-21", "2026-09-22"):
            self.store.put_prices([price(c, day, 100.0) for c in ("A", "B", "C")])
        self.store.put_flows([
            flow("A", "2026-09-21", 10.0, 0.0, 1000.0), flow("A", "2026-09-22", 0.0, 20.0, 1000.0),
            flow("B", "2026-09-21", 5.0, 0.0, 3000.0), flow("B", "2026-09-22", 0.0, 5.0, 3000.0),
            flow("C", "2026-09-21", 1.0, 0.0, 500.0), flow("C", "2026-09-22", 0.0, 1.0, 500.0)])
        spec = spec_of(self.cfg, "sec_flow", days=2)
        got = sector.sec_flow(self.store, self.cfg, spec, "2026-09-22T18:30", self.cal(), self.uni)
        self.assertAlmostEqual(got["반도체"][0], (30.0 + 10.0) / (1000.0 + 3000.0))
        self.assertAlmostEqual(got["은행"][0], 2.0 / 500.0)
        self.assertGreater(got["반도체"][0], got["은행"][0])
        self.assertAlmostEqual(got["반도체"][1], 0.5, msg="섹터 간 순위, 부호 + (순매수가 강한 쪽이 +)")
        self.assertAlmostEqual(got["은행"][1], -0.5)

    def test_sec_flow_drops_members_without_mktcap(self):
        for day in ("2026-09-21", "2026-09-22"):
            self.store.put_prices([price(c, day, 100.0) for c in ("A", "B", "C")])
        self.store.put_flows([
            flow("A", "2026-09-21", 10.0, 0.0, 1000.0), flow("A", "2026-09-22", 0.0, 20.0, 1000.0),
            flow("B", "2026-09-21", 999.0, 0.0, None), flow("B", "2026-09-22", 0.0, 999.0, None)])
        spec = spec_of(self.cfg, "sec_flow", days=2)
        got = sector.sec_flow(self.store, self.cfg, spec, "2026-09-22T18:30", self.cal(), self.uni)
        self.assertAlmostEqual(got["반도체"][0], 30.0 / 1000.0, msg="분자·분모에서 함께 뺀다")
        self.assertEqual(got["은행"], (None, 0.0, True), "쓸 수 있는 구성 종목이 없으면 결측")

    def test_sec_trend_rule(self):
        for i, close in enumerate([100.0, 110.0, 120.0]):
            self.store.put_prices([price("091160", f"2026-09-{16 + i:02d}", close),
                                   price("091170", f"2026-09-{16 + i:02d}", 120.0 - 10 * i)])
        spec = spec_of(self.cfg, "sec_trend", ma_days=3)
        got = sector.sec_trend(self.store, self.cfg, spec, "2026-09-18T18:30", self.cal(), self.uni)
        self.assertAlmostEqual(got["반도체"][0], 120.0 / 110.0)
        self.assertEqual(got["반도체"][1:], (1.0, False))
        self.assertEqual(got["은행"][1:], (-1.0, False), "이동평균 아래 → −1")

    def test_sec_trend_missing_without_etf_prices(self):
        spec = spec_of(self.cfg, "sec_trend", ma_days=3)
        got = sector.sec_trend(self.store, self.cfg, spec, "2026-09-18T18:30", self.cal(), self.uni)
        self.assertEqual(got["반도체"], (None, 0.0, True))


# ---------------------------------------------------------------- 합성 시장 (미래 누출 검사용)

class SyntheticMarket:
    """씨앗 하나로 결정되는 가짜 시장. 같은 씨앗이면 언제 만들어도 같은 값이 나온다.

    30종목 × 300거래일 + 섹터 3개 + ETF + 코스피·미국 지수·환율 + 공시 몇 건.
    **부분 저장**(`write(store, upto=…)`)을 지원하는 것이 핵심이다. 같은 기준 시각을
    '그날까지만 아는 DB'와 '미래까지 다 아는 DB'에서 각각 계산해 값이 같아야 미래 누출이 없다.
    """

    SECTORS = ("반도체", "2차전지", "자동차")
    SECTOR_ETF = {"반도체": "091160", "2차전지": "305720", "자동차": "091180"}
    NO_MKTCAP = "100007"        # 시가총액을 한 번도 못 받은 종목 → 수급 요인 결측
    STALE_MKTCAP = "100008"     # 옛 시가총액만 있는 종목 → 마지막으로 알려진 값을 쓴다
    ZERO_VOLUME = "100000"      # 마지막 날 거래량 0 → halt
    SPIKER = "100001"           # 최근 5거래일 급등 → spike
    NEW_LISTING = "100030"      # 마지막 스냅샷에만 들어오는 신규 편입 종목

    def __init__(self, seed=20260922, n_days=300, n_stocks=30, end=date(2026, 9, 22)):
        rnd = random.Random(seed)
        self.days = self._weekdays(end, n_days)
        self.codes = [f"{100000 + i}" for i in range(n_stocks)]
        self.sector_of = {c: self.SECTORS[i % len(self.SECTORS)] for i, c in enumerate(self.codes)}
        self.prices, self.flows, self.market, self.universe, self.disclosures = [], [], [], [], []
        self._prices(rnd)
        self._flows(rnd)
        self._market(rnd)
        self._universe()
        self._disclosures()

    @staticmethod
    def _weekdays(end, n):
        out, d = [], end
        while len(out) < n:
            if d.weekday() < 5:
                out.append(d)
            d -= timedelta(days=1)
        return list(reversed(out))

    def _walk(self, rnd, start, days, drift=0.0003, vol=0.015):
        out, level = [], float(start)
        for _ in days:
            level = max(1.0, level * (1.0 + rnd.gauss(drift, vol)))
            out.append(round(level, 2))
        return out

    def _prices(self, rnd):
        etfs = list(self.SECTOR_ETF.values()) + ["069500", "459580"]
        for i, code in enumerate(self.codes):
            closes = self._walk(rnd, 10000 + 500 * i, self.days)
            if code == self.SPIKER:                       # 최근 5거래일 급등 → spike 표시
                for k in range(5, 0, -1):
                    closes[-k] = round(closes[-6] * (1.09 ** (6 - k)), 2)
            for d, close in zip(self.days, closes):
                vol = 0.0 if (code == self.ZERO_VOLUME and d == self.days[-1]) else 1000.0 + i
                self.prices.append(price(code, d, close, volume=vol))
        for j, code in enumerate(etfs):
            drift = 0.0002 if code != "459580" else 0.00005     # 현금 대용은 거의 평평하다
            for d, close in zip(self.days, self._walk(rnd, 10000 + 100 * j, self.days, drift,
                                                      0.01 if code != "459580" else 0.0005)):
                self.prices.append(price(code, d, close))
        for d, close in zip(self.days[-30:], self._walk(rnd, 5000, self.days[-30:])):
            self.prices.append(price(self.NEW_LISTING, d, close))

    def _flows(self, rnd):
        for i, code in enumerate(self.codes):
            cap = 1e12 * (1 + i)
            for d in self.days:
                if code == self.NO_MKTCAP:
                    mktcap = None
                elif code == self.STALE_MKTCAP:
                    mktcap = cap if d <= self.days[100] else None
                else:
                    mktcap = cap
                self.flows.append(flow(code, d, rnd.gauss(0, 1e9), rnd.gauss(0, 1e9), mktcap))

    def _market(self, rnd):
        for name, start, vol in (("KOSPI", 2500.0, 0.008), ("SP500", 5000.0, 0.009),
                                 ("SOX", 4000.0, 0.015), ("USDKRW", 1300.0, 0.004)):
            for d, value in zip(self.days, self._walk(rnd, start, self.days, 0.0002, vol)):
                self.market.append(series(name, d, value))

    def _universe(self):
        for snap_at in (self.days[0], self.days[150], self.days[-1]):
            rows = [{"code": c, "name": f"종목{c}", "kind": "stock", "sector": self.sector_of[c]}
                    for c in self.codes]
            rows += [{"code": etf, "name": sec, "kind": "etf", "sector": sec}
                     for sec, etf in self.SECTOR_ETF.items()]
            rows += [{"code": "069500", "name": "KODEX 200", "kind": "etf", "sector": None},
                     {"code": "459580", "name": "현금대용", "kind": "cash_etf", "sector": None}]
            if snap_at == self.days[-1]:                  # 마지막 스냅샷에만 들어오는 신규 편입
                rows.append({"code": self.NEW_LISTING, "name": "신규", "kind": "stock",
                             "sector": "반도체"})
            self.universe.append((snap_at, rows))

    def _disclosures(self):
        last, prev, old = self.days[-1], self.days[-2], self.days[-8]

        def rec(no, code, title, day, seen, kind):
            return {"rcept_no": no, "stock_code": code, "corp_name": f"회사{code}",
                    "report_nm": title, "rcept_dt": day.strftime("%Y%m%d"),
                    "first_seen_at": seen, "first_seen_src": "dart", "ls_realkey": None,
                    "kind": kind, "ratio": None, "ratio_ok": None, "body_src": None}

        self.disclosures = [
            rec("1", "100002", "매매거래정지", last, f"{last}T09:00:00.000", "거래정지"),
            rec("2", "100003", "투자경고종목지정", prev, f"{prev}T10:00:00.000", "시장조치"),
            rec("3", "100004", "횡령ㆍ배임 혐의발생", last, f"{last}T11:00:00.000", None),
            rec("4", "100005", "매매거래정지", last, f"{last}T19:00:00.000", "거래정지"),
            rec("5", "100006", "매매거래정지", old, f"{old}T09:00:00.000", "거래정지"),
            rec("6", "100009", "단일판매ㆍ공급계약체결", last, f"{last}T13:00:00.000", "공급계약"),
        ]

    def write(self, store, upto=None):
        """days[upto] 까지만 저장한다 (upto=None 이면 전부). 미래 누출 검사의 '그날까지 아는 DB'."""
        limit = str(self.days[upto] if upto is not None else self.days[-1])
        store.put_prices([r for r in self.prices if r["date"] <= limit])
        store.put_flows([r for r in self.flows if r["date"] <= limit])
        store.put_market([r for r in self.market if r["date"] <= limit])
        for snap_at, rows in self.universe:
            if str(snap_at) <= limit:
                store.put_universe(snap_at, rows)
        for rec in self.disclosures:
            if rec["rcept_dt"] <= limit.replace("-", ""):
                store.upsert_disclosure(rec)
        store.commit()
        return store


MARKET_DATA = SyntheticMarket()          # 한 번만 만들어 모든 테스트가 나눠 쓴다 (씨앗 고정)


class SyntheticCase(unittest.TestCase):
    """합성 시장을 임시 DB 에 올린 뒤 요인·위험 표시·배치를 돌리는 공통 바탕."""

    UPTO = 219                            # '그날까지만 아는' 기준 (뒤로 80거래일이 더 있다)

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.cfg = copy.deepcopy(load_config())
        self.cfg["paths"]["db"] = str(self.root / "advisor.db")
        self.cfg["paths"]["ledger"] = str(self.root / "ledger" / "advisor_ledger.jsonl")
        self.cfg["paths"]["newsgap_db"] = str(self.root / "newsgap.db")
        self.m = MARKET_DATA
        self.specs = load_specs(self.cfg)

    def tearDown(self):
        self.dir.cleanup()

    def store_with(self, upto=None, name="advisor.db"):
        store = Store(self.root / name)
        self.addCleanup(store.close)
        return self.m.write(store, upto)

    def factors(self, store, as_of, stage="prelim"):
        cal = TradingCalendar(self.cfg, store)
        uni = asof.universe_snapshot(store, self.cfg, as_of)
        return compute.compute_all(store, self.cfg, self.specs, cal, as_of, stage, uni), uni


class LookAheadTest(SyntheticCase):
    """가장 중요한 검사: 미래 행을 덧붙여도 이전 기준 시각의 값이 하나도 바뀌지 않아야 한다."""

    def setUp(self):
        super().setUp()
        self.as_of = f"{self.m.days[self.UPTO]}T18:30"
        self.partial = self.store_with(upto=self.UPTO, name="partial.db")
        self.full = self.store_with(upto=None, name="full.db")

    @staticmethod
    def _key(rows):
        return sorted((r["entity"], r["factor_id"], r["raw_value"], r["score"], r["missing"])
                      for r in rows)

    def test_every_factor_is_unchanged_by_future_rows(self):
        part, _ = self.factors(self.partial, self.as_of)
        full, _ = self.factors(self.full, self.as_of)
        self.assertEqual(self._key(part), self._key(full),
                         "미래 일봉·수급·지수·공시·대상 목록이 요인 값을 바꿔서는 안 된다")
        # 요인마다 따로도 확인한다 (어느 요인이 샜는지 바로 보이게)
        for fid in self.specs:
            p = self._key([r for r in part if r["factor_id"] == fid])
            f = self._key([r for r in full if r["factor_id"] == fid])
            self.assertEqual(p, f, f"{fid} 에서 미래 정보가 샜다")
        self.assertTrue(any(not r["missing"] for r in part), "전부 결측이면 검사가 무의미하다")

    def test_future_universe_snapshot_is_not_used(self):
        _, uni = self.factors(self.full, self.as_of)
        self.assertEqual(uni["snapshot_date"], str(self.m.days[150]),
                         "as_of 이하의 마지막 스냅샷만 쓴다")
        self.assertNotIn(self.m.NEW_LISTING, uni["stocks"], "나중에 편입될 종목이 오늘 보이면 안 된다")

    def test_risk_flags_are_unchanged_by_future_rows(self):
        cal_p = TradingCalendar(self.cfg, self.partial)
        cal_f = TradingCalendar(self.cfg, self.full)
        rows_p, flags_p = compute_risk_flags(self.partial, self.cfg, cal_p, self.as_of)
        rows_f, flags_f = compute_risk_flags(self.full, self.cfg, cal_f, self.as_of)
        self.assertEqual(rows_p, rows_f)
        self.assertEqual(flags_p, flags_f)

    def test_factor_values_are_stable_across_repeated_runs(self):
        first, _ = self.factors(self.full, self.as_of)
        second, _ = self.factors(self.full, self.as_of)
        self.assertEqual(self._key(first), self._key(second), "같은 입력이면 같은 값이 나와야 재현된다")


class StageAndShapeTest(SyntheticCase):
    def setUp(self):
        super().setUp()
        self.store = self.store_with()
        self.day = self.m.days[-1]

    def test_overnight_is_missing_in_prelim_and_present_in_final(self):
        prelim, _ = self.factors(self.store, f"{self.day}T18:30", stage="prelim")
        final, _ = self.factors(self.store, f"{self.m.days[-1]}T07:40", stage="final")
        p = [r for r in prelim if r["factor_id"] == "mkt_overnight"][0]
        f = [r for r in final if r["factor_id"] == "mkt_overnight"][0]
        self.assertEqual((p["missing"], p["raw_value"]), (1, None),
                         "예비 단계에 밤사이 정보가 없는 것이 실험 설계다 (결정 11)")
        self.assertEqual(f["missing"], 0)
        self.assertIsNotNone(f["raw_value"])
        self.assertTrue(-1.0 <= f["score"] <= 1.0)

    def test_table_shape_is_complete(self):
        rows, uni = self.factors(self.store, f"{self.day}T18:30")
        by_factor = {}
        for r in rows:
            by_factor.setdefault(r["factor_id"], []).append(r)
        self.assertEqual(set(by_factor), set(self.specs), "설정의 모든 요인에 행이 있다")
        self.assertEqual(len(by_factor["mkt_trend"]), 1)
        self.assertEqual(len(by_factor["stk_high52"]), len(uni["stocks"]))
        self.assertEqual(len(by_factor["sec_flow"]), len(uni["sectors"]))
        # LLM 요인은 v0 에서 결측 자리만 잡는다 (나중에 같은 키로 덮어쓴다)
        self.assertTrue(all(r["missing"] == 1 for r in by_factor["stk_disclosure"]))
        self.assertTrue(all(r["missing"] == 1 for r in by_factor["news_risk"]))
        self.assertEqual(len(by_factor["stk_disclosure"]), len(uni["stocks"]))
        self.assertEqual(len(by_factor["news_risk"]), len(uni["sectors"]))
        self.assertTrue(all(r["missing"] == 1 for r in by_factor["mkt_credit"]))

    def test_sectors_without_data_are_missing_not_absent(self):
        rows, uni = self.factors(self.store, f"{self.day}T18:30")
        self.assertIn("조선", uni["sectors"], "설정에 있는 섹터는 데이터가 없어도 표에 남는다")
        trend = {r["entity"]: r for r in rows if r["factor_id"] == "sec_trend"}
        self.assertEqual(trend["조선"]["missing"], 1)
        self.assertEqual(trend["반도체"]["missing"], 0)

    def test_llm_rows_can_be_overwritten_in_place(self):
        store = self.store
        rows, uni = self.factors(store, f"{self.day}T18:30")
        rid = store.start_run("prelim", str(self.day))
        store.put_factor_values(rid, rows)
        code = sorted(uni["stocks"])[0]
        store.put_factor_values(rid, [{"entity": code, "factor_id": "stk_disclosure",
                                       "raw_value": 2.0, "score": 1.0, "missing": 0}])
        got = store.factor_values(rid, factor_id="stk_disclosure", entity=code)[0]
        self.assertEqual((got["score"], got["missing"]), (1.0, 0))
        self.assertEqual(len(store.factor_values(rid, factor_id="stk_disclosure")), len(uni["stocks"]),
                         "덮어써도 행 수는 그대로다 (표의 모양이 유지된다)")


class RiskFlagTest(SyntheticCase):
    def setUp(self):
        super().setUp()
        self.store = self.store_with()
        self.cal = TradingCalendar(self.cfg, self.store)
        self.day = self.m.days[-1]
        self.rows, self.flags = compute_risk_flags(self.store, self.cfg, self.cal, f"{self.day}T18:30")

    def test_halt_from_disclosure_and_zero_volume(self):
        self.assertIn("halt", self.flags.get("100002", []), "거래정지 공시")
        self.assertIn("halt", self.flags.get(self.m.ZERO_VOLUME, []), "마지막 일봉 거래량 0")

    def test_market_action_and_governance(self):
        self.assertIn("market_action", self.flags.get("100003", []))
        self.assertIn("governance", self.flags.get("100004", []),
                      "제목의 횡령·배임은 코드가 잡는다 (애매한 것만 LLM 몫)")
        self.assertNotIn("100009", self.flags, "공급계약은 위험 표시가 아니다")

    def test_spike(self):
        self.assertIn("spike", self.flags.get(self.m.SPIKER, []))
        detail = [r["detail"] for r in self.rows
                  if r["entity"] == self.m.SPIKER and r["flag_type"] == "spike"][0]
        self.assertIn("5거래일", detail)

    def test_disclosure_is_judged_by_when_it_was_first_seen(self):
        self.assertNotIn("100005", self.flags,
                         "19:00 에 처음 본 공시는 18:30 판단이 알 수 없다 (설계 2.3)")
        later = compute_risk_flags(self.store, self.cfg, self.cal, f"{self.day}T19:30")[1]
        self.assertIn("halt", later.get("100005", []), "19:30 판단에서는 보인다")

    def test_old_disclosure_falls_out_of_the_window(self):
        self.assertNotIn("100006", self.flags, "8거래일 전 거래정지 공시는 최근 5거래일 밖이다")

    def test_rows_and_dict_agree(self):
        for row in self.rows:
            self.assertIn(row["flag_type"], self.flags[row["entity"]])
            self.assertEqual(row["src"], "code")
        self.assertEqual(sorted(self.flags), sorted({r["entity"] for r in self.rows}))

    def test_missing_first_seen_falls_back_to_the_receipt_day_evening(self):
        rec = dict(self.m.disclosures[0], rcept_no="90", stock_code="100010", first_seen_at=None)
        self.store.upsert_disclosure(rec)
        self.store.commit()
        noon = compute_risk_flags(self.store, self.cfg, self.cal, f"{self.day}T12:00")[1]
        evening = compute_risk_flags(self.store, self.cfg, self.cal, f"{self.day}T18:30")[1]
        self.assertNotIn("100010", noon, "처음 본 시각이 없으면 접수일 18:00 로 보수적으로 친다")
        self.assertIn("halt", evening.get("100010", []))


# ---------------------------------------------------------------- 증빙 (해시 사슬)

class LedgerUnitTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "ledger" / "advisor_ledger.jsonl"
        self.record = ledger.decision_record(1, "final", "live", "2026-09-22", "cafe12345678", "v0",
                                             0.5, 0.7, [{"asset": "069500", "role": "core",
                                                         "weight": 0.7000000001},
                                                        {"asset": "459580", "role": "cash",
                                                         "weight": 0.2999999999}])

    def tearDown(self):
        self.dir.cleanup()

    def test_record_is_canonical(self):
        self.assertEqual(self.record["weights"], {"069500": 0.7, "459580": 0.3},
                         "비중은 6자리로 반올림해 환경 차이가 사슬을 깨지 않게 한다")
        other = ledger.decision_record(1, "final", "live", "2026-09-22", "cafe12345678", "v0", 0.5,
                                       0.7, {"459580": 0.3, "069500": 0.7})
        self.assertEqual(ledger.canonical_json(self.record), ledger.canonical_json(other),
                         "자산 순서나 입력 형태가 달라도 같은 판단이면 같은 JSON")

    def test_hash_depends_on_content_and_prev(self):
        h1 = ledger.record_hash(self.record, None)
        self.assertEqual(h1, ledger.record_hash(self.record, ""))
        self.assertNotEqual(h1, ledger.record_hash(self.record, h1))
        changed = dict(self.record, risk_weight=0.71)
        self.assertNotEqual(h1, ledger.record_hash(changed, None))

    def test_file_chain_verifies_and_detects_tampering(self):
        prev = None
        for i in range(3):
            rec = dict(self.record, run_id=i + 1)
            h = ledger.record_hash(rec, prev)
            ledger.append_ledger(self.path, rec, prev, h)
            prev = h
        self.assertEqual(ledger.verify_chain(str(self.path)), (True, None))

        lines = self.path.read_text(encoding="utf-8").splitlines()
        obj = json.loads(lines[1])
        obj["risk_weight"] = 0.99                      # 사후에 판단을 고쳤다고 하자
        lines[1] = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual(ledger.verify_chain(str(self.path)), (False, 1))

    def test_missing_line_breaks_the_chain(self):
        prev = None
        for i in range(3):
            rec = dict(self.record, run_id=i + 1)
            h = ledger.record_hash(rec, prev)
            ledger.append_ledger(self.path, rec, prev, h)
            prev = h
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
        self.assertEqual(ledger.verify_chain(str(self.path)), (False, 1), "줄을 지우면 사슬이 끊긴다")

    def test_empty_and_broken_files(self):
        self.assertEqual(ledger.verify_chain(str(self.path)), (True, None), "없는 파일은 빈 사슬")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{망가진 줄\n", encoding="utf-8")
        self.assertEqual(ledger.verify_chain(str(self.path)), (False, 0))


# ---------------------------------------------------------------- 배치 한 번

class PipelineTest(SyntheticCase):
    def setUp(self):
        super().setUp()
        self.store = self.store_with()
        self.day = self.m.days[-1]
        self.ledger_path = Path(self.cfg["paths"]["ledger"])

    def run_once(self, stage="final", as_of=None, mode="live", **kw):
        return run_mod.run_once(self.store, self.cfg, stage, as_of or self.day, mode=mode, **kw)

    def test_end_to_end_writes_every_table(self):
        summary = {"price_daily": 30, "note": "가짜 수집"}
        run_id = self.run_once(ingest_fn=lambda *a, **k: dict(summary))
        conn = self.store.conn
        row = conn.execute("SELECT * FROM run WHERE run_id=?", (run_id,)).fetchone()
        self.assertEqual((row["status"], row["stage"], row["mode"]), (run_mod.STATUS_OK, "final", "live"))
        self.assertEqual(row["as_of"], str(self.day))
        self.assertTrue(row["decision_time"].startswith(str(self.day)))
        self.assertEqual(json.loads(row["note"])["ingest"], summary, "수집 요약을 run.note 에 남긴다")
        self.assertEqual(row["llm_used"], 0, "v0 만 돌면 LLM 은 쓰지 않은 것이다")
        self.assertIsNotNone(row["finished_at"])
        self.assertTrue(conn.execute("SELECT COUNT(*) FROM config_version").fetchone()[0] >= 1)

        n_factor = conn.execute("SELECT COUNT(*) FROM factor_value WHERE run_id=?", (run_id,)).fetchone()[0]
        uni = asof.universe_snapshot(self.store, self.cfg, self.day)
        per_layer = {layer: sum(1 for s in self.specs.values() if s.layer == layer)
                     for layer in ("market", "sector", "stock")}
        self.assertEqual(per_layer, {"market": 4, "sector": 3, "stock": 4},
                         "stk_earn_growth(관찰 요인, 2026-09-28) 가 더해져 종목 계층 요인은 4개다")
        expected = (per_layer["market"] * 1 + per_layer["sector"] * len(uni["sectors"])
                    + per_layer["stock"] * len(uni["stocks"]))
        self.assertEqual(n_factor, expected, "요인 × 계층별 대상 수만큼 행이 나온다")

        # 종목 계층 요인이 하나도 없는 종목(합성 시장의 신규 편입)은 종합 점수가 없어 composite 에도 없다
        usable = {r["entity"] for r in conn.execute(
            "SELECT entity FROM factor_value WHERE run_id=? AND missing=0 AND factor_id IN "
            "('stk_high52','stk_flow')", (run_id,)).fetchall()}
        self.assertEqual(len(usable), len(uni["stocks"]) - 1)
        self.assertNotIn(self.m.NEW_LISTING, usable, "일봉 30일짜리 신규 편입은 두 요인 모두 결측")
        comp = conn.execute("SELECT layer, COUNT(*) c FROM composite WHERE run_id=? AND variant='v0' "
                            "GROUP BY layer", (run_id,)).fetchall()
        self.assertEqual({r["layer"]: r["c"] for r in comp},
                         {"market": 1, "sector": len(uni["sectors"]), "stock": len(usable)})
        market_row = conn.execute("SELECT * FROM composite WHERE run_id=? AND entity='MARKET'",
                                  (run_id,)).fetchone()
        self.assertEqual((market_row["adj"], market_row["vetoed"]), (0.0, 0))
        self.assertEqual(market_row["base_score"], market_row["final_score"])

        self.assertTrue(conn.execute("SELECT COUNT(*) FROM risk_flag WHERE run_id=?",
                                     (run_id,)).fetchone()[0] > 0)
        dec = conn.execute("SELECT * FROM decision WHERE run_id=?", (run_id,)).fetchone()
        self.assertEqual(dec["variant"], "v0")
        self.assertIsNotNone(dec["record_hash"])
        weights = self.store.target_weights(run_id, "v0")
        self.assertAlmostEqual(sum(w["weight"] for w in weights), 1.0, places=9)
        self.assertEqual(set(w["role"] for w in weights) & {"cash", "core"}, {"cash", "core"})
        self.assertAlmostEqual(1.0 - dict((w["asset"], w["weight"]) for w in weights)["459580"],
                               dec["risk_weight"], places=12, msg="위험자산 비중 = 1 − 현금 비중")

    def test_ledger_chain_verifies_and_detects_tampering(self):
        run_id = self.run_once(no_ingest=True)
        self.assertEqual(ledger.verify_chain(self.store, "live"), (True, None))
        self.assertTrue(self.ledger_path.exists(), "실시간 판단은 원장 파일에도 남는다")
        self.assertEqual(ledger.verify_chain(str(self.ledger_path)), (True, None))
        line = json.loads(self.ledger_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual((line["run_id"], line["variant"], line["mode"]), (run_id, "v0", "live"))

        # 사후에 목표 비중을 고치면 사슬이 깨진다
        self.store.conn.execute("UPDATE target_weight SET weight=weight+0.01 WHERE run_id=? "
                                "AND role='core'", (run_id,))
        self.store.commit()
        ok, idx = ledger.verify_chain(self.store, "live")
        self.assertFalse(ok)
        self.assertEqual(idx, 0)

    def test_replay_mode_does_not_touch_the_ledger_file(self):
        self.run_once(mode="replay", as_of=self.m.days[-2])
        self.assertFalse(self.ledger_path.exists(), "재현 기록은 증빙에 섞지 않는다 (설계 7.4)")
        self.assertEqual(ledger.verify_chain(self.store, "replay"), (True, None))
        self.assertEqual(ledger.verify_chain(self.store, "live"), (True, None), "빈 사슬도 정상")

    def test_holiday_run_is_skipped(self):
        saturday = self.m.days[-1] + timedelta(days=4)
        self.assertEqual(saturday.weekday(), 5)
        run_id = run_mod.run_once(self.store, self.cfg, "final", saturday, no_ingest=True)
        row = self.store.conn.execute("SELECT * FROM run WHERE run_id=?", (run_id,)).fetchone()
        self.assertEqual(row["status"], run_mod.STATUS_SKIPPED)
        self.assertIn("휴장일", row["note"])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM decision WHERE run_id=?",
                                                 (run_id,)).fetchone()[0], 0)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM factor_value WHERE run_id=?",
                                                 (run_id,)).fetchone()[0], 0)

    def test_rerun_creates_a_new_run_without_breaking_the_chain(self):
        first = self.run_once(no_ingest=True)
        second = self.run_once(no_ingest=True)
        self.assertNotEqual(first, second, "같은 (단계, 기준일)을 다시 돌리면 새 실행 기록이 생긴다")
        self.assertEqual(ledger.verify_chain(self.store, "live"), (True, None))
        self.assertEqual(ledger.verify_chain(str(self.ledger_path)), (True, None))
        rows = self.store.conn.execute("SELECT run_id, prev_hash, record_hash FROM decision "
                                       "ORDER BY run_id").fetchall()
        self.assertEqual(rows[1]["prev_hash"], rows[0]["record_hash"], "사슬이 이어진다")

    def test_previous_final_decision_becomes_the_holdings(self):
        earlier = run_mod.run_once(self.store, self.cfg, "final", self.m.days[-2], no_ingest=True)
        held = {w["asset"] for w in self.store.target_weights(earlier, "v0") if w["role"] == "stock"}
        self.assertTrue(held, "첫 판단이 종목을 담아야 이력 검사가 의미가 있다")
        self.assertEqual(run_mod.previous_holdings(self.store, "live", str(self.day)), held)

        seen = {}

        def spy(ctx):
            seen["holdings"] = set(ctx.holdings)
            return "ok"

        self.run_once(no_ingest=True, hooks={"book_trades": spy})
        self.assertEqual(seen["holdings"], held, "다음 판단이 그 보유를 이력으로 받는다")

    def test_holdings_ignore_other_modes_and_future_runs(self):
        run_mod.run_once(self.store, self.cfg, "final", self.m.days[-2], mode="replay", no_ingest=True)
        self.assertEqual(run_mod.previous_holdings(self.store, "live", str(self.day)), set(),
                         "재현 기록의 보유를 실시간 판단이 물려받지 않는다")
        later = run_mod.run_once(self.store, self.cfg, "final", self.day, no_ingest=True)
        self.assertEqual(run_mod.previous_holdings(self.store, "live", str(self.m.days[-2]),
                                                   exclude_run_id=later), set(),
                         "기준일보다 나중 판단은 보유로 쓰지 않는다")

    def test_prelim_and_final_of_the_same_day_differ_only_by_overnight(self):
        prelim = self.run_once(stage="prelim", as_of=self.m.days[-2], no_ingest=True)
        final = self.run_once(stage="final", as_of=self.day, no_ingest=True)
        got = {}
        for rid in (prelim, final):
            row = self.store.conn.execute(
                "SELECT missing FROM factor_value WHERE run_id=? AND factor_id='mkt_overnight'",
                (rid,)).fetchone()
            got[rid] = row["missing"]
        self.assertEqual((got[prelim], got[final]), (1, 0))

    def test_hooks_can_fill_llm_factors_and_add_the_llm_variant(self):
        codes = sorted(asof.universe_snapshot(self.store, self.cfg, self.day)["stocks"])
        chosen = {}

        def llm_factors(ctx):
            return [{"entity": codes[0], "factor_id": "stk_disclosure", "raw_value": 2.0,
                     "score": 1.0, "missing": 0}]

        def llm_adjust(ctx):
            picked = allocate.select_stocks(ctx.stock_scores, ctx.holdings, ctx.flags, ctx.cfg)
            chosen["flagged"] = next(c for c in picked if ctx.flags.get(c))
            chosen["clean"] = next(c for c in picked if not ctx.flags.get(c))
            return {codes[0]: {"adj": 0.5, "reason": "공시 반영", "adopted": ["stk_disclosure"]},
                    chosen["flagged"]: {"veto": True, "reason": "급등락"},
                    chosen["clean"]: {"veto": True, "reason": "표시 없는 자산의 거부"}}

        run_id = self.run_once(no_ingest=True, hooks={"llm_factors": llm_factors,
                                                      "llm_adjust": llm_adjust})
        row = self.store.conn.execute(
            "SELECT * FROM factor_value WHERE run_id=? AND factor_id='stk_disclosure' AND entity=?",
            (run_id, codes[0])).fetchone()
        self.assertEqual((row["score"], row["missing"]), (1.0, 0), "훅이 결측 자리를 덮어쓴다")

        llm = {w["asset"] for w in self.store.target_weights(run_id, "llm") if w["role"] == "stock"}
        self.assertNotIn(chosen["flagged"], llm, "위험 표시가 붙은 자산의 거부는 받아들인다")
        self.assertIn(chosen["clean"], llm, "표시 없는 자산의 거부는 무시한다 (설계 5.6)")
        self.assertAlmostEqual(sum(w["weight"] for w in self.store.target_weights(run_id, "llm")),
                               1.0, places=9)

        comp = self.store.conn.execute(
            "SELECT * FROM composite WHERE run_id=? AND variant='llm' AND entity=?",
            (run_id, codes[0])).fetchone()
        self.assertAlmostEqual(comp["final_score"] - comp["base_score"],
                               self.cfg["llm"]["adj_cap"], msg="조정 폭은 코드가 다시 ±0.2 로 자른다")
        self.assertEqual((comp["reason"], comp["adopted_json"]), ("공시 반영", '["stk_disclosure"]'))
        v0_row = self.store.conn.execute(
            "SELECT * FROM composite WHERE run_id=? AND variant='v0' AND entity=?",
            (run_id, codes[0])).fetchone()
        self.assertAlmostEqual(v0_row["base_score"], comp["base_score"],
                               msg="base 는 조정 전 점수 그대로 — 조정 기여를 한 행에서 읽는다")
        self.assertEqual(self.store.conn.execute(
            "SELECT vetoed FROM composite WHERE run_id=? AND variant='llm' AND entity=?",
            (run_id, chosen["clean"])).fetchone()[0], 0)

        self.assertEqual(self.store.conn.execute("SELECT llm_used FROM run WHERE run_id=?",
                                                 (run_id,)).fetchone()[0], 1)
        self.assertEqual(ledger.verify_chain(self.store, "live"), (True, None),
                         "한 실행의 두 판단도 같은 사슬에 이어 붙는다")
        self.assertEqual(len(self.ledger_path.read_text(encoding="utf-8").splitlines()), 2)

    def test_abstaining_llm_hook_records_a_variant_equal_to_v0(self):
        run_id = self.run_once(no_ingest=True, hooks={"llm_adjust": lambda ctx: {}})
        v0 = {w["asset"]: w["weight"] for w in self.store.target_weights(run_id, "v0")}
        llm = {w["asset"]: w["weight"] for w in self.store.target_weights(run_id, "llm")}
        self.assertEqual(v0, llm, "기권하면 llm 판단은 v0 와 같아지고 그렇게 기록된다 (설계 5.6)")
        adj = self.store.conn.execute("SELECT DISTINCT adj FROM composite WHERE run_id=? AND "
                                      "variant='llm'", (run_id,)).fetchall()
        self.assertEqual([r[0] for r in adj], [0.0])

    def test_failure_leaves_no_partial_decision(self):
        def boom(ctx):
            raise RuntimeError("훅이 터졌다")

        with self.assertRaises(RuntimeError):
            self.run_once(no_ingest=True, hooks={"llm_adjust": boom})
        row = self.store.conn.execute("SELECT * FROM run ORDER BY run_id DESC LIMIT 1").fetchone()
        self.assertEqual(row["status"], run_mod.STATUS_ERROR)
        self.assertIn("훅이 터졌다", row["note"])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM decision WHERE run_id=?",
                                                 (row["run_id"],)).fetchone()[0], 0)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM target_weight WHERE run_id=?",
                                                 (row["run_id"],)).fetchone()[0], 0)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM composite WHERE run_id=?",
                                                 (row["run_id"],)).fetchone()[0], 0)

    def test_ingest_failure_does_not_stop_the_judgement(self):
        def broken(*a, **k):
            raise ConnectionError("pykrx 로그인 실패")

        run_id = self.run_once(ingest_fn=broken)
        row = self.store.conn.execute("SELECT * FROM run WHERE run_id=?", (run_id,)).fetchone()
        self.assertEqual(row["status"], run_mod.STATUS_OK)
        self.assertIn("pykrx", json.loads(row["note"])["ingest"]["error"])
        self.assertTrue(self.store.target_weights(run_id, "v0"))

    def test_halted_stock_is_never_held(self):
        run_id = self.run_once(no_ingest=True, as_of=f"{self.day}T18:30", stage="prelim")
        assets = {w["asset"] for w in self.store.target_weights(run_id, "v0")}
        flagged = {r["entity"] for r in self.store.conn.execute(
            "SELECT entity FROM risk_flag WHERE run_id=? AND flag_type='halt'", (run_id,)).fetchall()}
        self.assertTrue(flagged)
        self.assertEqual(assets & flagged, set(), "halt 는 v0 에서도 후보에서 뺀다 (설계 5.5)")


class CliTest(SyntheticCase):
    def setUp(self):
        super().setUp()
        # CLI 는 실제 훅 묶음(advisor/hooks.py)을 쓴다. 설정에서 LLM 을 꺼 두지 않으면 이 테스트가
        # 진짜 모델을 부르게 된다 — 테스트는 네트워크를 쓰지 않는다 (이 파일 머리말).
        self.cfg["llm"]["enabled"] = False
        self.store = self.store_with()
        self.store.commit()
        self.store.close()
        self.cfg_path = self.root / "advisor.config.yaml"
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.cfg, f, allow_unicode=True, sort_keys=False)

    def cli(self, *args):
        argv = [str(a) for a in args] + ["--config", str(self.cfg_path), "--db", self.cfg["paths"]["db"]]
        self.out = io.StringIO()
        with contextlib.redirect_stdout(self.out):       # 진행 출력은 테스트 결과에 섞지 않는다
            return run_mod.main(argv)

    def test_as_of_defaults_to_the_stage_time(self):
        self.assertEqual(run_mod.resolve_as_of(self.cfg, "prelim", "2026-06-01"),
                         datetime(2026, 6, 1, 18, 30))
        self.assertEqual(run_mod.resolve_as_of(self.cfg, "final", "2026-06-01"),
                         datetime(2026, 6, 1, 7, 40))
        self.assertEqual(run_mod.resolve_as_of(self.cfg, "final", "2026-06-01T09:05"),
                         datetime(2026, 6, 1, 9, 5), "시각을 주면 그대로 쓴다")
        self.assertIsNotNone(run_mod.resolve_as_of(self.cfg, "final", None))

    def test_run_and_replay_range_exit_codes(self):
        self.assertEqual(self.cli("--stage", "final", "--as-of", self.m.days[-1], "--no-ingest"), 0)
        self.assertEqual(self.cli("--replay-range", self.m.days[-4], self.m.days[-1]), 0)
        printed = self.out.getvalue().splitlines()
        self.assertEqual(len(printed), 5, "거래일마다 한 줄 + 마지막 요약 한 줄")
        self.assertIn("재현 완료", printed[-1])
        with Store(self.cfg["paths"]["db"]) as store:
            rows = store.conn.execute("SELECT stage, mode, as_of, status FROM run ORDER BY run_id").fetchall()
            self.assertEqual(rows[0]["mode"], "live")
            replays = [r for r in rows if r["mode"] == "replay"]
            self.assertEqual(len(replays), 4, "구간의 거래일마다 한 번씩")
            self.assertTrue(all(r["stage"] == "final" and r["status"] == run_mod.STATUS_OK
                                for r in replays))
            self.assertEqual(ledger.verify_chain(store, "replay"), (True, None))

    def test_missing_stage_is_an_error(self):
        self.assertEqual(self.cli("--as-of", self.m.days[-1]), 1)

    def test_bad_config_path_is_an_error(self):
        self.assertEqual(run_mod.main(["--stage", "final", "--config", "/tmp/없는설정.yaml"]), 1)


if __name__ == "__main__":
    unittest.main()
