from .models import Intent

def _window(ticks, t_now, sec):
    return [t for t in ticks if t_now - sec < t.sim_t <= t_now]

def amount_1m(ticks, t_now):
    return sum(t.price * t.qty for t in _window(ticks, t_now, 60))

def buy_ratio(ticks, t_now, sec):
    w = _window(ticks, t_now, sec)
    q = sum(t.qty for t in w)
    return (sum(t.qty for t in w if t.side == "B") / q) if q else 0.0

class SignalEngine:
    """이벤트별로 진입 1회, 청산 1회만 판단한다. 모든 판단 입력을 signal_log에 남긴다.

    진입 시각 = entry_delay_sec 이후 첫 틱, 단 AI 게이트가 켜져 있으면 AI 응답이 오거나
    entry_delay_sec + ai.max_wait_sec 가 지날 때까지 미룬다 (둘 중 늦은 쪽).
    규칙(거래대금·매수비중·수신 전 급증)을 통과한 뒤 AI 판정으로 실제/가상(counterfactual) 진입을 가른다."""
    def __init__(self, store, cfg, ai_cfg=None):
        self.store, self.c = store, cfg
        self.ai = ai_cfg or {}
        self.ai_gate = bool(self.ai.get("enabled"))            # 꺼져 있으면 예전처럼 규칙만으로 실제 진입
        self.ai_max_wait = float(self.ai.get("max_wait_sec", 0) or 0)
        self.state = {}   # event_id -> dict(entered, exited, in_pos, is_cf, entry_t, entry_price, peak_ratio)

    def _ready(self, e, t_now):
        """진입 판단을 지금 해도 되는가 (AI 응답 도착 또는 최대 대기 경과)."""
        if t_now < self.c["entry_delay_sec"]:
            return False
        if not self.ai_gate or e.ai_decision is not None:
            return True
        return t_now >= self.c["entry_delay_sec"] + self.ai_max_wait

    def _ai_verdict(self, e):
        """(counterfactual 여부, 사유). AI 게이트가 꺼져 있으면 항상 실제 진입."""
        if not self.ai_gate:
            return False, "entry_rule"
        d = e.ai_decision
        if d == "BUY":
            return False, "entry_rule"
        if d == "SKIP":
            return True, f"ai_skip conf={(e.ai_conf or 0):.2f}"
        if d == "ERROR":
            return True, "ai_error"
        return True, "ai_timeout"

    def evaluate(self, e, t_now):
        s = self.state.setdefault(e.event_id, dict(entered=False, exited=False, peak_ratio=0.0))
        if not e.ticks:
            return None
        price = e.ticks[-1].price
        # ---- 진입 판단: 조건이 갖춰진 시점에 한 번 ----
        if not s["entered"] and self._ready(e, t_now):
            s["entered"] = True   # 판단 자체는 1회 (진입 여부와 무관)
            a = amount_1m(e.ticks, t_now); br = buy_ratio(e.ticks, t_now, 60)
            base = e.baseline_amount_1m
            if base is None:
                self.store.signal(e.event_id, t_now, a, None, br, price, "SKIP", "no_baseline"); return None
            ps = e.pre_spike_mult
            if ps is None and self.c.get("require_prespike"):
                self.store.signal(e.event_id, t_now, a, base, br, price, "SKIP", "no_prespike_data"); return None
            mult = a / base if base else 0
            tail = f"mult={mult:.1f} br={br:.2f} pre_spike={'na' if ps is None else format(ps, '.1f')}"
            if ps is not None and ps >= self.c["pre_spike_max_mult"]:
                # 수신 전에 이미 급증 → ①이 끝난 뒤 들어가는 후행 진입. 거른다.
                self.store.signal(e.event_id, t_now, a, base, br, price, "SKIP", f"pre_spike={ps:.1f} {tail}")
                return None
            if mult >= self.c["amount_mult"] and br >= self.c["buy_ratio_min"]:
                cf, why = self._ai_verdict(e)
                s.update(in_pos=True, is_cf=cf, entry_t=t_now, entry_price=price, peak_ratio=br)
                self.store.signal(e.event_id, t_now, a, base, br, price, "BUY",
                                  f"{'cf ' if cf else ''}{why} {tail}")
                return Intent(e.event_id, "BUY", why, t_now, price, counterfactual=cf)
            self.store.signal(e.event_id, t_now, a, base, br, price, "SKIP", tail)
            return None
        # ---- 청산 판단: 보유 중 매 틱 ----
        if s.get("in_pos") and not s["exited"]:
            held = t_now - s["entry_t"]
            ret = (price - s["entry_price"]) / s["entry_price"] * 100
            br30 = buy_ratio(e.ticks, t_now, 30)
            s["peak_ratio"] = max(s["peak_ratio"], br30)
            reason = None
            if e.vi_flag:                                   reason = "vi"
            elif ret <= self.c["stop_pct"]:                 reason = "stop"
            elif held >= self.c["max_hold_sec"]:            reason = "max_hold"
            elif s["peak_ratio"] - br30 >= self.c["decay_from_peak"] and held > 30: reason = "flow_decay"
            if reason:
                s["exited"] = True
                self.store.signal(e.event_id, t_now, None, None, br30, price, "SELL", reason)
                return Intent(e.event_id, "SELL", reason, t_now, price, counterfactual=s.get("is_cf", False))
        return None

    def mark_cf(self, event_id, cf):
        """Executor가 RiskGuard로 막아 가상 포지션이 된 경우 청산 Intent도 가상이어야 한다."""
        s = self.state.get(event_id)
        if s:
            s["is_cf"] = bool(cf)

    def timeout_exit(self, e, t_now):
        """틱이 오지 않아도 최대 보유 시간이 지나면 청산 (Pipeline.tick_clock 에서 호출)."""
        s = self.state.get(e.event_id)
        if s and s.get("in_pos") and not s["exited"] and t_now - s["entry_t"] >= self.c["max_hold_sec"]:
            return self.force_exit(e, t_now, "max_hold")
        return None

    def force_exit(self, e, t_now, reason="window_end"):
        s = self.state.get(e.event_id)
        if s and s.get("in_pos") and not s["exited"] and e.ticks:
            s["exited"] = True
            price = e.ticks[-1].price
            self.store.signal(e.event_id, t_now, None, None, None, price, "SELL", reason)
            return Intent(e.event_id, "SELL", reason, t_now, price, counterfactual=s.get("is_cf", False))
        return None
