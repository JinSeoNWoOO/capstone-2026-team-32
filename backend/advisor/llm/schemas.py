"""작업별 출력 스키마 (결정 15의 1번: 정규식으로 자유 서술을 파싱하지 않는다).

스키마를 함수로 만드는 이유는 **허용값이 설정에서 온다**는 것 하나다.
  - 공시: rubric_id 는 `disclosure_rubric` 에 실제로 있는 id 만.
  - 뉴스: sectors 는 그날 대상 목록의 섹터 이름만 (설정의 sector_etfs 키).
  - 조정: code 는 후보로 보낸 자산만, adj 는 설정의 `llm.adj_cap` 안에서만.
열거값을 스키마에 박아 두면 서버가 구조를 지켜 주고, 그래도 어긋난 값은 client.validate 가 잡는다.

타입은 JSON 스키마 표기(소문자)를 쓴다. google-genai 2.23 이 이 형태의 dict 를 response_schema 로
그대로 받는 것을 실호출로 확인했다(2026-09-22, gemini-3.5-flash-lite).
`propertyOrdering` 은 Gemini 가 필드 순서를 흔들지 않게 하는 힌트다 — 같은 입력이 같은 출력이 돼야
입력 해시 캐시(설계 9장)가 의미를 가진다.

## 배열에 `maxItems` 를 달지 않는다 (2026-09-22 실호출로 확인)

서버는 제약 디코딩을 위해 배열을 `maxItems` 만큼 펼쳐 두는 듯하고, 그 크기가 커지면 요청 자체를
**400 INVALID_ARGUMENT** 로 물린다(메시지는 "Request contains an invalid argument." 한 줄뿐이라
어느 칸이 문제인지 알려 주지 않는다). 실측: 조정 스키마에 요인 10개·코드 20개를 넣고
`maxItems=20` 을 달면 실패하고, 19 면 통과한다. `maxItems` 만 빼면 코드 40개도 통과한다.
뉴스 스키마도 `maxItems=30` 에서 같은 400 이 났다.

건수 제한은 스키마가 아니라 코드가 한다 — 조정은 `adjust.postprocess` 가 모르는 code 를 버리고
code 를 키로 합치며, 뉴스는 `news_risk` 가 모르는 id 를 버린다. 그래서 상한을 스키마에서 빼도
"후보 밖 자산을 만들어 낼 수 없다"는 계약은 그대로다.
"""

# 작업 이름. llm_call.task 에 그대로 들어가고 비용·건수 집계의 단위가 된다.
TASK_DISCLOSURE = "disclosure"
TASK_NEWS = "news"
TASK_ADJUST = "adjust"

# 뉴스 분류의 사건 종류 (결정 12: 전쟁·재난·정책·핵심 인사 발언 + 그 밖).
#   noise = 이미 일어난 가격·거래량을 전하는 자동 기사. 방향 0 으로 두고 점수에 넣지 않는다.
NEWS_CATEGORIES = ("war", "disaster", "policy", "key_person", "macro", "company", "noise")

# 기준표 수준의 폭 (−2..+2)과 점수 눈금의 폭 (−1..+1). 설계 5.1 의 눈금 정의 그 자체라 설정값이 아니다.
LEVEL_MIN, LEVEL_MAX = -2, 2
SCORE_SCALE = 1.0


def disclosure_schema(rubric_ids):
    """공시 한 건 → {level, rubric_id, evidence, confidence}.

    level 과 rubric_id 를 둘 다 받는 이유: 기준표에 값이 고정된 항목은 코드가 level 을 덮어쓰고,
    `other_material` 처럼 '수준을 LLM 이 정한다'고 적힌 항목만 모델의 수를 쓴다 (설계 5.4).
    confidence 는 낮은 확신을 그대로 드러내라고 두는 자리다 — 기록만 하고 점수는 깎지 않는다.
    """
    return {
        "type": "object",
        "properties": {
            "level": {"type": "integer", "minimum": LEVEL_MIN, "maximum": LEVEL_MAX},
            "rubric_id": {"type": "string", "enum": list(rubric_ids)},
            "evidence": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        },
        "required": ["level", "rubric_id", "evidence", "confidence"],
        "propertyOrdering": ["level", "rubric_id", "evidence", "confidence"],
    }


