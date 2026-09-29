import math
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, time as clock_time, timedelta, timezone

from backend.app.services.kis_account import get_position
from backend.app.services.kis_fill import get_order_fill
from backend.app.services.kis_market import get_stock_snapshot, get_volume_rank
from backend.app.services.kis_order import place_order
from backend.app.services.kis_read import KisReadError


SEOUL = timezone(timedelta(hours=9), name="Asia/Seoul")
DEFAULT_WATCHLIST = ["005930", "000660", "035420", "035720", "051910", "006400"]
MAX_CONSECUTIVE_READ_FAILURES = 5
MIN_CANDIDATE_PRICE = 5_000
MIN_CANDIDATE_CHANGE_RATE = -10.0
MAX_CANDIDATE_CHANGE_RATE = 15.0
STOCK_NAMES = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "035420": "NAVER",
    "035720": "카카오",
    "051910": "LG화학",
    "006400": "삼성SDI",
}


class AutoTradingError(RuntimeError):
    pass


@dataclass(frozen=True)
class AutoSettings:
    quantity: int = 1
    take_profit_pct: float = 0.7
    stop_loss_pct: float = 0.4
    max_hold_minutes: int = 10
    entry_momentum_pct: float = 0.15
    max_trades: int = 1
    poll_seconds: int = 5

    def validate(self) -> None:
        if not 1 <= self.quantity <= 1000:
            raise AutoTradingError("주문 수량은 1~1000주여야 합니다.")
        if not 0.1 <= self.take_profit_pct <= 20:
            raise AutoTradingError("익절률은 0.1~20%여야 합니다.")
        if not 0.1 <= self.stop_loss_pct <= 20:
            raise AutoTradingError("손절률은 0.1~20%여야 합니다.")
        if not 1 <= self.max_hold_minutes <= 240:
            raise AutoTradingError("최대 보유시간은 1~240분이어야 합니다.")
        if not 0.01 <= self.entry_momentum_pct <= 10:
            raise AutoTradingError("진입 모멘텀은 0.01~10%여야 합니다.")
        if not 1 <= self.max_trades <= 20:
            raise AutoTradingError("최대 거래 횟수는 1~20회여야 합니다.")
        if not 2 <= self.poll_seconds <= 60:
            raise AutoTradingError("조회 주기는 2~60초여야 합니다.")


def entry_signal(prices: list[int], threshold_pct: float) -> bool:
    if len(prices) < 5 or min(prices[-5:]) <= 0:
        return False
    recent = prices[-5:]
    momentum = (recent[-1] / recent[0] - 1) * 100
    return momentum >= threshold_pct and recent[-1] > sum(recent[:-1]) / 4


def exit_reason(
    current_price: int,
    entry_price: float,
    held_seconds: float,
    settings: AutoSettings,
) -> str | None:
    if entry_price <= 0:
        return None
    profit_pct = (current_price / entry_price - 1) * 100
    if profit_pct + 1e-9 >= settings.take_profit_pct:
        return "익절"
    if profit_pct - 1e-9 <= -settings.stop_loss_pct:
        return "손절"
    if held_seconds >= settings.max_hold_minutes * 60:
        return "최대 보유시간"
    return None


def _normalize(values: list[float]) -> list[float]:
    low, high = min(values), max(values)
    if high == low:
        return [0.5 for _ in values]
    return [(value - low) / (high - low) for value in values]


def candidate_rejection_reason(candidate: dict) -> str | None:
    price = int(candidate.get("price") or 0)
    change_rate = float(candidate.get("change_rate") or 0)
    upper_limit = int(candidate.get("upper_limit") or 0)
    lower_limit = int(candidate.get("lower_limit") or 0)

    if candidate.get("halted"):
        return "거래정지 종목"
    if price < MIN_CANDIDATE_PRICE:
        return f"{MIN_CANDIDATE_PRICE:,}원 미만 저가주"
    if upper_limit > 0 and price >= upper_limit:
        return "현재가가 상한가와 같음"
    if lower_limit > 0 and price <= lower_limit:
        return "현재가가 하한가와 같음"
    if change_rate >= MAX_CANDIDATE_CHANGE_RATE:
        return f"등락률 +{MAX_CANDIDATE_CHANGE_RATE:g}% 이상"
    if change_rate <= MIN_CANDIDATE_CHANGE_RATE:
        return f"등락률 {MIN_CANDIDATE_CHANGE_RATE:g}% 이하"
    return None


