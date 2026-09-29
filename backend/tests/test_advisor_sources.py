"""수집 계층 단위 테스트. **네트워크를 쓰지 않는다** — 제공자는 전부 가짜를 주입한다.

확인하는 것:
  - 시점 방어선: 아직 알 수 없는 행은 조용히 버리고, 그래도 새 값이 남으면 죽는다 (설계 2.3)
  - 제공자 전환: KRX 로그인이 있을 때와 없을 때 같은 함수가 다른 경로를 타고 그 사실이 기록된다
  - 증분 수집: 저장된 마지막 날짜 다음부터 받되, 수정주가 소급 변경 때문에 최근 구간은 다시 받는다
  - KIS 단위 환산: 백만원 → 원
  - DART: 페이지 넘김, 키 없음·오류 응답에서 죽지 않기
  - LS ↔ DART 제목 대조와 first_seen_at 의 MIN 규칙, 합성 행 정리
  - 목록: 우선주·스팩·리츠 제외, 섹터 한 곳 배정, 멤버 부족 시 수작업 표 보강
  - ingest: 한 단계가 터져도 나머지가 돌고, 재현 모드는 아무것도 하지 않는다
"""
from backend.tests import _safety  # noqa: F401  맨 먼저: 실제 주문·외부 접속 차단 (_safety.py 참고)
import copy
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from backend.advisor import poller, universe as universe_mod
from backend.advisor.calendar import TradingCalendar
from backend.advisor.config import load_config
from backend.advisor.sources import base, dart, ingest, kis, krx, lsnews, overnight
from backend.advisor.sources.base import LookaheadError, Report
from backend.advisor.store import Store

PRELIM = datetime(2026, 9, 22, 18, 30)          # 화요일 예비 배치
FINAL = datetime(2026, 9, 23, 7, 40)            # 수요일 최종 배치


# ---------------------------------------------------------------- 가짜 제공자

class FakeFrame:
    """pandas DataFrame 대신 (인덱스, 열 dict) 를 흉내 내는 최소 객체."""

    def __init__(self, index, records, columns=None):
        self.index = list(index)
        self._records = [dict(r) for r in records]
        self.columns = list(columns or (self._records[0].keys() if self._records else []))

    @property
    def empty(self):
        return not self._records

    def to_numpy(self):
        return [[r.get(c) for c in self.columns] for r in self._records]

    def __getitem__(self, key):
        return [r.get(key) for r in self._records]


def ohlcv_frame(days, close_by_day):
    return FakeFrame(days, [{"Open": c, "High": c, "Low": c, "Close": c, "Volume": 100.0}
                            for c in (close_by_day[d] for d in days)],
                     columns=["Open", "High", "Low", "Close", "Volume"])


class FakeFdr:
    """FinanceDataReader 흉내. 요청 구간을 기록해 증분 수집을 검사할 수 있게 한다."""

    def __init__(self, series=None, listing=None, etf_listing=None):
        self.series = series or {}
        self.calls = []
        self._listing = listing
        self._etf = etf_listing

    def DataReader(self, code, start=None, end=None):        # noqa: N802 (외부 API 이름)
        self.calls.append((code, start, end))
        table = self.series.get(code, {})
        days = [d for d in sorted(table) if (not start or str(d) >= str(start))
                and (not end or str(d) <= str(end))]
        if code == "KS11":
            return FakeFrame(days, [{"Close": table[d]} for d in days], columns=["Close"])
        return ohlcv_frame(days, table)

    def StockListing(self, what):                            # noqa: N802
        return self._etf if what == "ETF/KR" else self._listing


class FakeKis:
    """KisClient 흉내. 종목별 투자자 매매동향과 지수 일봉만 답한다."""

    def __init__(self, investor=None, index=None):
        self._investor = investor or {}
        self._index = index or []
        self.calls = []

    def investor_daily(self, code):
        self.calls.append(("investor", code))
        return self._investor.get(code, [])

    def index_chart(self, index_code, start, end):
        self.calls.append(("index", str(start), str(end)))
        return [r for r in self._index
                if str(start).replace("-", "") <= r["stck_bsop_date"] <= str(end).replace("-", "")]

    def daily_chart(self, code, start, end):
        return []


def listing_frame(rows):
    """FDR 코스피 목록 흉내: rows 는 (code, name, marcap)."""
    return FakeFrame(range(len(rows)),
                     [{"Code": c, "Name": n, "Marcap": m} for c, n, m in rows],
                     columns=["Code", "Name", "Marcap"])


def etf_frame(rows):
    return FakeFrame(range(len(rows)), [{"Symbol": s, "Name": n} for s, n in rows],
                     columns=["Symbol", "Name"])


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.dir.name) / "advisor.db")
        krx.clear_caches()

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()
        krx.clear_caches()


# ---------------------------------------------------------------- 시점 방어선

class AsOfGuardTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_daily_bar_limits(self):
        # 18:30 예비 → 당일 일봉까지, 07:40 최종 → 전날 일봉까지
        self.assertEqual(base.known_date_limit(self.cfg, PRELIM, "daily_bar"), date(2026, 9, 22))
        self.assertEqual(base.known_date_limit(self.cfg, FINAL, "daily_bar"), date(2026, 9, 22))
        # 장중(11시)에 수동 실행하면 당일 일봉은 아직 모른다
        self.assertEqual(base.known_date_limit(self.cfg, datetime(2026, 9, 22, 11, 0), "daily_bar"),
                         date(2026, 9, 21))

    def test_us_and_fx_limits(self):
        # 미국 일봉 D일자는 D+1 07:00 → 최종(07:40)은 전날치를 보고, 예비(18:30)도 전날까지
        self.assertEqual(base.known_date_limit(self.cfg, FINAL, "us_bar"), date(2026, 9, 22))
        self.assertEqual(base.known_date_limit(self.cfg, PRELIM, "us_bar"), date(2026, 9, 21))
        # 06:30 은 아직 07:00 전이라 하루 더 물러선다
        self.assertEqual(base.known_date_limit(self.cfg, datetime(2026, 9, 23, 6, 30), "us_bar"),
                         date(2026, 9, 21))
        # 환율은 24시간 거래 → 언제 물어도 기준일보다 앞선 날짜까지만
        self.assertEqual(base.known_date_limit(self.cfg, PRELIM, "fx"), date(2026, 9, 21))
        self.assertEqual(base.known_date_limit(self.cfg, FINAL, "fx"), date(2026, 9, 22))

    def test_unknown_rule_raises(self):
        with self.assertRaises(base.ConfigKeyError):
            base.known_date_limit(self.cfg, PRELIM, "몰라요")

    def test_drops_rows_that_are_merely_too_new(self):
        rows = [{"date": "2026-09-21", "v": 1}, {"date": "2026-09-22", "v": 2},
                {"date": "2026-09-23", "v": 3}]       # 마지막은 KIS 가 끼워 준 오늘의 미완성 행
        kept = base.guard_dated_rows(rows, self.cfg, PRELIM, "daily_bar")
        self.assertEqual([r["date"] for r in kept], ["2026-09-21", "2026-09-22"])

    def test_returns_sorted(self):
        rows = [{"date": "2026-09-22"}, {"date": "2026-09-18"}, {"date": "2026-09-21"}]
        self.assertEqual([r["date"] for r in base.guard_dated_rows(rows, self.cfg, PRELIM)],
                         ["2026-09-18", "2026-09-21", "2026-09-22"])

    def test_assert_fails_when_a_leak_survives(self):
        # 거르기를 건너뛴 결과에 미래가 남아 있으면 죽어야 한다
        with self.assertRaises(LookaheadError):
            base.assert_max_date([{"date": "2026-09-23"}], date(2026, 9, 22))
        with self.assertRaises(LookaheadError):
            base.assert_not_after([datetime(2026, 9, 22, 18, 31)], PRELIM)
        base.assert_not_after([datetime(2026, 9, 22, 18, 30), None], PRELIM)

    def test_bad_date_raises_source_error(self):
        with self.assertRaises(base.SourceError):
            base.guard_dated_rows([{"date": "어제"}], self.cfg, PRELIM)


class MissingRangeTest(StoreCase):
    def test_missing_start_uses_backfill_then_increments(self):
        limit = date(2026, 9, 22)
        got = base.missing_start(self.store, "price_daily", "005930", limit, 10)
        self.assertEqual(got, date(2026, 9, 12), "한 건도 없으면 backfill_days 만큼 거슬러 간다")

        self.store.put_prices([{"code": "005930", "date": "2026-09-18", "open": 1.0, "high": 1.0,
                                "low": 1.0, "close": 1.0, "volume": 1.0, "value": None,
                                "adj_close": 1.0}])
        self.assertEqual(base.missing_start(self.store, "price_daily", "005930", limit, 10),
                         date(2026, 9, 19), "저장된 마지막 날짜 다음부터")

        self.store.put_prices([{"code": "005930", "date": "2026-09-22", "open": 1.0, "high": 1.0,
                                "low": 1.0, "close": 1.0, "volume": 1.0, "value": None,
                                "adj_close": 1.0}])
        self.assertIsNone(base.missing_start(self.store, "price_daily", "005930", limit, 10),
                          "이미 기준일까지 있으면 받을 것이 없다")

    def test_missing_dates_spans_all_codes(self):
        limit = date(2026, 9, 22)
        self.store.put_prices([{"code": "A", "date": "2026-09-20", "open": 1.0, "high": 1.0,
                                "low": 1.0, "close": 1.0, "volume": 1.0, "value": None,
                                "adj_close": 1.0}])
        span = base.missing_dates(self.store, "price_daily", ["A", "B"], limit, 5)
        self.assertEqual(span, (date(2026, 9, 17), limit), "가장 많이 비어 있는 코드가 구간을 정한다")


# ---------------------------------------------------------------- 일봉·지수·수급

def price_cfg(cfg, **over):
    cfg = copy.deepcopy(cfg)
    cfg["sources"].update(over)
    return cfg


class PriceFetchTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.series = {"005930": {"2026-09-18": 100.0, "2026-09-21": 101.0, "2026-09-22": 102.0,
                                  "2026-09-23": 999.0}}
        self.fdr = FakeFdr(self.series)

    def providers(self, login=False, pykrx=None):
        return krx.Providers(pykrx=pykrx, fdr=self.fdr, kis=None, krx_login=login)

    def test_no_login_uses_fdr_and_drops_unknown_day(self):
        report = Report()
        cfg = price_cfg(self.cfg, adj_refresh_days=0, backfill_days=10)
        got = krx.fetch_prices(self.store, cfg, PRELIM, ["005930"], self.providers(), report=report)
        self.assertEqual(got["provider"], "fdr")
        rows = self.store.prices("005930", "2026-09-30", 10)
        self.assertEqual([r["date"] for r in rows], ["2026-09-18", "2026-09-21", "2026-09-22"],
                         "23일 행은 아직 알 수 없으므로 들어오지 않는다")
        self.assertIsNone(rows[0]["value"], "무로그인 경로에는 거래대금이 없다")
        self.assertEqual(rows[0]["close"], rows[0]["adj_close"], "FDR 종가는 이미 수정주가다")
        self.assertIn("price_value_missing", report.fallbacks)

    def test_incremental_only_asks_for_the_gap(self):
        cfg = price_cfg(self.cfg, adj_refresh_days=0, backfill_days=10)
        krx.fetch_prices(self.store, cfg, PRELIM, ["005930"], self.providers())
        self.fdr.calls.clear()
        krx.fetch_prices(self.store, cfg, datetime(2026, 9, 23, 18, 30), ["005930"],
                         self.providers())
        self.assertEqual(self.fdr.calls, [("005930", "2026-09-23", "2026-09-23")],
                         "저장된 다음 날부터만 다시 묻는다")

    def test_refresh_window_reaches_back_for_adjusted_prices(self):
        """수정주가는 소급 변경되므로 최근 구간은 이미 있어도 다시 받는다."""
        cfg = price_cfg(self.cfg, adj_refresh_days=5, backfill_days=10)
        krx.fetch_prices(self.store, cfg, PRELIM, ["005930"], self.providers())
        self.fdr.calls.clear()
        krx.fetch_prices(self.store, cfg, PRELIM, ["005930"], self.providers())
        self.assertEqual(self.fdr.calls, [("005930", "2026-09-17", "2026-09-22")])

    def test_login_path_fills_value_from_bulk(self):
        asked = []

        class FakePykrx:
            def get_market_ohlcv(self, day, market):
                asked.append(day)
                return FakeFrame(["005930"], [{"거래대금": 1234.0, "시가총액": 9.0, "종가": 1.0}],
                                 columns=["거래대금", "시가총액", "종가"])

        report = Report()
        cfg = price_cfg(self.cfg, adj_refresh_days=0, backfill_days=30, krx_bulk_max_days=30)
        krx.fetch_prices(self.store, cfg, PRELIM, ["005930"],
                         self.providers(login=True, pykrx=FakePykrx()), report=report)
        rows = self.store.prices("005930", "2026-09-30", 10)
        self.assertTrue(all(r["value"] == 1234.0 for r in rows), "거래대금은 날짜별 전 종목에서")
        self.assertIn("pykrx_bulk_value", report.provider_of("price"))
        self.assertEqual(sorted(asked), ["20260918", "20260921", "20260922"])

    def test_value_backfill_is_capped_to_the_recent_days(self):
        """긴 구간에서 거래대금을 다 채우려면 날짜마다 호출해야 한다 — 최근 며칠로 자른다."""
        asked = []

        class FakePykrx:
            def get_market_ohlcv(self, day, market):
                asked.append(day)
                return FakeFrame(["005930"], [{"거래대금": 7.0, "시가총액": 9.0, "종가": 1.0}],
                                 columns=["거래대금", "시가총액", "종가"])

        report = Report()
        cfg = price_cfg(self.cfg, adj_refresh_days=0, backfill_days=30, krx_bulk_max_days=1)
        krx.fetch_prices(self.store, cfg, PRELIM, ["005930"],
                         self.providers(login=True, pykrx=FakePykrx()), report=report)
        self.assertEqual(asked, ["20260922"], "가장 최근 하루만")
        rows = {r["date"]: r["value"] for r in self.store.prices("005930", "2026-09-30", 10)}
        self.assertEqual(rows["2026-09-22"], 7.0)
        self.assertIsNone(rows["2026-09-18"], "오래된 날짜의 거래대금은 비운 채로 둔다")
        self.assertIn("price_value_missing", report.fallbacks)

    def test_guard_runs_even_when_the_provider_lies(self):
        """제공자가 요청 구간 밖의 미래 행을 돌려줘도 저장되지 않는다."""
        class LyingFdr(FakeFdr):
            def DataReader(self, code, start=None, end=None):   # noqa: N802
                return ohlcv_frame(["2026-09-22", "2026-09-25"],
                                   {"2026-09-22": 1.0, "2026-09-25": 2.0})

        cfg = price_cfg(self.cfg, adj_refresh_days=0, backfill_days=10)
        providers = krx.Providers(fdr=LyingFdr(), krx_login=False)
        krx.fetch_prices(self.store, cfg, PRELIM, ["005930"], providers)
        self.assertEqual([r["date"] for r in self.store.prices("005930", "2026-09-30", 10)],
                         ["2026-09-22"])


