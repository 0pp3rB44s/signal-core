"""Live order-path clock-safety gate (Deployment Gap #2 fix).

REPRODUCED DEFECT (this mission, code-read confirmed): `clients/bitget_auth.py`'s
`timestamp_ms()` returns local wall-clock time directly (`str(int(time.time() * 1000))`) with
no exchange-time comparison, no drift check, no alerting -- `CLOCK_SKEW_DETECTION=NOT_FOUND` on
the live order-signing path, exactly as this sprint's audit named it. This is the same failure
CLASS (`CLOCK_REFERENCE_FAILED`) that took down the liquidation V2 collector this week -- see
LIQUIDATION_V2_RECOVERY_2026-09-18.md for that incident's root-cause (transient network RTT
degradation, not a local clock defect, not an implementation bug).

THIS FIX reuses that same, already-tested design (Bybit collector's `clock_reference()`), adapted
for Bitget's public server-time endpoint and the ORDER path specifically:
  - Same frozen safety bound: CLOCK_OFFSET_LIMIT_MS = 500, unchanged, not loosened.
  - Same conservative-interval method: bounded probe, coherent wall/monotonic pair required.
  - Same fail-closed contract: no valid, fresh reference -> NO ORDER. Never silently proceed on
    an unestablished or expired clock state.

Integration point (NOT wired into production by this mission -- see
MICRO_LIVE_ENGINEERING_GAP_FIXES.md): `require_safe_clock()` must be called, and must not raise,
before `clients/bitget_order_client.py` places or amends any live order. It is independent of,
and in addition to, `execution_service.execute()`'s own mandatory RiskManager gate
(risk/mandatory_authorization.py) -- the two close different gaps and neither substitutes for
the other.
"""
from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("clients.clock_safety")

BITGET_TIME_URL = "https://api.bitget.com/api/v2/public/time"
CLOCK_OFFSET_LIMIT_MS = 500.0        # frozen; same bound as the liquidation V2 collector
MAX_PROBE_RTT_MS = 1000.0
CLOCK_JUMP_NS = 100_000_000          # 100ms; same as the liquidation V2 collector
REFERENCE_FRESHNESS_SECONDS = 60.0   # an order must not proceed on a reference older than this


class ClockUnsafe(RuntimeError):
    """Raised whenever an order must NOT be submitted due to clock state. Fail closed."""


@dataclass(frozen=True, slots=True)
class ClockProbe:
    local_utc: str
    exchange_utc: str | None
    offset_ms: float | None       # midpoint estimate, informational only -- the BOUND governs safety
    offset_lower_ms: float | None
    offset_upper_ms: float | None
    rtt_ms: float | None
    measured_at: float            # time.monotonic(), for freshness checks
    status: str                   # OK | STALE | FAILED | ENDPOINT_ERROR | NETWORK_TIMEOUT | CLOCK_JUMP
    detail: str

    def freshness_seconds(self, *, now: float | None = None) -> float:
        return (now if now is not None else time.monotonic()) - self.measured_at

    def is_safe(self, *, now: float | None = None, max_age_s: float = REFERENCE_FRESHNESS_SECONDS) -> bool:
        if self.status != "OK":
            return False
        if self.offset_lower_ms is None or self.offset_upper_ms is None:
            return False
        if max(abs(self.offset_lower_ms), abs(self.offset_upper_ms)) > CLOCK_OFFSET_LIMIT_MS:
            return False
        if self.freshness_seconds(now=now) > max_age_s:
            return False
        return True

    def to_dict(self) -> dict:
        return dict(local_utc=self.local_utc, exchange_utc=self.exchange_utc, offset_ms=self.offset_ms,
                    offset_lower_ms=self.offset_lower_ms, offset_upper_ms=self.offset_upper_ms,
                    rtt_ms=self.rtt_ms, status=self.status, detail=self.detail)


def _clocks() -> tuple[int, int]:
    return time.time_ns(), time.monotonic_ns()


