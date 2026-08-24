"""AdaptiveTrend's frozen spec caps it at exactly ONE open position total
(across BTC/ETH/SOL) -- stricter than the generic per-strategy default of 2.
A hedge (simultaneous long+short) is structurally impossible once at most
one position can ever be open for the strategy.

Note: adaptive_trend_tsmom_v1 is still blocked earlier in execute() by the
HYBRID SAFE MODE gate (deliberately -- it is not yet wired into live order
eligibility, per the phased rollout). That earlier gate is intentionally
untouched here. This file proves two things instead: (1) the per-strategy
cap mechanism itself, generically, through the real execute() path with a
strategy that IS hybrid-gate-supported, and (2) that the real override
dict is actually wired into that mechanism for the adaptive_trend_tsmom_v1
key specifically, via a source-level check -- so if the override is
removed or the value drifts, this fails.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id
from clients.schemas import TradePlan
from execution.execution_service import ExecutionService

CANDLE_MS = 1_785_000_000_000
REPO = Path(__file__).resolve().parents[1]


def _settings(**over) -> Settings:
    base = {
        "EXECUTION_ENABLED": True,
        "EXECUTION_MODE": "LIVE",
        "PRODUCTION_SYMBOL_ALLOWLIST": "BTCUSDT,ETHUSDT,SOLUSDT",
        "MAX_SYMBOLS": 3,
        "MAX_OPEN_POSITIONS": 2,
        "EXECUTION_MAX_PER_CYCLE": 2,
        "ALLOW_AUTO_WATCHLIST_REFRESH": False,
        "EXECUTION_REQUIRE_CONFIRMATION": True,
        "MAKER_ENTRY_ENABLED": False,
        "SYMBOL_COOLDOWN_MINUTES": 0,
        "ENABLE_SHORTS": True,
    }
    base.update(over)
    return Settings(_env_file=None, **base)


def _plan(symbol: str, strategy: str, direction: str = "LONG") -> TradePlan:
    cid = deterministic_candidate_id(strategy, symbol, direction, CANDLE_MS)
    long_side = direction.upper() == "LONG"
    return TradePlan(
        candidate_id=cid,
        candidate_candle_open_timestamp_ms=CANDLE_MS,
        plan_id=deterministic_plan_id(cid),
        symbol=symbol, strategy=strategy, direction=direction,
        verdict="EXECUTABLE", score=92.0,
        entry_prices=[100.0],
        stop_loss=95.0 if long_side else 105.0,
        take_profits=[120.0 if long_side else 80.0],
        risk_reward_ratio=1.3, account_risk_pct=0.5, leverage=2.0,
        position_notional_usdt=26.79, notes=[], reasons=[], geometry_entry=100.0,
    )


def _service(monkeypatch, existing_open: list[dict] | None = None) -> ExecutionService:
    monkeypatch.setattr("execution.execution_service.resolve_account_equity",
                        lambda _s: (1000.0, "test"))
    svc = ExecutionService(settings=_settings())
    client = MagicMock()
    open_symbols = {row["symbol"] for row in (existing_open or [])}
    client.get_all_positions.return_value = {
        "data": [{"symbol": s, "total": "1.0"} for s in open_symbols]
    }
    client._format_size.return_value = 0.5
    svc.client = client
    svc.entry_submitter.client = client
    svc.store.load = lambda default=None: list(existing_open or [])
    return svc


def test_override_pins_adaptive_trend_to_exactly_one_slot():
    assert ExecutionService.PER_STRATEGY_MAX_OPEN_POSITIONS_OVERRIDE["adaptive_trend_tsmom_v1"] == 1


def test_override_is_actually_wired_into_the_cap_check_source():
    """Not just present as a class attribute -- prove the live cap-check line
    actually reads through it, so removing the wiring (leaving the constant
    orphaned) would fail this test."""
    source = (REPO / "execution" / "execution_service.py").read_text()
    assert "PER_STRATEGY_MAX_OPEN_POSITIONS_OVERRIDE.get(" in source
    marker = source.index("PER_STRATEGY_MAX_OPEN_POSITIONS_OVERRIDE.get(")
    window = source[marker:marker + 400]
    assert "strategy_max_open" in window
    assert "open_for_strategy >= strategy_max_open" in source


def test_cap_override_mechanism_blocks_a_second_position_when_pinned_to_one(monkeypatch):
    """Generic proof of the mechanism through the real execute() path: with
    the override temporarily pinned for a hybrid-gate-supported strategy
    (low_vol_reclaim, which already passes the earlier gate untouched), one
    open position must block a second."""
    strategy = "low_vol_reclaim"
    existing = [{"symbol": "BTCUSDT", "status": "OPEN", "strategy": strategy}]
    svc = _service(monkeypatch, existing_open=existing)
    monkeypatch.setitem(svc.PER_STRATEGY_MAX_OPEN_POSITIONS_OVERRIDE, strategy, 1)

    reports = svc.execute([_plan("ETHUSDT", strategy)])
    assert reports[0].status == "SKIPPED"
    assert "max open positions for strategy reached: 1/1" in reports[0].message


def test_cap_override_mechanism_is_direction_agnostic_no_hedge(monkeypatch):
    """Same mechanism, same symbol, opposite direction -- proves a pinned
    one-slot cap blocks a would-be hedge regardless of direction, without
    needing a separate hedge-specific gate."""
    strategy = "low_vol_reclaim"
    existing = [{"symbol": "BTCUSDT", "status": "OPEN", "strategy": strategy}]
    svc = _service(monkeypatch, existing_open=existing)
    monkeypatch.setitem(svc.PER_STRATEGY_MAX_OPEN_POSITIONS_OVERRIDE, strategy, 1)

    reports = svc.execute([_plan("BTCUSDT", strategy, direction="SHORT")])
    assert reports[0].status == "SKIPPED"


def test_generic_strategy_still_gets_two_slots_unaffected(monkeypatch):
    """Without any override, the shared default (2) is untouched."""
    existing = [{"symbol": "BTCUSDT", "status": "OPEN", "strategy": "low_vol_reclaim"}]
    svc = _service(monkeypatch, existing_open=existing)
    reports = svc.execute([_plan("ETHUSDT", "low_vol_reclaim")])
    assert "max open positions for strategy reached" not in reports[0].message


def test_adaptive_trend_is_still_blocked_by_the_earlier_hybrid_gate(monkeypatch):
    """Confirms the current, deliberate scope boundary: adaptive_trend_tsmom_v1
    plans never reach the per-strategy cap at all right now, because the
    HYBRID SAFE MODE gate earlier in execute() rejects it first. This is the
    correct state until the owner explicitly wires live order eligibility."""
    svc = _service(monkeypatch, existing_open=[])
    reports = svc.execute([_plan("BTCUSDT", "adaptive_trend_tsmom_v1")])
    assert reports[0].status == "SKIPPED"
    assert "hybrid gate blocked unsupported strategy" in reports[0].message
