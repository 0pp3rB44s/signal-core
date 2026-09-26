"""The capacity gate for NEW entries: the capacity monitor must be alive and quiet, or no new ladder starts.

The monitor (`lsrl.tools.capacity_monitor`) is a separate process: a dead monitor never stops a collection, so the thing that acts on it has to
treat "no fresh heartbeat" as an alarm. This gate reads the two files the monitor publishes (`monitor_status.json` = its own heartbeat, `ALARM.json` =
the currently active alarms) and answers ONE question: may a NEW entry be placed? It never blocks protection: stops, targets, cancels and the
verified flatten are not entries. Unknown (missing, stale, unparseable) is CLOSED.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable


class MonitorCapacityGate:
    MIN_STALE_S = 180.0

    def __init__(self, monitor_dir: str | Path, *, now: Callable[[], float] = time.time) -> None:
        self.dir, self._now = Path(monitor_dir), now

    def _read(self, name: str) -> dict | None:
        try:
            doc = json.loads((self.dir / name).read_text())
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) else None

    def blocked_reason(self) -> str | None:
        status = self._read("monitor_status.json")
        if status is None or status.get("alive") is not True:
            return "MONITOR_STATUS_UNAVAILABLE"
        stamp, interval = status.get("t"), status.get("interval_s")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or isinstance(interval, bool) \
                or not isinstance(interval, (int, float)) or interval <= 0:
            return "MONITOR_STATUS_MALFORMED"
        age = self._now() - stamp
        if not -5.0 <= age <= max(3 * interval, self.MIN_STALE_S):
            return f"MONITOR_STALE:{age:.0f}s"
        alarms = self._read("ALARM.json")
        if alarms is None or not isinstance(alarms.get("active"), dict):
            return "ALARM_FILE_UNREADABLE"
        if alarms["active"]:
            return "ACTIVE_ALARMS:" + ",".join(sorted(alarms["active"]))
        return None
