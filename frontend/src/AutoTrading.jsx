import { useEffect, useState } from "react";


const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";
const DEFAULT_WATCHLIST = "";


async function requestJson(path, options) {
  const response = await fetch(`${API_BASE_URL}${path}`, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail ?? `요청 실패 (${response.status})`);
  return data;
}


function formatWon(value) {
  return value === null || value === undefined
    ? "-"
    : `${Number(value).toLocaleString("ko-KR")}원`;
}


function AutoTrading({ config }) {
  const [state, setState] = useState(null);
  const [watchlist, setWatchlist] = useState(DEFAULT_WATCHLIST);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);
  const [settings, setSettings] = useState({
    quantity: 1,
    take_profit_pct: 0.7,
    stop_loss_pct: 0.4,
    max_hold_minutes: 10,
    entry_momentum_pct: 0.15,
    max_trades: 1,
    poll_seconds: 5,
  });

  useEffect(() => {
    let cancelled = false;
    const refresh = () => {
      requestJson("/auto/status")
        .then((data) => !cancelled && setState(data))
        .catch((error) => !cancelled && setMessage({ type: "error", text: error.message }));
    };
    const initialTimer = window.setTimeout(refresh, 0);
    const timer = window.setInterval(refresh, 2000);
    return () => {
      cancelled = true;
      window.clearTimeout(initialTimer);
      window.clearInterval(timer);
    };
  }, []);

  function updateSetting(name, value) {
    setSettings((current) => ({ ...current, [name]: Number(value) }));
  }

  async function scan() {
    const stockCodes = watchlist.split(",").map((item) => item.trim()).filter(Boolean);
    setBusy(true);
    setMessage(null);
    try {
      const result = await requestJson("/auto/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stock_codes: stockCodes.length ? stockCodes : null }),
      });
      setState(result);
      setMessage({ type: "success", text: `${result.selected.stock_name}을 자동매매 종목으로 선택했습니다.` });
    } catch (error) {
      setMessage({ type: "error", text: error.message });
    } finally {
      setBusy(false);
    }
  }

  async function start() {
    const selected = state?.selected;
    if (!selected) {
      setMessage({ type: "error", text: "먼저 후보 종목을 스캔해 주세요." });
      return;
    }
    const confirmed = window.confirm(
      `${selected.stock_name}(${selected.stock_code}) 자동매매를 시작할까요?\n진입 신호가 발생하면 KIS 모의투자 계좌에 주문이 접수됩니다.`,
    );
    if (!confirmed) return;

    setBusy(true);
    setMessage(null);
    try {
      const result = await requestJson("/auto/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settings),
      });
      setState(result);
      setMessage({ type: "success", text: "자동매매가 시작됐습니다." });
    } catch (error) {
      setMessage({ type: "error", text: error.message });
    } finally {
      setBusy(false);
    }
  }

  async function stop() {
    setBusy(true);
    try {
      const result = await requestJson("/auto/stop", { method: "POST" });
      setState(result);
      setMessage({ type: "info", text: "중지를 요청했습니다. 보유 중인 자동매매 수량은 매도 후 종료합니다." });
    } catch (error) {
      setMessage({ type: "error", text: error.message });
    } finally {
      setBusy(false);
    }
  }

  const running = state?.running;
  const selected = state?.selected;

  return (
    <>
      <header className="page-header">
        <div>
          <p className="eyebrow">AUTOMATED TRADING</p>
          <h1>자동 매매</h1>
          <p>후보 종목을 점수화하고 모멘텀 진입과 익절·손절을 자동 실행합니다.</p>
        </div>
        <span className={`auto-status status-${state?.status ?? "idle"}`}>
          {state?.phase ?? "상태 확인 중"}
        </span>
      </header>

      {message && <div className={`notice ${message.type}`} role="status">{message.text}</div>}

      <section className="summary-grid auto-summary" aria-label="자동매매 요약">
        <article className="summary-card">
          <span>선택 종목</span>
          <strong>{selected ? `${selected.stock_name} (${selected.stock_code})` : "미선택"}</strong>
        </article>
        <article className="summary-card">
          <span>현재가 / 진입가</span>
          <strong>{formatWon(state?.latest_price)} / {formatWon(state?.entry_price)}</strong>
        </article>
        <article className="summary-card accent">
          <span>자동매매 손익률</span>
          <strong>{state?.pnl_pct === null || state?.pnl_pct === undefined ? "-" : `${state.pnl_pct.toFixed(3)}%`}</strong>
        </article>
      </section>

      <div className="auto-grid">
        <section className="panel auto-control-panel">
          <div className="panel-heading">
            <div><p className="eyebrow">CONTROL</p><h2>종목 선정과 실행</h2></div>
            <span className="position-count">{config?.paper_trading ? "KIS 모의투자" : "Dry-run"}</span>
          </div>

          <label>
            후보 종목코드
            <textarea
              value={watchlist}
              placeholder="비워두면 KIS 거래대금 상위 종목을 자동 수집합니다."
              onChange={(event) => setWatchlist(event.target.value)}
              disabled={running}
            />
            <small>기본값은 거래대금 상위 보통주입니다. 직접 지정하려면 종목코드를 쉼표로 구분해 최대 20개까지 입력하세요.</small>
          </label>

          <div className="auto-settings-grid">
            <label>주문 수량<input type="number" min="1" value={settings.quantity} onChange={(e) => updateSetting("quantity", e.target.value)} disabled={running} /></label>
            <label>익절률 (%)<input type="number" min="0.1" step="0.1" value={settings.take_profit_pct} onChange={(e) => updateSetting("take_profit_pct", e.target.value)} disabled={running} /></label>
            <label>손절률 (%)<input type="number" min="0.1" step="0.1" value={settings.stop_loss_pct} onChange={(e) => updateSetting("stop_loss_pct", e.target.value)} disabled={running} /></label>
            <label>최대 보유 (분)<input type="number" min="1" value={settings.max_hold_minutes} onChange={(e) => updateSetting("max_hold_minutes", e.target.value)} disabled={running} /></label>
            <label>진입 상승률 (%)<input type="number" min="0.01" step="0.01" value={settings.entry_momentum_pct} onChange={(e) => updateSetting("entry_momentum_pct", e.target.value)} disabled={running} /></label>
            <label>최대 거래 횟수<input type="number" min="1" value={settings.max_trades} onChange={(e) => updateSetting("max_trades", e.target.value)} disabled={running} /></label>
          </div>

          <div className="auto-actions">
            <button type="button" className="secondary-button" onClick={scan} disabled={busy || running}>{busy ? "처리 중" : "후보 스캔"}</button>
            {!running ? (
              <button type="button" className="buy-button" onClick={start} disabled={busy || !selected}>자동매매 시작</button>
            ) : (
              <button type="button" className="sell-button" onClick={stop} disabled={busy}>자동매매 중지</button>
            )}
          </div>

          <p className="auto-note">기존 보유 주식은 시작 시 기준 수량으로 보호하며, 자동매매가 추가 매수한 수량만 매도합니다. 서버를 종료하면 실행 상태가 사라집니다.</p>
        </section>

        <section className="panel auto-log-panel">
          <div className="panel-heading">
            <div><p className="eyebrow">EVENT LOG</p><h2>실행 기록</h2></div>
            <span className="position-count">완료 {state?.trades_completed ?? 0}회</span>
          </div>
          <div className="auto-logs">
            {state?.logs?.length ? state.logs.map((log, index) => (
              <div className={`auto-log log-${log.level}`} key={`${log.time}-${index}`}>
                <time>{log.time}</time><span>{log.message}</span>
              </div>
            )) : <div className="empty-state"><strong>실행 기록이 없습니다.</strong><span>후보 스캔부터 시작해 주세요.</span></div>}
          </div>
        </section>
      </div>

      <section className="panel candidate-panel">
        <div className="panel-heading">
          <div><p className="eyebrow">CANDIDATES</p><h2>후보 점수</h2></div>
        </div>
        {state?.candidates?.length ? (
          <div className="candidate-table-wrap"><table className="candidate-table"><thead><tr><th>순위</th><th>종목</th><th>점수</th><th>현재가</th><th>등락률</th><th>전일 대비 거래량</th><th>거래량</th></tr></thead><tbody>
            {state.candidates.map((item, index) => <tr key={item.stock_code}><td>{index + 1}</td><td><strong>{item.stock_name}</strong><small>{item.stock_code}</small></td><td>{item.score.toFixed(2)}</td><td>{formatWon(item.price)}</td><td>{item.change_rate.toFixed(2)}%</td><td>{item.volume_ratio.toFixed(2)}%</td><td>{item.volume.toLocaleString("ko-KR")}</td></tr>)}
          </tbody></table></div>
        ) : <div className="empty-state compact"><strong>아직 후보 데이터가 없습니다.</strong><span>후보 스캔을 실행하면 점수 순으로 표시됩니다.</span></div>}
      </section>
    </>
  );
}


export default AutoTrading;
