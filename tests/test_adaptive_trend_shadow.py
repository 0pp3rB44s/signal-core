"""Shadow-mode correctness: never touches live state by construction, and its
hypothetical lifecycle follows the exact same entry/ATR-trailing rules used
for a real position."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import strategies.adaptive_trend_shadow as shadow_module
from strategies.adaptive_trend_shadow import (
    ShadowDecisionLog,
    ShadowLifecycle,
    build_freeze_blocked_record,
)


def test_shadow_module_has_no_execution_or_client_imports():
    """Structural guarantee, not a convention: a shadow decision cannot
    become a live one because this module cannot reach an exchange client
    or the execution engine at all."""
    src = Path(shadow_module.__file__).read_text()
    tree = ast.parse(src)
    forbidden_prefixes = ("clients", "execution")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(forbidden_prefixes), alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith(forbidden_prefixes), node.module


def test_freeze_blocked_record_decision_is_always_that_label():
    r = build_freeze_blocked_record(
        timestamp="2026-08-24T00:00:00Z", symbol="BTCUSDT", side="LONG",
        six_h_close=100.0, mom=0.05, atr=2.0, mom_strength=25.0,
        entry_candidate=100.0, initial_stop=95.0, risk_pct=0.005, notional=50.0,
    )
    assert r["decision"] == "ACCOUNT_FREEZE_BLOCKED"
    assert r["rejection_reason"] == "weekly_freeze_active"
    assert r["strategy_version"] == "adaptive_trend_tsmom_v1"


def test_shadow_log_append_and_read_roundtrip(tmp_path):
    log = ShadowDecisionLog(tmp_path / "shadow.jsonl")
    r1 = build_freeze_blocked_record(
        timestamp="t1", symbol="BTCUSDT", side="LONG", six_h_close=1, mom=1,
        atr=1, mom_strength=1, entry_candidate=1, initial_stop=1, risk_pct=1, notional=1,
    )
    r2 = build_freeze_blocked_record(
        timestamp="t2", symbol="ETHUSDT", side="SHORT", six_h_close=2, mom=2,
        atr=2, mom_strength=2, entry_candidate=2, initial_stop=2, risk_pct=2, notional=2,
    )
    log.append(r1)
    log.append(r2)
    rows = log.read_all()
    assert len(rows) == 2
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[1]["symbol"] == "ETHUSDT"


def test_shadow_log_is_append_only_never_rewrites_prior_rows(tmp_path):
    log = ShadowDecisionLog(tmp_path / "shadow.jsonl")
    log.append({"n": 1})
    raw_before = log.path.read_text()
    log.append({"n": 2})
    raw_after = log.path.read_text()
    assert raw_after.startswith(raw_before)


def test_shadow_lifecycle_ratchets_long_stop_and_stays_open():
    lc = ShadowLifecycle(symbol="BTCUSDT", side="LONG", entry_price=100.0,
                          stop=95.0, atr_at_entry=2.0, signal_candle_close_ms=0)
    advanced = lc.advance(candle_close_ms=1, close=110.0, high=111.0, low=105.0, atr=2.0)
    assert advanced.status == "OPEN"
    assert advanced.stop > lc.stop  # ratcheted up
    assert advanced.stop == pytest.approx(110.0 - 2.5 * 2.0)


def test_shadow_lifecycle_closes_on_stop_touch():
    lc = ShadowLifecycle(symbol="BTCUSDT", side="LONG", entry_price=100.0,
                          stop=95.0, atr_at_entry=2.0, signal_candle_close_ms=0)
    advanced = lc.advance(candle_close_ms=1, close=94.0, high=96.0, low=93.0, atr=2.0)
    assert advanced.status == "CLOSED"
    assert advanced.exit_reason == "trailing_stop"
    assert advanced.exit_price == 95.0


def test_shadow_lifecycle_once_closed_advance_is_a_noop():
    lc = ShadowLifecycle(symbol="BTCUSDT", side="LONG", entry_price=100.0,
                          stop=95.0, atr_at_entry=2.0, signal_candle_close_ms=0,
                          status="CLOSED", exit_price=95.0, exit_reason="trailing_stop",
                          closed_at_close_ms=1)
    advanced = lc.advance(candle_close_ms=2, close=200.0, high=200.0, low=200.0, atr=1.0)
    assert advanced is lc


def test_shadow_hypothetical_pnl_open_uses_mark_price():
    lc = ShadowLifecycle(symbol="BTCUSDT", side="LONG", entry_price=100.0,
                          stop=95.0, atr_at_entry=2.0, signal_candle_close_ms=0)
    assert lc.hypothetical_pnl_pct(110.0) == pytest.approx(0.10)


def test_shadow_hypothetical_pnl_closed_uses_exit_price_not_mark():
    lc = ShadowLifecycle(symbol="BTCUSDT", side="LONG", entry_price=100.0,
                          stop=95.0, atr_at_entry=2.0, signal_candle_close_ms=0,
                          status="CLOSED", exit_price=95.0, exit_reason="trailing_stop",
                          closed_at_close_ms=1)
    assert lc.hypothetical_pnl_pct(150.0) == pytest.approx(-0.05)


def test_shadow_short_lifecycle_ratchets_downward_and_closes_correctly():
    lc = ShadowLifecycle(symbol="ETHUSDT", side="SHORT", entry_price=100.0,
                          stop=105.0, atr_at_entry=2.0, signal_candle_close_ms=0)
    advanced = lc.advance(candle_close_ms=1, close=90.0, high=95.0, low=89.0, atr=2.0)
    assert advanced.stop < lc.stop
    closed = advanced.advance(candle_close_ms=2, close=advanced.stop + 1, high=advanced.stop + 1, low=advanced.stop, atr=2.0)
    assert closed.status == "CLOSED"
