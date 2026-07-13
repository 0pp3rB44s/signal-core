"""Build persistent learning knowledge from completed trades."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any
import csv

BASE_PATH = Path(__file__).resolve().parents[2]
LOGS_PATH = BASE_PATH / "logs"
DATASET_PATH = LOGS_PATH / "trade_dataset_v2.csv"
TRADE_PLANS_PATH = LOGS_PATH / "trade_plans.csv"
SNAPSHOTS_PATH = LOGS_PATH / "trade_decision_snapshots.csv"
REPORTS_PATH = BASE_PATH / "agents_v2" / "reports"
OUTPUT_PATH = REPORTS_PATH / "learning.json"


def load_trade_rows() -> list[dict[str, Any]]:
    """Load the trade dataset if it exists."""
    if not DATASET_PATH.exists():
        return []

    with DATASET_PATH.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def build_learning(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate historical performance by strategy and symbol."""

    strategies: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "trades": 0,
            "pnl": 0.0,
            "wins": 0,
            "losses": 0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
        }
    )
    symbols: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "trades": 0,
            "pnl": 0.0,
            "wins": 0,
            "losses": 0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
        }
    )

    for row in rows:
        if str(row.get("event_type", "")).upper() != "CLOSE":
            continue

        pnl = row.get("net_pnl") or row.get("pnl") or row.get("realized_pnl")
        try:
            pnl = float(pnl)
        except (TypeError, ValueError):
            continue

        strategy = str(row.get("strategy") or "UNKNOWN")
        symbol = str(row.get("symbol") or "UNKNOWN")

        # Update strategy aggregate
        strat_stats = strategies[strategy]
        strat_stats["trades"] += 1
        strat_stats["pnl"] += pnl
        if pnl > 0:
            strat_stats["wins"] += 1
            strat_stats["gross_profit"] += pnl
        elif pnl < 0:
            strat_stats["losses"] += 1
            strat_stats["gross_loss"] += abs(pnl)

        # Update symbol aggregate
        sym_stats = symbols[symbol]
        sym_stats["trades"] += 1
        sym_stats["pnl"] += pnl
        if pnl > 0:
            sym_stats["wins"] += 1
            sym_stats["gross_profit"] += pnl
        elif pnl < 0:
            sym_stats["losses"] += 1
            sym_stats["gross_loss"] += abs(pnl)

    def finalize(stats):
        trades = stats.get("trades", 0)
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        gross_profit = stats.get("gross_profit", 0.0)
        gross_loss = stats.get("gross_loss", 0.0)
        pnl = stats.get("pnl", 0.0)
        # winrate: percent of trades that are wins
        stats["winrate"] = (wins / trades * 100) if trades else 0.0
        # average_win: gross_profit / wins
        stats["average_win"] = (gross_profit / wins) if wins else None
        # average_loss: gross_loss / losses
        stats["average_loss"] = (gross_loss / losses) if losses else None
        # profit_factor: gross_profit / gross_loss, or None if no losses
        stats["profit_factor"] = (gross_profit / gross_loss) if gross_loss > 0 else None
        # expectancy: pnl / trades
        stats["expectancy"] = (pnl / trades) if trades else None

    closed_trades = sum(v["trades"] for v in strategies.values())

    # Finalize statistics for all strategies and symbols
    for stats in strategies.values():
        finalize(stats)
    for stats in symbols.values():
        finalize(stats)

    return {
        "version": 1,
        "metadata": {
            "dataset_path": str(DATASET_PATH),
            "rows_loaded": len(rows),
            "closed_trades_used": closed_trades,
            "trade_plans_available": TRADE_PLANS_PATH.exists(),
            "decision_snapshots_available": SNAPSHOTS_PATH.exists(),
        },
        "strategies": dict(strategies),
        "symbols": dict(symbols),
    }


def save_learning(report: dict[str, Any]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")


def run() -> dict[str, Any]:
    """Build and persist the learning report."""
    rows = load_trade_rows()
    report = build_learning(rows)
    save_learning(report)
    return report


if __name__ == "__main__":
    report = run()
    print(
        f"Learning report generated: "
        f"{len(report['strategies'])} strategies, "
        f"{len(report['symbols'])} symbols"
    )