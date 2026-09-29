import { useEffect, useMemo, useState } from "react";
import "./App.css";
import AutoTrading from "./AutoTrading.jsx";
import Advisor from "./Advisor.jsx";


const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";


function formatWon(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return `${Number(value).toLocaleString("ko-KR")}원`;
}


async function requestJson(path, options) {
  const response = await fetch(`${API_BASE_URL}${path}`, options);
  const data = await response.json().catch(() => ({}));

  if (!response.ok) {
    throw new Error(data.detail ?? `요청 실패 (${response.status})`);
  }
  return data;
}


function App() {
  const [activeView, setActiveView] = useState("manual");
  const [stockCode, setStockCode] = useState("005930");
  const [stock, setStock] = useState(null);
  const [balance, setBalance] = useState(null);
  const [quantity, setQuantity] = useState(1);
  const [orderType, setOrderType] = useState("market");
  const [limitPrice, setLimitPrice] = useState(0);
  const [config, setConfig] = useState(null);
  const [notice, setNotice] = useState(null);
  const [stockLoading, setStockLoading] = useState(false);
  const [orderLoading, setOrderLoading] = useState(false);

  const estimatedAmount = useMemo(() => {
    const unitPrice = orderType === "limit" ? limitPrice : stock?.price;
    return unitPrice && quantity > 0 ? unitPrice * quantity : 0;
  }, [limitPrice, orderType, quantity, stock]);

  async function loadStock(code = stockCode) {
    const normalizedCode = code.trim();
    if (!/^\d{6,7}$/.test(normalizedCode)) {
      setNotice({ type: "error", text: "종목코드는 숫자 6자리(ETN은 7자리)로 입력해 주세요." });
      return;
    }

    setStockLoading(true);
    try {
      const data = await requestJson(`/price/${normalizedCode}`);
      setStock(data);
      setStockCode(normalizedCode);
      if (orderType === "limit") {
        setLimitPrice(data.price);
      }
      setNotice(null);
    } catch (error) {
      setNotice({ type: "error", text: error.message });
    } finally {
      setStockLoading(false);
    }
  }

  async function loadBalance() {
    try {
      setBalance(await requestJson("/balance"));
    } catch (error) {
      setNotice({ type: "error", text: error.message });
    }
  }

  useEffect(() => {
    let cancelled = false;

    requestJson("/trading/config")
      .then((data) => !cancelled && setConfig(data))
      .catch((error) => !cancelled && setNotice({ type: "error", text: error.message }));

    requestJson("/price/005930")
      .then((data) => !cancelled && setStock(data))
      .catch((error) => !cancelled && setNotice({ type: "error", text: error.message }));

    // KIS REST 호출이 너무 촘촘해지는 것을 피한다.
    const balanceTimer = window.setTimeout(() => {
      requestJson("/balance")
        .then((data) => !cancelled && setBalance(data))
        .catch((error) => !cancelled && setNotice({ type: "error", text: error.message }));
    }, 500);

    return () => {
      cancelled = true;
      window.clearTimeout(balanceTimer);
    };
  }, []);

  function changeOrderType(nextType) {
    setOrderType(nextType);
    if (nextType === "limit" && stock?.price) {
      setLimitPrice(stock.price);
    }
  }

  async function submitOrder(side) {
    const action = side === "buy" ? "매수" : "매도";
    const price = orderType === "limit" ? Number(limitPrice) : 0;

    if (!stock || stock.stock_code !== stockCode.trim()) {
      setNotice({ type: "error", text: "먼저 종목을 조회해 주세요." });
      return;
    }
    if (!Number.isInteger(quantity) || quantity <= 0) {
      setNotice({ type: "error", text: "주문 수량은 1 이상의 정수여야 합니다." });
      return;
    }
    if (orderType === "limit" && (!Number.isInteger(price) || price <= 0)) {
      setNotice({ type: "error", text: "지정가를 1원 이상의 정수로 입력해 주세요." });
      return;
    }

    if (config?.paper_trading) {
      const priceText = orderType === "market" ? "시장가" : formatWon(price);
      const confirmed = window.confirm(
        `${stock.stock_name}(${stock.stock_code}) ${quantity}주를 ${priceText}로 ${action} 주문할까요?\nKIS 모의투자 계좌에 주문이 접수됩니다.`,
      );
      if (!confirmed) return;
    }

    setOrderLoading(true);
    setNotice(null);
    try {
      const result = await requestJson(`/${side}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          stock_code: stock.stock_code,
          quantity,
          order_type: orderType,
          price,
        }),
      });

      const orderNumber = result.order_no ? ` 주문번호: ${result.order_no}` : "";
      setNotice({
        type: result.submitted ? "success" : "info",
        text: `${action} ${result.message}${orderNumber}`,
      });
      if (result.submitted) {
        await loadBalance();
      }
    } catch (error) {
      setNotice({ type: "error", text: `${action} 실패: ${error.message}` });
    } finally {
      setOrderLoading(false);
    }
  }

  const modeLabel = config?.paper_trading ? "KIS 모의투자" : "Dry-run";

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">NA</span>
          <div>
            <strong>NA Trader</strong>
            <small>AI 투자 판단 지원</small>
          </div>
        </div>

        <nav aria-label="매매 모드">
          <button className={`nav-item ${activeView === "manual" ? "active" : ""}`} type="button" onClick={() => setActiveView("manual")}>
            <span>직접 주문</span>
            <small>수동 매매</small>
          </button>
          <button className={`nav-item ${activeView === "auto" ? "active" : ""}`} type="button" onClick={() => setActiveView("auto")}>
            <span>자동 매매</span>
            <small>종목 선정·전략 실행</small>
          </button>
          <button className={`nav-item ${activeView === "advisor" ? "active" : ""}`} type="button" onClick={() => setActiveView("advisor")}>
            <span>판단 지원</span>
            <small>비중 판단·근거 리포트</small>
          </button>
        </nav>

        <div className={`mode-card ${config?.paper_trading ? "paper" : "dry"}`}>
          <span className="status-dot" />
          <div>
            <small>현재 주문 모드</small>
            <strong>{config ? modeLabel : "확인 중"}</strong>
          </div>
        </div>
      </aside>

      <main className="dashboard">
        {activeView === "manual" ? (
          <>
        <header className="page-header">
          <div>
            <p className="eyebrow">MANUAL TRADING</p>
            <h1>수동 매매</h1>
            <p>종목을 조회하고 KIS 모의투자 주문 흐름을 검증합니다.</p>
          </div>
          <button className="secondary-button" type="button" onClick={loadBalance}>
            잔고 새로고침
          </button>
        </header>

        {notice && (
          <div className={`notice ${notice.type}`} role="status">
            {notice.text}
          </div>
        )}

        <section className="summary-grid" aria-label="계좌 요약">
          <article className="summary-card">
            <span>주문 가능 현금</span>
            <strong>{balance ? formatWon(balance.cash) : "조회 중..."}</strong>
          </article>
          <article className="summary-card">
            <span>총 평가금액</span>
            <strong>{balance ? formatWon(balance.total_asset) : "조회 중..."}</strong>
          </article>
          <article className="summary-card accent">
            <span>외부 주문 전송</span>
            <strong>{config?.paper_trading ? "활성화" : "차단됨"}</strong>
          </article>
        </section>

        <div className="content-grid">
          <section className="panel order-panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">ORDER</p>
                <h2>주문 입력</h2>
              </div>
              {stock && <span className="stock-name">{stock.stock_name}</span>}
            </div>

            <label>
              종목코드
              <div className="inline-field">
                <input
                  inputMode="numeric"
                  maxLength={7}
                  value={stockCode}
                  onChange={(event) => setStockCode(event.target.value.replace(/\D/g, ""))}
                  onKeyDown={(event) => event.key === "Enter" && loadStock()}
                  placeholder="예: 005930"
                />
                <button type="button" onClick={() => loadStock()} disabled={stockLoading}>
                  {stockLoading ? "조회 중" : "조회"}
                </button>
              </div>
            </label>

            <div className="quote-card">
              <span>{stock?.stock_code ?? "종목을 조회해 주세요"}</span>
              <strong>{stock ? formatWon(stock.price) : "-"}</strong>
              <small>현재가</small>
            </div>

            <div className="field-row">
              <label>
                주문 방식
                <select value={orderType} onChange={(event) => changeOrderType(event.target.value)}>
                  <option value="market">시장가</option>
                  <option value="limit">지정가</option>
                </select>
              </label>
              <label>
                수량
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={quantity}
                  onChange={(event) => setQuantity(Number(event.target.value))}
                />
              </label>
            </div>

            {orderType === "limit" && (
              <label>
                지정가
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={limitPrice}
                  onChange={(event) => setLimitPrice(Number(event.target.value))}
                />
              </label>
            )}

            <div className="estimate">
              <span>예상 주문금액</span>
              <strong>{estimatedAmount ? formatWon(estimatedAmount) : "-"}</strong>
            </div>

            <div className="order-actions">
              <button
                className="buy-button"
                type="button"
                disabled={orderLoading}
                onClick={() => submitOrder("buy")}
              >
                {orderLoading ? "처리 중" : "매수"}
              </button>
              <button
                className="sell-button"
                type="button"
                disabled={orderLoading}
                onClick={() => submitOrder("sell")}
              >
                {orderLoading ? "처리 중" : "매도"}
              </button>
            </div>
          </section>

          <section className="panel positions-panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">PORTFOLIO</p>
                <h2>보유 종목</h2>
              </div>
              <span className="position-count">{balance?.positions?.length ?? 0}종목</span>
            </div>

            {balance?.positions?.length > 0 ? (
              <div className="position-list">
                {balance.positions.map((position) => (
                  <article className="position-item" key={position.pdno ?? position.prdt_name}>
                    <div>
                      <strong>{position.prdt_name}</strong>
                      <span>{position.pdno}</span>
                    </div>
                    <div className="position-values">
                      <strong>{Number(position.hldg_qty).toLocaleString("ko-KR")}주</strong>
                      <span>현재가 {formatWon(position.prpr)}</span>
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <div className="empty-state">
                <strong>보유 종목이 없습니다.</strong>
                <span>모의주문 체결 후 새로고침해 확인할 수 있습니다.</span>
              </div>
            )}
          </section>
        </div>
          </>
        ) : activeView === "auto" ? (
          <AutoTrading config={config} />
        ) : (
          <Advisor />
        )}
      </main>
    </div>
  );
}


export default App;
