"""신용잔고·예탁금 (`mkt_credit`) — **1차에서는 구현하지 않는다**. 이 파일은 그 사실의 기록이다.

설계 12장이 미리 적어 둔 대로 "금융투자협회 FreeSIS 수집이 까다로우면 `mkt_credit` 은 생략하고
결측으로 기록한다". 2026-09-22 조사 결과 그 쪽으로 결론이 났다.

- FreeSIS(freesis.kofia.or.kr)는 화면이 자바스크립트로 조회 조건을 만들고 내부 POST 로 값을 받는데,
  그 규약이 공개돼 있지 않고 화면 구조에 묶여 있다. 마감까지의 시간을 쓸 만한 곳이 아니다.
- `mkt_credit` 은 **가중치 0 인 관찰 요인**이다 (설정 factors.mkt_credit.weight = 0). 값이 없어도
  시장 계층 점수는 나머지 세 요인으로 계산된다 (설계 5.3: 결측은 분자·분모에서 함께 뺀다).

그래서 여기서는 "값이 없다"를 **정직하게** 돌려준다. 빈 목록을 주면 요인 코드가 `missing=1` 로
기록하고, 리포트에는 "자료 없음"으로 뜬다 — 0 을 넣어 "관찰했는데 중립"으로 위장하지 않는다.

나중에 붙인다면: 한국거래소 정보데이터시스템의 신용거래융자 잔고(12025)와 금융투자협회의
투자자예탁금이 같은 값을 준다. 둘 다 KRX 로그인·별도 스크래핑이 필요하다.
"""
from .base import Report

SERIES_CREDIT = "CREDIT_BALANCE"     # 신용잔고 (market_daily.series 예약 이름)
SERIES_DEPOSIT = "INVESTOR_DEPOSIT"  # 투자자예탁금


def fetch_credit(store, cfg, as_of, report=None, **_):
    """항상 빈 결과. 사유를 함께 돌려줘 ingest 요약과 run.note 에 남게 한다."""
    report = report if report is not None else Report()
    report.fallback("mkt_credit_not_implemented")
    return {"rows": 0, "provider": None,
            "note": "KOFIA FreeSIS 는 1차 범위 밖 — mkt_credit 은 결측으로 둔다 (설계 12장)"}


__all__ = ["fetch_credit", "SERIES_CREDIT", "SERIES_DEPOSIT"]
