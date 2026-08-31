from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from funding_pilot.core import (
    ExchangeTruth,
    FailClosed,
    PilotConfig,
    PilotLedger,
    PilotRuntime,
    PilotSignal,
)
from funding_pilot.canonical import CanonicalFundingPilot


class FakeExchange:
    def __init__(self, *, minimum=1.0, available=100.0):
        self.minimum = minimum
        self.state = ExchangeTruth(True, 100.0, available, (), (), (), 20.0)
        self.calls = []

    def truth(self): return self.state
    def min_notional(self, symbol): return self.minimum
    def decision_book(self, symbol): return {"bid": 99.0, "ask": 101.0, "timestamp_ms": 1}
    def submit_entry(self, **kwargs): self.calls.append(("entry", kwargs)); return {"order_id": "e1"}
    def place_native_stop(self, *, position, stop_price):
        stop = {"symbol": position["symbol"], "side": position["side"], "stop_price": stop_price, "order_id": "s1", "client_oid": position["client_oid"]}
        self.state = replace(self.state, pilot_stops=(stop,))
        self.calls.append(("stop", stop))
        return stop
    def verify_native_stop(self, *, symbol, side, stop_price):
        return {"verified": any(x["symbol"] == symbol and x["stop_price"] == stop_price for x in self.state.pilot_stops)}
    def cancel_working_order(self, order): self.calls.append(("cancel_order", order))
    def close_reduce_only(self, position, reason):
        self.calls.append(("close", reason))
        self.state = replace(self.state, pilot_positions=())
        return {"status": "CONFIRMED_FLAT", "reason": reason}
    def cancel_stop(self, stop):
        self.calls.append(("cancel_stop", stop))
        self.state = replace(self.state, pilot_stops=tuple(x for x in self.state.pilot_stops if x != stop))


@pytest.fixture
def runtime(tmp_path):
    spec = tmp_path / "spec.json"
    source_spec = Path(__file__).resolve().parents[1] / "research/validation/FROZEN_SPECS.json"
    spec.write_bytes(source_spec.read_bytes())
    exchange = FakeExchange()
    ledger = PilotLedger(tmp_path / "pilot.sqlite")
    config = PilotConfig(spec_path=spec, state_path=tmp_path / "pilot.sqlite")
    return PilotRuntime(config, ledger, exchange), exchange, ledger


def signal():
    return PilotSignal("sig-1", 1, "DOGEUSDT", "LONG", 100.0, {"funding_pct": .99, "extension": 2.0})


def test_dynamic_compounding_and_no_order_telemetry(runtime):
    pilot, exchange, ledger = runtime
    result = pilot.process_signal(signal())
    assert result == {"status": "NO_ORDER", "notional": pytest.approx(2.744)}
    assert not exchange.calls
    assert ledger.events("SIGNAL")[0]["payload"]["target_notional"] == pytest.approx(2.744)


def test_min_notional_skips_without_increasing_exposure(runtime):
    pilot, exchange, _ = runtime
    exchange.minimum = 5.0
    with pytest.raises(FailClosed, match="SKIP_MIN_NOTIONAL"):
        pilot.process_signal(signal())
    assert not exchange.calls


def test_spec_mismatch_fails_closed(runtime):
    pilot, exchange, _ = runtime
    pilot.config.spec_path.write_text("tampered")
    with pytest.raises(FailClosed, match="integrity mismatch"):
        pilot.process_signal(signal())
    assert not exchange.calls


def test_unknown_unrealized_pnl_and_unprotected_position_fail_closed(runtime):
    pilot, exchange, _ = runtime
    position = {"symbol": "DOGEUSDT", "client_oid": "cgc-fcp-1", "notional": 2.0, "unrealized_pnl": None}
    exchange.state = replace(exchange.state, pilot_positions=(position,))
    with pytest.raises(FailClosed, match="unrealized PnL unknown"):
        pilot.reconcile()
    position["unrealized_pnl"] = 0.0
    with pytest.raises(FailClosed, match="unprotected"):
        pilot.reconcile()


def test_native_stop_ack_duplicate_prevention_recovery_and_normal_exit(runtime):
    pilot, exchange, ledger = runtime
    pilot.config = replace(pilot.config, orders_enabled=True)
    position = {"symbol": "DOGEUSDT", "side": "LONG", "entry_price": 100.0,
                "client_oid": "cgc-fcp-1", "notional": 2.744, "unrealized_pnl": 0.0}
    exchange.state = replace(exchange.state, pilot_positions=(position,))
    first = pilot.protect_filled_position(position)
    assert first["status"] == "VERIFIED"
    second = pilot.protect_filled_position(position)
    assert second["status"] == "ALREADY_VERIFIED"
    assert len([call for call in exchange.calls if call[0] == "stop"]) == 1
    recovered = PilotRuntime(pilot.config, PilotLedger(ledger.path), exchange)
    assert recovered.reconcile()["economics"]["nav"] == pytest.approx(27.44)
    recovered.normal_time_exit(position)
    assert not exchange.state.pilot_positions and not exchange.state.pilot_stops


