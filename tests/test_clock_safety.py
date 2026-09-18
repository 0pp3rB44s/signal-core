"""Deployment Gap #2 fix tests: live order-path clock-safety gate.

Required scenarios per mission: normal clock, positive skew, negative skew, stale reference,
endpoint failure, network timeout, clock jump, process restart.
"""
from __future__ import annotations

import json
import time

import pytest

from clients.clock_safety import (
    CLOCK_OFFSET_LIMIT_MS, ClockProbe, ClockSafetyCache, ClockUnsafe, probe_exchange_clock,
    require_safe_clock,
)


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body
    def read(self):
        return self.body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _bitget_response(server_ms: int) -> bytes:
    return json.dumps({"code": "00000", "msg": "success", "requestTime": server_ms,
                       "data": {"serverTime": str(server_ms)}}).encode()


def test_normal_clock_passes():
    now_ms = int(time.time() * 1000)
    def urlopen(url, context, timeout):
        return FakeResponse(_bitget_response(now_ms))
    probe = probe_exchange_clock(urlopen=urlopen)
    assert probe.status == "OK"
    assert probe.is_safe()
    cache = ClockSafetyCache()
    cache._last = probe
    result = require_safe_clock(cache)
    assert result.status == "OK"


def test_positive_skew_within_bound_passes():
    """Server clock 200ms ahead of local -- within the 500ms bound."""
    now_ms = int(time.time() * 1000) + 200
    def urlopen(url, context, timeout):
        return FakeResponse(_bitget_response(now_ms))
    probe = probe_exchange_clock(urlopen=urlopen)
    assert probe.status == "OK" and probe.is_safe()


def test_negative_skew_within_bound_passes():
    now_ms = int(time.time() * 1000) - 200
    def urlopen(url, context, timeout):
        return FakeResponse(_bitget_response(now_ms))
    probe = probe_exchange_clock(urlopen=urlopen)
    assert probe.status == "OK" and probe.is_safe()


def test_skew_beyond_bound_fails_closed_no_order():
    """Server clock 2 seconds off -- must FAIL CLOSED, not silently pass through."""
    now_ms = int(time.time() * 1000) + 2000
    def urlopen(url, context, timeout):
        return FakeResponse(_bitget_response(now_ms))
    cache = ClockSafetyCache()
    cache.refresh(prober=lambda: probe_exchange_clock(urlopen=urlopen))
    with pytest.raises(ClockUnsafe, match="exceeds frozen"):
        require_safe_clock(cache)


def test_bound_is_never_loosened():
    assert CLOCK_OFFSET_LIMIT_MS == 500.0


def test_stale_reference_triggers_refresh_and_blocks_if_refresh_also_fails(monkeypatch):
    """A previously-passing probe that has aged past the freshness window must never be used
    as-is -- require_safe_clock attempts a fresh probe; if that attempt itself fails (e.g. the
    exchange is unreachable right when a stale reference needed renewing), the order is still
    blocked, never allowed to fall back on the old, no-longer-trustworthy reading."""
    import clients.clock_safety as mod
    old_probe = ClockProbe(local_utc="x", exchange_utc="y", offset_ms=0, offset_lower_ms=-10,
                           offset_upper_ms=10, rtt_ms=50, measured_at=time.monotonic() - 120,
                           status="OK", detail="was fine 120s ago")
    cache = ClockSafetyCache()
    cache._last = old_probe

    def urlopen(url, context, timeout):
        raise ConnectionRefusedError("exchange unreachable during renewal")
    monkeypatch.setattr(mod, "probe_exchange_clock", lambda **kw: probe_exchange_clock(urlopen=urlopen))

    with pytest.raises(ClockUnsafe, match="not established"):
        require_safe_clock(cache, max_age_s=60.0, force_refresh=False)
    # the stale OK probe must have been REPLACED by the failed refresh attempt, not left in place
    assert cache.get().status != "OK"


