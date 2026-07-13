// CGC Trading Desk — live dashboard client.
// Polls /api/data every 5s and re-renders every section (not just a handful
// of fields like the previous version), so a full page reload is only a
// long-interval safety net against client-side drift, not something the UI
// depends on for freshness.

const POLL_MS = 5000;
const SAFETY_RELOAD_MS = 60 * 60 * 1000; // 1 hour
let lastReload = Date.now();
let actionInFlight = false;

function readInitialJson(id, fallback) {
  const el = document.getElementById(id);
  if (!el) return fallback;
  try {
    return JSON.parse(el.textContent || "");
  } catch {
    return fallback;
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function setText(id, value, fallback = "-") {
  const el = document.getElementById(id);
  if (el) el.textContent = value ?? fallback;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

// --------------------------------------------------------------- charts --

function renderEquityCurve(points) {
  const svg = document.getElementById("equity-curve-chart");
  if (!svg) return;

  const safePoints = Array.isArray(points) ? points : [];
  svg.innerHTML = "";

  if (!safePoints.length) {
    svg.innerHTML = '<text x="16" y="82" class="chart-empty">No equity points yet</text>';
    return;
  }

  const values = safePoints.map((point) => Number(point.equity || 0));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = Math.max(max - min, 1);

  const coords = values.map((value, index) => {
    const x = 16 + (index / Math.max(values.length - 1, 1)) * (420 - 32);
    const y = 160 - 16 - ((value - min) / range) * (160 - 32);
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });

  const polyline = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  polyline.setAttribute("points", coords.join(" "));
  polyline.setAttribute("class", "chart-line");
  svg.appendChild(polyline);
}

function renderPnlBars(periodicPnl) {
  if (!periodicPnl) return;
  const values = [
    ["pnl-bar-day", "pnl-bar-day-fill", Number(periodicPnl.daily || 0)],
    ["pnl-bar-week", "pnl-bar-week-fill", Number(periodicPnl.weekly || 0)],
    ["pnl-bar-month", "pnl-bar-month-fill", Number(periodicPnl.monthly || 0)],
  ];
  const maxAbs = Math.max(...values.map(([, , value]) => Math.abs(value)), 1);

  values.forEach(([labelId, fillId, value]) => {
    setText(labelId, value.toFixed(4));
    const fillEl = document.getElementById(fillId);
    if (fillEl) fillEl.style.height = `${clamp((Math.abs(value) / maxAbs) * 100, 8, 100)}%`;
  });
}

// ------------------------------------------------------------- renderers --

function renderBotControl(bot) {
  if (!bot) return;
  setText("bot-status-text", bot.status);
  setText("bot-pid-text", bot.pid ? `pid ${bot.pid}` : "not running");

  const card = document.getElementById("bot-control-card");
  if (card) {
    card.classList.toggle("good", !!bot.running);
    card.classList.toggle("danger", !bot.running);
  }

  const dot = document.getElementById("brand-dot");
  if (dot) {
    dot.classList.toggle("good", !!bot.running);
    dot.classList.toggle("danger", !bot.running);
  }

  const startBtn = document.getElementById("btn-start-bot");
  const stopBtn = document.getElementById("btn-stop-bot");
  if (startBtn && !actionInFlight) startBtn.disabled = !!bot.running;
  if (stopBtn && !actionInFlight) stopBtn.disabled = !bot.running;
}

function renderPositions(positions, protectionRows) {
  const container = document.getElementById("position-list");
  if (!container) return;

  const protectionBySymbol = {};
  (protectionRows || []).forEach((row) => {
    protectionBySymbol[row.symbol] = row;
  });

  if (!positions || !positions.length) {
    container.innerHTML = '<div class="empty-state"><strong>No open positions</strong><span>The bot is scanning; no active exposure right now.</span></div>';
    return;
  }

  container.innerHTML = positions
    .map((p) => {
      const protection = protectionBySymbol[p.symbol];
      const borderClass = p.sl === "MISSING" || p.tp === "MISSING" ? "danger-border" : "good-border";
      const direction = (p.direction || "").toLowerCase();
      const pnlClass = Number(p.pnl) >= 0 ? "good" : "danger";
      return `
        <article class="position-card ${borderClass}">
          <header class="position-card-head">
            <div>
              <strong>${escapeHtml(p.symbol)}</strong>
              <span class="direction-pill ${direction === "short" ? "short" : ""}">${escapeHtml(p.direction)}</span>
            </div>
            <div class="position-pnl ${pnlClass}">
              <strong>${escapeHtml(p.pnl)} USDT</strong>
              <span>${escapeHtml(p.pnl_pct)}%</span>
            </div>
          </header>
          <div class="position-lifecycle">
            <div class="lifecycle-step ${p.tp1_hit ? "good" : "pending"}"><span>TP1</span><strong>${p.tp1_hit ? "HIT" : "WAIT"}</strong></div>
            <div class="lifecycle-step ${p.break_even_active ? "good" : "pending"}"><span>BE</span><strong>${p.break_even_active ? "ON" : "OFF"}</strong></div>
            <div class="lifecycle-step ${p.tp2_hit ? "good" : "pending"}"><span>TP2</span><strong>${p.tp2_hit ? "HIT" : "WAIT"}</strong></div>
            <div class="lifecycle-step ${p.tp3_hit ? "good" : "pending"}"><span>TP3</span><strong>${p.tp3_hit ? "HIT" : "WAIT"}</strong></div>
          </div>
          <div class="position-grid">
            <div><span>Entry</span><strong>${escapeHtml(p.entry)}</strong></div>
            <div><span>Live Price</span><strong>${escapeHtml(p.price)}</strong></div>
            <div><span>Size</span><strong>${escapeHtml(p.size)}</strong></div>
            <div><span>Stop Loss</span><strong class="${p.sl === "MISSING" ? "danger" : ""}">${escapeHtml(p.sl)}</strong></div>
            <div><span>Take Profit</span><strong class="${p.tp === "MISSING" ? "danger" : ""}">${escapeHtml(p.tp)}</strong></div>
            <div><span>Protection</span><strong class="${protection ? protection.level : "warning"}">${protection ? escapeHtml(protection.status) : "UNKNOWN"}</strong></div>
            <div><span>Live RR</span><strong>${escapeHtml(p.live_rr)}</strong></div>
            <div><span>Distance to SL</span><strong>${escapeHtml(p.distance_to_sl_pct)}%</strong></div>
            <div><span>Notional</span><strong>${escapeHtml(p.notional)}</strong></div>
          </div>
        </article>`;
    })
    .join("");
}

function renderCandidates(candidates) {
  const container = document.getElementById("candidate-grid");
  if (!container) return;
  if (!candidates || !candidates.length) {
    container.innerHTML = '<div class="empty-state"><strong>No candidates</strong><span>No scanner candidates right now.</span></div>';
    return;
  }
  container.innerHTML = candidates
    .map(
      (c) => `
        <article class="candidate-card ${c.level}">
          <div class="candidate-head"><strong>${escapeHtml(c.symbol)}</strong><span class="badge ${c.level}">${c.level}</span></div>
          <p>${escapeHtml(c.summary)}</p>
        </article>`
    )
    .join("");
}

function renderVolatility(rows) {
  const container = document.getElementById("volatility-list");
  if (!container) return;
  if (!rows || !rows.length) {
    container.innerHTML = "<div><span>No scan data yet</span></div>";
    return;
  }
  container.innerHTML = rows
    .map(
      (row) => `
        <div>
          <span>${escapeHtml(row.symbol)} · ${escapeHtml(row.alignment)}</span>
          <strong class="${row.level}">VR ${escapeHtml(row.volume_ratio)} · VOL ${escapeHtml(row.volatility_rank)}</strong>
          <small>score ${escapeHtml(row.score_hint)} · ${escapeHtml(row.primary_trend)}/${escapeHtml(row.confirmation_trend)}${row.volume_expansion ? " · volume expansion" : ""}</small>
        </div>`
    )
    .join("");
}

function renderProtectionAlerts(alerts) {
  const container = document.getElementById("protection-alerts-list");
  if (!container) return;
  if (!alerts || !alerts.length) {
    container.innerHTML = '<code class="success">No protection alerts</code>';
    return;
  }
  container.innerHTML = alerts.map((alert) => `<code class="${alert.level}">${escapeHtml(alert.message)}</code>`).join("");
}

function renderExecutionFeed(events, containerId, limit) {
  const container = document.getElementById(containerId);
  if (!container) return;
  const rows = (events || []).slice(0, limit || events.length);
  if (!rows.length) {
    container.innerHTML = "<code>No recent activity yet.</code>";
    return;
  }
  container.innerHTML = rows
    .map((event) => `<code class="${event.level}">${escapeHtml(event.timestamp)} · ${escapeHtml(event.summary)}</code>`)
    .join("");
}

function renderStrategyPerformance(rows) {
  const container = document.getElementById("strategy-performance-list");
  if (!container) return;
  if (!rows || !rows.length) {
    container.innerHTML = "<div><span>No strategy performance data yet</span></div>";
    return;
  }
  container.innerHTML = rows
    .map(
      (row) => `
        <div>
          <span>${escapeHtml(row.strategy)}</span>
          <strong class="${row.level}">${escapeHtml(row.status)}</strong>
          <small>closed ${escapeHtml(row.closed_trades)} · win ${escapeHtml(row.winrate)} · exp ${escapeHtml(row.expectancy)} · net ${escapeHtml(row.net_pnl)} · exec rate ${escapeHtml(row.executable_rate)}</small>
        </div>`
    )
    .join("");
}

function renderRiskAndCoach(data) {
  const risk = data.risk || {};
  setText("wallet-equity", data.wallet?.equity);
  setText("wallet-available", data.wallet?.available);
  setText("wallet-used-margin", data.wallet?.used_margin);
  setText("equity-total-pnl", data.equity_curve?.total_pnl);
}

// ----------------------------------------------------------------- tabs --

function activateTab(targetId) {
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === targetId);
  });
  document.querySelectorAll(".tab-link").forEach((button) => {
    button.classList.toggle("active", button.dataset.tabTarget === targetId);
  });
  history.replaceState(null, "", `#${targetId}`);
}