def test_duplicate_and_orphan_stops_fail_closed(runtime):
    pilot, exchange, _ = runtime
    stop = {"symbol": "DOGEUSDT", "stop_price": 90.0, "client_oid": "cgc-fcp-1"}
    exchange.state = replace(exchange.state, pilot_stops=(stop,))
    with pytest.raises(FailClosed, match="orphan pilot stop"):
        pilot.reconcile()
    exchange.state = replace(exchange.state, pilot_stops=(stop, stop))
    with pytest.raises(FailClosed, match="duplicate pilot stop"):
        pilot.reconcile()


def test_kill_switch_latches_and_does_not_auto_reset(runtime):
    pilot, exchange, ledger = runtime
    ledger.append("ECONOMICS", {"realized_pnl": -1.40, "fees": 0, "funding": 0, "other_costs": 0})
    pilot.reconcile()
    assert ledger.get("status") == "HALTED"
    ledger.append("ECONOMICS", {"realized_pnl": 2.0, "fees": 0, "funding": 0, "other_costs": 0})
    with pytest.raises(FailClosed, match="pilot not ACTIVE"):
        pilot.process_signal(signal())


def test_execution_telemetry_is_complete_and_restart_safe(runtime):
    pilot, _, ledger = runtime
    payload = {key: 1 for key in __import__("funding_pilot.core", fromlist=["REQUIRED_EXECUTION_TELEMETRY"]).REQUIRED_EXECUTION_TELEMETRY}
    payload.update({"symbol": "DOGEUSDT", "side": "LONG", "signal_inputs": {"funding_pct": .99}})
    pilot.record_execution_telemetry(payload, signal_id="sig-1")
    reopened = PilotLedger(ledger.path)
    assert reopened.events("EXECUTION_TELEMETRY")[0]["payload"]["fill_price"] == 1
    del payload["fees"]
    with pytest.raises(FailClosed, match="fees"):
        pilot.record_execution_telemetry(payload, signal_id="sig-2")


def test_live_kill_switch_cancels_flattens_verifies_and_latches(runtime):
    pilot, exchange, ledger = runtime
    pilot.config = replace(pilot.config, orders_enabled=True)
    position = {"symbol": "DOGEUSDT", "side": "LONG", "entry_price": 100.0,
                "client_oid": "cgc-fcp-1", "notional": 2.744, "unrealized_pnl": -1.40}
    order = {"symbol": "DOGEUSDT", "client_oid": "cgc-fcp-entry"}
    stop = {"symbol": "DOGEUSDT", "stop_price": 90.0, "client_oid": "cgc-fcp-stop"}
    exchange.state = replace(exchange.state, pilot_positions=(position,),
                             pilot_working_orders=(order,), pilot_stops=(stop,))
    # Fake cancellation mutates working-order truth for final verification.
    exchange.cancel_working_order = lambda item: setattr(
        exchange, "state", replace(exchange.state, pilot_working_orders=()))
    pilot.reconcile()
    assert ledger.get("status") == "HALTED"
    assert not exchange.state.pilot_positions and not exchange.state.pilot_stops


def test_canonical_adapter_derives_dynamic_size_from_reconciled_ledger(runtime):
    pilot, _, ledger = runtime
    pilot.config = replace(pilot.config, orders_enabled=True)
    execution = MagicMock()
    canonical = CanonicalFundingPilot(pilot, execution, MagicMock())
    assert canonical.build_plan(signal()).position_notional_usdt == pytest.approx(2.744)
    ledger.append("ECONOMICS", {"realized_pnl": 2.56, "fees": 0, "funding": 0, "other_costs": 0})
    assert canonical.build_plan(signal()).position_notional_usdt == pytest.approx(3.0)
    assert execution.funding_pilot_state_provider == canonical.authoritative_state


def test_canonical_restart_recovers_exchange_schedule_hwm_and_latch(runtime):
    pilot, exchange, ledger = runtime
    pilot.config = replace(pilot.config, orders_enabled=True)
    position = {"symbol": "DOGEUSDT", "client_oid": "cgc-fcp-1", "notional": 2.744, "unrealized_pnl": 0.0}
    stop = {"symbol": "DOGEUSDT", "client_oid": "cgc-fcp-stop", "stop_price": 90.0}
    exchange.state = replace(exchange.state, pilot_positions=(position,), pilot_stops=(stop,))
    ledger.append("CANONICAL_OPEN", {"scheduled_exit_at_ms": 99_000, "notional": 2.744}, symbol="DOGEUSDT")
    ledger.set("high_water_mark", 30.0)
    restarted = PilotRuntime(pilot.config, PilotLedger(ledger.path), exchange)
    recovered = CanonicalFundingPilot(restarted, MagicMock(), MagicMock()).recover()
    assert recovered["scheduled_exits"] == {"DOGEUSDT": 99_000}
    assert recovered["high_water_mark"] == 30.0
    restarted.ledger.set("status", "HALTED")
    with pytest.raises(FailClosed, match="pilot not ACTIVE"):
        restarted.size(signal(), restarted.reconcile())


def test_foreign_owned_state_fails_closed(runtime):
    pilot, exchange, _ = runtime
    foreign = {"symbol": "BTCUSDT", "client_oid": "adaptivetrend-1", "notional": 10, "unrealized_pnl": 0}
    exchange.state = replace(exchange.state, pilot_positions=(foreign,))
    with pytest.raises(FailClosed, match="identity uncertain"):
        pilot.reconcile()
