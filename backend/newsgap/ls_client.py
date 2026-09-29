"""LS OpenAPI 접속 래퍼 (공식 스펙 기준). 수집기는 토큰과 실시간 웹소켓만 쓴다.

토큰   POST {rest_base}/oauth2/token  form: grant_type=client_credentials, appkey, appsecretkey, scope=oob
        → {access_token, expires_in}. 토큰은 발급 익일 07:00에 만료된다(개인). collector가 매일 재발급.
실시간 wss://openapi.ls-sec.co.kr:9443/websocket (모의 29443)
        등록 header{token, tr_type:"3"} body{tr_cd, tr_key} / 해제 tr_type "4"
        뉴스 NWS(tr_key NWS001), 장운영 JIF(0).

뉴스 자동매매를 없애면서 REST 조회 TR(t1101·t8412·t1301·t8430·t3102)과 주문 자리표시는 뺐다 (git 이력에 있다).
이 모듈에는 주문 경로가 없다 — 수신 전용 키(실전 키 포함)를 써도 되는 이유다.
"""
import logging, time
from datetime import datetime

log = logging.getLogger("newsgap.ls")

REST_BASE = "https://openapi.ls-sec.co.kr:8080"
WS_LIVE = "wss://openapi.ls-sec.co.kr:9443/websocket"
WS_PAPER = "wss://openapi.ls-sec.co.kr:29443/websocket"


class LSAuthError(RuntimeError):
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
    def __init__(self, app_key, app_secret, paper=True, rest_base=REST_BASE, ws_url=None):
        self.app_key, self.app_secret = app_key, app_secret
        self.paper = paper
        self.rest_base = rest_base.rstrip("/")
        self.ws_url = ws_url or (WS_PAPER if paper else WS_LIVE)
        self.token = None
        self.token_expires_at = 0.0
        self.token_issued_wall = None
        self.connected_port = None      # 웹소켓 접속 성공 후 collector가 실제 원격 포트를 채운다
        self._session = None

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


class MockLSClient:
    """키 없이 backend/newsgap_tools/mock_ls_ws.py 에 붙기 위한 대역. REST 없음, 토큰 고정."""
    def __init__(self, ws_url="ws://127.0.0.1:8765/websocket"):
        self.paper = True
        self.ws_url = ws_url
        self.token = "MOCK"
        self.token_expires_at = time.time() + 10 * 365 * 86400
        self.connected_port = None

    async def get_token(self, force=False):
        return self.token

    async def close(self):
        pass
