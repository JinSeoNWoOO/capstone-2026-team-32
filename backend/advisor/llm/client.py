"""LLM 호출부 (설계 9장, 결정 15). 없앤 뉴스 자동매매의 `newsgap/ai_judge.py`(git 이력에 있다)
호출·파싱 패턴을 배치용으로 일반화했다.
가져온 것: 비동기 SDK 호출, JSON 응답 강제, 방어적 JSON 추출, **예외를 던지지 않고 ERROR 반환**,
프롬프트 버전. 바뀌는 것은 설계 9장의 표 그대로다.

| 항목 | 옛 뉴스 판정 (`ai_judge`) | 여기 |
|---|---|---|
| 출력 | BUY/SKIP 고정 | 작업별 `response_schema` + **우리가 다시 검증** |
| 기록 | 디버그 로그 | `llm_call` 한 행 (입력·출력·토큰·비용·지연·상태) |
| 캐시 | 없음 | `input_hash` 일치 시 저장된 결과 재사용 |
| 예산 | 없음 | 하루 비용 합 + 하루 호출 수 상한 |

이 모듈의 기본 태도는 **기권(abstain)** 이다. 시간 초과·API 오류·스키마 위반은 전부 `ERROR` 로
돌려주고 호출부는 v0 값을 그대로 쓴다 (결정 15의 2번). ERROR 를 "중립(0점)"으로 바꾸면
"LLM 이 도움이 됐는가"(결정 4) 비교가 오염되므로 둘을 절대 섞지 않는다.

**스키마 검증을 SDK 에 맡기지 않는 이유**: `response_schema` 는 서버가 지켜 주지만, 안전필터로
응답이 비거나 방어적 추출 경로로 떨어지면 검증되지 않은 객체가 들어온다. 값의 범위(−2..2, 0..1)와
열거값을 여기서 한 번 더 보고, 어기면 **점수를 추측하지 않고 기권**한다 (설계 12장의 위험 표).
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..config import llm_cost_usd

log = logging.getLogger("advisor.llm")

KST = timezone(timedelta(hours=9))

# advisor.config.yaml 은 수집 담당이 소유해 이 작업에서 고칠 수 없다. 그래서 아직 YAML 에 없는
# 조정값은 **이 한 곳에** 모아 두고 tunable() 로만 읽는다. 나중에 같은 키를 YAML 에 넣으면
# 코드를 고치지 않아도 그쪽이 이긴다 (factors/asof.py 와 risk_flags.py 가 쓰는 방식과 같다).
DEFAULTS = {
    # --- 호출 한도 -------------------------------------------------------------
    # 단가가 아직 null 이라 비용 합이 None 으로 남는다 (config.llm_cost_usd). 비용만으로 막으면
    # "모르는 단가"가 무한히 돌 수 있어 호출 수 상한을 함께 둔다 (결정 15의 5번).
    "llm.max_calls_per_day": 400,
    # 한 실행에서 공시 채점·뉴스 분류에 쓸 최대 호출 수. 공시가 폭주한 날 하루 예산을 한 번에
    # 태우지 않게 하는 안전판이다 (넘는 건은 채점하지 않고 결측으로 남는다).
    "llm.disclosure_max_calls": 40,
    "llm.news_max_calls": 8,
    # --- 공시 채점 (설계 5.4) --------------------------------------------------
    # 공급계약을 '대규모'로 볼 최근매출액 대비 비율(%). 기준표의 supply_large/supply_small 경계다.
    "llm.supply_large_ratio_pct": 10.0,
    # first_seen_at 이 비어 있는 공시를 몇 시에 알았다고 볼 것인가. DART 배포가 18:00 에 끝나므로
    # 실제보다 늦으면 늦었지 빠르지 않다 (risk_flags.py 가 쓰는 규칙과 같은 값).
    "llm.disclosure_fallback_hour": 18,
    # 프롬프트에 넣을 공시 본문 길이 상한. LS 공시 본문은 대개 비어 있어 제목만으로 채점하는 것이 정상이다.
    "llm.body_max_chars": 1200,
    # 코드가 보는 지배구조 위험 문구. LLM 이 본문에서 같은 말을 인용해도 같은 표시를 붙인다 (설계 5.5).
    "llm.governance_pattern": "횡령|배임|감사의견|의견\\s*거절|부적정|한정",
    # --- 뉴스 분류 (설계 5.2의 news_risk) --------------------------------------
    # 한 번에 묶어 보내는 헤드라인 수와 한 실행의 상한. 건수가 많고 단순한 작업이라 묶을수록 싸다.
    "llm.news_batch_size": 30,
    "llm.news_max_items": 120,
    # 이미 일어난 가격·거래량을 전하는 자동 기사. ai_judge 프롬프트의 SKIP 목록을 규칙으로 옮긴 것이다.
    # 이런 제목은 부르기 전에 걸러 호출 수를 줄인다 — 놓치는 진짜 재료가 있을 수 있으나 news_risk 는
    # 가중치 0 인 관찰 요인이라 비용을 아끼는 쪽을 택했다 (결정 12).
    # '급등·강세' 같은 낱말은 단독으로 넣지 않는다. "유가 급등"·"원화 약세"처럼 진짜 사건을 전하는
    # 제목이 통째로 걸려 나가기 때문이다 — 합성어(급등세·급락세)와 지표 표현만 잡는다.
    "llm.news_noise_pattern": (
        "특징주|시황|증시\\s*(마감|개장|출발)|장\\s*마감|마감\\s*시황|수급\\s*포착|순매수|순매도|"
        "신고가|신저가|52주|거래량\\s*급증|급등세|급락세|급등락|상한가|하한가|"
        "\\d+\\s*거래일\\s*연속|[-+]?\\d+(\\.\\d+)?\\s*%\\s*(상승|하락|↑|↓)|코스피\\s*\\d|코스닥\\s*\\d"),
    # 직전 실행 기록이 없을 때 거슬러 올라갈 시간. YAML 의 sources.news_window_hours 가 있으면 그쪽이 이긴다.
    "sources.news_window_hours": 24,
    # --- 조정 (설계 5.6) -------------------------------------------------------
    # 한 번에 보여 줄 자산 수와 호출 수 상한. 설계는 "1~2회로 끝낸다"이다 (비용·일관성).
    "llm.adjust_max_assets_per_call": 20,
    "llm.adjust_max_calls": 2,
    # 근거 요약(factor_evidence.summary, 프롬프트의 최근 공시·뉴스 줄) 길이 상한.
    "llm.evidence_max_chars": 120,
}

# 모델별 학습 데이터 기준일 (결정 15의 6번). 실시간 기록은 항상 기준일 이후라 1차에서는 쓸 일이 없고,
# 과거 재현에서만 필요하다 — 재현 모드는 애초에 LLM 단계를 건너뛴다 (설계 7.4).
# 공식 모델 카드에서 기준일이 확인되면 여기에 (날짜, 출처 URL) 을 채운다. 비워 두면
# model_registry.training_cutoff 는 NULL 로 남는다 — 모르는 값을 지어내지 않는다.
MODEL_TRAINING_CUTOFF = {
    # "gemini-3.5-flash":      ("YYYY-MM-DD", "https://ai.google.dev/gemini-api/docs/models"),
    # "gemini-3.5-flash-lite": ("YYYY-MM-DD", "https://ai.google.dev/gemini-api/docs/models"),
}

STATUS_OK = "OK"
STATUS_ERROR = "ERROR"
STATUS_BUDGET = "SKIPPED_BUDGET"
STATUS_DISABLED = "DISABLED"


def tunable(cfg, dotted, defaults=DEFAULTS):
    """설정값을 점 표기로 읽고, 없으면 DEFAULTS 를 쓴다 (factors/asof.py 의 같은 이름 함수와 같은 규칙)."""
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return defaults[dotted]
        node = node[part]
    return defaults[dotted] if node is None else node


def to_datetime(x):
    """date | datetime | 'YYYY-MM-DD[THH:MM[:SS]]' → KST naive datetime.

    시각이 없는 날짜는 그날 00:00 으로 본다 — 날짜만 준 as_of 가 가장 보수적인 값이 되게 한다
    (factors/asof.py 의 같은 규칙. 시점 비교가 두 모듈에서 어긋나면 안 된다)."""
    if isinstance(x, datetime):
        return x.replace(tzinfo=None) if x.tzinfo is None else x.astimezone(KST).replace(tzinfo=None)
    if hasattr(x, "year") and hasattr(x, "day") and not isinstance(x, str):
        return datetime(x.year, x.month, x.day)
    if isinstance(x, str):
        s = x.strip().replace(" ", "T")
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
    raise TypeError(f"시각으로 볼 수 없습니다: {x!r}")


def clip_text(s, n=200):
    """줄바꿈을 접고 n 자로 자른다. 기록 열이 프롬프트만큼 길어지는 것을 막는다."""
    return " ".join(str(s or "").split())[:n]


def canonical(obj):
    """해시·저장용 정규 JSON (키 정렬, 공백 없음). 같은 입력이 같은 해시가 되게 하는 유일한 규칙이다."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def input_hash(model, prompt_ver, task, system, user, schema):
    """캐시 키 (설계 9장). 모델·프롬프트 버전·작업·입력·스키마 중 하나만 달라도 다른 호출이다.

    스키마를 해시에 넣는 이유: 같은 문장을 보내도 출력 스키마가 바뀌면 결과의 모양이 달라진다.
    프롬프트 버전만 믿으면 스키마만 고친 날 옛 결과가 되살아난다.
    """
    h = hashlib.sha256()
    for part in (model, prompt_ver, task, system, user, canonical(schema)):
        h.update(str(part).encode("utf-8"))
        h.update(b"\x00")               # 구분자가 없으면 이어 붙인 문자열이 우연히 같아질 수 있다
    return h.hexdigest()


