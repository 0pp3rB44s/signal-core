"""Incident centre: alert history, grouped into incidents with duration."""

from __future__ import annotations

import collections
import re
from datetime import datetime, timezone
from typing import Any

from dashboard_v3.core import sources as src
from dashboard_v3.core.status import Signal, SignalSet, Status

#: "[SEV] EVENT | message | host=... | commit=... | 2026-07-28T20:43:02Z"
LINE = re.compile(
    r"^\[(?P<sev>[A-Z]+)\]\s+(?P<event>[A-Z0-9_]+)\s*\|\s*(?P<msg>.*?)\s*\|\s*"
    r"host=(?P<host>\S*)\s*\|\s*commit=(?P<commit>\S*)\s*\|\s*(?P<ts>\S+)$"
)

SEVERITY_STATUS = {
    "CRITICAL": Status.OFFLINE,
    "HIGH": Status.DEGRADED,
    "WARN": Status.STALE,
    "INFO": Status.HEALTHY,
}

#: Alerts whose condition is a forward-paper artefact. In a LIVE deployment these
#: fire on every watchdog run and are not real incidents.
FORWARD_PAPER_NOISE = {"SUPERVISOR_DOWN", "MONITOR_DEAD", "HEALTH_DEGRADED"}

ACTIVE_WINDOW_SECONDS = 3600.0


def _parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def build() -> dict[str, Any]:
    signals = SignalSet()
    loaded = src.load_text_tail("logs/alerts.log", lines=600)
    lines = loaded.value if isinstance(loaded.value, list) else []

    grouped: dict[str, dict[str, Any]] = {}
    unparsed = 0
    for raw in lines:
        m = LINE.match(raw.strip())
        if not m:
            if raw.strip():
                unparsed += 1
            continue
        event = m.group("event")
        ts = _parse_ts(m.group("ts"))
        entry = grouped.setdefault(event, {
            "event": event,
            "severity": m.group("sev"),
            "message": m.group("msg"),
            "count": 0,
            "first_seen": ts,
            "last_seen": ts,
            "commits": set(),
        })
        entry["count"] += 1
        entry["message"] = m.group("msg")
        if ts:
            if entry["first_seen"] is None or ts < entry["first_seen"]:
                entry["first_seen"] = ts
            if entry["last_seen"] is None or ts > entry["last_seen"]:
                entry["last_seen"] = ts
        if m.group("commit"):
            entry["commits"].add(m.group("commit"))

    now = datetime.now(timezone.utc)
    incidents = []
    for entry in grouped.values():
        last = entry["last_seen"]
        age = (now - last).total_seconds() if last else None
        active = age is not None and age <= ACTIVE_WINDOW_SECONDS
        duration = ((entry["last_seen"] - entry["first_seen"]).total_seconds()
                    if entry["first_seen"] and entry["last_seen"] else 0.0)
        incidents.append({
            "event": entry["event"],
            "severity": entry["severity"],
            "message": entry["message"],
            "count": entry["count"],
            "first_seen": entry["first_seen"],
            "last_seen": entry["last_seen"],
            "age_seconds": age,
            "duration_seconds": duration,
            "active": active,
            "status": SEVERITY_STATUS.get(entry["severity"], Status.UNKNOWN),
            "commits": sorted(entry["commits"]),
            "noise": entry["event"] in FORWARD_PAPER_NOISE,
        })
    incidents.sort(key=lambda i: (not i["active"], -(i["count"])))

    active = [i for i in incidents if i["active"]]
    real_active = [i for i in active if not i["noise"]]

    alert_env = src.BASE_PATH / "state" / "alerting.env"
    delivery_configured = alert_env.exists()
    signals.add(Signal(
        "delivery", "Alert delivery",
        Status.HEALTHY if delivery_configured else Status.DEGRADED,
        "provider configured" if delivery_configured
        else "no provider — alerts reach logs/alerts.log only",
        "scripts/alert_config.sh validate reports the exact configuration state.",
    ))

    wd = src.file_provenance("state/watchdog_live_heartbeat.json")
    signals.add(Signal(
        "watchdog_sched", "Watchdog schedule",
        Status.HEALTHY if wd.exists and (wd.age_seconds or 1e9) < 180 else Status.DEGRADED,
        f"last run {wd.age_label} ago" if wd.exists else "never run",
        "com.cgc.watchdog invokes scripts/watchdog_live.sh every 60s via launchd.",
    ))

    if real_active:
        signals.add(Signal("active", "Active incidents", Status.BLOCKED,
                           f"{len(real_active)} active in the last hour"))
    else:
        signals.add(Signal("active", "Active incidents", Status.HEALTHY,
                           "none in the last hour"))

    return {
        "signals": signals,
        "status": signals.status,
        "incidents": incidents,
        "active": active,
        "real_active": real_active,
        "noise_count": sum(1 for i in incidents if i["noise"]),
        "unparsed": unparsed,
        "delivery_configured": delivery_configured,
        "provenance": loaded.provenance,
        "watchdog_provenance": wd,
        "blind_spot": (
            "A host-resident watchdog cannot report that its own host slept or lost "
            "power: launchd interval jobs do not run while asleep. Detecting that "
            "class of outage requires an off-host dead-man's switch."
        ),
    }


__all__ = ["FORWARD_PAPER_NOISE", "build"]
