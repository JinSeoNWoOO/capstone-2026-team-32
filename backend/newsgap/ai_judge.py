"""뉴스 제목 AI 판정 (Google Gemini API, google-genai SDK).

핫패스에서 쓰는 것: 뉴스 제목 한 줄을 넣고 "지금 개인이 몇 분 안에 이 종목을 사러 몰릴 재료인가"를
BUY/SKIP 으로 받는다. 감성분석이 아니라 '이미 오른 뒤의 후행 기사'를 걸러내는 필터다
(첫날 규칙 BUY 13건 중 10건이 시황봇 기사였다 — README 참고).

- judge() 는 절대 예외를 던지지 않는다. 타임아웃·API 오류·파싱 실패 → decision="ERROR", conf=0.0.
  호출부는 ERROR 를 SKIP 과 같이 취급하되 counterfactual 가상 진입은 그대로 기록한다.
- 모델·타임아웃·프롬프트 버전은 config.yaml 의 ai: 섹션에서 온다 (코드에 숫자 하드코딩 금지).
- SDK 호출: await client.aio.models.generate_content(model=, contents=, config=)  → resp.text
  config 에 response_mime_type="application/json" 을 줘서 JSON 만 받되, 파싱은 방어적으로 한다.
- 오프라인 라벨러: python -m newsgap.ai_judge --db data/newsgap.db --date 2026-09-08
"""
import argparse, asyncio, json, logging, os, re, time
from dataclasses import dataclass

log = logging.getLogger("newsgap.ai")

# 짧은 제목 분류 → 현행 세대 Flash-Lite (가장 싸고 빠른 급, thinking 기본 minimal).
# 더 싸게: gemini-3.1-flash-lite($0.25/$1.50), gemini-2.5-flash-lite($0.10/$0.40, 이전 세대).
DEFAULT_MODEL = "gemini-3.5-flash-lite"

PROMPT_VER = "v1"

# 프롬프트를 고칠 때는 PROMPT_VER 를 같이 올린다 (news.ai_prompt_ver 로 라벨 세대를 구분한다).
SYSTEM_PROMPT_V1 = """너는 한국 주식 단타 트레이더의 뉴스 필터다.
입력은 방금 수신한 국내 종목 뉴스의 '제목'과 종목코드, 언론사 id 뿐이다. 본문은 없다.
판단할 것은 감성(호재/악재)이 아니라 딱 하나다:
"이 제목을 본 개인 투자자들이 지금부터 몇 분 안에 이 종목을 사러 몰릴 것인가?"
제목은 방금 도착한 새 기사다. 이미 벌어진 주가 움직임을 전하는 기사라면 늦은 것이다.

BUY = 아직 주가에 반영되지 않은, 그 회사에 국한된 새 재료.
 - 대형 고객사·빅테크와의 공급계약·수주, 규제 승인(FDA·품목허가 등), M&A·대규모 투자유치,
   깜짝 실적, 정책·테마의 직접 수혜로 지목, 독점적 기술 성과·개발 성공.
SKIP = 그 외 전부. 특히
 - 이미 일어난 가격·거래량을 전하는 기사(+4.19%, 상승세, 급등, 강세, 특징주, 시황, 개장/마감,
   수급포착, 순매수, 신고가, 52주 최고, 거래량 급증, N거래일 연속 등)
 - 거시·업종·시장 전반 뉴스, 중립·부정 뉴스, 광고·홍보·이벤트 기사
 - 제목 내용이 그 종목과 무관해 보이는 경우(코드가 잘못 붙은 기사)
 - 규모가 안 적힌 통상적 공시·일정 안내

conf 는 보정해서 쓴다. 0.9 이상은 누가 봐도 즉시 매수세가 붙을 재료일 때만, 애매하면 0.5 이하.

출력은 JSON 객체 하나만. 코드펜스·설명·다른 텍스트 금지.
{"decision":"BUY"|"SKIP","conf":0.0~1.0,"reason":"40자 이내 한국어 근거"}"""

