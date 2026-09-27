

"""Performance audit metrics for the Audit Engine."""

from __future__ import annotations

from collections import Counter
from typing import Any


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


# --- Added for improved PnL extraction and test row filtering ---
def _is_test_row(row: dict[str, Any]) -> bool:
    symbol = str(row.get("symbol", "")).upper()
    strategy = str(row.get("strategy", "")).lower()
    return symbol.startswith("TEST") or "dataset_write_test" in strategy


def _extract_pnl(row: dict[str, Any]) -> float | None:
    """Return the first available realized PnL value.

    Preference order:
    1. net_pnl
    2. pnl
    3. realized_pnl
    """
    for field in ("net_pnl", "pnl", "realized_pnl"):
        value = _to_float(row.get(field))
        if value is not None:
            return value
    return None


def build_performance_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [
        r
        for r in rows
        if str(r.get("event_type", "")).upper() == "CLOSE"
        and not _is_test_row(r)
    ]

    pnl_values = [_extract_pnl(r) for r in closed]
    pnl_values = [v for v in pnl_values if v is not None]

    wins = sum(1 for v in pnl_values if v > 0)
    losses = sum(1 for v in pnl_values if v < 0)
    breakeven = sum(1 for v in pnl_values if v == 0)

    total = len(pnl_values)
    winrate = round((wins / total) * 100, 2) if total else 0.0

    gross_profit = sum(v for v in pnl_values if v > 0)
    gross_loss = abs(sum(v for v in pnl_values if v < 0))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss else None

    avg_win = round(gross_profit / wins, 4) if wins else 0.0
    avg_loss = round(gross_loss / losses, 4) if losses else 0.0
    expectancy = (
        round((gross_profit - gross_loss) / total, 4)
        if total
        else 0.0
    )

    strategy_pnl: dict[str, float] = {}
    symbol_pnl: dict[str, float] = {}

    for row in closed:
        pnl = _extract_pnl(row)
        if pnl is None:
            continue

        strategy = str(row.get("strategy") or "UNKNOWN")
        symbol = str(row.get("symbol") or "UNKNOWN")

        strategy_pnl[strategy] = strategy_pnl.get(strategy, 0.0) + pnl
        symbol_pnl[symbol] = symbol_pnl.get(symbol, 0.0) + pnl

    strategy_counts = Counter(
        str(r.get("strategy", "UNKNOWN")) for r in closed if r.get("strategy")
    )

    best_strategies = sorted(strategy_pnl.items(), key=lambda x: x[1], reverse=True)[:5]
    worst_strategies = sorted(strategy_pnl.items(), key=lambda x: x[1])[:5]
    best_symbols = sorted(symbol_pnl.items(), key=lambda x: x[1], reverse=True)[:5]
    worst_symbols = sorted(symbol_pnl.items(), key=lambda x: x[1])[:5]

    return {
        "closed_trades": len(closed),
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "winrate": winrate,
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "profit_factor": profit_factor,
        "top_strategies": strategy_counts.most_common(5),
        "average_win": avg_win,
        "average_loss": avg_loss,
        "expectancy": expectancy,
        "best_strategies": best_strategies,
        "worst_strategies": worst_strategies,
        "best_symbols": best_symbols,
        "worst_symbols": worst_symbols,
    }