function initTabs() {
  const tabs = Array.from(document.querySelectorAll(".tab-link"));
  tabs.forEach((button) => {
    button.addEventListener("click", () => activateTab(button.dataset.tabTarget));
  });
  const initial = window.location.hash.replace("#", "") || "overview";
  if (document.getElementById(initial)) activateTab(initial);
}

// -------------------------------------------------------- bot control --

async function callBotAction(url, confirmMessage) {
  if (actionInFlight) return;
  if (!window.confirm(confirmMessage)) return;

  actionInFlight = true;
  const startBtn = document.getElementById("btn-start-bot");
  const stopBtn = document.getElementById("btn-stop-bot");
  const messageEl = document.getElementById("bot-control-message");
  if (startBtn) startBtn.disabled = true;
  if (stopBtn) stopBtn.disabled = true;
  if (messageEl) messageEl.textContent = "Working…";

  try {
    const res = await fetch(url, { method: "POST" });
    const result = await res.json();
    if (messageEl) messageEl.textContent = result.message || (result.ok ? "Done." : "Something went wrong.");
  } catch (err) {
    if (messageEl) messageEl.textContent = `Request failed: ${err}`;
  } finally {
    actionInFlight = false;
    await refreshDashboardSnapshot();
  }
}

function initBotControls() {
  const startBtn = document.getElementById("btn-start-bot");
  const stopBtn = document.getElementById("btn-stop-bot");
  if (startBtn) {
    startBtn.addEventListener("click", () =>
      callBotAction("/api/bot/start", "Start the live trading bot now? This enables real order execution.")
    );
  }
  if (stopBtn) {
    stopBtn.addEventListener("click", () =>
      callBotAction("/api/bot/stop", "Stop the trading bot? Open positions stay open on the exchange, but no new scans/entries will happen until it's restarted.")
    );
  }
}

