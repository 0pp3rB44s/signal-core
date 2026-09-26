"""Live gate collectors for the ceremony provider: each observes ONE thing about the PROPOSED live runtime (never PAPER) and returns an Observation.

All are read-only. A collector that raises is a FAILED gate, never a passed one; unknown is not healthy. Nothing here decides to trade.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable

_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{24,}")


@dataclass(frozen=True)
class Observation:
    passed: bool
    doc: dict[str, Any] = field(default_factory=dict)          # NON-SECRET facts; their canonical digest becomes the gate's evidence


def observe(fn: Callable[[], Observation]) -> Observation:
    try:
        out = fn()
        return out if isinstance(out, Observation) else Observation(False, {"error": "collector returned no Observation"})
    except Exception as exc:                                   # noqa: BLE001 - a collector that fails is a FAILED gate
        return Observation(False, {"error": f"{type(exc).__name__}: {_TOKEN.sub('<redacted>', str(exc))[:160]}"})


def clock_gate(client: Any) -> Observation:
    """The exchange-clock reference of the client that will place orders is fresh and within tolerance (the same gate every order passes)."""
    client._require_safe_clock_for_order()
    return Observation(True, {"clock": "safe_for_orders"})


def feed_gate(runtime: Any, *, uptime_s: float, min_uptime_s: float = 60.0, max_frame_age_s: float = 5.0) -> Observation:
    st = runtime.status()
    feed, ws = st.get("feed") or {}, st.get("ws") or {}
    age = ws.get("last_frame_age_s")
    ok = (feed.get("state") == "OK" and ws.get("connected") is True and isinstance(age, (int, float)) and age <= max_frame_age_s
          and not st.get("engine_halted") and uptime_s >= min_uptime_s)
    return Observation(bool(ok), {"feed_state": feed.get("state"), "ws_connected": ws.get("connected"), "last_frame_age_ok": isinstance(age, (int, float)) and age <= max_frame_age_s,
                                  "engine_halted": bool(st.get("engine_halted")), "warmed_up": uptime_s >= min_uptime_s, "mode": st.get("mode")})


def recorder_gate(runtime: Any) -> Observation:
    st = runtime.status()
    recorder = getattr(runtime.driver, "recorder", None)
    ok = recorder is not None and not st.get("recorder_error")
    return Observation(bool(ok), {"recorder_present": recorder is not None, "recorder_error": bool(st.get("recorder_error"))})


def disk_gate(capacity_gate: Any, volume: str, *, min_free_gb: float = 8.0,
              disk_usage: Callable[[str], Any] = shutil.disk_usage) -> Observation:
    """Free space above the monitor's own alarm level AND a fresh, quiet capacity monitor."""
    free_gb = disk_usage(volume).free / 1e9
    blocked = capacity_gate.blocked_reason()
    return Observation(free_gb >= min_free_gb and blocked is None, {"free_gb_ok": free_gb >= min_free_gb, "min_free_gb": min_free_gb, "monitor_blocked": blocked})


def risk_manager_gate(runtime: Any, risk_manager: Any) -> Observation:
    """The central RiskManager is the one behind the entry gate (not the null gate a paper runtime uses)."""
    from lsrl.adapters.riskgate import CentralRiskGate
    gate = getattr(getattr(runtime.driver, "executor", None), "gate", None)
    ok = isinstance(gate, CentralRiskGate) and gate.risk_manager is risk_manager and callable(getattr(risk_manager, "evaluate", None))
    return Observation(bool(ok), {"central_gate": isinstance(gate, CentralRiskGate), "same_risk_manager": getattr(gate, "risk_manager", None) is risk_manager})


def kill_switch_gate(risk_manager: Any, settings: Any) -> Observation:
    """The loss limits that stop new entries are configured (non-zero) and READABLE: an unreadable daily report fails closed inside the RiskManager,
    and here it fails the gate before the owner is even asked."""
    daily_pct = float(getattr(settings, "hard_daily_stop_pct", 0.0) or 0.0)
    weekly_pct = float(getattr(settings, "weekly_freeze_loss_pct", 0.0) or 0.0)
    daily = risk_manager._daily_defensive_status()
    readable = isinstance(daily, dict) and not daily.get("daily_status_unreadable")
    weekly_readable = True
    try:
        risk_manager._weekly_realized_pnl()
    except Exception:                                          # noqa: BLE001
        weekly_readable = False
    return Observation(daily_pct > 0 and weekly_pct > 0 and readable and weekly_readable,
                       {"hard_daily_stop_configured": daily_pct > 0, "weekly_freeze_configured": weekly_pct > 0, "daily_report_readable": readable,
                        "weekly_pnl_readable": weekly_readable})


def reconciliation_gate(runtime: Any) -> Observation:
    """The executor of the proposed runtime has reconciled with the exchange: recovered, nothing unclassified, nothing owned, no halt."""
    s = runtime.driver.executor.summary()
    ok = (s["recovered"] is True and not s["halt_reason"] and not s["emergency"] and s["exposure_state"] in ("NONE", None)
          and not s["startup_unreadable"] and s["unclassified_orders"] == 0 and s["foreign_plans"] == 0 and s["cancel_failures"] == 0
          and s["open_setups"] == 0)
    return Observation(bool(ok), {k: s[k] for k in ("authority", "recovered", "halt_reason", "emergency", "exposure_state", "startup_unreadable",
                                                     "unclassified_orders", "foreign_plans", "cancel_failures", "open_setups")})


def exchange_protection_gate(audit_report: dict[str, Any]) -> Observation:
    """No unprotected exposure and no orphaned protection: from the account-wide audit, the account holds no position and no plan/resting order."""
    ok = bool(audit_report.get("complete")) and audit_report.get("REAL_POSITIONS") == 0 and audit_report.get("REAL_PLAN_ORDERS") == 0 \
        and audit_report.get("REAL_ORDERS") == 0
    return Observation(ok, {"complete": bool(audit_report.get("complete")), "positions": audit_report.get("REAL_POSITIONS"),
                            "orders": audit_report.get("REAL_ORDERS"), "plan_orders": audit_report.get("REAL_PLAN_ORDERS")})