def probe_exchange_clock(url: str = BITGET_TIME_URL, *, timeout_s: float = 5.0,
                          urlopen=urllib.request.urlopen) -> ClockProbe:
    """One bounded HTTPS probe against Bitget's public server-time endpoint. Never raises for
    ordinary failure modes (timeout, DNS, malformed response) -- returns a FAILED-status probe
    instead, so the caller's fail-closed logic is centralized in require_safe_clock(), not
    duplicated at every call site."""
    w0, m0 = _clocks()
    try:
        ctx = ssl.create_default_context()
        with urlopen(url, context=ctx, timeout=timeout_s) as resp:
            raw = resp.read()
    except TimeoutError as exc:
        return ClockProbe(_iso(w0), None, None, None, None, None, time.monotonic(),
                          "NETWORK_TIMEOUT", str(exc))
    except Exception as exc:  # noqa: BLE001 -- DNS, connection refused, TLS, etc. all fail closed identically
        return ClockProbe(_iso(w0), None, None, None, None, None, time.monotonic(),
                          "ENDPOINT_ERROR", f"{type(exc).__name__}: {exc}")
    w1, m1 = _clocks()

    try:
        data = json.loads(raw)
        server_ms = int(data["data"]["serverTime"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return ClockProbe(_iso(w0), None, None, None, None, None, time.monotonic(),
                          "ENDPOINT_ERROR", f"malformed response: {exc}")

    server_ns = server_ms * 1_000_000
    rtt_ms = (m1 - m0) / 1e6
    lower_ms = (server_ns - w1) / 1e6
    upper_ms = (server_ns - w0) / 1e6
    coherent = abs((w1 - w0) - (m1 - m0)) <= CLOCK_JUMP_NS

    if not coherent:
        return ClockProbe(_iso(w0), _iso(server_ns), (lower_ms + upper_ms) / 2, lower_ms, upper_ms,
                          rtt_ms, time.monotonic(), "CLOCK_JUMP",
                          "wall/monotonic elapsed diverged beyond 100ms during the probe itself")
    if rtt_ms > MAX_PROBE_RTT_MS:
        return ClockProbe(_iso(w0), _iso(server_ns), (lower_ms + upper_ms) / 2, lower_ms, upper_ms,
                          rtt_ms, time.monotonic(), "STALE", f"rtt {rtt_ms:.1f}ms exceeds {MAX_PROBE_RTT_MS:.0f}ms ceiling")

    return ClockProbe(_iso(w0), _iso(server_ns), (lower_ms + upper_ms) / 2, lower_ms, upper_ms,
                      rtt_ms, time.monotonic(), "OK", "probe succeeded and is internally coherent")


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat(timespec="milliseconds")


class ClockSafetyCache:
    """Per-process cache so every order does not re-probe on the hot path; a fresh-enough
    passing probe may be reused within REFERENCE_FRESHNESS_SECONDS. Deliberately NOT
    persisted to disk: a process restart must re-establish the reference from scratch,
    never trust a cross-process cached clock state."""

    def __init__(self) -> None:
        self._last: ClockProbe | None = None

    def get(self) -> ClockProbe | None:
        return self._last

    def refresh(self, *, prober=None) -> ClockProbe:
        # Looked up dynamically (module global) rather than bound as a function-definition-time
        # default, so tests can monkeypatch clients.clock_safety.probe_exchange_clock and have
        # it actually take effect here.
        probe = (prober or probe_exchange_clock)()
        self._last = probe
        return probe


def require_safe_clock(cache: ClockSafetyCache, *, max_age_s: float = REFERENCE_FRESHNESS_SECONDS,
                        force_refresh: bool = False) -> ClockProbe:
    """Call this before any live order is placed or amended. Returns the passing ClockProbe on
    success; raises ClockUnsafe on anything else. Never silently proceeds."""
    probe = cache.get()
    if force_refresh or probe is None or probe.freshness_seconds() > max_age_s:
        probe = cache.refresh()

    if probe.status != "OK":
        raise ClockUnsafe(f"clock reference not established: status={probe.status} detail={probe.detail}")
    if probe.offset_lower_ms is None or probe.offset_upper_ms is None:
        raise ClockUnsafe("clock reference missing offset bounds -- refusing to trust it")
    bound = max(abs(probe.offset_lower_ms), abs(probe.offset_upper_ms))
    if bound > CLOCK_OFFSET_LIMIT_MS:
        raise ClockUnsafe(
            f"clock offset bound {bound:.1f}ms exceeds frozen {CLOCK_OFFSET_LIMIT_MS:.0f}ms limit "
            f"(local={probe.local_utc} exchange={probe.exchange_utc} rtt={probe.rtt_ms})"
        )
    if probe.freshness_seconds() > max_age_s:
        raise ClockUnsafe(f"clock reference is {probe.freshness_seconds():.1f}s old, exceeds {max_age_s:.0f}s freshness bound")

    log.info("CLOCK_SAFETY_OK | offset_bound_ms=%.1f | rtt_ms=%.1f | age_s=%.1f",
             bound, probe.rtt_ms or -1, probe.freshness_seconds())
    return probe


__all__ = ["ClockProbe", "ClockSafetyCache", "ClockUnsafe", "probe_exchange_clock",
           "require_safe_clock", "CLOCK_OFFSET_LIMIT_MS", "REFERENCE_FRESHNESS_SECONDS",
           "BITGET_TIME_URL"]
