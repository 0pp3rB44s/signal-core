from __future__ import annotations

from unittest.mock import MagicMock

from app.config import Settings
from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id
from clients.schemas import TradePlan
from execution.position_manager import PositionManager
from risk.risk_manager import RiskManager


SPEC = "cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13"


def plan(notional: float = 2.744) -> TradePlan:
    strategy, symbol, direction, candle = "funding_crowding_continuation_24h", "DOGEUSDT", "LONG", 1_700_000_000_000
    candidate = deterministic_candidate_id(strategy, symbol, direction, candle)
    return TradePlan(
        candidate_id=candidate, candidate_candle_open_timestamp_ms=candle,
        plan_id=deterministic_plan_id(candidate), symbol=symbol, strategy=strategy,
        direction=direction, verdict="EXECUTABLE", score=80, entry_prices=[100],
        stop_loss=90, take_profits=[], risk_reward_ratio=0, account_risk_pct=10,
        leverage=1, position_notional_usdt=notional, notes=[], reasons=[],
        protection_mode="STOP_ONLY_TIME_EXIT", scheduled_exit_at_ms=4_000_000_000_000,
        frozen_spec_sha256=SPEC, pilot_authorized=True,
    )


def test_risk_manager_enforces_dynamic_nav_caps_and_kill_switch():
    manager = RiskManager(Settings(_env_file=None))
    approved = manager.validate_funding_pilot_plan(
        plan(), pilot_nav=27.44, available_margin=20, current_gross_notional=0,
        current_position_count=0, kill_switch_latched=False, native_stop_available=True,
    )
    assert approved.allowed
    grown = manager.validate_funding_pilot_plan(
        plan(3.0), pilot_nav=30.0, available_margin=20, current_gross_notional=3.0,
        current_position_count=1, kill_switch_latched=False, native_stop_available=True,
    )
    assert grown.allowed
    halted = manager.validate_funding_pilot_plan(
        plan(), pilot_nav=27.44, available_margin=20, current_gross_notional=0,
        current_position_count=0, kill_switch_latched=True, native_stop_available=True,
    )
    assert not halted.allowed and "kill switch" in " ".join(halted.reasons)


def test_position_manager_restart_loads_persisted_time_exit_then_closes_and_cancels_stop(tmp_path):
    row = {
        "symbol": "DOGEUSDT", "direction": "LONG", "strategy": "funding_crowding_continuation_24h",
        "status": "OPEN", "protection_mode": "STOP_ONLY_TIME_EXIT", "scheduled_exit_at_ms": 1000,
        "frozen_spec_sha256": SPEC, "pilot_authorized": True,
        "exchange_position_id": "p1", "protection_payload": {"stop_order_id": "stop-1"},
    }
    store = MagicMock()
    store.load.return_value = [row]
    restarted = object.__new__(PositionManager)
    restarted.store = store
    restarted.client = MagicMock()
    restarted.client.close_futures_position_full.return_value = {"status": "CLOSED", "orderId": "close-1"}
    restarted.client.get_all_positions.side_effect = [
        {"data": [{"symbol": "DOGEUSDT", "positionId": "p1", "total": "1"}]},
        {"data": []},
    ]
    restarted.client.cancel_futures_plan_order.return_value = {"cancelled": ["stop-1"]}
    restarted.client.get_futures_protection_orders.return_value = {"stop_orders": [], "take_profit_orders": []}
    restarted.client.get_pending_orders.return_value = {"data": {"entrustedList": []}}

    outcomes = restarted.process_stop_only_time_exits(now_ms=1001)

    assert outcomes == [{"symbol": "DOGEUSDT", "status": "POSITION_CLOSED_STOP_CANCELLED"}]
    restarted.client.close_futures_position_full.assert_called_once_with(symbol="DOGEUSDT", direction="LONG")
    restarted.client.cancel_futures_plan_order.assert_called_once_with(
        symbol="DOGEUSDT", order_id="stop-1", plan_type="loss_plan"
    )
    assert row["status"] == "CLOSED_TIME_EXIT"
    store.save.assert_called_once()


def test_position_manager_does_not_touch_adaptivetrend_rows():
    row = {"symbol": "BTCUSDT", "direction": "LONG", "strategy": "AdaptiveTrend", "status": "OPEN", "scheduled_exit_at_ms": 1}
    manager = object.__new__(PositionManager)
    manager.store = MagicMock()
    manager.store.load.return_value = [row]
    manager.client = MagicMock()
    assert manager.process_stop_only_time_exits(now_ms=2) == []
    manager.client.close_futures_position_full.assert_not_called()


def test_time_exit_refuses_close_status_without_exchange_zero_proof():
    row = {
        "symbol": "DOGEUSDT", "direction": "LONG", "strategy": "funding_crowding_continuation_24h",
        "status": "OPEN", "protection_mode": "STOP_ONLY_TIME_EXIT", "scheduled_exit_at_ms": 1,
        "frozen_spec_sha256": SPEC, "pilot_authorized": True,
        "exchange_position_id": "p1", "protection_payload": {"stop_order_id": "stop-1"},
    }
    manager = object.__new__(PositionManager)
    manager.store = MagicMock()
    manager.store.load.return_value = [row]
    manager.client = MagicMock()
    manager.client.close_futures_position_full.return_value = {"status": "CLOSED"}
    manager.client.get_all_positions.side_effect = [
        {"data": [{"symbol": "DOGEUSDT", "positionId": "p1", "total": "0.1"}]},
        {"data": [{"symbol": "DOGEUSDT", "positionId": "p1", "total": "0.1"}]},
    ]
    assert manager.process_stop_only_time_exits(now_ms=2) == [
        {"symbol": "DOGEUSDT", "status": "POSITION_ZERO_NOT_CONFIRMED"}
    ]
    manager.client.cancel_futures_plan_order.assert_not_called()