def test_stale_reference_refreshes_successfully_and_passes(monkeypatch):
    """The mirror case: a stale-but-renewable reference refreshes cleanly and then passes --
    proves staleness alone isn't a permanent block, only an unrenewable one is."""
    import clients.clock_safety as mod
    old_probe = ClockProbe(local_utc="x", exchange_utc="y", offset_ms=0, offset_lower_ms=-10,
                           offset_upper_ms=10, rtt_ms=50, measured_at=time.monotonic() - 120,
                           status="OK", detail="was fine 120s ago")
    cache = ClockSafetyCache()
    cache._last = old_probe
    now_ms = int(time.time() * 1000)
    def urlopen(url, context, timeout):
        return FakeResponse(_bitget_response(now_ms))
    monkeypatch.setattr(mod, "probe_exchange_clock", lambda **kw: probe_exchange_clock(urlopen=urlopen))
    result = require_safe_clock(cache, max_age_s=60.0, force_refresh=False)
    assert result.status == "OK"


def test_endpoint_failure_blocks():
    def urlopen(url, context, timeout):
        raise ConnectionRefusedError("connection refused")
    probe = probe_exchange_clock(urlopen=urlopen)
    assert probe.status == "ENDPOINT_ERROR"
    cache = ClockSafetyCache()
    cache._last = probe
    with pytest.raises(ClockUnsafe, match="not established"):
        require_safe_clock(cache)


def test_network_timeout_blocks():
    def urlopen(url, context, timeout):
        raise TimeoutError("timed out")
    probe = probe_exchange_clock(urlopen=urlopen)
    assert probe.status == "NETWORK_TIMEOUT"
    cache = ClockSafetyCache()
    cache._last = probe
    with pytest.raises(ClockUnsafe):
        require_safe_clock(cache)


def test_malformed_response_blocks():
    def urlopen(url, context, timeout):
        return FakeResponse(b"not json at all")
    probe = probe_exchange_clock(urlopen=urlopen)
    assert probe.status == "ENDPOINT_ERROR"


def test_clock_jump_detected_and_blocks(monkeypatch):
    """Simulate a wall-clock jump occurring mid-probe (e.g. suspend/resume, NTP step) by
    making wall time advance far more than monotonic time between the two clock reads."""
    import clients.clock_safety as mod
    calls = {"n": 0}
    real_clocks = mod._clocks

    def jumping_clocks():
        calls["n"] += 1
        w, m = real_clocks()
        if calls["n"] == 2:  # the second read, taken after the fake HTTP call "completes"
            w += 5_000_000_000  # +5s wall jump, monotonic unaffected -> incoherent pair
        return w, m

    monkeypatch.setattr(mod, "_clocks", jumping_clocks)
    now_ms = int(time.time() * 1000)
    def urlopen(url, context, timeout):
        return FakeResponse(_bitget_response(now_ms))
    probe = probe_exchange_clock(urlopen=urlopen)
    assert probe.status == "CLOCK_JUMP"
    cache = ClockSafetyCache()
    cache._last = probe
    with pytest.raises(ClockUnsafe, match="not established"):
        require_safe_clock(cache)


def test_process_restart_forces_fresh_probe(monkeypatch):
    """A fresh ClockSafetyCache (simulating a new process) starts with no reference at all --
    require_safe_clock must trigger a real probe rather than assume a previously-good state
    carried over from a prior process."""
    import clients.clock_safety as mod
    now_ms = int(time.time() * 1000)
    def urlopen(url, context, timeout):
        return FakeResponse(_bitget_response(now_ms))
    monkeypatch.setattr(mod, "probe_exchange_clock", lambda **kw: probe_exchange_clock(urlopen=urlopen))
    cache = ClockSafetyCache()
    assert cache.get() is None
    result = require_safe_clock(cache)
    assert result.status == "OK"
    assert cache.get() is not None
