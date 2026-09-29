from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

# 내부 정규화 모델. LS 패킷 → 이 모델로 변환하는 책임은 collector/replay에 있음.

@dataclass
class News:
    realkey: str
    ls_datetime: str      # "YYYYMMDDHHMMSS" (NWS date+time = 기사 송고 시각)
    code: str             # 6자리 종목코드, 없으면 ""
    source_id: str        # NWS id 필드
    title: str
    recv_mono: float      # 수신 시각 (monotonic)
    recv_wall: str        # 수신 시각 (ISO)

@dataclass
class Tick:
    code: str
    exch_time: str        # "HHMMSS"
    price: int
    qty: int
    side: str             # "B" 매수체결 / "S" 매도체결
    recv_mono: float
    sim_t: float          # 이벤트 기준 상대초 (replay/실연결 모두 채움: t - t0)
    venue: str = "KRX"    # "KRX" / "NXT"

@dataclass
class Event:
    event_id: int
    realkey: str
    code: str
    t0_mono: float
    base_price: Optional[int] = None          # 뉴스 후 첫 체결가
    baseline_amount_1m: Optional[float] = None
    sub_end_mono: float = 0.0
    vi_flag: int = 0
    ticks: list = field(default_factory=list)
    pre_price: Optional[int] = None           # t0 직후 REST(t1101) 현재가 = 첫 체결 전 가격
    pre60_price: Optional[int] = None         # t0 직전 완성된 1분봉 종가 (①이 이미 올린 폭 측정용)
    market: str = ""                          # "KOSPI" / "KOSDAQ" / ""
    t0_wall: str = ""                         # 이벤트 생성(뉴스 수신) 벽시계 시각 ISO. t1301 조회 구간 계산에 쓴다
    baseline_vol_1m: Optional[float] = None   # 직전 1분봉 거래량(주) 중앙값 (t8412)
    pre_spike_mult: Optional[float] = None    # 수신 전 pre_spike_window_sec 안의 10초 거래량 최대 배수 (t1301)
    ai_decision: Optional[str] = None         # "BUY" / "SKIP" / "ERROR". 아직 응답 전이면 None
    ai_conf: Optional[float] = None
    ai_ready_mono: Optional[float] = None     # AI 응답이 도착한 monotonic (진입 시각 결정용)

@dataclass
class Intent:
    event_id: int
    side: str             # "BUY" / "SELL"
    reason: str
    at_mono: float
    ref_price: int
    counterfactual: bool = False   # True면 가상 진입/청산 (RiskGuard·손익 집계에서 제외, 측정에만 쓴다)

@dataclass
class Order:
    order_id: int
    event_id: int
    side: str
    qty: int
    state: str            # INTENT SENT ACKED FILLED REJECTED CANCELLED
    sent_mono: float
    ls_order_no: str = ""
    fill_price: Optional[int] = None
    counterfactual: bool = False
