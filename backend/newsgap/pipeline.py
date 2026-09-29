"""메시지 입력 → EventManager → SignalEngine → Executor. replay와 실연결이 공유하는 부분.

입력 메시지 형식 (collector/replay가 이 형태로 정규화):
  NWS : LS 패킷 그대로  {"header":{"tr_cd":"NWS"},"body":{date,code(12자리),realkey,time,id,title}}
  TICK: {"header":{"tr_cd":"TICK"},"body":{code,time,price,qty,side("B"/"S"),sim_t,venue}}
  VI  : {"header":{"tr_cd":"VI"},"body":{code,flag(0 해제/1 발동),time,venue}}
  JIF : LS 패킷 그대로 (장운영정보, 기록만)
"""
import time
from .models import News, Tick
from .event_manager import EventManager
from .signal_engine import SignalEngine
from .executor import Executor

def norm_code(raw):
    """NWS code(12자리 0패딩) → 6자리. 코드가 없으면 ""."""
    raw = (raw or "").strip()
    if not raw.strip("0"):
        return ""
    return raw.lstrip("0")[-6:].zfill(6)

class Pipeline:
    def __init__(self, store, cfg, ls_client=None, baseline_lookup=None, subscribe=None, unsubscribe=None, market_lookup=None):
        self.store, self.cfg = store, cfg
        self.em = EventManager(store, cfg["event"]["window_sec"], subscribe, unsubscribe)
        self.se = SignalEngine(store, cfg["signal"], cfg.get("ai"))
        self.ex = Executor(store, cfg, cfg["env"], cfg["cost"]["round_trip_pct"], ls_client)
        # 기준선: replay에서는 상수 함수. 실연결에서는 None을 돌려주고 collector가 t8412로 비동기 보강한다.
        self.baseline_lookup = baseline_lookup or (lambda code: None)
        self.market_lookup = market_lookup or (lambda code: "")
        self.market_status = None   # 마지막 JIF body

    def on_message(self, msg, now_mono=None):
        now = now_mono if now_mono is not None else time.monotonic()
        tr = msg["header"].get("tr_cd")
        b = msg.get("body") or {}
        if tr != "TICK":                       # 체결은 tick 테이블에 있으므로 raw 중복 저장 안 함
            self.store.raw(tr, msg)
        if tr == "NWS":
            code = norm_code(b.get("code"))
            n = News(b.get("realkey", ""), (b.get("date") or "") + (b.get("time") or ""), code,
                     b.get("id", ""), b.get("title", ""), now, self.store.now_wall())
            # t0_wall: 이벤트 생성(뉴스 수신) 벽시계. collector가 t1301 조회 구간(수신 전 급증)을 계산할 때 쓴다.
            return self.em.on_news(n, baseline_amount_1m=self.baseline_lookup(code), market=self.market_lookup(code),
                                   t0_wall=n.recv_wall or self.store.now_wall())
        if tr == "TICK":
            t = Tick(b["code"], b["time"], int(b["price"]), int(b["qty"]), b["side"], now, float(b["sim_t"]),
                     b.get("venue", "KRX"))
            e = self.em.on_tick(t)
            if e:
                intent = self.se.evaluate(e, t.sim_t)
                if intent:
                    self._execute(e, intent)
            return e
        if tr == "VI":
            return self.em.on_vi(b["code"], b.get("flag", 1))
        if tr == "JIF":
            self.market_status = b
        return None

    def _execute(self, e, intent):
        o = self.ex.handle(e, intent)
        if intent.side == "BUY" and o is not None:
            self.se.mark_cf(e.event_id, o.counterfactual)   # RiskGuard에 막혀 가상이 됐으면 청산도 가상
        return o

    def tick_clock(self, now_mono):
        # 체결이 끊긴 종목은 틱이 없어 청산 판단이 돌지 않는다 → 시계로 최대 보유 시간을 강제한다.
        for e in list(self.em.active.values()):
            intent = self.se.timeout_exit(e, now_mono - e.t0_mono)
            if intent:
                self._execute(e, intent)
        for e in self.em.expire(now_mono):
            intent = self.se.force_exit(e, e.sub_end_mono - e.t0_mono)
            if intent:
                self._execute(e, intent)
