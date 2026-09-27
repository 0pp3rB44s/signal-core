"""Expectancy and the risk gates it drives — including data provenance.

Symbol gating now reads risk.symbol_expectancy: directional, keyed by
(symbol, direction), sourced from exchange-confirmed live closes in
logs/trade_dataset_v2.csv, and stamped with freshness and sample size.

reports/backtests/latest_summary.json is still shown, but only to make the
migration visible — it is offline backtest output and no longer participates in
any live decision. That distinction is the most consequential fact on this page,
so both sources are labelled explicitly rather than merged.
"""

from __future__ import annotations

from typing import Any

from dashboard_v3.core import sources as src
from dashboard_v3.core.status import Signal, SignalSet, Status
from risk import symbol_expectancy as se

#: Thresholds mirrored from risk/ for DISPLAY ONLY.
#: This module never gates anything; it explains what the engine decided.
STRATEGY_MIN_TRADES = 5

#: Freshness -> ladder status, for colour only.
FRESHNESS_STATUS = {
    se.FRESH: Status.HEALTHY,
    se.AGING: Status.HEALTHY,
    se.STALE: Status.STALE,
    se.EXPIRED: Status.STALE,
}


def _f(v: Any, d: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "") else d
    except (TypeError, ValueError):
        return d


def _record_row(rec: se.SymbolExpectancyRecord) -> dict[str, Any]:
    """One directional row, with the gate outcome the engine would reach."""
    blocked, reason = se.evaluate(rec)
    return {
        "symbol": rec.symbol,
        "direction": rec.direction,
        "sample_size": rec.sample_size,
        "expectancy": rec.expectancy,
        "winrate": rec.winrate,
        "tp1_hit_rate": rec.tp1_hit_rate,
        "status": rec.status,
        "freshness_state": rec.freshness_state,
        "freshness_status": FRESHNESS_STATUS.get(rec.freshness_state, Status.UNKNOWN),
        "confidence": rec.confidence,
        "last_trade_at": rec.last_trade_at,
        "window_days": rec.window_days,
        "source": rec.source,
        "blocked": blocked,
        "gate_status": Status.BLOCKED if blocked else Status.HEALTHY,
        "reason": reason or f"{rec.status} — does not block",
    }


