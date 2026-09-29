from backend.tests import _safety  # noqa: F401  맨 먼저: 실제 주문·외부 접속 차단 (_safety.py 참고)
import unittest
from unittest.mock import Mock, patch

import requests

from backend.app.services.kis_read import KisReadError, get_kis_json


class KisReadTests(unittest.TestCase):
    @patch("backend.app.services.kis_read.READ_MAX_ATTEMPTS", 3)
    @patch("backend.app.services.kis_read.time.sleep")
    @patch("backend.app.services.kis_read.requests.get")
    def test_get_retries_transient_network_failure(self, request_get, sleep):
        success = Mock()
        success.json.return_value = {"rt_cd": "0", "output": {"value": 1}}
        request_get.side_effect = [requests.Timeout(), success]

        data = get_kis_json(
            "https://example.test/read",
            headers={},
            params={},
            failure_message="조회 실패",
        )

        self.assertEqual(data["output"]["value"], 1)
        self.assertEqual(request_get.call_count, 2)
        sleep.assert_called_once()

    @patch("backend.app.services.kis_read.READ_MAX_ATTEMPTS", 3)
    @patch("backend.app.services.kis_read.time.sleep")
    @patch("backend.app.services.kis_read.requests.get")
    def test_get_raises_after_bounded_retries(self, request_get, sleep):
        request_get.side_effect = requests.Timeout()

        with self.assertRaisesRegex(KisReadError, "3회 시도 후 실패"):
            get_kis_json(
                "https://example.test/read",
                headers={},
                params={},
                failure_message="잔고 조회 요청에 실패했습니다.",
            )

        self.assertEqual(request_get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
