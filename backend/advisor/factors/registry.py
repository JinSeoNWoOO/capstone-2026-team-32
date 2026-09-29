"""요인 메타데이터 (결정 6·12). 설정의 factors: 블록을 코드가 다루기 쉬운 형태로 옮긴다.

가중치·부호·채점 주 기간·출처를 요인마다 한곳에 모아 두는 이유는, 이 표가 그대로
README 의 "왜 이 가중치인가" 표가 되고 도전 가중치 안의 비교 대상이 되기 때문이다.
값은 설정 파일에만 있고 여기에는 없다 — 이 모듈은 옮겨 담기만 한다.
"""
from dataclasses import dataclass, field

from ..config import validate_config


@dataclass(frozen=True)
class FactorSpec:
    """요인 하나의 메타데이터. 설정 한 줄과 일대일로 대응한다."""

    id: str
    layer: str                  # market | sector | stock (결정 3의 세 계층)
    weight: int                 # 근거 등급 2·1·0 — 0은 점수만 저장하는 관찰 요인
    sign: int                   # +1 | -1, 문헌 근거로 미리 고정한 부호 방향 (결정 9)
    horizon: int                # 채점 주 기간(거래일). 사후에 고르면 끼워 맞추기가 된다 (결정 5)
    transform: str              # rank | hist_pct | rule | rubric
    params: dict = field(default_factory=dict)
    stages: tuple = ()          # 비어 있으면 모든 단계. 예: ("final",) 은 밤사이 정보 요인
    llm: bool = False
    source: str = ""

    def applies_to(self, stage):
        """이 단계에서 값이 있는 요인인가. stages 가 비어 있으면 항상 참."""
        return not self.stages or stage is None or stage in self.stages

    @property
    def used_in_weighting(self):
        """가중 합산에 들어가는가. 관찰 요인(가중치 0)은 저장만 하고 점수에는 영향이 없다."""
        return self.weight > 0


def load_specs(cfg):
    """설정 → {factor_id: FactorSpec}. 설정 파일의 등장 순서를 지킨다."""
    validate_config(cfg)
    specs = {}
    for fid, meta in (cfg.get("factors") or {}).items():
        stages = meta.get("stages")
        specs[fid] = FactorSpec(
            id=fid,
            layer=meta["layer"],
            weight=int(meta["weight"]),
            sign=int(meta["sign"]),
            horizon=int(meta["horizon"]),
            transform=meta["transform"],
            params=dict(meta.get("params") or {}),
            stages=tuple(stages) if stages else (),
            llm=bool(meta.get("llm", False)),
            source=str(meta.get("source") or ""),
        )
    return specs


def specs_for(specs, layer=None, stage=None):
    """계층·단계로 걸러낸 {factor_id: FactorSpec}."""
    return {fid: s for fid, s in specs.items()
            if (layer is None or s.layer == layer) and s.applies_to(stage)}