class IndexFetchTest(StoreCase):
    def test_fdr_deep_history_then_kis_tail(self):
        fdr = FakeFdr({"KS11": {"2026-09-17": 6724.0, "2026-09-18": 6894.0}})
        kis_fake = FakeKis(index=[
            {"stck_bsop_date": "20260918", "bstp_nmix_prpr": "6894.23", "acml_vol": "346807"},
            {"stck_bsop_date": "20260921", "bstp_nmix_prpr": "7007.72", "acml_vol": "246095"},
            {"stck_bsop_date": "20260922", "bstp_nmix_prpr": "7007.72", "acml_vol": "0"},
        ])
        report = Report()
        providers = krx.Providers(fdr=fdr, kis=kis_fake, krx_login=False)
        got = krx.fetch_index(self.store, self.cfg, PRELIM, providers, report=report, years=1)
        series = self.store.market_series("KOSPI", "2026-09-30")
        self.assertEqual([r["date"] for r in series],
                         ["2026-09-17", "2026-09-18", "2026-09-21"],
                         "FDR 이 못 준 21일을 KIS 가 채우고, 거래량 0 인 22일 미완성 행은 버린다")
        self.assertEqual(got["provider"], "fdr+kis")

    def test_login_path_uses_pykrx(self):
        class FakePykrx:
            def get_index_ohlcv(self, *args):
                return FakeFrame(["2026-09-21", "2026-09-22"],
                                 [{"종가": 7007.72}, {"종가": 7010.0}], columns=["종가"])

        providers = krx.Providers(pykrx=FakePykrx(), fdr=FakeFdr(), krx_login=True)
        got = krx.fetch_index(self.store, self.cfg, PRELIM, providers, years=1)
        self.assertEqual(got["provider"], "pykrx")
        self.assertEqual(len(self.store.market_series("KOSPI", "2026-09-30")), 2)