# ------------------------------------------------------------------ 방어적 파싱 (ai_judge 에서 가져옴)

def extract_json(text):
    """응답에서 첫 JSON 값을 꺼낸다. 코드펜스·앞뒤 잡문 허용. 실패하면 None.

    `ai_judge._extract_json` 과 같은 방식이되 **배열도 받는다** — 뉴스 분류·조정은 목록을 낸다.
    구조화 출력이 켜져 있어도 남겨 두는 이유는 안전필터·잘린 응답 같은 예외 경로 때문이다.
    """
    if not text:
        return None
    t = re.sub(r"```[a-zA-Z]*", "", text).replace("```", "")
    try:
        return json.loads(t)
    except Exception:
        pass
    openers = {"{": "}", "[": "]"}
    i = 0
    while i < len(t):
        ch = t[i]
        if ch not in openers:
            i += 1
            continue
        depth, in_str, esc = 0, False, False
        for j in range(i, len(t)):
            c = t[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c in openers:
                depth += 1
            elif c in ("}", "]"):
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[i:j + 1])
                    except Exception:
                        break
        i += 1
    return None


def resp_text(resp):
    """GenerateContentResponse.text. 안전필터 등으로 후보가 비면 빈 문자열 (ai_judge 와 같다)."""
    try:
        return getattr(resp, "text", None) or ""
    except Exception:
        return ""


