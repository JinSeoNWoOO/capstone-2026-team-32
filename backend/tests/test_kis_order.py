from backend.tests import _safety  # noqa: F401  맨 먼저: 실제 주문·외부 접속 차단 (_safety.py 참고)
import os
import unittest
from unittest.mock import Mock, patch

from backend.app.services import kis_order


class KisOrderTests(unittest.TestCase):
    def test_dry_run_does_not_call_kis(self):
        with patch.dict(os.environ, {"KIS_ORDER_MODE": "dry-run"}), patch.object(
            kis_order.requests, "post"
        ) as post:
            result = kis_order.place_order("buy", "005930", 1)

        self.assertTrue(result["ok"])
        self.assertFalse(result["submitted"])
        self.assertEqual(result["status"], "simulated")
        self.assertEqual(result["price"], 0)
        post.assert_not_called()

    def test_paper_market_buy_uses_buy_tr_id_and_zero_price(self):
        response = Mock()
        response.json.return_value = {
            "rt_cd": "0",
            "msg1": "주문 전송 완료",
            "output": {"ODNO": "10001", "ORD_TMD": "091501"},
        }

        with (
            patch.dict(os.environ, {"KIS_ORDER_MODE": "paper"}),
            patch.multiple(
                kis_order,
                APP_KEY="app-key",
                APP_SECRET="app-secret",
                ACCOUNT_NO="12345678",
                ACCOUNT_PRODUCT_CODE="01",
            ),
            patch.object(kis_order, "get_access_token", return_value="token"),
            patch.object(kis_order, "get_hash_key", return_value="hash"),
            patch.object(kis_order.requests, "post", return_value=response) as post,
        ):
            result = kis_order.place_order("buy", "005930", 2, order_type="market")

        sent = post.call_args.kwargs
        self.assertEqual(sent["headers"]["tr_id"], "VTTC0012U")
        self.assertEqual(sent["headers"]["hashkey"], "hash")
        self.assertEqual(sent["json"]["ORD_DVSN"], "01")
        self.assertEqual(sent["json"]["ORD_QTY"], "2")
        self.assertEqual(sent["json"]["ORD_UNPR"], "0")
        self.assertTrue(result["submitted"])
        self.assertEqual(result["order_no"], "10001")

    def test_paper_limit_sell_uses_sell_tr_id_and_general_sell_type(self):
        response = Mock()
        response.json.return_value = {
            "rt_cd": "0",
            "msg1": "주문 전송 완료",
            "output": {"ODNO": "10002", "ORD_TMD": "091601"},
        }

        with (
            patch.dict(os.environ, {"KIS_ORDER_MODE": "paper"}),
            patch.multiple(
                kis_order,
                APP_KEY="app-key",
                APP_SECRET="app-secret",
                ACCOUNT_NO="12345678",
                ACCOUNT_PRODUCT_CODE="01",
            ),
            patch.object(kis_order, "get_access_token", return_value="token"),
            patch.object(kis_order, "get_hash_key", return_value="hash"),
            patch.object(kis_order.requests, "post", return_value=response) as post,
        ):
            result = kis_order.place_order(
                "sell",
                "005930",
                3,
                price=72000,
                order_type="limit",
            )

        sent = post.call_args.kwargs
        self.assertEqual(sent["headers"]["tr_id"], "VTTC0011U")
        self.assertEqual(sent["json"]["ORD_DVSN"], "00")
        self.assertEqual(sent["json"]["ORD_UNPR"], "72000")
        self.assertEqual(sent["json"]["SLL_TYPE"], "01")
        self.assertEqual(result["side"], "sell")

    def test_invalid_orders_are_rejected_before_network_call(self):
        invalid_orders = [
            ("buy", "5930", 1, 0, "market"),
            ("buy", "005930", 0, 0, "market"),
            ("sell", "005930", 1, 0, "limit"),
        ]

        for side, code, quantity, price, order_type in invalid_orders:
            with self.subTest(code=code, quantity=quantity, order_type=order_type):
                with self.assertRaises(kis_order.KisOrderValidationError):
                    kis_order.place_order(side, code, quantity, price, order_type)

    def test_real_mode_is_always_blocked(self):
        with patch.dict(os.environ, {"KIS_ORDER_MODE": "real"}):
            with self.assertRaisesRegex(kis_order.KisOrderError, "실전 주문은 차단"):
                kis_order.place_order("buy", "005930", 1)


if __name__ == "__main__":
    unittest.main()