SYSTEM_PROMPT_V2 = """너는 한국 주식 단타 트레이더의 뉴스 필터다.
방금 수신한 국내 종목 뉴스를 보고 딱 하나를 판단한다:
"이 종목이 지금부터 5분 안에 2% 이상 오를 것인가?"
호재/악재 감성이 아니라 '지금 살 사람이 몰릴 재료인가'다.

입력 블록은 있는 것만 온다 (없으면 그 블록이 통째로 빠진다):
 [제목] 항상 있다.
 [본문] 기사 본문 또는 종목 카드(기업개요·최대주주). 잘려 있을 수 있고, 전송 과정에서
        약 60자마다 한 글자가 '�' 로 깨져 있다 — 문맥으로 읽고 그 자체를 문제 삼지 마라.
 [공시] 공시 원문에서 뽑은 항목. 계약금액이 최근매출액의 몇 %인지가 핵심이다.
        같은 "공급계약체결"도 0.5%면 무의미하고 20%면 큰 재료다. '금액미공개'는 규모를 모른다는 뜻.
 [시장] 수신 시점의 상태. pre_spike=수신 직전 120초 거래량이 평소의 몇 배인가.
        pre60=직전 1분 가격 변화율. spread=최우선 매도호가와 매수호가의 차이(왕복 비용에 더해진다).

판단 지침:
 - 이미 일어난 가격·거래량을 전하는 기사(+4.19%, 상승세, 급등, 특징주, 시황, 개장/마감, 수급포착,
   순매수, 신고가, N거래일 연속)는 후행 기사다 → SKIP.
 - 거시·업종·시장 전반, 중립·부정, 광고·홍보·행사, 제목이 그 종목과 무관해 보이는 것(코드 오귀속) → SKIP.
 - 규모가 안 적힌 통상적 공시·일정 안내 → SKIP.
 - BUY 는 그 회사에 국한된 새 재료가 있을 때만: 대형 고객사 수주·공급계약, 규제 승인, M&A·대규모 투자유치,
   깜짝 실적, 정책·테마의 직접 수혜 지목, 독점적 기술 성과.
 - [시장]이 있으면 참고하되 그것만으로 결정하지 마라. pre_spike 가 이미 크면 재료가 이미 알려진
   것일 수 있고, 반대로 재료가 진짜인데 아직 조용할 수도 있다. 판단 근거를 reason 에 남겨라.

conf 는 decision 에 대한 확신도가 아니라 **"5분 안에 2% 이상 오를 확률" 그 자체**다.
SKIP 이면 대개 0.1 이하가 되고, 확실한 재료여야 0.7 이상이다. SKIP 인데 conf 가 높으면 모순이다.
(이 값으로 이벤트를 줄세울 것이므로 결정과 따로 보정하지 마라.)

출력은 JSON 객체 하나만. 코드펜스·설명·다른 텍스트 금지.
{"decision":"BUY"|"SKIP","conf":0.0~1.0,"reason":"40자 이내 한국어 근거"}"""

PROMPTS = {"v1": SYSTEM_PROMPT_V1, "v2": SYSTEM_PROMPT_V2}


@dataclass
class Judgment:
    decision: str        # "BUY" | "SKIP" | "ERROR"
    conf: float          # 0.0~1.0 (ERROR 는 0.0)
    model: str           # 실제 사용 모델 ID (NullJudge 는 "none")
    prompt_ver: str
    latency_ms: float    # 요청→응답 monotonic 기준
    reason: str          # 모델 근거 또는 에러 메시지 (<=200자)
    inputs: str = "title"   # 실제로 넣은 입력 블록 (예 "title+body+market") — 변형 간 대조용


def _clip(s, n=200):
    s = " ".join(str(s).split())
    return s[:n]


def _extract_json(text):
    """응답에서 첫 JSON 객체를 꺼낸다. 코드펜스·앞뒤 잡문 허용. 실패하면 None."""
    if not text:
        return None
    t = re.sub(r"```[a-zA-Z]*", "", text).replace("```", "")
    start = t.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(t[start:i + 1])
                    except Exception:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
        start = t.find("{", start + 1)
    return None


def _resp_text(resp):
    """GenerateContentResponse.text. 안전필터 등으로 후보가 비면 빈 문자열."""
    try:
        return getattr(resp, "text", None) or ""
    except Exception:                               # .text 는 파트가 없으면 예외/경고를 낼 수 있다
        return ""


def _finish_detail(resp):
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