def finish_detail(resp):
    """텍스트가 비었을 때 이유(finish_reason / prompt_feedback)를 에러 메시지에 남긴다."""
    bits = []
    for c in getattr(resp, "candidates", None) or []:
        fr = getattr(c, "finish_reason", None)
        if fr:
            bits.append(f"finish={fr}")
    pf = getattr(resp, "prompt_feedback", None)
    if pf is not None:
        br = getattr(pf, "block_reason", None)
        if br:
            bits.append(f"block={br}")
    return " ".join(bits)


# ------------------------------------------------------------------ 스키마 검증

class SchemaError(ValueError):
    """모델 출력이 작업 스키마를 어겼다. 값을 고쳐 쓰지 않고 그 호출을 기권시킨다."""


def validate(value, schema, path="$"):
    """스키마에 맞는 값만 통과시키고 **선언된 키만 남겨** 돌려준다. 어기면 SchemaError.

    JSON 스키마 전부가 아니라 이 프로젝트가 쓰는 만큼만 본다:
    type(object/array/string/number/integer/boolean) · properties · required · enum ·
    minimum/maximum · items · minItems/maxItems.

    선언되지 않은 키를 버리는 이유는 프롬프트를 고칠 때 모델이 덧붙인 임시 필드가 저장 구조로
    새는 것을 막기 위해서다. 정수 자리에 3.0 이 오면 정수로 받아들이지만(JSON 에는 정수 표기가
    없는 구현이 있다) 3.5 는 어긴 것으로 본다.
    """
    typ = schema.get("type")
    if typ == "object":
        if not isinstance(value, dict):
            raise SchemaError(f"{path}: 객체가 아닙니다 ({type(value).__name__})")
        props = schema.get("properties") or {}
        for key in schema.get("required") or ():
            if key not in value or value[key] is None:
                raise SchemaError(f"{path}.{key}: 필수 키가 없습니다")
        out = {}
        for key, sub in props.items():
            if key in value and value[key] is not None:
                out[key] = validate(value[key], sub, f"{path}.{key}")
        return out
    if typ == "array":
        if not isinstance(value, list):
            raise SchemaError(f"{path}: 배열이 아닙니다 ({type(value).__name__})")
        lo, hi = schema.get("minItems"), schema.get("maxItems")
        if lo is not None and len(value) < int(lo):
            raise SchemaError(f"{path}: 항목이 {len(value)}개뿐입니다 (최소 {lo})")
        if hi is not None and len(value) > int(hi):
            raise SchemaError(f"{path}: 항목이 {len(value)}개입니다 (최대 {hi})")
        item_schema = schema.get("items") or {}
        return [validate(v, item_schema, f"{path}[{i}]") for i, v in enumerate(value)]
    if typ == "boolean":
        if not isinstance(value, bool):
            raise SchemaError(f"{path}: 참/거짓이 아닙니다 ({value!r})")
        return value
    if typ in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SchemaError(f"{path}: 수가 아닙니다 ({value!r})")
        num = float(value)
        if num != num:                                   # NaN
            raise SchemaError(f"{path}: NaN")
        if typ == "integer":
            if abs(num - round(num)) > 1e-9:
                raise SchemaError(f"{path}: 정수가 아닙니다 ({value!r})")
            num = int(round(num))
        lo, hi = schema.get("minimum"), schema.get("maximum")
        if lo is not None and num < float(lo):
            raise SchemaError(f"{path}: {num} 은 최소 {lo} 미만입니다")
        if hi is not None and num > float(hi):
            raise SchemaError(f"{path}: {num} 은 최대 {hi} 초과입니다")
        return num
    if typ == "string":
        if not isinstance(value, str):
            raise SchemaError(f"{path}: 문자열이 아닙니다 ({type(value).__name__})")
        enum = schema.get("enum")
        if enum is not None and value not in enum:
            raise SchemaError(f"{path}: 허용되지 않은 값 {value!r}")
        return value
    enum = schema.get("enum")                            # type 없이 enum 만 준 경우
    if enum is not None and value not in enum:
        raise SchemaError(f"{path}: 허용되지 않은 값 {value!r}")
    return value


