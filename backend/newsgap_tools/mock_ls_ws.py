"""LS 웹소켓 흉내 — 키 없이 collector를 끝까지 돌려보기 위한 것.

- 등록(tr_type 3)/해제(4) 메시지를 받아 rsp 프레임을 돌려주고 이력을 self.regs 에 남긴다.
- NWS 등록이 들어오면 replay 스트림(replay/gen.py 형식)의 뉴스를 NWS 패킷 그대로 시간 압축 재생한다.
  스트림의 체결(TICK) 줄은 건너뛴다 — 수집기는 뉴스만 받는다 (뉴스 자동매매를 없애며 체결 구독도 뺐다).
사용: python -m backend.newsgap_tools.mock_ls_ws [stream.jsonl] [speed]   → ws://127.0.0.1:8765/websocket
"""
import asyncio, json, sys

from websockets.asyncio.server import serve


class MockLS:
    def __init__(self, stream_path, speed=50.0):
        with open(stream_path, encoding="utf-8") as f:
            self.events = [json.loads(l) for l in f if l.strip()]
        self.speed = speed
        self.regs = []            # (tr_type, tr_cd, tr_key)
        self.subscribed = set()   # (tr_cd, tr_key)
        self.done = asyncio.Event()

    async def handler(self, ws):
        replay = None
        try:
            async for raw in ws:
                m = json.loads(raw)
                h, b = m.get("header") or {}, m.get("body") or {}
                key = (b.get("tr_cd"), (b.get("tr_key") or "").strip())
                self.regs.append((h.get("tr_type"), *key))
                if h.get("tr_type") == "3":
                    self.subscribed.add(key)
                elif h.get("tr_type") == "4":
                    self.subscribed.discard(key)
                await ws.send(json.dumps({"header": {"tr_cd": key[0], "tr_key": b.get("tr_key"),
                                                     "rsp_cd": "00000", "rsp_msg": "정상처리"}, "body": None}))
                if key == ("NWS", "NWS001") and h.get("tr_type") == "3" and replay is None:
                    replay = asyncio.create_task(self._replay(ws))
        finally:
            if replay:
                replay.cancel()

    async def _replay(self, ws):
        t_prev = self.events[0]["t"] if self.events else 0.0
        for ev in self.events:
            dt = (ev["t"] - t_prev) / self.speed
            t_prev = ev["t"]
            if dt > 0:
                await asyncio.sleep(dt)
            msg = ev["msg"]
            if msg["header"].get("tr_cd") == "NWS":
                await ws.send(json.dumps(msg, ensure_ascii=False))
        self.done.set()

    async def serve(self, host="127.0.0.1", port=8765):
        return await serve(self.handler, host, port)


async def main(path, speed):
    mock = MockLS(path, speed)
    server = await mock.serve()
    print(f"mock LS ws on ws://127.0.0.1:8765/websocket  stream={path} speed={speed}x")
    await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "replay/stream.jsonl",
                     float(sys.argv[2]) if len(sys.argv) > 2 else 50.0))