class AIJudge:
    """제목만 보고 BUY/SKIP. client 를 넣으면 그대로 쓴다(테스트용 fake).

    client 는 google-genai 의 genai.Client 와 같은 모양이면 된다:
    await client.aio.models.generate_content(model=..., contents=..., config=...) → .text 를 가진 응답.
    """

    def __init__(self, cfg, client=None):
        cfg = cfg or {}
        self.model = cfg.get("model") or DEFAULT_MODEL
        self.timeout_sec = float(cfg.get("timeout_sec", 6.0))
        self.prompt_ver = str(cfg.get("prompt_ver") or PROMPT_VER)
        self.max_tokens = int(cfg.get("max_tokens", 200))
        self.system = PROMPTS.get(self.prompt_ver, SYSTEM_PROMPT_V1)
        # dict 로 넘겨도 SDK 가 GenerateContentConfig 로 검증한다 → 여기서 google.genai 를 import 하지 않는다.
        self.gen_config = {
            "system_instruction": self.system,
            "response_mime_type": "application/json",   # JSON 강제. 그래도 파싱은 방어적으로.
            "max_output_tokens": self.max_tokens,
            "temperature": float(cfg.get("temperature", 0.0)),
        }
        # Gemini 3.x 는 thinking 이 기본 on. 핫패스 지연을 줄이려 최소로 둔다.
        # gemini-2.5-* 처럼 thinking_level 을 모르는 모델로 바꾸면 config.yaml 에서 빈 값으로 꺼라.
        level = cfg.get("thinking_level", "MINIMAL")
        if level:
            self.gen_config["thinking_config"] = {"thinking_level": str(level).upper()}
        if client is not None:
            self._client = client
            return
        from .ls_client import load_env_file
        env_name = cfg.get("api_key_env") or "GEMINI_API_KEY"
        load_env_file(".env")
        key = os.environ.get(env_name) or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError(
                f"{env_name} 가 없습니다. .env 에 {env_name}=... 를 넣으세요 (.env.example 참고, "
                "https://aistudio.google.com/apikey). AI 를 끄려면 config.yaml 의 ai.enabled: false (NullJudge, 항상 BUY)")
        from google import genai
        # HttpOptions.timeout 단위는 밀리초. 실제 상한은 judge() 의 wait_for.
        self._client = genai.Client(api_key=key.strip(),
                                    http_options={"timeout": int(max(self.timeout_sec, 10.0) * 1000) + 1000})  # HTTP 데드라인은 서버 최소치 10초 이상. 실제 예산(timeout_sec)은 judge()의 wait_for

    def _user_msg(self, title, code, source_id, body=None, disclosure=None, market=None):
        """있는 블록만 넣는다. 블록 구성이 곧 변형(inputs)이므로 순서를 바꾸지 않는다."""
        parts = [f"[제목] {title}", f"종목코드: {code}  언론사id: {source_id}"]
        if disclosure:
            parts.append(f"[공시] {disclosure}")
        if body:
            parts.append(f"[본문] {body}")
        if market:
            parts.append("[시장] " + market)
        return "\n".join(parts)

    @staticmethod
    def _inputs_tag(body, disclosure, market):
        tag = ["title"]
        if disclosure:
            tag.append("disc")
        if body:
            tag.append("body")
        if market:
            tag.append("market")
        return "+".join(tag)

    async def _call(self, msg):
        return await self._client.aio.models.generate_content(
            model=self.model, contents=msg, config=self.gen_config)

    async def judge(self, title, code="", source_id="", body=None, disclosure=None, market=None):
        t0 = time.monotonic()
        tag = self._inputs_tag(body, disclosure, market)
        ms = lambda: (time.monotonic() - t0) * 1000.0
        err = lambda msg: Judgment("ERROR", 0.0, self.model, self.prompt_ver, ms(), _clip(msg), tag)
        msg = self._user_msg(title, code, source_id, body, disclosure, market)
        try:
            resp = await asyncio.wait_for(self._call(msg), self.timeout_sec)
        except asyncio.TimeoutError:
            return err(f"timeout {self.timeout_sec}s")
        except Exception as e:                      # API·네트워크 오류. 핫패스를 막지 않는다.
            return err(f"{type(e).__name__}: {e}")
        u = getattr(resp, "usage_metadata", None)
        if u is not None:
            log.debug("ai usage model=%s in=%s out=%s thoughts=%s", self.model,
                      getattr(u, "prompt_token_count", None), getattr(u, "candidates_token_count", None),
                      getattr(u, "thoughts_token_count", None))
        text = _resp_text(resp)
        obj = _extract_json(text)
        if not obj:
            return err(f"unparsable: {text or _finish_detail(resp) or 'empty response'}")
        decision = str(obj.get("decision", "")).strip().upper()
        if decision not in ("BUY", "SKIP"):
            return err(f"bad decision: {_clip(obj.get('decision'), 40)}")
        try:
            conf = float(obj.get("conf", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = min(1.0, max(0.0, conf))
        return Judgment(decision, conf, self.model, self.prompt_ver, ms(),
                        _clip(obj.get("reason", ""), 200), tag)


class NullJudge:
    """AI 끔. 항상 BUY, model='none' — 기존 규칙 기반 동작과 동일하게 만드는 대역."""

    def __init__(self, cfg=None):
        self.model, self.prompt_ver = "none", (cfg or {}).get("prompt_ver") or PROMPT_VER

    async def judge(self, title, code="", source_id="", body=None, disclosure=None, market=None):
        return Judgment("BUY", 1.0, "none", self.prompt_ver, 0.0, "ai disabled", "title")


def build_judge(cfg):
    """config.yaml 의 ai: 섹션 하나로 AIJudge/NullJudge 선택. 핫패스에서 이걸 부르면 된다."""
    cfg = cfg or {}
    return AIJudge(cfg) if cfg.get("enabled", True) else NullJudge(cfg)


# ---------------------------------------------------------------- 오프라인 라벨러

SELECT_SQL = ("SELECT realkey, code, source_id, title FROM news "
              "WHERE code IS NOT NULL AND code != '' AND recv_wall LIKE ?")


async def run_label(db_path, date, limit=None, concurrency=4, force=False, judge=None, cfg=None):
    """그날 코드 붙은 뉴스에 ai_* 컬럼을 채운다. 이미 라벨된 행은 --force 없으면 건너뛴다."""
    from .store import Store
    store = Store(db_path)
    sql = SELECT_SQL + ("" if force else " AND ai_decision IS NULL") + " ORDER BY recv_wall"
    params = [f"{date}%"]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = store.conn.execute(sql, params).fetchall()
    print(f"{len(rows)} rows to label (db={db_path} date={date} force={force})")
    if not rows:
        return {}
    j = judge if judge is not None else AIJudge(cfg or {})
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def one(row):
        async with sem:
            return row, await j.judge(row[3] or "", row[1] or "", row[2] or "")

    counts, lat, done = {}, [], 0
    for fut in asyncio.as_completed([asyncio.ensure_future(one(r)) for r in rows]):
        row, res = await fut
        store.news_update(row[0], ai_decision=res.decision, ai_conf=res.conf, ai_model=res.model,
                          ai_prompt_ver=res.prompt_ver, ai_latency_ms=res.latency_ms)
        counts[res.decision] = counts.get(res.decision, 0) + 1
        lat.append(res.latency_ms)
        if res.decision == "ERROR":
            log.warning("ERROR %s %s: %s", row[0], row[1], res.reason)
        done += 1
        if done % 50 == 0:
            store.commit()
            print(f"  {done}/{len(rows)} {counts}")
    store.commit()
    mean_lat = sum(lat) / len(lat) if lat else 0.0
    print(f"done {done}/{len(rows)} {counts} mean_latency={mean_lat:.0f}ms errors={counts.get('ERROR', 0)}")
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description="뉴스 제목 AI 라벨러 (news.ai_* 채우기)")
    ap.add_argument("--db", default="data/newsgap.db")
    ap.add_argument("--date", required=True, help="recv_wall 앞부분, 예: 2026-09-08")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--force", action="store_true", help="이미 라벨된 행도 다시 판정")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--model", default=None, help="config.yaml ai.model 덮어쓰기")
    ap.add_argument("--timeout", type=float, default=20.0, help="오프라인은 핫패스보다 넉넉히")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = {}
    if os.path.exists(a.config):
        import yaml
        cfg = (yaml.safe_load(open(a.config, encoding="utf-8")) or {}).get("ai") or {}
    cfg = dict(cfg, timeout_sec=a.timeout)          # 라벨러는 ai.enabled 와 무관하게 실제 판정을 한다
    if a.model:
        cfg["model"] = a.model
    asyncio.run(run_label(a.db, a.date, a.limit, a.concurrency, a.force, cfg=cfg))


if __name__ == "__main__":
    main()
