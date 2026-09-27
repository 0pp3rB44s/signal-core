"""The dashboard must be supervised, and must read production timestamps.

Both defects here were found on the live Runner, not in review: the dashboard
had been down for 15 hours with launchd holding no restart policy, and the
AdaptiveTrend panel silently rendered every real timestamp as UNKNOWN because
production writes epoch milliseconds, not ISO strings.
"""

from __future__ import annotations

import plistlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dashboard_v3.panels import adaptive_trend as at

TEMPLATES = Path(__file__).resolve().parents[1] / "deploy" / "launchd"

#: Every installed service must be supervised somehow. Two shapes are valid:
#: a daemon that launchd keeps alive, or a periodic probe on StartInterval.
#: The dashboard had NEITHER, which is why one clean exit was permanent.
SUPERVISED = ("com.cgc.dashboard", "com.cgc.live", "com.cgc.watchdog")

#: Daemons specifically — these must be restarted, not merely re-run on a timer.
DAEMONS = ("com.cgc.dashboard", "com.cgc.live")


def _plist(label: str) -> dict:
    raw = (TEMPLATES / f"{label}.plist.template").read_text()
    return plistlib.loads(raw.replace("__PROJECT_DIR__", "/tmp/project").encode())


@pytest.mark.parametrize("label", SUPERVISED)
def test_every_service_is_supervised(label):
    """Either launchd keeps it alive, or it re-runs on an interval. Neither is
    how the dashboard sat dead for 15 hours."""
    plist = _plist(label)
    assert "KeepAlive" in plist or "StartInterval" in plist, (
        f"{label} has no supervision: no KeepAlive and no StartInterval")


@pytest.mark.parametrize("label", DAEMONS)
def test_daemons_are_restarted_after_exit(label):
    """A long-lived server must come back on its own. The watchdog is excluded
    deliberately — it is a periodic probe, and its template says so."""
    assert "KeepAlive" in _plist(label), f"{label} is a daemon with no KeepAlive"


def test_watchdog_stays_a_periodic_probe():
    """Guards the other direction: the watchdog must not be turned into a
    respawning daemon by someone copying the dashboard fix."""
    plist = _plist("com.cgc.watchdog")
    assert plist.get("StartInterval") == 60
    assert "KeepAlive" not in plist


def test_dashboard_throttles_restarts():
    """Without a throttle, a persistently failing service hot-loops."""
    assert _plist("com.cgc.dashboard").get("ThrottleInterval", 0) >= 10


def test_dashboard_service_never_launches_the_engine():
    """A dashboard restart must not be able to touch trading."""
    body = (TEMPLATES / "com.cgc.dashboard.plist.template").read_text()
    agent = (TEMPLATES / "dashboard_agent.sh").read_text()
    for forbidden in ("launch_live", "app.main", "com.cgc.live", "stop_all"):
        assert forbidden not in body, f"dashboard plist references {forbidden}"
        assert forbidden not in agent, f"dashboard agent references {forbidden}"


# --- timestamp parsing -----------------------------------------------------

def test_epoch_milliseconds_are_parsed():
    """Production writes this exact shape."""
    got = at._ts(1787889726776)
    assert got is not None
    assert got.tzinfo is timezone.utc
    assert got.year == 2026


def test_epoch_milliseconds_as_string_are_parsed():
    assert at._ts("1787889726776") == at._ts(1787889726776)


def test_epoch_seconds_are_parsed():
    got = at._ts(1787889726)
    assert got is not None and got.year == 2026


def test_iso_strings_still_parse():
    got = at._ts("2026-08-27T08:55:01+00:00")
    assert got == datetime(2026, 8, 27, 8, 55, 1, tzinfo=timezone.utc)


def test_naive_iso_is_treated_as_utc():
    assert at._ts("2026-08-27T08:55:01").tzinfo is timezone.utc


def test_unparseable_values_are_unknown_not_epoch_zero():
    for bad in (None, "", "not-a-time", 42, "0"):
        assert at._ts(bad) is None, f"{bad!r} should be UNKNOWN"


# --- outcome classification -------------------------------------------------

def _view(**kw):
    base = dict(symbol="SOLUSDT", side="LONG", six_h_close=106.773, mom=0.1033,
                mom_strength=3.008, atr=3.667, entry_candidate=106.773,
                initial_stop=97.606, notional=None, risk_pct=None,
                decision="ACCOUNT_FREEZE_BLOCKED", rejection_reason=None,
                timestamp=None, code_sha="7f29ce5")
    base.update(kw)
    return at.SymbolView(**base)


def test_sizing_rejection_is_distinguished_from_a_risk_block():
    """Production emits ACCOUNT_FREEZE_BLOCKED for both. They are not the same
    operator situation and must not read the same."""
    sizing = _view(rejection_reason="ACCOUNT_TOO_SMALL_FOR_SAFE_ORDER")
    risk = _view(rejection_reason="weekly_freeze_active")
    assert sizing.outcome == at.OUTCOME_SIZING_REJECTED
    assert risk.outcome == at.OUTCOME_RISK_BLOCKED
    assert sizing.outcome != risk.outcome


def test_a_qualifying_signal_can_still_be_sizing_rejected():
    """mom far above threshold, yet no order — the real production case."""
    v = _view(mom=0.1033, rejection_reason="ACCOUNT_TOO_SMALL_FOR_SAFE_ORDER")
    assert v.actionable is True
    assert v.outcome == at.OUTCOME_SIZING_REJECTED


def test_unknown_outcome_when_nothing_was_recorded():
    assert _view(decision=None, rejection_reason=None).outcome == at.OUTCOME_UNKNOWN
