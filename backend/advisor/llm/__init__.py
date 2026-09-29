"""LLM 계층 (설계 5.4·5.6·9장, 결정 15). 코드가 못 정하는 '글 판단'만 여기로 온다.

이 패키지의 경계는 하나다. **숫자는 코드가, 글은 LLM 이** (결정 3).
  - `disclosure_score`: 공시 유형·비율로 확정되는 것은 부르지 않고, 남은 것만 채점한다 (설계 5.4).
  - `news_risk`: 헤드라인을 분류해 섹터 방향과 테마 의심을 남긴다 (관찰 요인, 결정 12).
  - `adjust`: 이미 계산된 v0 점수를 ±상한 안에서만 손대고, 위험 표시가 있는 자산만 거부할 수 있다 (결정 4).
  - `client`: 호출·기록·캐시·예산. 예외를 던지지 않고 기권(ERROR)으로 돌려준다 (결정 15).
  - `stage`: 배치(run.py)가 부르는 두 개의 함수. 이 패키지 밖에서는 이 둘만 알면 된다.

LLM 이 꺼져 있거나 실패해도 파이프라인은 그대로 돈다 — v0 판단이 그날의 판단이 된다 (결정 4).
"""
from .client import DEFAULTS, LLMClient, LLMResult
from .stage import llm_adjust, llm_factors

__all__ = ["DEFAULTS", "LLMClient", "LLMResult", "llm_adjust", "llm_factors"]