# ------------------------------------------------------------------ 결과

@dataclass
class LLMResult:
    """호출 한 번의 결과. **예외 대신 이 값으로만 실패를 알린다.**

    status: OK | ERROR | SKIPPED_BUDGET | DISABLED
      - ERROR  : 불렀는데 실패했다 (시간 초과·API 오류·스키마 위반) → 기권
      - SKIPPED_BUDGET : 예산·호출 수 상한에 걸려 **부르지 않았다**
      - DISABLED : 설정에서 껐거나 키가 없다 → 그날은 v0 만 기록된다
    """

    status: str
    data: object = None                 # 검증을 통과한 파싱 결과 (OK 일 때만)
    call_id: str = None
    cache_hit: bool = False
    error: str = ""
    model: str = ""
    latency_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = None

    @property
    def ok(self):
        return self.status == STATUS_OK


@dataclass
class CallStats:
    """한 실행에서 쌓인 호출 통계. run.note·리포트의 '오늘 LLM 비용'이 이걸 쓴다 (설계 10.1)."""

    n_calls: int = 0                    # 기록된 호출 (캐시·기권 포함)
    n_api: int = 0                      # 실제로 API 를 때린 호출
    n_cache: int = 0
    n_error: int = 0
    cost: float = 0.0                   # 단가를 아는 호출의 비용 합
    cost_unknown: int = 0               # 단가를 몰라 비용을 못 매긴 호출 수
    statuses: dict = field(default_factory=dict)

    def add(self, res):
        self.n_calls += 1
        self.statuses[res.status] = self.statuses.get(res.status, 0) + 1
        if res.cache_hit:
            self.n_cache += 1
        elif res.status in (STATUS_OK, STATUS_ERROR):
            self.n_api += 1
        if res.status == STATUS_ERROR:
            self.n_error += 1
        if res.cost_usd is None:
            if not res.cache_hit and res.status in (STATUS_OK, STATUS_ERROR):
                self.cost_unknown += 1
        else:
            self.cost += float(res.cost_usd)
        return res


