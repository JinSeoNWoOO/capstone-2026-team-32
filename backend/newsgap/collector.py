"""LS 실시간 뉴스·공시 속보 수집기 (asyncio + websockets).

토큰 발급 → 웹소켓 접속 → NWS(뉴스·공시 속보)·JIF(장운영) 등록 → 받은 뉴스를 news 표에 저장.
부수 작업: 일일 토큰 재발급·재접속, 무효 토큰 복구, 주기 commit, 원본/정규화 스트림을 data/raw/ 에 JSONL 로 기록.

판단 지원(advisor)이 이 DB 를 **읽기만** 한다 (공시 속보 → 공시 요인·인지 시각, 뉴스 → 뉴스 위험 분류·근거 시각).
뉴스 자동매매를 없애면서 종목별 체결·VI 구독, REST 보강, AI 판정, 진입·청산은 모두 뺐다 —
판단 지원은 뉴스 제목·수신 시각만 쓰므로 수집기가 할 일은 "빠짐없이 받아 적기" 하나다.
"""
import asyncio, json, logging, os, time
from datetime import datetime

from websockets.asyncio.client import connect

from .models import News

log = logging.getLogger("newsgap.collector")

INVALID_TOKEN_RSP_CODES = {"IGW00121"}   # 등록 응답에 이 코드가 오면 캐시된 토큰이 이미 무효 → 강제 재발급


def norm_code(raw):
    """NWS code(12자리 0패딩) → 6자리. 코드가 없으면 ""."""
    raw = (raw or "").strip()
    if not raw.strip("0"):
        return ""
    return raw.lstrip("0")[-6:].zfill(6)


