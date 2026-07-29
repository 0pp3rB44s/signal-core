"""Runtime, host and process state — the Command Centre's spine."""

from __future__ import annotations

import time
from typing import Any

from dashboard_v3.core import sources as src
from dashboard_v3.core.status import Signal, SignalSet, Status, freshness

ENGINE_PATTERN = r"[Pp]ython(3)?.*(-m )?app\.main"


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "UNKNOWN"
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}u {m}m"
    if h:
        return f"{h}u {m}m"
    return f"{m}m"


def build() -> dict[str, Any]:
    signals = SignalSet()

    runtime_state = src.read_kv_state("state/live_runtime.state")
    heartbeat = src.load_json("state/runtime_heartbeat.json", default={})
    watchdog = src.load_json("state/watchdog_heartbeat.json", default={})
    shutdown = src.load_json("state/last_shutdown.json", default={})

    # --- engine process -------------------------------------------------
    pid_loaded = src.load_json("state/bot.pid", default=None)
    raw_pid = None
    pid_file = src.BASE_PATH / "state" / "bot.pid"
    if pid_file.exists():
        try:
            raw_pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            raw_pid = None

    live_pids = src.matching_pids(ENGINE_PATTERN)
    alive = src.pid_alive(raw_pid)
    proc = src.process_info(raw_pid) if alive else {}

    if alive:
        signals.add(Signal("engine", "Engine", Status.HEALTHY,
                           f"PID {raw_pid} · uptime {_fmt_duration(proc.get('uptime_seconds'))}"))
    elif raw_pid:
        signals.add(Signal("engine", "Engine", Status.OFFLINE,
                           f"PID {raw_pid} in state/bot.pid is not alive",
                           "The engine is not running, or the pid file is stale."))
    else:
        signals.add(Signal("engine", "Engine", Status.UNKNOWN,
                           "no PID recorded in state/bot.pid"))

    # PID mismatch / duplicate engines — neither was detected by the old stack.
    if len(live_pids) > 1:
        signals.add(Signal("duplicate", "Duplicate engines", Status.DEGRADED,
                           f"{len(live_pids)} processes match app.main: {live_pids}",
                           "Two engines can double-submit. Investigate immediately."))
    elif live_pids and raw_pid and raw_pid not in live_pids:
        signals.add(Signal("duplicate", "PID mismatch", Status.DEGRADED,
                           f"bot.pid={raw_pid} but running engine is {live_pids[0]}",
                           "The pid file does not describe the running process."))
    elif live_pids:
        signals.add(Signal("duplicate", "Process identity", Status.HEALTHY,
                           "exactly one engine process"))
    else:
        signals.add(Signal("duplicate", "Process identity", Status.UNKNOWN,
                           "no engine process found"))

    # --- heartbeat ------------------------------------------------------
    hb = heartbeat.value if isinstance(heartbeat.value, dict) else {}
    hb_age = heartbeat.provenance.age_seconds
    if not heartbeat.provenance.exists:
        hb_status = Status.UNKNOWN
        hb_detail = "state/runtime_heartbeat.json absent"
    elif not heartbeat.provenance.parsed:
        hb_status = Status.DEGRADED
        hb_detail = heartbeat.provenance.error
    else:
        hb_status = freshness(hb_age, stale_after=600, offline_after=3600)
        hb_detail = f"{heartbeat.provenance.age_label} old · stage {hb.get('stage', 'UNKNOWN')}"
    signals.add(Signal("heartbeat", "Heartbeat", hb_status, hb_detail,
                       "A stale heartbeat means the engine is wedged, or the host slept."))

    # A schema-1 heartbeat predates the liveness fix: counters will not advance.
    schema = hb.get("schema_version")
    heartbeat_capable = schema is not None and int(schema or 0) >= 2
    if hb and not heartbeat_capable:
        signals.add(Signal(
            "heartbeat_schema", "Heartbeat telemetry", Status.DEGRADED,
            f"schema v{schema} — scan counters are not emitted in this build",
            "The running engine predates the liveness fix; heartbeat age is "
            "still meaningful but cycle counters stay at zero.",
        ))

    # --- host -----------------------------------------------------------
    boot = src.host_boot_epoch()
    host_uptime = time.time() - boot if boot else None
    signals.add(Signal(
        "host", "Host", Status.HEALTHY if boot else Status.UNKNOWN,
        f"awake {_fmt_duration(host_uptime)}" if boot else "boot time unavailable",
        "Host uptime cannot prove the host stayed awake — sleep does not reset it.",
    ))

    # --- commit drift ---------------------------------------------------
    running_commit = (runtime_state.value or {}).get("commit", "")
    head = src.repo_head()
    if running_commit and head:
        if running_commit == head:
            commit_status, commit_detail = Status.HEALTHY, f"{running_commit[:7]} matches HEAD"
        else:
            commit_status = Status.DEGRADED
            commit_detail = f"running {running_commit[:7]} · repo HEAD {head[:7]}"
    else:
        commit_status, commit_detail = Status.UNKNOWN, "commit unavailable"
    signals.add(Signal("commit", "Code version", commit_status, commit_detail,
                       "Unmerged fixes are not in the running process until restart."))

    # --- watchdog & alerting -------------------------------------------
    wd = watchdog.value if isinstance(watchdog.value, dict) else {}
    wd_age = watchdog.provenance.age_seconds
    wd_status = (Status.UNKNOWN if not watchdog.provenance.exists
                 else freshness(wd_age, stale_after=3600, offline_after=86400))
    signals.add(Signal("watchdog", "Watchdog", wd_status,
                       f"last run {watchdog.provenance.age_label} ago"
                       if watchdog.provenance.exists else "never run",
                       "scripts/watchdog.sh has no scheduler; it only runs when invoked."))

    alert_env = src.BASE_PATH / "state" / "alerting.env"
    alerting_configured = alert_env.exists()
    signals.add(Signal(
        "alerting", "Alert delivery",
        Status.HEALTHY if alerting_configured else Status.DEGRADED,
        "provider configured" if alerting_configured else "no provider — local log only",
        "Without a provider, incidents are written to logs/alerts.log and nobody is told.",
    ))

    return {
        "signals": signals,
        "status": signals.status,
        "engine": {
            "pid": raw_pid,
            "alive": alive,
            "live_pids": live_pids,
            "uptime_seconds": proc.get("uptime_seconds"),
            "uptime_label": _fmt_duration(proc.get("uptime_seconds")) if alive else "UNKNOWN",
            "rss_mb": round(proc.get("rss_kb", 0) / 1024, 1) if proc else None,
            "cpu_pct": proc.get("cpu_pct"),
            "started": proc.get("started"),
        },
        "runtime": runtime_state.value or {},
        "runtime_prov": runtime_state.provenance,
        "heartbeat": hb,
        "heartbeat_prov": heartbeat.provenance,
        "heartbeat_capable": heartbeat_capable,
        "watchdog": wd,
        "watchdog_prov": watchdog.provenance,
        "shutdown": shutdown.value or {},
        "host_uptime_label": _fmt_duration(host_uptime),
        "host_boot_epoch": boot,
        "running_commit": running_commit,
        "repo_head": head,
        "alerting_configured": alerting_configured,
    }


__all__ = ["build"]
