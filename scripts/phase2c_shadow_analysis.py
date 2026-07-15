from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> Any:
    return json.loads(path.read_text())


def _key(row: dict[str, Any]) -> tuple[str, str, int]:
    return row["strategy"], row["symbol"], int(row["signal_timestamp"])


def performance(rows: list[dict[str, Any]]) -> dict[str, float | int]:
    closed = [row for row in rows if row.get("fill_status") == "FILLED" and row.get("final_exit_reason") not in {"", "OPEN_AT_DATA_END"}]
    pnl = [float(row["net_pnl"]) for row in closed]
    wins = sum(value for value in pnl if value > 0)
    losses = -sum(value for value in pnl if value < 0)
    equity = peak = 1000.0
    drawdown = 0.0
    for value in pnl:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": len(closed),
        "net_pnl": sum(pnl),
        "profit_factor": wins / losses if losses else 0.0,
        "expectancy": sum(pnl) / len(pnl) if pnl else 0.0,
        "max_drawdown": drawdown,
    }


def build_shadow_rows(production: Path, structural: Path, proxy: Path) -> list[dict[str, Any]]:
    modes = {
        "production": _load(production / "risk_gate_decisions.json"),
        "structural": _load(structural / "risk_gate_decisions.json"),
        "proxy": _load(proxy / "risk_gate_decisions.json"),
    }
    summaries = {
        "structural": {row["strategy"]: row for row in _load(structural / "strategy_summary.json")},
        "proxy": {row["strategy"]: row for row in _load(proxy / "strategy_summary.json")},
    }
    production_summaries = _load(production / "strategy_summary.json")
    strategies = sorted(row["strategy"] for row in production_summaries)
    rows = []
    for strategy in strategies:
        rows.append({
            "strategy": strategy,
            "selected_candidates": sum(row["strategy"] == strategy for row in modes["production"]),
            "production_accepted": sum(row["strategy"] == strategy and row["allowed"] for row in modes["production"]),
            "structural_accepted": sum(row["strategy"] == strategy and row["allowed"] for row in modes["structural"]),
            "proxy_accepted": sum(row["strategy"] == strategy and row["allowed"] for row in modes["proxy"]),
            "structural_trades": int(summaries["structural"][strategy]["closed_trades"]),
            "proxy_trades": int(summaries["proxy"][strategy]["closed_trades"]),
        })
    return rows


def build_gate_value_rows(structural: Path, proxy: Path) -> list[dict[str, Any]]:
    structural_records = _load_csv(structural / "trade_level.csv")
    record_by_key = {_key(row): row for row in structural_records}
    decisions = _load(proxy / "risk_gate_decisions.json")
    proxy_reasons = sorted({reason for row in decisions for reason in row["reasons"] if reason.startswith("proxy blocked:")})
    allowed_keys = {_key(row) for row in decisions if row["allowed"]}
    allowed_performance = performance([record_by_key[key] for key in allowed_keys if key in record_by_key])
    rows = []
    for reason in proxy_reasons:
        blocked_keys = {_key(row) for row in decisions if reason in row["reasons"]}
        blocked_performance = performance([record_by_key[key] for key in blocked_keys if key in record_by_key])
        combined = performance([
            record_by_key[key] for key in allowed_keys | blocked_keys if key in record_by_key
        ])
        rows.append({
            "condition": reason,
            "candidates_blocked": len(blocked_keys),
            "allowed_trades": allowed_performance["trades"],
            "allowed_net_pnl": allowed_performance["net_pnl"],
            "blocked_trades": blocked_performance["trades"],
            "hypothetical_blocked_net_pnl": blocked_performance["net_pnl"],
            "allowed_profit_factor": allowed_performance["profit_factor"],
            "combined_profit_factor_without_condition": combined["profit_factor"],
            "allowed_expectancy": allowed_performance["expectancy"],
            "combined_expectancy_without_condition": combined["expectancy"],
            "allowed_max_drawdown": allowed_performance["max_drawdown"],
            "combined_max_drawdown_without_condition": combined["max_drawdown"],
        })
    return rows


def _load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--production", type=Path, required=True)
    parser.add_argument("--structural", type=Path, required=True)
    parser.add_argument("--proxy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    shadow = build_shadow_rows(args.production, args.structural, args.proxy)
    gate_value = build_gate_value_rows(args.structural, args.proxy)
    payload = {"shadow_comparison": shadow, "gate_value": gate_value}
    payload["analysis_hash"] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    (args.output / "phase2c_comparison.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    _write_csv(args.output / "shadow_comparison.csv", shadow)
    _write_csv(args.output / "gate_value.csv", gate_value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