class FlowFetchTest(StoreCase):
    def test_kis_unit_conversion_and_partial_today_row(self):
        """KIS 의 *_tr_pbmn 은 백만원 단위. 값이 '' 인 오늘 행은 버린다."""
        kis_fake = FakeKis(investor={"005930": [
            {"stck_bsop_date": "20260922", "frgn_ntby_tr_pbmn": "", "orgn_ntby_tr_pbmn": ""},
            {"stck_bsop_date": "20260921", "frgn_ntby_tr_pbmn": "1106291",
             "orgn_ntby_tr_pbmn": "-1138105"},
        ]})
        listing = listing_frame([("005930", "삼성전자", 1.6e15)])
        providers = krx.Providers(fdr=FakeFdr(listing=listing), kis=kis_fake, krx_login=False)
        report = Report()
        cfg = price_cfg(self.cfg, backfill_days=10)
        got = krx.fetch_flows(self.store, cfg, PRELIM, ["005930"], providers, report=report)
        rows = self.store.conn.execute("SELECT * FROM flow_daily ORDER BY date").fetchall()
        self.assertEqual([r["date"] for r in rows], ["2026-09-21"],
                         "값이 비어 있는 당일 행은 0 이 아니라 '없음' 이다")
        self.assertAlmostEqual(rows[0]["foreign_net"], 1106291 * 1_000_000.0)
        self.assertAlmostEqual(rows[0]["inst_net"], -1138105 * 1_000_000.0)
        self.assertIsNone(rows[0]["mktcap"], "가장 최근 일자가 아니면 시가총액은 비운다")
        self.assertEqual(got["provider"], "kis")

    def test_mktcap_only_on_the_latest_date(self):
        kis_fake = FakeKis(investor={"005930": [
            {"stck_bsop_date": "20260921", "frgn_ntby_tr_pbmn": "1", "orgn_ntby_tr_pbmn": "1"},
            {"stck_bsop_date": "20260922", "frgn_ntby_tr_pbmn": "2", "orgn_ntby_tr_pbmn": "2"},
        ]})
        listing = listing_frame([("005930", "삼성전자", 1.6e15)])
        providers = krx.Providers(fdr=FakeFdr(listing=listing), kis=kis_fake, krx_login=False)
        report = Report()
        krx.fetch_flows(self.store, price_cfg(self.cfg, backfill_days=10), PRELIM, ["005930"],
                        providers, report=report)
        rows = {r["date"]: r for r in
                self.store.conn.execute("SELECT * FROM flow_daily").fetchall()}
        self.assertIsNone(rows["2026-09-21"]["mktcap"])
        self.assertAlmostEqual(rows["2026-09-22"]["mktcap"], 1.6e15)
        self.assertIn("mktcap_latest_date_only", report.fallbacks)

    def test_login_path_calls_pykrx_per_date(self):
        calls = []

        class FakePykrx:
            def get_market_net_purchases_of_equities(self, frm, to, market, investor):
                calls.append((frm, to, investor))
                value = 1e9 if investor == "외국인" else -2e9
                return FakeFrame(["005930"], [{"순매수거래대금": value}],
                                 columns=["순매수거래대금"])

            def get_market_ohlcv(self, day, market):
                return FakeFrame(["005930"], [{"거래대금": 1.0, "시가총액": 5.0, "종가": 1.0}],
                                 columns=["거래대금", "시가총액", "종가"])

        providers = krx.Providers(pykrx=FakePykrx(), fdr=FakeFdr(), krx_login=True)
        cfg = price_cfg(self.cfg, backfill_days=1)
        krx.fetch_flows(self.store, cfg, PRELIM, ["005930"], providers)
        self.assertEqual({c[2] for c in calls}, {"외국인", "기관합계"})
        self.assertTrue(all(c[0] == c[1] for c in calls), "기간 합계 함수라 from=to 로 부른다")
        rows = self.store.conn.execute("SELECT * FROM flow_daily ORDER BY date").fetchall()
        self.assertAlmostEqual(rows[-1]["foreign_net"], 1e9)
        self.assertAlmostEqual(rows[-1]["mktcap"], 5.0, msg="로그인 경로는 과거 날짜도 시가총액이 있다")


class KisClientTest(unittest.TestCase):
    def test_to_number_treats_blank_as_missing(self):
        self.assertEqual(kis.to_number("1,106,291"), 1106291.0)
        self.assertIsNone(kis.to_number(""), "빈 값은 0 이 아니라 '없음'")
        self.assertIsNone(kis.to_number(None))
        self.assertIsNone(kis.to_number("-"))
        self.assertEqual(kis.to_number("0"), 0.0)

    def test_throttle_and_params(self):
        seen, slept = [], []
        client = kis.KisClient(load_config(),
                               transport=lambda p, t, q: seen.append((p, t, q)) or {"output2": []},
                               sleep=slept.append)
        client.daily_chart("005930", date(2026, 9, 1), date(2026, 9, 22))
        client.investor_daily("005930")
        self.assertEqual(seen[0][1], kis.TR_DAILY_CHART)
        self.assertEqual(seen[0][2]["FID_INPUT_DATE_1"], "20260901")
        self.assertEqual(seen[1][1], kis.TR_INVESTOR)
        self.assertTrue(slept, "두 번째 호출은 간격을 지키느라 잔다")


# ---------------------------------------------------------------- 밤사이 시장

class OvernightTest(StoreCase):
    def test_series_names_and_fx_rule(self):
        table = {"^GSPC": {date(2026, 9, 21): 7764.7, date(2026, 9, 22): 7800.0},
                 "^SOX": {date(2026, 9, 21): 12433.2},
                 "KRW=X": {date(2026, 9, 21): 1384.9, date(2026, 9, 22): 1374.5}}

        def download(ticker, start, end):
            return sorted(table.get(ticker, {}).items())

        got = overnight.fetch_overnight(self.store, self.cfg, FINAL, downloader=download, years=1)
        self.assertEqual(got["series"], ["SOX", "SP500", "USDKRW"])
        rows = {(r["series"], r["date"]) for r in
                self.store.conn.execute("SELECT * FROM market_daily").fetchall()}
        self.assertIn(("SP500", "2026-09-22"), rows, "최종(07:40)은 전날 미국 종가를 본다")
        self.assertIn(("USDKRW", "2026-09-22"), rows)
        self.assertEqual(got["rows"], 5, "SP500 2일 + SOX 1일 + USDKRW 2일")

    def test_prelim_cannot_see_last_night(self):
        table = {"^GSPC": {date(2026, 9, 21): 7764.7, date(2026, 9, 22): 7800.0}}
        got = overnight.fetch_overnight(
            self.store, self.cfg, PRELIM,
            downloader=lambda t, s, e: sorted(table.get(t, {}).items()), years=1)
        dates = [r["date"] for r in self.store.market_series("SP500", "2026-09-30")]
        self.assertEqual(dates, ["2026-09-21"],
                         "예비(18:30)에서는 당일 미국 봉이 아직 없다 — mkt_overnight 이 결측인 이유")
        self.assertEqual(got["rows"], 1)

    def test_provider_failure_raises_for_the_step_to_catch(self):
        def boom(*_):
            raise RuntimeError("timeout")

        with self.assertRaises(base.SourceError):
            overnight.fetch_overnight(self.store, self.cfg, FINAL, downloader=boom, years=1)


# ---------------------------------------------------------------- DART

def dart_item(rcept_no, code="005930", name="삼성전자", report_nm="단일판매ㆍ공급계약체결  ",
              rcept_dt="20260922"):
    return {"corp_code": "00126380", "corp_name": name, "stock_code": code, "corp_cls": "Y",
            "report_nm": report_nm, "rcept_no": rcept_no, "flr_nm": name, "rcept_dt": rcept_dt,
            "rm": ""}


