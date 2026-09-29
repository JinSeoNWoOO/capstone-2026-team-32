"""advisor.config.yaml 로드·검증·버전 해시.

설정을 코드와 분리하는 이유는 두 가지다 (결정 6).
  1. 가중치·기간·비중 규칙을 바꾼 것이 곧 '도전 안'이라, 무엇을 바꿨는지가 파일 하나의 diff 로 보여야 한다.
  2. 모든 실행 기록에 config_hash 를 남겨 두면 "이 판단은 어느 설정으로 나온 것인가"가 사후에 복원된다.

경로는 **이 파일 기준**으로 푼다. 배치는 cron·스케줄러·터미널 어디서 실행돼도 같은 설정을 읽어야 하는데
cwd 기준으로 풀면 실행 위치에 따라 다른 파일을 읽거나 못 찾는다.

검증은 "값이 이상하면 배치를 시작하기 전에 죽는다"를 목표로 한다. 요인 하나의 가중치 오타가
점수·비중·채점까지 조용히 흘러가면 그날 기록 전체를 못 쓰게 된다.
"""
import hashlib
import json
from pathlib import Path

import yaml

# 이 파일: backend/advisor/config.py → 설정: backend/advisor.config.yaml
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "advisor.config.yaml"

# 설계 5.1 의 변환 종류와 결정 3의 세 계층. 여기에 없는 값은 오타로 본다.
TRANSFORMS = ("rank", "hist_pct", "rule", "rubric")
LAYERS = ("market", "sector", "stock")
STAGES = ("prelim", "final")
WEIGHTS = (0, 1, 2)            # 결정 6의 근거 등급: 강함 2, 중간 1, 관찰 0

REQUIRED_TOP = ("paths", "schedule", "calendar", "universe", "factors", "normalize", "combine",
                "allocate", "costs", "scoring", "reeval", "llm", "disclosure_rubric", "risk_flags")
REQUIRED_SECTIONS = {
    "paths": ("db", "newsgap_db", "ledger"),
    "schedule": ("prelim", "final", "fallback_deadline", "poll_interval_min", "poll_window"),
    "universe": ("stock_index", "core_etf", "cash_etf", "sector_etfs", "other_etfs"),
    "normalize": ("hist_window_years", "min_history"),
    "combine": ("sector_tilt",),
    "allocate": ("risk_base", "risk_slope", "risk_min", "risk_max", "core_share", "sector_share",
                 "n_sectors", "n_stocks", "stock_cap", "exit_rank", "rebalance_band"),
    "costs": ("slippage", "fee", "tax_stock_sell"),
    "llm": ("enabled", "provider", "api_key_env", "daily_budget_usd", "top_n", "adj_cap",
            "timeout_sec", "retries", "temperature", "models", "prompt_ver", "price_per_mtok"),
    "risk_flags": ("spike_days", "spike_abs_ret"),
}
REQUIRED_FACTOR = ("layer", "weight", "sign", "horizon", "transform")


class ConfigError(Exception):
    """설정 파일이 계약을 어겼다. 메시지는 '어느 키가 왜 틀렸는가'까지 담는다."""


def load_config(path=None):
    """설정을 읽고 검증해서 dict 로 준다. path 를 생략하면 저장소의 advisor.config.yaml."""
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if not p.exists():
        raise ConfigError(f"설정 파일이 없습니다: {p}")
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ConfigError(f"설정 파일이 매핑이 아닙니다: {p}")
    validate_config(cfg)
    return cfg


def config_yaml_text(path=None):
    """설정 파일 원문. config_version 테이블에 그대로 넣어 사후에 해시를 검산할 수 있게 한다."""
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(p, encoding="utf-8") as f:
        return f.read()


