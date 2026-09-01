from __future__ import annotations
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from funding_pilot.bitget_exchange import BitgetPilotExchangePort
from funding_pilot.core import FailClosed, PilotConfig, PilotLedger, PilotRuntime, PilotSignal
from funding_pilot.canonical import CanonicalFundingPilot
from funding_pilot.runner import CanonicalFundingPilotRunner


SPEC = Path(__file__).resolve().parents[1] / "research/validation/FROZEN_SPECS.json"


class TransportHarness:
    """Mocked transport below the real Bitget adapter, never the adapter itself."""
    def __init__(self):
        self.positions, self.orders, self.plans, self.history, self.bills = [], [], [], [], []
        self.write_calls = []
    def get_accounts(self):
        return {"data": [{"marginCoin": "USDT", "accountEquity": "100", "available": "80"}]}
    def get_all_positions(self): return {"data": list(self.positions)}
    def get_pending_orders(self, **_): return {"data": {"entrustedList": list(self.orders)}}
    def get_tpsl_orders(self, **_): return {"data": {"entrustedList": list(self.plans)}}
    def get_position_history(self, **_): return {"data": {"list": list(self.history)}}
    def _request(self, *_args, **_kwargs): return {"data": {"list": list(self.bills)}}
    def _min_notional(self, _symbol): return 1.0
    def get_orderbook(self, **_): return {"data": {"bids": [["99", "1"]], "asks": [["101", "1"]]}}
    def verify_active_stop_loss(self, **_): return {"verified": bool(self.plans)}
    def close_futures_position_full(self, **kwargs): self.write_calls.append(("close", kwargs)); self.positions=[]; return {"status":"CLOSED"}
    def cancel_futures_order(self, **kwargs): self.write_calls.append(("cancel_order", kwargs)); self.orders=[]; return {"code":"00000"}
    def cancel_futures_plan_order(self, **kwargs): self.write_calls.append(("cancel_stop", kwargs)); self.plans=[]; return {"code":"00000"}


def test_real_adapter_read_truth_ownership_and_economics(tmp_path):
    client, ledger = TransportHarness(), PilotLedger(tmp_path / "pilot.sqlite")
    ledger.append("ENTRY_INTENT", {"symbol":"DOGEUSDT", "entry_client_oid":"cgc-fcp-entry",
                                    "scheduled_exit_at_ms":999}, symbol="DOGEUSDT")
    client.positions = [{"symbol":"DOGEUSDT", "total":"2", "openPriceAvg":"10", "markPrice":"11", "unrealizedPL":"2"},
                        {"symbol":"BTCUSDT", "total":"1", "openPriceAvg":"100", "markPrice":"100", "unrealizedPL":"0", "marginSize":"10"}]
    client.orders = [{"symbol":"BTCUSDT", "clientOid":"adaptivetrend-order"}]
    client.plans = [{"symbol":"DOGEUSDT", "orderId":"stop-1", "triggerPrice":"9"},
                    {"symbol":"BTCUSDT", "orderId":"adaptive-stop", "clientOid":"adaptivetrend-stop"}]
    ledger.append("CANONICAL_OPEN", {"symbol":"DOGEUSDT", "entry_client_oid":"cgc-fcp-entry",
                                      "stop_order_id":"stop-1", "scheduled_exit_at_ms":999,
                                      "exchange_position_id":"p1"}, symbol="DOGEUSDT")
    client.positions[0]["positionId"] = "p1"
    client.history = [{"symbol":"DOGEUSDT", "positionId":"p1", "pnl":"1.5", "openFee":"-0.1",
                       "closeFee":"-0.1", "totalFunding":"0.05", "utime":"1"}]
    truth = BitgetPilotExchangePort(client, ledger).truth()
    assert [row["symbol"] for row in truth.pilot_positions] == ["DOGEUSDT"]
    assert not truth.pilot_working_orders
    assert [row["order_id"] for row in truth.pilot_stops] == ["stop-1"]
    economics = ledger.economics(truth)
    assert economics["realized_pnl"] == pytest.approx(1.5)
    assert economics["fees"] == pytest.approx(0.2)
    assert economics["funding"] == pytest.approx(-0.05)


def test_real_adapter_is_hard_disarmed_and_foreign_state_is_never_mutated(tmp_path):
    client, ledger = TransportHarness(), PilotLedger(tmp_path / "pilot.sqlite")
    adapter = BitgetPilotExchangePort(client, ledger, armed_live=False)
    with pytest.raises(FailClosed, match="REAL_ORDER_ARMED=FALSE"):
        adapter.close_reduce_only({"symbol":"DOGEUSDT", "side":"LONG"}, "test")
    assert client.write_calls == []


