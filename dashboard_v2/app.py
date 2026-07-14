import logging
import secrets
from datetime import timedelta
from functools import wraps
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from app.config import Settings
from dashboard_v2 import bot_control
from dashboard_v2.data_provider import get_dashboard_data

settings = Settings()

app = Flask(__name__)
app.secret_key = settings.dashboard_secret_key.get_secret_value() or secrets.token_urlsafe(32)
app.permanent_session_lifetime = timedelta(days=7)

logger = logging.getLogger("dashboard")

DASHBOARD_PASSWORD = settings.dashboard_password.get_secret_value()
if not DASHBOARD_PASSWORD:
    raise RuntimeError(
        "DASHBOARD_PASSWORD is required; refusing to start the dashboard without authentication configuration"
    )


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
    # threaded=True: /api/data does several live Bitget API round-trips plus
    # log/CSV parsing, so a single request can take longer than the 5s client
    # poll interval. Without threading, Flask's dev server handles requests
    # one at a time and polling clients back up behind each other.
    app.run(
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        debug=settings.dashboard_debug,
        threaded=True,
    )