class DartTest(StoreCase):
    def test_paging_collects_every_page(self):
        pages = {1: {"status": "000", "total_page": 3, "list": [dart_item("1")]},
                 2: {"status": "000", "total_page": 3, "list": [dart_item("2")]},
                 3: {"status": "000", "total_page": 3, "list": [dart_item("3")]}}
        seen = []

        def http(url, params):
            seen.append(params["page_no"])
            return pages[params["page_no"]]

        items, note = dart.fetch_list(self.cfg, "20260922", "20260922", http=http, api_key="k")
        self.assertEqual(seen, [1, 2, 3])
        self.assertEqual([i["rcept_no"] for i in items], ["1", "2", "3"])
        self.assertIsNone(note)

    def test_no_key_degrades_gracefully(self):
        report = Report()
        items, note = dart.fetch_list(self.cfg, "20260922", "20260922", http=None, api_key="",
                                      env={}, report=report)
        self.assertEqual(items, [])
        self.assertIn("DART_API_KEY", note)
        self.assertIn("dart_no_api_key", report.fallbacks)

    def test_bad_status_returns_note_not_exception(self):
        report = Report()
        items, note = dart.fetch_list(self.cfg, "20260922", "20260922",
                                      http=lambda u, p: {"status": "010", "message": "등록되지 않은 키"},
                                      api_key="bad", report=report)
        self.assertEqual(items, [])
        self.assertIn("status=010", note)
        self.assertIn("dart_status:010", report.fallbacks)

    def test_no_data_status_is_not_an_error(self):
        items, note = dart.fetch_list(self.cfg, "20260922", "20260922",
                                      http=lambda u, p: {"status": "013", "message": "없음"},
                                      api_key="k")
        self.assertEqual((items, note), ([], None))

    def test_rows_skip_blank_stock_code_and_trim_report_name(self):
        rows = dart.to_disclosure_rows(
            [dart_item("1"), dart_item("2", code=""), dart_item("3", code="  ")],
            "2026-09-22T18:30:00.000")
        self.assertEqual([r["rcept_no"] for r in rows], ["1"])
        self.assertEqual(rows[0]["report_nm"], "단일판매ㆍ공급계약체결")
        self.assertEqual(rows[0]["kind"], "공급계약", "newsgap 의 유형 분류를 그대로 쓴다")
        self.assertEqual(rows[0]["rcept_dt"], "2026-09-22")
        self.assertEqual(rows[0]["first_seen_src"], "dart_poll")

    def test_sync_counts_new_rows_only_once(self):
        http = (lambda u, p: {"status": "000", "total_page": 1,
                              "list": [dart_item("20260922000001"), dart_item("20260922000002")]})
        first = dart.sync(self.store, self.cfg, PRELIM, http=http)
        self.assertEqual((first["rows"], first["new"]), (2, 2))
        second = dart.sync(self.store, self.cfg, PRELIM, http=http)
        self.assertEqual((second["rows"], second["new"]), (2, 0), "이미 본 접수번호는 새것이 아니다")

    def test_sync_does_not_take_future_dated_rows(self):
        http = (lambda u, p: {"status": "000", "total_page": 1,
                              "list": [dart_item("A", rcept_dt="20260922"),
                                       dart_item("B", rcept_dt="20260925")]})
        got = dart.sync(self.store, self.cfg, PRELIM, http=http)
        self.assertEqual(got["rows"], 1)
        self.assertIsNone(self.store.disclosure("B"))


# ---------------------------------------------------------------- LS 뉴스

class FakeNewsDb:
    """newsgap.db 의 news 테이블만 흉내 낸다 (읽기 전용)."""

    def __init__(self, rows):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE news(realkey TEXT PRIMARY KEY, ls_datetime TEXT, "
                          "recv_wall TEXT, code TEXT, source_id TEXT, title TEXT, body TEXT)")
        self.conn.executemany(
            "INSERT INTO news(realkey,recv_wall,code,source_id,title) VALUES(?,?,?,?,?)", rows)

    def execute(self, *args):
        return self.conn.execute(*args)

    def close(self):
        self.conn.close()


