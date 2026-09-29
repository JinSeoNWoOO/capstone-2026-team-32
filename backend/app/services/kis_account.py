import os
from dotenv import load_dotenv
from pathlib import Path

from backend.app.services.kis_auth import get_access_token
from backend.app.services.kis_read import get_kis_json

ENV_PATH = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(ENV_PATH)

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")

ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO")
ACCOUNT_PRODUCT_CODE = os.getenv("KIS_ACCOUNT_PRODUCT_CODE") or os.getenv(
    "KIS_ACCOUNT_PRODUCT"
)

BASE_URL = "https://openapivts.koreainvestment.com:29443"


def get_balance():
    access_token = get_access_token()

    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance"

    headers = {
        "authorization": f"Bearer {access_token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "VTTC8434R",
        "custtype": "P",
    }

    params = {
        "CANO": ACCOUNT_NO,
        "ACNT_PRDT_CD": ACCOUNT_PRODUCT_CODE,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "01",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "00",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }

    return get_kis_json(
        url,
        headers=headers,
        params=params,
        failure_message="잔고 조회 요청에 실패했습니다.",
    )


def get_position(stock_code: str) -> dict:
    data = get_balance()
    for position in data.get("output1", []):
        if position.get("pdno") == stock_code:
            return {
                "stock_code": stock_code,
                "quantity": int(position.get("hldg_qty") or 0),
                "sellable_quantity": int(position.get("ord_psbl_qty") or 0),
                "average_price": float(position.get("pchs_avg_pric") or 0),
                "current_price": int(float(position.get("prpr") or 0)),
            }
    return {
        "stock_code": stock_code,
        "quantity": 0,
        "sellable_quantity": 0,
        "average_price": 0.0,
        "current_price": 0,
    }


if __name__ == "__main__":
    data = get_balance()
    print(data)
