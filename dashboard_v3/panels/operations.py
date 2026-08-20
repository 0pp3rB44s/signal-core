"""Current-architecture operational projections for Dashboard V3.

This is an adapter layer, not a control plane. It reads bounded structured state,
tails bounded logs, and labels missing facts UNKNOWN. It never invokes RiskManager,
collectors, execution services, or any write/mutation method.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from dashboard_v3.core import sources as src
from dashboard_v3.core.status import Signal, SignalSet, Status, worst
from telemetry.close_record_sources import is_displayable_close

PRODUCTION_REF = "origin/production/live-baseline-cd8671"
RECOVERABLE_INTENTS = {"PREPARED", "SUBMITTING", "SUBMITTED", "AMBIGUOUS", "ADOPTED", "ABSENT", "FILLED"}
BLOCKING_INTENTS = {"UNKNOWN"}

LOG_PATHS = (
    "logs/live.out", "logs/agent.log", "logs/runtime.log", "logs/dashboard.out",
    "data_store/microflow-v1/collector.out", "data_store/research_liq_oi/collector.out",
    "data_store/research_binance/collector.out", "data_store/research_binance_spot/collector.out",
)
LOG_MARKERS = (
    "ERROR", "CRITICAL", "WARNING", "WARN", "RISK_REJECTED", "STARTUP", "RECOVERY",
    "OWNERSHIP", "DEPLOY", "COLLECTOR", "RECONNECT", "RATE_LIMIT",
)
SECRET_ASSIGNMENT = re.compile(
    r"(?i)(api[_-]?key|api[_-]?secret|secret|token|password|passwd|authorization|cookie)"
    r"(\s*[:=]\s*)([^\s|,;]+)"
)
BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+\-/=]+")
BITGET_KEY_SHAPE = re.compile(r"\b[A-Fa-f0-9]{32,64}\b")


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    match = re.search(r"\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+Z?", text)
    if match:
        text = match.group(0)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def redact(text: str) -> str:
    """Redact credential-shaped content before it can reach a template or API."""
    text = SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", str(text))
    text = BEARER.sub(lambda m: f"{m.group(1)} [REDACTED]", text)
    return BITGET_KEY_SHAPE.sub("[REDACTED_HEX]", text)


def _latest_marker(lines: list[str], marker: str) -> str | None:
    marker = marker.upper()
    return next((line for line in reversed(lines) if marker in line.upper()), None)


def _all_operational_lines(per_file: int = 400) -> tuple[list[dict[str, Any]], list[Any]]:
    rows: list[dict[str, Any]] = []
    provenance = []
    for rel in LOG_PATHS:
        loaded = src.load_text_tail(rel, lines=per_file, max_bytes=400_000)
        provenance.append(loaded.provenance)
        for raw in loaded.value if isinstance(loaded.value, list) else []:
            upper = raw.upper()
            categories = [marker for marker in LOG_MARKERS if marker in upper]
            if not categories:
                continue
            rows.append({
                "timestamp": (_parse_ts(raw) or "UNKNOWN"),
                "source": rel,
                "levels": categories,
                "message": redact(raw)[:1200],
            })
    rows.sort(key=lambda row: row["timestamp"] if isinstance(row["timestamp"], datetime)
              else datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return rows[:300], provenance


def _sha_state() -> dict[str, Any]:
    runtime = src.read_kv_state("state/live_runtime.state")
    marker = src.read_first_line("state/deployed_commit.txt")
    runner = src.repo_head()
    production = src.git_ref(PRODUCTION_REF)
    runtime_sha = str((runtime.value or {}).get("commit") or "")
    values = {"github_production": production, "runner": runner,
              "marker": str(marker.value or ""), "runtime": runtime_sha}
    known = [value for value in values.values() if value]
    if len(known) != 4:
        status, match = Status.UNKNOWN, "UNKNOWN"
    elif len(set(known)) == 1:
        status, match = Status.HEALTHY, "MATCH"
    else:
        status, match = Status.BLOCKED, "MISMATCH"
    return {"shas": values, "status": status, "match": match,
            "runtime_provenance": runtime.provenance, "marker_provenance": marker.provenance}


def build_deployment() -> dict[str, Any]:
    sha = _sha_state()
    worktree = src.worktree_status()
    stop_flags = []
    for rel in ("state/live.stop", "state/supervisor.stop", "state/forward_paper_keepalive.stop"):
        prov = src.file_provenance(rel)
        stop_flags.append({"path": rel, "set": prov.exists, "provenance": prov})
    supervisor_pids = src.matching_pids(r"run_supervised\.sh|live_agent\.sh|forward_paper_keepalive")
    return {
        "sha": sha, "worktree": worktree, "stop_flags": stop_flags,
        "supervisor_pids": supervisor_pids,
        "supervisor_status": "ACTIVE" if supervisor_pids else "UNKNOWN",
        "status": worst(sha["status"], Status.HEALTHY if worktree.get("clean") else
                        (Status.DEGRADED if worktree.get("known") else Status.UNKNOWN)),
    }


def _kv_fields(line: str | None) -> dict[str, str]:
    if not line:
        return {}
    fields: dict[str, str] = {}
    for part in line.split("|"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[key.strip().lower().replace(" ", "_")] = value.strip()
    return fields


def _weekly_net(rows: list[dict[str, Any]]) -> float | None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    total = 0.0
    seen = set()
    observed = False
    for row in rows:
        if not is_displayable_close(row):
            continue
        when = _parse_ts(row.get("closed_at") or row.get("timestamp"))
        if not when or when < cutoff:
            continue
        lifecycle = str(row.get("position_lifecycle_id") or "")
        if lifecycle and lifecycle in seen:
            continue
        net = _optional_float(row.get("exchange_truth_pnl") if row.get("exchange_truth_pnl") not in (None, "") else row.get("net_pnl"))
        if net is None:
            continue
        observed = True
        total += net
        if lifecycle:
            seen.add(lifecycle)
    return round(total, 8) if observed else None


def build_risk() -> dict[str, Any]:
    settings = Settings()
    guard = src.load_json("state/portfolio_equity_guard.json", default={})
    daily = src.load_json("data_store/trades/daily_learning_report.json", default={})
    journal = src.load_csv("logs/trade_dataset_v2.csv", limit=None)
    log_lines = src.load_text_tail("logs/live.out", lines=1200, max_bytes=1_000_000)
    lines = log_lines.value if isinstance(log_lines.value, list) else []
    evaluations = [line for line in lines if "RISK_EVALUATION" in line]
    latest = evaluations[-1] if evaluations else None
    fields = _kv_fields(latest)
    rejected = []
    for line in reversed(evaluations):
        parsed = _kv_fields(line)
        if parsed.get("decision") != "RISK_REJECTED":
            continue
        reason = parsed.get("reasons", "UNKNOWN")
        value_match = re.search(r"(?:value|actual|drawdown_pct|daily_loss_pct)=(-?[0-9.]+)", reason)
        limit_match = re.search(r"(?:limit|max|threshold|hard_daily_stop_pct)=(-?[0-9.]+)", reason)
        rejected.append({"timestamp": _parse_ts(line) or "UNKNOWN",
                         "symbol": parsed.get("symbol", "UNKNOWN"), "reason": redact(reason),
                         "value": value_match.group(1) if value_match else "UNKNOWN",
                         "limit": limit_match.group(1) if limit_match else "UNKNOWN"})
        if len(rejected) >= 25:
            break

    d = daily.value if isinstance(daily.value, dict) else {}
    g = guard.value if isinstance(guard.value, dict) else {}
    drawdown = _optional_float(g.get("drawdown_pct"))
    breaker_limit = _optional_float(g.get("hard_daily_stop_pct"))
    if not guard.ok:
        breaker, breaker_status = "UNKNOWN", Status.UNKNOWN
    elif drawdown is None or breaker_limit is None:
        breaker, breaker_status = "UNKNOWN", Status.UNKNOWN
    elif drawdown >= breaker_limit:
        breaker, breaker_status = "BLOCKED", Status.BLOCKED
    elif drawdown >= breaker_limit * 0.5:
        breaker, breaker_status = "WARNING", Status.DEGRADED
    else:
        breaker, breaker_status = "GREEN", Status.HEALTHY

    weekly = _weekly_net(journal.value if isinstance(journal.value, list) else [])
    weekly_limit = _optional_float(getattr(settings, "weekly_freeze_loss_pct", None))
    current_equity = _optional_float(g.get("last_equity"))
    weekly_loss_pct = (abs(weekly) / current_equity * 100.0
                       if weekly is not None and weekly < 0 and current_equity else 0.0
                       if weekly is not None and current_equity else None)
    weekly_frozen = (weekly_loss_pct >= weekly_limit if weekly_loss_pct is not None and weekly_limit else None)
    last_ts = _parse_ts(latest) if latest else None
    age = (datetime.now(timezone.utc) - last_ts).total_seconds() if last_ts else None
    active = age is not None and age <= 3600
    status = worst(breaker_status, guard.status,
                   Status.HEALTHY if active else Status.UNKNOWN)
    return {
        "status": status, "active": active if last_ts else None, "last_evaluation": last_ts,
        "last_decision": fields.get("decision", "UNKNOWN"), "last_fields": fields,
        "daily_realized_loss": _optional_float(d.get("daily_total_net_pnl")),
        "daily_loss_limit_pct": _optional_float(getattr(settings, "hard_daily_stop_pct", None)),
        "consecutive_losses": d.get("consecutive_losses") if daily.ok else None,
        "consecutive_loss_limit": 3,
        "weekly_realized_pnl": weekly, "weekly_freeze_limit_pct": weekly_limit,
        "weekly_loss_pct": weekly_loss_pct, "weekly_frozen": weekly_frozen,
        "session_multiplier": _optional_float(fields.get("session_multiplier")),
        "session_reason": fields.get("session_reason", "UNKNOWN"),
        # These are not persisted by current production RiskManager telemetry.
        "correlated_exposure": None, "total_portfolio_exposure": None,
        "equity_high_water": _optional_float(g.get("high_water_equity")),
        "current_equity": current_equity, "drawdown_pct": drawdown,
        "drawdown_breaker_threshold_pct": breaker_limit,
        "breaker": breaker, "breaker_status": breaker_status, "rejections": rejected,
        "guard_provenance": guard.provenance, "daily_provenance": daily.provenance,
        "journal_provenance": journal.provenance, "log_provenance": log_lines.provenance,
    }


def _collector(name: str, rel: str, pid_rel: str) -> dict[str, Any]:
    loaded = src.load_json(rel, default={})
    payload = loaded.value if isinstance(loaded.value, dict) else {}
    pid_loaded = src.read_first_line(pid_rel)
    try:
        pid = int(pid_loaded.value) if pid_loaded.value else None
    except (TypeError, ValueError):
        pid = None
    alive = src.pid_alive(pid)
    raw_rows = payload.get("rows") if isinstance(payload.get("rows"), dict) else {}
    if not raw_rows:
        aliases = {"trade": "trade_rows", "book": "book_rows", "candidate": "candidate_rows",
                   "near_miss": "near_miss_rows", "oi": "oi_rows", "liq": "liq_rows"}
        raw_rows = {name: payload[key] for name, key in aliases.items() if key in payload}
    error_keys = [key for key in payload
                  if key.endswith("errors") or key in {"parse_failures", "dropped", "malformed_frames"}]
    error_count = sum(int(payload.get(key) or 0) for key in error_keys) if error_keys else None
    if not loaded.ok:
        status = Status.UNKNOWN
    elif loaded.provenance.age_seconds is not None and loaded.provenance.age_seconds > 180:
        status = Status.OFFLINE
    elif pid is not None and not alive:
        status = Status.OFFLINE
    else:
        stream_values = [str(v.get("status")) for v in (payload.get("streams") or {}).values()
                         if isinstance(v, dict)]
        explicit = [str(payload.get(k)) for k in payload if k.endswith("_stream_health")]
        unhealthy = any(v in {"STALE", "NO_DELIVERY_PROVEN", "UNKNOWN_AGE", "DOWN"}
                        for v in stream_values + explicit)
        status = Status.DEGRADED if unhealthy or (error_count or 0) > 0 else Status.HEALTHY
    updated_ms = _optional_float(payload.get("write_timestamp_ms") or payload.get("updated_ms"))
    return {"name": name, "status": status, "pid": pid, "alive": alive if pid else None,
            "payload": payload, "rows": raw_rows, "error_count": error_count,
            "streams": payload.get("streams") or {},
            "last_update": datetime.fromtimestamp(updated_ms / 1000.0, tz=timezone.utc)
            if updated_ms else None, "provenance": loaded.provenance,
            "pid_provenance": pid_loaded.provenance}


def build_collectors() -> dict[str, Any]:
    collectors = [
        _collector("Bitget research", "data_store/research_liq_oi/status.json",
                   "data_store/research_liq_oi/collector.pid"),
        _collector("Binance futures", "data_store/research_binance/status.json",
                   "data_store/research_binance/collector.pid"),
        _collector("Binance spot", "data_store/research_binance_spot/status.json",
                   "data_store/research_binance_spot/collector.pid"),
        _collector("Bitget MicroFlow LIVE sensor", "data_store/microflow_live/status.json",
                   "data_store/microflow_live/collector.pid"),
        _collector("Bitget MicroFlow research sensor", "data_store/microflow-v1/status.json",
                   "data_store/microflow-v1/collector.pid"),
    ]
    clean = src.load_json("state/research_collection_window.json", default={})
    clean_payload = clean.value if isinstance(clean.value, dict) else {}
    return {"collectors": collectors, "status": worst(*(c["status"] for c in collectors)),
            "clean_data_start_utc": clean_payload.get("clean_data_start_utc"),
            "earliest_research_date": clean_payload.get("earliest_research_date"),
            "window_provenance": clean.provenance}


def build_logs() -> dict[str, Any]:
    rows, provenance = _all_operational_lines()
    counts = Counter(level for row in rows for level in row["levels"])
    return {"rows": rows, "counts": dict(counts), "provenance": provenance,
            "status": Status.DEGRADED if any("CRITICAL" in r["levels"] or "ERROR" in r["levels"] for r in rows[:25])
            else (Status.HEALTHY if rows else Status.UNKNOWN)}


def _vault_root() -> Path | None:
    configured = os.environ.get("DASHBOARD_OBSIDIAN_VAULT", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.append(src.BASE_PATH.parent / "Obsidian vault")
    return next((path for path in candidates if path.is_dir()), None)


def _compact_markdown(path: Path, max_lines: int = 18) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    useful = [line.strip() for line in lines if line.strip() and not line.strip().startswith("---")]
    return [redact(line)[:500] for line in useful[:max_lines]]


def build_project() -> dict[str, Any]:
    root = _vault_root()
    files = {
        "CURRENT_PRODUCTION": "00_PROJECT_STATE/CURRENT_PRODUCTION.md",
        "CURRENT_TASK": "00_PROJECT_STATE/CURRENT_TASK.md",
        "NEXT_ACTIONS": "00_PROJECT_STATE/NEXT_ACTIONS.md",
        "CURRENT_STATE": "00_PROJECT_STATE/CURRENT_STATE.md",
    }
    docs = []
    if root:
        for name, rel in files.items():
            path = root / rel
            docs.append({"name": name, "path": rel, "exists": path.is_file(),
                         "lines": _compact_markdown(path) if path.is_file() else [],
                         "mtime": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                         if path.is_file() else None})
    all_lines = [line for doc in docs for line in doc["lines"]]
    open_p0 = sum(1 for line in all_lines if re.search(r"(?i)- \[ \].*\bP0\b", line))
    open_p1 = sum(1 for line in all_lines if re.search(r"(?i)- \[ \].*\bP1\b", line))
    strategy_line = next((line for line in all_lines if "microflow" in line.lower()), "UNKNOWN")
    review_dir = root / "04_TRADES" if root else None
    decision_dir = root / "05_DECISIONS" if root else None
    latest_review = max(review_dir.glob("*.md"), key=lambda p: p.stat().st_mtime).stem if review_dir and review_dir.is_dir() else "UNKNOWN"
    latest_decision = max(decision_dir.glob("*.md"), key=lambda p: p.stat().st_mtime).stem if decision_dir and decision_dir.is_dir() else "UNKNOWN"
    return {"root_available": root is not None, "root": str(root) if root else "UNKNOWN",
            "documents": docs, "open_p0": open_p0 if root else None,
            "open_p1": open_p1 if root else None, "strategy_disposition": strategy_line,
            "latest_trade_review": latest_review, "latest_strategy_decision": latest_decision,
            "status": Status.HEALTHY if root and all(d["exists"] for d in docs) else Status.UNKNOWN}


def build_strategy_state() -> dict[str, Any]:
    settings = Settings()
    from microflow.candidates import FrozenResearchSpec
    spec = FrozenResearchSpec()
    logs = src.load_text_tail("logs/live.out", lines=600)
    started = _latest_marker(logs.value if isinstance(logs.value, list) else [],
                             "MICROFLOW_LIVE_RUNTIME_STARTED")
    strategy_match = re.search(r"strategy=([^|\s]+)", started or "")
    return {"strategy_id": strategy_match.group(1) if strategy_match else "UNKNOWN", "spec_id": spec.spec_id,
            "enabled": bool(settings.microflow_scalper_enabled), "tp_bps": spec.tp_bps,
            "sl_bps": spec.sl_bps, "max_hold_ms": spec.max_hold_ms,
            "risk_per_trade_pct": _optional_float(settings.account_risk_per_trade_pct),
            "symbols": list(settings.microflow_symbol_set)}


def build_reconciliation() -> dict[str, Any]:
    intents = src.load_json("state/order_intents.json", default=[])
    rows = intents.value if isinstance(intents.value, list) else []
    unresolved = [row for row in rows if str(row.get("state") or "").upper() in RECOVERABLE_INTENTS | BLOCKING_INTENTS]
    logs = src.load_text_tail("logs/live.out", lines=1200, max_bytes=1_000_000)
    lines = logs.value if isinstance(logs.value, list) else []
    recovery = _latest_marker(lines, "STARTUP_RECOVERY") or _latest_marker(lines, "STARTUP_CLOSE_RECOVERY")
    ownership = _latest_marker(lines, "OWNERSHIP_AUDIT") or _latest_marker(lines, "OWNERSHIP_BLOCKED")
    return {"intents": unresolved, "unresolved_count": len(unresolved) if intents.ok else None,
            "startup_recovery": redact(recovery) if recovery else "UNKNOWN",
            "ownership_audit": redact(ownership) if ownership else "UNKNOWN",
            "intents_provenance": intents.provenance, "logs_provenance": logs.provenance,
            "status": Status.BLOCKED if unresolved else (Status.HEALTHY if intents.ok else Status.UNKNOWN)}


def build_quality() -> dict[str, Any]:
    loaded = src.load_json("state/dashboard_review_summary.json", default={})
    payload = loaded.value if isinstance(loaded.value, dict) else {}
    entry = {key: payload.get("entry", {}).get(key) for key in ("GOOD", "ACCEPTABLE", "LATE", "CHASE", "EXHAUSTION")}
    direction = {key: payload.get("direction", {}).get(key) for key in ("CORRECT", "WEAK", "WRONG")}
    exits = {key: payload.get("exit", {}).get(key) for key in ("TP", "STOP", "MAX_HOLD", "PROFIT_LOCK", "OTHER")}
    return {"available": loaded.ok and bool(payload), "entry": entry, "direction": direction,
            "exit": exits, "pattern_a_profit_lock_watch_count": payload.get("pattern_a_profit_lock_watch_count"),
            "late_chase_count": payload.get("late_chase_count"), "provenance": loaded.provenance,
            "status": Status.HEALTHY if loaded.ok and payload else Status.UNKNOWN}


def build_microflow_funnel() -> dict[str, Any]:
    """Project the persisted MicroFlow episode/log funnel; never replay research."""
    loaded = src.load_segment_tail("data_store/microflow_live/near_miss", row_limit=6000)
    if not loaded.ok:
        loaded = src.load_segment_tail("data_store/microflow-v1/near_miss", row_limit=6000)
    events = loaded.value if isinstance(loaded.value, list) else []
    episodes = {str(row.get("episode_id")) for row in events if row.get("episode_id")}
    static_pass = sum(1 for row in events if isinstance(row.get("gates"), dict)
                      and all(bool(v) for key, v in row["gates"].items() if key != "persistence"))
    persistence = sum(1 for row in events if str(row.get("state")) == "CANDIDATE")
    reasons = Counter(str(row.get("last_failed_gate") or row.get("reason") or "other")
                      for row in events if row.get("last_failed_gate") or row.get("reason"))
    log_loaded = src.load_text_tail("logs/live.out", lines=2000, max_bytes=1_500_000)
    lines = log_loaded.value if isinstance(log_loaded.value, list) else []
    pre_pass = sum("MICROFLOW_PRE_SUBMIT_PASS" in line for line in lines)
    pre_block_lines = [line for line in lines if "MICROFLOW_PRE_SUBMIT_BLOCKED" in line]
    orders = sum(any(marker in line for marker in ("ORDER_SUBMITTED", "ENTRY_SUBMITTED")) for line in lines)
    fills = sum(any(marker in line for marker in ("ORDER_FILLED", "ENTRY_FILLED", "FILL_ANALYTICS")) for line in lines)
    block_reasons = Counter()
    for line in pre_block_lines:
        match = re.search(r"reason=([^|]+)", line)
        reason = (match.group(1).strip() if match else "other").lower()
        if "slippage" in reason:
            reason = "slippage"
        elif "signal_no_longer_valid" in reason:
            reason = "signal_no_longer_valid"
        elif "risk" in reason:
            reason = "risk_rejected"
        block_reasons[reason] += 1
    for key, value in reasons.items():
        normalized = key if key in {"volatility_floor", "freshness", "persistence"} else "other"
        block_reasons[normalized] += value
    stages = [
        ("near_miss_episodes", len(episodes) if loaded.ok else None),
        ("static_gates_pass", static_pass if loaded.ok else None),
        ("persistence_pass", persistence if loaded.ok else None),
        ("candidates", persistence if loaded.ok else None),
        ("pre_submit_passes", pre_pass if log_loaded.ok else None),
        ("pre_submit_blocks", len(pre_block_lines) if log_loaded.ok else None),
        ("orders", orders if log_loaded.ok else None),
        ("fills", fills if log_loaded.ok else None),
    ]
    return {"stages": [{"name": name, "count": count} for name, count in stages],
            "block_reasons": dict(block_reasons), "near_miss_provenance": loaded.provenance,
            "log_provenance": log_loaded.provenance,
            "status": Status.HEALTHY if loaded.ok or log_loaded.ok else Status.UNKNOWN}


def build() -> dict[str, Any]:
    return {"deployment": build_deployment(), "risk": build_risk(),
            "collectors": build_collectors(), "logs": build_logs(),
            "project": build_project(), "strategy_state": build_strategy_state(),
            "reconciliation": build_reconciliation(), "quality": build_quality(),
            "microflow_funnel": build_microflow_funnel()}


__all__ = ["build", "build_collectors", "build_deployment", "build_logs", "build_project",
           "build_quality", "build_reconciliation", "build_risk", "redact"]
