"""Data & system health: file freshness, parse status, provenance conflicts."""

from __future__ import annotations

from typing import Any

from dashboard_v3.core import sources as src
from dashboard_v3.core.status import Signal, SignalSet, Status

#: Every file the dashboard or the engine depends on, with what it feeds.
TRACKED = [
    ("state/runtime_heartbeat.json", "Engine liveness", True),
    ("state/live_runtime.state", "Session record", True),
    ("state/bot.pid", "Process identity", True),
    ("state/order_intents.json", "Order write-ahead log", False),
    ("state/executed_trades.json", "Local position ledger", True),
    ("state/execution_events.json", "Execution reports", True),
    ("state/account_equity.json", "Equity snapshot", True),
    ("state/watchdog_heartbeat.json", "Watchdog", False),
    ("state/last_shutdown.json", "Last shutdown", False),
    ("reports/backtests/strategy_expectancy.json", "Strategy gates", True),
    ("reports/backtests/latest_summary.json", "Symbol gates (OFFLINE)", True),
    ("logs/trade_dataset_v2.csv", "Closed-trade dataset", True),
    ("logs/trade_plans.csv", "Plan scores", True),
    ("data_store/funnel_events.jsonl", "Decision funnel", True),
    ("data_store/dynamic_grid_v1_events.jsonl", "dynamic_grid_v1 decisions", False),
    ("state/dynamic_grid_v1.json", "dynamic_grid_v1 lifecycle", False),
    ("logs/live.out", "Engine stdout", True),
    ("logs/alerts.log", "Alert history", False),
]


def build() -> dict[str, Any]:
    signals = SignalSet()
    files = []
    missing_required = 0
    corrupt = 0
    stale = 0

    for rel, purpose, required in TRACKED:
        prov = src.file_provenance(rel)
        parsed = True
        error = prov.error
        if prov.exists and rel.endswith(".json"):
            loaded = src.load_json(rel)
            parsed = loaded.provenance.parsed
            error = loaded.provenance.error
        status = prov.status if parsed else Status.DEGRADED
        if not prov.exists:
            status = Status.UNKNOWN
            if required:
                missing_required += 1
        elif not parsed:
            corrupt += 1
        elif status in (Status.STALE, Status.OFFLINE):
            stale += 1
        files.append({
            "path": rel,
            "purpose": purpose,
            "required": required,
            "exists": prov.exists,
            "parsed": parsed,
            "error": error,
            "age_label": prov.age_label,
            "age_seconds": prov.age_seconds,
            "size_bytes": prov.size_bytes,
            "status": status,
        })

    if corrupt:
        signals.add(Signal("corrupt", "Corrupt sources", Status.DEGRADED,
                           f"{corrupt} file(s) failed to parse"))
    if missing_required:
        signals.add(Signal("missing", "Missing sources", Status.UNKNOWN,
                           f"{missing_required} required file(s) absent"))
    if stale:
        signals.add(Signal("stale", "Stale sources", Status.STALE,
                           f"{stale} file(s) past their freshness budget"))
    if not (corrupt or missing_required or stale):
        signals.add(Signal("files", "Data sources", Status.HEALTHY,
                           f"all {len(files)} sources fresh and parseable"))

    # --- provenance conflicts -------------------------------------------
    conflicts = []
    symbol_prov = src.file_provenance("reports/backtests/latest_summary.json")
    strategy_prov = src.file_provenance("reports/backtests/strategy_expectancy.json")
    if symbol_prov.exists and strategy_prov.exists:
        gap_days = ((symbol_prov.age_seconds or 0) - (strategy_prov.age_seconds or 0)) / 86400.0
        if gap_days > 1:
            conflicts.append({
                "title": "Symbol and strategy gates use different vintages",
                "detail": (f"Symbol expectancy is {gap_days:.1f} days older than strategy "
                           f"expectancy. They are not comparable cohorts."),
                "status": Status.DEGRADED,
            })

    dataset_prov = src.file_provenance("logs/trade_dataset_v2.csv")
    if dataset_prov.exists and (dataset_prov.age_seconds or 0) > 86400 * 3:
        conflicts.append({
            "title": "Closed-trade dataset is stale",
            "detail": (f"logs/trade_dataset_v2.csv last written {dataset_prov.age_label} ago; "
                       "performance and expectancy figures do not include newer trades."),
            "status": Status.STALE,
        })

    running_commit = (src.read_kv_state("state/live_runtime.state").value or {}).get("commit", "")
    head = src.repo_head()
    if running_commit and head and running_commit != head:
        conflicts.append({
            "title": "Running code differs from repository HEAD",
            "detail": (f"Engine is executing {running_commit[:7]}; the working tree is at "
                       f"{head[:7]}. Committed fixes are not live until restart."),
            "status": Status.DEGRADED,
        })

    for c in conflicts:
        signals.add(Signal("conflict", c["title"], c["status"], c["detail"]))

    return {
        "signals": signals,
        "status": signals.status,
        "files": files,
        "conflicts": conflicts,
        "counts": {
            "total": len(files),
            "missing_required": missing_required,
            "corrupt": corrupt,
            "stale": stale,
        },
        "running_commit": running_commit,
        "repo_head": head,
    }


__all__ = ["TRACKED", "build"]
