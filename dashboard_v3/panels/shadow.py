"""Sunday shadow run: observability only. Builds every fact the shadow summary,
positions, closed-trade and safety panels need from the shadow checkout's own
structured files - never from log text when a structured source exists, per
data_store/forward_paper_events.jsonl / forward_paper_outcomes.csv / state/*.

Hard rule carried over from the incident that started this feature: a PID that
exists is not evidence of health. Every status here requires the heartbeat's own
timestamp and scan counters to be current, not merely a live process.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from dashboard_v3.core import shadow_sources as ssrc
from dashboard_v3.core import sources as psrc
from dashboard_v3.core.status import Status

#: Overridable via env so a future deploy doesn't require a code change.
EXPECTED_SHADOW_SHA = os.environ.get("EXPECTED_SHADOW_SHA", "b1d9fa9").strip()
HARDENING_BOUNDARY_SHA = os.environ.get("SHADOW_HARDENING_BOUNDARY_SHA", EXPECTED_SHADOW_SHA).strip()
EXPECTED_PRODUCTION_SHA = os.environ.get(
    "EXPECTED_PRODUCTION_SHA", "4d2fb7182afc49687cfa27cde3fcc7c4783f22ae"
).strip()

#: A scan cycle runs on a fixed interval; the heartbeat and last-scan timestamps
#: must be current within a small multiple of it, not an arbitrary guess.
_SCAN_INTERVAL_FALLBACK_SEC = 60
_HEARTBEAT_HEALTHY_MULTIPLE = 3   # 3 missed cycles = STALE, not one slow tick
_HEARTBEAT_HUNG_MULTIPLE = 10     # 10 missed cycles with a live PID = HUNG

HISTORICAL_ADVERSE_SELECTION_REFERENCE_BPS = 13.8

REAL_NUMERIC_TOLERANCE = 1e-9


def _sha_match(a: str, b: str) -> bool:
    """Short-vs-full SHA prefixes of the same commit are a match, not a mismatch."""
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer.startswith(shorter)


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    return None


def _percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolation percentile. No numpy dependency for a handful of rows."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"latest": None, "avg": None, "median": None, "p25": None, "p75": None}
    return {
        "latest": values[-1],
        "avg": sum(values) / len(values),
        "median": _percentile(values, 0.5),
        "p25": _percentile(values, 0.25),
        "p75": _percentile(values, 0.75),
    }


# --- runtime identity / health -------------------------------------------

@dataclass
class ShadowRuntime:
    root_available: bool
    root_error: str
    heartbeat: dict[str, Any] = field(default_factory=dict)
    heartbeat_prov: Any = None
    runtime_state: dict[str, str] = field(default_factory=dict)
    runtime_state_prov: Any = None

    pid: int | None = None
    pid_alive: bool = False
    process_cwd: str | None = None
    cwd_matches_shadow: bool | None = None

    runtime_sha: str = ""
    sha_match: str = "UNKNOWN"          # YES / NO / UNKNOWN

    heartbeat_age_seconds: float | None = None
    last_successful_scan_utc: str | None = None
    seconds_since_last_scan: float | None = None
    scan_cycles_completed: int | None = None
    scan_cycles_started: int | None = None
    last_error_type: str | None = None

    market_data_receiving: bool | None = None
    signal_loop_running: bool | None = None

    status: Status = Status.UNKNOWN
    status_reason: str = "shadow root unavailable"


def load_runtime() -> ShadowRuntime:
    if ssrc.SHADOW_ROOT is None:
        return ShadowRuntime(root_available=False, root_error=ssrc.SHADOW_ROOT_ERROR)

    hb = ssrc.load_json("state/runtime_heartbeat.json", default={})
    rt = ssrc.read_kv_state("state/forward_paper_runtime.state")

    r = ShadowRuntime(root_available=True, root_error="",
                       heartbeat=hb.value or {}, heartbeat_prov=hb.provenance,
                       runtime_state=rt.value or {}, runtime_state_prov=rt.provenance)

    pid_raw = r.heartbeat.get("pid") or r.runtime_state.get("pid")
    try:
        r.pid = int(pid_raw) if pid_raw not in (None, "") else None
    except (TypeError, ValueError):
        r.pid = None
    r.pid_alive = ssrc.pid_alive(r.pid)
    r.process_cwd = ssrc.process_cwd(r.pid) if r.pid_alive else None
    if r.process_cwd is not None:
        try:
            r.cwd_matches_shadow = os.path.realpath(r.process_cwd) == os.path.realpath(str(ssrc.SHADOW_ROOT))
        except OSError:
            r.cwd_matches_shadow = None

    r.runtime_sha = str(r.heartbeat.get("commit") or r.runtime_state.get("sha") or "")
    if r.runtime_sha and EXPECTED_SHADOW_SHA:
        r.sha_match = "YES" if _sha_match(r.runtime_sha, EXPECTED_SHADOW_SHA) else "NO"
    else:
        r.sha_match = "UNKNOWN"

    now = time.time()
    hb_ts = r.heartbeat.get("timestamp")
    if hb_ts:
        try:
            r.heartbeat_age_seconds = max(0.0, now - datetime.fromisoformat(
                str(hb_ts).replace("Z", "+00:00")).timestamp())
        except ValueError:
            r.heartbeat_age_seconds = None

    r.last_successful_scan_utc = r.heartbeat.get("last_successful_scan_utc")
    if r.last_successful_scan_utc:
        try:
            r.seconds_since_last_scan = max(0.0, now - datetime.fromisoformat(
                str(r.last_successful_scan_utc).replace("Z", "+00:00")).timestamp())
        except ValueError:
            r.seconds_since_last_scan = None

    r.scan_cycles_completed = r.heartbeat.get("scan_cycles_completed")
    r.scan_cycles_started = r.heartbeat.get("scan_cycles_started")
    r.last_error_type = r.heartbeat.get("last_error_type")

    scan_interval = _num(r.runtime_state.get("scan_interval_sec")) or _SCAN_INTERVAL_FALLBACK_SEC
    healthy_threshold = scan_interval * _HEARTBEAT_HEALTHY_MULTIPLE
    hung_threshold = scan_interval * _HEARTBEAT_HUNG_MULTIPLE

    # Multi-signal classification. PID existence alone never yields HEALTHY.
    if not r.heartbeat_prov.exists and not r.runtime_state_prov.exists:
        r.status, r.status_reason = Status.UNKNOWN, "no shadow state files found"
    elif not r.pid_alive:
        r.status, r.status_reason = Status.OFFLINE, "recorded PID is not running"
    elif r.seconds_since_last_scan is None:
        r.status, r.status_reason = Status.UNKNOWN, "no successful-scan timestamp recorded"
    elif r.seconds_since_last_scan > hung_threshold:
        r.status, r.status_reason = Status.OFFLINE, (
            f"PID {r.pid} alive but no successful scan for "
            f"{r.seconds_since_last_scan:.0f}s (>{hung_threshold:.0f}s) - HUNG")
    elif r.seconds_since_last_scan > healthy_threshold:
        r.status, r.status_reason = Status.STALE, (
            f"last successful scan {r.seconds_since_last_scan:.0f}s ago "
            f"(>{healthy_threshold:.0f}s)")
    elif r.cwd_matches_shadow is False:
        r.status, r.status_reason = Status.DEGRADED, (
            f"PID {r.pid} is alive and scanning, but its cwd ({r.process_cwd}) "
            "does not match the shadow checkout - verify this is really the shadow engine")
    else:
        r.status, r.status_reason = Status.HEALTHY, (
            f"PID {r.pid}, last scan {r.seconds_since_last_scan:.0f}s ago")

    r.market_data_receiving = r.status in (Status.HEALTHY, Status.STALE) and r.last_error_type in (None, "")
    r.signal_loop_running = r.status is Status.HEALTHY

    return r


# --- trade parsing ----------------------------------------------------------

_OUTCOME_NUMERIC_FIELDS = (
    "planned_entry", "simulated_fill", "initial_stop", "initial_risk_r",
    "expected_reward_to_risk", "expected_move_bps", "spread_bps", "signal_to_fill_bps",
    "open_interest", "open_interest_change_5m", "open_interest_change_15m",
    "taker_buy_sell_volume_ratio", "top_trader_long_short_ratio", "global_long_short_ratio",
    "metrics_age_seconds", "exit_price", "gross_pnl", "fees", "net_pnl", "result_r",
    "holding_duration_seconds", "mfe_pct", "mae_pct",
)


def _classify_hardening(git_commit: str) -> str:
    """PRE_HARDENING / POST_HARDENING / UNKNOWN. Never guessed from string equality
    alone - uses real git ancestry against the hardening boundary commit.

    POST_HARDENING requires the boundary commit to be an ancestor of (or equal
    to) the trade's commit - i.e. the trade's commit happened at or after
    hardening landed. PRE_HARDENING requires the reverse. Neither is claimed
    on a plain string-length/prefix comparison, and an inconclusive git answer
    (sibling branch, unknown commit) stays UNKNOWN rather than being guessed.
    """
    if not git_commit:
        return "UNKNOWN"
    if git_commit.strip().lower() == HARDENING_BOUNDARY_SHA.strip().lower():
        return "POST_HARDENING"
    if ssrc.is_ancestor(HARDENING_BOUNDARY_SHA, git_commit) == "YES":
        return "POST_HARDENING"
    if ssrc.is_ancestor(git_commit, HARDENING_BOUNDARY_SHA) == "YES":
        return "PRE_HARDENING"
    return "UNKNOWN"


def load_closed_trades() -> tuple[list[dict[str, Any]], Any]:
    loaded = ssrc.load_csv("data_store/forward_paper_outcomes.csv", limit=None)
    rows: list[dict[str, Any]] = []
    for raw in (loaded.value or []):
        row: dict[str, Any] = dict(raw)
        for key in _OUTCOME_NUMERIC_FIELDS:
            row[key] = _num(raw.get(key))
        row["metrics_available"] = _bool(raw.get("metrics_available"))
        row["metrics_stale"] = _bool(raw.get("metrics_stale"))
        row["hardening"] = _classify_hardening(raw.get("git_commit", ""))
        row["is_win"] = (row["net_pnl"] is not None and row["net_pnl"] > REAL_NUMERIC_TOLERANCE)
        row["is_loss"] = (row["net_pnl"] is not None and row["net_pnl"] < -REAL_NUMERIC_TOLERANCE)
        rows.append(row)
    rows.sort(key=lambda r: r.get("exit_timestamp") or "", reverse=True)
    return rows, loaded.provenance


_OPEN_TRACKED_EVENTS = ("MFE_UPDATE", "MAE_UPDATE", "STOP_UPDATED", "PARTIAL_EXIT",
                        "TP_TOUCH", "SL_TOUCH", "EXIT_REASON_TRANSITION", "PROFIT_LOCK_ACTIVATED")


def _symbol_of(payload: dict[str, Any]) -> str:
    if payload.get("symbol"):
        return str(payload["symbol"])
    try:
        return str(payload["strategy_features"]["market_context"]["instrument"]["symbol"])
    except (KeyError, TypeError):
        return "UNKNOWN"


def load_open_trades() -> tuple[list[dict[str, Any]], Any]:
    """Trades with a TRADE_OPENED event and no TRADE_CLOSED event yet, folded
    forward through their own event stream for current display state."""
    loaded = ssrc.load_jsonl("data_store/forward_paper_events.jsonl", limit=None)
    by_trade: dict[str, list[dict[str, Any]]] = {}
    for event in (loaded.value or []):
        tid = event.get("trade_id")
        if tid:
            by_trade.setdefault(tid, []).append(event)

    open_trades: list[dict[str, Any]] = []
    for trade_id, events in by_trade.items():
        events_sorted = sorted(events, key=lambda e: e.get("sequence", 0))
        opened = next((e for e in events_sorted if e.get("event_type") == "TRADE_OPENED"), None)
        closed = next((e for e in events_sorted if e.get("event_type") == "TRADE_CLOSED"), None)
        if opened is None or closed is not None:
            continue
        payload = dict(opened.get("payload") or {})
        state: dict[str, Any] = {
            "trade_id": trade_id,
            "symbol": _symbol_of(payload),
            "direction": payload.get("direction"),
            "strategy": payload.get("strategy"),
            "source_candle_timestamp": payload.get("candidate_candle_open_timestamp_ms"),
            # signal_timestamp and decision_timestamp are distinct fields in the
            # real schema (decision can trail signal by minutes, once portfolio
            # selection runs across several candidates) - never conflate them.
            "signal_timestamp": payload.get("signal_timestamp"),
            "decision_timestamp": payload.get("decision_timestamp"),
            "signal_price": _num(payload.get("planned_entry")),
            "intended_entry_price": _num(payload.get("planned_entry")),
            "hypothetical_fill_price": _num(payload.get("simulated_fill")),
            "signal_to_fill_bps": _num(payload.get("signal_to_fill_bps")),
            "spread_bps": _num(payload.get("spread_bps")),
            "position_size": _num(payload.get("position_size")),
            "stop_loss": _num(payload.get("initial_stop")),
            "take_profit": (payload.get("initial_targets") or [None])[0],
            "git_commit": payload.get("git_commit"),
            "hardening": _classify_hardening(payload.get("git_commit", "")),
            "opened_at": opened.get("timestamp"),
            "current_mark_price": None,
            "latest_mfe_pct": None,
            "latest_mae_pct": None,
            "latest_excursion_at": None,
            # Entry-time metrics context - captured once, at TRADE_OPENED, not
            # something that only exists once a trade closes.
            "metrics_available": _bool(payload.get("metrics_available")),
            "metrics_stale": _bool(payload.get("metrics_stale")),
            "metrics_age_seconds": _num(payload.get("metrics_age_seconds")),
            "binance_metrics_timestamp": payload.get("binance_metrics_timestamp"),
            "open_interest": _num(payload.get("open_interest")),
            "open_interest_change_5m": _num(payload.get("open_interest_change_5m")),
            "open_interest_change_15m": _num(payload.get("open_interest_change_15m")),
            "taker_buy_sell_volume_ratio": _num(payload.get("taker_buy_sell_volume_ratio")),
            "top_trader_long_short_ratio": _num(payload.get("top_trader_long_short_ratio")),
            "global_long_short_ratio": _num(payload.get("global_long_short_ratio")),
        }
        for event in events_sorted:
            etype = event.get("event_type")
            p = event.get("payload") or {}
            if etype == "MFE_UPDATE":
                state["latest_mfe_pct"] = p.get("excursion_pct")
                state["current_mark_price"] = p.get("price", state["current_mark_price"])
                state["latest_excursion_at"] = event.get("timestamp")
            elif etype == "MAE_UPDATE":
                state["latest_mae_pct"] = p.get("excursion_pct")
                state["current_mark_price"] = p.get("price", state["current_mark_price"])
                state["latest_excursion_at"] = event.get("timestamp")
            elif etype == "STOP_UPDATED":
                state["stop_loss"] = _num(p.get("new_stop", state["stop_loss"]))
        try:
            opened_dt = datetime.fromisoformat(str(state["opened_at"]).replace("Z", "+00:00"))
            state["trade_age_seconds"] = max(0.0, time.time() - opened_dt.timestamp())
        except (ValueError, TypeError):
            state["trade_age_seconds"] = None
        open_trades.append(state)

    open_trades.sort(key=lambda t: t.get("opened_at") or "", reverse=True)
    return open_trades, loaded.provenance


# --- aggregation -------------------------------------------------------
#
# Entry-time telemetry (signal_to_fill_bps, spread_bps, metrics_available, OI,
# etc.) is captured once, at TRADE_OPENED - it does not require a trade to
# close. Every function below that aggregates "post-fix trades" or execution
# quality therefore takes BOTH open and closed trades, never closed alone.

def _all_opened(open_trades: list[dict[str, Any]], closed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list(open_trades) + list(closed)


def build_execution_quality(open_trades: list[dict[str, Any]],
                             closed: list[dict[str, Any]]) -> dict[str, Any]:
    def stats_for(rows: list[dict[str, Any]]) -> dict[str, Any]:
        s2f = [r["signal_to_fill_bps"] for r in rows if r.get("signal_to_fill_bps") is not None]
        spread = [r["spread_bps"] for r in rows if r.get("spread_bps") is not None]
        return {
            "n": len(rows),
            "signal_to_fill": _stats(s2f),
            "spread_avg": (sum(spread) / len(spread)) if spread else None,
            "spread_median": _percentile(spread, 0.5),
            "favorable_fill_count": sum(1 for v in s2f if v is not None and v <= 0),
            "adverse_fill_count": sum(1 for v in s2f if v is not None and v > 0),
        }

    opened = _all_opened(open_trades, closed)
    pre = [r for r in opened if r["hardening"] == "PRE_HARDENING"]
    post = [r for r in opened if r["hardening"] == "POST_HARDENING"]
    return {
        "all_opened": stats_for(opened),
        "pre_hardening": stats_for(pre),
        "post_hardening": stats_for(post),
        "historical_reference_bps": HISTORICAL_ADVERSE_SELECTION_REFERENCE_BPS,
        "reference_label": "REFERENCE_ONLY_NOT_UNIVERSAL",
    }


def build_metrics_panel(open_trades: list[dict[str, Any]],
                         closed: list[dict[str, Any]]) -> dict[str, Any]:
    """POST_FIX_* counters over every post-fix trade ever OPENED - open or
    closed - never gated on a trade having closed. Definitions:
      OPENED_TOTAL   = post-fix trades opened (open + closed)
      CURRENTLY_OPEN = post-fix trades not yet closed
      CLOSED_TOTAL   = post-fix trades already closed
      WITH_METRICS   = post-fix OPENED trades whose entry-time metrics
                       context was successfully captured (metrics_available
                       True at TRADE_OPENED)
      WITHOUT_METRICS = post-fix OPENED trades lacking that context
    """
    post_open = [r for r in open_trades if r["hardening"] == "POST_HARDENING"]
    post_closed = [r for r in closed if r["hardening"] == "POST_HARDENING"]
    post_all = post_open + post_closed
    with_metrics = [r for r in post_all if r.get("metrics_available") is True]
    without_metrics = [r for r in post_all if r.get("metrics_available") is not True]

    if not post_all:
        validation_status = "WAITING_FOR_FIRST_ORGANIC_POST_FIX_TRADE"
    elif with_metrics:
        validation_status = "METRICS_OBSERVED_ON_POST_FIX_TRADES"
    else:
        validation_status = "POST_FIX_TRADES_EXIST_BUT_NO_METRICS_YET"

    return {
        "post_fix_trades_opened_total": len(post_all),
        "post_fix_trades_currently_open": len(post_open),
        "post_fix_trades_closed_total": len(post_closed),
        "post_fix_trades_with_metrics": len(with_metrics),
        "post_fix_trades_without_metrics": len(without_metrics),
        "metrics_availability_rate": (len(with_metrics) / len(post_all)) if post_all else None,
        "validation_status": validation_status,
        "rows": post_all,
    }


def build_summary(runtime: ShadowRuntime, closed: list[dict[str, Any]],
                   open_trades: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(1 for r in closed if r["is_win"])
    losses = sum(1 for r in closed if r["is_loss"])
    gross = [r["gross_pnl"] for r in closed if r.get("gross_pnl") is not None]
    net = [r["net_pnl"] for r in closed if r.get("net_pnl") is not None]
    return {
        "engine_status": runtime.status,
        "engine_status_reason": runtime.status_reason,
        "pid": runtime.pid,
        "runtime_sha": runtime.runtime_sha,
        "expected_sha": EXPECTED_SHADOW_SHA,
        "sha_match": runtime.sha_match,
        "last_successful_scan_utc": runtime.last_successful_scan_utc,
        "seconds_since_last_scan": runtime.seconds_since_last_scan,
        "market_data_receiving": runtime.market_data_receiving,
        "signal_loop_running": runtime.signal_loop_running,
        "signals_total": None,  # no distinct "signal" record exists separate from
                                  # trade_plans.csv (every scanned candidate, not
                                  # just tradeable ones) - reported as UNKNOWN
                                  # rather than conflating plans with signals.
        "trades_opened_total": len(closed) + len(open_trades),
        "trades_closed_total": len(closed),
        "trades_currently_open": len(open_trades),
        "winners": wins,
        "losers": losses,
        "cumulative_gross_pnl": sum(gross) if gross else None,
        "cumulative_net_pnl": sum(net) if net else None,
        "proven_alpha": "NO",
        "proven_net_edge": "NO",
        "mode": "SHADOW_ONLY",
    }


# --- production-side safety (reused, not duplicated logic) ----------------

def build_production_safety() -> dict[str, Any]:
    sha = psrc.repo_head()
    stop = psrc.file_provenance("state/live.stop")
    supervisor = psrc.file_provenance("state/supervisor.stop")
    engine_pids = psrc.matching_pids("app.main")
    # A pid matching "app.main" may belong to the shadow engine (same module,
    # different checkout) - only count it as production if its cwd matches.
    production_engine_running = False
    for pid in engine_pids:
        cwd = ssrc.process_cwd(pid)
        if cwd and os.path.realpath(cwd) == os.path.realpath(str(psrc.BASE_PATH)):
            production_engine_running = True
            break
    return {
        "production_sha": sha,
        "expected_production_sha": EXPECTED_PRODUCTION_SHA,
        "production_sha_match": _sha_match(sha, EXPECTED_PRODUCTION_SHA) if sha else None,
        "production_engine_running": production_engine_running,
        "live_stop_present": stop.exists,
        "supervisor_stop_present": supervisor.exists,
        "production_worktree_clean": psrc.worktree_status().get("clean"),
    }


def build_real_exchange_safety() -> dict[str, Any]:
    """Local-only fallback: this dashboard does not make an authenticated exchange
    call, so it must never claim to be showing live exchange truth."""
    positions = psrc.load_json("state/position_events.json", default={})
    orders = psrc.load_json("state/order_intents.json", default={})

    def _last_status(loaded_value: Any) -> str | None:
        items = loaded_value.get("data", loaded_value) if isinstance(loaded_value, dict) else loaded_value
        if isinstance(items, list) and items:
            return items[-1].get("status")
        return None

    return {
        "source": "LOCAL_ONLY",
        "freshness": "NOT_VERIFIED",
        "last_known_position_status": _last_status(positions.value),
        "positions_provenance": positions.provenance.as_dict() if hasattr(positions.provenance, "as_dict") else None,
        "note": "No authenticated exchange read performed. This reflects local "
                "bookkeeping only, not a fresh exchange truth check.",
    }


# --- overall status ---------------------------------------------------------

def overall_status(runtime: ShadowRuntime, prod_safety: dict[str, Any],
                    real_safety: dict[str, Any], metrics_panel: dict[str, Any]) -> Status:
    if prod_safety["production_engine_running"]:
        return Status.OFFLINE  # RED: unexpected production activity
    if not prod_safety["live_stop_present"] or not prod_safety["supervisor_stop_present"]:
        return Status.OFFLINE  # RED: missing stop flag
    if prod_safety["production_sha_match"] is False:
        return Status.OFFLINE  # RED: wrong production SHA
    if runtime.sha_match == "NO":
        return Status.OFFLINE  # RED: wrong shadow runtime SHA
    if runtime.status in (Status.OFFLINE,):
        return Status.OFFLINE  # RED: shadow hung/stopped
    if runtime.status is Status.UNKNOWN:
        return Status.UNKNOWN
    if runtime.status is Status.STALE:
        return Status.DEGRADED  # AMBER
    if real_safety["source"] == "LOCAL_ONLY":
        return Status.DEGRADED  # AMBER: exchange state not freshly verified
    if metrics_panel["validation_status"] == "WAITING_FOR_FIRST_ORGANIC_POST_FIX_TRADE":
        return Status.DEGRADED  # AMBER: expected, not a defect
    return Status.HEALTHY


def build() -> dict[str, Any]:
    runtime = load_runtime()
    closed, closed_prov = load_closed_trades()
    open_trades, open_prov = load_open_trades()
    execution_quality = build_execution_quality(open_trades, closed)
    metrics_panel = build_metrics_panel(open_trades, closed)
    summary = build_summary(runtime, closed, open_trades)
    prod_safety = build_production_safety()
    real_safety = build_real_exchange_safety()
    status = overall_status(runtime, prod_safety, real_safety, metrics_panel)

    return {
        "root_available": runtime.root_available,
        "root_error": runtime.root_error,
        "runtime": runtime,
        "summary": summary,
        "open_trades": open_trades,
        "open_trades_provenance": open_prov,
        "closed_trades": closed,
        "closed_trades_provenance": closed_prov,
        "execution_quality": execution_quality,
        "metrics_panel": metrics_panel,
        "production_safety": prod_safety,
        "real_exchange_safety": real_safety,
        "hardening_boundary_sha": HARDENING_BOUNDARY_SHA,
        "overall": status,
        "generated_at": datetime.now(timezone.utc),
    }


__all__ = ["build", "load_runtime", "load_closed_trades", "load_open_trades",
           "EXPECTED_SHADOW_SHA", "HARDENING_BOUNDARY_SHA", "EXPECTED_PRODUCTION_SHA"]