def build() -> dict[str, Any]:
    signals = SignalSet()

    strategy_loaded = src.load_json("reports/backtests/strategy_expectancy.json", default={})
    symbol_loaded = src.load_json("reports/backtests/latest_summary.json", default={})

    sdata = strategy_loaded.value if isinstance(strategy_loaded.value, dict) else {}
    ydata = symbol_loaded.value if isinstance(symbol_loaded.value, dict) else {}

    # --- strategies (live-maintained, rolling window) --------------------
    strategies = []
    for name, stats in (sdata.get("strategies") or {}).items():
        if not isinstance(stats, dict):
            continue
        exp = _f(stats.get("expectancy"))
        trades = int(_f(stats.get("trades")))
        fresh = stats.get("fresh_since_geometry_fix") or {}
        strategies.append({
            "name": name,
            "trades": trades,
            "winrate": _f(stats.get("winrate")),
            "expectancy": exp,
            "profit_factor": _f(stats.get("profit_factor")),
            "tp1_hit_rate": _f(stats.get("tp1_hit_rate")),
            "total_pnl": _f(stats.get("total_pnl")),
            "status": str(stats.get("status") or "UNKNOWN"),
            "gate_status": Status.BLOCKED if exp < 0 else Status.HEALTHY,
            "probe": exp < 0 and trades >= STRATEGY_MIN_TRADES,
            "fresh_expectancy": _f(fresh.get("expectancy")) if fresh else None,
            "fresh_trades": int(_f(fresh.get("trades"))) if fresh else None,
        })
    strategies.sort(key=lambda s: s["expectancy"])

    # --- symbols: directional, live-sourced ------------------------------
    records, source_status = se.load_records()
    symbols = [_record_row(r) for r in sorted(records.values(),
                                              key=lambda r: (r.symbol, r.direction))]

    # --- retired source, shown only to prove it no longer gates ----------
    retired_symbols = [
        {"symbol": name,
         "trades": int(_f(stats.get("trades"))),
         "expectancy": _f(stats.get("expectancy")),
         "winrate": _f(stats.get("winrate"))}
        for name, stats in (ydata.get("by_symbol") or {}).items()
        if isinstance(stats, dict)
    ]
    retired_symbols.sort(key=lambda s: s["expectancy"], reverse=True)

    # --- provenance verdicts --------------------------------------------
    symbol_prov = symbol_loaded.provenance
    symbol_age_days = (symbol_prov.age_seconds or 0) / 86400.0
    symbol_stale = symbol_age_days > 2

    if source_status == se.SOURCE_MALFORMED:
        signals.add(Signal(
            "symbol_data", "Symbol expectancy source", Status.BLOCKED,
            f"{se.SOURCE_NAME} malformed — failing closed",
            "The engine blocks every symbol until this source parses again. "
            "Corruption must not silently disable symbol protection.",
        ))
    elif source_status == se.SOURCE_ABSENT:
        signals.add(Signal(
            "symbol_data", "Symbol expectancy source", Status.UNKNOWN,
            f"{se.SOURCE_NAME} absent — treated as insufficient live data",
            "No live closes recorded yet. This does not imply a positive edge; "
            "per-setup gates remain the active filter.",
        ))
    else:
        blocking = [s for s in symbols if s["blocked"]]
        expired = [s for s in symbols if s["freshness_state"] in (se.STALE, se.EXPIRED)]
        signals.add(Signal(
            "symbol_data", "Symbol expectancy source",
            Status.HEALTHY if symbols else Status.UNKNOWN,
            f"{len(symbols)} (symbol,direction) records · {len(blocking)} blocking · "
            f"{len(expired)} past blocking freshness",
            f"Live exchange-confirmed closes from {se.SOURCE_NAME}, "
            f"{se.WINDOW_DAYS}d rolling window, min sample {se.MIN_SAMPLE}. "
            f"Evidence older than {se.STALE_MAX_DAYS}d is reported but never enforced.",
        ))

    if retired_symbols:
        signals.add(Signal(
            "symbol_data_retired", "Retired symbol source", Status.HEALTHY,
            f"latest_summary.json ({symbol_age_days:.1f}d old) no longer gates",
            "Offline backtest output. Retained for comparison only; removed from "
            "every live decision path.",
        ))

    if not strategy_loaded.provenance.exists:
        signals.add(Signal("strategy_data", "Strategy expectancy source", Status.UNKNOWN,
                           "strategy_expectancy.json absent"))
    else:
        signals.add(Signal("strategy_data", "Strategy expectancy source",
                           strategy_loaded.provenance.status,
                           f"{strategy_loaded.provenance.age_label} old · "
                           f"{sdata.get('expectancy_window_days', '?')}d rolling window"))

    paused_strategies = [s for s in strategies if s["status"] == "PAUSE"]
    if paused_strategies:
        signals.add(Signal(
            "strategy_pause", "Strategy gates", Status.BLOCKED,
            f"{len(paused_strategies)} of {len(strategies)} PAUSE",
            "PAUSE means negative expectancy; the engine trades these at probe size.",
        ))

    paused_symbols = [s for s in symbols if s["blocked"]]
    if paused_symbols:
        signals.add(Signal(
            "symbol_pause", "Symbol gates", Status.BLOCKED,
            f"{len(paused_symbols)} of {len(symbols)} (symbol,direction) blocked",
            "Only a fresh, sufficiently-sampled negative live verdict blocks.",
        ))

    summary = sdata.get("summary") or {}
    recovery = sdata.get("recovery_events") or {}

    return {
        "signals": signals,
        "status": signals.status,
        "strategies": strategies,
        "symbols": symbols,
        "retired_symbols": retired_symbols,
        "symbol_source": se.SOURCE_NAME,
        "symbol_source_status": source_status or "OK",
        "symbol_window_days": se.WINDOW_DAYS,
        "symbol_min_sample": se.MIN_SAMPLE,
        "freshness_thresholds": {
            se.FRESH: se.FRESH_MAX_DAYS,
            se.AGING: se.AGING_MAX_DAYS,
            se.STALE: se.STALE_MAX_DAYS,
            se.EXPIRED: None,
        },
        "symbol_provenance": symbol_prov,
        "strategy_provenance": strategy_loaded.provenance,
        "symbol_stale": symbol_stale,
        "symbol_age_days": symbol_age_days,
        "window_days": sdata.get("expectancy_window_days"),
        "created_at": sdata.get("created_at_utc"),
        "symbol_created_at": ydata.get("created_at_utc"),
        "cohorts": {
            "strategy": summary.get("strategy") or {},
            "recovery": summary.get("recovery") or {},
            "all_closed": summary.get("all_closed") or {},
        },
        "recovery_events": [
            {"name": k, **{kk: v.get(kk) for kk in
                           ("trades", "winrate", "expectancy", "total_pnl", "status")}}
            for k, v in recovery.items() if isinstance(v, dict)
        ],
    }


__all__ = ["build"]
