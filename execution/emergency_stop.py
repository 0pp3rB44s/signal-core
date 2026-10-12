"""File-based EMERGENCY STOP for new entries (TradingBot MVP, E-INFRA).

Semantics (deliberately minimal):

* ENGAGED  -> ExecutionService refuses every new entry (see `_entry_guard_reason`).
              Existing positions keep being managed (SL/TP maintenance, protection
              repair, closes) - an emergency stop must never remove protection.
* The flag file is the single source of truth, so it survives restarts and works
  across processes (CLI, runner, dashboard exporter).
* FAIL-CLOSED: a flag file that exists but cannot be parsed counts as ENGAGED.
* Engaging is idempotent and never overwrites the original reason.
* Clearing is explicit (`clear`) and records who/why; nothing clears it implicitly.

Flattening real positions is a SEPARATE, explicit action (scripts/emergency_stop.py
flatten ...); engaging the stop never closes anything by itself.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

DEFAULT_PATH = "state/EMERGENCY_STOP.json"
ENV_PATH = "EMERGENCY_STOP_PATH"


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class EmergencyStop:
    def __init__(self, path: str | os.PathLike | None = None, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path or os.environ.get(ENV_PATH) or DEFAULT_PATH)
        self._clock = clock

    # ------------------------------------------------------------------ read
    def status(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"active": False, "reason": "", "engaged_by": None, "engaged_at_utc": None, "source": str(self.path), "corrupt": False}
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict):
                raise ValueError("flag file is not a JSON object")
            return {
                "active": True,
                "reason": str(data.get("reason") or "emergency stop engaged"),
                "engaged_by": data.get("engaged_by"),
                "engaged_at_utc": data.get("engaged_at_utc"),
                "source": str(self.path),
                "corrupt": False,
            }
        except Exception as exc:  # fail closed
            return {"active": True, "reason": f"emergency stop flag unreadable ({type(exc).__name__}); failing closed",
                    "engaged_by": None, "engaged_at_utc": None, "source": str(self.path), "corrupt": True}

    def is_active(self) -> bool:
        return bool(self.status()["active"])

    def reason(self) -> str:
        """Non-empty string when new entries must be refused."""
        s = self.status()
        return f"EMERGENCY_STOP: {s['reason']}" if s["active"] else ""

    # ----------------------------------------------------------------- write
    def engage(self, reason: str, by: str = "operator") -> dict[str, Any]:
        cur = self.status()
        if cur["active"] and not cur["corrupt"]:
            return cur  # idempotent: keep the original reason/time
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": "emergency_stop_v1", "reason": reason.strip() or "unspecified",
                   "engaged_by": by, "engaged_at_utc": _utc(self._clock())}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True))
        os.replace(tmp, self.path)
        return self.status()

    def clear(self, by: str, note: str = "") -> dict[str, Any]:
        """Remove the flag. Appends a record to <flag>.history.jsonl first."""
        if not by.strip():
            raise ValueError("clearing the emergency stop requires an identified operator")
        prev = self.status()
        if self.path.exists():
            hist = self.path.with_suffix(self.path.suffix + ".history.jsonl")
            with open(hist, "a") as f:
                f.write(json.dumps({"cleared_at_utc": _utc(self._clock()), "cleared_by": by, "note": note, "previous": prev}, sort_keys=True) + "\n")
            self.path.unlink()
        return self.status()