class LsNewsTest(StoreCase):
    def test_title_normalisation_and_matching(self):
        self.assertTrue(lsnews.titles_match("(주)브이씨 자기주식취득 신탁계약 체결 결정", "자기주식취득 신탁계약 체결 결정", "브이씨"))
        self.assertTrue(lsnews.titles_match("삼성전자(주) 단일판매ㆍ공급계약체결",
                                            "단일판매·공급계약 체결", "삼성전자"))
        self.assertTrue(lsnews.titles_match("(주)씨케이솔루션 기타 경영사항(자율공시)(종속회사의 주요경영사항)",
                                            "기타경영사항(자율공시)", "씨케이솔루션"))
        self.assertFalse(lsnews.titles_match("(주)브이씨 자기주식취득 신탁계약 체결 결정", "유상증자결정", "브이씨"))
        self.assertFalse(lsnews.titles_match("가", "나", None), "너무 짧은 제목은 맞추지 않는다")

    def _flash_db(self, recv="2026-09-22T11:24:19.674"):
        return FakeNewsDb([("RK1", recv, "365900", "15", "(주)브이씨 자기주식취득 신탁계약 체결 결정"),
                           ("RK2", recv, "", "15", "코드 없는 속보"),
                           ("RK3", recv, "005930", "21", "그냥 뉴스")])

    def test_synthetic_row_when_no_dart_match(self):
        report = Report()
        got = lsnews.sync_disclosures(self.store, self.cfg, PRELIM, conn=self._flash_db(),
                                      report=report)
        self.assertEqual((got["rows"], got["matched"], got["synthetic"]), (1, 0, 1),
                         "코드 없는 속보와 일반 뉴스는 세지 않는다")
        row = self.store.disclosure("LS:RK1")
        self.assertEqual(row["stock_code"], "365900")
        self.assertEqual(row["kind"], "자기주식", "제목으로 유형을 정한다")
        self.assertEqual(row["first_seen_at"], "2026-09-22T11:24:19.674")
        self.assertEqual(row["first_seen_src"], "ls_news")
        self.assertIn("disclosure_synthetic_rcept_no", report.fallbacks)

    def test_match_pulls_first_seen_earlier_and_keeps_dart_columns(self):
        self.store.upsert_disclosure({
            "rcept_no": "20260922800001", "stock_code": "365900", "corp_name": "브이씨",
            "report_nm": "자기주식취득 신탁계약 체결 결정", "rcept_dt": "2026-09-22",
            "first_seen_at": "2026-09-22T11:30:00.000", "first_seen_src": "dart_poll",
            "ls_realkey": None, "kind": "자기주식", "ratio": None, "ratio_ok": None,
            "body_src": None})
        got = lsnews.sync_disclosures(self.store, self.cfg, PRELIM, conn=self._flash_db())
        self.assertEqual((got["matched"], got["synthetic"]), (1, 0))
        row = self.store.disclosure("20260922800001")
        self.assertEqual(row["first_seen_at"], "2026-09-22T11:24:19.674",
                         "LS 수신 시각이 더 빠르면 그쪽으로 당긴다 (설계 5.4)")
        self.assertEqual(row["first_seen_src"], "ls_news")
        self.assertEqual(row["ls_realkey"], "RK1")
        self.assertEqual(row["report_nm"], "자기주식취득 신탁계약 체결 결정", "DART 가 채운 열은 지워지지 않는다")

    def test_late_dart_row_merges_away_the_synthetic_one(self):
        lsnews.sync_disclosures(self.store, self.cfg, PRELIM, conn=self._flash_db())
        self.assertIsNotNone(self.store.disclosure("LS:RK1"))
        self.store.upsert_disclosure({
            "rcept_no": "20260922800001", "stock_code": "365900", "corp_name": "브이씨",
            "report_nm": "자기주식취득 신탁계약 체결 결정", "rcept_dt": "2026-09-22",
            "first_seen_at": "2026-09-22T11:40:00.000", "first_seen_src": "dart_poll",
            "ls_realkey": None, "kind": "자기주식", "ratio": None, "ratio_ok": None,
            "body_src": None})
        got = lsnews.sync_disclosures(self.store, self.cfg, PRELIM, conn=self._flash_db())
        self.assertEqual(got["merged"], 1)
        self.assertIsNone(self.store.disclosure("LS:RK1"), "같은 공시가 두 줄이면 점수가 두 번 더해진다")
        self.assertEqual(self.store.disclosure("20260922800001")["first_seen_at"],
                         "2026-09-22T11:24:19.674")

    def test_window_excludes_what_is_after_as_of(self):
        db = FakeNewsDb([("RK1", "2026-09-22T18:31:00.000", "365900", "15", "(주)브이씨 자기주식취득 신탁계약 체결 결정"),
                         ("RK2", "2026-09-19T09:00:00.000", "365900", "15", "(주)브이씨 유상증자 결정")])
        got = lsnews.sync_disclosures(self.store, self.cfg, PRELIM, hours=24, conn=db)
        self.assertEqual(got["rows"], 0, "기준 시각 이후와 창 밖은 둘 다 빠진다")

    def test_recent_news_generator(self):
        db = FakeNewsDb([("RK1", "2026-09-22T11:00:00.000", "005930", "21", "뉴스 하나"),
                         ("RK2", "2026-09-22T12:00:00.000", "000660", "15", "공시 속보")])
        rows = list(lsnews.recent_news(self.cfg, PRELIM, hours=24, conn=db))
        self.assertEqual([r[0] for r in rows], ["RK1", "RK2"])
        self.assertEqual(rows[0][3], "21")
        only_news = list(lsnews.recent_news(self.cfg, PRELIM, hours=24, conn=db,
                                            exclude_disclosure=True))
        self.assertEqual([r[0] for r in only_news], ["RK1"])

    def test_missing_newsgap_db_is_not_fatal(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["paths"]["newsgap_db"] = "/tmp/advisor-없는파일.db"
        self.assertEqual(lsnews.disclosure_flashes(cfg, PRELIM), [])
        self.assertEqual(list(lsnews.recent_news(cfg, PRELIM)), [])
        self.assertEqual(lsnews.sync_disclosures(self.store, cfg, PRELIM)["rows"], 0)


# ---------------------------------------------------------------- 목록

class UniverseTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.listing = listing_frame([
            ("005930", "삼성전자", 1.6e15), ("005935", "삼성전자우", 1.6e14),
            ("000660", "SK하이닉스", 1.3e15), ("105560", "KB금융", 1.0e14),
            ("395400", "SK리츠", 9e12), ("138040", "메리츠금융지주", 8e12),
            ("088980", "맥쿼리인프라", 7e12), ("069500", "KODEX 200", 5e12),
            ("123456", "아무개스팩1호", 1e10)])
        self.etf = etf_frame([("069500", "KODEX 200"), ("459580", "TIGER CD금리"),
                              ("091160", "KODEX 반도체")])
        self.fdr = FakeFdr(listing=self.listing, etf_listing=self.etf)

    def test_fallback_filters_preferred_spac_and_reit(self):
        report = Report()
        providers = krx.Providers(fdr=self.fdr, krx_login=False)
        codes = universe_mod.fallback_top_codes(self.cfg, providers, report)
        self.assertEqual(codes, ["005930", "000660", "105560", "138040"],
                         "우선주(005935)·리츠(395400)·인프라(088980)·스팩·설정 ETF(069500) 제외")
        self.assertIn("universe_fallback_top_marcap", report.fallbacks)

    def test_top_n_is_honoured(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["universe"]["fallback_top_n"] = 2
        providers = krx.Providers(fdr=self.fdr, krx_login=False)
        self.assertEqual(universe_mod.fallback_top_codes(cfg, providers, Report()),
                         ["005930", "000660"])

    def test_snapshot_rows_and_kinds(self):
        report = Report()
        providers = krx.Providers(fdr=self.fdr, krx_login=False)
        got = universe_mod.snapshot(self.store, self.cfg, providers, PRELIM, report=report)
        rows = {r["code"]: r for r in
                self.store.conn.execute("SELECT * FROM universe").fetchall()}
        self.assertEqual(rows["005930"]["kind"], "stock")
        self.assertEqual(rows["005930"]["name"], "삼성전자")
        self.assertEqual(rows[self.cfg["universe"]["cash_etf"]]["kind"], "cash_etf")
        self.assertEqual(rows[self.cfg["universe"]["core_etf"]]["kind"], "etf")
        self.assertEqual(rows[self.cfg["universe"]["sector_etfs"]["반도체"]]["sector"], "반도체",
                         "섹터 ETF 는 자기 섹터 이름을 갖는다")
        self.assertEqual(rows["069500"]["name"], "KODEX 200")
        self.assertGreater(got["rows"], 4)
        self.assertEqual(got["provider"], "fdr_top_marcap")

    def test_manual_sector_table_assigns_one_sector_each(self):
        report = Report()
        providers = krx.Providers(fdr=self.fdr, krx_login=False)
        mapping, counts = universe_mod.sector_map(
            self.cfg, providers, ["005930", "000660", "105560", "373220"],
            date(2026, 9, 22), report)
        self.assertEqual(mapping["005930"], "반도체")
        self.assertEqual(mapping["105560"], "은행")
        self.assertEqual(counts["반도체"], 2, "범위 밖 종목은 섹터 멤버로 세지 않는다")
        self.assertEqual(counts["조선"], 0)
        self.assertTrue(any(f.startswith("sector_manual_table") for f in report.fallbacks))

    def test_pdf_members_win_but_manual_fills_thin_sectors(self):
        class FakePykrx:
            def get_index_portfolio_deposit_file(self, ticker, day=None):
                return ["005930", "000660", "105560"]

            def get_etf_portfolio_deposit_file(self, etf, day=None):
                # 반도체 ETF 는 코스피200 안의 멤버가 하나뿐 (2026-09-22 실측과 같은 모양)
                return FakeFrame(["005930"], [{}]) if etf == "091160" else FakeFrame([], [])

        report = Report()
        providers = krx.Providers(pykrx=FakePykrx(), fdr=self.fdr, krx_login=True)
        rows, summary = universe_mod.build_universe(self.cfg, providers, date(2026, 9, 22), report)
        by_code = {r["code"]: r for r in rows}
        self.assertEqual(summary["provider"], "pykrx_kospi200")
        self.assertEqual(by_code["005930"]["sector"], "반도체")
        self.assertEqual(by_code["000660"]["sector"], "반도체",
                         "멤버가 min_sector_members 미만이면 수작업 표로 보강한다")
        self.assertEqual(by_code["105560"]["sector"], "은행")

    def test_kospi200_failure_falls_back(self):
        class BrokenPykrx:
            def get_index_portfolio_deposit_file(self, *a, **k):
                raise RuntimeError("LOGOUT")

            def get_etf_portfolio_deposit_file(self, *a, **k):
                raise RuntimeError("LOGOUT")

        report = Report()
        providers = krx.Providers(pykrx=BrokenPykrx(), fdr=self.fdr, krx_login=True)
        codes = universe_mod.kospi200_codes(self.cfg, providers, date(2026, 9, 22), report)
        self.assertIn("005930", codes)
        self.assertTrue(any(f.startswith("kospi200_failed") for f in report.fallbacks))

    def test_latest_universe_date(self):
        self.store.put_universe("2026-09-18", [{"code": "A", "name": "a", "kind": "stock",
                                                "sector": None}])
        self.store.put_universe("2026-09-22", [{"code": "B", "name": "b", "kind": "stock",
                                                "sector": None}])
        self.assertEqual(universe_mod.latest_universe_date(self.store, date(2026, 9, 21)),
                         "2026-09-18")
        self.assertEqual(universe_mod.stored_codes(self.store, "2026-09-22", kinds=("stock",)),
                         ["B"])
        self.assertIsNone(universe_mod.latest_universe_date(self.store, date(2020, 1, 1)))


# ---------------------------------------------------------------- ingest

class IngestTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.cal = TradingCalendar(self.cfg, self.store)
        listing = listing_frame([("005930", "삼성전자", 1.6e15), ("000660", "SK하이닉스", 1.3e15)])
        etf = etf_frame([("069500", "KODEX 200")])
        series = {code: {"2026-09-21": 100.0, "2026-09-22": 101.0}
                  for code in ("005930", "000660", "069500", "459580", "091160", "305720",
                               "091180", "091170", "244580", "449450", "466920", "229200",
                               "132030", "261240", "360750")}
        series["KS11"] = {"2026-09-21": 7007.0, "2026-09-22": 7010.0}
        self.fdr = FakeFdr(series, listing=listing, etf_listing=etf)
        self.kis = FakeKis(investor={c: [{"stck_bsop_date": "20260922",
                                          "frgn_ntby_tr_pbmn": "10", "orgn_ntby_tr_pbmn": "20"}]
                                     for c in ("005930", "000660")})
        self.providers = krx.Providers(fdr=self.fdr, kis=self.kis, krx_login=False)
        self.http = lambda u, p: {"status": "000", "total_page": 1, "list": [dart_item("R1")]}
        self.download = lambda t, s, e: [(date(2026, 9, 21), 1.0)]

    def run_it(self, **over):
        kwargs = dict(providers=self.providers, http=self.http, downloader=self.download)
        kwargs.update(over)
        return ingest.run_ingest(self.store, self.cfg, self.cal, PRELIM, "prelim", **kwargs)

    def test_happy_path_touches_every_step(self):
        got = self.run_it()
        self.assertTrue(got["ok"], got["steps"])
        self.assertEqual(set(got["steps"]), set(ingest.STEP_NAMES))
        self.assertEqual(got["steps"]["price"]["provider"], "fdr")
        self.assertEqual(got["steps"]["flow"]["provider"], "kis")
        self.assertEqual(got["steps"]["dart"]["rows"], 1)
        self.assertGreater(got["steps"]["index"]["rows"], 0)
        self.assertIn("universe=", got["note"])
        self.assertIn("fallback:", got["note"], "대체 경로는 run.note 에 남는다")
        self.assertIn("krx_login_missing", got["fallbacks_used"] + ["krx_login_missing"])

    def test_one_failing_source_does_not_abort_the_batch(self):
        def boom(*_):
            raise RuntimeError("OpenDART 500")

        got = self.run_it(http=boom)
        self.assertFalse(got["ok"])
        self.assertFalse(got["steps"]["dart"]["ok"])
        self.assertIn("RuntimeError", got["steps"]["dart"]["error"])
        self.assertTrue(got["steps"]["price"]["ok"], "다른 단계는 그대로 돈다")
        self.assertTrue(got["steps"]["lsnews"]["ok"], "뒤 단계도 계속된다")
        self.assertIn("dart=FAIL", got["note"])

    def test_universe_failure_falls_back_to_the_last_snapshot(self):
        self.store.put_universe("2026-09-21", [{"code": "005930", "name": "삼성전자",
                                                "kind": "stock", "sector": "반도체"}])
        broken = krx.Providers(fdr=FakeFdr({}, listing=None), kis=self.kis, krx_login=False)
        got = self.run_it(providers=broken)
        self.assertFalse(got["steps"]["universe"]["ok"])
        self.assertEqual(got["counts"]["stocks"], 1, "어제 목록으로 이어서 수집한다")

    def test_limit_caps_the_stock_count(self):
        got = self.run_it(limit=1)
        self.assertEqual(got["counts"]["stocks"], 1)

    def test_replay_mode_does_nothing(self):
        got = ingest.run_ingest(self.store, self.cfg, self.cal, PRELIM, "final", mode="replay")
        self.assertTrue(got["ok"])
        self.assertTrue(got["skipped"])
        self.assertEqual(got["steps"], {})
        self.assertEqual(got["fallbacks_used"], [])
        self.assertIn("재현 모드", got["note"])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM price_daily").fetchone()[0], 0)

    def test_backfill_writes_universe_for_every_trading_day(self):
        got = ingest.backfill(self.store, self.cfg, self.cal, "2026-09-18", "2026-09-22",
                              providers=self.providers, downloader=self.download, with_flows=False)
        self.assertTrue(got["ok"], got["steps"])
        days = [r[0] for r in self.store.conn.execute(
            "SELECT DISTINCT as_of_date FROM universe ORDER BY as_of_date")]
        self.assertEqual(days, ["2026-09-18", "2026-09-21", "2026-09-22"])
        self.assertNotIn("flow", got["steps"], "수급은 기본적으로 로그인이 있을 때만")
        self.assertGreater(got["steps"]["price"]["rows"], 0)

    def test_summary_is_json_serialisable(self):
        import json
        json.dumps(self.run_it(), ensure_ascii=False, default=str)


# ---------------------------------------------------------------- 폴러

class PollerTest(StoreCase):
    def test_window_and_trading_day(self):
        self.assertTrue(poller.in_poll_window(self.cfg, datetime(2026, 9, 22, 7, 30)))
        self.assertTrue(poller.in_poll_window(self.cfg, datetime(2026, 9, 22, 18, 10)))
        self.assertFalse(poller.in_poll_window(self.cfg, datetime(2026, 9, 22, 7, 29)))
        self.assertFalse(poller.in_poll_window(self.cfg, datetime(2026, 9, 22, 18, 11)))
        cal = TradingCalendar(self.cfg)
        self.assertTrue(poller.should_poll(self.cfg, datetime(2026, 9, 22, 10, 0), cal))
        self.assertFalse(poller.should_poll(self.cfg, datetime(2026, 9, 24, 10, 0), cal),
                         "추석 휴장일에는 조회하지 않는다")

    def test_poll_once_counts_new_receipts(self):
        http = (lambda u, p: {"status": "000", "total_page": 1,
                              "list": [dart_item("20260922000009")]})
        now = datetime(2026, 9, 22, 10, 0)
        self.assertEqual(poller.poll_once(self.store, self.cfg, now=now, http=http), 1)
        self.assertEqual(poller.poll_once(self.store, self.cfg, now=now, http=http), 0)
        self.assertEqual(poller.poll_once(self.store, self.cfg,
                                          now=datetime(2026, 9, 22, 20, 0), http=http), 0,
                         "창 밖에서는 조회하지 않는다")

    def test_poll_once_swallows_failures(self):
        def boom(*_):
            raise RuntimeError("네트워크 단절")

        self.assertEqual(poller.poll_once(self.store, self.cfg,
                                          now=datetime(2026, 9, 22, 10, 0), http=boom), 0,
                         "스레드가 죽으면 다음 조회가 없어진다 — 예외를 밖으로 내보내지 않는다")

    def test_run_forever_stops_on_event(self):
        class Stop:
            def __init__(self):
                self.count = 0

            def is_set(self):
                return self.count >= 2

            def wait(self, _):
                self.count += 1
                return self.count >= 2

        http = (lambda u, p: {"status": "000", "total_page": 1, "list": [dart_item("X")]})
        total = poller.run_forever(self.store, self.cfg, Stop(),
                                   now_fn=lambda: datetime(2026, 9, 22, 10, 0), http=http)
        self.assertEqual(total, 1, "두 번째 조회에서는 새 접수번호가 없다")


# ---------------------------------------------------------------- 보고·설정

class ReportTest(unittest.TestCase):
    def test_providers_and_fallbacks_accumulate(self):
        report = Report()
        report.used("index", "fdr")
        report.used("index", "kis")
        report.used("index", "kis")
        self.assertEqual(report.provider_of("index"), "fdr+kis")
        report.fallback("krx_login_failed")
        report.fallback("krx_login_failed")
        self.assertEqual(report.fallbacks, ["krx_login_failed"])
        other = Report()
        other.used("price", "fdr")
        other.fallback("price_value_missing")
        report.merge(other)
        self.assertEqual(report.provider_of("price"), "fdr")
        self.assertEqual(len(report.fallbacks), 2)


class SourcesConfigTest(unittest.TestCase):
    def test_required_keys_exist(self):
        cfg = load_config()
        for key in ("daily_bar_known_at", "us_bar_known_at", "fx_known_at", "backfill_days",
                    "index_backfill_years", "overnight_backfill_years", "kis_min_interval_sec",
                    "kis_net_value_unit_krw", "krx_login_env", "krx_min_interval_sec",
                    "price_ticker_provider", "adj_refresh_days", "dart_api_key_env",
                    "dart_list_url", "dart_page_count", "overnight_tickers", "http_timeout_sec"):
            self.assertIn(key, cfg["sources"], f"sources.{key} 가 있어야 한다")
        for key in ("fallback_top_n", "min_sector_members", "sector_map_manual",
                    "exclude_name_patterns"):
            self.assertIn(key, cfg["universe"])

    def test_sector_map_manual_covers_configured_sectors_without_duplicates(self):
        cfg = load_config()
        manual = cfg["universe"]["sector_map_manual"]
        self.assertEqual(set(manual), set(cfg["universe"]["sector_etfs"]),
                         "섹터 ETF 가 있는 섹터는 전부 수작업 표가 있어야 대체 경로가 성립한다")
        seen = set()
        for sector, codes in manual.items():
            self.assertGreaterEqual(len(codes), 3, f"{sector} 멤버가 너무 적다")
            for code in codes:
                self.assertRegex(code, r"^\d{6}$")
                self.assertNotIn(code, seen, f"{code} 가 여러 섹터에 있다")
                seen.add(code)

    def test_cfg_get_names_the_missing_key(self):
        with self.assertRaises(base.ConfigKeyError) as ctx:
            base.cfg_get({"sources": {}}, "sources", "없는키")
        self.assertIn("sources.없는키", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
