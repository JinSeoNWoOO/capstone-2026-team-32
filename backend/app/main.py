from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.app.services.kis_account import get_balance
from backend.app.services.auto_trading import (
    AutoSettings,
    AutoTradingError,
    auto_trading_engine,
)
from backend.app.services.kis_market import get_current_price
from backend.app.services.news_collector import NewsCollectorError, news_collector
from backend.app.services.kis_order import (
    KisOrderError,
    KisOrderValidationError,
    get_order_mode,
    place_order,
)
from backend.app.services.advisor import AdvisorError, advisor_service


app = FastAPI(title="NA Trader API", version="0.1.0")


class OrderRequest(BaseModel):
    stock_code: str = Field(min_length=6, max_length=7)
    quantity: int = Field(gt=0, le=1_000_000)
    order_type: Literal["market", "limit"] = "market"
    price: int = Field(default=0, ge=0)


class AutoScanRequest(BaseModel):
    stock_codes: list[str] | None = None


class AutoStartRequest(BaseModel):
    quantity: int = Field(default=1, ge=1, le=1000)
    take_profit_pct: float = Field(default=0.7, ge=0.1, le=20)
    stop_loss_pct: float = Field(default=0.4, ge=0.1, le=20)
    max_hold_minutes: int = Field(default=10, ge=1, le=240)
    entry_momentum_pct: float = Field(default=0.15, ge=0.01, le=10)
    max_trades: int = Field(default=1, ge=1, le=20)
    poll_seconds: int = Field(default=5, ge=2, le=60)


class AdvisorRunRequest(BaseModel):
    """배치 수동 실행 (설계 10.1). as_of 를 생략하면 지금 시각이 판단 시각이 된다."""
    stage: Literal["prelim", "final"]
    as_of: str | None = None
    mode: Literal["live", "replay"] = "live"


STOCK_NAMES = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"message": "NA Trader API"}


@app.get("/trading/config")
def trading_config():
    try:
        mode = get_order_mode()
    except KisOrderError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "order_mode": mode,
        "paper_trading": mode == "paper",
        "real_trading": False,
    }


@app.get("/price/{stock_code}")
def price(stock_code: str):
    try:
        current_price = get_current_price(stock_code)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "stock_code": stock_code,
        "stock_name": STOCK_NAMES.get(stock_code, "등록되지 않은 종목"),
        "price": current_price,
    }


@app.get("/balance")
def balance():
    try:
        data = get_balance()
        summary = data["output2"][0]
    except (RuntimeError, KeyError, IndexError, TypeError) as exc:
        raise HTTPException(status_code=502, detail=f"잔고 조회 실패: {exc}") from exc

    return {
        "cash": int(summary["dnca_tot_amt"]),
        "total_asset": int(summary["tot_evlu_amt"]),
        "positions": data.get("output1", []),
    }


def _submit_order(side: Literal["buy", "sell"], request: OrderRequest):
    try:
        return place_order(
            side=side,
            stock_code=request.stock_code,
            quantity=request.quantity,
            order_type=request.order_type,
            price=request.price,
        )
    except KisOrderValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KisOrderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/buy")
def buy(request: OrderRequest):
    return _submit_order("buy", request)


@app.post("/sell")
def sell(request: OrderRequest):
    return _submit_order("sell", request)


@app.get("/auto/status")
def auto_status():
    return auto_trading_engine.status()


@app.post("/auto/scan")
def auto_scan(request: AutoScanRequest):
    try:
        return auto_trading_engine.scan(request.stock_codes)
    except AutoTradingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/auto/start")