def test_real_runner_is_schedulable_dry_run_and_uses_real_adapter_reads(tmp_path):
    client = TransportHarness()
    execution = MagicMock()
    execution.client = client
    positions = MagicMock()
    positions.store.load.return_value = []
    now = 2_000_000_000_000
    runner = CanonicalFundingPilotRunner(
        execution_service=execution, position_manager=positions, spec_path=SPEC,
        ledger_path=tmp_path / "pilot.sqlite", armed_live=False,
        signal_poller=lambda: [PilotSignal("s1", now, "DOGEUSDT", "LONG", 100, {"funding":1})],
    )
    assert runner.armed_live is False and runner.exchange.armed_live is False
    result = runner.tick(now_ms=now)
    assert result["decisions"][0]["status"] == "NO_ORDER"
    assert result["decisions"][0]["notional"] == pytest.approx(2.744)
    execution.execute.assert_not_called()
    assert runner.ledger.events("HEARTBEAT")


def test_real_adapter_armed_stop_mismatch_flatten_and_kill_mutations_are_owned(tmp_path):
    client, ledger = TransportHarness(), PilotLedger(tmp_path / "pilot.sqlite")
    ledger.append("ENTRY_INTENT", {"symbol":"DOGEUSDT", "entry_client_oid":"cgc-fcp-entry",
                                    "scheduled_exit_at_ms":999}, symbol="DOGEUSDT")
    client.positions = [{"symbol":"DOGEUSDT", "total":"1", "openPriceAvg":"10", "markPrice":"10", "unrealizedPL":"0"}]
    adapter = BitgetPilotExchangePort(client, ledger, armed_live=True)
    adapter.close_reduce_only({"symbol":"DOGEUSDT", "side":"LONG"}, "stop_mismatch")
    assert client.write_calls == [("close", {"symbol":"DOGEUSDT", "direction":"LONG"})]
    assert adapter.truth().pilot_positions == ()


def test_real_adapter_path_flattens_when_acknowledged_stop_is_missing(tmp_path):
    client, ledger = TransportHarness(), PilotLedger(tmp_path / "pilot.sqlite")
    adapter = BitgetPilotExchangePort(client, ledger, armed_live=True)
    runtime = PilotRuntime(replace(PilotConfig(SPEC, tmp_path / "pilot.sqlite"), orders_enabled=True), ledger, adapter)
    execution = MagicMock()
    execution.client = client
    execution.entry_submitter.client_oid_for.return_value = "cgc-fcp-entry"
    def execute(_plans):
        client.positions = [{"symbol":"DOGEUSDT", "total":"0.02744", "openPriceAvg":"100",
                             "markPrice":"100", "unrealizedPL":"0", "positionId":"p1"}]
        report = MagicMock(); report.status = "EXECUTED"
        return [report]
    execution.execute.side_effect = execute
    execution.store.load.return_value = [{
        "plan_id": "ignored", "status":"OPEN", "entry_protection_verified": True,
        "protection_payload": {"stop_order_id":"missing-stop"},
    }]
    positions = MagicMock(); positions.store.load.return_value = []
    canonical = CanonicalFundingPilot(runtime, execution, positions)
    signal = PilotSignal("s1", 2_000_000_000_000, "DOGEUSDT", "LONG", 100, {"funding":1})
    # Make the persisted row follow the deterministic plan id produced internally.
    original_load = execution.store.load
    def load(default=None):
        plan_id = execution.execute.call_args.args[0][0].plan_id
        return [{"plan_id":plan_id, "status":"OPEN", "entry_protection_verified":True,
                 "protection_payload":{"stop_order_id":"missing-stop"},
                 "exchange_entry_client_oid":"cgc-fcp-entry", "exchange_position_id":"p1",
                 "confirmed_opening_fee_usdt":0.01}]
    execution.store.load.side_effect = load
    with pytest.raises(FailClosed, match="flattened"):
        canonical.process_signal(signal)
    assert client.positions == []
    assert any(kind == "close" for kind, _ in client.write_calls)


def test_real_adapter_path_kill_switch_cancels_flattens_verifies_and_latches(tmp_path):
    client, ledger = TransportHarness(), PilotLedger(tmp_path / "pilot.sqlite")
    ledger.append("CANONICAL_OPEN", {"symbol":"DOGEUSDT", "entry_client_oid":"cgc-fcp-entry",
                                      "stop_order_id":"stop-1", "scheduled_exit_at_ms":999,
                                      "exchange_position_id":"p1"}, symbol="DOGEUSDT")
    client.positions = [{"symbol":"DOGEUSDT", "total":"0.02744", "openPriceAvg":"100",
                         "markPrice":"100", "unrealizedPL":"-1.4", "positionId":"p1"}]
    client.orders = [{"symbol":"DOGEUSDT", "orderId":"working-1", "clientOid":"cgc-fcp-exit"}]
    client.plans = [{"symbol":"DOGEUSDT", "orderId":"stop-1", "triggerPrice":"90"}]
    adapter = BitgetPilotExchangePort(client, ledger, armed_live=True)
    runtime = PilotRuntime(replace(PilotConfig(SPEC, tmp_path / "pilot.sqlite"), orders_enabled=True), ledger, adapter)
    runtime.reconcile()
    assert ledger.get("status") == "HALTED"
    assert adapter.truth().pilot_positions == ()
    assert adapter.truth().pilot_working_orders == ()
    assert adapter.truth().pilot_stops == ()
    assert {kind for kind, _ in client.write_calls} == {"cancel_order", "close", "cancel_stop"}


