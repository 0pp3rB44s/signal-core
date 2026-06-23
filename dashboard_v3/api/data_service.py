import csv
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DAILY_LEARNING_REPORT = PROJECT_ROOT / "reports" / "backtests" / "daily_learning_report.json"
STRATEGY_EXPECTANCY = PROJECT_ROOT / "reports" / "backtests" / "strategy_expectancy.json"
TRADE_DATASET = PROJECT_ROOT / "logs" / "trade_dataset_v2.csv"


def _safe_json(path):
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_rows(path, limit=5000):
    if not path.exists():
        return []

    rows = []
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
                if len(rows) >= limit:
                    break
    except Exception:
        return []

    return rows


def get_overview():
    daily = _safe_json(DAILY_LEARNING_REPORT)
    expectancy = _safe_json(STRATEGY_EXPECTANCY)
    rows = _read_rows(TRADE_DATASET)

    warnings = []
    data_confidence = daily.get("data_confidence_verdict") or "UNKNOWN"

    if str(data_confidence).upper() in {"LOW_CONFIDENCE", "LOW", "UNKNOWN"}:
        warnings.append("Data confidence is not high. Treat performance metrics carefully.")

    if not TRADE_DATASET.exists() or len(rows) == 0:
        warnings.append("Trade dataset missing or empty.")

    return {
        "dashboard": "v3",
        "status": "online",
        "source_health": {
            "daily_learning_report": DAILY_LEARNING_REPORT.exists(),
            "strategy_expectancy": STRATEGY_EXPECTANCY.exists(),
            "trade_dataset": TRADE_DATASET.exists() and len(rows) > 0,
        },
        "metrics": {
            "dataset_rows": daily.get("dataset_rows") or daily.get("trade_dataset_rows") or len(rows),
            "closed_trades": daily.get("closed_trades") or daily.get("closed_trade_count") or "—",
            "winrate_pct": daily.get("winrate_pct") or daily.get("winrate") or "—",
            "profit_factor": daily.get("profit_factor") or "—",
            "net_pnl": daily.get("net_pnl") or daily.get("total_pnl") or "—",
            "data_confidence": data_confidence,
            "strategy_count": len(expectancy),
        },
        "warnings": warnings,
    }