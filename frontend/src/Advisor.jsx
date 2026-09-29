import { useCallback, useEffect, useMemo, useState } from "react";


const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

// NAV 곡선 색. 차트 라이브러리를 쓰지 않고 SVG를 직접 그리므로 색도 여기서 정한다 (설계 10.2).
// 시스템 포트폴리오는 진한 색, 기준선은 흐린 색이라 한눈에 갈린다.
const SERIES_COLORS = {
  sys_final_llm: "#2563eb",
  sys_final_v0: "#06b6d4",
  sys_prelim_llm: "#7c3aed",
  bl_kodex200: "#94a3b8",
  bl_sma10m: "#f59e0b",
  bl_6040: "#64748b",
};

const ROLE_COLORS = {
  cash: "#94a3b8",
  core: "#2563eb",
  sector: "#06b6d4",
  stock: "#e5484d",
};

const STAGE_OPTIONS = [
  { value: "", label: "자동" },
  { value: "prelim", label: "예비 18:30" },
  { value: "final", label: "최종 07:40" },
];


async function requestJson(path, options) {
  const response = await fetch(`${API_BASE_URL}${path}`, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail ?? `요청 실패 (${response.status})`);
  return data;
}


function pct(value, digits = 1) {
  if (value === null || value === undefined) return "-";
  return `${(Number(value) * 100).toFixed(digits)}%`;
}


function signedPct(value, digits = 1) {
  if (value === null || value === undefined) return "-";
  const out = (Number(value) * 100).toFixed(digits);
  return `${Number(value) > 0 ? "+" : ""}${out}%p`;
}


function score(value, digits = 2) {
  if (value === null || value === undefined) return "-";
  const n = Number(value);
  return `${n > 0 ? "+" : ""}${n.toFixed(digits)}`;
}


function num(value, digits = 2) {
  if (value === null || value === undefined) return "-";
  return Number(value).toFixed(digits);
}


function scoreClass(value) {
  if (value === null || value === undefined) return "adv-neutral";
  if (value > 0.05) return "adv-up";
  if (value < -0.05) return "adv-down";
  return "adv-neutral";
}


/** 요청 상태(불러오는 중·오류·기록 없음)를 한 자리에서 그린다. 구역마다 같은 모양이어야 한다. */
function Section({ eyebrow, title, subtitle, aside, loading, error, data, empty, children }) {
  let body = children;
  if (loading && !data) {
    body = <div className="empty-state compact"><strong>불러오는 중입니다.</strong></div>;
  } else if (error) {
    body = (
      <div className="empty-state compact">
        <strong>불러오지 못했습니다.</strong>
        <span>{error}</span>
      </div>
    );
  } else if (data && data.available === false) {
    body = (
      <div className="empty-state compact">
        <strong>{empty ?? "아직 기록이 없습니다."}</strong>
        <span>{data.reason}</span>
      </div>
    );
  }
  return (
    <section className="panel adv-panel">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">{eyebrow}</p>
          <h2>{title}</h2>
          {subtitle && <p className="adv-sub">{subtitle}</p>}
        </div>
        {aside}
      </div>
      {body}
    </section>
  );
}


/** 목표 비중 막대. 현금·핵심·섹터·종목 네 몫이 곧 이 모드의 결론이다 (결정 10). */
function WeightBar({ totals, variant }) {
  const rows = (totals ?? []).map((row) => ({
    role: row.role,
    label: row.label,
    weight: Number(row[`weight_${variant}`] ?? row.weight_v0 ?? 0),
  }));
  const sum = rows.reduce((acc, row) => acc + row.weight, 0) || 1;
  return (
    <div className="adv-bar-wrap">
      <div className="adv-bar" role="img" aria-label={rows.map((r) => `${r.label} ${pct(r.weight)}`).join(", ")}>
        {rows.map((row) => (
          <span
            key={row.role}
            className="adv-bar-seg"
            style={{ width: `${(100 * row.weight) / sum}%`, background: ROLE_COLORS[row.role] }}
            title={`${row.label} ${pct(row.weight)}`}
          >
            {row.weight / sum > 0.08 ? pct(row.weight, 0) : ""}
          </span>
        ))}
      </div>
      <div className="adv-legend">
        {rows.map((row) => (
          <span key={row.role}>
            <i style={{ background: ROLE_COLORS[row.role] }} />
            {row.label} {pct(row.weight)}
          </span>
        ))}
      </div>
    </div>
  );
}