def news_schema(sectors):
    """헤드라인 묶음 → 항목별 분류 목록.

    id 를 되돌려 받는 이유는 입력과 출력을 1:1 로 맞추기 위해서다. 순서가 바뀌거나 몇 건이
    빠져도 id 로 다시 붙일 수 있고, 모르는 id 는 버린다.
    direction 은 그 섹터 주가에 미칠 방향(−2..+2), severity 는 사건의 크기(0~3)다.
    두 값을 나눠 받는 이유는 "크지만 방향을 모르는 사건"을 0 방향 + 높은 severity 로 남기기 위해서다.

    건수 상한은 달지 않는다 (머리말: `maxItems` 는 400 을 부른다). 묶음 크기는 부르는 쪽이
    `llm.news_batch_size` 로 정하고, 모르는 id 는 `news_risk` 가 버린다.
    """
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "category": {"type": "string", "enum": list(NEWS_CATEGORIES)},
                "sectors": {"type": "array", "items": {"type": "string", "enum": list(sectors)}},
                "direction": {"type": "integer", "minimum": LEVEL_MIN, "maximum": LEVEL_MAX},
                "severity": {"type": "integer", "minimum": 0, "maximum": 3},
                "theme_suspect": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["id", "category", "sectors", "direction", "severity", "theme_suspect",
                         "reason"],
            "propertyOrdering": ["id", "category", "sectors", "direction", "severity",
                                 "theme_suspect", "reason"],
        },
    }


def adjust_schema(codes, factor_ids, cap):
    """자산별 조정 → {code, adj, veto, adopted, rejected, reason} 목록 (설계 5.6).

    adopted·rejected 를 필수로 두는 것이 결정 4의 '기록 의무'다. 이유 없이 점수를 움직이면
    사후에 "무엇을 보고 그랬는가"를 복원할 수 없다.

    **adj 의 스키마 범위는 조정 상한(±0.2)이 아니라 점수 눈금(±1)이다.** 상한은 설계 5.6 에
    "코드에서 다시 자른다"고 적힌 규칙이라, 0.25 를 낸 응답은 묶음 전체를 기권시키는 대신
    0.2 로 자르는 편이 맞다. 반대로 ±1 을 벗어난 수는 눈금 자체를 잘못 이해한 것이므로
    스키마 위반으로 보고 기권한다 (level·enum 과 같은 취급).

    건수 상한(`maxItems`)은 달지 않는다 — 머리말에 적은 대로 코드 20개짜리 묶음에서 요청이
    통째로 400 이 된다. "후보 밖 자산을 만들어 낼 수 없다"는 code enum 이, "한 자산 한 줄"은
    `postprocess` 가 code 를 키로 합치는 것이 지킨다.
    """
    cap = abs(float(cap))
    bound = max(cap, SCORE_SCALE)
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "enum": list(codes)},
                "adj": {"type": "number", "minimum": -bound, "maximum": bound},
                "veto": {"type": "boolean"},
                "adopted": {"type": "array", "items": {"type": "string", "enum": list(factor_ids)}},
                "rejected": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "factor_id": {"type": "string", "enum": list(factor_ids)},
                            "reason": {"type": "string"},
                        },
                        "required": ["factor_id", "reason"],
                        "propertyOrdering": ["factor_id", "reason"],
                    },
                },
                "reason": {"type": "string"},
            },
            "required": ["code", "adj", "veto", "adopted", "rejected", "reason"],
            "propertyOrdering": ["code", "adj", "veto", "adopted", "rejected", "reason"],
        },
    }


__all__ = ["LEVEL_MAX", "LEVEL_MIN", "NEWS_CATEGORIES", "SCORE_SCALE", "TASK_ADJUST",
           "TASK_DISCLOSURE", "TASK_NEWS", "adjust_schema", "disclosure_schema", "news_schema"]
