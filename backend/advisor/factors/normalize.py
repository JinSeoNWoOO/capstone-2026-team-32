"""눈금 변환 (결정 9, 설계 5.1). 모든 하위 점수를 -1~+1 의 같은 눈금으로 만든다.

눈금이 같아야 가중치 2·1·0 이 실제 중요도 비율을 뜻하고, 도전 가중치 안의 사후 재평가가
단순 가중 합산이 된다 (결정 6). 순위 기반을 택한 이유는 극단값에 흔들리지 않고 조정할
파라미터가 없어 "임의로 정한 값" 시비가 가장 적기 때문이다.

모든 함수는 **부수 효과가 없는 순수 함수**이고 `(score, missing)` 짝을 돌려준다.
결측은 0 점(중립)이지만 missing=True 로 표시해 계층 점수의 분자·분모에서 모두 빠진다 —
"자료가 없다"와 "봤는데 중립이다"를 구분하지 않으면 채점이 오염된다.

수식에 나오는 2·0.5·1 같은 수는 눈금 정의 그 자체(설계 5.1의 식)라 설정값이 아니다.
바꿀 수 있는 값(과거 분포 길이, 감쇠 기간 등)은 전부 인자로 받는다.
"""

# LLM 기준표의 수준은 -2~+2 의 5단계다. 2로 나눠 같은 눈금으로 옮긴다 (결정 9).
RUBRIC_MAX_LEVEL = 2


def _is_num(v):
    """None 과 NaN 을 결측으로 본다 (NaN 은 자기 자신과 다르다)."""
    return v is not None and v == v


def clip(x, lo, hi):
    """lo~hi 로 자른다. 비중·조정 폭처럼 상한이 규칙인 곳에서 쓴다."""
    return lo if x < lo else (hi if x > hi else x)


def rank_scores(values, sign):
    """같은 날 대상 N개의 값 → 순위 점수 {키: (score, missing)}.

    오름차순 순위 r(1..N)에 대해 score = sign × (2 × (r − 0.5) / N − 1).
    동점은 평균 순위를 쓴다. 결측은 순위에서 빼고 (0.0, True).
    N=1 이면 식이 그대로 0.0 을 준다 — 비교 대상이 없으면 우열도 없다.
    """
    out = {k: (0.0, True) for k in values}
    present = [(k, float(v)) for k, v in values.items() if _is_num(v)]
    n = len(present)
    if n == 0:
        return out
    present.sort(key=lambda kv: kv[1])
    i = 0
    while i < n:
        j = i
        while j + 1 < n and present[j + 1][1] == present[i][1]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0            # 동점 구간의 평균 순위
        score = sign * (2.0 * (avg_rank - 0.5) / n - 1.0)
        for t in range(i, j + 1):
            out[present[t][0]] = (float(score), False)
        i = j + 1
    return out


def hist_percentile_score(value, history, sign, min_history=None):
    """자기 과거 분포 대비 백분위 → 점수 (시장 계층, 설계 5.1).

    p = (value 보다 작은 개수 + 같은 개수 × 0.5) ÷ 전체, score = sign × (2p − 1).
    동점을 중간 순위로 세는 이유는 값이 계단처럼 반복되는 계열(규칙형에 가까운 지표)에서
    백분위가 0 또는 1 로 튀지 않게 하기 위해서다.

    history 가 min_history 개에 못 미치면 점수를 지어내지 않고 결측으로 둔다 — 표본 몇 개짜리
    분포에서 나온 백분위는 그 자체가 잡음이다. min_history 는 설정값(normalize.min_history)이다.
    """
    hist = [float(h) for h in (history or []) if _is_num(h)]
    if not _is_num(value) or not hist:
        return (0.0, True)
    if min_history is not None and len(hist) < int(min_history):
        return (0.0, True)
    v = float(value)
    less = sum(1 for h in hist if h < v)
    equal = sum(1 for h in hist if h == v)
    p = (less + 0.5 * equal) / len(hist)
    return (float(sign * (2.0 * p - 1.0)), False)


def rule_score(cond, sign):
    """규칙형: 조건 참이면 +1, 거짓이면 −1 (부호를 곱한다). 조건을 못 구하면 결측."""
    if cond is None:
        return (0.0, True)
    return (float(sign * (1.0 if cond else -1.0)), False)


def rubric_score(level, sign):
    """LLM 기준표 수준(−2..+2) → 점수. 수준이 없으면 결측 (이벤트 없음과 구분)."""
    if not _is_num(level):
        return (0.0, True)
    return (float(clip(sign * float(level) / RUBRIC_MAX_LEVEL, -1.0, 1.0)), False)


def linear_decay(elapsed_days, decay_days):
    """이벤트 점수의 선형 감쇠 계수 max(0, 1 − 경과 ÷ 감쇠 기간) (결정 9의 공통 규칙 3).

    공시·뉴스 점수를 영원히 들고 있으면 한 번의 사건이 채점 기간 밖까지 순위를 밀어 올린다.
    경과가 음수(아직 오지 않은 사건)면 1.0 로 본다 — 시점 규칙 위반은 여기가 아니라 수집에서 막는다.
    """
    if decay_days is None or float(decay_days) <= 0:
        return 0.0
    elapsed = max(0.0, float(elapsed_days))
    return max(0.0, 1.0 - elapsed / float(decay_days))