/** NAV 곡선. 외부 차트 라이브러리 없이 SVG로 그린다 (설계 10.2). */
function NavChart({ portfolios, hidden, onToggle }) {
  const shown = portfolios.filter((p) => !hidden.has(p.portfolio_id));
  const dates = useMemo(() => {
    const set = new Set();
    portfolios.forEach((p) => p.points.forEach((q) => set.add(q.date)));
    return [...set].sort();
  }, [portfolios]);

  if (!portfolios.length || dates.length < 2) {
    return (
      <div className="empty-state compact">
        <strong>그릴 NAV가 아직 없습니다.</strong>
        <span>가상 포트폴리오가 이틀 이상 기록돼야 곡선이 그려집니다.</span>
      </div>
    );
  }

  const width = 960;
  const height = 300;
  const pad = { left: 46, right: 14, top: 14, bottom: 28 };
  const values = shown.flatMap((p) => p.points.map((q) => q.nav));
  const low = values.length ? Math.min(...values, 1) : 0.9;
  const high = values.length ? Math.max(...values, 1) : 1.1;
  const span = high - low || 0.02;
  const xOf = (date) => {
    const i = dates.indexOf(date);
    return pad.left + (i / (dates.length - 1)) * (width - pad.left - pad.right);
  };
  const yOf = (nav) =>
    pad.top + (1 - (nav - low) / span) * (height - pad.top - pad.bottom);
  const ticks = [low, low + span / 2, high];

  return (
    <>
      <div className="adv-chart-wrap">
        <svg viewBox={`0 0 ${width} ${height}`} className="adv-chart" role="img"
          aria-label="포트폴리오별 NAV 곡선">
          {ticks.map((t) => (
            <g key={t}>
              <line x1={pad.left} x2={width - pad.right} y1={yOf(t)} y2={yOf(t)} className="adv-grid" />
              <text x={pad.left - 8} y={yOf(t) + 4} className="adv-axis" textAnchor="end">
                {t.toFixed(2)}
              </text>
            </g>
          ))}
          <line x1={pad.left} x2={width - pad.right} y1={yOf(1)} y2={yOf(1)} className="adv-base" />
          {shown.map((p) => (
            <polyline
              key={p.portfolio_id}
              className={`adv-line${p.mode === "replay" ? " adv-line-replay" : ""}`}
              stroke={SERIES_COLORS[p.base_id] ?? "#475569"}
              points={p.points.map((q) => `${xOf(q.date)},${yOf(q.nav)}`).join(" ")}
            />
          ))}
          <text x={pad.left} y={height - 8} className="adv-axis">{dates[0]}</text>
          <text x={width - pad.right} y={height - 8} className="adv-axis" textAnchor="end">
            {dates[dates.length - 1]}
          </text>
        </svg>
      </div>
      <div className="adv-legend adv-legend-click">
        {portfolios.map((p) => (
          <button
            type="button"
            key={p.portfolio_id}
            className={hidden.has(p.portfolio_id) ? "adv-off" : ""}
            onClick={() => onToggle(p.portfolio_id)}
          >
            <i style={{ background: SERIES_COLORS[p.base_id] ?? "#475569" }}
              className={p.mode === "replay" ? "adv-dash" : ""} />
            {p.label}
          </button>
        ))}
      </div>
    </>
  );
}


