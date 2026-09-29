"""KIS 모의투자 REST 의 조회 전용 래퍼 (일봉·지수 일봉·투자자별 매매동향).

기존 단타 모드의 `app/services/kis_read.get_kis_json`(지수 백오프 재시도)을 그대로 쓰고,
advisor 가 필요한 것만 두 가지 더 얹는다.

1. **호출 간격**. 배치 한 번에 종목 수만큼 호출하므로 모의투자 서버의 초당 제한에 걸리기 쉽다.
   설정 `sources.kis_min_interval_sec` 만큼 무조건 쉬고 부른다 (재시도는 kis_read 가 한다).
2. **주입 가능한 전송 계층**. 단위 테스트는 네트워크를 쓰지 않는다 — 생성자에 `transport` 를
   넣으면 그 함수만 호출한다. 기본 전송은 **함수 안에서 늦게 import** 한다. app.services 는
   import 하는 것만으로 .env 를 읽고 토큰 캐시를 건드리기 때문이다.

숫자 파싱은 관대하게 한다. KIS 는 빈 문자열('')로 "아직 값이 없다"를 표현한다 — 장중에 조회한
투자자별 매매동향의 오늘 행이 그렇다. 빈 값은 0 이 아니라 None 이다.
"""
import time

from .base import compact_date, sources_cfg

# 조회 전용 TR 번호 (모두 domestic-stock/v1/quotations 아래).
TR_DAILY_CHART = "FHKST03010100"      # 국내주식 기간별 시세 (일봉, output2 에 거래대금 포함)
TR_INDEX_CHART = "FHKUP03500100"      # 업종/지수 기간별 시세
TR_INVESTOR = "FHKST01010900"         # 종목별 투자자 매매동향 (최근 30영업일)

PATH_DAILY_CHART = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
PATH_INDEX_CHART = "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
PATH_INVESTOR = "/uapi/domestic-stock/v1/quotations/inquire-investor"


def to_number(value):
    """'1,234' → 1234.0, '' · None · 파싱 실패 → None.

    0 으로 때우지 않는 이유: 투자자별 매매동향의 미완성 오늘 행이 '' 로 오는데 이걸 0 으로 읽으면
    "오늘 외국인 순매수 0원"이라는 없는 사실이 저장된다.
    """
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in ("-", "null"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


class KisClient:
    """조회 전용 KIS 호출. 실패하면 예외를 던지고, 호출자(krx.py)가 대체 경로를 고른다."""

    def __init__(self, cfg, transport=None, sleep=time.sleep):
        self.cfg = cfg
        self.min_interval = float(sources_cfg(cfg, "kis_min_interval_sec"))
        self._transport = transport
        self._sleep = sleep
        self._last_call = 0.0

    # ------------------------------------------------------------ 전송

    def _default_transport(self, path, tr_id, params):
        """진짜 KIS 호출. import 를 여기서 하는 이유는 머리말 2 참고."""
        from ...app.services.kis_auth import APP_KEY, APP_SECRET, BASE_URL, get_access_token
        from ...app.services.kis_read import get_kis_json
        headers = {"authorization": f"Bearer {get_access_token()}", "appkey": APP_KEY,
                   "appsecret": APP_SECRET, "tr_id": tr_id, "custtype": "P"}
        return get_kis_json(f"{BASE_URL}{path}", headers=headers, params=params,
                            failure_message=f"KIS {tr_id} 조회에 실패했습니다.")

    def call(self, path, tr_id, params):
        """간격을 지켜 한 번 호출한다."""
        gap = self.min_interval - (time.monotonic() - self._last_call)
        if gap > 0:
            self._sleep(gap)
        try:
            transport = self._transport or self._default_transport
            return transport(path, tr_id, params) or {}
        finally:
            self._last_call = time.monotonic()

    # ------------------------------------------------------------ 조회

    def daily_chart(self, code, start, end, market_div="J"):
        """종목 일봉. 한 번에 최근 kis_chart_max_rows 행까지만 오므로 호출자가 구간을 쪼갠다.

        FID_ORG_ADJ_PRC 는 설정값(기본 "0" = 수정주가)이다. 수정주가로 받아야 액면분할·유상증자
        전후의 stk_high52(250거래일 최고가 대비) 가 가짜 급락을 만들지 않는다.
        """
        data = self.call(PATH_DAILY_CHART, TR_DAILY_CHART, {
            "FID_COND_MRKT_DIV_CODE": market_div,
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": compact_date(start),
            "FID_INPUT_DATE_2": compact_date(end),
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": str(sources_cfg(self.cfg, "kis_adj_price")),
        })
        return [r for r in (data.get("output2") or []) if r and r.get("stck_bsop_date")]

    def index_chart(self, index_code, start, end):
        """지수 일봉 (코스피는 설정의 kis_index_code, 기본 '0001')."""
        data = self.call(PATH_INDEX_CHART, TR_INDEX_CHART, {
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": str(index_code),
            "FID_INPUT_DATE_1": compact_date(start),
            "FID_INPUT_DATE_2": compact_date(end),
            "FID_PERIOD_DIV_CODE": "D",
        })
        return [r for r in (data.get("output2") or []) if r and r.get("stck_bsop_date")]

    def investor_daily(self, code, market_div="J"):
        """종목별 투자자 매매동향. 날짜 인자가 없고 **최근 30영업일**이 통째로 온다.

        그래서 증분 수집이라도 받아오는 양은 같다 — 호출 수만 줄이면 된다.
        """
        data = self.call(PATH_INVESTOR, TR_INVESTOR, {
            "FID_COND_MRKT_DIV_CODE": market_div,
            "FID_INPUT_ISCD": code,
        })
        return [r for r in (data.get("output") or []) if r and r.get("stck_bsop_date")]


__all__ = ["KisClient", "to_number", "TR_DAILY_CHART", "TR_INDEX_CHART", "TR_INVESTOR",
           "PATH_DAILY_CHART", "PATH_INDEX_CHART", "PATH_INVESTOR"]
