"""Watchdog status: read state/watchdog_status.json, the file scripts/wd_extended.py
(driven by scripts/watchdog_live.sh, scheduled via com.cgc.watchdog) writes.

This panel does not evaluate anything itself — it is a read-only window onto
what the watchdog already decided, same as every other Dashboard V3 panel.
"""

from __future__ import annotations

from typing import Any

from dashboard_v3.core import sources as src
from dashboard_v3.core.status import Signal, SignalSet, Status, freshness, worst

OVERALL_TO_STATUS = {
    "GREEN": Status.HEALTHY,
    "YELLOW": Status.DEGRADED,
    "RED": Status.OFFLINE,
}


def build() -> dict[str, Any]:
    signals = SignalSet()
    loaded = src.load_json("state/watchdog_status.json", default={})
    payload = loaded.value if isinstance(loaded.value, dict) else {}

    if not loaded.provenance.exists:
        signals.add(Signal("watchdog", "Watchdog", Status.UNKNOWN,
                           "state/watchdog_status.json absent",
                           "scripts/wd_extended.py has not written a status file yet."))
        return {"signals": signals, "status": Status.UNKNOWN, "payload": {},
                "provenance": loaded.provenance, "schedule_status": Status.UNKNOWN}

    age = loaded.provenance.age_seconds
    schedule_status = freshness(age, stale_after=180, offline_after=900)
    signals.add(Signal(
        "schedule", "Watchdog schedule", schedule_status,
        f"last write {loaded.provenance.age_label} ago" if loaded.provenance.exists else "never run",
        "com.cgc.watchdog runs scripts/watchdog_live.sh every 60s; a gap this large "
        "means the launchd job stopped, or the host slept through it.",
    ))

    overall = str(payload.get("overall_health") or "UNKNOWN")
    overall_status = OVERALL_TO_STATUS.get(overall, Status.UNKNOWN)
    reasons = payload.get("overall_health_reasons") or []
    signals.add(Signal(
        "overall", f"Watchdog: {overall}", overall_status,
        "; ".join(reasons) if reasons else "no active findings",
    ))

    return {
        "signals": signals,
        "status": worst(overall_status, schedule_status),
        "schedule_status": schedule_status,
        "overall_health": overall,
        "overall_health_reasons": reasons,
        "payload": payload,
        "provenance": loaded.provenance,
    }


__all__ = ["build"]
