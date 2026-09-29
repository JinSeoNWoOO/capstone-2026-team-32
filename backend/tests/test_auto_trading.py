from backend.tests import _safety  # noqa: F401  맨 먼저: 실제 주문·외부 접속 차단 (_safety.py 참고)
from backend.tests._safety import block_calls
import unittest
from unittest.mock import patch

from backend.app.services import auto_trading
from backend.app.services.auto_trading import (
    AutoSettings,
    AutoTradingEngine,
    candidate_rejection_reason,
    entry_signal,
    exit_reason,
)
from backend.app.services.kis_read import KisReadError


# 엔진이 이름으로 import 해 쓰는 KIS 함수 전부. 테스트가 @patch 로 준 것 말고는 불리면 실패한다.
KIS_NAMES = ("place_order", "get_position", "get_order_fill", "get_stock_snapshot", "get_volume_rank")


class AutoTradingTests(unittest.TestCase):
    def setUp(self):
        block_calls(self, auto_trading, *KIS_NAMES)

    def test_entry_signal_requires_sustained_momentum(self):
        self.assertTrue(entry_signal([1000, 1001, 1002, 1003, 1004], 0.3))
        self.assertFalse(entry_signal([1000, 999, 1001, 998, 1000], 0.3))

    def test_exit_reason_handles_profit_loss_and_time(self):
        settings = AutoSettings(take_profit_pct=0.7, stop_loss_pct=0.4, max_hold_minutes=10)
        self.assertEqual(exit_reason(1007, 1000, 1, settings), "익절")
        self.assertEqual(exit_reason(996, 1000, 1, settings), "손절")
        self.assertEqual(exit_reason(1001, 1000, 600, settings), "최대 보유시간")

    def test_candidate_filter_rejects_untradeable_and_overheated_stocks(self):
        normal = {
            "price": 70_000,
            "upper_limit": 90_000,
            "lower_limit": 50_000,
            "change_rate": 3.0,
            "halted": False,
        }
        self.assertIsNone(candidate_rejection_reason(normal))
        self.assertIn(
            "저가주",
            candidate_rejection_reason({**normal, "price": 3_640}),
        )
        self.assertIn(
            "상한가",
            candidate_rejection_reason(
                {**normal, "price": 90_000, "upper_limit": 90_000}
            ),
        )
        self.assertIn(
            "+15%",
            candidate_rejection_reason({**normal, "change_rate": 15.0}),
        )
        self.assertIn(
            "-10%",
            candidate_rejection_reason({**normal, "change_rate": -10.0}),
        )

    @patch("backend.app.services.auto_trading.get_stock_snapshot")
    def test_scan_selects_liquid_volatile_candidate(self, snapshot):
        samples = {
            "005930": {"price": 70000, "change_rate": 0.5, "open": 69800, "high": 70500, "low": 69500, "volume": 1_000_000, "trading_value": 70_000_000_000, "volume_ratio": 90, "halted": False},
            "000660": {"price": 200000, "change_rate": 2.0, "open": 195000, "high": 204000, "low": 193000, "volume": 4_000_000, "trading_value": 800_000_000_000, "volume_ratio": 180, "halted": False},
        }
        snapshot.side_effect = lambda code: {"stock_code": code, **samples[code]}
        status = AutoTradingEngine().scan(["005930", "000660"])
        self.assertEqual(status["selected"]["stock_code"], "000660")
        self.assertEqual(status["status"], "ready")

    @patch("backend.app.services.auto_trading.get_stock_snapshot")
    @patch("backend.app.services.auto_trading.get_volume_rank")
    def test_scan_without_watchlist_uses_kis_ranking(self, volume_rank, snapshot):
        volume_rank.return_value = [
            {"stock_code": "005930", "stock_name": "삼성전자", "price": 70000, "change_rate": 0.5, "open": 0, "high": 0, "low": 0, "volume": 1_000_000, "trading_value": 70_000_000_000, "volume_ratio": 90, "halted": False},
            {"stock_code": "000660", "stock_name": "SK하이닉스", "price": 200000, "change_rate": 2.0, "open": 0, "high": 0, "low": 0, "volume": 4_000_000, "trading_value": 800_000_000_000, "volume_ratio": 180, "halted": False},
        ]
        snapshot.return_value = {
            "stock_code": "000660",
            "price": 200000,
            "upper_limit": 250000,
            "lower_limit": 150000,
            "change_rate": 2.0,
            "open": 195000,
            "high": 204000,
            "low": 193000,
            "volume": 4_000_000,
            "trading_value": 800_000_000_000,
            "volume_ratio": 180,
            "halted": False,
        }

        status = AutoTradingEngine().scan()

        volume_rank.assert_called_once_with(20)
        snapshot.assert_called_once_with("000660")
        self.assertEqual(status["selected"]["stock_code"], "000660")

    @patch("backend.app.services.auto_trading.get_stock_snapshot")
    def test_raw_share_volume_does_not_outweigh_trading_value(self, snapshot):
        samples = {
            "111111": {
                "price": 100000,
                "change_rate": 2.0,
                "open": 99000,
                "high": 101000,
                "low": 98000,
                "volume": 100_000,
                "trading_value": 1_000_000_000_000,
                "volume_ratio": 100,
                "halted": False,
            },
            "222222": {
                "price": 5000,
                "change_rate": 2.0,
                "open": 4900,
                "high": 5100,
                "low": 4800,
                "volume": 100_000_000,
                "trading_value": 10_000_000_000,
                "volume_ratio": 100,
                "halted": False,
            },
        }
        snapshot.side_effect = lambda code: {"stock_code": code, **samples[code]}

        status = AutoTradingEngine().scan(["111111", "222222"])

        self.assertEqual(status["selected"]["stock_code"], "111111")

    @patch("backend.app.services.auto_trading.get_stock_snapshot")
    def test_run_continues_after_one_transient_read_failure(self, snapshot):
        snapshot.side_effect = [
            KisReadError("일시 오류"),
            {"price": 70000},
        ]
        engine = AutoTradingEngine()
        engine._selected = {"stock_code": "005930"}
        engine._status = "running"
        engine._phase = "진입 신호 관찰"
        engine._stop_requested = True

        with patch.object(engine._stop_event, "wait", return_value=False):
            engine._run()

        status = engine.status()
        self.assertEqual(snapshot.call_count, 2)
        self.assertEqual(status["status"], "stopped")
        self.assertTrue(
            any("자동매매를 계속합니다" in item["message"] for item in status["logs"])
        )


if __name__ == "__main__":
    unittest.main()