def auto_start(request: AutoStartRequest):
    try:
        return auto_trading_engine.start(AutoSettings(**request.model_dump()))
    except (AutoTradingError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/auto/stop")
def auto_stop():
    return auto_trading_engine.stop()


# ---------------------------------------------------------------- 뉴스·공시 속보 수집기
# 판단 지원이 쓰는 data/newsgap.db 를 채우는 별도 프로세스. 여기서는 시작·중지·상태만 다룬다.
# 판단 지원 탭 상단의 수집기 카드 옆 버튼이 부른다 (상태는 /advisor/status 의 collector 에도 실린다).


@app.get("/news/status")
def news_status():
    return news_collector.status()


@app.post("/news/collector/start")
def news_collector_start(mock: bool = False):
    """mock=true 면 키 없이 backend/newsgap_tools/mock_ls_ws.py 에 붙는다 (시연·점검용)."""
    try:
        result = news_collector.start_collector(mock=mock)
        advisor_service.note_collector_manual_stop(False)
        return result
    except NewsCollectorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/news/collector/stop")
def news_collector_stop():
    advisor_service.note_collector_manual_stop(True)   # 사람이 껐으니 advisor 스케줄러가 되살리지 않는다
    return news_collector.stop_collector()


# ---------------------------------------------------------------- 판단 지원(advisor)
# 배치는 별도 프로세스다 (설계 2.1). 여기서는 수동 실행·상태만 다루고, 판단 기록은 배치가 쓴
# data/advisor.db 를 **읽기 전용**으로 읽는다. 아래 조회는 기록이 없어도 500 을 내지 않고
# {"available": false, ...} 로 물러선다 — 배치가 한 번도 안 돈 상태가 개발 중에는 정상이다.


@app.get("/advisor/status")
def advisor_status(mode: Literal["live", "replay"] = "live"):
    """마지막 실행, 다음 예정 시각, 스케줄러·폴러 상태, 오늘 LLM 비용."""
    return advisor_service.status(mode=mode)


@app.get("/advisor/report")
def advisor_report(
    date: str | None = None,
    stage: Literal["prelim", "final"] | None = None,
    mode: Literal["live", "replay"] = "live",
):
    """그날 판단 전체. date 를 생략하면 가장 최근 판단, stage 를 생략하면 그날의 마지막 단계."""
    return advisor_service.report(date=date, stage=stage, mode=mode)


@app.get("/advisor/scores")
def advisor_scores(
    date: str | None = None,
    layer: Literal["market", "sector", "stock"] | None = None,
    stage: Literal["prelim", "final"] | None = None,
    mode: Literal["live", "replay"] = "live",
):
    """계층별 점수표 (요인별 원본 값·점수·결측). layer 를 생략하면 세 계층 모두."""
    return advisor_service.scores(date=date, layer=layer, stage=stage, mode=mode)


@app.get("/advisor/asset/{code}")
def advisor_asset(
    code: str,
    date: str | None = None,
    stage: Literal["prelim", "final"] | None = None,
    mode: Literal["live", "replay"] = "live",
):
    """한 자산의 점수 분해, 위험 표시, 근거, LLM 조정 내용 (채택·기각 원인)."""
    return advisor_service.asset(code, date=date, stage=stage, mode=mode)


@app.get("/advisor/performance")
def advisor_performance(mode: Literal["live", "replay"] = "live"):
    """포트폴리오별 NAV 시계열과 요약 지표. 재현 모드는 실시간과 섞지 않는다 (설계 7.4)."""
    return advisor_service.performance(mode=mode)


@app.get("/advisor/metrics")
def advisor_metrics(mode: Literal["live", "replay"] = "live"):
    """요인 지표 (순위 상관·적중률·유효 표본). n_eff 가 작으면 화면은 '판단 불가'로 둔다."""
    return advisor_service.metrics(mode=mode)


@app.post("/advisor/run")
def advisor_run(request: AdvisorRunRequest):
    """배치 수동 실행. 이미 돌고 있으면 422 로 거절한다."""
    try:
        return advisor_service.run_batch(request.stage, as_of=request.as_of, mode=request.mode)
    except AdvisorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# 스케줄러·폴러 스레드는 ADVISOR_SCHEDULER=1 일 때만 뜬다 (기본 꺼짐).
# 그래서 테스트나 평소 개발용 실행이 18:30·07:40 을 지나도 배치가 저절로 뜨지 않는다.
advisor_service.start_if_enabled()
