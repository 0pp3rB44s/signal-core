from flask import Flask, render_template, jsonify
from dashboard_v2.data_provider import get_dashboard_data
from collections import Counter
from pathlib import Path
import os
from dotenv import load_dotenv


load_dotenv()

app = Flask(__name__)


LOG_PATH = Path("logs/agent.log")


def build_optimization_advice():
    advice = {
        "market_regime": "UNKNOWN",
        "strategies_to_watch": [],
        "strategies_to_boost": [],
        "symbols_to_pause": [],
        "stats": {},
    }

    if not LOG_PATH.exists():
        return advice

    try:
        lines = LOG_PATH.read_text(errors="ignore").splitlines()[-1200:]
    except Exception:
        return advice

    continuation_rejects = 0
    momentum_mentions = 0
    conflicted_count = 0

    symbol_rejections = Counter()
    strategy_rejections = Counter()

    for line in lines:
        if "CONTINUATION_REJECT" in line:
            continuation_rejects += 1

            parts = [part.strip() for part in line.split("|")]
            symbol = ""

            if "CONTINUATION_REJECT" in parts:
                idx = parts.index("CONTINUATION_REJECT")
                if len(parts) > idx + 1:
                    symbol = parts[idx + 1].strip()
            elif len(parts) >= 5:
                symbol = parts[4].strip()

            if symbol and symbol not in {"CONTINUATION_REJECT", "NO_SETUP", "REJECTED_SETUP"}:
                symbol_rejections[symbol] += 1

        if "MOMENTUM_BREAKOUT" in line or "volume expansion" in line:
            momentum_mentions += 1

        if "alignment=conflicted" in line or "alignment=mixed" in line:
            conflicted_count += 1

        if "trend_continuation" in line and "REJECTED_SETUP" in line:
            strategy_rejections["trend_continuation"] += 1

    if conflicted_count >= 25:
        advice["market_regime"] = "CHOPPY / CONFLICTED"
    elif momentum_mentions > continuation_rejects:
        advice["market_regime"] = "TRENDING / EXPANSION"
    else:
        advice["market_regime"] = "MIXED"

    if continuation_rejects >= 10:
        advice["strategies_to_watch"].append({
            "strategy": "trend_continuation",
            "status": "WATCH",
            "reason": "high rejection rate / weak continuation quality",
        })

    if momentum_mentions >= 10:
        advice["strategies_to_boost"].append({
            "strategy": "momentum_breakout",
            "status": "BOOST",
            "reason": "strong expansion + volume confirmation",
        })

    for symbol, count in symbol_rejections.most_common(5):
        if count >= 3:
            advice["symbols_to_pause"].append({
                "symbol": symbol,
                "reason": f"{count} recent continuation rejects / conflicted structure",
            })

    advice["stats"] = {
        "continuation_rejects": continuation_rejects,
        "momentum_mentions": momentum_mentions,
        "conflicted_signals": conflicted_count,
    }

    return advice


@app.route("/")
def index():
    data = get_dashboard_data()
    data["roadmap_focus"] = "P3.9 Sweep Evolution Engine"
    data["optimization_advice"] = build_optimization_advice()
    return render_template("index.html", data=data)


@app.route("/api/data")
def api_data():
    data = get_dashboard_data()
    data["roadmap_focus"] = "P3.9 Sweep Evolution Engine"
    data["optimization_advice"] = build_optimization_advice()
    return jsonify(data)


if __name__ == "__main__":
    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.getenv("DASHBOARD_PORT", "8501"))
    debug = os.getenv("DASHBOARD_DEBUG", "false").strip().lower() in {"1", "true", "yes", "on"}
    app.run(host=host, port=port, debug=debug)