/** 요인 분해 표. 시장·섹터·종목 어느 계층에나 같은 모양으로 쓴다. */
function FactorTable({ factors }) {
  if (!factors?.length) {
    return <div className="empty-state compact"><strong>요인 값이 없습니다.</strong></div>;
  }
  return (
    <div className="candidate-table-wrap">
      <table className="candidate-table adv-table">
        <thead>
          <tr>
            <th>요인</th><th>원본 값</th><th>점수</th><th>가중치</th><th>기여</th><th>비고</th>
          </tr>
        </thead>
        <tbody>
          {factors.map((f) => (
            <tr key={f.factor_id} className={f.missing ? "adv-missing" : ""}>
              <td title={f.source ?? ""}>
                <strong>{f.name}</strong>
                <small>{f.factor_id} · {f.transform}{f.llm ? " · LLM" : ""}</small>
              </td>
              <td>{f.raw_value === null || f.raw_value === undefined ? "-" : num(f.raw_value, 3)}</td>
              <td className={scoreClass(f.score)}>{f.missing ? "결측" : score(f.score)}</td>
              <td>{f.weight === 0 ? "관찰" : f.weight}</td>
              <td className={scoreClass(f.contribution)}>
                {f.contribution === null || f.contribution === undefined ? "-" : score(f.contribution)}
              </td>
              <td className="adv-note-cell">
                {f.missing ? (f.stages ? `${f.stages.join("·")} 단계에만 있음` : "값 없음") : ""}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}


function Flags({ flags }) {
  if (!flags?.length) return <span className="adv-dim">없음</span>;
  return (
    <>
      {flags.map((f, i) => (
        <span className={`chip ${f.flag_type === "halt" ? "warn" : "muted"}`} key={`${f.flag_type}-${i}`}
          title={f.detail ?? ""}>
          {f.label}
        </span>
      ))}
    </>
  );
}


const EVIDENCE_PREVIEW = 3;          // 근거를 처음에 몇 건 보일 것인가. 나머지는 접어 둔다


/** '2026-09-22T07:40:00.000' → '9/22 07:40'. 날짜만 있으면 '9/22'. */
function shortTime(iso) {
  if (!iso) return null;
  const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/);
  if (!m) return String(iso);
  const day = `${Number(m[2])}/${Number(m[3])}`;
  return m[4] ? `${day} ${m[4]}:${m[5]}` : day;
}


/** 근거 한 건. 종류·제목을 크게, 기계용 머리말은 작게, 시각과 판단까지의 경과를 한 줄에. */
function EvidenceItem({ ev }) {
  const isNews = ev.ref_type === "news";
  const meta = isNews
    ? [ev.time_label, ev.age_label]
    : [ev.age_label ?? ev.time_label, ev.received_at ? `처음 확인 ${shortTime(ev.received_at)}` : null];
  const when = meta.filter(Boolean);
  return (
    <li>
      <div className="adv-ev-title">
        <span className={`chip adv-ev-kind ${isNews ? "news" : "disclosure"}`}>{ev.ref_label ?? ev.ref_type}</span>
        <strong>{ev.title ?? ev.summary}</strong>
      </div>
      <div className="adv-ev-meta">
        {when.length ? when.join(" · ") : "시각 기록 없음"}
        {ev.after_decision && <span className="adv-warn"> · 판단 뒤 정보</span>}
        {ev.factor_name && <span> · {ev.factor_name}</span>}
      </div>
      {ev.detail && <div className="adv-ev-detail">{ev.detail}</div>}
      {ev.head && <div className="adv-dim adv-ev-head">{ev.head}</div>}
    </li>
  );
}


/**
 * LLM 조정 한 건 (③). 이유 한 줄만 보이면 "무엇을, 언제의 정보로 판단했나"가 가려져
 * 옛 뉴스로 판단한 것처럼 읽힌다. 그래서 조정 전후 점수, 채택·기각 요인, 근거와 그 시각을 함께 그린다.
 * 새 필드가 없는 구버전 응답이면 예전 모양(이름·조정값·이유·기각)만 그린다.
 */
function LlmAdjustment({ row }) {
  const [open, setOpen] = useState(false);
  const evidence = row.evidence ?? [];
  const shown = open ? evidence : evidence.slice(0, EVIDENCE_PREVIEW);
  const adopted = row.adopted_factors ?? [];
  const rejected = row.rejected_factors
    ?? (Array.isArray(row.rejected) ? row.rejected : []).map((r) => ({ ...r, name: r.factor_id }));
  const hasScores = row.base_score != null && row.final_score != null;
  return (
    <li>
      <strong>{row.name}</strong>{" "}
      <span className={scoreClass(row.adj)}>{score(row.adj)}</span>
      {row.vetoed && <span className="chip warn">거부</span>}
      {hasScores && (
        <small> · 점수 {score(row.base_score)} → {score(row.final_score)}{row.layer_label ? ` (${row.layer_label})` : ""}</small>
      )}
      <div className="adv-reason">{row.reason}</div>
      {adopted.length > 0 && (
        <div className="adv-ev-factors">
          <span className="adv-dim">채택 요인</span>
          {adopted.map((f, i) => <span className="chip ok" key={f.factor_id ?? i}>{f.name}</span>)}
        </div>
      )}
      {rejected.map((r, i) => (
        <div className="adv-dim" key={i}>기각 {r.name ?? r.factor_id}: {r.reason}</div>
      ))}
      {row.evidence !== undefined && (
        evidence.length ? (
          <>
            <div className="adv-dim">
              근거 {row.evidence_total ?? evidence.length}건
              {row.evidence_scope === "entity" && " · 채택 요인에 딸린 근거가 없어 이 자산의 전체 근거를 보입니다"}
              {row.decision_time && ` · 판단 시각 ${shortTime(row.decision_time)}`}
            </div>
            <ul className="adv-evidence">
              {shown.map((ev, i) => <EvidenceItem ev={ev} key={`${ev.ref_type}-${ev.ref_id ?? i}`} />)}
            </ul>
            {evidence.length > EVIDENCE_PREVIEW && (
              <button type="button" className="adv-more" onClick={() => setOpen((v) => !v)}>
                {open ? "근거 접기" : `근거 ${evidence.length - EVIDENCE_PREVIEW}건 더 보기`}
              </button>
            )}
            {(row.evidence_total ?? 0) > evidence.length && (
              <div className="adv-dim">근거 {row.evidence_total}건 중 {evidence.length}건만 실었습니다.</div>
            )}
          </>
        ) : (
          <div className="adv-dim">이 조정에 연결된 뉴스·공시 근거 기록이 없습니다.</div>
        )
      )}
    </li>
  );
}


/** 자산 상세. ②의 표에서 행을 누르면 열린다. */
function AssetDetail({ detail, busy, error, onClose }) {
  if (busy && !detail) {
    return <div className="adv-detail"><div className="empty-state compact"><strong>불러오는 중입니다.</strong></div></div>;
  }
  if (error) {
    return (
      <div className="adv-detail">
        <div className="empty-state compact"><strong>불러오지 못했습니다.</strong><span>{error}</span></div>
      </div>
    );
  }
  if (!detail) return null;
  if (detail.available === false) {
    return (
      <div className="adv-detail">
        <div className="adv-detail-head">
          <h3>자산 상세</h3>
          <button type="button" className="secondary-button" onClick={onClose}>닫기</button>
        </div>
        <div className="empty-state compact"><strong>기록이 없습니다.</strong><span>{detail.reason}</span></div>
      </div>
    );
  }

  const llm = detail.llm;
  return (
    <div className="adv-detail">
      <div className="adv-detail-head">
        <div>
          <h3>{detail.name} <small>{detail.code}</small></h3>
          <p className="adv-sub">
            {detail.layer_label} 계층{detail.sector ? ` · ${detail.sector}` : ""} ·
            {" "}{detail.date} {detail.run?.stage_label}
          </p>
        </div>
        <button type="button" className="secondary-button" onClick={onClose}>닫기</button>
      </div>

      <div className="adv-kpis">
        <div><small>계층 점수</small><strong className={scoreClass(detail.layer_score)}>{score(detail.layer_score)}</strong></div>
        {detail.sector_part && (
          <div>
            <small>섹터 기울기 ({detail.sector_part.tilt})</small>
            <strong className={scoreClass(detail.sector_part.contribution)}>
              {score(detail.sector_part.contribution)}
            </strong>
          </div>
        )}
        <div><small>v0 종합</small><strong className={scoreClass(detail.score_v0)}>{score(detail.score_v0)}</strong></div>
        <div><small>LLM 조정 후</small><strong className={scoreClass(detail.score_llm)}>{score(detail.score_llm)}</strong></div>
        <div><small>목표 비중 (v0 / LLM)</small><strong>
          {pct(detail.weights?.v0?.weight ?? 0)} / {pct(detail.weights?.llm?.weight ?? 0)}
        </strong></div>
      </div>

      <FactorTable factors={detail.factors} />

      <div className="adv-detail-grid">
        <div>
          <h4>위험 표시</h4>
          <p><Flags flags={detail.flags} /></p>
          <h4>근거</h4>
          {detail.evidence?.length ? (
            <ul className="adv-list">
              {detail.evidence.map((e, i) => (
                <li key={`${e.ref_id}-${i}`}>
                  <span className="chip muted">{e.factor_id}</span> {e.summary}
                  {e.disclosure && (
                    <small> · {e.disclosure.report_nm} ({e.disclosure.rcept_dt})
                      {e.disclosure.first_seen_at ? ` · 최초 인지 ${e.disclosure.first_seen_at.slice(11, 19)}` : ""}
                    </small>
                  )}
                </li>
              ))}
            </ul>
          ) : <p className="adv-dim">기록된 근거가 없습니다.</p>}
        </div>
        <div>
          <h4>LLM 조정</h4>
          {llm ? (
            <>
              <p>
                조정 <strong className={scoreClass(llm.adj)}>{score(llm.adj)}</strong>
                {llm.vetoed && <span className="chip warn">거부</span>}
              </p>
              {llm.reason && <p className="adv-reason">{llm.reason}</p>}
              <p><strong>채택</strong>{" "}
                {(llm.adopted ?? []).length
                  ? (llm.adopted ?? []).map((a) => <span className="chip ok" key={a}>{a}</span>)
                  : <span className="adv-dim">없음</span>}
              </p>
              <div>
                <strong>기각</strong>
                {(llm.rejected ?? []).length ? (
                  <ul className="adv-list">
                    {(llm.rejected ?? []).map((r, i) => (
                      <li key={i}><span className="chip muted">{r.factor_id ?? "-"}</span> {r.reason ?? ""}</li>
                    ))}
                  </ul>
                ) : <span className="adv-dim"> 없음</span>}
              </div>
            </>
          ) : <p className="adv-dim">{detail.llm_note ?? "LLM 조정 없음 (v0와 동일)"}</p>}
        </div>
      </div>
    </div>
  );
}


function Advisor() {
  const [mode, setMode] = useState("live");
  const [stage, setStage] = useState("");
  const [date, setDate] = useState("");
  const [reportState, setReportState] = useState(null);
  const [statusData, setStatusData] = useState(null);
  const [perfState, setPerfState] = useState(null);
  const [metricsState, setMetricsState] = useState(null);
  const [detailState, setDetailState] = useState(null);
  const [dates, setDates] = useState([]);
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);
  const [hidden, setHidden] = useState(new Set());
  const [metricStage, setMetricStage] = useState("final");
  const [metricVariant, setMetricVariant] = useState("v0");

  // 응답에 그 응답이 어느 조회 조건의 것인지(key)를 함께 담아 둔다. 조건이 바뀌는 순간
  // 지난 조건의 결과는 저절로 '없는 것'이 되므로, 효과에서 상태를 지우는 정리 작업이 필요 없다.
  const viewKey = `${mode}|${date}|${stage}`;

  const loadReport = useCallback(() => {
    const key = `${mode}|${date}|${stage}`;
    const params = new URLSearchParams({ mode });
    if (date) params.set("date", date);
    if (stage) params.set("stage", stage);
    return Promise.all([
      requestJson(`/advisor/report?${params}`),
      requestJson(`/advisor/status?mode=${mode}`).catch(() => null),
    ])
      .then(([data, st]) => {
        setReportState({ key, data });
        if (st) setStatusData(st);
        if (data.dates?.length) setDates(data.dates);
      })
      .catch((error) => setReportState({ key, error: error.message }));
  }, [date, mode, stage]);

  useEffect(() => {
    loadReport();
  }, [loadReport]);

  useEffect(() => {
    const key = mode;
    requestJson(`/advisor/performance?mode=${mode}`)
      .then((data) => setPerfState({ key, data }))
      .catch((error) => setPerfState({ key, error: error.message }));
    requestJson(`/advisor/metrics?mode=${mode}`)
      .then((data) => setMetricsState({ key, data }))
      .catch((error) => setMetricsState({ key, error: error.message }));
  }, [mode]);

  const reportFresh = reportState?.key === viewKey ? reportState : null;
  const perfFresh = perfState?.key === mode ? perfState : null;
  const metricsFresh = metricsState?.key === mode ? metricsState : null;
  const detailFresh = detailState?.key === viewKey ? detailState : null;

  const report = reportFresh?.data ?? null;
  const perf = perfFresh?.data ?? null;
  const metrics = metricsFresh?.data ?? null;
  const loading = { report: !reportFresh, perf: !perfFresh, metrics: !metricsFresh };
  const errors = {
    report: reportFresh?.error ?? null,
    perf: perfFresh?.error ?? null,
    metrics: metricsFresh?.error ?? null,
  };

  async function openAsset(code) {
    const key = viewKey;
    const params = new URLSearchParams({ mode });
    if (report?.date) params.set("date", report.date);
    if (report?.stage) params.set("stage", report.stage);
    setDetailState({ key, busy: true });
    try {
      setDetailState({ key, data: await requestJson(`/advisor/asset/${encodeURIComponent(code)}?${params}`) });
    } catch (error) {
      setDetailState({ key, error: error.message });
    }
  }

  async function runBatch() {
    const label = stage === "prelim" ? "예비" : "최종";
    const ok = window.confirm(
      `${label} 배치를 지금 실행할까요?\n판단 기록이 새로 생기고 수 분이 걸릴 수 있습니다.`
      + `\n(모드: ${mode === "live" ? "실시간" : "재현"}${date ? `, 기준일 ${date}` : ""})`,
    );
    if (!ok) return;
    setBusy(true);
    setNotice(null);
    try {
      const out = await requestJson("/advisor/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stage: stage || "final", as_of: date || null, mode }),
      });
      setStatusData(out.status ?? null);
      setNotice({ type: "success", text: `배치를 시작했습니다 (pid ${out.batch?.pid}). 끝나면 새로고침하세요.` });
    } catch (error) {
      setNotice({ type: "error", text: error.message });
    } finally {
      setBusy(false);
    }
  }

  function toggleSeries(id) {
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  const ready = report?.available;
  const variant = report?.primary_variant ?? "v0";
  const scheduler = statusData?.scheduler;
  const collector = statusData?.collector;
  const metricRows = (metrics?.metrics ?? []).filter(
    (m) => m.stage === metricStage && m.variant === metricVariant,
  );

  return (
    <>
      <header className="page-header">
        <div>
          <p className="eyebrow">ADVISOR</p>
          <h1>판단 지원</h1>
          <p className="page-subtitle">
            주문을 내지 않고 하루 두 번 시장·섹터·종목을 점수화해 현금·ETF·종목의 목표 비중을 정합니다.
            판단과 근거, 사후 결과를 모두 저장해 무엇이 맞고 틀렸는지 측정합니다.
          </p>
        </div>
        <div className="adv-status-row">
          <div className={`mode-card adv-status-card ${scheduler?.running ? "paper" : "dry"}`}>
            <span className="status-dot" />
            <div>
              <small>자동 판단 (18:30 · 07:40)</small>
              <strong>{scheduler?.running ? "가동 중" : scheduler?.enabled ? "시작 중" : "꺼짐"}</strong>
            </div>
          </div>
          <div className={`mode-card adv-status-card ${collector?.running ? "paper" : "dry"}`}
            title={collector?.manual_stop ? "뉴스 탭에서 직접 끈 상태라 자동으로 되살리지 않습니다" : ""}>
            <span className="status-dot" />
            <div>
              <small>실시간 뉴스·공시 수집</small>
              <strong>{collector?.running ? "수집 중" : collector?.manual_stop ? "수동 중지" : "꺼짐"}</strong>
            </div>
          </div>
        </div>
      </header>

      {notice && <div className={`notice ${notice.type}`} role="status">{notice.text}</div>}
      {statusData?.fallback_needed && (
        <div className="notice error" role="status">
          대체 규칙: {statusData.fallback?.reason} — 전날 예비 판단을 체결 예약으로 승격해야 합니다.
        </div>
      )}
      {mode === "replay" && (
        <div className="notice info adv-replay-note" role="status">
          재현 모드 — 성과 주장 아님. 과거 날짜로 같은 절차를 돌린 기록이며 코드 요인만 계산됩니다.
        </div>
      )}

      <div className="panel adv-controls">
        <label>
          기준일
          <select value={date} onChange={(e) => setDate(e.target.value)}>
            <option value="">최근 판단</option>
            {dates.map((d) => (
              <option key={d.date} value={d.date}>
                {d.date}{d.has_decision ? "" : " (판단 없음)"}
              </option>
            ))}
          </select>
        </label>
        <div className="adv-toggle" role="group" aria-label="판단 단계">
          <span>단계</span>
          {STAGE_OPTIONS.map((opt) => (
            <button type="button" key={opt.value}
              className={stage === opt.value ? "active" : ""}
              onClick={() => setStage(opt.value)}>{opt.label}</button>
          ))}
        </div>
        <div className="adv-toggle" role="group" aria-label="기록 모드">
          <span>모드</span>
          <button type="button" className={mode === "live" ? "active" : ""}
            onClick={() => setMode("live")}>실시간</button>
          <button type="button" className={mode === "replay" ? "active" : ""}
            onClick={() => setMode("replay")}>재현</button>
        </div>
        <div className="adv-controls-actions">
          <button type="button" className="secondary-button" onClick={loadReport} disabled={busy}>
            새로고침
          </button>
          <button type="button" className="secondary-button" onClick={runBatch} disabled={busy}>
            배치 실행
          </button>
        </div>
      </div>

      {/* ① 오늘의 판단 */}
      <Section
        eyebrow="TODAY"
        title="오늘의 판단"
        subtitle={ready ? `${report.date} · ${report.run?.stage_label} 단계 · 판단 시각 ${report.run?.decision_time?.slice(11, 16) ?? "-"}` : null}
        aside={ready && (
          <span className={`auto-status ${report.run?.status === "ok" ? "status-running" : "status-error"}`}>
            {report.run?.status_label}
          </span>
        )}
        loading={loading.report}
        error={errors.report}
        data={report}
        empty="아직 판단 기록이 없습니다."
      >
        {ready && (
          <>
            <div className="adv-kpis">
              <div><small>위험자산 비중</small><strong>{pct(report.market?.risk_weight)}</strong></div>
              <div><small>시장 점수</small>
                <strong className={scoreClass(report.market?.score)}>{score(report.market?.score)}</strong>
              </div>
              <div><small>판단 버전</small><strong>{report.variants?.join(" · ") || "-"}</strong></div>
              <div><small>설정 지문</small><strong>{report.run?.config_hash ?? "-"}</strong></div>
            </div>

            <WeightBar totals={report.allocation?.role_totals} variant={variant} />

            {report.llm_note && <p className="auto-note">{report.llm_note}</p>}

            <div className="adv-two-col">
              <div>
                <h4>어제와 무엇이 달라졌나</h4>
                {report.diff?.available ? (
                  <>
                    <p className="adv-reason">{report.diff.summary}</p>
                    <div className="candidate-table-wrap">
                      <table className="candidate-table adv-table">
                        <thead><tr><th>몫</th><th>직전</th><th>이번</th><th>변화</th></tr></thead>
                        <tbody>
                          {report.diff.role_totals.map((row) => (
                            <tr key={row.role}>
                              <td><strong>{row.label}</strong></td>
                              <td>{pct(row.prev)}</td>
                              <td>{pct(row.current)}</td>
                              <td className={row.delta > 0 ? "adv-up" : row.delta < 0 ? "adv-down" : ""}>
                                {signedPct(row.delta)}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <p className="auto-note">
                      직전 {report.diff.prev_run?.stage_label} 판단 ({report.diff.prev_run?.as_of}) 대비입니다.
                    </p>
                  </>
                ) : (
                  <p className="adv-dim">{report.diff?.reason ?? "비교할 직전 판단이 없습니다."}</p>
                )}
              </div>
              <div>
                <h4>왜 — 점수가 가장 크게 움직인 요인</h4>
                {report.diff?.factors?.length ? (
                  <ul className="adv-list">
                    {report.diff.factors.map((f) => (
                      <li key={`${f.entity}-${f.factor_id}`}>
                        <span className="chip muted">{f.entity_name}</span> {f.name}{" "}
                        <span className={scoreClass(f.delta)}>{score(f.prev)} → {score(f.current)}</span>
                      </li>
                    ))}
                  </ul>
                ) : <p className="adv-dim">비교할 요인 값이 없습니다.</p>}

                <h4>수집 요약</h4>
                <p>
                  {(report.run?.note?.providers ?? []).map((p) => (
                    <span className="chip muted" key={p.name ?? p}>
                      {p.name ? `${p.name}: ${p.detail}` : String(p)}
                    </span>
                  ))}
                  {!(report.run?.note?.providers ?? []).length && <span className="adv-dim">기록 없음</span>}
                </p>
                {(report.run?.note?.fallbacks_used ?? []).length > 0 && (
                  <p className="adv-warn">대체 경로: {report.run.note.fallbacks_used.join(" · ")}</p>
                )}
                {report.run?.note?.text && <p className="adv-warn">{report.run.note.text}</p>}
              </div>
            </div>
          </>
        )}
      </Section>

      {/* ② 왜 이 판단인가 */}
      <Section
        eyebrow="WHY"
        title="왜 이 판단인가"
        subtitle="시장 → 섹터 → 종목 순으로 점수를 분해합니다. 행을 누르면 자산 상세가 열립니다."
        loading={loading.report}
        error={errors.report}
        data={report}
        empty="점수 기록이 없습니다."
      >
        {ready && (
          <>
            <h4>시장 점수 분해</h4>
            <FactorTable factors={report.market?.factors} />

            <h4>섹터 점수</h4>
            <div className="candidate-table-wrap">
              <table className="candidate-table adv-table adv-clickable">
                <thead>
                  <tr><th>섹터</th><th>v0 점수</th><th>LLM 점수</th><th>요인</th><th>표시</th></tr>
                </thead>
                <tbody>
                  {(report.sectors?.rows ?? []).map((row) => (
                    <tr key={row.entity} onClick={() => openAsset(row.entity)} tabIndex={0}
                      onKeyDown={(e) => e.key === "Enter" && openAsset(row.entity)}>
                      <td><strong>{row.name}</strong></td>
                      <td className={scoreClass(row.score_v0)}>{score(row.score_v0)}</td>
                      <td className={scoreClass(row.score_llm)}>{score(row.score_llm)}</td>
                      <td className="adv-note-cell">
                        {row.factors.filter((f) => !f.missing).map((f) => `${f.name} ${score(f.score)}`).join(" · ") || "-"}
                      </td>
                      <td><Flags flags={row.flags} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <h4>종목 상위 {report.stocks?.rows?.length ?? 0} <small className="adv-dim">(전체 {report.stocks?.total ?? 0})</small></h4>
            <div className="candidate-table-wrap">
              <table className="candidate-table adv-table adv-clickable adv-rank">
                <thead>
                  <tr>
                    <th>#</th><th>종목</th><th>섹터</th><th>v0 점수</th><th>조정</th>
                    <th>LLM 점수</th><th>표시</th>
                  </tr>
                </thead>
                <tbody>
                  {(report.stocks?.rows ?? []).map((row, i) => (
                    <tr key={row.entity} onClick={() => openAsset(row.entity)} tabIndex={0}
                      onKeyDown={(e) => e.key === "Enter" && openAsset(row.entity)}
                      className={row.vetoed ? "adv-vetoed" : ""}>
                      <td>{i + 1}</td>
                      <td><strong>{row.name}</strong><small>{row.entity}</small></td>
                      <td>{row.sector ?? "-"}</td>
                      <td className={scoreClass(row.score_v0)}>{score(row.score_v0)}</td>
                      <td className={scoreClass(row.adj)}>{row.adj ? score(row.adj) : "-"}</td>
                      <td className={scoreClass(row.score_llm)}>
                        {row.vetoed ? <span className="chip warn">거부</span> : score(row.score_llm)}
                      </td>
                      <td><Flags flags={row.flags} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {detailFresh && (
              <AssetDetail detail={detailFresh.data} busy={detailFresh.busy}
                error={detailFresh.error} onClose={() => setDetailState(null)} />
            )}
          </>
        )}
      </Section>

      {/* ③ v0 대 LLM, 예비 대 최종 */}
      <Section
        eyebrow="COMPARE"
        title="v0 대 LLM · 예비 대 최종"
        subtitle="같은 날 두 판단의 비중 차이와 LLM이 남긴 채택·기각 이유입니다."
        loading={loading.report}
        error={errors.report}
        data={report}
        empty="비교할 판단이 없습니다."
      >
        {ready && (
          <div className="adv-two-col">
            <div>
              <h4>v0 대 LLM 조정</h4>
              {report.variants?.includes("llm") ? (
                <>
                  <div className="candidate-table-wrap">
                    <table className="candidate-table adv-table">
                      <thead><tr><th>자산</th><th>몫</th><th>v0</th><th>LLM</th><th>차이</th></tr></thead>
                      <tbody>
                        {(report.allocation?.weights ?? [])
                          .filter((row) => Math.abs(row.delta ?? 0) > 1e-9)
                          .map((row) => (
                            <tr key={row.asset}>
                              <td><strong>{row.name}</strong><small>{row.asset}</small></td>
                              <td>{row.role_label}</td>
                              <td>{pct(row.weight_v0 ?? 0)}</td>
                              <td>{pct(row.weight_llm ?? 0)}</td>
                              <td className={row.delta > 0 ? "adv-up" : "adv-down"}>{signedPct(row.delta)}</td>
                            </tr>
                          ))}
                      </tbody>
                    </table>
                  </div>
                  {!(report.allocation?.weights ?? []).some((r) => Math.abs(r.delta ?? 0) > 1e-9) && (
                    <p className="adv-dim">LLM 조정이 목표 비중을 바꾸지 않았습니다 (점수만 움직였습니다).</p>
                  )}
                  <h4>LLM이 남긴 이유</h4>
                  {report.llm_adjustments?.length ? (
                    <ul className="adv-list">
                      {report.llm_adjustments.map((row) => (
                        <LlmAdjustment row={row} key={row.entity} />
                      ))}
                    </ul>
                  ) : <p className="adv-dim">조정된 자산이 없습니다.</p>}
                </>
              ) : (
                <p className="adv-dim">{report.llm_note}</p>
              )}
            </div>
            <div>
              <h4>예비 대 최종</h4>
              {report.stage_compare?.available ? (
                <>
                  <p className="auto-note">
                    {report.stage_compare.current.run.stage_label} ({report.stage_compare.current.run.decision_time?.slice(11, 16)})
                    {" 대 "}
                    {report.stage_compare.other.run.stage_label} ({report.stage_compare.other.run.decision_time?.slice(11, 16)}) ·
                    {" 시장 점수 "}
                    <span className={scoreClass(report.stage_compare.current.decision?.market_score)}>
                      {score(report.stage_compare.current.decision?.market_score)}
                    </span>
                    {" 대 "}
                    <span className={scoreClass(report.stage_compare.other.decision?.market_score)}>
                      {score(report.stage_compare.other.decision?.market_score)}
                    </span>
                  </p>
                  <div className="candidate-table-wrap">
                    <table className="candidate-table adv-table">
                      <thead><tr><th>자산</th><th>이번 단계</th><th>다른 단계</th><th>차이</th></tr></thead>
                      <tbody>
                        {report.stage_compare.weights.slice(0, 12).map((row) => (
                          <tr key={row.asset}>
                            <td><strong>{row.name}</strong><small>{row.role_label}</small></td>
                            <td>{pct(row.current ?? 0)}</td>
                            <td>{pct(row.other ?? 0)}</td>
                            <td className={row.delta > 0 ? "adv-up" : row.delta < 0 ? "adv-down" : ""}>
                              {signedPct(row.delta)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              ) : <p className="adv-dim">{report.stage_compare?.reason}</p>}
            </div>
          </div>
        )}
      </Section>

      {/* ④ 성과 */}
      <Section
        eyebrow="PERFORMANCE"
        title="성과"
        subtitle="가상 포트폴리오와 기준선의 NAV입니다. 실시간과 재현 기록은 절대 한 곡선으로 잇지 않습니다."
        aside={perf?.available && (
          <span className="position-count">
            {perf.dates?.[0]} ~ {perf.dates?.[perf.dates.length - 1]}
          </span>
        )}
        loading={loading.perf}
        error={errors.perf}
        data={perf}
        empty="아직 NAV 기록이 없습니다."
      >
        {perf?.available && (
          <>
            {perf.replay && <p className="adv-warn">재현 모드 — 성과 주장 아님 (점선)</p>}
            <NavChart portfolios={perf.portfolios} hidden={hidden} onToggle={toggleSeries} />
            <div className="candidate-table-wrap">
              <table className="candidate-table adv-table">
                <thead>
                  <tr>
                    <th>포트폴리오</th><th>NAV</th><th>수익률</th><th>연율 변동성</th>
                    <th>최대 낙폭</th><th>회전율</th><th>비용</th><th>일수</th>
                  </tr>
                </thead>
                <tbody>
                  {perf.portfolios.map((p) => (
                    <tr key={p.portfolio_id} className={p.mode === "replay" ? "adv-replay-row" : ""}>
                      <td>
                        <strong>
                          <i className="adv-swatch" style={{ background: SERIES_COLORS[p.base_id] ?? "#475569" }} />
                          {p.label}
                        </strong>
                        <small>{p.portfolio_id}</small>
                      </td>
                      <td>{num(p.summary.nav, 4)}</td>
                      <td className={p.summary.total_return > 0 ? "adv-up" : "adv-down"}>
                        {pct(p.summary.total_return, 2)}
                      </td>
                      <td>{p.summary.ann_vol === null ? "-" : pct(p.summary.ann_vol, 1)}</td>
                      <td className="adv-down">{pct(p.summary.max_drawdown, 2)}</td>
                      <td>{pct(p.summary.turnover, 1)}</td>
                      <td>{pct(p.summary.cost, 3)}</td>
                      <td>{p.summary.n_days}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {perf.other_mode_available && (
              <p className="auto-note">
                {perf.replay ? "실시간" : "재현"} 모드 기록도 있습니다. 위의 모드 전환으로 볼 수 있습니다.
              </p>
            )}
          </>
        )}
      </Section>

      {/* ⑤ 요인 성적표 */}
      <Section
        eyebrow="FACTOR METRICS"
        title="요인 성적표"
        subtitle={`유효 표본(n_eff)이 ${metrics?.min_n_eff ?? "-"} 미만이면 회색 '판단 불가'로 둡니다. 표본이 모자란 지표를 숫자로 보이면 그것부터 결론처럼 읽힙니다.`}
        aside={metrics?.available && (
          <div className="adv-toggle adv-toggle-sm">
            {["prelim", "final"].map((s) => (
              <button type="button" key={s} className={metricStage === s ? "active" : ""}
                onClick={() => setMetricStage(s)}>{s === "prelim" ? "예비" : "최종"}</button>
            ))}
            {["v0", "llm"].map((v) => (
              <button type="button" key={v} className={metricVariant === v ? "active" : ""}
                onClick={() => setMetricVariant(v)}>{v === "v0" ? "v0" : "LLM"}</button>
            ))}
          </div>
        )}
        loading={loading.metrics}
        error={errors.metrics}
        data={metrics}
        empty="아직 요인 지표가 없습니다."
      >
        {metrics?.available && (
          <>
            {metrics.note && <p className="adv-warn">{metrics.note}</p>}
            {metricRows.length ? (
              <div className="candidate-table-wrap">
                <table className="candidate-table adv-table">
                  <thead>
                    <tr>
                      <th>요인</th><th>계층</th><th>가중치</th><th>기간</th>
                      <th>n_eff</th><th>순위 상관</th><th>적중률</th><th>판정</th>
                    </tr>
                  </thead>
                  <tbody>
                    {metricRows.map((m) => (
                      <tr key={`${m.factor_id}-${m.horizon}`} className={m.judgable ? "" : "adv-missing"}>
                        <td title={m.source ?? ""}><strong>{m.name}</strong><small>{m.factor_id}</small></td>
                        <td>{m.layer}</td>
                        <td>{m.weight === 0 ? "관찰" : m.weight}</td>
                        <td>{m.horizon}일</td>
                        <td>{num(m.n_eff, 1)} <small>({m.n_days}일)</small></td>
                        <td className={m.judgable ? scoreClass(m.rank_ic_mean) : ""}>
                          {m.rank_ic_mean === null ? "해당 없음"
                            : `${score(m.rank_ic_mean, 3)} ± ${num(m.rank_ic_std, 3)}`}
                        </td>
                        <td>{m.hit_rate === null ? "-" : pct(m.hit_rate, 1)}</td>
                        <td>{m.judgable
                          ? <span className="chip ok">측정 중</span>
                          : <span className="chip muted">판단 불가</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="empty-state compact">
                <strong>이 조합의 지표가 없습니다.</strong>
                <span>단계·판단 버전을 바꿔 보세요.</span>
              </div>
            )}
            <p className="auto-note">
              시장 요인은 하루에 값이 하나라 날짜 안의 순위 상관이 없습니다 — 부호 적중률로 봅니다.
              실시간 기록이 며칠뿐이라 어떤 요인도 효과가 있다·없다를 말할 수 없습니다.
            </p>
          </>
        )}
      </Section>
    </>
  );
}


export default Advisor;
