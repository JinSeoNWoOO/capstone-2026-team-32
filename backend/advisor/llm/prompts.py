"""작업별 시스템 프롬프트와 입력 블록 (설계 9장의 프롬프트 원칙).

세 가지 원칙이 세 프롬프트에 모두 들어간다.

1. **판단일 이후를 암시하지 않는다.** 결과·주가 반응·후속 공시를 묻지도, 알려 주지도 않는다.
   재현 모드에서 모델이 "그 뒤에 어떻게 됐는지"를 떠올리면 그날 점수가 미래를 본 값이 된다 (결정 15의 6번).
2. **수치는 코드가 준다.** 비율·점수·순위는 이미 계산해서 넣는다. 다시 계산하지 말라고 못박는다 —
   자유 서술 속 재계산은 검산할 수 없고, 애초에 코드가 정확하다 (결정 3).
3. **근거는 입력에서 인용한다.** evidence·reason 은 환각 점검용이다. 입력에 없는 문장이 인용으로
   나오면 그 건은 사람이 바로 알아볼 수 있다.

프롬프트를 고칠 때는 **버전을 올린다**(`llm.prompt_ver`). 버전은 입력 해시에 들어가므로
문구가 바뀌면 캐시가 자동으로 갈리고, 저장된 판단이 어느 문구에서 나왔는지가 남는다.
"""
from .client import clip_text

# 지원하는 프롬프트 버전. 설정(llm.prompt_ver)이 여기에 없는 값이면 조용히 옛 문구를 쓰지 않고 죽는다 —
# 기록에는 v2 라고 적혀 있는데 v1 문구로 물어본 판단이 남는 것이 가장 나쁘다.
PROMPT_VERSIONS = ("v1",)
DEFAULT_PROMPT_VER = "v1"


def check_version(ver):
    ver = str(ver or DEFAULT_PROMPT_VER)
    if ver not in PROMPT_VERSIONS:
        raise ValueError(f"모르는 프롬프트 버전입니다: {ver} (가능: {PROMPT_VERSIONS})")
    return ver


# ---------------------------------------------------------------- 공시 채점 (설계 5.4)

_DISCLOSURE_V1 = """너는 한국 유가증권시장 공시를 기준표대로 채점하는 분류기다.
입력은 방금 접수된 공시의 제목과(있으면) 본문, 그리고 **코드가 이미 뽑아 둔 수치**다.

판단할 것은 하나다: "이 공시는 아래 기준표의 어느 항목인가."
- rubric_id 는 기준표에 있는 id 중 하나만 쓴다.
- level 은 그 항목에 적힌 값을 그대로 쓴다. 기준표에 값이 고정돼 있지 않은 항목
  (other_material)만 네가 -2~+2 안에서 정한다.
- 어느 항목인지 모르겠으면 other_material 과 낮은 confidence 를 쓴다. 억지로 고르지 마라.

지켜야 할 것:
- [수치] 블록의 값은 이미 검산된 것이다. 다시 계산하거나 다른 수를 지어내지 마라.
- 공시 이후에 무슨 일이 있었는지(주가 반응·후속 공시·시장 평가)는 아무도 모른다. 쓰지 마라.
- evidence 는 [제목]이나 [본문]에 **실제로 있는 문구를 그대로 60자 이내로 인용**한다.
  인용할 문구가 없으면 제목을 그대로 적는다. 입력에 없는 말은 쓰지 마라.
- confidence 는 채점이 맞을 확률이다. 제목만 있고 단서가 약하면 0.5 이하가 정상이다.
- evidence 는 한국어로 쓴다.

[기준표]
{rubric}

출력은 JSON 객체 하나뿐이다."""


