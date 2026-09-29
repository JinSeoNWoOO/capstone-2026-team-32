"""LS 실시간 수집기 (asyncio + websockets).

토큰 발급 → 웹소켓 접속 → NWS·JIF 등록
→ 뉴스에 종목코드가 붙으면 그 종목의 체결(S3_/K3_, 옵션 NS3)과 VI(VI_/NVI) 등록
→ 수신 패킷을 정규화해 Pipeline.on_message 로 넘김 (sim_t = 수신 monotonic − 이벤트 t0_mono)
→ 창(window)이 끝나면 해제.
부수 작업: 뉴스마다 t1101 스냅샷(첫 체결 전 가격·호가)과 t8412 1분봉(기준선 거래대금, 직전 분봉 종가) 비동기 보강,
           일일 토큰 재발급·재접속, 주기 commit, 원본/정규화 스트림을 data/raw/에 JSONL로 기록.
이 모듈은 데이터 역할 클라이언트만 받는다. 주문 경로는 여기 없다.
"""
import asyncio, json, logging, os, time
from datetime import datetime

from websockets.asyncio.client import connect

log = logging.getLogger("newsgap.collector")

# 수신 전 급증 지표의 정의(tools/reaction_seconds.py 와 동일): 10초 이동 거래량 / (1분봉 거래량 중앙값 / 6)
PRE_SPIKE_BIN_SEC = 10
PRE_SPIKE_MAX_PAGES = 3        # t1301 한 페이지 20건. 진입 판단 시각 전에 끝내야 하므로 상한을 둔다
ENRICH_MARGIN_SEC = 2          # 이 시간 안에 응답이 못 올 것 같으면 REST 호출 자체를 건너뛴다 (호출 제한 아끼기)

TICK_TRS = {"S3_": "KRX", "K3_": "KRX", "NS3": "NXT"}
VI_TRS = {"VI_": "KRX", "NVI": "NXT"}
MARKET_NAME = {"1": "KOSPI", "2": "KOSDAQ"}
INVALID_TOKEN_RSP_CODES = {"IGW00121"}   # 등록 응답에 이 코드가 오면 캐시된 토큰이 이미 무효 → 강제 재발급


def nxt_key(code):
    """NS3/NVI tr_key: "N"+종목코드, 10자리 우측 공백 패딩 (공식 예: "N010950   ")."""
    return f"N{code}".ljust(10)