def test_same_symbol_unproven_position_conflict_fails_without_mutation(tmp_path):
    client, ledger = TransportHarness(), PilotLedger(tmp_path / "pilot.sqlite")
    ledger.append("ENTRY_INTENT", {"symbol":"DOGEUSDT", "entry_client_oid":"cgc-fcp-entry",
                                    "scheduled_exit_at_ms":999}, symbol="DOGEUSDT")
    client.positions = [{"symbol":"DOGEUSDT", "positionId":"adaptive-pos", "total":"1",
                         "openPriceAvg":"100", "markPrice":"100", "unrealizedPL":"0"}]
    adapter = BitgetPilotExchangePort(client, ledger, armed_live=True)
    with pytest.raises(FailClosed, match="SKIP_SIGNAL_SHARED_EXPOSURE_CONFLICT"):
        adapter.truth()
    assert client.write_calls == []


def test_foreign_same_symbol_history_is_not_ingested_without_exact_position_id(tmp_path):
    client, ledger = TransportHarness(), PilotLedger(tmp_path / "pilot.sqlite")
    ledger.append("ENTRY_INTENT", {"symbol":"DOGEUSDT", "entry_client_oid":"cgc-fcp-entry",
                                    "scheduled_exit_at_ms":999}, symbol="DOGEUSDT")
    client.history = [{"symbol":"DOGEUSDT", "positionId":"adaptive-closed", "pnl":"99",
                       "openFee":"-1", "closeFee":"-1", "totalFunding":"1"}]
    BitgetPilotExchangePort(client, ledger).truth()
    assert ledger.events("ECONOMICS") == []


@pytest.mark.parametrize("kind", ["entry", "reduce_only", "stop", "plan", "multiple"])
def test_every_foreign_same_symbol_order_kind_is_a_no_mutation_conflict(tmp_path, kind):
    client, ledger = TransportHarness(), PilotLedger(tmp_path / "pilot.sqlite")
    ledger.append("ENTRY_INTENT", {"symbol":"DOGEUSDT", "entry_client_oid":"cgc-fcp-entry",
                                    "scheduled_exit_at_ms":999}, symbol="DOGEUSDT")
    regular = {"symbol":"DOGEUSDT", "orderId":"foreign-1", "clientOid":"adaptive-1"}
    plan = {"symbol":"DOGEUSDT", "orderId":"foreign-plan", "clientOid":"adaptive-plan"}
    if kind == "reduce_only": regular["reduceOnly"] = "YES"
    if kind in {"entry", "reduce_only"}: client.orders = [regular]
    elif kind in {"stop", "plan"}: client.plans = [plan]
    else: client.orders, client.plans = [regular, {**regular, "orderId":"foreign-2"}], [plan]
    with pytest.raises(FailClosed, match="SKIP_SIGNAL_SHARED_EXPOSURE_CONFLICT"):
        BitgetPilotExchangePort(client, ledger, armed_live=True).truth()
    assert client.write_calls == []


def test_newer_stop_ack_identity_cannot_be_overwritten_by_older_open_event(tmp_path):
    client, ledger = TransportHarness(), PilotLedger(tmp_path / "pilot.sqlite")
    ledger.append("CANONICAL_OPEN", {"symbol":"DOGEUSDT", "entry_client_oid":"cgc-fcp-entry",
                                      "stop_order_id":"old", "exchange_position_id":"p1"}, symbol="DOGEUSDT")
    ledger.append("STOP_ACK", {"symbol":"DOGEUSDT", "entry_client_oid":"cgc-fcp-entry",
                               "stop_order_id":"new", "exchange_position_id":"p1"}, symbol="DOGEUSDT")
    assert BitgetPilotExchangePort(client, ledger)._owned()["DOGEUSDT"]["stop_order_id"] == "new"


def test_open_position_accrued_funding_updates_current_nav_before_close(tmp_path):
    client, ledger = TransportHarness(), PilotLedger(tmp_path / "pilot.sqlite")
    ledger.append("CANONICAL_OPEN", {"symbol":"DOGEUSDT", "entry_client_oid":"cgc-fcp-entry",
        "stop_order_id":"stop-1", "exchange_position_id":"p1"}, symbol="DOGEUSDT")
    client.positions = [{"symbol":"DOGEUSDT", "positionId":"p1", "total":"0.02744",
        "openPriceAvg":"100", "markPrice":"100", "unrealizedPL":"0"}]
    client.plans = [{"symbol":"DOGEUSDT", "orderId":"stop-1"}]
    client.bills = [{"id":"bill-1", "positionId":"p1", "amount":"-0.10"}]
    adapter = BitgetPilotExchangePort(client, ledger)
    truth = adapter.truth()
    economics = ledger.economics(truth)
    assert economics["funding"] == pytest.approx(0.10)
    assert economics["nav"] == pytest.approx(27.34)