class AutoTradingEngine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._status = "idle"
        self._phase = "대기"
        self._selected: dict | None = None
        self._candidates: list[dict] = []
        self._settings = AutoSettings()
        self._prices: deque[int] = deque(maxlen=20)
        self._baseline_quantity = 0
        self._managed_quantity = 0
        self._entry_price: float | None = None
        self._entry_monotonic: float | None = None
        self._pending_since: float | None = None
        self._latest_price: int | None = None
        self._trades_completed = 0
        self._last_order: dict | None = None
        self._stop_requested = False
        self._logs: deque[dict] = deque(maxlen=100)

    def _log(self, message: str, level: str = "info") -> None:
        with self._lock:
            self._logs.appendleft(
                {
                    "time": datetime.now(SEOUL).strftime("%H:%M:%S"),
                    "level": level,
                    "message": message,
                }
            )

    def status(self) -> dict:
        with self._lock:
            pnl_pct = None
            if self._entry_price and self._latest_price:
                pnl_pct = round((self._latest_price / self._entry_price - 1) * 100, 3)
            return {
                "status": self._status,
                "phase": self._phase,
                "selected": dict(self._selected) if self._selected else None,
                "candidates": [dict(item) for item in self._candidates],
                "settings": asdict(self._settings),
                "latest_price": self._latest_price,
                "entry_price": self._entry_price,
                "pnl_pct": pnl_pct,
                "baseline_quantity": self._baseline_quantity,
                "managed_quantity": self._managed_quantity,
                "trades_completed": self._trades_completed,
                "last_order": dict(self._last_order) if self._last_order else None,
                "logs": list(self._logs),
                "running": self._status in {"running", "stopping"},
            }

    def scan(self, stock_codes: list[str] | None = None) -> dict:
        with self._lock:
            if self._status in {"running", "stopping"}:
                raise AutoTradingError("자동매매 실행 중에는 종목을 다시 스캔할 수 없습니다.")
            self._status = "scanning"
            self._phase = "후보 데이터 수집"

        snapshots = []
        if stock_codes:
            codes = list(dict.fromkeys(code.strip() for code in stock_codes if code.strip()))
            if not codes or any(not code.isdigit() or len(code) != 6 for code in codes):
                with self._lock:
                    self._status = "error"
                    self._phase = "입력 오류"
                raise AutoTradingError("후보 종목코드는 숫자 6자리로 입력해야 합니다.")
            if len(codes) > 20:
                with self._lock:
                    self._status = "error"
                    self._phase = "입력 오류"
                raise AutoTradingError("한 번에 최대 20개 종목까지 스캔할 수 있습니다.")
            snapshots = self._scan_custom_watchlist(codes)
        else:
            self._log("KIS 거래대금 상위 보통주를 자동 수집합니다.")
            try:
                snapshots = get_volume_rank(20)
            except RuntimeError as exc:
                self._log(f"거래량 순위 조회 실패: {exc}. 기본 관심종목으로 재시도합니다.", "warning")
                snapshots = self._scan_custom_watchlist(self._watchlist_from_env())

        if not snapshots:
            with self._lock:
                self._status = "error"
                self._phase = "스캔 실패"
            raise AutoTradingError("사용 가능한 후보 종목 데이터를 가져오지 못했습니다.")

        eligible_snapshots = []
        rejection_counts: dict[str, int] = {}
        for snapshot in snapshots:
            reason = candidate_rejection_reason(snapshot)
            if reason:
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            else:
                eligible_snapshots.append(snapshot)

        if rejection_counts:
            summary = ", ".join(
                f"{reason} {count}개" for reason, count in rejection_counts.items()
            )
            self._log(f"후보 필터로 {len(snapshots) - len(eligible_snapshots)}개 제외: {summary}")

        if not eligible_snapshots:
            with self._lock:
                self._status = "error"
                self._phase = "후보 없음"
            raise AutoTradingError("가격·등락률·거래 가능 조건을 통과한 후보가 없습니다.")

        snapshots = eligible_snapshots
        liquidity = _normalize([math.log1p(item["trading_value"]) for item in snapshots])
        volume_surge = _normalize([math.log1p(max(item["volume_ratio"], 0)) for item in snapshots])
        volatility_values = [
            ((item["high"] - item["low"]) / item["open"] * 100) if item["open"] else 0
            for item in snapshots
        ]
        momentum = _normalize([max(item["change_rate"], 0) for item in snapshots])

        candidates = []
        for index, item in enumerate(snapshots):
            score = (
                liquidity[index] * 0.65
                + volume_surge[index] * 0.20
                + momentum[index] * 0.15
            ) * 100
            candidates.append(
                {
                    **item,
                    "intraday_volatility_pct": round(volatility_values[index], 3),
                    "score": round(score, 2),
                }
            )
        candidates.sort(key=lambda item: item["score"], reverse=True)

        selected = None
        rejected_codes = set()
        for candidate in candidates:
            try:
                latest = get_stock_snapshot(candidate["stock_code"])
            except RuntimeError as exc:
                rejected_codes.add(candidate["stock_code"])
                self._log(
                    f"{candidate['stock_name']} 최종 거래 가능 여부 조회 실패: {exc}",
                    "warning",
                )
                continue

            verified = {**candidate, **latest, "stock_name": candidate["stock_name"]}
            reason = candidate_rejection_reason(verified)
            if reason:
                rejected_codes.add(candidate["stock_code"])
                self._log(f"{candidate['stock_name']} 최종 후보 제외: {reason}", "warning")
                continue
            selected = verified
            break

        if not selected:
            with self._lock:
                self._status = "error"
                self._phase = "후보 없음"
            raise AutoTradingError("최종 거래 가능 여부를 통과한 후보가 없습니다.")

        candidates = [
            selected,
            *[
                item
                for item in candidates
                if item["stock_code"] != selected["stock_code"]
                and item["stock_code"] not in rejected_codes
            ],
        ]

        with self._lock:
            self._candidates = candidates
            self._selected = dict(selected)
            self._latest_price = selected["price"]
            self._status = "ready"
            self._phase = "시작 대기"
        self._log(
            f"{selected['stock_name']}({selected['stock_code']})을 자동매매 종목으로 선택했습니다."
        )
        return self.status()

    def _scan_custom_watchlist(self, codes: list[str]) -> list[dict]:
        self._log(f"{len(codes)}개 후보 종목의 데이터를 수집합니다.")
        snapshots = []
        for index, code in enumerate(codes):
            try:
                snapshot = get_stock_snapshot(code)
                snapshot["stock_name"] = STOCK_NAMES.get(code, code)
                if snapshot["price"] > 0 and not snapshot["halted"]:
                    snapshots.append(snapshot)
            except RuntimeError as exc:
                self._log(f"{code} 조회 실패: {exc}", "warning")
            if index < len(codes) - 1:
                time.sleep(0.55)
        return snapshots

    def start(self, settings: AutoSettings) -> dict:
        settings.validate()
        with self._lock:
            if self._status in {"running", "stopping"}:
                raise AutoTradingError("자동매매가 이미 실행 중입니다.")
            if not self._selected:
                raise AutoTradingError("먼저 후보 종목을 스캔해 주세요.")
        if not self._market_is_open():
            raise AutoTradingError("자동 주문은 평일 09:00~15:20에 시작할 수 있습니다.")

        position = get_position(self._selected["stock_code"])
        with self._lock:
            self._settings = settings
            self._baseline_quantity = position["quantity"]
            self._managed_quantity = 0
            self._entry_price = None
            self._entry_monotonic = None
            self._pending_since = None
            self._prices.clear()
            self._trades_completed = 0
            self._last_order = None
            self._stop_requested = False
            self._stop_event.clear()
            self._status = "running"
            self._phase = "진입 신호 관찰"
            self._worker = threading.Thread(target=self._run, daemon=True, name="auto-trader")
            self._worker.start()
        self._log(f"자동매매를 시작했습니다. 기존 보유량 {position['quantity']}주는 보호합니다.")
        return self.status()

    def stop(self) -> dict:
        with self._lock:
            if self._status not in {"running", "stopping"}:
                self._status = "stopped"
                self._phase = "사용자 중지"
                return self.status()
            self._stop_requested = True
            self._status = "stopping"
            if self._managed_quantity == 0 and self._phase == "진입 신호 관찰":
                self._stop_event.set()
        self._log("중지 요청을 받았습니다. 관리 중인 수량이 있으면 먼저 매도합니다.")
        return self.status()

    def _run(self) -> None:
        consecutive_read_failures = 0
        try:
            while not self._stop_event.is_set():
                try:
                    self._run_cycle()
                    consecutive_read_failures = 0
                except KisReadError as exc:
                    consecutive_read_failures += 1
                    self._log(
                        "KIS 조회 일시 실패 "
                        f"({consecutive_read_failures}/{MAX_CONSECUTIVE_READ_FAILURES}): "
                        f"{exc}. 자동매매를 계속합니다.",
                        "warning",
                    )
                    if consecutive_read_failures >= MAX_CONSECUTIVE_READ_FAILURES:
                        raise AutoTradingError(
                            "KIS 조회가 연속으로 실패해 자동매매를 중지합니다."
                        ) from exc

                self._stop_event.wait(self._settings.poll_seconds)
        except Exception as exc:
            with self._lock:
                self._status = "error"
                self._phase = "오류"
            self._log(str(exc), "error")

    def _run_cycle(self) -> None:
        selected = self._selected
        if not selected:
            raise AutoTradingError("선택된 종목이 없습니다.")
        snapshot = get_stock_snapshot(selected["stock_code"])
        price = snapshot["price"]
        with self._lock:
            self._latest_price = price
            self._prices.append(price)
            phase = self._phase

        if phase == "진입 신호 관찰":
            if self._stop_requested:
                self._finish("사용자 중지")
            elif entry_signal(list(self._prices), self._settings.entry_momentum_pct):
                self._submit_buy(price)
        elif phase == "매수 체결 확인":
            self._confirm_buy(price)
        elif phase == "포지션 관리":
            reason = "사용자 중지" if self._stop_requested else exit_reason(
                price,
                self._entry_price or 0,
                time.monotonic() - (self._entry_monotonic or time.monotonic()),
                self._settings,
            )
            if reason:
                self._submit_sell(reason)
        elif phase == "매도 체결 확인":
            self._confirm_sell()

    def _submit_buy(self, price: int) -> None:
        result = place_order("buy", self._selected["stock_code"], self._settings.quantity)
        with self._lock:
            self._last_order = result
        if not result["submitted"]:
            with self._lock:
                self._managed_quantity = self._settings.quantity
                self._entry_price = float(price)
                self._entry_monotonic = time.monotonic()
                self._phase = "포지션 관리"
            self._log(f"Dry-run 매수: {price:,}원, {self._settings.quantity}주")
            return
        with self._lock:
            self._pending_since = time.monotonic()
            self._phase = "매수 체결 확인"
        self._log(f"매수 주문 접수: 주문번호 {result.get('order_no') or '-'}")

    def _confirm_buy(self, price: int) -> None:
        """실제 체결 평균가로 진입가를 잡는다.

        예전에는 잔고에서 수량이 는 것을 보고 '그 순간의 현재가'를 진입가로 삼았는데,
        체결은 조회 주기만큼 전에 일어났고 시장가 매수는 최우선 매도호가에 체결되므로
        현재가와 다르다. 익절·손절·손익이 모두 진입가에서 나오니 그대로 판단 오차가 된다.
        체결 조회가 안 되면(모의투자 응답 지연 등) 예전 방식으로 물러난다."""
        order_no = (self._last_order or {}).get("order_no")
        filled_qty, avg_price, source = 0, 0.0, "잔고"
        if order_no:
            try:
                fill = get_order_fill(order_no, self._selected["stock_code"])
                if fill["filled_qty"] > 0:
                    filled_qty, avg_price, source = fill["filled_qty"], fill["avg_price"], "체결조회"
                    if fill["partial"]:
                        self._log(f"부분 체결: {filled_qty}/{fill['order_qty']}주", "warning")
            except KisReadError as exc:
                self._log(f"체결 조회 실패, 잔고로 확인합니다: {exc}", "warning")

        if filled_qty == 0:
            position = get_position(self._selected["stock_code"])
            filled_qty = max(position["quantity"] - self._baseline_quantity, 0)
            avg_price = float(price)

        if filled_qty > 0:
            with self._lock:
                self._managed_quantity = filled_qty
                self._entry_price = float(avg_price)
                self._entry_monotonic = time.monotonic()
                self._phase = "포지션 관리"
            self._log(f"매수 체결 확인({source}): {filled_qty}주, 진입가 {avg_price:,.0f}원")
        elif time.monotonic() - (self._pending_since or time.monotonic()) > 60:
            raise AutoTradingError("매수 주문 체결을 60초 안에 확인하지 못했습니다.")

    def _submit_sell(self, reason: str) -> None:
        if self._managed_quantity <= 0:
            self._finish(reason)
            return
        result = place_order("sell", self._selected["stock_code"], self._managed_quantity)
        with self._lock:
            self._last_order = result
        if not result["submitted"]:
            self._complete_trade(reason)
            return
        with self._lock:
            self._pending_since = time.monotonic()
            self._phase = "매도 체결 확인"
        self._log(f"{reason} 매도 주문 접수: {self._managed_quantity}주")

    def _confirm_sell(self) -> None:
        position = get_position(self._selected["stock_code"])
        remaining = max(position["quantity"] - self._baseline_quantity, 0)
        with self._lock:
            self._managed_quantity = remaining
        if remaining == 0:
            self._complete_trade("매도 체결")
        elif time.monotonic() - (self._pending_since or time.monotonic()) > 60:
            raise AutoTradingError("매도 주문 체결을 60초 안에 확인하지 못했습니다.")

    def _complete_trade(self, reason: str) -> None:
        with self._lock:
            self._trades_completed += 1
            self._managed_quantity = 0
            self._entry_price = None
            self._entry_monotonic = None
            should_stop = self._stop_requested or self._trades_completed >= self._settings.max_trades
            self._prices.clear()
            self._phase = "진입 신호 관찰"
        self._log(f"거래 1회를 종료했습니다: {reason}")
        if should_stop:
            self._finish("거래 한도 도달" if not self._stop_requested else "사용자 중지")

    def _finish(self, reason: str) -> None:
        with self._lock:
            self._status = "stopped"
            self._phase = reason
            self._stop_event.set()
        self._log(f"자동매매 종료: {reason}")

    @staticmethod
    def _market_is_open() -> bool:
        now = datetime.now(SEOUL)
        return now.weekday() < 5 and clock_time(9, 0) <= now.time() <= clock_time(15, 20)

    @staticmethod
    def _watchlist_from_env() -> list[str]:
        raw = os.getenv("AUTO_WATCHLIST", "")
        return [item.strip() for item in raw.split(",") if item.strip()] or DEFAULT_WATCHLIST


auto_trading_engine = AutoTradingEngine()
