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
    "max_open_positions", "max_leverage", "default_leverage",
    "execution_require_confirmation", "position_manager_enabled",
    "position_loop_enabled", "bitget_product_type", "dashboard_host",
    "dashboard_port", "symbol_cooldown_minutes",
)

NAV = [
    ("command", "Command", "/"),
    ("funnel", "Funnel", "/funnel"),
    ("positions", "Positions", "/positions"),
    ("performance", "Performance", "/performance"),
    ("risk", "Risk & Expectancy", "/risk"),
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
    return _page("command.html", "command")


@app.route("/funnel")
@login_required
def funnel():
    return _page("funnel.html", "funnel")


@app.route("/positions")
@login_required
def positions():
    return _page("positions.html", "positions")


@app.route("/performance")
@login_required
def performance():
    return _page("performance.html", "performance")


@app.route("/risk")
@login_required
def risk():
    return _page("risk.html", "risk")


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


if __name__ == "__main__":
    app.run(host=settings.dashboard_host, port=settings.dashboard_port,
            debug=False, threaded=True)
