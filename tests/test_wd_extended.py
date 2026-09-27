"""scripts/wd_extended.py: deployment classification, health-model mapping,
first-trade watch, and the read-only import boundary.

None of these tests perform a real exchange call or a real Telegram send —
send_info/raise_alert/resolve_alerts are monkeypatched or exercised only with
--no-deliver, matching the isolation pattern in tests/test_watchdog_live.py.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

spec = importlib.util.spec_from_file_location("wd_extended", REPO / "scripts" / "wd_extended.py")
wd_extended = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd_extended)  # type: ignore[union-attr]


# --- deployment SHA classification ---------------------------------------

def test_matching_sha_is_match():
    assert wd_extended.classify_deployment("abc123", "abc123") == "MATCH"


def test_missing_sha_is_unknown():
    assert wd_extended.classify_deployment("", "abc123") == "UNKNOWN"
    assert wd_extended.classify_deployment("abc123", "") == "UNKNOWN"
    assert wd_extended.classify_deployment(None, None) == "UNKNOWN"  # type: ignore[arg-type]


def _resolve(ref: str) -> str:
    import subprocess
    return subprocess.run(["git", "rev-parse", ref], cwd=str(REPO), capture_output=True,
                          text=True, check=True).stdout.strip()


def test_docs_only_commit_is_docs_only_drift():
    # e81263a touches only docs/ADAPTIVE_TREND_V1.md against its parent.
    assert wd_extended.classify_deployment(_resolve("e81263a~1"), _resolve("e81263a")) == "DOCS_ONLY_DRIFT"


def test_code_change_is_functional_mismatch():
    # b315b07 touches app/logger.py and a test file against its parent.
    assert wd_extended.classify_deployment(_resolve("b315b07~1"), _resolve("b315b07")) == "FUNCTIONAL_MISMATCH"


def test_unresolvable_sha_is_unknown_not_a_crash():
    assert wd_extended.classify_deployment("not-a-real-sha", "also-not-real") == "UNKNOWN"


# --- GREEN / YELLOW / RED health model ------------------------------------

def _base_kwargs(**overrides):
    kwargs = dict(
        engine_alive=True, heartbeat_stale=False, scan_stalled=False,
        position_unprotected=False, hedge_detected=False, exchange_mismatch=False,
        unresolved_intents_stuck=False, duplicate_engine=False, recovery_pending=False,
        dashboard_down=False, logging_lag=False, risk_freeze_active=False,
        deploy_functional_mismatch=False, signal_stale=False,
    )
    kwargs.update(overrides)
    return kwargs


def test_all_clear_is_green():
    overall, reasons = wd_extended.compute_overall_health(**_base_kwargs())
    assert overall == "GREEN"
    assert reasons == []


def test_engine_down_is_red():
    overall, reasons = wd_extended.compute_overall_health(**_base_kwargs(engine_alive=False))
    assert overall == "RED"
    assert any("engine" in r for r in reasons)


def test_unprotected_position_is_red():
    overall, _ = wd_extended.compute_overall_health(**_base_kwargs(position_unprotected=True))
    assert overall == "RED"


def test_hedge_is_red():
    overall, _ = wd_extended.compute_overall_health(**_base_kwargs(hedge_detected=True))
    assert overall == "RED"


def test_dashboard_down_alone_is_yellow_not_red():
    overall, reasons = wd_extended.compute_overall_health(**_base_kwargs(dashboard_down=True))
    assert overall == "YELLOW"
    assert any("dashboard" in r for r in reasons)


def test_logging_lag_alone_is_yellow():
    overall, _ = wd_extended.compute_overall_health(**_base_kwargs(logging_lag=True))
    assert overall == "YELLOW"


def test_risk_freeze_alone_does_not_degrade_engine_health():
    """Risk-blocked and engine-unhealthy are different concepts: a freeze must
    not turn engine health RED or even YELLOW on its own."""
    overall, reasons = wd_extended.compute_overall_health(**_base_kwargs(risk_freeze_active=True))
    assert overall == "GREEN"
    assert reasons == []


def test_red_outranks_yellow_when_both_present():
    overall, reasons = wd_extended.compute_overall_health(
        **_base_kwargs(engine_alive=False, dashboard_down=True)
    )
    assert overall == "RED"
    assert not any("dashboard" in r for r in reasons)  # RED short-circuits before YELLOW is evaluated


# --- first-trade / position-close watch -----------------------------------

@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(wd_extended, "STATE_DIR", tmp_path)
    monkeypatch.setattr(wd_extended, "SEEN_POSITIONS_FILE", tmp_path / "wd_extended_seen_positions.json")
    sent: list[tuple[str, str]] = []

    def fake_send_info(event, msg, no_deliver=False):
        if not no_deliver:
            sent.append((event, msg))

    monkeypatch.setattr(wd_extended, "send_info", fake_send_info)
    return tmp_path, sent


def test_flat_to_open_fires_entry_notification(isolated_state):
    _, sent = isolated_state
    positions = [{"symbol": "BTCUSDT", "side": "LONG", "entry": 65000.0, "size": 0.01,
                 "active_stop": 64000.0, "margin": 10.0, "leverage": 3}]
    wd_extended.watch_position_transitions(positions, True, now=1000.0, no_deliver=False)
    assert len(sent) == 1
    assert sent[0][0] == "ADAPTIVETREND_LIVE_ENTRY"
    assert "BTCUSDT" in sent[0][1]


def test_open_to_flat_fires_close_notification(isolated_state):
    tmp_path, sent = isolated_state
    positions = [{"symbol": "BTCUSDT", "side": "LONG", "entry": 65000.0, "size": 0.01}]
    wd_extended.watch_position_transitions(positions, True, now=1000.0, no_deliver=False)
    sent.clear()
    wd_extended.watch_position_transitions([], True, now=1100.0, no_deliver=False)
    assert len(sent) == 1
    assert sent[0][0] == "ADAPTIVETREND_POSITION_CLOSED"


def test_stable_open_position_fires_nothing(isolated_state):
    _, sent = isolated_state
    positions = [{"symbol": "BTCUSDT", "side": "LONG", "entry": 65000.0, "size": 0.01}]
    wd_extended.watch_position_transitions(positions, True, now=1000.0, no_deliver=False)
    sent.clear()
    wd_extended.watch_position_transitions(positions, True, now=1060.0, no_deliver=False)
    assert sent == []


def test_unreachable_exchange_never_fires_a_transition(isolated_state):
    """An unreachable exchange must never be read as 'flat' — that would fire a
    false CLOSE notification for a position that is simply unobservable."""
    _, sent = isolated_state
    positions = [{"symbol": "BTCUSDT", "side": "LONG", "entry": 65000.0, "size": 0.01}]
    wd_extended.watch_position_transitions(positions, True, now=1000.0, no_deliver=False)
    sent.clear()
    wd_extended.watch_position_transitions([], False, now=1060.0, no_deliver=False)
    assert sent == []


def test_no_deliver_suppresses_send(isolated_state):
    _, sent = isolated_state
    positions = [{"symbol": "BTCUSDT", "side": "LONG", "entry": 65000.0, "size": 0.01}]
    wd_extended.watch_position_transitions(positions, True, now=1000.0, no_deliver=True)
    assert sent == []


# --- atomic write ----------------------------------------------------------

def test_atomic_write_json_leaves_no_tmp_file(tmp_path):
    target = tmp_path / "status.json"
    wd_extended.atomic_write_json(target, {"a": 1})
    assert target.exists()
    assert list(tmp_path.glob(".*")) == []


# --- read-only import boundary ---------------------------------------------

FORBIDDEN_SUBSTRINGS = (
    "bitget_order_client", "bitget_tpsl_client", "execution.", "risk.risk_manager",
    "place_futures", "close_futures_position", "cancel_futures", "place_position_tpsl",
)


def test_wd_extended_never_references_mutating_trading_code():
    source = (REPO / "scripts" / "wd_extended.py").read_text(encoding="utf-8")
    for token in FORBIDDEN_SUBSTRINGS:
        assert token not in source, f"wd_extended.py must stay read-only; found {token!r}"
