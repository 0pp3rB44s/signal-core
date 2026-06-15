from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_PATH = Path(__file__).resolve().parents[1]

LOGS_PATH = BASE_PATH / "logs"
STATE_PATH = BASE_PATH / "state"
DATA_STORE = BASE_PATH / "data_store"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except Exception:
        return []


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    tmp.replace(path)


# Helper: normalize any record payload into list-of-dicts for dataset summary

def _as_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "trades", "events", "positions"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return [payload]
    return []


# Helper: Compute trade dataset stats
def _trade_dataset_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    close_rows = [row for row in rows if str(row.get("event_type") or "").upper() == "CLOSE"]
    open_rows = [row for row in rows if str(row.get("event_type") or "").upper() == "OPEN"]
    real_close_rows = [
        row for row in close_rows
        if str(row.get("symbol") or "").upper() != "TESTUSDT"
        and str(row.get("data_confidence") or "").upper() != "TEST_ONLY"
    ]
    symbols = sorted({str(row.get("symbol") or "").upper() for row in real_close_rows if row.get("symbol")})
    return {
        "trade_dataset_rows_total": len(rows),
        "trade_dataset_open_rows": len(open_rows),
        "trade_dataset_close_rows": len(close_rows),
        "trade_dataset_real_close_rows": len(real_close_rows),
        "trade_dataset_real_symbols": symbols,
    }


def build_dataset() -> dict[str, Any]:
    DATA_STORE.mkdir(exist_ok=True)

    market_context = _read_csv(LOGS_PATH / "market_context.csv")
    trade_dataset = _read_csv(LOGS_PATH / "trade_dataset_v2.csv")
    decision_snapshots = _read_csv(LOGS_PATH / "trade_decision_snapshots.csv")

    executed_trades = _read_json(STATE_PATH / "executed_trades.json", [])
    execution_events = _read_json(STATE_PATH / "execution_events.json", [])
    position_events = _read_json(STATE_PATH / "position_events.json", [])

    executed_trade_records = _as_records(executed_trades)
    execution_event_records = _as_records(execution_events)
    position_event_records = _as_records(position_events)
    trade_stats = _trade_dataset_stats(trade_dataset)

    payload = {
        "generated_at": _now(),
        "source_files": {
            "market_context": str(LOGS_PATH / "market_context.csv"),
            "trade_dataset": str(LOGS_PATH / "trade_dataset_v2.csv"),
            "decision_snapshots": str(LOGS_PATH / "trade_decision_snapshots.csv"),
            "executed_trades": str(STATE_PATH / "executed_trades.json"),
            "execution_events": str(STATE_PATH / "execution_events.json"),
            "position_events": str(STATE_PATH / "position_events.json"),
        },
        "counts": {
            "market_context_rows": len(market_context),
            "trade_dataset_rows": len(trade_dataset),
            "trade_dataset_open_rows": trade_stats["trade_dataset_open_rows"],
            "trade_dataset_close_rows": trade_stats["trade_dataset_close_rows"],
            "trade_dataset_real_close_rows": trade_stats["trade_dataset_real_close_rows"],
            "decision_snapshot_rows": len(decision_snapshots),
            "executed_trades": len(executed_trade_records),
            "execution_events": len(execution_event_records),
            "position_events": len(position_event_records),
        },
        "data": {
            "market_context": market_context[-5000:],
            "trade_dataset": trade_dataset[-5000:],
            "decision_snapshots": decision_snapshots[-5000:],
            "executed_trades": executed_trade_records[-5000:],
            "execution_events": execution_event_records[-5000:],
            "position_events": position_event_records[-5000:],
        },
    }

    _write_json(DATA_STORE / "exports" / "latest_dataset_bundle.json", payload)
    _write_json(DATA_STORE / "trades" / "latest_trades.json", payload["data"]["trade_dataset"])
    _write_json(DATA_STORE / "trades" / "latest_real_closed_trades.json", [row for row in payload["data"]["trade_dataset"] if str(row.get("event_type") or "").upper() == "CLOSE" and str(row.get("symbol") or "").upper() != "TESTUSDT" and str(row.get("data_confidence") or "").upper() != "TEST_ONLY"])
    _write_json(DATA_STORE / "decisions" / "latest_decisions.json", payload["data"]["decision_snapshots"])
    _write_json(DATA_STORE / "raw" / "latest_market_context.json", payload["data"]["market_context"])

    summary = {
        "generated_at": payload["generated_at"],
        "counts": payload["counts"],
        "trade_stats": trade_stats,
        "verdict": "OK" if payload["counts"]["market_context_rows"] > 0 else "NO_MARKET_CONTEXT",
    }
    _write_json(DATA_STORE / "backtests" / "dataset_summary.json", summary)

    return summary


if __name__ == "__main__":
    result = build_dataset()
    print(json.dumps(result, indent=2))