"""LS 실시간 뉴스·공시 속보 수집기 진입점. 백엔드(services/news_collector.py)가 별도 프로세스로 띄운다.

사용: python -m backend.newsgap.main --config backend/newsgap.config.yaml [--mock]
--mode 는 예전 실행 명령(--mode live)이 그대로 돌게 받아만 둔다. 뉴스 자동매매용 replay 모드는 없앴다.
"""
import argparse, asyncio, logging, signal, sys, yaml
from .store import Store

log = logging.getLogger("newsgap.main")


def build_data_client(cfg):
    """뉴스 수신용 클라이언트. ls.data_key 가 paper면 모의 서버, live면 실전 서버(읽기 전용)."""
    from .ls_client import LSClient, credentials
    ls = cfg["ls"]
    which = ls.get("data_key", "paper")
    if which not in ("paper", "live"):
        raise SystemExit(f"ls.data_key={which}: paper 또는 live")
    key, sec = credentials(which, ls, ls.get("env_file", ".env"))
    return LSClient(key, sec, paper=(which == "paper"), rest_base=ls["rest_base"],
                    ws_url=ls["ws_url_paper" if which == "paper" else "ws_url_live"])


async def run_live(cfg, mock=False):
    from .collector import Collector
    from .ls_client import MockLSClient
    if not mock:
        try:
            import aiohttp  # noqa: F401  (토큰 발급에 필요. 없으면 재접속 루프 대신 여기서 멈춘다)
        except ImportError:
            raise SystemExit(f"aiohttp 가 없습니다 (python={sys.executable}). 실행: {sys.executable} -m pip install -r requirements.txt")
    store = Store(cfg["storage"]["sqlite_path"])      # 실연결 DB는 지우지 않고 이어 쓴다
    if mock:
        cfg.setdefault("collector", {})["mock"] = True
        ls = MockLSClient(cfg["collector"].get("mock_ws_url", "ws://127.0.0.1:8765/websocket"))
    else:
        ls = build_data_client(cfg)
    col = Collector(cfg, store, ls)
    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, col.request_stop)
        except NotImplementedError:
            pass
    try:
        await col.run()
    finally:
        store.commit()
        await ls.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["live"], default="live")
    ap.add_argument("--config", default="backend/newsgap.config.yaml")
    ap.add_argument("--mock", action="store_true",
                    help="backend/newsgap_tools/mock_ls_ws.py 에 붙는다 (키 불필요)")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run_live(cfg, mock=a.mock or bool((cfg.get("collector") or {}).get("mock"))))
