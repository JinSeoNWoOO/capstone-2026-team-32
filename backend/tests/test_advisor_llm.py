"""판단 지원(advisor) 모드 LLM 계층 단위 테스트. **네트워크를 쓰지 않는다.**

모델 호출은 `genai.Client` 와 같은 모양의 가짜 객체로 대신한다
(`await client.aio.models.generate_content(model=, contents=, config=)` → `.text`·`.usage_metadata`).
가짜를 쓰는 이유는 비용이 아니라 **판정 가능성**이다. 실제 모델은 같은 입력에 다른 문장을 낼 수
있어 "스키마를 어기면 기권한다" 같은 계약을 시험할 수 없다.

확인하는 것:
  - 호출부: 정상 경로, 스키마 위반 → ERROR(기권), 예외·시간 초과 → ERROR(절대 안 던짐),
    입력 해시 캐시(행은 남기고 API 는 안 부름), 예산·호출 수 상한, 꺼짐
  - 공시 채점: 코드로 확정되는 경우는 부르지 않는다, 기준표 수준은 코드가 이긴다,
    휴장일을 건너뛴 감쇠, **first_seen_at 이 as_of 이후인 공시는 세지 않는다(미래 참조 금지)**
  - 뉴스 분류: 잡음·중복·시점 선거름, 모르는 id 버리기, 섹터 점수와 테마 표시
  - 조정: ±상한 자르기, 위험 표시 없는 거부 무시, 모르는 코드 버리기
  - 단계: 재현 모드는 부르지 않는다, 결측 자리를 덮어쓴다
"""
from backend.tests import _safety  # noqa: F401  맨 먼저: 실제 주문·외부 접속 차단 (_safety.py 참고)
import asyncio
import copy
import json
import logging
import tempfile
import unittest
from pathlib import Path

from backend.advisor.calendar import TradingCalendar
from backend.advisor.config import load_config
from backend.advisor.llm import adjust as adj_mod
from backend.advisor.llm import client as cl
from backend.advisor.llm import disclosure_score as ds
from backend.advisor.llm import news_risk as nr
from backend.advisor.llm import prompts, schemas, stage
from backend.advisor.store import Store

def setUpModule():
    """실패 경로를 일부러 밟는 테스트가 많아 경고 로그가 그대로 쏟아진다. 결과만 보이게 낮춘다."""
    logging.getLogger("advisor.llm").setLevel(logging.CRITICAL)


SIMPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "level": {"type": "integer", "minimum": -2, "maximum": 2},
        "kind": {"type": "string", "enum": ["a", "b"]},
        "conf": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["level", "kind", "conf"],
}


# ---------------------------------------------------------------- 가짜 SDK

class FakeUsage:
    def __init__(self, tin=100, tout=40, thoughts=0):
        self.prompt_token_count = tin
        self.candidates_token_count = tout
        self.thoughts_token_count = thoughts


class FakeResponse:
    def __init__(self, text, usage=None):
        self.text = text
        self.usage_metadata = usage or FakeUsage()
        self.candidates = []
        self.prompt_feedback = None


class FakeModels:
    """준비된 응답을 순서대로 돌려준다. 응답 대신 예외를 넣으면 그 자리에서 던지고,
    목록 대신 함수를 주면 모든 호출에 그 함수를 쓴다(건수를 미리 세기 어려울 때)."""

    def __init__(self, responses=None):
        self.handler = responses if callable(responses) else None
        self.responses = [] if self.handler else list(responses or [])
        self.calls = []

    async def generate_content(self, model=None, contents=None, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self.handler is not None:
            return self.handler(model, contents, config)
        if not self.responses:
            raise AssertionError("준비된 가짜 응답이 없습니다 (호출이 예상보다 많다)")
        item = self.responses.pop(0)
        if callable(item) and not isinstance(item, BaseException):
            item = item(model, contents, config)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeClient:
    def __init__(self, responses=None):
        self.models = FakeModels(responses)
        self.aio = type("Aio", (), {"models": self.models})()

    @property
    def n_calls(self):
        return len(self.models.calls)


def body(obj, **usage):
    return FakeResponse(json.dumps(obj, ensure_ascii=False), FakeUsage(**usage))


def always(obj, **usage):
    """모든 호출에 같은 응답을 주는 handler (건수를 미리 세기 어려운 경우)."""
    return lambda model, contents, config: body(obj, **usage)


class LLMTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "advisor.db")
        self.addCleanup(self.store.close)
        self.cfg = copy.deepcopy(load_config())
        # 본문 조회가 실제 newsgap.db 를 건드리지 않게 한다 (없는 경로면 조용히 None 이다).
        self.cfg["paths"]["newsgap_db"] = str(Path(self.tmp.name) / "newsgap.db")
        self.cal = TradingCalendar(self.cfg)

    def client(self, responses=None, **llm_over):
        fake = FakeClient(responses)
        cfg = self.cfg
        if llm_over:
            cfg = copy.deepcopy(self.cfg)
            cfg["llm"].update(llm_over)
        self.last_cfg = cfg
        llm = cl.LLMClient(cfg, self.store, run_id=1, client=fake)
        self.addCleanup(llm.close)                    # 소유한 이벤트 루프를 닫는다
        return llm, fake

    def rows(self, table, **where):
        sql = f"SELECT * FROM {table}"
        params = []
        if where:
            sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
            params = list(where.values())
        return self.store.conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------- 스키마 검증

