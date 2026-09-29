"""공시 원문(src=15) 파서. 본문에서 매매 판단에 쓰는 수치를 뽑는다.

일반 기사와 달리 공시 본문은 항목 구조라 정규식으로 값이 정확히 나온다.
가장 중요한 건 **계약금액 / 최근매출액 비율** — 발행사가 직접 계산해 적어둔다.
제목만 보면 "단일판매ㆍ공급계약체결"이 다 똑같지만 이 비율은 0.5%~수백%까지 벌어진다.

본문은 t3102 블록 경계에서 공백이 들쭉날쭉하므로, 공백을 모두 지운 문자열에서 찾는다.
뽑은 값은 검산한다(계약금액/최근매출액 이 공시에 적힌 비율과 맞는지) — 어긋나면 숫자가
블록 경계에서 손상된 것이므로 ratio_ok=False 로 표시하고 믿지 않는다.
"""
import re

from .body_clean import clean

KINDS = [
    ("공급계약", r"단일판매|공급계약"), ("유상증자", r"유상증자"), ("무상증자", r"무상증자"),
    ("전환사채", r"전환사채|신주인수권부사채|교환사채"), ("자기주식", r"자기주식|자사주"),
    ("임상", r"임상|품목허가|허가신청"), ("최대주주변경", r"최대주주\s*변경"),
    ("실적", r"매출액또는손익구조|영업\(잠정\)실적"), ("배당", r"배당"),
    ("주식분할병합", r"액면분할|액면병합|주식분할|주식병합"),
    ("상장폐지", r"상장폐지|정리매매"), ("거래정지", r"매매거래정지|거래정지"),
    ("시장조치", r"투자경고|투자주의|투자위험|단기과열|공매도\s*과열"),
    ("타법인출자", r"타법인\s*주식|출자증권"), ("증권발행", r"증권발행실적|추가상장"),
    ("IR", r"기업설명회|IR"), ("주요경영사항", r"투자판단\s*관련\s*주요경영사항"),
]
NUM = r"([\d,]+)"


def kind_of(title: str) -> str:
    for name, pat in KINDS:
        if re.search(pat, title or ""):
            return name
    return "기타"


def _num(compact, *labels):
    """공백을 지운 본문에서 라벨 뒤 숫자. 라벨은 먼저 오는 것을 우선한다."""
    for lab in labels:
        m = re.search(lab + r"\(?[원%주]?\)?" + NUM, compact)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


def _float(compact, *labels):
    for lab in labels:
        m = re.search(lab + r"\(?%?\)?([\d]+\.?[\d]*)", compact)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


