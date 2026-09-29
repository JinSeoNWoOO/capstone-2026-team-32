from __future__ import annotations
from dataclasses import dataclass

# 내부 정규화 모델. LS 패킷 → 이 모델로 변환하는 책임은 collector 에 있음.

@dataclass
class News:
    realkey: str
    ls_datetime: str      # "YYYYMMDDHHMMSS" (NWS date+time = 기사 송고 시각)
    code: str             # 6자리 종목코드, 없으면 ""
    source_id: str        # NWS id 필드 (15 = 공시 속보)
    title: str
    recv_mono: float      # 수신 시각 (monotonic)
    recv_wall: str        # 수신 시각 (ISO). 판단 지원이 "언제 알 수 있었나"로 쓴다