class Collector:
    def __init__(self, cfg, store, ls):
        self.cfg = cfg
        self.c = dict(reauth_time="07:05:00", commit_interval_sec=5, heartbeat_sec=60,
                      raw_dir="./data/raw", mock=False)
        self.c.update(cfg.get("collector") or {})
        self.store, self.ls = store, ls
        self.ws = None
        self.subs = {("NWS", "NWS001"), ("JIF", "0")}   # 접속할 때마다 전부 다시 등록한다
        self.stop = asyncio.Event()
        self.stats = dict(frames=0, ctrl=0, nws=0, nws_coded=0, jif=0, reconnects=0)
        self.market_status = None                    # 마지막 JIF body (기록·점검용)
        self._force_reauth = False
        self._bad_token_close = False        # 이번 접속이 무효 토큰(IGW00121)으로 닫혔는지 (backoff 리셋 여부에 씀)
        now = datetime.now()
        self._reauth_day = now.strftime("%Y%m%d") if now.strftime("%H:%M:%S") >= self.c["reauth_time"] else None
        self._files = {}

    def request_stop(self):
        self.stop.set()
        if self.ws is not None:
            asyncio.get_running_loop().create_task(self.ws.close())

    # ---- 메인 루프 ------------------------------------------------------------
    async def run(self):
        backoff = 2
        self._log("INFO", f"collector start ws={self.ls.ws_url} mock={self.c['mock']}")
        while not self.stop.is_set():
            prev_backoff = backoff
            self._bad_token_close = False
            try:
                await self.ls.get_token(force=self._force_reauth)
                self._force_reauth = False
                async with connect(self.ls.ws_url, ping_interval=20, ping_timeout=20, open_timeout=15,
                                   max_size=4 * 1024 * 1024) as ws:
                    self.ws = ws
                    port = (ws.remote_address or (None, None))[1]
                    self.ls.connected_port = port
                    self._log("INFO", f"ws open {self.ls.ws_url} remote_port={port}")
                    backoff = 2
                    for tr_cd, tr_key in sorted(self.subs):
                        await self._register(ws, tr_cd, tr_key)
                    clock = asyncio.create_task(self._clock(ws))
                    try:
                        async for raw in ws:
                            await self._handle(raw)
                    finally:
                        clock.cancel()
                        self.ws = None
                        self.ls.connected_port = None
                self._log("INFO", "ws closed")
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                self._log("WARN", f"ws error: {type(ex).__name__}: {ex}")
            self.store.commit()
            if self.stop.is_set():
                break
            self.stats["reconnects"] += 1
            if self._bad_token_close:
                backoff = prev_backoff       # 무효 토큰으로 바로 닫힌 접속은 정상 접속으로 치지 않는다 (backoff 리셋 취소)
            self._log("INFO", f"reconnect in {backoff}s")
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)
        self.store.commit()
        self._close_files()
        self._log("INFO", "collector stopped " + json.dumps(self.stats))
        self.store.commit()

    async def _register(self, ws, tr_cd, tr_key):
        payload = {"header": {"token": self.ls.token, "tr_type": "3"}, "body": {"tr_cd": tr_cd, "tr_key": tr_key}}
        await ws.send(json.dumps(payload, ensure_ascii=False))
        log.debug("registered %s %s", tr_cd, tr_key)

    async def _clock(self, ws):
        last_commit = last_hb = time.monotonic()
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            if now - last_commit >= self.c["commit_interval_sec"]:
                self.store.commit(); last_commit = now
            if now - last_hb >= self.c["heartbeat_sec"]:
                self._log("INFO", "hb " + json.dumps(self.stats))
                last_hb = now
            if self._check_reauth(datetime.now()):
                self._log("INFO", "daily reauth: closing ws to reconnect with a fresh token")
                await ws.close()
                return

    def _check_reauth(self, wall):
        """일일 재발급(reauth_time)이나 만료 임박(expiring) 때 _force_reauth 를 세운다.
        만료 임박으로 reauth_time 전에 먼저 재발급했으면 _reauth_day 는 건드리지 않는다 —
        그래야 reauth_time 이후에 한 번 더(진짜 새 토큰을) 재발급한다. reauth_time 이후의
        재발급만 그날 치로 쳐서 _reauth_day 를 세운다."""
        day = wall.strftime("%Y%m%d")
        expiring = time.time() > getattr(self.ls, "token_expires_at", 1e18) - 120
        is_daily = wall.strftime("%H:%M:%S") >= self.c["reauth_time"] and self._reauth_day != day
        if not (is_daily or expiring):
            return False
        if is_daily:
            self._reauth_day = day
        self._force_reauth = True
        return True

    # ---- 수신 처리 ------------------------------------------------------------
    async def _handle(self, raw):
        now = time.monotonic()
        self.stats["frames"] += 1
        try:
            msg = json.loads(raw)
        except Exception:
            self._log("WARN", f"bad frame: {str(raw)[:200]}")
            return
        hdr = msg.get("header") or {}
        body = msg.get("body")
        tr = hdr.get("tr_cd") or ""
        self._write("ls", now, msg)
        if not body:                                   # 등록/해제 응답 등 제어 프레임
            self.stats["ctrl"] += 1
            self._log("INFO", f"ctrl {json.dumps(hdr, ensure_ascii=False)}")
            rsp_cd = str(hdr.get("rsp_cd") or "")
            if rsp_cd in INVALID_TOKEN_RSP_CODES and not self._force_reauth:
                # 캐시된 토큰이 이미 무효 → 다음 접속에서 강제 재발급. 한 접속에서 여러 번 와도
                # _force_reauth 가드로 한 번만 처리(ws.close() 중복 호출 방지)
                self._force_reauth = True
                self._bad_token_close = True
                self._log("WARN", f"invalid token ({rsp_cd}): forcing reauth, closing ws")
                await self.ws.close()
            return
        if tr == "NWS":
            self.stats["nws"] += 1
            if self.on_news(msg, now):
                self.stats["nws_coded"] += 1
            self._write("stream", now, msg, "news")
        elif tr == "JIF":
            self.stats["jif"] += 1
            self.store.raw(tr, msg)
            self.market_status = body
            self._write("stream", now, msg, "jif")
            self._log("INFO", f"JIF {json.dumps(body, ensure_ascii=False)}")
        else:
            log.debug("unhandled tr %s", tr)

    def on_news(self, msg, now_mono):
        """NWS 패킷 하나를 raw·news 표에 적는다. 종목 코드가 붙은 뉴스면 True.

        같은 realkey 가 재접속 등으로 다시 와도 news 는 INSERT OR IGNORE 라 첫 수신 시각이 남는다
        — 판단 지원은 이 recv_wall 을 "언제 알 수 있었나"로 쓰므로 뒤늦은 재수신이 덮으면 안 된다."""
        b = msg.get("body") or {}
        self.store.raw("NWS", msg)
        code = norm_code(b.get("code"))
        self.store.news(News(b.get("realkey", ""), (b.get("date") or "") + (b.get("time") or ""), code,
                             b.get("id", ""), b.get("title", ""), now_mono, self.store.now_wall()))
        return bool(code)

    # ---- 파일/로그 ------------------------------------------------------------
    def _write(self, kind, mono, msg, typ=None):
        """kind="ls": LS 원본 프레임. kind="stream": 정규화 메시지 (뉴스·장운영만)."""
        day = datetime.now().strftime("%Y%m%d")
        f = self._files.get(kind)
        if f is None or f[0] != day:
            if f:
                f[1].close()
            os.makedirs(self.c["raw_dir"], exist_ok=True)
            f = (day, open(os.path.join(self.c["raw_dir"], f"{day}.{kind}.jsonl"), "a", encoding="utf-8"))
            self._files[kind] = f
        rec = {"t": mono, "wall": self.store.now_wall(), "msg": msg}
        if typ:
            rec["type"] = typ
        f[1].write(json.dumps(rec, ensure_ascii=False) + "\n")
        f[1].flush()

    def _close_files(self):
        for _, fh in self._files.values():
            fh.close()
        self._files = {}

    def _log(self, level, msg):
        (log.warning if level == "WARN" else log.info)(msg)
        self.store.log(level, msg)
