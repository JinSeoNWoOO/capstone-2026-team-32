import argparse, asyncio, json, logging, os, signal, sys, yaml
from .store import Store
from .pipeline import Pipeline
from . import analyzer

log = logging.getLogger("newsgap.main")

def replay_db(cfg):
    """replay 전용 DB. 실수집 DB(sqlite_path)와 분리해서 replay가 수집 데이터를 지우지 않게 한다."""
    return cfg["storage"].get("replay_sqlite_path", "./data/replay.db")

def run_replay(cfg, stream_path):
    db = replay_db(cfg)
    store = Store(db)
    cfg["signal"]["require_prespike"] = False              # replay 에는 REST(t1301)가 없어 수신 전 급증을 잴 수 없다
    cfg["ai"] = dict(cfg.get("ai") or {}, enabled=False)    # 판정할 제목도 판정기도 없다 → AI 게이트 끔 (규칙만)
    # replay 기준선: 시나리오 base 가격 × 가정 거래량. 실연결에서는 collector가 t8412로 채운다.
    baseline = lambda code: 2_000_000.0
    pipe = Pipeline(store, cfg, baseline_lookup=baseline)
    n = 0
    with open(stream_path, encoding="utf-8") as f:
        for line in f:
            m = json.loads(line)
            pipe.on_message(m["msg"], now_mono=m["t"])
            pipe.tick_clock(m["t"])
            n += 1
    pipe.tick_clock(1e12)
    store.commit()
    print(f"replayed {n} messages -> {db}")
    rows = analyzer.run(db, "data/report.csv")
    for r in rows:
        print({k: r[k] for k in ("event_id", "code", "r_5s_pct", "r_60s_pct", "r_300s_pct", "exit_reason", "pnl_after_cost")})
    print("report -> data/report.csv")


def build_data_client(cfg):
    """뉴스·체결 수신용 클라이언트. ls.data_key 가 paper면 모의 서버, live면 실전 서버(읽기 전용)."""
    from .ls_client import LSClient, credentials
    ls = cfg["ls"]
    which = ls.get("data_key", "paper")
    if which not in ("paper", "live"):
        raise SystemExit(f"ls.data_key={which}: paper 또는 live")
    key, sec = credentials(which, ls, ls.get("env_file", ".env"))
    return LSClient(key, sec, paper=(which == "paper"), rest_base=ls["rest_base"],
                    ws_url=ls["ws_url_paper" if which == "paper" else "ws_url_live"], role="data")


async def load_market_map(ls):
    """t8430으로 종목→시장 구분(1 KOSPI, 2 KOSDAQ). 실패하면 빈 dict → S3_/K3_ 둘 다 등록."""
    m = {}
    for g in ("1", "2"):
        try:
            d = await ls.t8430(g)
            for r in (d or {}).get("t8430OutBlock") or []:
                m[r["shcode"]] = g
        except Exception as ex:
            log.warning("t8430 gubun=%s failed: %s", g, ex)
    log.info("market map: %d codes", len(m))
    return m


def build_judge(cfg):
    """AI 제목 판정기. ai.enabled 가 아니면 None(판정 없음).
    키가 없어 AIJudge 생성이 실패하면 NullJudge(항상 BUY)로 떨어뜨려 수집만은 계속한다."""
    ai = cfg.get("ai") or {}
    if not ai.get("enabled"):
        return None
    try:
        from .ai_judge import AIJudge
        return AIJudge(ai)
    except Exception as ex:                       # 키 없음(RuntimeError)·모듈/의존성 문제
        log.warning("!!! AI 판정기를 만들지 못했습니다 (%s: %s) → NullJudge 로 수집을 계속합니다 "
                    "(모든 뉴스 BUY 라벨, 실제 진입은 규칙만으로 결정)", type(ex).__name__, ex)
        try:
            from .ai_judge import NullJudge
            return NullJudge(ai)
        except Exception:
            return None


async def run_live(cfg, mock=False):
    from .collector import Collector
    from .ls_client import MockLSClient
    if cfg["env"] not in ("shadow", "paper"):
        raise SystemExit("env는 shadow 또는 paper 여야 합니다")
    if not mock:
        try:
            import aiohttp  # noqa: F401  (실접속 REST/토큰에 필요. 없으면 재접속 루프 대신 여기서 멈춘다)
        except ImportError:
            raise SystemExit(f"aiohttp 가 없습니다 (python={sys.executable}). 실행: {sys.executable} -m pip install -r requirements.txt")
    store = Store(cfg["storage"]["sqlite_path"])      # 실연결 DB는 지우지 않고 이어 쓴다
    if mock:
        cfg.setdefault("collector", {})["mock"] = True
        cfg["signal"]["require_prespike"] = False   # 목업에도 REST가 없다
        ls = MockLSClient(cfg["collector"].get("mock_ws_url", "ws://127.0.0.1:8765/websocket"))
        baseline = lambda code: 2_000_000.0
        market_map = {}
    else:
        ls = build_data_client(cfg)
        baseline = lambda code: None                   # collector._enrich 가 t8412로 채운다
        market_map = await load_market_map(ls)
    # ls_client=None: 데이터 키는 주문 경로(Executor)에 절대 들어가지 않는다. paper 주문 클라이언트는 별도 구현.
    pipe = Pipeline(store, cfg, ls_client=None, baseline_lookup=baseline)
    col = Collector(cfg, store, pipe, ls, market_map, judge=build_judge(cfg))
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
    ap.add_argument("--mode", choices=["replay", "live"], default="replay")
    ap.add_argument("--stream", default="replay/stream.jsonl")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--mock", action="store_true", help="live 모드를 tools/mock_ls_ws.py 에 붙인다 (키 불필요)")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if a.mode == "replay":
        if os.path.exists(replay_db(cfg)):
            os.remove(replay_db(cfg))
        run_replay(cfg, a.stream)
    else:
        asyncio.run(run_live(cfg, mock=a.mock or bool((cfg.get("collector") or {}).get("mock"))))
