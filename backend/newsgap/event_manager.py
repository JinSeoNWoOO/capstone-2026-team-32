from .models import Event

class EventManager:
    """뉴스에 종목코드가 붙어 오면 이벤트를 만들고, 구독 창(window) 동안 체결을 이벤트에 귀속시킨다.
    subscribe/unsubscribe 콜백은 실연결에서는 LS 웹소켓 등록/해제(collector), replay에서는 no-op."""
    def __init__(self, store, window_sec, subscribe=None, unsubscribe=None):
        self.store = store
        self.window = window_sec
        self.subscribe = subscribe or (lambda code: None)
        self.unsubscribe = unsubscribe or (lambda code: None)
        self.active = {}        # code -> Event
        # 재시작해도 이어지도록 DB의 마지막 event_id 다음부터 (안 그러면 실수집 DB에서 UNIQUE 충돌 → 접속이 끊긴다)
        self.next_id = store.next_event_id() if hasattr(store, "next_event_id") else 1

    def on_news(self, n, base_price=None, baseline_amount_1m=None, market="", t0_wall=""):
        self.store.news(n)
        if not n.code:
            return None
        if n.code in self.active:
            return None   # 같은 종목 창 안의 두 번째 뉴스: MVP에서는 무시 (TODO prev_event_id 연결)
        e = Event(self.next_id, n.realkey, n.code, n.recv_mono, base_price, baseline_amount_1m,
                  n.recv_mono + self.window, market=market, t0_wall=t0_wall or n.recv_wall)
        self.next_id += 1
        self.active[n.code] = e
        self.store.event(e)
        self.subscribe(n.code)
        return e

    def on_tick(self, t):
        e = self.active.get(t.code)
        if not e:
            return None
        if e.base_price is None:
            e.base_price = t.price
            self.store.event_update(e.event_id, base_price=t.price)
        e.ticks.append(t)
        self.store.tick(e.event_id, t)
        return e

    def on_vi(self, code, flag):
        e = self.active.get(code)
        if not e:
            return None
        e.vi_flag = int(flag)
        self.store.event_update(e.event_id, vi_flag=e.vi_flag)
        return e

    def expire(self, now_mono):
        done = [c for c, e in self.active.items() if now_mono >= e.sub_end_mono]
        for c in done:
            self.unsubscribe(c)
            yield self.active.pop(c)
