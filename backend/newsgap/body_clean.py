"""뉴스 본문 정리. t3102 원문(HTML)에서 기사 본문만 남긴다.

t3102 는 본문을 60~100자 블록 수십 개로 쪼개 보내고, LS 서버가 블록 경계에 걸친 한글을
바이트 단위로 잘라 U+FFFD 로 망가뜨린다(약 60자당 1자, 실측 0.14%). 서버 쪽 손상이라
복원할 수 없으므로 여기서는 연속된 U+FFFD 를 한 글자로 줄여 토큰만 아낀다.

제거 대상 (실측한 발행사별 상용구):
  - <style>/<script> 및 태그를 벗겨도 남는 CSS 잔여물 (src=21·15)
  - 꼬리의 관련기사·주요뉴스 묶음 (☞ ▶ - 로 나열)
  - 저작권 표기 (Copyright ⓒ / 저작권자 / ＜ⓒ...＞)
  - 광고 (인포스탁 "실시간 포착", 한경 "스탁론", 데이터투자 "골든클럽" 등)
  - 기자 이메일·바이라인, 사진 캡션, 로이터 DISCLAIMER 머리말
"""
import html as _html
import re

# --- 꼬리 절단 마커: 본문 뒤쪽(40% 이후)에 나올 때만 그 지점부터 버린다 ---
TAIL = [
    r"\[?관련\s?기사\]?", r"\[[^\]]{0,20}주요\s?뉴스\]", r"※?\s*저작권자", r"Copyright\s*[ⓒ©]",
    r"[＜<]\s*[ⓒ©]", r"[ⓒ©]\s*[가-힣A-Za-z][^\n]{0,30}(?:무단|재배포)", r"무단\s*전재",
    r"※\s*이\s*기사는",
]
TAIL_RE = re.compile("|".join(TAIL))

# --- 위치와 무관하게 그 지점부터 버리는 구조적 꼬리 ---
# 인포스탁(21) 종목 카드는 제목·기업개요·최대주주까지가 내용이고 그 뒤는 시세표다.
HARD_TAIL_RE = re.compile("|".join([
    r"Update\s*:", r"개인/외국인/기관\s*일별", r"종목토론실", r"종목차트", r"\]?반기보고서\(",
    r"Click For Restrictions", r"골든클럽", r"스탁론", r"▶\s*해외\s*증시흐름",
    r"\[실시간\s*포착\]", r"\[긴급\s*분석\]",
]))

# --- 줄 단위로 지우는 광고·잡음 ---
DROP_LINE = re.compile(
    r"(무료\s*제공|선착순|신청하기|바로가기|텔레그램|카카오톡|네이버\s*구독|앱\s*다운|"
    r"pw\s*[:：]|체험권|필승\s*비법|급등주\s*발굴|이\s*컨텐츠는\s*투자\s*참고용|"
    r"모바일\s*주식신문|^▶|^●|^☞)")

# --- 머리말: 이 표시가 나오면 그 뒤부터가 본문 ---
HEAD_SKIP = [
    (re.compile(r"DISCLAIMER.*?습니다\.\s*", re.S), ""),          # 로이터 고지문
    (re.compile(r"^\s*t3102OutBlock1\s*"), ""),                    # 블록명 누출
]
# 길이 상한을 둔다 — 상한이 없으면 CSS 없는 긴 본문(공시 원문은 수만 자)에서 백트래킹이 폭발한다.
CSS_RE = re.compile(r"(?:^|\s)[.#a-zA-Z@][\w\-.,#:()\s>*]{0,120}\{[^{}]{0,2000}\}")
BYLINE_RE = re.compile(r"[\w.\-]+@[\w.\-]+\.\w+(?:\s*[가-힣]{2,4}\s*(?:기자|특파원|PD))?")
DATELINE_RE = re.compile(r"^\s*\[[^\]]{0,25}=[^\]]{0,20}\]\s*[가-힣]{2,4}\s*기자\s*=\s*")


def clean(body: str, source_id: str | None = None, max_chars: int = 0) -> str:
    """원문 HTML → 기사 본문 텍스트. max_chars 를 주면 그 길이로 자른다(문장 경계 우선)."""
    if not body:
        return ""
    t = re.sub(r"<(script|style)\b.*?</\1>", " ", body, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = _html.unescape(t)
    for pat, rep in HEAD_SKIP:
        t = pat.sub(rep, t, count=1)
    t = CSS_RE.sub(" ", t)
    t = re.sub(r"�+", "�", t)

    # 꼬리 절단 — 본문 뒤쪽(40% 이후)에 처음 나오는 마커부터 버린다
    cut = len(t)
    m = HARD_TAIL_RE.search(t)
    if m:
        cut = m.start()
    for m in TAIL_RE.finditer(t):
        if m.start() > len(t) * 0.4:
            cut = min(cut, m.start())
            break
    t = t[:cut]

    lines = [ln for ln in (l.strip() for l in t.split("\n")) if ln and not DROP_LINE.search(ln)]
    t = "\n".join(lines)
    t = BYLINE_RE.sub(" ", t)
    t = DATELINE_RE.sub("", t)
    t = re.sub(r"[ \t　]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t).strip()

    if max_chars and len(t) > max_chars:
        head = t[:max_chars]
        p = max(head.rfind("다."), head.rfind(". "))
        t = head[:p + 1] if p > max_chars * 0.6 else head
    return t


if __name__ == "__main__":                                  # 미리보기: 소스별 정리 전후
    import sqlite3, sys
    db = sqlite3.connect("data/bodies.db")
    q = "SELECT source_id,body FROM body WHERE status='OK' AND length(body)>800"
    if len(sys.argv) > 1:
        q += f" AND source_id='{sys.argv[1]}'"
    for src, b in db.execute(q + " ORDER BY RANDOM() LIMIT 4"):
        c = clean(b, src, max_chars=600)
        raw = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", b)).strip()
        print(f"=== src={src}  원문 {len(raw)}자 → 정리 {len(c)}자 ({100*len(c)/max(len(raw),1):.0f}%)")
        print(c[:500], "\n")
