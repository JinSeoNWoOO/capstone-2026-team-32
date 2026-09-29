"""체결 조회 파싱 테스트. KIS 서버로 요청을 보내지 않는다.

응답 필드명은 계좌 종류·API 개정에 따라 다를 수 있어 후보 목록으로 방어적으로 읽는다.
여기서는 그 후보들과 부분 체결·미체결·평균가 역산이 제대로 동작하는지 본다.
"""
from backend.tests import _safety  # noqa: F401  맨 먼저: 실제 주문·외부 접속 차단 (_safety.py 참고)
from backend.tests._safety import block_calls
import unittest
from unittest.mock import patch

from backend.app.services import auto_trading, kis_fill
from backend.app.services.auto_trading import AutoSettings, AutoTradingEngine
from backend.app.services.kis_read import KisReadError


def _fetch(rows):
    """get_kis_json 을 대신해 주어진 output1 을 돌려준다."""
    return lambda *a, **kw: {"rt_cd": "0", "output1": rows}


class KisFillTest(unittest.TestCase):
    def test_full_fill(self):
        rows = [{"odno": "0000012345", "pdno": "005930", "ord_qty": "10",
                 "tot_ccld_qty": "10", "avg_prvs": "71,250"}]
        with patch.object(kis_fill, "get_kis_json", _fetch(rows)), \
             patch.object(kis_fill, "get_access_token", lambda: "t"):
            fill = kis_fill.get_order_fill("12345", "005930")
        self.assertTrue(fill["found"])
        self.assertTrue(fill["filled"])
        self.assertFalse(fill["partial"])
        self.assertEqual(fill["filled_qty"], 10)
        self.assertEqual(fill["avg_price"], 71250.0, "쉼표가 있는 숫자도 읽는다")

    def test_partial_fill(self):
        rows = [{"odno": "12345", "ord_qty": "10", "tot_ccld_qty": "4", "avg_prvs": "71300"}]
        with patch.object(kis_fill, "get_kis_json", _fetch(rows)), \
             patch.object(kis_fill, "get_access_token", lambda: "t"):
            fill = kis_fill.get_order_fill("12345")
        self.assertTrue(fill["partial"])
        self.assertFalse(fill["filled"])
        self.assertEqual(fill["filled_qty"], 4)

    def test_not_filled_yet(self):
        rows = [{"odno": "12345", "ord_qty": "10", "tot_ccld_qty": "0", "avg_prvs": "0"}]
        with patch.object(kis_fill, "get_kis_json", _fetch(rows)), \
             patch.object(kis_fill, "get_access_token", lambda: "t"):
            fill = kis_fill.get_order_fill("12345")
        self.assertTrue(fill["found"])
        self.assertFalse(fill["filled"])
        self.assertFalse(fill["partial"])
        self.assertEqual(fill["filled_qty"], 0)

    def test_average_price_derived_from_amount(self):
        """평균가 필드가 비어 있으면 체결 금액을 수량으로 나눠 되돌린다."""
        rows = [{"odno": "12345", "ord_qty": "4", "tot_ccld_qty": "4",
                 "avg_prvs": "", "tot_ccld_amt": "285200"}]
        with patch.object(kis_fill, "get_kis_json", _fetch(rows)), \
             patch.object(kis_fill, "get_access_token", lambda: "t"):
            fill = kis_fill.get_order_fill("12345")
        self.assertEqual(fill["avg_price"], 71300.0)

    def test_alternate_field_names(self):
        rows = [{"ODNO": "12345", "ORD_QTY": "3", "TOT_CCLD_QTY": "3", "AVG_PRVS": "1000"}]
        with patch.object(kis_fill, "get_kis_json", _fetch(rows)), \
             patch.object(kis_fill, "get_access_token", lambda: "t"):
            fill = kis_fill.get_order_fill("12345")
        self.assertTrue(fill["filled"])
        self.assertEqual(fill["avg_price"], 1000.0)

    def test_order_not_found(self):
        rows = [{"odno": "99999", "ord_qty": "1", "tot_ccld_qty": "1", "avg_prvs": "100"}]
        with patch.object(kis_fill, "get_kis_json", _fetch(rows)), \
             patch.object(kis_fill, "get_access_token", lambda: "t"):
            fill = kis_fill.get_order_fill("12345")
        self.assertFalse(fill["found"])
        self.assertEqual(fill["filled_qty"], 0)


class AutoTradingEntryPriceTest(unittest.TestCase):
    """진입가는 조회 시점 현재가가 아니라 체결 평균가여야 한다."""

    def setUp(self):
        # 엔진의 KIS 함수는 테스트가 @patch 로 준 것만 쓴다. 나머지가 불리면 실패.
        block_calls(self, auto_trading, "place_order", "get_position", "get_order_fill",
                    "get_stock_snapshot", "get_volume_rank")

    def _engine(self):
        engine = AutoTradingEngine()
        engine._selected = {"stock_code": "005930", "stock_name": "삼성전자"}
        engine._settings = AutoSettings(quantity=10)
        engine._phase = "매수 체결 확인"
        engine._last_order = {"order_no": "12345", "submitted": True}
        engine._baseline_quantity = 0
        return engine

    @patch("backend.app.services.auto_trading.get_order_fill")
    def test_entry_price_uses_fill_average(self, get_fill):
        get_fill.return_value = {"order_qty": 10, "filled_qty": 10, "avg_price": 71250.0,
                                 "filled": True, "partial": False, "found": True}
        engine = self._engine()
        engine._confirm_buy(70000)                      # 현재가는 70,000원으로 다르게 준다
        self.assertEqual(engine._entry_price, 71250.0)
        self.assertEqual(engine._managed_quantity, 10)
        self.assertEqual(engine._phase, "포지션 관리")

    @patch("backend.app.services.auto_trading.get_order_fill")
    def test_partial_fill_manages_only_filled_quantity(self, get_fill):
        get_fill.return_value = {"order_qty": 10, "filled_qty": 4, "avg_price": 71300.0,
                                 "filled": False, "partial": True, "found": True}
        engine = self._engine()
        engine._confirm_buy(70000)
        self.assertEqual(engine._managed_quantity, 4, "체결된 수량만 관리한다")
        self.assertEqual(engine._entry_price, 71300.0)

    @patch("backend.app.services.auto_trading.get_position")
    @patch("backend.app.services.auto_trading.get_order_fill")
    def test_falls_back_to_balance_when_fill_query_fails(self, get_fill, get_position):
        get_fill.side_effect = KisReadError("조회 실패")
        get_position.return_value = {"quantity": 10}
        engine = self._engine()
        engine._confirm_buy(70000)
        self.assertEqual(engine._managed_quantity, 10)
        self.assertEqual(engine._entry_price, 70000.0, "조회가 안 되면 예전 방식으로 물러난다")


if __name__ == "__main__":
    unittest.main()
