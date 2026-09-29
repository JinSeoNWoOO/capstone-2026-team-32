"""LS OpenAPI 접속 래퍼 (공식 스펙 기준, LsApiHelper specs로 필드 확인).

토큰   POST {rest_base}/oauth2/token  form: grant_type=client_credentials, appkey, appsecretkey, scope=oob
        → {access_token, expires_in}. 토큰은 발급 익일 07:00에 만료된다(개인). collector가 매일 재발급.
REST   POST {rest_base}{path}  headers: content-type, authorization: Bearer, tr_cd, tr_cont, tr_cont_key, mac_address
        body {"<tr>InBlock": {...}}. TR별 초당 허용 건수(TPS)가 다르다.
실시간 wss://openapi.ls-sec.co.kr:9443/websocket (모의 29443)
        등록 header{token, tr_type:"3"} body{tr_cd, tr_key} / 해제 tr_type "4"
        뉴스 NWS(tr_key NWS001), 장운영 JIF(0), 체결 S3_(KOSPI)/K3_(KOSDAQ)/NS3(NXT, tr_key "N"+코드 10자리),
        호가 H1_/HA_/NH1, VI VI_/NVI.
        체결 body: shcode, chetime, price, cvolume, cgubun("+" 매수/"-" 매도), volume, value, exchname("KRX"/"NXT"), status

역할(role): "data" = 수신 전용 (실전 키를 써도 됨), "order" = 주문 전용 (모의 키 + 29443 접속 확인 필수).
"""
import asyncio, logging, time
from datetime import datetime
from urllib.parse import urlparse

log = logging.getLogger("newsgap.ls")

REST_BASE = "https://openapi.ls-sec.co.kr:8080"
WS_LIVE = "wss://openapi.ls-sec.co.kr:9443/websocket"
WS_PAPER = "wss://openapi.ls-sec.co.kr:29443/websocket"
PAPER_PORT = 29443

# TR별 초당 허용 건수 (공식 스펙 transaction_per_sec) 와 경로.
# t8412·t1301 은 스펙대로 부르면 실측에서 호출 제한(IGW00201)이 걸린다 → 간격을 넉넉히 (1/TPS 초).
TPS = {"t8430": 2, "t1101": 10, "t8412": 1 / 1.2, "t3102": 1, "t1301": 1 / 0.6, "t8411": 1}
PATH = {"t8430": "/stock/etc", "t1101": "/stock/market-data", "t8412": "/stock/chart", "t3102": "/stock/investinfo",
        "t1301": "/stock/market-data", "t8411": "/stock/chart"}


class LSAuthError(RuntimeError):
    pass

class LSApiError(RuntimeError):
    pass


def read_secret(path):
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def load_env_file(path=".env"):
    """KEY=VALUE 줄을 os.environ에 넣는다 (이미 있는 변수는 덮지 않음). 주석·빈 줄·따옴표 허용. 파일이 없으면 무시."""
    import os
    if not os.path.exists(path):
        return 0
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            if k and k not in os.environ:
                os.environ[k] = v
                n += 1
    return n


def credentials(which, cfg_ls, env_file=".env"):
    """which="paper"|"live". 우선순위: 환경변수(.env 포함) LS_{WHICH}_APP_KEY / LS_{WHICH}_APP_SECRET → cfg의 *_app_key_file 파일."""
    import os
    load_env_file(env_file)
    k, s = os.environ.get(f"LS_{which.upper()}_APP_KEY"), os.environ.get(f"LS_{which.upper()}_APP_SECRET")
    if k and s:
        return k.strip(), s.strip()
    kf, sf = cfg_ls.get(f"{which}_app_key_file"), cfg_ls.get(f"{which}_app_secret_file")
    if kf and sf and os.path.exists(kf) and os.path.exists(sf):
        return read_secret(kf), read_secret(sf)
    raise LSAuthError(f"{which} 키가 없습니다. .env 에 LS_{which.upper()}_APP_KEY / LS_{which.upper()}_APP_SECRET 를 넣으세요 (.env.example 참고)")