def config_hash(cfg):
    """설정 내용의 지문 12자리.

    원문 텍스트가 아니라 파싱된 값을 정규화(키 정렬)해 해싱한다 — 주석·들여쓰기·키 순서를 고쳐도
    값이 같으면 같은 설정으로 봐야 실행 기록이 쓸데없이 쪼개지지 않는다.
    """
    canonical = json.dumps(cfg, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def validate_config(cfg):
    """필수 키·가중치 등급·변환·계층을 확인한다. 어기면 ConfigError."""
    for key in REQUIRED_TOP:
        if key not in cfg:
            raise ConfigError(f"필수 키가 없습니다: {key}")
    for section, keys in REQUIRED_SECTIONS.items():
        block = cfg.get(section)
        if not isinstance(block, dict):
            raise ConfigError(f"{section} 은 매핑이어야 합니다")
        for key in keys:
            if key not in block:
                raise ConfigError(f"필수 키가 없습니다: {section}.{key}")

    factors = cfg.get("factors")
    if not isinstance(factors, dict) or not factors:
        raise ConfigError("factors 가 비어 있습니다")
    for fid, meta in factors.items():
        if not isinstance(meta, dict):
            raise ConfigError(f"factors.{fid} 은 매핑이어야 합니다")
        for key in REQUIRED_FACTOR:
            if key not in meta:
                raise ConfigError(f"필수 키가 없습니다: factors.{fid}.{key}")
        if meta["layer"] not in LAYERS:
            raise ConfigError(f"factors.{fid}.layer 가 알 수 없는 계층입니다: {meta['layer']} (가능: {LAYERS})")
        if meta["transform"] not in TRANSFORMS:
            raise ConfigError(f"factors.{fid}.transform 이 알 수 없는 변환입니다: {meta['transform']} (가능: {TRANSFORMS})")
        if meta["weight"] not in WEIGHTS:
            raise ConfigError(f"factors.{fid}.weight 는 근거 등급 {WEIGHTS} 중 하나여야 합니다: {meta['weight']}")
        if meta["sign"] not in (1, -1):
            raise ConfigError(f"factors.{fid}.sign 은 +1 또는 -1 이어야 합니다: {meta['sign']}")
        if not isinstance(meta["horizon"], int) or meta["horizon"] <= 0:
            raise ConfigError(f"factors.{fid}.horizon 은 양의 거래일 수여야 합니다: {meta['horizon']}")
        stages = meta.get("stages")
        if stages is not None:
            if not isinstance(stages, (list, tuple)) or not stages:
                raise ConfigError(f"factors.{fid}.stages 는 비어 있지 않은 목록이어야 합니다")
            for st in stages:
                if st not in STAGES:
                    raise ConfigError(f"factors.{fid}.stages 에 알 수 없는 단계: {st} (가능: {STAGES})")
        params = meta.get("params")
        if params is not None and not isinstance(params, dict):
            raise ConfigError(f"factors.{fid}.params 는 매핑이어야 합니다")
    return cfg


def factor_ids(cfg, layer=None, stage=None):
    """조건에 맞는 요인 id 목록 (설정 파일의 등장 순서를 지킨다).

    stages 가 없는 요인은 모든 단계에서 계산한다. stages 가 있으면 그 단계에서만 값이 있고
    다른 단계에서는 결측이다 — mkt_overnight 이 prelim 에서 빠지는 것이 곧 실험 설계다 (결정 11).
    """
    out = []
    for fid, meta in (cfg.get("factors") or {}).items():
        if layer is not None and meta.get("layer") != layer:
            continue
        stages = meta.get("stages")
        if stage is not None and stages is not None and stage not in stages:
            continue
        out.append(fid)
    return out


def resolve_path(cfg, key, root=None):
    """paths 의 상대 경로를 저장소 루트 기준 절대 경로로 바꾼다 (cwd 에 의존하지 않기 위해)."""
    raw = (cfg.get("paths") or {}).get(key)
    if raw is None:
        raise ConfigError(f"필수 키가 없습니다: paths.{key}")
    base = Path(root) if root else DEFAULT_CONFIG_PATH.parent.parent
    p = Path(raw)
    return p if p.is_absolute() else (base / p).resolve()


def llm_cost_usd(cfg, model, tokens_in, tokens_out):
    """호출 비용(USD). 단가가 아직 안 채워졌으면 값을 지어내지 않고 None 을 준다.

    price_per_mtok 에 null 자리만 잡아 둔 상태에서 0 으로 계산하면 하루 예산 검사가
    '항상 예산 안'이라고 거짓말을 한다 (결정 15의 비용 상한). 그래서 None 을 그대로 흘린다.
    """
    price = ((cfg.get("llm") or {}).get("price_per_mtok") or {}).get(model)
    if not isinstance(price, dict):
        return None
    p_in, p_out = price.get("in"), price.get("out")
    if p_in is None or p_out is None:
        return None
    per_mtok = 1_000_000.0
    return (float(tokens_in or 0) * float(p_in) + float(tokens_out or 0) * float(p_out)) / per_mtok


__all__ = ["ConfigError", "DEFAULT_CONFIG_PATH", "TRANSFORMS", "LAYERS", "STAGES", "WEIGHTS",
           "load_config", "config_yaml_text", "config_hash", "validate_config",
           "factor_ids", "resolve_path", "llm_cost_usd"]