class Collector:
    def __init__(self, cfg, store, pipeline, ls, market_map=None, judge=None):
        self.cfg = cfg
        self.c = dict(nxt=True, snapshot=True, baseline_bars=20, reauth_time="07:05:00",
                      commit_interval_sec=5, heartbeat_sec=60, raw_dir="./data/raw", mock=False)
        self.c.update(cfg.get("collector") or {})
        self.store, self.pipe, self.ls = store, pipeline, ls
        self.judge = judge                          # AIJudge/NullJudge (None이면 AI 판정 없음)
        self.market_map = market_map or {}          # code -> "1"(KOSPI) / "2"(KOSDAQ)
        self.ws = None
        self.subs = {("NWS", "NWS001"), ("JIF", "0")}   # 접속 시 항상 등록되어야 하는 것들 (재접속 시 전부 재등록)
        self.code_regs = {}                          # code -> [(tr_cd, tr_key), ...]
        self.outq = asyncio.Queue()
        self.stop = asyncio.Event()
        self.stats = dict(frames=0, ctrl=0, nws=0, nws_coded=0, ticks=0, vi=0, reconnects=0, enrich_fail=0,
                          ai_buy=0, ai_skip=0, ai_error=0)
        self._force_reauth = False
        self._bad_token_close = False        # 이번 접속이 무효 토큰(IGW00121)으로 닫혔는지 (backoff 리셋 여부에 씀)
        now = datetime.now()
        self._reauth_day = now.strftime("%Y%m%d") if now.strftime("%H:%M:%S") >= self.c["reauth_time"] else None
        self._files = {}
        # EventManager 콜백을 웹소켓 등록/해제에 연결
        pipeline.em.subscribe = self.subscribe_code
        pipeline.em.unsubscribe = self.unsubscribe_code
        pipeline.market_lookup = lambda code: MARKET_NAME.get(self.market_map.get(code, ""), "")

    # ---- 등록/해제 (동기 콜백 → 큐 → sender 태스크) ------------------------------
    def _regs_for(self, code):
        g = self.market_map.get(code)
        regs = [("S3_", code)] if g == "1" else [("K3_", code)] if g == "2" else [("S3_", code), ("K3_", code)]
        regs.append(("VI_", code))
        if self.c["nxt"]:
            regs += [("NS3", nxt_key(code)), ("NVI", nxt_key(code))]
        return regs

    def subscribe_code(self, code):
        regs = self._regs_for(code)
        self.code_regs[code] = regs
        for r in regs:
            self.subs.add(r)
            self.outq.put_nowait(("3", *r))

    def unsubscribe_code(self, code):
        for r in self.code_regs.pop(code, []):
            self.subs.discard(r)
            self.outq.put_nowait(("4", *r))

    def request_stop(self):
        self.stop.set()
        if self.ws is not None:
            asyncio.get_running_loop().create_task(self.ws.close())

    # ---- 메인 루프 ------------------------------------------------------------
    async def run(self):
        backoff = 2
        self._log("INFO", f"collector start ws={self.ls.ws_url} nxt={self.c['nxt']} mock={self.c['mock']}")
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
                    while not self.outq.empty():           # 이전 접속의 잔여 큐는 버리고 전부 재등록
                        self.outq.get_nowait()
                    for tr_cd, tr_key in sorted(self.subs):
                        self.outq.put_nowait(("3", tr_cd, tr_key))
                    tasks = [asyncio.create_task(self._sender(ws)), asyncio.create_task(self._clock(ws))]
                    try:
                        async for raw in ws:
                            await self._handle(raw)
                    finally:
                        for t in tasks:
                            t.cancel()
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

    async def _sender(self, ws):
        while True:
            tr_type, tr_cd, tr_key = await self.outq.get()
            payload = {"header": {"token": self.ls.token, "tr_type": tr_type}, "body": {"tr_cd": tr_cd, "tr_key": tr_key}}
            await ws.send(json.dumps(payload, ensure_ascii=False))
            log.debug("sent tr_type=%s %s %s", tr_type, tr_cd, tr_key)

    async def _clock(self, ws):
        last_commit = last_hb = time.monotonic()
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            self.pipe.tick_clock(now)                       # 창 만료 → force_exit → unsubscribe 콜백
            if now - last_commit >= self.c["commit_interval_sec"]:
                self.store.commit(); last_commit = now
            if now - last_hb >= self.c["heartbeat_sec"]:
                self._log("INFO", "hb " + json.dumps(self.stats) + f" active={sorted(self.pipe.em.active)}")
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
            e = self.pipe.on_message(msg, now)          # raw/news 저장, 코드 있으면 이벤트 생성 + subscribe 콜백
            self._write("stream", now, msg, "news")
            if e:
                self.stats["nws_coded"] += 1
                self._log("INFO", f"event {e.event_id} {e.code} {body.get('title', '')[:60]}")
                asyncio.create_task(self._enrich(e))
                if self.judge is not None:
                    asyncio.create_task(self._judge(e, body.get("title", ""), body.get("id", "")))
        elif tr in TICK_TRS:
            code = (body.get("shcode") or "").strip()
            e = self.pipe.em.active.get(code)
            if not e:
                return                                  # 해제 직후 도착한 잔여 체결
            t = {"header": {"tr_cd": "TICK"},
                 "body": {"code": code, "time": body.get("chetime", ""), "price": int(body["price"]),
                          "qty": int(body.get("cvolume") or 0), "side": "B" if body.get("cgubun") == "+" else "S",
                          "sim_t": now - e.t0_mono, "venue": body.get("exchname") or TICK_TRS[tr]}}
            self.stats["ticks"] += 1
            self.pipe.on_message(t, now)
            self._write("stream", now, t, "tick")
        elif tr in VI_TRS:
            code = (body.get("shcode") or "").strip()
            v = {"header": {"tr_cd": "VI"},
                 "body": {"code": code, "flag": 0 if str(body.get("vi_gubun", "0")) == "0" else 1,
                          "time": body.get("time", ""), "venue": VI_TRS[tr]}}
            self.stats["vi"] += 1
            self.pipe.on_message(v, now)
            self._write("stream", now, v, "vi")
            self._log("INFO", f"VI {code} gubun={body.get('vi_gubun')} {VI_TRS[tr]}")
        elif tr == "JIF":
            self.pipe.on_message(msg, now)
            self._write("stream", now, msg, "jif")
            self._log("INFO", f"JIF {json.dumps(body, ensure_ascii=False)}")
        else:
            log.debug("unhandled tr %s", tr)

    async def _enrich(self, e):
        """뉴스 직후 REST 보강: t1101(첫 체결 전 가격·호가) → t8412(기준선 거래대금·거래량, 직전 분봉 종가)
        → t1301(수신 전 급증 배수). 진입 판단(entry_delay_sec) 전에 끝나야 의미가 있다."""
        if self.c["mock"] or not self.c["snapshot"] or not hasattr(self.ls, "t1101"):
            return
        cols = {}
        try:
            q = await self.ls.t1101(e.code)
            blk = (q or {}).get("t1101OutBlock") or {}
            self.store.snapshot(e.event_id, "t0", time.monotonic(), _i(blk.get("price")), _i(blk.get("offerho1")),
                                _i(blk.get("bidho1")), _i(blk.get("offerrem1")), _i(blk.get("bidrem1")), _i(blk.get("volume")), blk)
            e.pre_price = _i(blk.get("price")) or None
            cols["pre_price"] = e.pre_price
        except Exception as ex:
            self.stats["enrich_fail"] += 1
            self._log("WARN", f"t1101 {e.code} failed: {ex}")
        try:
            if not self._in_budget("t8412", e):
                self._log("WARN", f"baseline skipped (backlog) {e.event_id} {e.code}")
            else:
                ch = await self.ls.t8412(e.code, ncnt=1, qrycnt=self.c["baseline_bars"])
                bars = sorted((ch or {}).get("t8412OutBlock1") or [], key=lambda b: (b.get("date", ""), b.get("time", "")))
                done = bars[:-1]                        # 마지막 봉은 진행 중일 수 있으므로 제외
                amts = sorted(_i(b.get("jdiff_vol")) * _i(b.get("close")) for b in done)
                vols = sorted(_i(b.get("jdiff_vol")) for b in done)
                if amts:
                    e.baseline_amount_1m = float(amts[len(amts) // 2])    # 중앙값 (급등 분봉의 영향 완화)
                    cols["baseline_amount_1m"] = e.baseline_amount_1m
                if vols:
                    e.baseline_vol_1m = float(vols[len(vols) // 2])       # 수신 전 급증 배수의 기준선(주 단위)
                    cols["baseline_vol_1m"] = e.baseline_vol_1m
                if done:
                    e.pre60_price = _i(done[-1].get("close")) or None
                    cols["pre60_price"] = e.pre60_price
        except Exception as ex:
            self.stats["enrich_fail"] += 1
            self._log("WARN", f"t8412 {e.code} failed: {ex}")
        try:
            ps = await self._pre_spike(e)
            if ps is not None:
                e.pre_spike_mult = ps
                cols["pre_spike_mult"] = ps
                self._log("INFO", f"pre_spike {e.event_id} {e.code} x{ps:.1f} "
                                  f"(base_vol={e.baseline_vol_1m} win={self.cfg['signal']['pre_spike_window_sec']}s)")
        except Exception as ex:
            self.stats["enrich_fail"] += 1
            self._log("WARN", f"t1301 {e.code} failed: {ex}")
        if cols:
            self.store.event_update(e.event_id, **cols)

    def _in_budget(self, tr, e):
        """이 TR을 지금 걸면 진입 판단 시각(entry_delay_sec-2초) 안에 나가는가. 큐가 밀렸으면 부르지 않는다."""
        nxt = getattr(self.ls, "next_call_mono", None)
        if nxt is None:
            return True
        return nxt(tr) <= e.t0_mono + self.cfg["signal"]["entry_delay_sec"] - ENRICH_MARGIN_SEC

    async def _pre_spike(self, e):
        """t0 직전 pre_spike_window_sec 초의 체결(t1301)에서 10초 이동 거래량의 기준선 대비 최대 배수.
        기준선 = baseline_vol_1m/6 (1분봉 거래량 중앙값의 10초분). 뉴스 수신 전에 이미 시장이 반응했는지 본다."""
        w = int(self.cfg["signal"]["pre_spike_window_sec"])
        if not e.baseline_vol_1m or not e.t0_wall or not hasattr(self.ls, "t1301"):
            return None
        t0 = _sod(e.t0_wall)
        if t0 is None:
            return None
        s_from = t0 - w
        if not self._in_budget("t1301", e):
            self._log("WARN", f"pre_spike skipped (backlog) {e.event_id} {e.code}")
            return None
        trades, cts, cont, key = [], "", "N", ""
        for _ in range(PRE_SPIKE_MAX_PAGES):                 # 페이지 20건·최신순
            d = await self.ls.t1301(e.code, _hhmm(s_from), _hhmm(t0), cts, cont, key)
            rows = d.get("t1301OutBlock1") or []
            for r in rows:
                sec = _sod_hhmmss(r.get("chetime"))
                if sec is not None and s_from <= sec < t0:
                    trades.append((sec, _i(r.get("cvolume"))))
            cts = (d.get("t1301OutBlock") or {}).get("cts_time", "")
            cont, key = d.get("_tr_cont", "N"), d.get("_tr_cont_key", "")
            oldest = min((_sod_hhmmss(r.get("chetime")) or t0) for r in rows) if rows else s_from
            if len(rows) < 20 or cont != "Y" or oldest < s_from:
                break
        expected = max(e.baseline_vol_1m / (60.0 / PRE_SPIKE_BIN_SEC), 1.0)   # 평상시 10초 거래량
        rel = [(sec - t0, q) for sec, q in trades]
        best = 0.0
        for s in range(-w + PRE_SPIKE_BIN_SEC, 1):           # 슬라이딩 창의 최대치
            v = sum(q for r, q in rel if s - PRE_SPIKE_BIN_SEC < r <= s)
            if v > best:
                best = v
        return round(best / expected, 2)

    async def _judge(self, e, title, source_id):
        """AI 제목 판정(핫패스). 응답이 오면 이벤트에 표시하고 news 테이블에 라벨을 남긴다.
        진입 시각은 SignalEngine이 entry_delay_sec 와 이 응답 시각 중 늦은 쪽으로 잡는다."""
        try:
            j = await self.judge.judge(title, code=e.code, source_id=source_id)
        except Exception as ex:
            self.stats["ai_error"] += 1
            e.ai_decision, e.ai_conf, e.ai_ready_mono = "ERROR", 0.0, time.monotonic()
            self.store.event_update(e.event_id, ai_decision="ERROR")
            self.store.news_update(e.realkey, ai_decision="ERROR", ai_conf=0.0)
            self._log("WARN", f"ai {e.event_id} {e.code} failed: {type(ex).__name__}: {ex}")
            return
        decision = str(j.decision or "").upper()
        if decision not in ("BUY", "SKIP", "ERROR"):
            decision = "ERROR"
        conf = float(j.conf or 0.0)
        e.ai_decision, e.ai_conf, e.ai_ready_mono = decision, conf, time.monotonic()
        self.stats["ai_" + decision.lower()] += 1
        self.store.news_update(e.realkey, ai_decision=decision, ai_conf=conf, ai_model=j.model,
                               ai_prompt_ver=j.prompt_ver, ai_latency_ms=j.latency_ms)
        self.store.event_update(e.event_id, ai_decision=decision)
        self._log("INFO", f"ai {e.event_id} {e.code} {decision} conf={conf:.2f} {j.latency_ms:.0f}ms "
                          f"+{time.monotonic() - e.t0_mono:.1f}s {str(j.reason or '')[:40]}")

    # ---- 파일/로그 ------------------------------------------------------------
    def _write(self, kind, mono, msg, typ=None):
        """kind="ls": LS 원본 프레임. kind="stream": 정규화 메시지, replay/stream.jsonl 과 같은 형식 (재생 가능)."""
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


def _i(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def _sod_hhmmss(hhmmss):
    """"HHMMSS" → 자정 이후 초. 형식이 아니면 None."""
    s = (hhmmss or "").strip()
    if len(s) < 6 or not s[:6].isdigit():
        return None
    return int(s[0:2]) * 3600 + int(s[2:4]) * 60 + int(s[4:6])


def _sod(iso_wall):
    """ISO 벽시계("YYYY-MM-DDTHH:MM:SS.mmm") → 자정 이후 초."""
    try:
        t = iso_wall.split("T")[1]
        return int(t[0:2]) * 3600 + int(t[3:5]) * 60 + int(t[6:8])
    except (AttributeError, IndexError, ValueError):
        return None


def _hhmm(sec):
    sec = max(0, min(sec, 24 * 3600 - 60))
    return f"{sec // 3600:02d}{(sec % 3600) // 60:02d}"
