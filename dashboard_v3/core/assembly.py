"""Assembles panels into page payloads, with caching and per-panel isolation.

Two invariants:
  * one failing panel must never take down a page — every build is wrapped;
  * expensive sources are cached, so a browser poll does not hammer the exchange
    or re-parse a 78 MB event log.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from dashboard_v3.core.status import Signal, SignalSet, Status, worst

log = logging.getLogger("dashboard_v3")

#: Per-panel TTL in seconds. Exchange reads are the most expensive and the most
#: rate-limited, so they get the longest floor.
TTL = {
    "runtime": 5.0,
    "funnel": 30.0,
    "exchange": 20.0,
    "expectancy": 60.0,
    "health": 30.0,
    "incidents": 30.0,
    "history": 120.0,
    "strategy": 120.0,
    "operations": 5.0,
}

_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


def cached(key: str, builder: Callable[[], Any], ttl: float | None = None) -> Any:
    """Memoise a panel build. Never raises: a failure yields a described stub."""
    ttl = TTL.get(key, 30.0) if ttl is None else ttl
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and (now - hit[0]) < ttl:
            return hit[1]
    try:
        value = builder()
    except Exception as exc:  # a broken panel must not break the page
        log.exception("panel %s failed", key)
        signals = SignalSet()
        signals.add(Signal(key, key.title(), Status.UNKNOWN,
                           f"panel failed: {type(exc).__name__}",
                           "This widget could not be built; other panels are unaffected."))
        value = {
            "panel_error": f"{type(exc).__name__}: {str(exc)[:200]}",
            "signals": signals,
            "status": Status.UNKNOWN,
        }
    with _lock:
        _cache[key] = (now, value)
    return value


def invalidate() -> None:
    with _lock:
        _cache.clear()


def session_start() -> datetime | None:
    """Start of the current live session, used to scope the funnel window."""
    from dashboard_v3.core import sources as src

    state = src.read_kv_state("state/live_runtime.state").value or {}
    raw = state.get("started_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def build_all() -> dict[str, Any]:
    """Every panel, cached independently."""
    from dashboard_v3.panels import (
        exchange, expectancy, funnel, health, history, incidents, operations, runtime, strategy,
    )

    start = session_start()
    panels = {
        "runtime": cached("runtime", runtime.build),
        "funnel": cached("funnel", lambda: funnel.build(session_start=start)),
        "scores": cached("scores", funnel.score_distribution, ttl=120.0),
        "exchange": cached("exchange", exchange.build),
        "expectancy": cached("expectancy", expectancy.build),
        "health": cached("health", health.build),
        "incidents": cached("incidents", incidents.build),
        "history": cached("history", history.build),
        "strategy": cached("strategy", strategy.build),
        "operations": cached("operations", operations.build),
    }

    overall = worst(*(p.get("status", Status.UNKNOWN) for p in panels.values()))

    # Trading-permission verdict is a first-class fact, derived once, here.
    permission = _trading_permission(panels)

    return {
        "panels": panels,
        "overall": overall,
        "permission": permission,
        "session_start": start,
        "generated_at": datetime.now(timezone.utc),
    }


def _trading_permission(panels: dict[str, Any]) -> dict[str, Any]:
    """Why is the bot trading, or not? One answer, derived from evidence."""
    reasons: list[str] = []
    status = Status.HEALTHY

    exch = panels.get("exchange") or {}
    fun = panels.get("funnel") or {}
    rt = panels.get("runtime") or {}

    if not (rt.get("engine") or {}).get("alive"):
        status = Status.OFFLINE
        reasons.append("Engine process is not running.")

    if exch.get("unprotected_count"):
        status = worst(status, Status.BLOCKED)
        reasons.append(
            f"{exch['unprotected_count']} open position(s) lack confirmed exchange-side "
            "protection — new entries must stay blocked.")

    decisive = fun.get("decisive")
    if decisive:
        status = worst(status, Status.BLOCKED)
        top = fun.get("blockers") or []
        detail = ", ".join(b["label"] for b in top[:2]) if top else decisive["label"]
        reasons.append(f"{decisive['label']} admits 0 of {decisive['total']} candidates ({detail}).")

    if not exch.get("reachable"):
        status = worst(status, Status.UNKNOWN)
        reasons.append("Exchange state cannot be verified.")

    if not reasons:
        reasons.append("No blocking condition detected in the current window.")

    return {"status": status, "reasons": reasons}


__all__ = ["build_all", "cached", "invalidate", "session_start"]