// --------------------------------------------------------- live refresh --

async function refreshDashboardSnapshot() {
  try {
    document.body.classList.add("loading");
    const loadingBar = document.getElementById("loading-bar");
    if (loadingBar) loadingBar.classList.add("active");

    const res = await fetch("/api/data", { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();

    setText("generated-at", `Last updated: ${data.meta?.generated_at || "-"}`);
    renderBotControl(data.bot);
    renderRiskAndCoach(data);
    renderPositions(data.positions, data.position_protection);
    renderCandidates(data.candidate_board);
    renderVolatility(data.volatility_heatmap);
    renderProtectionAlerts(data.protection_alerts);
    renderExecutionFeed(data.execution_timeline, "recent-activity-list", 8);
    renderExecutionFeed(data.execution_timeline, "execution-feed-list");
    renderStrategyPerformance(data.strategy_performance?.strategies);
    renderEquityCurve(data.equity_curve?.points || []);
    renderPnlBars(data.periodic_pnl);

    if (Date.now() - lastReload > SAFETY_RELOAD_MS) {
      window.location.reload();
    }
  } catch (err) {
    console.warn("Dashboard refresh failed", err);
  } finally {
    document.body.classList.remove("loading");
    const loadingBar = document.getElementById("loading-bar");
    if (loadingBar) loadingBar.classList.remove("active");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  initBotControls();
  renderEquityCurve(readInitialJson("initial-equity-points", []));
  setInterval(refreshDashboardSnapshot, POLL_MS);
});
