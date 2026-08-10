#!/usr/bin/env python3
"""Evaluate the frozen dynamic_grid_v1 shadow promotion gate."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys


def evaluate(path: Path, *, min_cycles: int = 48, min_hours: float = 4.0) -> dict:
    rows = []
    malformed = 0
    if not path.exists():
        return {"verdict": "FAIL", "reason": "events_file_missing"}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            malformed += 1
            continue
        if row.get("strategy") == "dynamic_grid_v1" and row.get("mode") == "SHADOW":
            rows.append(row)
    selections = [row for row in rows if row.get("event_type") == "GRID_SELECTION"]
    decisions = [row for row in rows if row.get("event_type") == "GRID_DECISION"]
    fees = [row for row in rows if row.get("event_type") == "FEE_RATE_AUTHENTICATED"]
    hypothetical_fills = [row for row in rows if row.get("event_type") == "SHADOW_LEVEL_FILLED"]
    hypothetical_tps = [row for row in rows if row.get("event_type") == "SHADOW_TP_HIT"]
    errors = [
        row for row in rows
        if row.get("event_type") in {"GRID_STOP", "GRID_ORDER_ERROR"}
    ]
    timestamps = []
    for row in selections:
        try:
            timestamps.append(datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00")))
        except (KeyError, TypeError, ValueError):
            malformed += 1
    span_hours = (
        (max(timestamps) - min(timestamps)).total_seconds() / 3600.0
        if len(timestamps) >= 2 else 0.0
    )
    symbols = {str(row.get("symbol") or "") for row in decisions}
    fee_symbols = {str(row.get("symbol") or "") for row in fees}
    invalid_decisions = [
        row for row in decisions
        if row.get("regime") == "GRID_ALLOWED"
        and (
            len(row.get("levels") or []) != 3
            or float((row.get("economics") or {}).get("expected_net_capture_bps") or 0.0) <= 0
        )
    ]
    checks = {
        "cycles": len(selections) >= min_cycles,
        "duration": span_hours >= min_hours,
        "decision_symbols": symbols == {"BTCUSDT", "SOLUSDT"},
        "authenticated_fee_symbols": fee_symbols == {"BTCUSDT", "SOLUSDT"},
        "allowed_opportunity_observed": any(
            row.get("regime") == "GRID_ALLOWED" for row in decisions
        ),
        "no_runtime_errors": not errors,
        "well_formed": malformed == 0 and not invalid_decisions,
        "hypothetical_fill_mapping_well_formed": all(
            row.get("level") in {1, 2, 3}
            and float(row.get("entry_price") or 0.0) > 0
            and float(row.get("target_price") or 0.0) > float(row.get("entry_price") or 0.0)
            and float(row.get("expected_net_capture_bps") or 0.0) > 0
            for row in hypothetical_fills
        ),
    }
    return {
        "verdict": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "selection_cycles": len(selections),
        "span_hours": round(span_hours, 4),
        "symbols": sorted(symbols),
        "fee_symbols": sorted(fee_symbols),
        "error_count": len(errors),
        "malformed_count": malformed,
        "invalid_decision_count": len(invalid_decisions),
        "hypothetical_fill_count": len(hypothetical_fills),
        "hypothetical_tp_count": len(hypothetical_tps),
    }


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: evaluate_dynamic_grid_shadow.py <events.jsonl>", file=sys.stderr)
        return 2
    result = evaluate(Path(sys.argv[1]))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("verdict") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
