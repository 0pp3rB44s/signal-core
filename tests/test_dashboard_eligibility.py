"""Trade eligibility: several things can block at once; only one decides."""

from __future__ import annotations

from dashboard_v3.core.status import Status
from dashboard_v3.panels.eligibility import Eligibility, assess

OK = dict(engine_running=True, heartbeat_stale=False, runtime_sha="abc1234",
          deployed_sha="abc1234", weekly_frozen=False, live_entry_enabled=True,
          signal_data_stale=False)


def test_ready_when_nothing_blocks():
    v = assess(**OK)
    assert v.eligibility == Eligibility.READY
    assert v.status is Status.HEALTHY
    assert v.primary is None


def test_engine_down_outranks_everything():
    """A stopped engine makes every other blocker academic."""
    v = assess(**{**OK, "engine_running": False, "weekly_frozen": True,
                  "live_entry_enabled": False, "heartbeat_stale": True})
    assert v.eligibility == Eligibility.ENGINE_DOWN
    assert v.primary.key == "engine_down"
    assert v.status is Status.OFFLINE
    assert {b.key for b in v.secondary} >= {"weekly_freeze", "live_entry_disabled"}


def test_weekly_freeze_is_the_primary_risk_blocker():
    v = assess(**{**OK, "weekly_frozen": True})
    assert v.eligibility == Eligibility.RISK_BLOCKED
    assert v.primary.key == "weekly_freeze"
    assert v.status is Status.BLOCKED


def test_sha_mismatch_is_a_config_block_and_outranks_risk():
    v = assess(**{**OK, "runtime_sha": "aaaaaaa", "deployed_sha": "bbbbbbb", "weekly_frozen": True})
    assert v.primary.key == "runtime_sha_mismatch"
    assert v.eligibility == Eligibility.CONFIG_BLOCKED
    assert "aaaaaaa" in v.primary.detail and "bbbbbbb" in v.primary.detail


def test_matching_sha_prefixes_are_not_a_mismatch():
    """Short and full SHAs of the same commit must not raise a false alarm."""
    v = assess(**{**OK, "runtime_sha": "b05e27e", "deployed_sha": "b05e27e5d4e5df43211dc093"})
    assert v.eligibility == Eligibility.READY


def test_live_entry_disabled_is_config_blocked():
    v = assess(**{**OK, "live_entry_enabled": False})
    assert v.eligibility == Eligibility.CONFIG_BLOCKED
    assert v.primary.key == "live_entry_disabled"


def test_stale_heartbeat_is_data_blocked():
    v = assess(**{**OK, "heartbeat_stale": True})
    assert v.eligibility == Eligibility.DATA_BLOCKED
    assert v.status is Status.STALE


def test_stale_signal_data_is_data_blocked():
    v = assess(**{**OK, "signal_data_stale": True})
    assert v.eligibility == Eligibility.DATA_BLOCKED
    assert v.primary.key == "stale_market_data"


def test_unresolved_intents_block_and_report_the_count():
    v = assess(**{**OK, "unresolved_intents": 3})
    assert v.eligibility == Eligibility.RISK_BLOCKED
    assert "3" in v.primary.detail


def test_unknown_engine_state_is_unknown_not_ready():
    v = assess(**{**OK, "engine_running": None})
    assert v.eligibility == Eligibility.UNKNOWN
    assert v.status is Status.UNKNOWN


def test_unknown_never_outranks_a_proven_blocker():
    """'We cannot tell' must not mask 'it is frozen'."""
    v = assess(**{**OK, "engine_running": None, "weekly_frozen": True})
    assert v.primary.key == "weekly_freeze"


def test_daily_stop_is_reported():
    v = assess(**{**OK, "daily_stop_active": True})
    assert v.primary.key == "daily_stop"
    assert v.eligibility == Eligibility.RISK_BLOCKED
