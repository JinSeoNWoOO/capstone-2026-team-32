"""주문 체결 조회. 주문번호로 실제 체결 수량과 체결 평균가를 받아온다.

왜 필요한가: 주문 접수 성공(ODNO 수신)은 체결이 아니다. 지금까지 자동매매는 주문을 보낸 뒤
잔고에서 수량이 늘어난 것을 보고 "그 순간 조회한 현재가"를 진입가로 삼았는데, 이는

  - 시점이 다르고 (체결은 조회 주기만큼 전에 일어났다)
  - 가격 종류가 다르며 (시장가 매수는 최우선 매도호가에 체결되고 현재가는 마지막 체결가다.
    실측 스프레드 중앙값이 0.17%, 현재가와 매도호가 차이가 중앙값 0.055%다)
  - 부분 체결을 구분하지 못한다

익절·손절·손익이 모두 진입가에서 나오므로, 이 오차는 그대로 판단 오차가 된다.

`INQUIRE_DAILY_CCLD` (주식일별주문체결조회)로 주문번호를 찾아 tot_ccld_qty(총체결수량)와
avg_prvs(체결평균가)를 읽는다. 모의투자 tr_id 는 VTTC8001R 이다.

**주의**: 응답 필드명은 계좌 종류와 API 개정에 따라 다를 수 있다. 아래 후보 목록으로
방어적으로 읽고, 못 찾으면 raw 를 함께 돌려주니 `python -m backend.app.services.kis_fill <주문번호>`
로 실제 응답을 한 번 찍어 확인할 것.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from backend.app.services.kis_auth import get_access_token
from backend.app.services.kis_read import KisReadError, get_kis_json

ENV_PATH = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(ENV_PATH)

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO")
ACCOUNT_PRODUCT_CODE = os.getenv("KIS_ACCOUNT_PRODUCT_CODE") or os.getenv("KIS_ACCOUNT_PRODUCT")

BASE_URL = "https://openapivts.koreainvestment.com:29443"
SEOUL = timezone(timedelta(hours=9), name="Asia/Seoul")

# 응답 필드명 후보. 앞에 있는 것부터 찾는다.
ORDER_NO_KEYS = ("odno", "ODNO", "ord_no")
FILLED_QTY_KEYS = ("tot_ccld_qty", "ccld_qty", "TOT_CCLD_QTY")
ORDER_QTY_KEYS = ("ord_qty", "ORD_QTY")
AVG_PRICE_KEYS = ("avg_prvs", "ccld_avg_unpr", "AVG_PRVS", "ccld_prvs")
FILLED_AMOUNT_KEYS = ("tot_ccld_amt", "ccld_amt", "TOT_CCLD_AMT")


def _pick(row: dict, keys) -> str | None:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _number(value, default=0.0) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def get_order_fill(order_no: str, stock_code: str | None = None, date: str | None = None) -> dict:
    """주문번호의 당일 체결 상태.

    반환: {order_no, order_qty, filled_qty, avg_price, filled, partial, found, raw}
      - filled_qty 0 이면 아직 체결 전이다(주문은 살아 있다).
      - avg_price 는 체결 평균가. 체결 금액만 오고 평균가가 비면 금액/수량으로 계산한다.
      - found=False 는 그 주문번호를 못 찾은 것이다. 방금 낸 주문은 반영까지 몇 초 걸릴 수 있다.
    """
    if not order_no:
        raise KisReadError("주문번호가 없습니다.")
    day = date or datetime.now(SEOUL).strftime("%Y%m%d")
    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
    headers = {
        "authorization": f"Bearer {get_access_token()}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "VTTC8001R",          # 모의투자 주식일별주문체결조회 (실전은 TTTC8001R, 사용 안 함)
        "custtype": "P",
    }
    params = {
        "CANO": ACCOUNT_NO,
        "ACNT_PRDT_CD": ACCOUNT_PRODUCT_CODE,
        "INQR_STRT_DT": day,
        "INQR_END_DT": day,
        "SLL_BUY_DVSN_CD": "00",       # 00 전체
        "INQR_DVSN": "00",             # 00 역순
        "PDNO": stock_code or "",
        "CCLD_DVSN": "00",             # 00 전체 (체결·미체결 모두)
        "ORD_GNO_BRNO": "",
        "ODNO": str(order_no),
        "INQR_DVSN_3": "00",
        "INQR_DVSN_1": "",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }
    data = get_kis_json(url, headers=headers, params=params,
                        failure_message="주문 체결 조회에 실패했습니다.")
    rows = data.get("output1") or data.get("output") or []
    target = next((r for r in rows if str(_pick(r, ORDER_NO_KEYS) or "").lstrip("0")
                   == str(order_no).lstrip("0")), None)
    if target is None:
        return {"order_no": order_no, "order_qty": 0, "filled_qty": 0, "avg_price": 0.0,
                "filled": False, "partial": False, "found": False, "raw": rows[:3]}

    order_qty = int(_number(_pick(target, ORDER_QTY_KEYS)))
    filled_qty = int(_number(_pick(target, FILLED_QTY_KEYS)))
    avg_price = _number(_pick(target, AVG_PRICE_KEYS))
    if avg_price <= 0 and filled_qty > 0:            # 평균가가 비면 체결 금액에서 되돌린다
        avg_price = _number(_pick(target, FILLED_AMOUNT_KEYS)) / filled_qty
    return {
        "order_no": order_no,
        "order_qty": order_qty,
        "filled_qty": filled_qty,
        "avg_price": round(avg_price, 2),
        "filled": filled_qty > 0 and filled_qty >= order_qty,
        "partial": 0 < filled_qty < order_qty,
        "found": True,
        "raw": target,
    }


def wait_for_fill(order_no: str, stock_code: str | None = None, *,
                  timeout_sec: float = 30.0, interval_sec: float = 1.0) -> dict:
    """체결될 때까지(또는 시간이 다 될 때까지) 조회한다. 부분 체결이면 그 상태로 돌려준다."""
    import time as _time
    deadline = _time.monotonic() + timeout_sec
    last = {"order_no": order_no, "filled_qty": 0, "avg_price": 0.0,
            "filled": False, "partial": False, "found": False, "raw": None}
    while _time.monotonic() < deadline:
        try:
            last = get_order_fill(order_no, stock_code)
        except KisReadError:
            pass                                     # 조회 실패는 다음 주기에 다시 본다
        if last.get("filled"):
            return last
        _time.sleep(interval_sec)
    return last


if __name__ == "__main__":                           # 실제 응답 필드 확인용
    import json
    import sys
    if len(sys.argv) < 2:
        raise SystemExit("사용: python -m backend.app.services.kis_fill <주문번호> [종목코드]")
    print(json.dumps(get_order_fill(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None),
                     ensure_ascii=False, indent=2))
