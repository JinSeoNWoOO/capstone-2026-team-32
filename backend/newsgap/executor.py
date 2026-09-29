from .models import Order

class LiveOrderBlocked(RuntimeError):
    pass

class RiskGuard:
    """설정으로 풀 수 없는 하드 규칙. 여기서 막히면 Executor는 주문을 보내지 않는다."""
    def __init__(self, cfg):
        self.c = cfg
        self.open_positions = 0
        self.realized_pnl = 0.0
        self.halted = False
        self.traded_events = set()

    def check_buy(self, intent, qty):
        if self.halted:                                         return "daily_loss_halt"
        if self.open_positions >= self.c["max_positions"]:      return "max_positions"
        if intent.ref_price * qty > self.c["max_order_krw"]:    return "max_order_krw"
        if intent.event_id in self.traded_events:               return "no_reentry"
        return None

    def on_close(self, pnl):
        self.realized_pnl += pnl
        if self.realized_pnl <= self.c["daily_loss_limit_krw"]:
            self.halted = True

class Executor:
    """SignalEngine의 Intent를 받아 RiskGuard를 통과시킨 뒤 주문 상태 머신을 돌린다.
    env=shadow : 주문 없이 ref_price로 즉시 체결된 것으로 기록 (가상 매매)
    env=paper  : LS 모의투자 REST 주문 (TODO ls_client.place_order) — 접속 포트가 29443임을 검증한 뒤에만
    env=live   : MVP 기간에는 무조건 예외

    counterfactual Intent(= AI 거절/무응답, 또는 RiskGuard에 막힌 실제 진입)는 RiskGuard·손익 집계를 건드리지 않고
    orders(ls_order_no="SHADOW_CF")·position(counterfactual=1)에만 남긴다. 이벤트별 측정과 포트폴리오 제약을 분리하기 위함."""
    def __init__(self, store, cfg, env, cost_pct, ls_client=None):
        if env not in ("shadow", "paper"):
            raise LiveOrderBlocked(f"env={env}: MVP 기간에는 실전 주문이 차단됩니다")
        self.store, self.c, self.env, self.cost = store, cfg, env, cost_pct
        self.ls = ls_client
        self.guard = RiskGuard(cfg["risk"])
        self.next_order_id = store.next_order_id() if hasattr(store, "next_order_id") else 1
        self.positions = {}   # event_id -> dict

    def _order(self, intent, qty, price, cf=False):
        o = Order(self.next_order_id, intent.event_id, intent.side, qty, "INTENT", intent.at_mono, counterfactual=cf)
        self.next_order_id += 1
        if cf or self.env == "shadow":
            o.state, o.fill_price = "FILLED", price
            o.ls_order_no = "SHADOW_CF" if cf else "SHADOW"
        else:
            if not self.ls or not self.ls.is_paper():
                raise LiveOrderBlocked("paper 모드인데 모의투자 서버 연결이 확인되지 않음")
            o.state = "SENT"
            o.ls_order_no = self.ls.place_order(code=None, side=intent.side, qty=qty)   # TODO 종목코드 전달
            # TODO 주문체결통보(WS) 수신 후 ACKED/FILLED 전이. MVP 데드라인 이후.
        self.store.order(o)
        return o

    def handle(self, event, intent):
        cf = bool(getattr(intent, "counterfactual", False))
        if intent.side == "BUY":
            qty = max(1, self.c["risk"]["max_order_krw"] // intent.ref_price)
            reason = intent.reason
            if not cf:
                why = self.guard.check_buy(intent, qty)
                if why:
                    # 막힌 진입도 이벤트별 측정은 남긴다: BLOCKED 신호 + 가상 포지션
                    self.store.signal(event.event_id, intent.at_mono, None, None, None, intent.ref_price, "BLOCKED", why)
                    cf, reason = True, f"blocked:{why}"
            o = self._order(intent, qty, intent.ref_price, cf)
            if not cf:
                self.guard.open_positions += 1
                self.guard.traded_events.add(intent.event_id)
            self.positions[intent.event_id] = dict(code=event.code, entry_price=o.fill_price, entry_t=intent.at_mono,
                                                   qty=qty, cf=cf, reason=reason)
            self.store.position(event_id=intent.event_id, code=event.code, entry_price=o.fill_price,
                                entry_t=intent.at_mono, qty=qty, counterfactual=int(cf), entry_reason=reason)
            return o
        if intent.side == "SELL" and intent.event_id in self.positions:
            p = self.positions.pop(intent.event_id)
            cf = p["cf"]
            o = self._order(intent, p["qty"], intent.ref_price, cf)
            pnl_raw = (o.fill_price - p["entry_price"]) * p["qty"]
            pnl_after = pnl_raw - (p["entry_price"] * p["qty"] * self.cost / 100)   # 왕복 비용(거래세+수수료) 차감
            if not cf:
                self.guard.open_positions -= 1
                self.guard.on_close(pnl_after)
            self.store.position(event_id=intent.event_id, code=p["code"], entry_price=p["entry_price"], entry_t=p["entry_t"],
                                exit_price=o.fill_price, exit_t=intent.at_mono, exit_reason=intent.reason, qty=p["qty"],
                                pnl_raw=pnl_raw, pnl_after_cost=pnl_after, counterfactual=int(cf), entry_reason=p["reason"])
            return o
        return None
