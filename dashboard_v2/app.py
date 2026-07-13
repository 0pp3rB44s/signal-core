import logging
import os
import secrets
from datetime import timedelta
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from dashboard_v2 import bot_control
from dashboard_v2.data_provider import get_dashboard_data

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("DASHBOARD_SECRET_KEY") or secrets.token_urlsafe(32)
app.permanent_session_lifetime = timedelta(days=7)

logger = logging.getLogger("dashboard")

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
if not DASHBOARD_PASSWORD:
    DASHBOARD_PASSWORD = secrets.token_urlsafe(18)
    logger.warning(
        "DASHBOARD_PASSWORD not set in .env -- generated a one-time password for "
        "this run: %s (set DASHBOARD_PASSWORD in .env to keep it stable across restarts)",
        DASHBOARD_PASSWORD,
    )
    print(f"[dashboard] No DASHBOARD_PASSWORD set. One-time password: {DASHBOARD_PASSWORD}")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        submitted = request.form.get("password", "")
        if secrets.compare_digest(submitted, DASHBOARD_PASSWORD):
            session.clear()
            session["authenticated"] = True
            session.permanent = True
            next_url = request.args.get("next") or url_for("index")
            return redirect(next_url)
        error = "Incorrect password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    data = get_dashboard_data()
    return render_template("index.html", data=data)


@app.route("/api/data")
@login_required
def api_data():
    return jsonify(get_dashboard_data())


@app.route("/api/bot/start", methods=["POST"])
@login_required
def api_bot_start():
    result = bot_control.start_bot(reason="dashboard_start")
    return jsonify(result)


@app.route("/api/bot/stop", methods=["POST"])
@login_required
def api_bot_stop():
    result = bot_control.stop_bot(reason="dashboard_stop")
    return jsonify(result)


if __name__ == "__main__":
    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.getenv("DASHBOARD_PORT", "8501"))
    debug = os.getenv("DASHBOARD_DEBUG", "false").strip().lower() in {"1", "true", "yes", "on"}
    # threaded=True: /api/data does several live Bitget API round-trips plus
    # log/CSV parsing, so a single request can take longer than the 5s client
    # poll interval. Without threading, Flask's dev server handles requests
    # one at a time and polling clients back up behind each other.
    app.run(host=host, port=port, debug=debug, threaded=True)
