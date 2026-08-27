#!/usr/bin/env python3
"""Extended production watchdog checks: AdaptiveTrend, exchange truth, position
protection, risk, deployment integrity, first-trade watch.

Companion to scripts/watchdog_live.sh, which owns engine-process, heartbeat and
log-tail checks in bash. This module owns everything that is naturally a JSON/
CSV read rather than a process probe, and everything that already has a
read-only implementation in dashboard_v3 — it reuses that implementation
instead of recomputing exchange truth, risk truth or deployment SHAs a second
way that could silently drift from what the dashboard shows.

READ-ONLY. This script imports dashboard_v3.core.assembly and
dashboard_v3.panels.*, whose own guard tests (tests/test_dashboard_v3.py)
already restrict exchange calls to a GET-only allowlist. It never imports the
order-placement or TP/SL-mutation clients, the execution package, or the risk
manager. It writes exactly two files of its own: state/watchdog_status.json
(the dashboard's watchdog source) and state/wd_extended_seen_positions.json
(its own first-trade-watch memory) — both watchdog-owned, neither read by the
trading engine. tests/test_wd_extended.py enforces this boundary by scanning
this file's source for the forbidden module names directly.

Usage:
  wd_extended.py --now <epoch>              evaluate, persist, alert
  wd_extended.py --now <epoch> --dry-run     evaluate and print only
  wd_extended.py --now <epoch> --no-deliver  evaluate and persist, never alert
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

STATE_DIR = REPO / (os.environ.get("WD_STATE_DIR", "state"))
STATUS_FILE = STATE_DIR / "watchdog_status.json"
SEEN_POSITIONS_FILE = STATE_DIR / "wd_extended_seen_positions.json"
CYCLE_FILE = STATE_DIR / "wd_extended_cycle.json"
ALERT_STATE = STATE_DIR / "watchdog_alerts.json"

#: Exchange calls (account, positions, TP/SL, pending orders) are bounded to
#: once every N invocations of this script. At the watchdog's 60s cadence that
#: is one Bitget round-trip every WD_EXCHANGE_EVERY_N minutes, independent of
#: how often the dashboard itself is being viewed. Local-file checks (engine,
#: heartbeat, AdaptiveTrend scan state, risk, deployment SHA, logging lag) are
#: free and run every invocation.
EXCHANGE_EVERY_N = int(os.environ.get("WD_EXCHANGE_EVERY_N", "5"))

# A position/order-intent condition open for longer than this is escalated
# from "transient, expected during a fill" to "stuck, needs a human".
UNRESOLVED_INTENT_GRACE_SEC = int(os.environ.get("WD_UNRESOLVED_INTENT_GRACE_SEC", "600"))
LOG_LAG_WARN_SEC = int(os.environ.get("WD_LOG_LAG_WARN_SEC", "900"))

DOC_LIKE_SUFFIXES = (".md", ".txt")
DOC_LIKE_PREFIXES = ("docs/", "Live_Release_v1/", "Obsidian", "RC2/")
DOC_LIKE_NAMES = {
    "README.md", "CHANGELOG.md", "ROADMAP.md", "MASTER_PLAN.md",
    "MASTER_STATUS_REPORT.md", "PROJECT_STATUS.md", "RELEASE_NOTES.md",
    "GO_LIVE_CHECKLIST.md", "GO_LIVE_RUNBOOK.md", "DAILY_OPERATIONS.md",
    "BEDIENING.md", "WORKING_BOT_EXECUTION_PATH.md",
}


def now_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


# --- alert plumbing: reuse wd_alert_state.py exactly as watchdog_live.sh does ---

def raise_alert(key: str, severity: str, message: str, *, now: int, no_deliver: bool,
                 dry_run: bool = False) -> None:
    if dry_run:
        print(f"      [ext] would alert: [{severity}] {key}")
        return
    subprocess.run(
        [sys.executable, str(REPO / "scripts" / "lib" / "wd_alert_state.py"), "raise",
         "--state", str(ALERT_STATE), "--key", key, "--severity", severity,
         "--message", message, "--now", str(now), "--cooldown", "1800",
         "--no-deliver", "1" if no_deliver else "0"],
        cwd=str(REPO), timeout=30, capture_output=True,
    )


def resolve_alerts(active_keys: list[str], *, now: int, no_deliver: bool, dry_run: bool = False) -> None:
    if dry_run:
        return
    subprocess.run(
        [sys.executable, str(REPO / "scripts" / "lib" / "wd_alert_state.py"), "resolve",
         "--state", str(ALERT_STATE), "--now", str(now),
         "--active", ",".join(active_keys), "--no-deliver", "1" if no_deliver else "0"],
        cwd=str(REPO), timeout=30, capture_output=True,
    )


def send_info(event: str, message: str, *, no_deliver: bool) -> None:
    """One-shot INFO notification, outside the raise/resolve dedup ladder —
    used for events (entry opened, position closed) that happen once and are
    never 'active' or 'resolved' in the alert-state sense."""
    if no_deliver:
        return
    script = (
        'set -uo pipefail; . scripts/lib/alert.sh; '
        'send_alert "$1" "$2" "$3" >/dev/null 2>&1'
    )
    try:
        subprocess.run(["bash", "-c", script, "_", "INFO", event, message],
                        cwd=str(REPO), timeout=20, capture_output=True)
    except Exception:
        pass


# --- deployment SHA classification -------------------------------------------

def classify_deployment(runtime_sha: str, repo_head: str) -> str:
    if not runtime_sha or not repo_head:
        return "UNKNOWN"
    if runtime_sha == repo_head or runtime_sha.startswith(repo_head) or repo_head.startswith(runtime_sha):
        return "MATCH"
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", runtime_sha, repo_head],
            cwd=str(REPO), timeout=15, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return "UNKNOWN"
        changed = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    except Exception:
        return "UNKNOWN"
    if not changed:
        return "MATCH"

    def is_doc(path: str) -> bool:
        if path in DOC_LIKE_NAMES:
            return True
        if path.endswith(DOC_LIKE_SUFFIXES):
            return True
        return any(path.startswith(prefix) for prefix in DOC_LIKE_PREFIXES)

    return "DOCS_ONLY_DRIFT" if all(is_doc(p) for p in changed) else "FUNCTIONAL_MISMATCH"


# --- logging freshness --------------------------------------------------------

def log_lag_seconds(rel_path: str, *, now: float) -> float | None:
    path = REPO / rel_path
    if not path.exists():
        return None
    return now - path.stat().st_mtime


# --- health model: mission's GREEN/YELLOW/RED, distinct from dashboard Status ---

def compute_overall_health(*, engine_alive, heartbeat_stale, scan_stalled,
                            position_unprotected, hedge_detected,
                            exchange_mismatch, unresolved_intents_stuck,
                            duplicate_engine, recovery_pending,
                            dashboard_down, logging_lag, risk_freeze_active,
                            deploy_functional_mismatch, signal_stale) -> tuple[str, list[str]]:
    red_reasons = []
    if engine_alive is False:
        red_reasons.append("engine not running")
    if duplicate_engine:
        red_reasons.append("duplicate executor")
    if heartbeat_stale:
        red_reasons.append("heartbeat stale beyond hard threshold")
    if scan_stalled:
        red_reasons.append("scan cycles stopped advancing")
    if position_unprotected:
        red_reasons.append("open position lacks confirmed protection")
    if hedge_detected:
        red_reasons.append("hedge / second position detected")
    if exchange_mismatch:
        red_reasons.append("exchange truth unreachable or mismatched")
    if unresolved_intents_stuck:
        red_reasons.append("unresolved intents beyond safe transient window")
    if recovery_pending:
        red_reasons.append("startup recovery stuck")
    if red_reasons:
        return "RED", red_reasons

    yellow_reasons = []
    if dashboard_down:
        yellow_reasons.append("dashboard down")
    if logging_lag:
        yellow_reasons.append("logging lag")
    if deploy_functional_mismatch:
        yellow_reasons.append("deployment SHA functional mismatch")
    # Risk freeze and signal inactivity are explicitly NOT engine-health
    # findings — trade eligibility is tracked separately in the status file.
    if yellow_reasons:
        return "YELLOW", yellow_reasons

    return "GREEN", []


def build_status(*, now: float, no_deliver: bool, dry_run: bool = False) -> dict[str, Any]:
    from dashboard_v3.core import assembly, sources as src
    from dashboard_v3.core.status import Status
    from dashboard_v3.panels import adaptive_trend as at

    cycle = load_json(CYCLE_FILE, default={"n": 0}) or {"n": 0}
    cycle_n = int(cycle.get("n", 0)) + 1
    run_exchange = (cycle_n % EXCHANGE_EVERY_N) == 1
    prev_status = load_json(STATUS_FILE, default={}) or {}

    assembly.invalidate() if run_exchange else None
    data = assembly.build_all()
    panels = data["panels"]
    runtime = panels.get("runtime") or {}
    exch = panels.get("exchange") or {}
    ops = panels.get("operations") or {}
    permission = data.get("permission") or {}

    if not run_exchange and prev_status.get("exchange"):
        # Reuse the last fetched exchange truth rather than forcing a call —
        # bounds Bitget calls to once per EXCHANGE_EVERY_N cycles.
        exch_view = prev_status["exchange"]
        exch_view["reused_from"] = prev_status.get("timestamp")
    else:
        exch_view = None  # computed below from the fresh panel

    adaptive = at.build(now=datetime.fromtimestamp(now, tz=timezone.utc))
    scan_state_raw = (src.load_json("state/adaptive_trend_scan_state.json", default={}).value or {})
    scan_state_data = scan_state_raw.get("data") if isinstance(scan_state_raw, dict) else {}
    last_6h_candle = {
        sym: {
            "epoch_ms": (scan_state_data or {}).get(sym),
            "utc": (
                datetime.fromtimestamp((scan_state_data or {}).get(sym) / 1000.0, tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")
                if isinstance((scan_state_data or {}).get(sym), (int, float)) else None
            ),
        }
        for sym in adaptive.get("universe", [])
    }

    engine = runtime.get("engine") or {}
    engine_alive = engine.get("alive")
    duplicate_engine = len(engine.get("live_pids") or []) > 1
    hb = runtime.get("heartbeat") or {}
    hb_prov = runtime.get("heartbeat_prov")
    hb_age = getattr(hb_prov, "age_seconds", None)
    heartbeat_stale = bool(hb_age is not None and hb_age > 600)
    scan_started = hb.get("scan_cycles_started")
    scan_completed = hb.get("scan_cycles_completed")
    scan_stalled = bool(
        engine_alive and hb_age is not None and hb_age > 900
        and isinstance(scan_started, int) and isinstance(scan_completed, int)
    )

    positions = exch.get("positions") or []
    reachable = exch.get("reachable")
    unprotected = [p for p in positions if p.get("protection") != "PROTECTED"]
    by_symbol: dict[str, set[str]] = {}
    for p in positions:
        by_symbol.setdefault(p.get("symbol", "UNKNOWN"), set()).add(p.get("side", "UNKNOWN"))
    hedge_detected = any(len(sides) > 1 for sides in by_symbol.values())
    second_position = len(positions) > 1
    exchange_mismatch = reachable is False

    unresolved_intents = exch.get("unresolved_intents") or 0
    prev_first_seen = ((prev_status or {}).get("safety") or {}).get("_unresolved_first_seen")
    if unresolved_intents:
        unresolved_first_seen = prev_first_seen if prev_first_seen is not None else now
    else:
        unresolved_first_seen = None
    recovery_pending = bool(unresolved_intents) and unresolved_first_seen is not None and (
        (now - float(unresolved_first_seen)) > UNRESOLVED_INTENT_GRACE_SEC
    )

    dashboard_down = None  # left to watchdog_live.sh's own loopback probe
    lag_agent = log_lag_seconds("logs/agent.log", now=now)
    lag_live = log_lag_seconds("logs/live.out", now=now)
    logging_lag = bool(
        (lag_agent is not None and lag_agent > LOG_LAG_WARN_SEC)
        or (lag_live is not None and lag_live > LOG_LAG_WARN_SEC)
    )

    deployment = ops.get("deployment") or {}
    shas = (deployment.get("sha") or {}).get("shas") or {}
    runtime_sha = str(shas.get("runtime") or "")
    repo_head = str(shas.get("runner") or src.repo_head() or "")
    deploy_class = classify_deployment(runtime_sha, repo_head)

    risk = ops.get("risk") or {}
    freeze_active = risk.get("weekly_frozen")

    overall, red_yellow_reasons = compute_overall_health(
        engine_alive=engine_alive, heartbeat_stale=heartbeat_stale, scan_stalled=scan_stalled,
        position_unprotected=bool(unprotected) and reachable is not False,
        hedge_detected=hedge_detected or second_position,
        exchange_mismatch=exchange_mismatch,
        unresolved_intents_stuck=recovery_pending,
        duplicate_engine=duplicate_engine, recovery_pending=recovery_pending,
        dashboard_down=False, logging_lag=logging_lag,
        risk_freeze_active=bool(freeze_active),
        deploy_functional_mismatch=(deploy_class == "FUNCTIONAL_MISMATCH"),
        signal_stale=adaptive.get("signal_status") is Status.STALE,
    )

    status = {
        "timestamp": now_iso(now),
        "watchdog_pid": os.getpid(),
        "watchdog_version": "1.0.0",
        "watchdog_sha": repo_head[:12] if repo_head else None,
        "overall_health": overall,
        "overall_health_reasons": red_yellow_reasons,
        "engine": {
            "status": "ALIVE" if engine_alive else ("DEAD" if engine_alive is False else "UNKNOWN"),
            "pid": engine.get("pid"),
            "heartbeat_age": hb_age,
            "uptime": engine.get("uptime_seconds"),
            "scan_age": hb_age,
            "cycles_started": scan_started,
            "cycles_completed": scan_completed,
            "duplicate_engine": duplicate_engine,
        },
        "strategy": {
            "active_strategy": adaptive.get("strategy_id"),
            "adaptive_enabled": permission.get("live_entry_enabled"),
            "last_6h_candle": last_6h_candle,
            "next_6h_boundary": str(adaptive.get("next_boundary")) if adaptive.get("next_boundary") else None,
            "last_signal": str(adaptive.get("last_signal_at")) if adaptive.get("last_signal_at") else None,
            "last_signal_age_seconds": adaptive.get("last_signal_age_seconds"),
            "signal_status": getattr(adaptive.get("signal_status"), "value", str(adaptive.get("signal_status"))),
            "trade_eligible": permission.get("ready"),
            "blocker": permission.get("primary_blocker").label if permission.get("primary_blocker") else None,
        },
        "risk": {
            "mode": "FROZEN" if freeze_active else ("ACTIVE" if freeze_active is False else "UNKNOWN"),
            "daily_pnl": risk.get("daily_realized_loss"),
            "weekly_pnl": risk.get("weekly_realized_pnl"),
            "weekly_loss_pct": risk.get("weekly_loss_pct"),
            "freeze_active": freeze_active,
        },
        "exchange": exch_view or {
            "reachable": reachable,
            "equity": (exch.get("account") or {}).get("equity"),
            "positions": len(positions),
            "orders": exch.get("normal_order_count"),
            "plan_orders": exch.get("plan_order_count"),
        },
        "safety": {
            "unresolved_intents": unresolved_intents,
            "startup_recovery_pending": recovery_pending,
            "position_protected": not unprotected if reachable is not False else None,
            "unprotected_count": len(unprotected),
            "hedge_detected": hedge_detected,
            "second_position_detected": second_position,
            "_unresolved_first_seen": unresolved_first_seen,
        },
        "logging": {
            "agent_log_lag_seconds": lag_agent,
            "live_out_lag_seconds": lag_live,
        },
        "deployment": {
            "runtime_sha": runtime_sha or None,
            "runner_sha": repo_head or None,
            "classification": deploy_class,
        },
        "dashboard": {
            "status": "SEE_WATCHDOG_LIVE_SH",
        },
        "alerts": {
            "active_alerts": red_yellow_reasons,
        },
        "meta": {
            "exchange_checked_this_cycle": run_exchange,
            "exchange_check_every_n_cycles": EXCHANGE_EVERY_N,
            "cycle": cycle_n,
        },
    }

    # --- alerting for conditions bash cannot see ------------------------
    active_keys: list[str] = []
    if status["safety"]["unprotected_count"] and reachable is not False:
        active_keys.append("POSITION_UNPROTECTED")
        raise_alert("POSITION_UNPROTECTED", "CRITICAL",
                    f"{status['safety']['unprotected_count']} open position(s) without "
                    "confirmed exchange-side stop-loss+take-profit", now=int(now), no_deliver=no_deliver, dry_run=dry_run)
    if hedge_detected or second_position:
        active_keys.append("SECOND_POSITION_HEDGE")
        raise_alert("SECOND_POSITION_HEDGE", "CRITICAL",
                    f"{len(positions)} open position(s), hedge={hedge_detected} — "
                    "AdaptiveTrend v1 permits exactly one", now=int(now), no_deliver=no_deliver, dry_run=dry_run)
    if exchange_mismatch:
        active_keys.append("EXCHANGE_STATE_MISMATCH")
        raise_alert("EXCHANGE_STATE_MISMATCH", "CRITICAL",
                    f"Bitget unreachable: {', '.join(exch.get('errors') or []) or 'unknown error'}",
                    now=int(now), no_deliver=no_deliver, dry_run=dry_run)
    if recovery_pending:
        active_keys.append("RECOVERY_STUCK")
        raise_alert("RECOVERY_STUCK", "CRITICAL",
                    f"{unresolved_intents} unresolved intent(s) beyond "
                    f"{UNRESOLVED_INTENT_GRACE_SEC}s grace window", now=int(now), no_deliver=no_deliver, dry_run=dry_run)
    if logging_lag:
        active_keys.append("LOGGING_LAG")
        raise_alert("LOGGING_LAG", "WARN",
                    f"agent.log lag={lag_agent}s live.out lag={lag_live}s "
                    f"(threshold {LOG_LAG_WARN_SEC}s)", now=int(now), no_deliver=no_deliver, dry_run=dry_run)
    if deploy_class == "FUNCTIONAL_MISMATCH":
        active_keys.append("DEPLOY_FUNCTIONAL_MISMATCH")
        raise_alert("DEPLOY_FUNCTIONAL_MISMATCH", "HIGH",
                    f"running {runtime_sha[:7] if runtime_sha else '?'} vs repo HEAD "
                    f"{repo_head[:7] if repo_head else '?'} with non-doc changes between them",
                    now=int(now), no_deliver=no_deliver, dry_run=dry_run)

    resolve_alerts(active_keys, now=int(now), no_deliver=no_deliver, dry_run=dry_run)

    # --- first-trade / position-close watch ------------------------------
    watch_position_transitions(positions, reachable, now=now, no_deliver=no_deliver, dry_run=dry_run)

    if not dry_run:
        atomic_write_json(CYCLE_FILE, {"n": cycle_n})
    return status


def watch_position_transitions(positions: list[dict], reachable: bool | None, *, now: float,
                                no_deliver: bool, dry_run: bool = False) -> None:
    """flat -> open fires an ENTRY notification; open -> flat fires a CLOSE
    notification. State lives in its own watchdog-owned marker file so this
    never depends on, or writes to, trading state."""
    if reachable is False:
        return  # cannot trust a transition we could not observe
    seen = load_json(SEEN_POSITIONS_FILE, default={"open": {}}) or {"open": {}}
    prev_open = seen.get("open", {})
    current_open = {f"{p.get('symbol')}:{p.get('side')}": p for p in positions}

    for key, pos in current_open.items():
        if key not in prev_open:
            symbol, side = key.split(":", 1)
            msg = (
                f"symbol={symbol} side={side} entry={pos.get('entry')} "
                f"size={pos.get('size')} stop={pos.get('active_stop')} "
                f"margin={pos.get('margin')} leverage={pos.get('leverage')} "
                f"time={now_iso(now)}"
            )
            send_info("ADAPTIVETREND_LIVE_ENTRY", f"🟢 ADAPTIVETREND LIVE ENTRY | {msg}",
                       no_deliver=no_deliver)

    for key in prev_open:
        if key not in current_open:
            symbol, side = key.split(":", 1)
            close_row = _latest_close_row(symbol)
            if close_row:
                msg = (
                    f"symbol={symbol} side={side} net_pnl={close_row.get('net_pnl') or close_row.get('pnl')} "
                    f"closed_at={close_row.get('closed_at') or close_row.get('timestamp')}"
                )
            else:
                msg = f"symbol={symbol} side={side} — exchange-confirmed close, ledger row not yet written"
            send_info("ADAPTIVETREND_POSITION_CLOSED", f"⚪ ADAPTIVETREND POSITION CLOSED | {msg}",
                       no_deliver=no_deliver)

    if not dry_run:
        atomic_write_json(SEEN_POSITIONS_FILE, {"open": current_open, "updated_at": now_iso(now)})


def _latest_close_row(symbol: str) -> dict | None:
    path = REPO / "logs" / "trade_dataset_v2.csv"
    if not path.exists():
        return None
    best = None
    try:
        with path.open("r", newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if str(row.get("symbol", "")).upper() != symbol.upper():
                    continue
                ts = row.get("closed_at") or row.get("timestamp") or ""
                if best is None or ts > (best.get("closed_at") or best.get("timestamp") or ""):
                    best = row
    except Exception:
        return None
    return best


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--now", type=float, default=time.time())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-deliver", action="store_true")
    args = parser.parse_args()

    no_deliver = args.dry_run or args.no_deliver
    status = build_status(now=args.now, no_deliver=no_deliver, dry_run=args.dry_run)

    print(f"=== WATCHDOG(extended) {status['timestamp']} | overall={status['overall_health']} ===")
    for reason in status["overall_health_reasons"]:
        print(f"  [{status['overall_health']}] {reason}")

    if not args.dry_run:
        atomic_write_json(STATUS_FILE, status)
    else:
        print(json.dumps(status, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
