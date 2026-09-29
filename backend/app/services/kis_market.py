import os
from dotenv import load_dotenv
from pathlib import Path

from backend.app.services.kis_auth import get_access_token
from backend.app.services.kis_read import get_kis_json

ENV_PATH = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(ENV_PATH)

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")

BASE_URL = "https://openapivts.koreainvestment.com:29443"


def _get_price_output(stock_code: str) -> dict:
    access_token = get_access_token()

    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"

    headers = {
        "authorization": f"Bearer {access_token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010100",
        "custtype": "P",
    }

    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": stock_code,
    }

    data = get_kis_json(
        url,
        headers=headers,
        params=params,
        failure_message="현재가 조회 요청에 실패했습니다.",
    )

    return data["output"]


def _to_int(value) -> int:
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _to_float(value) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def get_current_price(stock_code: str) -> int:
    return _to_int(_get_price_output(stock_code)["stck_prpr"])


def get_stock_snapshot(stock_code: str) -> dict:
    output = _get_price_output(stock_code)
    return {
        "stock_code": stock_code,
        "price": _to_int(output.get("stck_prpr")),
        "upper_limit": _to_int(output.get("stck_mxpr")),
        "lower_limit": _to_int(output.get("stck_llam")),
        "change_rate": _to_float(output.get("prdy_ctrt")),
        "open": _to_int(output.get("stck_oprc")),
        "high": _to_int(output.get("stck_hgpr")),
        "low": _to_int(output.get("stck_lwpr")),
        "volume": _to_int(output.get("acml_vol")),
        "trading_value": _to_int(output.get("acml_tr_pbmn")),
        "volume_ratio": _to_float(output.get("prdy_vrss_vol_rate")),
        "halted": output.get("temp_stop_yn") == "Y",
    }


def get_volume_rank(limit: int = 10) -> list[dict]:
    access_token = get_access_token()
    headers = {
        "authorization": f"Bearer {access_token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHPST01710000",
        "custtype": "P",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_COND_SCR_DIV_CODE": "20171",
        "FID_INPUT_ISCD": "0000",
        "FID_DIV_CLS_CODE": "1",
        "FID_BLNG_CLS_CODE": "3",
        "FID_TRGT_CLS_CODE": "111111111",
        # ETF/ETN은 단일 종목 단타 후보에서 제외합니다.
        # 투자위험/경고/주의, 관리/정리매매, 거래정지, ETF/ETN 등은 제외합니다.
        "FID_TRGT_EXLS_CLS_CODE": "1111111111",
        "FID_INPUT_PRICE_1": "5000",
        "FID_INPUT_PRICE_2": "",
        "FID_VOL_CNT": "",
        "FID_INPUT_DATE_1": "",
    }
    data = get_kis_json(
        f"{BASE_URL}/uapi/domestic-stock/v1/quotations/volume-rank",
        headers=headers,
        params=params,
        failure_message="거래량 순위 조회 요청에 실패했습니다.",
    )

    results = []
    for item in (data.get("output") or [])[:limit]:
        code = item.get("mksc_shrn_iscd") or item.get("stck_shrn_iscd")
        if not code:
            continue
        results.append(
            {
                "stock_code": code,
                "stock_name": item.get("hts_kor_isnm") or code,
                "price": _to_int(item.get("stck_prpr")),
                # 거래금액 순위 응답에는 상하한가가 없어 최종 선택 전에 재조회합니다.
                "upper_limit": 0,
                "lower_limit": 0,
                "change_rate": _to_float(item.get("prdy_ctrt")),
                "open": 0,
                "high": 0,
                "low": 0,
                "volume": _to_int(item.get("acml_vol")),
                "trading_value": _to_int(item.get("acml_tr_pbmn")),
                "volume_ratio": _to_float(item.get("vol_inrt")),
                "halted": False,
            }
        )
    return results


if __name__ == "__main__":
    price = get_current_price("005930")
    print("현재가:", price)
