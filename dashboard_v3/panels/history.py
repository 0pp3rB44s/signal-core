"""Trade history and performance, strictly separated by data cohort.

Cohorts are never silently merged. A LIVE figure and a backtest figure are
different measurements of different things; presenting their union as one number
is how a losing system looks profitable.
"""

from __future__ import annotations

import collections
from datetime import datetime, timedelta, timezone
from typing import Any

from dashboard_v3.core import sources as src
from dashboard_v3.core.status import Signal, SignalSet, Status

COHORTS = {
    "LIVE": "Live exchange fills",
    "RECOVERY_ONLY": "Adopted / low-confidence — excluded from gating",
    "BACKTEST": "Offline simulation",
}


def _f(v: Any, d: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "") else d
    except (TypeError, ValueError):
        return d


def _ts(row: dict[str, Any]) -> datetime | None:
    for key in ("closed_at", "close_time", "exit_time", "timestamp", "opened_at"):
        raw = row.get(key)
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _pnl(row: dict[str, Any]) -> float | None:
    for key in ("net_pnl", "pnl", "realized_pnl", "profit"):
        if row.get(key) not in (None, ""):
            try:
                return float(row[key])
            except (TypeError, ValueError):
                continue
    return None


def _truthy(value: Any) -> bool | None:
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "hit"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _stats(pnls: list[float]) -> dict[str, Any]:
    if not pnls:
        return {"trades": 0, "wins": 0, "losses": 0, "winrate": None, "expectancy": None,
                "total": 0.0, "profit_factor": None, "avg_win": None, "avg_loss": None,
                "max_drawdown": None}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    equity, peak, dd = 0.0, 0.0, 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
    return {
        "trades": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": len(wins) / len(pnls),
        "expectancy": sum(pnls) / len(pnls),
        "total": sum(pnls),
        "profit_factor": (gross_win / gross_loss) if gross_loss else None,
        "avg_win": (gross_win / len(wins)) if wins else None,
        "avg_loss": (-gross_loss / len(losses)) if losses else None,
        "max_drawdown": dd,
    }


def build(window_days: int = 30) -> dict[str, Any]:
    signals = SignalSet()
    loaded = src.load_csv("logs/trade_dataset_v2.csv", limit=None)
    rows = loaded.value if isinstance(loaded.value, list) else []

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    trades: list[dict[str, Any]] = []
    undated = 0

    for row in rows:
        status = str(row.get("status") or "").upper()
        if not status.startswith("CLOSED"):
            continue
        pnl = _pnl(row)
        if pnl is None:
            continue
        when = _ts(row)
        if when is None:
            undated += 1
            continue
        strategy = str(row.get("strategy") or row.get("setup_strategy") or "unknown")
        confidence = str(row.get("data_confidence") or "").upper()
        recovery = strategy == "recovered_exchange_position" or confidence in {"LOW_CONFIDENCE", "UNKNOWN"}
        trades.append({
            "symbol": str(row.get("symbol") or "?").upper(),
            "strategy": strategy,
            "direction": str(row.get("direction") or "").upper(),
            "entry": _f(row.get("actual_entry") or row.get("avg_entry") or row.get("entry")),
            "exit": _f(row.get("exit_price") or row.get("close_price")),
            "opened_at": row.get("opened_at"),
            "closed_at": when,
            "pnl": pnl,
            "result": "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "FLAT"),
            "r_multiple": _f(row.get("r_multiple")) if row.get("r_multiple") else None,
            "exit_reason": row.get("exit_reason") or row.get("result") or "UNKNOWN",
            "tp1_hit": _truthy(row.get("tp1_hit")),
            "sl_hit": _truthy(row.get("sl_hit")) if "sl_hit" in row else None,
            "fees": _f(row.get("fees_paid")),
            "score": _f(row.get("score")) if row.get("score") else None,
            "cohort": "RECOVERY_ONLY" if recovery else "LIVE",
            "in_window": when >= cutoff,
        })

    trades.sort(key=lambda t: t["closed_at"], reverse=True)
    windowed = [t for t in trades if t["in_window"]]
    live = [t for t in windowed if t["cohort"] == "LIVE"]
    recovery = [t for t in windowed if t["cohort"] == "RECOVERY_ONLY"]

    live_stats = _stats([t["pnl"] for t in live])
    recovery_stats = _stats([t["pnl"] for t in recovery])

    tp1_tracked = [t for t in live if t["tp1_hit"] is not None]
    tp1_hits = [t for t in tp1_tracked if t["tp1_hit"]]
    live_stats["tp1_hit_rate"] = (len(tp1_hits) / len(tp1_tracked)) if tp1_tracked else None
    live_stats["tp1_tracked"] = len(tp1_tracked)
    live_stats["tp1_missing"] = len(live) - len(tp1_tracked)

    by_symbol = collections.defaultdict(list)
    by_strategy = collections.defaultdict(list)
    for t in live:
        by_symbol[t["symbol"]].append(t["pnl"])
        by_strategy[t["strategy"]].append(t["pnl"])

    equity_curve = []
    running = 0.0
    for t in sorted(live, key=lambda x: x["closed_at"]):
        running += t["pnl"]
        equity_curve.append({"t": t["closed_at"].isoformat(), "equity": round(running, 4)})

    if not loaded.provenance.exists:
        signals.add(Signal("dataset", "Trade dataset", Status.UNKNOWN, "file absent"))
    elif loaded.provenance.status in (Status.STALE, Status.OFFLINE):
        signals.add(Signal("dataset", "Trade dataset", Status.STALE,
                           f"last written {loaded.provenance.age_label} ago",
                           "Newer trades are not represented in these figures."))
    else:
        signals.add(Signal("dataset", "Trade dataset", Status.HEALTHY,
                           f"{len(trades)} closed trades"))

    if recovery:
        signals.add(Signal(
            "cohort", "Cohort separation", Status.STALE,
            f"{len(recovery)} recovery/low-confidence trades held separate",
            "These are adopted positions or unreliable attributions. Merging them "
            "with strategy results would overstate performance.",
        ))

    return {
        "signals": signals,
        "status": signals.status,
        "trades": trades[:500],
        "trade_count": len(trades),
        "window_days": window_days,
        "windowed_count": len(windowed),
        "undated": undated,
        "live_stats": live_stats,
        "recovery_stats": recovery_stats,
        "equity_curve": equity_curve,
        "by_symbol": sorted(
            ({"key": k, **_stats(v)} for k, v in by_symbol.items()),
            key=lambda r: r["total"], reverse=True),
        "by_strategy": sorted(
            ({"key": k, **_stats(v)} for k, v in by_strategy.items()),
            key=lambda r: r["total"], reverse=True),
        "provenance": loaded.provenance,
        "cohorts": COHORTS,
    }


__all__ = ["COHORTS", "build"]
