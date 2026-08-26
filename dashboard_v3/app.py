"""TERMINAL — read-only operations console for the Bitget engine.

Security posture:
  * password-gated, fails closed when DASHBOARD_PASSWORD is unset;
  * binds to DASHBOARD_HOST (127.0.0.1 by default) — no public listener;
  * NO control endpoints. There is no start, stop, order or position mutation
    surface in this application, by construction. A guard test enforces it.
  * never renders secrets; the settings view exposes an allow-listed subset only.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any

from flask import (Flask, jsonify, redirect, render_template, request, session, url_for)

from app.config import Settings
from dashboard_v3.core import assembly
from dashboard_v3.core.status import GLYPH, TONE, Status

settings = Settings()
log = logging.getLogger("dashboard_v3")

app = Flask(__name__)
app.secret_key = settings.dashboard_secret_key.get_secret_value() or secrets.token_urlsafe(32)
app.permanent_session_lifetime = timedelta(days=7)

DASHBOARD_PASSWORD = settings.dashboard_password.get_secret_value()
if not DASHBOARD_PASSWORD:
    raise RuntimeError(
        "DASHBOARD_PASSWORD is required; refusing to start without authentication."
    )

#: Config keys safe to render. Anything not listed is never exposed, so a new
#: secret-bearing setting cannot leak by being added to Settings later.
SAFE_SETTINGS = (
    "execution_mode", "execution_enabled", "forward_paper_only", "max_symbols",
    "production_symbol_allowlist",
    "max_open_positions", "max_leverage", "default_leverage",
    "execution_require_confirmation", "position_manager_enabled",
    "position_loop_enabled", "bitget_product_type", "dashboard_host",
    "dashboard_port", "symbol_cooldown_minutes",
)

NAV = [
    ("command", "Command", "/"),
    ("adaptive_trend", "AdaptiveTrend", "/adaptive-trend"),
    ("signals", "Signals", "/signals"),
    ("operations", "System", "/operations"),
    ("funnel", "Funnel", "/funnel"),
    ("strategy", "Strategies", "/strategy"),
    ("positions", "Positions", "/positions"),
    ("performance", "Performance", "/performance"),
    ("risk", "RiskManager", "/risk"),
    ("collectors", "Collectors", "/collectors"),
    ("logs", "Logs", "/logs"),
    ("project", "Project", "/project"),
    ("incidents", "Incidents", "/incidents"),
    ("health", "Data Health", "/health"),
]


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


# --- template helpers ----------------------------------------------------

@app.context_processor
def _helpers() -> dict[str, Any]:
    import os
    def tone(status: Any) -> str:
        return TONE.get(status, "unknown") if isinstance(status, Status) else "unknown"

    def glyph(status: Any) -> str:
        return GLYPH.get(status, "?") if isinstance(status, Status) else "?"

    def label(status: Any) -> str:
        return status.value if isinstance(status, Status) else "UNKNOWN"

    def num(value: Any, digits: int = 2, dash: str = "UNKNOWN") -> str:
        """nl-NL number formatting: thousands '.', decimal ','."""
        if value is None or value == "":
            return dash
        try:
            text = f"{float(value):,.{digits}f}"
        except (TypeError, ValueError):
            return str(value)
        return text.replace(",", " ").replace(".", ",").replace(" ", ".")

    def pct(value: Any, digits: int = 1, dash: str = "UNKNOWN") -> str:
        if value is None:
            return dash
        try:
            return num(float(value) * 100, digits) + "%"
        except (TypeError, ValueError):
            return dash

    def pct_raw(value: Any, digits: int = 1, dash: str = "UNKNOWN") -> str:
        if value is None:
            return dash
        return num(value, digits) + "%"

    def money(value: Any, coin: str = "USDT", digits: int = 2) -> str:
        """Never converts currency — renders the unit present in the source."""
        if value is None:
            return "UNKNOWN"
        return f"{num(value, digits)} {coin}"

    def dt_utc(value: Any) -> str:
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).strftime("%d-%m-%Y %H:%M:%S") + "Z"
        return str(value or "UNKNOWN")

    def dt_local(value: Any) -> str:
        if isinstance(value, datetime):
            return value.astimezone().strftime("%d-%m-%Y %H:%M:%S")
        return str(value or "UNKNOWN")

    def ago(seconds: Any) -> str:
        if seconds is None:
            return "UNKNOWN"
        seconds = float(seconds)
        if seconds < 90:
            return f"{int(seconds)}s"
        if seconds < 5400:
            return f"{seconds / 60:.0f}m"
        if seconds < 172800:
            return f"{seconds / 3600:.1f}u"
        return f"{seconds / 86400:.1f}d"

    return {
        "tone": tone, "glyph": glyph, "status_label": label,
        "num": num, "pct": pct, "pct_raw": pct_raw, "money": money,
        "dt_utc": dt_utc, "dt_local": dt_local, "ago": ago,
        "NAV": NAV, "Status": Status,
        "REFRESH_SECONDS": min(300, max(2, int(os.environ.get("DASHBOARD_REFRESH_SECONDS", "5")))),
    }


# --- auth ---------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("password", ""), DASHBOARD_PASSWORD):
            session.clear()
            session["authenticated"] = True
            session.permanent = True
            nxt = request.args.get("next") or url_for("command")
            # Only allow relative redirects — no open redirect.
            if not nxt.startswith("/"):
                nxt = url_for("command")
            return redirect(nxt)
        error = "Onjuist wachtwoord."
        log.warning("dashboard login failed from %s", request.remote_addr)
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- pages --------------------------------------------------------------

def _page(template: str, active: str, **extra):
    data = assembly.build_all()
    return render_template(template, active=active, d=data, **extra)


@app.route("/")
@login_required
def command():
    from dashboard_v3.panels import adaptive_trend as at
    return _page("command.html", "command",
                 adaptive_trend=assembly.cached("adaptive_trend", at.build, ttl=30.0))


@app.route("/adaptive-trend")
@login_required
def adaptive_trend():
    """The primary strategy page: what the only entry-enabled strategy sees now."""
    from dashboard_v3.panels import adaptive_trend as at
    return _page("adaptive_trend.html", "adaptive_trend", at=assembly.cached("adaptive_trend", at.build, ttl=30.0))


@app.route("/signals")
@login_required
def signals_page():
    """Chronological AdaptiveTrend decision history, with filters."""
    from dashboard_v3.panels import adaptive_trend as at
    window = request.args.get("window", "7d")
    symbol = request.args.get("symbol", "")
    decision = request.args.get("decision", "")
    rows = at.timeline()
    now = datetime.now(timezone.utc)
    spans = {"today": None, "24h": timedelta(hours=24), "7d": timedelta(days=7), "all": None}
    if window == "today":
        rows = [r for r in rows if r.timestamp and r.timestamp.date() == now.date()]
    elif spans.get(window):
        rows = [r for r in rows if r.timestamp and (now - r.timestamp) <= spans[window]]
    if symbol:
        rows = [r for r in rows if r.symbol == symbol]
    if decision:
        rows = [r for r in rows if (r.decision or "UNKNOWN") == decision]
    all_rows = at.timeline()
    return _page(
        "signals.html", "signals", rows=rows,
        window=window, symbol=symbol, decision=decision,
        symbols=sorted({r.symbol for r in all_rows if r.symbol}),
        decisions=sorted({(r.decision or "UNKNOWN") for r in all_rows}),
        total=len(all_rows),
    )


@app.route("/funnel")
@login_required
def funnel():
    return _page("funnel.html", "funnel")


@app.route("/operations")
@login_required
def operations():
    return _page("operations.html", "operations")


@app.route("/positions")
@login_required
def positions():
    return _page("positions.html", "positions")


@app.route("/performance")
@login_required
def performance():
    from dashboard_v3.core import sources as src
    from dashboard_v3.panels import adaptive_trend as at
    from dashboard_v3.panels import performance_eras as eras

    def _build():
        rows = src.load_csv("logs/trade_dataset_v2.csv", limit=None).value or []
        rotated = src.load_csv("logs/trade_dataset_v2.csv.1", limit=None).value or []
        return eras.compute_all(list(rotated) + list(rows), at.timeline())

    return _page("performance.html", "performance",
                 eras=assembly.cached("performance_eras", _build, ttl=120.0))


@app.route("/strategy")
@login_required
def strategy():
    return _page("strategy.html", "strategy")


@app.route("/risk")
@login_required
def risk():
    return _page("risk.html", "risk")


@app.route("/collectors")
@login_required
def collectors():
    return _page("collectors.html", "collectors")


@app.route("/logs")
@login_required
def logs():
    return _page("logs.html", "logs")


@app.route("/project")
@login_required
def project():
    return _page("project.html", "project")


@app.route("/incidents")
@login_required
def incidents():
    return _page("incidents.html", "incidents")


@app.route("/health")
@login_required
def health():
    safe = {k: getattr(settings, k, None) for k in SAFE_SETTINGS}
    return _page("health.html", "health", safe_settings=safe)


# --- read-only JSON -----------------------------------------------------

@app.route("/api/status")
@login_required
def api_status():
    """Compact status payload for the header poller."""
    data = assembly.build_all()
    rt = data["panels"]["runtime"]
    return jsonify({
        "overall": data["overall"].value,
        "permission": data["permission"]["status"].value,
        "engine_alive": bool((rt.get("engine") or {}).get("alive")),
        "heartbeat_age": (rt.get("heartbeat_prov").age_seconds
                          if rt.get("heartbeat_prov") else None),
        "positions": data["panels"]["exchange"].get("position_count"),
        "unprotected": data["panels"]["exchange"].get("unprotected_count"),
        "generated_at": data["generated_at"].isoformat(),
    })


@app.errorhandler(404)
def _404(_e):
    return render_template("error.html", code=404,
                           message="Deze pagina bestaat niet."), 404


@app.errorhandler(500)
def _500(_e):
    log.exception("unhandled dashboard error")
    return render_template("error.html", code=500,
                           message="Interne fout. Andere panelen blijven werken."), 500


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def resolve_bind_host(configured: str, allow_public: bool) -> tuple[str, str]:
    """Return (host, note). Refuses a non-loopback bind unless opted in.

    The ambient config carries DASHBOARD_HOST=0.0.0.0, which would publish this
    console to every interface on the LAN over plain HTTP with only a form
    password in front. Binding is therefore forced to loopback unless the
    operator explicitly sets DASHBOARD_ALLOW_PUBLIC_BIND=true, and even then a
    warning is emitted. Tunnel with SSH rather than opening the port.
    """
    configured = (configured or "").strip() or "127.0.0.1"
    if configured in LOOPBACK:
        return configured, ""
    if allow_public:
        return configured, (
            f"WARNING: binding to {configured} — this console is reachable from "
            "the network over plain HTTP. Terminate TLS and authenticate in front of it."
        )
    return "127.0.0.1", (
        f"NOTE: DASHBOARD_HOST={configured} ignored; bound to 127.0.0.1. "
        "Set DASHBOARD_ALLOW_PUBLIC_BIND=true to override, or tunnel: "
        "ssh -N -L 8501:127.0.0.1:8501 <host>"
    )


if __name__ == "__main__":
    import os

    allow_public = os.environ.get("DASHBOARD_ALLOW_PUBLIC_BIND", "").lower() == "true"
    host, note = resolve_bind_host(settings.dashboard_host, allow_public)
    if note:
        print(note, flush=True)
    print(f"dashboard_v3 on http://{host}:{settings.dashboard_port}", flush=True)
    app.run(host=host, port=settings.dashboard_port, debug=False, threaded=True)