class ValidateTest(unittest.TestCase):
    def test_passes_and_drops_unknown_keys(self):
        out = cl.validate({"level": 1, "kind": "a", "conf": 0.5, "extra": "x"}, SIMPLE_SCHEMA)
        self.assertEqual(out, {"level": 1, "kind": "a", "conf": 0.5})

    def test_range_enum_required(self):
        for bad in ({"level": 3, "kind": "a", "conf": 0.5},          # 범위 밖
                    {"level": 1, "kind": "c", "conf": 0.5},          # 열거값 밖
                    {"level": 1, "kind": "a"},                       # 필수 없음
                    {"level": 1.5, "kind": "a", "conf": 0.5},        # 정수 아님
                    {"level": "1", "kind": "a", "conf": 0.5}):       # 수가 아님
            with self.assertRaises(cl.SchemaError):
                cl.validate(bad, SIMPLE_SCHEMA)

    def test_integer_from_float_is_accepted(self):
        self.assertEqual(cl.validate({"level": 2.0, "kind": "b", "conf": 1}, SIMPLE_SCHEMA)["level"], 2)

    def test_array_items(self):
        schema = {"type": "array", "items": SIMPLE_SCHEMA}
        out = cl.validate([{"level": 0, "kind": "a", "conf": 0.1}], schema)
        self.assertEqual(len(out), 1)
        with self.assertRaises(cl.SchemaError):
            cl.validate({"level": 0}, schema)

    def test_extract_json_handles_fence_and_array(self):
        self.assertEqual(cl.extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(cl.extract_json('설명\n[{"a": 1}]\n꼬리'), [{"a": 1}])
        self.assertIsNone(cl.extract_json("빈 응답"))

    def test_no_array_gets_a_max_items_bound(self):
        """배열 스키마에 `maxItems` 를 달면 요청이 통째로 400 이 된다 (schemas.py 머리말).

        2026-09-22 실호출: 조정 스키마에 코드 20개 + `maxItems=20` → 400 INVALID_ARGUMENT,
        `maxItems` 만 빼면 코드 40개도 통과. 서버가 배열을 상한만큼 펼쳐 두는 탓으로 보인다.
        건수는 스키마가 아니라 후처리(모르는 code·id 버리기)가 지킨다.
        """
        cfg = load_config()
        schemas_made = [schemas.adjust_schema([f"{100000 + i}" for i in range(20)],
                                              list(cfg["factors"]), 0.2),
                        schemas.news_schema(list(cfg["universe"]["sector_etfs"])),
                        schemas.disclosure_schema(["a", "b"])]
        for schema in schemas_made:
            self.assertNotIn("maxItems", schema, schema.get("type"))
            self.assertNotIn("maxItems", schema.get("items") or {})


# ---------------------------------------------------------------- 호출부

class ClientTest(LLMTestBase):
    def call(self, llm, user="입력", task="t"):
        return llm.call(task, "시스템", user, SIMPLE_SCHEMA, "classify")

    def test_ok_path_logs_row_with_tokens(self):
        llm, fake = self.client([body({"level": 1, "kind": "a", "conf": 0.8}, tin=120, tout=30,
                                      thoughts=12)])
        res = self.call(llm)
        self.assertEqual(res.status, "OK")
        self.assertEqual(res.data, {"level": 1, "kind": "a", "conf": 0.8})
        self.assertFalse(res.cache_hit)
        row = self.rows("llm_call")[0]
        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["run_id"], 1)
        self.assertEqual(row["model"], self.cfg["llm"]["models"]["classify"])
        self.assertEqual(row["tokens_in"], 120)
        self.assertEqual(row["tokens_out"], 42, "사고 토큰은 출력에 더한다 (출력 단가로 과금된다)")
        self.assertEqual(row["cache_hit"], 0)
        self.assertIn("시스템", row["input_text"])
        price = self.cfg["llm"]["price_per_mtok"][self.cfg["llm"]["models"]["classify"]]
        self.assertAlmostEqual(row["cost_usd"], (120 * price["in"] + 42 * price["out"]) / 1e6,
                               msg="설정의 단가표로 계산한다 (사고 토큰도 출력 단가로 센다)")
        self.assertEqual(json.loads(row["parsed_json"])["kind"], "a")
        # 구조화 출력이 실제로 켜져 나갔는가 (결정 15의 1번)
        cfg_sent = fake.models.calls[0]["config"]
        self.assertEqual(cfg_sent["response_mime_type"], "application/json")
        self.assertEqual(cfg_sent["response_schema"], SIMPLE_SCHEMA)
        self.assertEqual(cfg_sent["temperature"], 0.0)
        self.assertEqual(self.rows("model_registry")[0]["model"], row["model"])

    def test_unknown_price_is_not_guessed(self):
        """단가를 모르는 모델은 비용을 0 으로 적지 않는다 — 예산 검사가 거짓말하면 상한이 무의미하다."""
        llm, _ = self.client([body({"level": 1, "kind": "a", "conf": 0.5})], price_per_mtok={})
        res = self.call(llm)
        self.assertEqual(res.status, "OK")
        self.assertIsNone(res.cost_usd)
        self.assertIsNone(self.rows("llm_call")[0]["cost_usd"])
        self.assertEqual(llm.stats.cost_unknown, 1)

    def test_schema_violation_is_error_not_guess(self):
        llm, fake = self.client([body({"level": 7, "kind": "a", "conf": 0.5})])
        res = self.call(llm)
        self.assertEqual(res.status, "ERROR")
        self.assertIsNone(res.data)
        self.assertIn("schema", res.error)
        row = self.rows("llm_call")[0]
        self.assertEqual(row["status"], "ERROR")
        self.assertIsNone(row["parsed_json"])
        self.assertIn("7", row["output_text"], "원본 응답은 남긴다 (사후 점검용)")

    def test_unparsable_response_is_error(self):
        llm, _ = self.client([FakeResponse("죄송합니다, 답할 수 없습니다")])
        self.assertEqual(self.call(llm).status, "ERROR")

    def test_exception_and_timeout_never_raise(self):
        llm, fake = self.client([RuntimeError("boom"), RuntimeError("boom again")])
        res = self.call(llm)
        self.assertEqual(res.status, "ERROR")
        self.assertIn("RuntimeError", res.error)
        self.assertEqual(fake.n_calls, 2, "실패는 설정된 횟수만큼 재시도한다 (llm.retries=1)")

        llm2, fake2 = self.client([asyncio.TimeoutError(), asyncio.TimeoutError()])
        res2 = self.call(llm2, user="다른 입력")
        self.assertEqual(res2.status, "ERROR")
        self.assertIn("timeout", res2.error)
        self.assertEqual([r["status"] for r in self.rows("llm_call")], ["ERROR", "ERROR"])

    def test_cache_hit_writes_row_without_calling_api(self):
        llm, fake = self.client([body({"level": -1, "kind": "b", "conf": 0.3})])
        first = self.call(llm)
        second = self.call(llm)
        self.assertEqual(fake.n_calls, 1, "같은 입력 해시는 API 를 다시 부르지 않는다")
        self.assertTrue(second.cache_hit)
        self.assertEqual(second.data, first.data)
        rows = self.rows("llm_call")
        self.assertEqual(len(rows), 2, "캐시도 행을 남긴다 (실행별 호출 수가 온전해야 한다)")
        self.assertEqual(rows[1]["cache_hit"], 1)
        self.assertEqual((rows[1]["tokens_in"], rows[1]["tokens_out"]), (0, 0))
        self.assertEqual(rows[1]["cost_usd"], 0.0)
        self.assertEqual(rows[0]["input_hash"], rows[1]["input_hash"])
        # 프롬프트 버전이 달라지면 다른 호출이다
        llm.prompt_ver = "v2"
        llm._client.models.responses.append(body({"level": 0, "kind": "a", "conf": 0.1}))
        self.assertFalse(self.call(llm).cache_hit)

    def test_budget_skip_by_cost_and_by_call_count(self):
        llm, fake = self.client([body({"level": 0, "kind": "a", "conf": 0.1})],
                                daily_budget_usd=1.0)
        self.store.conn.execute(
            "INSERT INTO llm_call(call_id,run_id,task,model,prompt_ver,input_hash,status,cost_usd,"
            "cache_hit,created_at) VALUES('x',1,'t','m','v1','h','OK',1.5,0,?)",
            (self.store.now_wall(),))
        res = self.call(llm)
        self.assertEqual(res.status, "SKIPPED_BUDGET")
        self.assertEqual(fake.n_calls, 0, "예산을 넘으면 부르지 않는다")
        self.assertEqual(self.rows("llm_call", status="SKIPPED_BUDGET")[0]["run_id"], 1)

        # 단가를 모르면 비용 합이 0 이라 막히지 않는다 → 호출 수 상한이 그 자리를 대신한다
        llm2, fake2 = self.client([body({"level": 0, "kind": "a", "conf": 0.1})] * 3,
                                  daily_budget_usd=None, max_calls_per_day=1)
        self.assertEqual(self.call(llm2, user="가").status, "SKIPPED_BUDGET",
                         "이미 오늘 2건이 기록돼 상한을 넘었다")

    def test_disabled_when_switched_off(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["llm"]["enabled"] = False
        llm = cl.LLMClient(cfg, self.store, run_id=1)
        res = self.call(llm)
        self.assertEqual(res.status, "DISABLED")
        self.assertIsNone(res.data)
        self.assertEqual(self.rows("llm_call")[0]["status"], "DISABLED")

    def test_disabled_when_key_missing(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["llm"]["api_key_env"] = "ADVISOR_TEST_KEY_THAT_DOES_NOT_EXIST"
        llm = cl.LLMClient(cfg, self.store, run_id=1)
        self.assertFalse(llm.enabled)
        self.assertEqual(self.call(llm).status, "DISABLED")


# ---------------------------------------------------------------- 공시 채점

def disclosure(rcept_no="20260923000001", code="005930", title="단일판매ㆍ공급계약체결",
               rcept_dt="20260923", seen="2026-09-23T09:00:00.000", kind=None, ratio=None,
               ratio_ok=None, corp="테스트전자"):
    return {"rcept_no": rcept_no, "stock_code": code, "corp_name": corp, "report_nm": title,
            "rcept_dt": rcept_dt, "first_seen_at": seen, "first_seen_src": "test",
            "ls_realkey": None, "kind": kind if kind is not None else ds.kind_of(title),
            "ratio": ratio, "ratio_ok": ratio_ok, "body_src": None}


class DisclosureCodePathTest(LLMTestBase):
    """코드로 확정되는 경우는 LLM 을 부르지 않는다 (설계 5.4의 2번)."""

    def decide(self, title, **kw):
        rec = disclosure(title=title, **kw)
        return ds.code_decide(rec["report_nm"], rec["kind"], rec["ratio"], rec["ratio_ok"], self.cfg)

    def test_code_decided_cases(self):
        self.assertEqual(self.decide("유상증자결정(제3자배정)")[:2], ("rights_third", 1))
        self.assertEqual(self.decide("유상증자결정(주주배정후 실권주 일반공모)")[:2],
                         ("rights_public", -2))
        self.assertEqual(self.decide("전환사채권발행결정")[:2], ("convertible", -1))
        self.assertEqual(self.decide("주요사항보고서(자기주식취득결정)")[:2], ("buyback", 1))
        self.assertEqual(self.decide("단일판매ㆍ공급계약체결", ratio=25.0, ratio_ok=1)[:2],
                         ("supply_large", 1))
        self.assertEqual(self.decide("단일판매ㆍ공급계약체결", ratio=2.5, ratio_ok=1)[:2],
                         ("supply_small", 0))
        self.assertEqual(self.decide("(정정)분기보고서")[:2], ("routine", 0))
        self.assertEqual(self.decide("사업보고서 (2025.12)")[:2], ("routine", 0))

    def test_ambiguous_cases_go_to_llm(self):
        self.assertIsNone(self.decide("유상증자결정"), "배정 방식이 제목에 없다")
        self.assertIsNone(self.decide("단일판매ㆍ공급계약체결", ratio=None),
                          "비율을 모르면 코드가 정하지 않는다")
        self.assertIsNone(self.decide("단일판매ㆍ공급계약체결", ratio=30.0, ratio_ok=0),
                          "검산이 깨진 비율은 믿지 않는다")
        self.assertIsNone(self.decide("자기주식처분결정"), "처분은 방향이 반대다")
        self.assertIsNone(self.decide("매출액또는손익구조30%이상변동"), "실적은 글 판단이다")
        self.assertIsNone(self.decide("투자판단 관련 주요경영사항"))
        self.assertIsNone(self.decide("(정정)감사보고서 제출(의견거절)"),
                          "감사의견이 걸린 정정은 '단순 정정'이 아니다")

    def test_code_path_does_not_call_the_model(self):
        self.store.upsert_disclosure(disclosure(title="전환사채권발행결정"))
        self.store.commit()
        llm, fake = self.client([])
        out = ds.score_disclosures(self.store, self.cfg, self.cal, "2026-09-23T18:30", llm)
        self.assertEqual(fake.n_calls, 0)
        self.assertEqual(out.n_code, 1)
        self.assertEqual(out.n_llm, 0)
        row = out.factor_rows[0]
        self.assertEqual(row["entity"], "005930")
        self.assertEqual(row["factor_id"], "stk_disclosure")
        self.assertEqual(row["missing"], 0, "공시가 있었으면 결측이 아니다 (0점이어도)")
        self.assertAlmostEqual(row["score"], -0.5, places=6)   # level -1 → -0.5, 당일이라 감쇠 1.0


class DisclosureLLMPathTest(LLMTestBase):
    def test_llm_scores_ambiguous_disclosure(self):
        self.store.upsert_disclosure(disclosure(title="매출액또는손익구조30%이상변동"))
        self.store.commit()
        llm, fake = self.client([body({"level": 2, "rubric_id": "earnings_up",
                                       "evidence": "영업이익 전년 대비 210% 증가", "confidence": 0.8})])
        out = ds.score_disclosures(self.store, self.cfg, self.cal, "2026-09-23T18:30", llm)
        self.assertEqual(fake.n_calls, 1)
        self.assertEqual(out.n_llm, 1)
        self.assertAlmostEqual(out.factor_rows[0]["score"], 1.0)
        self.assertIn("earnings_up", out.evidence_rows[0]["summary"])
        self.assertEqual(out.evidence_rows[0]["ref_type"], "disclosure")
        self.assertEqual(out.evidence_rows[0]["ref_id"], "20260923000001")
        user = fake.models.calls[0]["contents"]
        self.assertIn("[제목] 매출액또는손익구조30%이상변동", user)
        self.assertIn("공시종류=실적", user, "코드가 뽑은 수치를 함께 준다")

    def test_rubric_level_from_config_wins_over_model_number(self):
        """기준표에 값이 고정된 항목은 코드가 수준을 정한다 (숫자는 코드, 글은 LLM)."""
        self.store.upsert_disclosure(disclosure(title="유상증자결정"))
        self.store.commit()
        llm, _ = self.client([body({"level": 0, "rubric_id": "rights_public",
                                    "evidence": "주주배정", "confidence": 0.6})])
        out = ds.score_disclosures(self.store, self.cfg, self.cal, "2026-09-23T18:30", llm)
        self.assertEqual(out.scored[0].level, -2)
        self.assertAlmostEqual(out.factor_rows[0]["score"], -1.0)

    def test_other_material_keeps_model_level(self):
        self.store.upsert_disclosure(disclosure(title="투자판단 관련 주요경영사항"))
        self.store.commit()
        llm, _ = self.client([body({"level": 1, "rubric_id": "other_material",
                                    "evidence": "공급 물량 확대", "confidence": 0.4})])
        out = ds.score_disclosures(self.store, self.cfg, self.cal, "2026-09-23T18:30", llm)
        self.assertEqual(out.scored[0].level, 1)

    def test_model_error_abstains_and_leaves_no_score(self):
        self.store.upsert_disclosure(disclosure(title="유상증자결정"))
        self.store.commit()
        llm, _ = self.client([RuntimeError("down"), RuntimeError("down")])
        out = ds.score_disclosures(self.store, self.cfg, self.cal, "2026-09-23T18:30", llm)
        self.assertEqual(out.n_error, 1)
        self.assertEqual(out.factor_rows, [], "기권한 공시는 점수를 만들지 않는다 (결측으로 남는다)")

    def test_governance_flag_from_title_and_from_quote(self):
        self.store.upsert_disclosure(disclosure(rcept_no="A1", title="횡령·배임 혐의발생"))
        self.store.upsert_disclosure(disclosure(rcept_no="A2", code="000660",
                                                title="투자판단 관련 주요경영사항"))
        self.store.commit()
        llm, _ = self.client(always({"level": -2, "rubric_id": "other_material",
                                     "evidence": "감사의견 거절 사유 발생", "confidence": 0.7}))
        out = ds.score_disclosures(self.store, self.cfg, self.cal, "2026-09-23T18:30", llm)
        flags = {(f["entity"], f["src"]) for f in out.flag_rows}
        self.assertIn(("005930", "code"), flags, "제목에서 보이면 코드가 붙인다")
        self.assertIn(("000660", "llm"), flags, "본문 인용에서 보이면 LLM 이 붙인다")
        self.assertTrue(all(f["flag_type"] == "governance" for f in out.flag_rows))


class DisclosureTimeTest(LLMTestBase):
    """시점 규칙 (설계 2.3) — 이 테스트가 깨지면 그날 점수 전체를 믿을 수 없다."""

    def test_disclosure_first_seen_after_as_of_is_invisible(self):
        self.store.upsert_disclosure(disclosure(rcept_no="L1", title="전환사채권발행결정",
                                                seen="2026-09-23T19:05:00.000"))
        self.store.commit()
        llm, _ = self.client([])
        early = ds.score_disclosures(self.store, self.cfg, self.cal, "2026-09-23T18:30", llm)
        self.assertEqual(early.factor_rows, [], "18:30 판단은 19:05 에 알게 될 공시를 모른다")
        late = ds.score_disclosures(self.store, self.cfg, self.cal, "2026-09-24T07:40", llm)
        self.assertEqual(len(late.factor_rows), 1, "다음 판단에서는 보인다")

    def test_missing_first_seen_falls_back_to_dart_close(self):
        self.store.upsert_disclosure(disclosure(rcept_no="L2", title="전환사채권발행결정", seen=None))
        self.store.commit()
        llm, _ = self.client([])
        noon = ds.score_disclosures(self.store, self.cfg, self.cal, "2026-09-23T12:00", llm)
        self.assertEqual(noon.factor_rows, [], "수신 시각을 모르면 그날 18:00 에 알았다고 본다")
        evening = ds.score_disclosures(self.store, self.cfg, self.cal, "2026-09-23T18:30", llm)
        self.assertEqual(len(evening.factor_rows), 1)

    def test_decay_counts_trading_days_across_a_holiday(self):
        """9/24·9/25 는 휴장이다. 9/23 공시를 9/28 에 보면 경과는 1거래일이지 5일이 아니다."""
        self.assertEqual(ds.elapsed_trading_days(self.cal, ds.to_datetime("2026-09-23").date(),
                                                 ds.to_datetime("2026-09-28").date()), 1)
        self.store.upsert_disclosure(disclosure(rcept_no="D1", title="주요사항보고서(자기주식취득결정)"))
        self.store.commit()
        llm, _ = self.client([])
        out = ds.score_disclosures(self.store, self.cfg, self.cal, "2026-09-28T18:30", llm)
        sc = out.scored[0]
        self.assertEqual(sc.elapsed, 1)
        self.assertAlmostEqual(sc.decay, 1.0 - 1.0 / 20)
        self.assertAlmostEqual(out.factor_rows[0]["score"], 0.5 * (1.0 - 1.0 / 20), places=6)
        self.assertAlmostEqual(out.factor_rows[0]["raw_value"], 0.5, places=6,
                               msg="raw_value 는 감쇠 전 합이다 (사후에 다른 감쇠를 적용할 수 있게)")

    def test_out_of_window_disclosure_is_not_counted(self):
        self.store.upsert_disclosure(disclosure(rcept_no="O1", rcept_dt="20260701",
                                                seen="2026-07-01T09:00:00.000",
                                                title="전환사채권발행결정"))
        self.store.commit()
        llm, _ = self.client([])
        out = ds.score_disclosures(self.store, self.cfg, self.cal, "2026-09-28T18:30", llm)
        self.assertEqual(out.factor_rows, [], "감쇠 창 밖은 계수 0 이라 아예 보지 않는다")

    def test_sum_is_clipped_to_scale(self):
        for i in range(4):
            self.store.upsert_disclosure(disclosure(rcept_no=f"C{i}", title="전환사채권발행결정"))
        self.store.commit()
        llm, _ = self.client([])
        out = ds.score_disclosures(self.store, self.cfg, self.cal, "2026-09-23T18:30", llm)
        self.assertAlmostEqual(out.factor_rows[0]["score"], -1.0, msg="[-1,+1] 눈금을 넘지 않는다")
        self.assertAlmostEqual(out.factor_rows[0]["raw_value"], -2.0)


# ---------------------------------------------------------------- 뉴스 분류

def news(key, when, title, code=None, source="21"):
    return (key, when, code, source, title)


class NewsPrefilterTest(LLMTestBase):
    def test_noise_duplicates_and_time_are_filtered(self):
        rows = [
            news("k1", "2026-09-23T09:00:00.000", "[특징주] 테스트전자, 외국인 순매수에 강세"),
            news("k2", "2026-09-23T09:10:00.000", "미국, 반도체 관세 전면 도입 발표"),
            news("k3", "2026-09-23T09:20:00.000", "미국, 반도체 관세 전면 도입 발표"),
            news("k4", "2026-09-23T19:00:00.000", "장 마감 후 나온 기사"),
            news("k5", "2026-09-22T09:00:00.000", "창 이전의 오래된 기사"),
            news("k6", "2026-09-23T09:30:00.000", "공시 속보", source="15"),
        ]
        items, n_seen, n_noise = nr.prefilter(rows, self.cfg, "2026-09-23T18:30",
                                              "2026-09-23T00:00:00.000")
        self.assertEqual([it["key"] for it in items], ["k2"])
        self.assertEqual((n_seen, n_noise), (3, 1))
        self.assertEqual(items[0]["id"], "n1", "묶음 안에서 쓰는 id 는 짧게 다시 매긴다")

    def test_since_defaults_to_previous_run(self):
        self.store.start_run("prelim", "2026-09-22", decision_time="2026-09-22T18:30:00.000")
        self.store.commit()
        self.assertEqual(nr.since_time(self.store, self.cfg, "2026-09-23T07:40"),
                         "2026-09-22T18:30:00.000")
        # 직전 실행이 창보다 오래됐으면 창이 이긴다
        self.assertEqual(nr.since_time(self.store, self.cfg, "2026-09-30T07:40")[:10], "2026-09-29")


class NewsClassifyTest(LLMTestBase):
    def rows(self):
        return [news("k1", "2026-09-23T09:00:00.000", "중동 분쟁 확대, 반도체 공급망 차질 우려"),
                news("k2", "2026-09-23T10:00:00.000", "여당 대표, 특정 기업인과 회동", code="005930")]

    def run_classify(self, payload, as_of="2026-09-23T18:30", sectors=("반도체", "은행")):
        llm, fake = self.client([body(payload)])
        out = nr.classify_news(self.store, self.cfg, self.cal, as_of, llm, sectors=sectors,
                               recent_news=lambda store, cfg, as_of, since: self.rows(),
                               since="2026-09-23T00:00:00.000")
        return out, fake

    def test_sector_score_evidence_and_theme_flag(self):
        out, fake = self.run_classify([
            {"id": "n1", "category": "war", "sectors": ["반도체"], "direction": -2, "severity": 3,
             "theme_suspect": False, "reason": "중동 분쟁 확대"},
            {"id": "n2", "category": "key_person", "sectors": [], "direction": 0, "severity": 1,
             "theme_suspect": True, "reason": "정치인 회동"},
        ])
        self.assertEqual(fake.n_calls, 1, "한 묶음이면 한 번만 부른다")
        self.assertEqual(len(out.factor_rows), 1)
        row = out.factor_rows[0]
        self.assertEqual((row["entity"], row["factor_id"], row["missing"]),
                         ("반도체", "news_risk", 0))
        self.assertAlmostEqual(row["score"], -1.0, msg="방향 -2 → -1.0, 당일이라 감쇠 1.0")
        self.assertEqual(out.evidence_rows[0]["ref_type"], "news")
        self.assertEqual(out.evidence_rows[0]["ref_id"], "k1")
        self.assertEqual([(f["entity"], f["flag_type"], f["src"]) for f in out.flag_rows],
                         [("005930", "theme", "llm")])

    def test_unknown_id_and_unknown_sector_are_dropped(self):
        out, _ = self.run_classify([
            {"id": "n99", "category": "policy", "sectors": ["반도체"], "direction": 2,
             "severity": 2, "theme_suspect": False, "reason": "없는 id"},
            {"id": "n1", "category": "policy", "sectors": [], "direction": 2, "severity": 2,
             "theme_suspect": False, "reason": "섹터 없음"},
        ])
        self.assertEqual(out.factor_rows, [], "입력에 없는 id 와 섹터 없는 항목은 점수가 되지 않는다")

    def test_decay_applies_to_older_news(self):
        out, _ = self.run_classify(
            [{"id": "n1", "category": "war", "sectors": ["반도체"], "direction": -2, "severity": 3,
              "theme_suspect": False, "reason": "분쟁"},
             {"id": "n2", "category": "noise", "sectors": [], "direction": 0, "severity": 0,
              "theme_suspect": False, "reason": "잡음"}],
            as_of="2026-09-28T18:30")
        # news_risk 의 감쇠 기간은 5거래일, 9/23 → 9/28 은 1거래일 경과
        self.assertAlmostEqual(out.factor_rows[0]["score"], -1.0 * (1.0 - 1.0 / 5), places=6)

    def test_no_source_is_reported_not_raised(self):
        def boom(store, cfg, as_of, since):
            raise RuntimeError("lsnews 없음")
        llm, _ = self.client([])
        out = nr.classify_news(self.store, self.cfg, self.cal, "2026-09-23T18:30", llm,
                               sectors=["반도체"], recent_news=boom)
        self.assertEqual(out.status, "NO_SOURCE")
        self.assertEqual(out.factor_rows, [])

    def test_batches_are_split(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["llm"]["news_batch_size"] = 1
        fake = FakeClient(always([]))
        llm = cl.LLMClient(cfg, self.store, run_id=1, client=fake)
        self.addCleanup(llm.close)
        nr.classify_news(self.store, cfg, self.cal, "2026-09-23T18:30", llm,
                         sectors=["반도체"], recent_news=lambda store, cfg, as_of, since: self.rows(),
                         since="2026-09-23T00:00:00.000")
        self.assertEqual(fake.n_calls, 2, "묶음 크기 1 이면 두 건은 두 번 부른다")


# ---------------------------------------------------------------- 조정

class AdjustTest(LLMTestBase):
    def assets(self):
        return [{"code": "005930", "entity": "005930", "label": "", "composite": 0.4,
                 "held": True, "flags": ["spike"]},
                {"code": "000660", "entity": "000660", "label": "", "composite": 0.2,
                 "held": False, "flags": []}]

    def test_postprocess_clips_veto_and_unknown(self):
        records = [
            {"code": "005930", "adj": 0.9, "veto": True, "adopted": ["stk_high52"],
             "rejected": [{"factor_id": "stk_flow", "reason": "최근 수급 반전"}], "reason": "급등 경계"},
            {"code": "000660", "adj": -0.5, "veto": True, "adopted": [], "rejected": [],
             "reason": "표시 없음"},
            {"code": "999999", "adj": 0.1, "veto": False, "adopted": [], "rejected": [],
             "reason": "모르는 코드"},
        ]
        out = adj_mod.postprocess(records, self.assets(), self.cfg,
                                  flags={"005930": ["spike"]}, call_id="c1")
        self.assertEqual(set(out), {"005930", "000660"}, "후보 밖 코드는 버린다")
        self.assertAlmostEqual(out["005930"].adj, 0.2, msg="±adj_cap 으로 다시 자른다")
        self.assertTrue(out["005930"].vetoed)
        self.assertAlmostEqual(out["000660"].adj, -0.2)
        self.assertFalse(out["000660"].vetoed, "위험 표시가 없으면 거부는 무시된다 (결정 4)")
        self.assertEqual(out["005930"].rejected[0]["factor_id"], "stk_flow")
        self.assertEqual(out["005930"].call_id, "c1")

    def test_select_assets_covers_top_n_sector_etf_and_flagged_holdings(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["llm"]["top_n"] = 2
        comps = {"005930": 0.9, "000660": 0.5, "005380": 0.1, "068270": -0.3}
        assets = adj_mod.select_assets(cfg, comps, sector_scores={"반도체": 0.3},
                                       flags={"068270": ["halt"]}, holdings={"068270", "005380"},
                                       sector_etf={"반도체": "091160"})
        codes = [a["code"] for a in assets]
        self.assertEqual(codes[:2], ["005930", "000660"], "v0 상위 종목")
        self.assertIn("091160", codes, "섹터 ETF 후보")
        self.assertIn("068270", codes, "위험 표시가 붙은 보유 종목")
        self.assertNotIn("005380", codes, "표시 없는 보유 종목은 순위로만 들어온다")
        self.assertTrue(next(a for a in assets if a["code"] == "091160")["entity"] == "반도체",
                        "ETF 의 점수·근거는 섹터 이름으로 저장돼 있다")

    def test_run_adjust_sends_context_and_returns_adjustments(self):
        self.store.put_factor_values(1, [
            {"entity": "005930", "factor_id": "stk_high52", "raw_value": 0.98, "score": 0.8,
             "missing": 0},
            {"entity": "MARKET", "factor_id": "mkt_trend", "raw_value": 1.0, "score": 1.0,
             "missing": 0}])
        self.store.put_factor_evidence(1, [
            {"entity": "005930", "factor_id": "stk_disclosure", "ref_type": "disclosure",
             "ref_id": "R1", "summary": "buyback level=+1 자기주식 취득"}])
        self.store.commit()
        llm, fake = self.client([body([{"code": "005930", "adj": 0.1, "veto": False,
                                        "adopted": ["stk_high52"], "rejected": [],
                                        "reason": "자사주 매입"}])])
        out, status = adj_mod.run_adjust(self.store, self.cfg, llm, {"005930": 0.4},
                                         sector_scores={}, market_ctx={"score": 0.5},
                                         flags={}, holdings=set(), run_id=1, sector_etf={})
        self.assertEqual(status, "OK")
        self.assertAlmostEqual(out["005930"].adj, 0.1)
        user = fake.models.calls[0]["contents"]
        self.assertIn("시장 점수 = 0.500", user)
        self.assertIn("stk_high52", user)
        self.assertIn("자기주식 취득", user, "최근 근거를 함께 준다 (설계 5.6)")
        self.assertEqual(fake.models.calls[0]["model"], self.cfg["llm"]["models"]["adjust"])

    def test_error_means_no_adjustment(self):
        llm, _ = self.client([RuntimeError("x"), RuntimeError("x")])
        out, status = adj_mod.run_adjust(self.store, self.cfg, llm, {"005930": 0.4}, {}, {"score": 0},
                                         {}, set(), 1, sector_etf={})
        self.assertEqual((out, status), ({}, "ERROR"), "기권은 조정 없음이다 (중립과 구분)")

    def test_one_failed_chunk_does_not_throw_away_the_others(self):
        """묶음 하나가 죽어도 살아남은 묶음의 조정은 남는다 → 상태는 PARTIAL 이다.

        상태를 ERROR 로 올리면 호출부(`hooks.ADJUST_OK_STATUSES`)가 그날 조정을 통째로 버린다.
        2026-09-21 실행에서 실제로 그랬다 — 첫 묶음이 죽자 둘째 묶음의 조정이 함께 사라졌다.
        """
        llm, fake = self.client(
            [RuntimeError("첫 묶음"), RuntimeError("재시도"),
             body([{"code": "000660", "adj": 0.1, "veto": False, "adopted": ["stk_high52"],
                    "rejected": [], "reason": "둘째 묶음은 살았다"}])],
            adjust_max_assets_per_call=1)                  # 자산 둘 → 묶음 둘
        out, status = adj_mod.run_adjust(self.store, self.last_cfg, llm,
                                         {"005930": 0.9, "000660": 0.5}, {}, {"score": 0},
                                         {}, set(), 1, sector_etf={})
        self.assertEqual(fake.n_calls, 3, "첫 묶음 2회(재시도 포함) + 둘째 묶음 1회")
        self.assertEqual(status, "PARTIAL")
        self.assertEqual(set(out), {"000660"})
        self.assertAlmostEqual(out["000660"].adj, 0.1)


# ---------------------------------------------------------------- 단계 (run.py 훅)

class StageTest(LLMTestBase):
    def setUp(self):
        super().setUp()
        self.store.put_universe("2026-09-23", [
            {"code": "005930", "name": "테스트전자", "kind": "stock", "sector": "반도체"},
            {"code": "000660", "name": "테스트반도체", "kind": "stock", "sector": "반도체"}])
        self.store.commit()

    def start(self, mode="live"):
        rid = self.store.start_run("prelim", "2026-09-23", mode=mode,
                                   decision_time="2026-09-23T18:30:00.000")
        self.store.commit()
        return rid

    def placeholders(self, run_id):
        self.store.put_factor_values(run_id, [
            {"entity": "005930", "factor_id": "stk_disclosure", "raw_value": None, "score": 0.0,
             "missing": 1},
            {"entity": "000660", "factor_id": "stk_disclosure", "raw_value": None, "score": 0.0,
             "missing": 1},
            {"entity": "반도체", "factor_id": "news_risk", "raw_value": None, "score": 0.0,
             "missing": 1}])
        self.store.commit()

    def patch_client(self, responses):
        """LLMClient 가 가짜 SDK 를 쓰게 한다 (stage 는 클라이언트를 스스로 만든다)."""
        fake = FakeClient(responses)
        real = cl.LLMClient

        def make(cfg, store, run_id=None, client=None):
            return real(cfg, store, run_id, client=fake)
        cl.LLMClient = make
        stage.LLMClient = make
        self.addCleanup(lambda: (setattr(cl, "LLMClient", real),
                                 setattr(stage, "LLMClient", real)))
        return fake

    def test_replay_never_calls(self):
        rid = self.start(mode="replay")
        fake = self.patch_client([])
        got = stage.llm_factors(self.store, self.cfg, self.cal, rid, "2026-09-23T18:30", "prelim")
        self.assertEqual(got["status"], "SKIPPED_REPLAY")
        self.assertEqual(got["n_calls"], 0)
        adjusted = stage.llm_adjust(self.store, self.cfg, rid, {"005930": 0.4})
        self.assertEqual(adjusted["status"], "SKIPPED_REPLAY")
        self.assertEqual(adjusted["adjustments"], {})
        self.assertEqual(fake.n_calls, 0)
        self.assertEqual(self.rows("llm_call"), [], "재현 모드는 기록도 남기지 않는다")

    def test_factors_overwrite_placeholder_rows(self):
        rid = self.start()
        self.placeholders(rid)
        self.store.upsert_disclosure(disclosure(title="주요사항보고서(자기주식취득결정)"))
        self.store.commit()
        self.patch_client([])
        got = stage.llm_factors(self.store, self.cfg, self.cal, rid, "2026-09-23T18:30", "prelim")
        self.assertIn(got["status"], ("OK", "PARTIAL"))
        rows = {(r["entity"], r["factor_id"]): r
                for r in self.store.factor_values(rid)}
        filled = rows[("005930", "stk_disclosure")]
        self.assertEqual(filled["missing"], 0)
        self.assertAlmostEqual(filled["score"], 0.5)
        self.assertEqual(rows[("000660", "stk_disclosure")]["missing"], 1,
                         "공시가 없는 종목은 결측으로 남는다 (설계 5.1)")
        self.assertEqual(len(self.store.factor_values(rid)), 3, "표의 모양은 그대로다")
        ev = self.rows("factor_evidence")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["factor_id"], "stk_disclosure")

    def test_factors_are_idempotent_for_the_same_run(self):
        rid = self.start()
        self.placeholders(rid)
        self.store.upsert_disclosure(disclosure(title="전환사채권발행결정"))
        self.store.commit()
        self.patch_client([])
        stage.llm_factors(self.store, self.cfg, self.cal, rid, "2026-09-23T18:30", "prelim")
        stage.llm_factors(self.store, self.cfg, self.cal, rid, "2026-09-23T18:30", "prelim")
        self.assertEqual(len(self.rows("factor_evidence")), 1, "두 번 불려도 근거가 겹쳐 쌓이지 않는다")

    def test_disabled_records_status_and_keeps_placeholders(self):
        rid = self.start()
        self.placeholders(rid)
        self.store.upsert_disclosure(disclosure(title="유상증자결정"))   # LLM 이 필요한 건
        self.store.commit()
        cfg = copy.deepcopy(self.cfg)
        cfg["llm"]["enabled"] = False
        got = stage.llm_factors(self.store, cfg, self.cal, rid, "2026-09-23T18:30", "prelim")
        self.assertEqual(got["status"], "DISABLED")
        rows = {(r["entity"], r["factor_id"]): r for r in self.store.factor_values(rid)}
        self.assertEqual(rows[("005930", "stk_disclosure")]["missing"], 1)
        self.assertTrue(self.rows("llm_call"), "왜 값이 없는지는 llm_call 에 남는다")

    def test_adjust_hook_returns_adjustments(self):
        rid = self.start()
        self.store.put_factor_values(rid, [{"entity": "MARKET", "factor_id": "mkt_trend",
                                            "raw_value": 1.0, "score": 1.0, "missing": 0}])
        self.store.commit()
        self.patch_client([body([{"code": "005930", "adj": -0.3, "veto": True, "adopted": [],
                                  "rejected": [], "reason": "급등 경계"}])])
        got = stage.llm_adjust(self.store, self.cfg, rid, {"005930": 0.4, "000660": 0.1},
                               flags={"005930": ["spike"]}, holdings={"005930"})
        self.assertEqual(got["status"], "OK")
        a = got["adjustments"]["005930"]
        self.assertAlmostEqual(a.adj, -0.2)
        self.assertTrue(a.vetoed)
        self.assertEqual(got["n_calls"], 1)


class PromptTest(unittest.TestCase):
    """프롬프트 원칙 (설계 9장) 을 문구 수준에서 지킨다."""

    def setUp(self):
        self.cfg = load_config()

    def test_rubric_levels_come_from_config(self):
        text = prompts.disclosure_system(ds.rubric_rows(self.cfg))
        for row in ds.rubric_rows(self.cfg):
            self.assertIn(row["id"], text)
        self.assertIn("level -2", text, "기준표 수준을 그대로 옮긴다")
        self.assertIn("other_material (주요경영사항, LLM 이 수준을 정함)", text)

    def test_prompts_forbid_recomputation_and_future(self):
        for text in (prompts.disclosure_system(ds.rubric_rows(self.cfg)),
                     prompts.news_system(["반도체"]),
                     prompts.adjust_system(0.2)):
            self.assertIn("모른다", text, "판단 시각 이후를 암시하지 않는다")
        self.assertIn("다시 계산", prompts.adjust_system(0.2))
        self.assertIn("인용", prompts.disclosure_system(ds.rubric_rows(self.cfg)))

    def test_unknown_prompt_version_is_loud(self):
        with self.assertRaises(ValueError):
            prompts.news_system(["반도체"], ver="v99")


if __name__ == "__main__":
    unittest.main()