def disclosure_system(rubric, ver=DEFAULT_PROMPT_VER):
    """기준표를 설정에서 그대로 옮겨 넣은 시스템 프롬프트.

    기준표를 코드에 다시 적지 않는 이유: 설정의 level 과 프롬프트의 level 이 어긋나면
    저장된 점수와 모델이 본 표가 달라진다. 설정이 유일한 출처다 (결정 6).
    """
    check_version(ver)
    lines = []
    for row in rubric or ():
        rid = row.get("id")
        if not rid:
            continue
        fixed = "LLM 이 수준을 정함" if row.get("decided_by") == "llm" and rid == "other_material" \
            else f"level {int(row.get('level', 0)):+d}"
        lines.append(f"- {rid} ({row.get('kind', '')}, {fixed}): {row.get('desc', '')}")
    return _DISCLOSURE_V1.format(rubric="\n".join(lines))


def disclosure_user(title, corp=None, code=None, rcept_dt=None, numbers=None, body=None,
                    ver=DEFAULT_PROMPT_VER):
    """공시 한 건의 입력 블록. 있는 블록만 넣는다 (ai_judge 와 같은 구성).

    LS 공시 속보에 본문이 실려 오는 일이 드물어 **제목만 주는 것이 정상 경로**다 (설계 5.4의 4번).
    본문이 없다는 사실 자체는 알리지 않는다 — 없는 블록은 통째로 빠진다.
    """
    check_version(ver)
    head = " ".join(str(x) for x in (rcept_dt or "", corp or "", f"({code})" if code else "") if x)
    parts = [f"[공시] {head}".rstrip(), f"[제목] {title}"]
    if numbers:
        parts.append(f"[수치] {numbers}")
    if body:
        parts.append(f"[본문] {body}")
    return "\n".join(parts)


# ---------------------------------------------------------------- 뉴스 분류 (결정 12의 관찰 요인)

_NEWS_V1 = """너는 한국 주식시장 뉴스에서 '시장을 움직일 사건'을 골라내는 분류기다.
입력은 방금 수신한 뉴스 제목 묶음이다. 본문은 없다. 제목에 적힌 것만 보고 판단한다.

항목마다 정할 것:
- category: war(전쟁·지정학 충돌) / disaster(재난·사고) / policy(정부·규제·정책) /
  key_person(핵심 인사의 발언·인사·사법) / macro(금리·환율·물가·해외 지수) /
  company(개별 기업의 사업 소식) / noise(그 밖)
- sectors: 아래 목록에 있는 이름만 고른다. 영향이 특정되지 않으면 빈 목록으로 둔다.
  목록에 없는 섹터는 쓰지 마라.
- direction: 고른 섹터의 주가에 미칠 방향 -2(크게 부정) ~ +2(크게 긍정). 모르면 0.
- severity: 사건 자체의 크기 0(없음)~3(시장 전체가 반응할 사건). 방향과 따로 매긴다 —
  큰 사건인데 방향을 모르면 severity 는 높고 direction 은 0 이다.
- theme_suspect: 정치인·유력 인사와의 인맥으로 종목을 엮는 기사면 true. 사업 내용이 근거면 false.
- reason: 제목에서 인용한 20자 이내 한국어 근거.

이미 일어난 가격·거래량을 전하는 기사(특징주·시황·마감·수급·신고가·급등락)는 사건이 아니다
→ category=noise, sectors=[], direction=0, severity=0.
제목에 없는 사실을 추측하지 마라. 뉴스 이후에 시장이 어떻게 반응했는지는 아무도 모른다.

[섹터 목록] {sectors}

입력의 id 를 **그대로** 되돌려 준다. 입력에 없는 id 를 만들지 마라.
입력 항목 수만큼 출력한다. 출력은 JSON 배열 하나뿐이다."""


def news_system(sectors, ver=DEFAULT_PROMPT_VER):
    check_version(ver)
    return _NEWS_V1.format(sectors=", ".join(sectors) if sectors else "(없음)")


def news_user(items, ver=DEFAULT_PROMPT_VER):
    """헤드라인 묶음. items 는 {id, time, code, title} 의 목록이다."""
    check_version(ver)
    lines = []
    for it in items:
        code = it.get("code") or "-"
        when = clip_text(it.get("time") or "", 19)
        lines.append(f"id={it['id']} | {when} | {code} | {clip_text(it.get('title'), 200)}")
    return "[뉴스]\n" + "\n".join(lines)