def parse(title: str, body: str) -> dict:
    """공시 제목·본문 → {kind, flags, fields, ratio_ok, related}. 값이 없으면 키가 없다."""
    txt = clean(body)
    compact = re.sub(r"\s+", "", txt)
    kind = kind_of(title)
    out = {"kind": kind, "fields": {}, "flags": []}
    if re.search(r"\(정정\)|정정공시", title or ""):
        out["flags"].append("정정")
    if "자율공시" in (title or ""):
        out["flags"].append("자율공시")
    f = out["fields"]

    if kind == "공급계약":
        f["계약금액"] = _num(compact, "계약금액총액", "확정계약금액", "계약금액")
        f["최근매출액"] = _num(compact, "최근매출액")
        f["매출액대비"] = _float(compact, "매출액대비")
        m = re.search(r"계약상대방?(.{2,30}?)(?:-|최근매출액|주요사업|회사와의관계)", compact)
        if m:
            f["계약상대"] = m.group(1)
        m = re.search(r"계약기간시작일(\d{4}-\d{2}-\d{2}|-)종료일(\d{4}-\d{2}-\d{2}|-)", compact)
        if m:
            f["계약기간"] = f"{m.group(1)}~{m.group(2)}"
        m = re.search(r"판매ㆍ?공급지역([^-\d].{0,19}?)(?=\d\.|계약기간)", compact)
        if m:
            f["공급지역"] = m.group(1)
        # 금액 자리가 "-" 면 공시유보(금액 비공개)다 — 값 없음과 구별해 표시한다
        if f.get("계약금액") is None and re.search(r"계약금액총액\(원\)-", compact):
            out["flags"].append("금액미공개")
    elif kind == "유상증자":
        f["신주수"] = _num(compact, r"신주의종류와수보통주식", r"나\.주식수")
        f["확정발행가"] = _num(compact, "확정가액", "확정발행가액")
        f["시설자금"] = _num(compact, "시설자금")
        f["운영자금"] = _num(compact, "운영자금")
        f["채무상환자금"] = _num(compact, "채무상환자금")
        f["증자전주식수"] = _num(compact, "증자전발행주식총수보통주식")
        m = re.search(r"증자방식(.{2,20}?)(?:※|\d\.)", compact)
        if m:
            f["증자방식"] = m.group(1)
        if f.get("신주수") and f.get("증자전주식수"):
            f["증자비율"] = round(100 * f["신주수"] / f["증자전주식수"], 1)
    elif kind == "자기주식":
        f["취득금액"] = _num(compact, "취득예정금액", "취득금액")
        f["취득주식수"] = _num(compact, "취득예정주식")
    elif kind == "실적":
        f["매출액"] = _num(compact, "매출액")
        f["영업이익"] = _num(compact, "영업이익")
    elif kind == "임상" or kind == "주요경영사항":
        m = re.search(r"(\d)상|(\d)a상|임상(\d)", compact)
        if m:
            f["임상단계"] = next(g for g in m.groups() if g)
        m = re.search(r"목표시험대상자수(\d+)명", compact)
        if m:
            f["목표대상자"] = int(m.group(1))

    # 검산: 금액/매출액 이 공시에 적힌 비율과 맞는가
    a, r_, p = f.get("계약금액"), f.get("최근매출액"), f.get("매출액대비")
    if a and r_ and p:
        out["ratio_ok"] = abs(100 * a / r_ - p) < max(0.15, p * 0.02)
    elif a and r_:
        f["매출액대비"] = round(100 * a / r_, 2)
        out["ratio_ok"] = None

    out["related"] = re.findall(r"관련공시(\d{4}-\d{2}-\d{2})", compact)
    return out


def to_prompt(d: dict) -> str:
    """AI 프롬프트에 넣을 한 줄 요약. 값이 없는 항목은 넣지 않는다."""
    parts = [f"공시종류={d['kind']}"]
    if d["flags"]:
        parts.append("/".join(d["flags"]))
    f = d["fields"]
    if f.get("계약금액") and f.get("매출액대비") is not None:
        parts.append(f"계약금액={f['계약금액']:,}원(최근매출액의 {f['매출액대비']}%)")
        if d.get("ratio_ok") is False:
            parts.append("[숫자 손상 의심]")
    for k in ("계약상대", "공급지역", "계약기간", "증자방식", "증자비율", "임상단계", "목표대상자",
              "확정발행가", "시설자금", "취득금액"):
        if f.get(k) not in (None, ""):
            v = f[k]
            parts.append(f"{k}={v:,}" if isinstance(v, int) else f"{k}={v}")
    if d["related"]:
        parts.append(f"관련공시={','.join(d['related'])}")
    return " | ".join(parts)


if __name__ == "__main__":
    import sqlite3
    db = sqlite3.connect("data/bodies.db")
    n = sqlite3.connect("file:data/newsgap.db?mode=ro", uri=True)
    rows = db.execute("SELECT realkey,body FROM body WHERE source_id='15' AND status='OK'").fetchall()
    print(f"공시 {len(rows)}건")
    for rk, body in rows:
        t = n.execute("SELECT title FROM news WHERE realkey=?", (rk,)).fetchone()
        d = parse(t[0] if t else "", body)
        print(f"  {(t[0] if t else '')[:44]}\n    → {to_prompt(d)}")
