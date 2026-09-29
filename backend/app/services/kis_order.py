import os
from pathlib import Path
from typing import Literal

import requests
from dotenv import load_dotenv

from backend.app.services.kis_auth import (
    BASE_URL,
    KisAuthError,
    REQUEST_TIMEOUT,
    get_access_token,
    get_hash_key,
)


ENV_PATH = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(ENV_PATH)

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO")
ACCOUNT_PRODUCT_CODE = os.getenv("KIS_ACCOUNT_PRODUCT_CODE") or os.getenv(
    "KIS_ACCOUNT_PRODUCT"
)

OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]

PAPER_TR_IDS = {
    "buy": "VTTC0012U",
    "sell": "VTTC0011U",
}
ORDER_DIVISION_CODES = {
    "limit": "00",
    "market": "01",
}


class KisOrderError(RuntimeError):
    """KIS 주문 처리 실패."""


class KisOrderValidationError(ValueError):
    """주문 입력값 오류."""


def get_order_mode() -> str:
    mode = os.getenv("KIS_ORDER_MODE", "dry-run").strip().lower()
    aliases = {"dryrun": "dry-run", "dry_run": "dry-run", "demo": "paper"}
    mode = aliases.get(mode, mode)

    if mode not in {"dry-run", "paper"}:
        raise KisOrderError(
            "KIS_ORDER_MODE는 dry-run 또는 paper만 사용할 수 있습니다. 실전 주문은 차단됩니다."
        )
    return mode


def _validate_order(
    side: str,
    stock_code: str,
    quantity: int,
    order_type: str,
    price: int,
) -> None:
    if side not in PAPER_TR_IDS:
        raise KisOrderValidationError("매수(buy) 또는 매도(sell)만 가능합니다.")
    if not stock_code.isdigit() or len(stock_code) not in {6, 7}:
        raise KisOrderValidationError("종목코드는 숫자 6자리(ETN은 7자리)여야 합니다.")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise KisOrderValidationError("주문 수량은 1 이상의 정수여야 합니다.")
    if quantity > 1_000_000:
        raise KisOrderValidationError("주문 수량이 허용 범위를 초과했습니다.")
    if order_type not in ORDER_DIVISION_CODES:
        raise KisOrderValidationError("주문 방식은 market 또는 limit이어야 합니다.")
    if isinstance(price, bool) or not isinstance(price, int) or price < 0:
        raise KisOrderValidationError("주문 가격은 0 이상의 정수여야 합니다.")
    if order_type == "limit" and price <= 0:
        raise KisOrderValidationError("지정가 주문에는 1원 이상의 가격이 필요합니다.")


def _require_paper_configuration() -> None:
    missing = [
        name
        for name, value in (
            ("KIS_APP_KEY", APP_KEY),
            ("KIS_APP_SECRET", APP_SECRET),
            ("KIS_ACCOUNT_NO", ACCOUNT_NO),
            ("KIS_ACCOUNT_PRODUCT_CODE", ACCOUNT_PRODUCT_CODE),
        )
        if not value
    ]
    if missing:
        raise KisOrderError(f"필수 환경변수가 없습니다: {', '.join(missing)}")


def place_order(
    side: OrderSide,
    stock_code: str,
    quantity: int,
    price: int = 0,
    order_type: OrderType = "market",
) -> dict:
    """dry-run을 수행하거나 KIS 모의투자 현금 주문을 접수한다."""
    normalized_code = stock_code.strip()
    _validate_order(side, normalized_code, quantity, order_type, price)

    normalized_price = price if order_type == "limit" else 0
    mode = get_order_mode()
    common_result = {
        "ok": True,
        "mode": mode,
        "side": side,
        "stock_code": normalized_code,
        "quantity": quantity,
        "order_type": order_type,
        "price": normalized_price,
    }

    if mode == "dry-run":
        return {
            **common_result,
            "submitted": False,
            "status": "simulated",
            "order_no": None,
            "order_time": None,
            "message": "Dry-run 검증 완료: KIS 서버로 주문을 전송하지 않았습니다.",
        }

    _require_paper_configuration()

    body = {
        "CANO": ACCOUNT_NO,
        "ACNT_PRDT_CD": ACCOUNT_PRODUCT_CODE,
        "PDNO": normalized_code,
        "ORD_DVSN": ORDER_DIVISION_CODES[order_type],
        "ORD_QTY": str(quantity),
        "ORD_UNPR": str(normalized_price),
        "EXCG_ID_DVSN_CD": "KRX",
        "SLL_TYPE": "01" if side == "sell" else "",
        "CNDT_PRIC": "",
    }

    try:
        headers = {
            "Content-Type": "application/json",
            "authorization": f"Bearer {get_access_token()}",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
            "tr_id": PAPER_TR_IDS[side],
            "custtype": "P",
            "hashkey": get_hash_key(body),
        }
        response = requests.post(
            f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
            headers=headers,
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError, KisAuthError) as exc:
        raise KisOrderError("KIS 모의투자 주문 요청에 실패했습니다.") from exc

    if data.get("rt_cd") != "0":
        code = data.get("msg_cd", "UNKNOWN")
        message = data.get("msg1", "알 수 없는 KIS 주문 오류")
        raise KisOrderError(f"KIS 주문 거절({code}): {message}")

    output = data.get("output") or {}
    return {
        **common_result,
        "submitted": True,
        "status": "accepted",
        "order_no": output.get("ODNO"),
        "order_time": output.get("ORD_TMD"),
        "message": data.get("msg1") or "KIS 모의투자 주문이 접수되었습니다.",
    }


def buy_stock(
    stock_code: str,
    quantity: int,
    price: int = 0,
    order_type: OrderType = "market",
) -> dict:
    return place_order("buy", stock_code, quantity, price, order_type)


def sell_stock(
    stock_code: str,
    quantity: int,
    price: int = 0,
    order_type: OrderType = "market",
) -> dict:
    return place_order("sell", stock_code, quantity, price, order_type)