# ---------------------------------------------------------------- 종합 점수 조정 (설계 5.6)

_ADJUST_V1 = """너는 이미 계산이 끝난 점수표를 검토하는 조정자다. 점수를 다시 계산하지 않는다.
각 자산의 하위 점수는 코드가 정해진 규칙으로 만든 값이고, 종합 점수도 이미 나와 있다.
네 권한은 둘뿐이다.

1. adj: 종합 점수를 {cap:+.2f} 안에서만 움직인다. 움직일 이유가 없으면 0 이다.
   근거는 입력에 있는 하위 점수·위험 표시·최근 공시/뉴스 요약뿐이다.
2. veto: **위험 표시가 붙은 자산만** 후보에서 뺄 수 있다. 표시가 없는 자산의 veto 는 무시된다.

목록에 없는 자산을 새로 넣을 권한은 없다. 자산을 빼거나 더하는 판단은 하지 마라.
adopted 에는 이 자산에서 실제로 근거로 삼은 요인 id 를, rejected 에는 점수는 있지만
믿지 않기로 한 요인 id 와 그 이유를 적는다. 둘 다 비어 있어도 되지만 adj 가 0 이 아니면
반드시 이유가 있어야 한다.

지켜야 할 것:
- 입력에 없는 사실(오늘 이후의 주가·뉴스·실적)을 쓰지 마라. 판단 시각 이후는 아무도 모른다.
- 수치를 다시 계산하지 마라. 입력의 값을 인용하라.
- reason 은 40자 이내 한국어 한 문장. 무엇을 보고 얼마나 움직였는지가 드러나야 한다.
- 입력에 있는 자산 code 만 쓰고, 모든 자산에 대해 한 줄씩 낸다.

출력은 JSON 배열 하나뿐이다."""


def adjust_system(cap, ver=DEFAULT_PROMPT_VER):
    check_version(ver)
    return _ADJUST_V1.format(cap=abs(float(cap)))


def _fmt(x, nd=3):
    return "-" if x is None else f"{float(x):.{nd}f}"


def adjust_user(market_ctx, assets, ver=DEFAULT_PROMPT_VER):
    """시장 맥락 + 자산별 표. assets 는 adjust.build_assets 가 만든 dict 목록이다.

    시장 맥락을 함께 주는 이유는 설계 5.6 그대로다 — 같은 하위 점수라도 시장 국면에 따라
    "이 요인을 믿을 것인가"가 달라진다.
    """
    check_version(ver)
    out = ["[시장]", f"시장 점수 = {_fmt((market_ctx or {}).get('score'))}"]
    for f in (market_ctx or {}).get("factors") or ():
        mark = " (결측)" if f.get("missing") else ""
        out.append(f"  - {f.get('factor_id')}: 점수 {_fmt(f.get('score'))} "
                   f"원본 {_fmt(f.get('raw_value'), 4)}{mark}")
    if (market_ctx or {}).get("note"):
        out.append(f"  {market_ctx['note']}")

    out.append("")
    out.append("[자산] 종합 = v0 종합 점수 (조정 전)")
    for a in assets:
        head = f"- code={a['code']} {a.get('label', '')}".rstrip()
        out.append(f"{head} | 종합 {_fmt(a.get('composite'))} | "
                   f"보유 {'예' if a.get('held') else '아니오'}")
        subs = a.get("factors") or []
        if subs:
            out.append("    하위: " + ", ".join(
                f"{s.get('factor_id')} {_fmt(s.get('score'), 2)}"
                + ("(결측)" if s.get("missing") else "") for s in subs))
        if a.get("flags"):
            out.append("    위험 표시: " + ", ".join(a["flags"]))
        for ev in a.get("evidence") or ():
            out.append(f"    근거: {clip_text(ev, 160)}")
    return "\n".join(out)


__all__ = ["DEFAULT_PROMPT_VER", "PROMPT_VERSIONS", "adjust_system", "adjust_user", "check_version",
           "disclosure_system", "disclosure_user", "news_system", "news_user"]
