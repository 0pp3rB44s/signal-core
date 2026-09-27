"""Append-only, joinable forensic lifecycle events for LIVE v2."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


_lock = threading.Lock()
_IDENTITY_FIELDS = (
    "strategy_id", "plan_id", "candidate_id", "executor_id", "host_id", "pid",
    "production_sha", "credential_fingerprint", "client_id_namespace",
)


def emit_forensic_event(
    event_type: str,
    lifecycle: Mapping[str, Any],
    *,
    path: str = "logs/live_v2_forensics.jsonl",
    **fields: Any,
) -> bool:
    row = {
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event_type": str(event_type),
        "symbol": lifecycle.get("symbol"),
        "direction": lifecycle.get("direction"),
        **{name: lifecycle.get(name) for name in _IDENTITY_FIELDS},
        **fields,
    }
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, default=str, sort_keys=True)
        with _lock, target.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        return False
    return True


__all__ = ["emit_forensic_event"]