# ------------------------------------------------------------------ 클라이언트

class LLMClient:
    """작업별 스키마 호출 + 기록 + 캐시 + 예산. **call() 은 절대 예외를 던지지 않는다.**

    client 를 넣으면 그대로 쓴다(테스트용 fake). google-genai 의 `genai.Client` 와 같은 모양이면 된다:
    `await client.aio.models.generate_content(model=, contents=, config=)` → `.text` 와 `.usage_metadata`.
    """

    def __init__(self, cfg, store, run_id=None, client=None):
        self.cfg = cfg or {}
        self.store = store
        self.run_id = run_id
        llm = self.cfg.get("llm") or {}
        self.llm_cfg = llm
        self.models = dict(llm.get("models") or {})
        self.prompt_ver = str(llm.get("prompt_ver") or "v1")
        self.timeout_sec = float(llm.get("timeout_sec", 60))
        self.retries = int(llm.get("retries", 1))
        self.temperature = float(llm.get("temperature", 0.0))
        self.daily_budget = llm.get("daily_budget_usd")
        self.max_calls_per_day = int(tunable(self.cfg, "llm.max_calls_per_day"))
        self.stats = CallStats()
        self._loop = None
        self._registered = set()
        self._client = client
        self.disabled_reason = None
        if client is None:
            if not llm.get("enabled", True):
                self.disabled_reason = "설정에서 껐습니다 (llm.enabled=false)"
            elif not self._api_key():
                self.disabled_reason = f"{llm.get('api_key_env') or 'GEMINI_API_KEY'} 가 없습니다"

    # -------------------------------------------------------------- 준비

    def _api_key(self):
        """.env 를 포함한 환경변수에서 키를 찾는다 (newsgap 의 load_env_file 재사용, 결정 15).

        키 값은 어디에도 기록하지 않는다 — llm_call 에는 프롬프트만 남는다.
        """
        env_name = (self.llm_cfg.get("api_key_env") or "GEMINI_API_KEY")
        key = os.environ.get(env_name) or os.environ.get("GOOGLE_API_KEY")
        if key:
            return key.strip()
        try:
            from ...newsgap.ls_client import load_env_file
            load_env_file(".env")
        except Exception:
            return None
        key = os.environ.get(env_name) or os.environ.get("GOOGLE_API_KEY")
        return key.strip() if key else None

    @property
    def enabled(self):
        return self.disabled_reason is None

    def _ensure_client(self):
        """SDK 클라이언트를 처음 쓸 때 만든다. google.genai import 가 가볍지 않아 미룬다."""
        if self._client is not None:
            return self._client
        key = self._api_key()
        if not key:
            self.disabled_reason = f"{self.llm_cfg.get('api_key_env') or 'GEMINI_API_KEY'} 가 없습니다"
            return None
        try:
            from google import genai
            # HttpOptions.timeout 단위는 밀리초. 실제 상한은 _acall 의 wait_for 다 (ai_judge 와 같은 이유).
            self._client = genai.Client(
                api_key=key, http_options={"timeout": int(self.timeout_sec * 1000) + 5000})
        except Exception as exc:                        # SDK 가 없거나 키가 형식을 어긴 경우
            self.disabled_reason = f"SDK 초기화 실패: {type(exc).__name__}: {exc}"
            log.warning("LLM 사용 불가 → v0 만 기록합니다: %s", self.disabled_reason)
            return None
        return self._client

    def close(self):
        """소유한 이벤트 루프를 닫는다. 배치가 끝날 때 부른다 (안 불러도 프로세스 종료로 정리된다)."""
        if self._loop is not None:
            try:
                self._loop.close()
            finally:
                self._loop = None

    # -------------------------------------------------------------- 예산

    def budget_today(self):
        """오늘(KST) 실제로 API 를 때린 호출의 (비용 합, 단가 미확인 건수, 호출 수).

        '오늘'은 as_of 가 아니라 **벽시계**다. 예산은 실제로 나가는 돈에 대한 상한이라
        과거 날짜를 재현하는 실행도 같은 지갑을 쓴다.
        """
        day = datetime.now(KST).strftime("%Y-%m-%d")
        row = self.store.conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0.0) AS cost, "
            "SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS unknown, COUNT(*) AS n "
            "FROM llm_call WHERE substr(created_at,1,10)=? AND cache_hit=0 AND status IN (?,?)",
            (day, STATUS_OK, STATUS_ERROR)).fetchone()
        return float(row["cost"] or 0.0), int(row["unknown"] or 0), int(row["n"] or 0)

    def over_budget(self):
        """예산·호출 수 상한에 걸렸으면 사유 문자열, 아니면 None.

        단가가 아직 null 이면 비용 합이 0 으로 남는다 (config.llm_cost_usd 가 None 을 흘린다).
        그 상태에서 비용만 보면 '항상 예산 안'이라는 거짓말이 되므로 호출 수 상한을 함께 본다.
        """
        cost, unknown, n = self.budget_today()
        if self.daily_budget is not None and cost >= float(self.daily_budget):
            return f"하루 예산 ${float(self.daily_budget):.2f} 소진 (오늘 ${cost:.4f}, {n}회)"
        if n >= self.max_calls_per_day:
            return (f"하루 호출 수 상한 {self.max_calls_per_day}회 도달 "
                    f"(오늘 {n}회, 단가 미확인 {unknown}회)")
        return None

    # -------------------------------------------------------------- 기록

    def _register_model(self, model):
        """이 실행에서 처음 쓰는 모델이면 model_registry 에 자리를 만든다 (결정 15의 6번).

        학습 기준일을 모르면 NULL 로 남긴다 — 모르는 값을 지어내면 기억 누출 점검이 무의미해진다.
        아는 값이 생기면 MODEL_TRAINING_CUTOFF 에 채우면 된다.
        """
        if model in self._registered:
            return
        cutoff, url = MODEL_TRAINING_CUTOFF.get(model, (None, None))
        try:
            self.store.conn.execute(
                "INSERT OR IGNORE INTO model_registry(model,training_cutoff,source_url,noted_at) "
                "VALUES(?,?,?,?)", (model, cutoff, url, self.store.now_wall()))
        except Exception as exc:                        # 기록 실패가 판단을 막지는 않는다
            log.warning("model_registry 기록 실패(%s): %s", model, exc)
        self._registered.add(model)

    def _write_call(self, task, model, ih, system, user, output_text, parsed, status,
                    tokens_in=0, tokens_out=0, cost=None, latency_ms=0.0, cache_hit=False):
        """llm_call 한 행. 캐시 재사용도 **행을 새로 남긴다** — 그래야 실행별 호출 수가 온전하다."""
        call_id = uuid.uuid4().hex
        try:
            self.store.conn.execute(
                "INSERT INTO llm_call(call_id,run_id,task,model,prompt_ver,input_hash,input_text,"
                "output_text,parsed_json,status,tokens_in,tokens_out,cost_usd,latency_ms,cache_hit,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (call_id, self.run_id, task, model, self.prompt_ver, ih,
                 f"[system]\n{system}\n[user]\n{user}", output_text,
                 canonical(parsed) if parsed is not None else None, status,
                 int(tokens_in or 0), int(tokens_out or 0), cost, float(latency_ms or 0.0),
                 1 if cache_hit else 0, self.store.now_wall()))
        except Exception as exc:
            log.warning("llm_call 기록 실패(task=%s): %s", task, exc)
            return None
        return call_id

    # -------------------------------------------------------------- 호출

    def _run(self, coro):
        """비동기 SDK 호출을 동기로 감싼다. 배치는 평범한 스크립트라 이벤트 루프가 없다.

        호출마다 `asyncio.run` 을 부르면 호출 수만큼 루프를 만들고 버린다. 그래서
        **클라이언트 하나가 루프 하나를 소유**하고 배치 내내 재사용한다(close 에서 닫는다).
        이미 돌고 있는 루프 안에서 불린 경우(백엔드 스레드 등)에는 그 루프를 막을 수 없으므로
        별도 스레드에서 asyncio.run 으로 돌린다 — 배치에서는 타지 않는 경로다.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
            return self._loop.run_until_complete(coro)
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(asyncio.run, coro).result()

    async def _acall(self, model, system, user, schema):
        gen_config = {
            "system_instruction": system,
            "response_mime_type": "application/json",   # JSON 강제. 그래도 파싱은 방어적으로 한다.
            "response_schema": schema,                  # 구조화 출력 (결정 15의 1번)
            "temperature": self.temperature,
        }
        return await asyncio.wait_for(
            self._client.aio.models.generate_content(model=model, contents=user, config=gen_config),
            self.timeout_sec)

    def _usage(self, resp):
        """토큰 수. **사고(thoughts) 토큰은 출력에 더한다** — 출력 단가로 과금되기 때문이다."""
        u = getattr(resp, "usage_metadata", None)
        if u is None:
            return 0, 0
        def n(name):
            try:
                return int(getattr(u, name, 0) or 0)
            except (TypeError, ValueError):
                return 0
        return n("prompt_token_count"), n("candidates_token_count") + n("thoughts_token_count")

    def call(self, task, system, user, schema, model_key):
        """작업 하나를 부르고 결과를 돌려준다. 절대 예외를 던지지 않는다.

        model_key 는 설정의 llm.models 키(classify|adjust)다. 키가 없으면 그 문자열을 모델 id 로 본다.
        """
        model = self.models.get(model_key) or model_key
        ih = input_hash(model, self.prompt_ver, task, system, user, schema)

        if not self.enabled:
            call_id = self._write_call(task, model, ih, system, user, None, None, STATUS_DISABLED)
            return self.stats.add(LLMResult(STATUS_DISABLED, None, call_id, False,
                                            self.disabled_reason or "", model))
        self._register_model(model)

        cached = self._cache_lookup(ih, schema)
        if cached is not None:
            call_id = self._write_call(task, model, ih, system, user, None, cached, STATUS_OK,
                                       tokens_in=0, tokens_out=0, cost=0.0, cache_hit=True)
            return self.stats.add(LLMResult(STATUS_OK, cached, call_id, True, "", model,
                                            cost_usd=0.0))

        reason = self.over_budget()
        if reason:
            log.warning("LLM 건너뜀(%s): %s", task, reason)
            call_id = self._write_call(task, model, ih, system, user, None, None, STATUS_BUDGET)
            return self.stats.add(LLMResult(STATUS_BUDGET, None, call_id, False, reason, model))

        if self._ensure_client() is None:               # 키·SDK 문제는 여기서만 드러날 수 있다
            call_id = self._write_call(task, model, ih, system, user, None, None, STATUS_DISABLED)
            return self.stats.add(LLMResult(STATUS_DISABLED, None, call_id, False,
                                            self.disabled_reason or "", model))

        text, err, resp, latency = "", "", None, 0.0
        for attempt in range(max(1, self.retries + 1)):
            t0 = time.monotonic()
            try:
                resp = self._run(self._acall(model, system, user, schema))
                err = ""
            except asyncio.TimeoutError:
                err = f"timeout {self.timeout_sec}s"
            except Exception as exc:                    # API·네트워크 오류. 배치를 막지 않는다.
                err = f"{type(exc).__name__}: {exc}"
            latency = (time.monotonic() - t0) * 1000.0
            if not err:
                break
            log.warning("LLM 호출 실패(task=%s, %d/%d): %s", task, attempt + 1,
                        max(1, self.retries + 1), clip_text(err, 200))

        tokens_in, tokens_out = self._usage(resp) if resp is not None else (0, 0)
        cost = llm_cost_usd(self.cfg, model, tokens_in, tokens_out)

        def fail(message):
            call_id = self._write_call(task, model, ih, system, user, text, None, STATUS_ERROR,
                                       tokens_in, tokens_out, cost, latency)
            return self.stats.add(LLMResult(STATUS_ERROR, None, call_id, False, clip_text(message),
                                            model, latency, tokens_in, tokens_out, cost))

        if err:
            return fail(err)
        text = resp_text(resp)
        obj = extract_json(text)
        if obj is None:
            return fail(f"unparsable: {clip_text(text) or finish_detail(resp) or 'empty response'}")
        # 스키마 위반은 **추측하지 않고 기권**한다 (설계 12장: 구조화 출력이 스키마를 어기는 경우)
        try:
            data = validate(obj, schema)
        except SchemaError as exc:
            return fail(f"schema: {exc}")
        call_id = self._write_call(task, model, ih, system, user, text, data, STATUS_OK,
                                   tokens_in, tokens_out, cost, latency)
        return self.stats.add(LLMResult(STATUS_OK, data, call_id, False, "", model, latency,
                                        tokens_in, tokens_out, cost))

    def _cache_lookup(self, ih, schema):
        """같은 입력으로 성공한 호출의 결과. 없거나 지금 스키마를 못 지키면 None.

        예비(18:30)와 최종(07:40)은 같은 공시·뉴스를 다시 본다. 그때 같은 값을 두 번 사는 것을
        막는 것이 이 캐시의 목적이다 (결정 11·15).
        """
        try:
            row = self.store.conn.execute(
                "SELECT parsed_json FROM llm_call WHERE input_hash=? AND status=? AND parsed_json "
                "IS NOT NULL ORDER BY created_at LIMIT 1", (ih, STATUS_OK)).fetchone()
        except Exception as exc:
            log.warning("캐시 조회 실패: %s", exc)
            return None
        if row is None:
            return None
        try:
            return validate(json.loads(row["parsed_json"]), schema)
        except Exception:
            return None                                 # 옛 결과가 지금 스키마를 못 지키면 다시 부른다


__all__ = ["DEFAULTS", "LLMClient", "LLMResult", "CallStats", "SchemaError", "STATUS_BUDGET",
           "STATUS_DISABLED", "STATUS_ERROR", "STATUS_OK", "canonical", "clip_text",
           "extract_json", "input_hash", "to_datetime", "tunable", "validate"]