class LSClient:
    def __init__(self, app_key, app_secret, paper=True, rest_base=REST_BASE, ws_url=None, role="data"):
        self.app_key, self.app_secret = app_key, app_secret
        self.paper = paper
        self.role = role
        self.rest_base = rest_base.rstrip("/")
        self.ws_url = ws_url or (WS_PAPER if paper else WS_LIVE)
        self.ws_port = urlparse(self.ws_url).port or 443
        self.token = None
        self.token_expires_at = 0.0
        self.token_issued_wall = None
        self.connected_port = None      # 웹소켓 접속 성공 후 collector가 실제 원격 포트를 채운다
        self._session = None
        self._last = {}                 # tr_cd -> 마지막으로 예약된 호출 시각 monotonic (TPS 제한)
        self.rate_limited = 0           # IGW00201(호출 제한) 발생 횟수 (진단용)

    # ---- 안전장치 ----------------------------------------------------------
    def is_paper(self):
        """설정값(paper)과 실제 접속 포트(connected_port) 둘 다 29443이어야 True. 접속 전에는 False."""
        return bool(self.paper and self.ws_port == PAPER_PORT and self.connected_port == PAPER_PORT)

    def place_order(self, code, side, qty):
        if self.role != "order":
            raise RuntimeError("데이터 역할(role=data) 클라이언트로는 주문할 수 없습니다")
        if not self.is_paper():
            raise RuntimeError("모의투자 서버(29443) 접속이 확인되지 않아 주문을 보내지 않습니다")
        raise NotImplementedError("모의투자 주문 TR — 발표 전 구현 예정. 반드시 is_paper() 확인 후 호출")

    # ---- HTTP ---------------------------------------------------------------
    async def session(self):
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_token(self, force=False):
        if self.token and not force and time.time() < self.token_expires_at - 60:
            return self.token
        import aiohttp
        s = await self.session()
        data = {"grant_type": "client_credentials", "appkey": self.app_key,
                "appsecretkey": self.app_secret, "scope": "oob"}
        try:
            async with s.post(self.rest_base + "/oauth2/token", data=data,
                              headers={"content-type": "application/x-www-form-urlencoded"},
                              timeout=aiohttp.ClientTimeout(total=15)) as r:
                body = await r.json(content_type=None)
        except Exception as ex:
            raise LSAuthError(f"token request failed: {ex}") from ex
        if not isinstance(body, dict) or not body.get("access_token"):
            raise LSAuthError(f"token refused: {str(body)[:300]}")
        self.token = body["access_token"]
        self.token_expires_at = time.time() + int(body.get("expires_in") or 86400)
        self.token_issued_wall = datetime.now().isoformat(timespec="seconds")
        log.info("token issued (expires_in=%s)", body.get("expires_in"))
        return self.token

    def next_call_mono(self, tr):
        """지금 이 TR을 부르면 스로틀 때문에 실제로 나가는 시각(monotonic). 큐가 밀렸는지 판단하는 데 쓴다."""
        return max(time.monotonic(), self._last.get(tr, 0.0) + 1.0 / TPS.get(tr, 1))

    async def _throttle(self, tr):
        """호출 슬롯을 먼저 예약(_last 갱신)하고 그 시각까지 잔다. 예약식이라 대기 중인 호출까지
        next_call_mono 에 반영된다 (뉴스가 몰릴 때 큐가 얼마나 밀렸는지 알 수 있어야 한다)."""
        at = self._last[tr] = max(time.monotonic(), self._last.get(tr, 0.0) + 1.0 / TPS.get(tr, 1))
        wait = at - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

    async def call(self, tr_cd, body, tr_cont="N", tr_cont_key=""):
        """REST 호출. 연속조회는 응답의 data["_tr_cont"]=="Y" 이면 data["_tr_cont_key"] 와 body의 cts 필드를 넣어 다시 부른다.
        호출 제한(IGW00201)은 잠시 쉬고 재시도한다."""
        import aiohttp
        await self._throttle(tr_cd)
        tok = await self.get_token()
        headers = {"content-type": "application/json; charset=UTF-8", "authorization": f"Bearer {tok}",
                   "tr_cd": tr_cd, "tr_cont": tr_cont or "N", "tr_cont_key": tr_cont_key or "", "mac_address": ""}
        s = await self.session()
        url = self.rest_base + PATH[tr_cd]
        for attempt in range(4):
            async with s.post(url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json(content_type=None)
                limited = r.status == 429 or (isinstance(data, dict) and str(data.get("rsp_cd")) == "IGW00201")
                if limited:
                    self.rate_limited += 1
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if r.status >= 400:
                    raise LSApiError(f"{tr_cd} HTTP {r.status}: {str(data)[:200]}")
                if isinstance(data, dict):
                    if str(data.get("rsp_cd", "00000")) not in ("00000", "0"):
                        log.warning("%s rsp_cd=%s %s", tr_cd, data.get("rsp_cd"), data.get("rsp_msg"))
                    data["_tr_cont"] = r.headers.get("tr_cont", "N")
                    data["_tr_cont_key"] = r.headers.get("tr_cont_key", "")
                return data
        raise LSApiError(f"{tr_cd}: 호출 제한(IGW00201) 4회 초과")

    # ---- 조회 TR ------------------------------------------------------------
    async def t8430(self, gubun):
        """주식종목조회. gubun "1" KOSPI, "2" KOSDAQ, "0" 전체 → t8430OutBlock[{shcode,hname,gubun,...}]"""
        return await self.call("t8430", {"t8430InBlock": {"gubun": gubun}})

    async def t1101(self, shcode):
        """현재가·10호가. t1101OutBlock{price, offerho1, bidho1, offerrem1, bidrem1, volume, hotime, ...}"""
        return await self.call("t1101", {"t1101InBlock": {"shcode": shcode}})

    async def t8412(self, shcode, ncnt=1, qrycnt=30):
        """N분봉. t8412OutBlock1[{date,time,open,high,low,close,jdiff_vol,value(백만원),...}]"""
        return await self.call("t8412", {"t8412InBlock": {
            "shcode": shcode, "ncnt": ncnt, "qrycnt": qrycnt, "nday": "0", "sdate": "", "stime": "",
            "edate": "99999999", "etime": "", "cts_date": "", "cts_time": "", "comp_yn": "N"}})

    async def t1301(self, shcode, starttime, endtime, cts_time="", tr_cont="N", tr_cont_key=""):
        """시간대별 체결 (당일). starttime/endtime "HHMM". 페이지 20건, 최신순. 연속조회는 tr_cont/tr_cont_key + cts_time."""
        return await self.call("t1301", {"t1301InBlock": {"shcode": shcode, "cvolume": 0, "starttime": starttime,
                                                          "endtime": endtime, "cts_time": cts_time}}, tr_cont, tr_cont_key)

    async def t3102(self, newsno):
        """뉴스 본문. 개인 초당 1건. t3102OutBlock1[{sBody}], t3102OutBlock[{sJongcode}]"""
        return await self.call("t3102", {"t3102InBlock": {"sNewsno": newsno}})


class MockLSClient:
    """키 없이 tools/mock_ls_ws.py 에 붙기 위한 대역. REST 없음, 토큰 고정."""
    def __init__(self, ws_url="ws://127.0.0.1:8765/websocket"):
        self.paper = True
        self.role = "data"
        self.ws_url = ws_url
        self.ws_port = urlparse(ws_url).port
        self.token = "MOCK"
        self.token_expires_at = time.time() + 10 * 365 * 86400
        self.connected_port = None

    def is_paper(self):
        return False    # 목업은 절대 주문 경로에 쓰지 않는다

    async def get_token(self, force=False):
        return self.token

    async def close(self):
        pass
