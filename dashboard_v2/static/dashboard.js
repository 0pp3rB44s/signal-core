
const POLL_MS = 5000;
const HARD_REFRESH_MS = 300000;
let lastHardRefresh = Date.now();

function readInitialJson(id, fallback) {
  const el = document.getElementById(id);
  if (!el) return fallback;

  try {
    return JSON.parse(el.textContent || "");
  } catch {
    return fallback;
  }
}

window.__INITIAL_EQUITY_POINTS__ = readInitialJson("initial-equity-points", []);
window.__INITIAL_PERIODIC_PNL__ = readInitialJson("initial-periodic-pnl", {});

function setText(id, value, fallback = "-") {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = value ?? fallback;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

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
    ["pnl-bar-day", Number(periodicPnl.daily || 0)],
    ["pnl-bar-week", Number(periodicPnl.weekly || 0)],
    ["pnl-bar-month", Number(periodicPnl.monthly || 0)],
  ];

  const maxAbs = Math.max(...values.map(([, value]) => Math.abs(value)), 1);

  values.forEach(([id, value]) => {
    const label = document.getElementById(id);
    if (!label) return;

    const bar = label.parentElement?.querySelector("strong");
    if (!bar) return;

    label.textContent = value.toFixed(4);
    bar.style.height = `${clamp((Math.abs(value) / maxAbs) * 100, 12, 100)}%`;
  });
}

function activateTab(targetId) {
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === targetId);
  });

  document.querySelectorAll(".tab-link").forEach((button) => {
    const isActive = button.dataset.tabTarget === targetId;
    button.classList.toggle("active", isActive);
    button.setAttribute("aria-selected", isActive ? "true" : "false");
    button.setAttribute("tabindex", isActive ? "0" : "-1");
  });

  document.body.dataset.activeTab = targetId;

  const overviewExtra = document.querySelector("[data-overview-extra]");

  if (overviewExtra) {
    overviewExtra.classList.toggle("active", targetId === "overview" || targetId === "open-trades");
  }

  history.replaceState(null, "", `#${targetId}`);
}

function initTabs() {
  const tabs = Array.from(document.querySelectorAll(".tab-link"));

  tabs.forEach((button, index) => {
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", button.classList.contains("active") ? "true" : "false");
    button.setAttribute("tabindex", button.classList.contains("active") ? "0" : "-1");

    button.addEventListener("click", () => activateTab(button.dataset.tabTarget));

    button.addEventListener("keydown", (event) => {
      if (!["ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;

      event.preventDefault();

      let nextIndex = index;

      if (event.key === "ArrowDown" || event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
      if (event.key === "ArrowUp" || event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
      if (event.key === "Home") nextIndex = 0;
      if (event.key === "End") nextIndex = tabs.length - 1;

      const nextTab = tabs[nextIndex];
      nextTab.focus();
      activateTab(nextTab.dataset.tabTarget);
    });
  });

  const initial = window.location.hash.replace("#", "") || "overview";

  if (document.getElementById(initial)) {
    activateTab(initial);
  }
}

async function refreshDashboardSnapshot() {
  try {
    document.body.classList.add("loading");

    const loadingBar = document.getElementById("dashboard-loading-bar");
    if (loadingBar) {
      loadingBar.classList.add("active");
    }

    const res = await fetch("/api/data", { cache: "no-store" });

    if (!res.ok) return;

    const data = await res.json();
    const rejectPanel = document.getElementById('reject-analytics-panel');
    if (rejectPanel && data?.intelligence?.rejections) {
      const total = data.intelligence.rejections.total || 0;
      rejectPanel.dataset.totalRejects = total;
    }

    const runtimeStatus = document.getElementById("runtime-status");
    if (runtimeStatus) {
      const isOnline = data?.bot?.status === "RUNNING";
      runtimeStatus.textContent = isOnline ? "ONLINE" : "CHECK";
      runtimeStatus.classList.toggle("good", isOnline);
      runtimeStatus.classList.toggle("danger", !isOnline);
    }

    setText("wallet-balance", data?.wallet?.balance);
    setText("wallet-equity", data?.wallet?.equity);
    setText("wallet-available", data?.wallet?.available);
    setText("wallet-used-margin", data?.wallet?.used_margin);

    renderEquityCurve(data?.equity_curve?.points || []);
    renderPnlBars(data?.periodic_pnl);

    if (Date.now() - lastHardRefresh > HARD_REFRESH_MS) {
      window.location.reload();
    }
  } catch (err) {
    console.warn("Dashboard refresh failed", err);
  } finally {
    document.body.classList.remove("loading");

    const loadingBar = document.getElementById("dashboard-loading-bar");
    if (loadingBar) {
      loadingBar.classList.remove("active");
    }
  }
}

document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  renderEquityCurve(window.__INITIAL_EQUITY_POINTS__ || []);
  renderPnlBars(window.__INITIAL_PERIODIC_PNL__ || {});
  refreshDashboardSnapshot();
  setInterval(refreshDashboardSnapshot, POLL_MS);
});