"""HYBRID SAFE MODE gate: adaptive_trend_tsmom_v1 becomes a recognized live
strategy ONLY when ADAPTIVE_TREND_LIVE_ENTRY_ENABLED is explicitly true.
Every other strategy's supported/blocked behavior is unchanged.

Binds against the real ExecutionService.execute() gate condition -- not a
double -- so if the flag coupling is removed, or any other strategy's
eligibility drifts, these go red.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id
from clients.schemas import TradePlan
from execution.execution_service import ExecutionService
from tests.test_entry_path_audit import _live_settings, _service

CANDLE_MS = 1_785_700_000_000
ADAPTIVE_STRATEGY = "adaptive_trend_tsmom_v1"


def _adaptive_plan(symbol="BTCUSDT", direction="LONG") -> TradePlan:
    cid = deterministic_candidate_id(ADAPTIVE_STRATEGY, symbol, direction, CANDLE_MS)
    long_side = direction.upper() == "LONG"
    return TradePlan(
        candidate_id=cid,
        candidate_candle_open_timestamp_ms=CANDLE_MS,
        plan_id=deterministic_plan_id(cid),
        symbol=symbol, strategy=ADAPTIVE_STRATEGY, direction=direction,
        verdict="EXECUTABLE", score=90.0,
        entry_prices=[100.0],
        stop_loss=95.0 if long_side else 105.0,
        take_profits=[],  # trailing-stop-only, by design
        risk_reward_ratio=0.0, account_risk_pct=0.5, leverage=2.0,
        position_notional_usdt=26.79, notes=[], reasons=[], geometry_entry=100.0,
    )


def _service_with_flag(monkeypatch, *, adaptive_flag: bool, **overrides) -> ExecutionService:
    monkeypatch.setattr("execution.execution_service.resolve_account_equity",
                        lambda _s: (1000.0, "test"))
    settings = _live_settings(ADAPTIVE_TREND_LIVE_ENTRY_ENABLED=adaptive_flag, **overrides)
    svc = ExecutionService(settings=settings)
    client = MagicMock()
    client.get_all_positions.return_value = {"data": []}
    client._format_size.return_value = 0.5
    client._contract_price_scale.return_value = 2
    client.extract_fill_metrics.return_value = {
        "order_id": "srv-1", "avg_price": 100.0, "filled_qty": 0.5, "fee": 0.01,
        "pnl": 0.0, "state": "filled",
    }
    client.place_futures_market_order.return_value = {"data": {"orderId": "srv-1"}}
    client.set_futures_leverage.return_value = {"code": "00000", "data": {}}
    client.place_futures_protection_orders.return_value = {
        "protection_verified": True, "protection_integrity": "OK",
        "stop_loss_verified": True, "take_profit_count": 0,
        "expected_take_profit_count": 0,
    }
    client.get_order_detail.return_value = {"data": {"orderId": "srv-1"}}
    svc.client = client
    svc.entry_submitter.client = client
    return svc


# --- 1/2. flag gates AdaptiveTrend recognition at the hybrid gate itself ---


def test_adaptive_trend_blocked_when_flag_false(monkeypatch):
    svc = _service_with_flag(monkeypatch, adaptive_flag=False)
    reports = svc.execute([_adaptive_plan()])
    assert reports[0].status == "SKIPPED"
    assert "hybrid gate blocked unsupported strategy" in reports[0].message
    assert svc.client.place_futures_market_order.call_count == 0


def test_adaptive_trend_blocked_when_flag_missing_default(monkeypatch):
    """Settings default is False -- omitting the override must behave
    identically to explicitly passing False."""
    svc = _service_with_flag(monkeypatch, adaptive_flag=False)
    reports = svc.execute([_adaptive_plan()])
    assert "hybrid gate blocked unsupported strategy" in reports[0].message


def test_adaptive_trend_permitted_past_the_gate_when_flag_true(monkeypatch):
    svc = _service_with_flag(monkeypatch, adaptive_flag=True)
    svc.client.get_all_positions.side_effect = [
        {"data": []}, {"data": []},
        {"data": [{"symbol": "BTCUSDT", "holdSide": "long", "total": "0.5",
                    "openPriceAvg": "100.0", "markPrice": "100.0"}]},
    ]
    reports = svc.execute([_adaptive_plan()])
    assert "hybrid gate blocked unsupported strategy" not in reports[0].message
    assert svc.client.place_futures_market_order.call_count == 1


# --- 3. MicroFlow remains blocked (retirement gate lives in RiskManager,
# not here -- but its hybrid-gate SUPPORT must remain unaffected either way)


def test_microflow_strategy_name_still_recognized_by_hybrid_gate(monkeypatch):
    """MicroFlow's hybrid-gate recognition (is_microflow) is untouched by
    this patch -- its actual retirement happens one layer up in RiskManager
    and is out of scope here, but the gate itself must not have regressed."""
    from execution.execution_service import is_microflow_scalper_v1
    assert is_microflow_scalper_v1("microflow_scalper_v1") is True


# --- 4. unknown strategy remains blocked regardless of the flag -----------


def test_unknown_strategy_still_blocked_even_with_flag_true(monkeypatch):
    svc = _service_with_flag(monkeypatch, adaptive_flag=True)
    cid = deterministic_candidate_id("totally_unknown_strategy", "BTCUSDT", "LONG", CANDLE_MS)
    plan = TradePlan(
        candidate_id=cid, candidate_candle_open_timestamp_ms=CANDLE_MS,
        plan_id=deterministic_plan_id(cid), symbol="BTCUSDT",
        strategy="totally_unknown_strategy", direction="LONG",
        verdict="EXECUTABLE", score=90.0, entry_prices=[100.0], stop_loss=95.0,
        take_profits=[110.0], risk_reward_ratio=1.3, account_risk_pct=0.5,
        leverage=2.0, position_notional_usdt=26.79, notes=[], reasons=[],
        geometry_entry=100.0,
    )
    reports = svc.execute([plan])
    assert "hybrid gate blocked unsupported strategy" in reports[0].message


# --- 5. existing supported strategies behave unchanged ---------------------


def test_low_vol_reclaim_unaffected_by_the_new_flag_in_either_state(monkeypatch):
    for flag in (False, True):
        svc = _service_with_flag(monkeypatch, adaptive_flag=flag)
        cid = deterministic_candidate_id("low_vol_reclaim", "BTCUSDT", "LONG", CANDLE_MS)
        plan = TradePlan(
            candidate_id=cid, candidate_candle_open_timestamp_ms=CANDLE_MS,
            plan_id=deterministic_plan_id(cid), symbol="BTCUSDT",
            strategy="low_vol_reclaim", direction="LONG",
            verdict="EXECUTABLE", score=90.0, entry_prices=[100.0], stop_loss=95.0,
            take_profits=[110.0], risk_reward_ratio=1.3, account_risk_pct=0.5,
            leverage=2.0, position_notional_usdt=26.79, notes=[], reasons=[],
            geometry_entry=100.0,
        )
        svc.client.get_all_positions.side_effect = [
            {"data": []}, {"data": []},
            {"data": [{"symbol": "BTCUSDT", "holdSide": "long", "total": "0.5",
                        "openPriceAvg": "100.0", "markPrice": "100.0"}]},
        ]
        reports = svc.execute([plan])
        assert "hybrid gate blocked unsupported strategy" not in reports[0].message


# --- 6. weekly freeze still blocks AdaptiveTrend even with flag TRUE ------
# (enforced upstream of execute() in execution/adaptive_trend_entry.py,
# independently of this gate -- proven here it is untouched by this patch)


def test_weekly_freeze_gate_upstream_of_execute_is_untouched(monkeypatch):
    from execution.adaptive_trend_entry import submit_adaptive_trend_entry
    from strategies.adaptive_trend_tsmom import Side, SignalCandidate, initial_stop, size_position

    winner = SignalCandidate(symbol="BTCUSDT", side=Side.LONG, signal_candle_close_ms=CANDLE_MS,
                              close=100.0, mom=0.05, atr=2.0)
    stop = initial_stop(winner.close, winner.atr, winner.side)
    sizing = size_position(equity=1000.0, entry_price=winner.close, stop_price=stop,
                            exchange_min_notional=5.0)
    svc = MagicMock()
    result = submit_adaptive_trend_entry(
        winner=winner, winner_sizing=sizing, weekly_freeze_active=True,
        execution_service=svc,
    )
    assert result is None
    svc.execute.assert_not_called()


# --- 7. exposure/risk rejection still blocks AdaptiveTrend normally -------


def test_max_open_positions_still_blocks_adaptive_trend_when_flag_true(monkeypatch):
    svc = _service_with_flag(monkeypatch, adaptive_flag=True, MAX_OPEN_POSITIONS=2)
    svc.client.get_all_positions.return_value = {
        "data": [
            {"symbol": "BTCUSDT", "holdSide": "long", "total": "0.5"},
            {"symbol": "ETHUSDT", "holdSide": "long", "total": "0.5"},
        ]
    }
    reports = svc.execute([_adaptive_plan(symbol="SOLUSDT")])
    assert reports[0].status == "SKIPPED"
    assert svc.client.place_futures_market_order.call_count == 0


# --- 8. trailing-stop-only AdaptiveTrend not rejected for zero TPs --------


def test_zero_take_profits_not_rejected_when_flag_true(monkeypatch):
    svc = _service_with_flag(monkeypatch, adaptive_flag=True)
    svc.client.get_all_positions.side_effect = [
        {"data": []}, {"data": []},
        {"data": [{"symbol": "BTCUSDT", "holdSide": "long", "total": "0.5",
                    "openPriceAvg": "100.0", "markPrice": "100.0"}]},
    ]
    reports = svc.execute([_adaptive_plan()])
    assert "invalid or missing SL/TP" not in reports[0].message
    assert "take_profit_invalid" not in reports[0].message


# --- 9. successful AdaptiveTrend path still requires/provisions initial SL


def test_zero_stop_loss_still_rejected_even_with_flag_true(monkeypatch):
    """A zero stop is rejected even earlier than the hybrid gate -- at
    portfolio selection itself (select_execution_winner's own
    stop_loss_invalid check) -- so execute() never even reaches this plan.
    Either way, zero orders are placed."""
    svc = _service_with_flag(monkeypatch, adaptive_flag=True)
    plan = _adaptive_plan()
    plan.stop_loss = 0.0
    reports = svc.execute([plan])
    assert reports == []
    assert svc.client.place_futures_market_order.call_count == 0


def test_successful_entry_places_protection_with_the_real_stop(monkeypatch):
    svc = _service_with_flag(monkeypatch, adaptive_flag=True)
    svc.client.get_all_positions.side_effect = [
        {"data": []}, {"data": []},
        {"data": [{"symbol": "BTCUSDT", "holdSide": "long", "total": "0.5",
                    "openPriceAvg": "100.0", "markPrice": "100.0"}]},
    ]
    reports = svc.execute([_adaptive_plan()])
    assert reports[0].status == "EXECUTED"
    assert svc.client.place_futures_protection_orders.call_count == 1
    call = svc.client.place_futures_protection_orders.call_args.kwargs
    assert call["stop_loss"] > 0
    assert call["take_profits"] == []


# --- 10. duplicate/one-position protections remain active -----------------


def test_one_position_cap_still_applies_when_flag_true(monkeypatch):
    svc = _service_with_flag(monkeypatch, adaptive_flag=True)
    svc.store.save([{"symbol": "BTCUSDT", "status": "OPEN", "strategy": ADAPTIVE_STRATEGY}])
    svc.client.get_all_positions.return_value = {
        "data": [{"symbol": "BTCUSDT", "holdSide": "long", "total": "0.5"}]
    }
    reports = svc.execute([_adaptive_plan(symbol="ETHUSDT")])
    assert reports[0].status == "SKIPPED"
    assert svc.client.place_futures_market_order.call_count == 0
