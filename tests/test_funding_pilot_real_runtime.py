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
    client.history = [{"symbol":"DOGEUSDT", "positionId":"p1", "orderId":"close-1", "pnl":"1.5", "openFee":"-0.1",
                       "closeFee":"-0.1", "totalFunding":"0.05", "utime":"1"}]
    truth = BitgetPilotExchangePort(client, ledger).truth()
    assert [row["symbol"] for row in truth.pilot_positions] == ["DOGEUSDT"]
    assert not truth.pilot_working_orders
    assert [row["order_id"] for row in truth.pilot_stops] == ["stop-1"]
    economics = ledger.economics(truth)
    assert economics["realized_pnl"] == pytest.approx(1.5)
    assert economics["fees"] == pytest.approx(0.2)
    assert economics["funding"] == pytest.approx(0.0)


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


def test_economics_overlap_and_restart_replay_are_exactly_once(tmp_path):
    client, ledger = TransportHarness(), PilotLedger(tmp_path / "pilot.sqlite")
    ledger.append("CANONICAL_OPEN", {"symbol":"DOGEUSDT", "entry_client_oid":"cgc-fcp-entry",
        "stop_order_id":"stop-1", "exchange_position_id":"p1"}, symbol="DOGEUSDT")
    ledger.append_economic_once("opening_fee:cgc-fcp-entry",
        {"realized_pnl":0, "fees":0.1, "funding":0, "other_costs":0}, symbol="DOGEUSDT")
    client.history = [{"symbol":"DOGEUSDT", "positionId":"p1", "closeOrderId":"close-1", "pnl":"1", "openFee":"-0.1",
                       "closeFee":"-0.2", "totalFunding":"-0.05"}]
    client.bills = [{"id":"fund-1", "positionId":"p1", "amount":"-0.05"}]
    adapter = BitgetPilotExchangePort(client, ledger)
    first = ledger.economics(adapter.truth())
    restarted = BitgetPilotExchangePort(client, PilotLedger(ledger.path))
    second = restarted.ledger.economics(restarted.truth())
    assert first == second
    assert second["fees"] == pytest.approx(0.3)
    assert second["funding"] == pytest.approx(0.05)
    assert second["realized_pnl"] == pytest.approx(1.0)


def test_rejected_intent_is_terminal_and_does_not_poison_next_attempt(tmp_path):
    client, ledger = TransportHarness(), PilotLedger(tmp_path / "pilot.sqlite")
    adapter = BitgetPilotExchangePort(client, ledger, armed_live=True)
    runtime = PilotRuntime(replace(PilotConfig(SPEC, tmp_path / "pilot.sqlite"), orders_enabled=True), ledger, adapter)
    execution = MagicMock(); execution.client = client
    execution.entry_submitter.client_oid_for.return_value = "cgc-fcp-rejected"
    execution.execute.return_value = []
    positions = MagicMock(); positions.store.load.return_value = []
    canonical = CanonicalFundingPilot(runtime, execution, positions)
    with pytest.raises(FailClosed, match="did not produce"):
        canonical.process_signal(PilotSignal("reject", 2_000_000_000_000, "DOGEUSDT", "LONG", 100, {}))
    assert ledger.events("ENTRY_TERMINAL")[-1]["payload"]["state"] == "REJECTED"
    assert adapter._owned() == {}


def test_post_execution_uncertainty_retains_ownership_until_safe_closed(tmp_path):
    client, ledger = TransportHarness(), PilotLedger(tmp_path / "pilot.sqlite")
    oid="cgc-fcp-uncertain"
    ledger.append("ENTRY_INTENT", {"symbol":"DOGEUSDT","entry_client_oid":oid}, symbol="DOGEUSDT")
    ledger.append("ENTRY_TERMINAL", {"symbol":"DOGEUSDT","entry_client_oid":oid,
        "state":"HALTED_UNCERTAIN","reason":"persistence_unknown"}, symbol="DOGEUSDT")
    adapter=BitgetPilotExchangePort(client, ledger)
    assert adapter._owned()["DOGEUSDT"]["entry_client_oid"] == oid
    ledger.append("ENTRY_TERMINAL", {"symbol":"DOGEUSDT","entry_client_oid":oid,
        "state":"SAFE_CLOSED"}, symbol="DOGEUSDT")
    assert adapter._owned() == {}


def test_uncertain_lifecycle_zero_proof_reconciler_is_restart_safe(tmp_path):
    client, ledger = TransportHarness(), PilotLedger(tmp_path/"pilot.sqlite")
    oid="cgc-fcp-uncertain"
    ledger.append("ENTRY_INTENT", {"symbol":"DOGEUSDT","entry_client_oid":oid}, symbol="DOGEUSDT")
    ledger.append("ENTRY_TERMINAL", {"symbol":"DOGEUSDT","entry_client_oid":oid,
        "state":"HALTED_UNCERTAIN"}, symbol="DOGEUSDT")
    adapter=BitgetPilotExchangePort(client,ledger)
    canonical=CanonicalFundingPilot(PilotRuntime(PilotConfig(SPEC,ledger.path),ledger,adapter),MagicMock(),MagicMock())
    assert canonical.reconcile_uncertain_lifecycles() == [oid]
    assert adapter._owned() == {}
    assert canonical.reconcile_uncertain_lifecycles() == []


def test_uncertain_live_exposure_is_owned_flattened_and_zero_proved(tmp_path):
    client, ledger = TransportHarness(), PilotLedger(tmp_path/"pilot.sqlite")
    oid="cgc-fcp-uncertain"
    ledger.append("ENTRY_INTENT", {"symbol":"DOGEUSDT","entry_client_oid":oid}, symbol="DOGEUSDT")
    ledger.append("STOP_ACK", {"symbol":"DOGEUSDT","entry_client_oid":oid,
        "exchange_position_id":"p1","stop_order_id":"stop-1"}, symbol="DOGEUSDT")
    ledger.append("ENTRY_TERMINAL", {"symbol":"DOGEUSDT","entry_client_oid":oid,
        "state":"HALTED_UNCERTAIN"}, symbol="DOGEUSDT")
    client.positions=[{"symbol":"DOGEUSDT","positionId":"p1","total":"1","openPriceAvg":"10",
                       "markPrice":"10","unrealizedPL":"0"}]
    client.orders=[{"symbol":"DOGEUSDT","orderId":"work-1","clientOid":"cgc-fcp-work"}]
    client.plans=[{"symbol":"DOGEUSDT","orderId":"stop-1","clientOid":"cgc-fcp-stop"}]
    adapter=BitgetPilotExchangePort(client,ledger,armed_live=True)
    config=replace(PilotConfig(SPEC,ledger.path),orders_enabled=True)
    canonical=CanonicalFundingPilot(PilotRuntime(config,ledger,adapter),MagicMock(),MagicMock())
    assert canonical.reconcile_uncertain_lifecycles() == []
    assert ledger.events("EXIT_PENDING")
    assert adapter._owned()["DOGEUSDT"]["entry_client_oid"] == oid
    client.history=[{"positionId":"p1","closeOrderId":"recovery-close","pnl":"0",
                     "openFee":"0","closeFee":"-.1"}]
    assert canonical.reconcile_uncertain_lifecycles() == [oid]
    assert adapter.truth().pilot_positions == adapter.truth().pilot_working_orders == adapter.truth().pilot_stops == ()
    assert {kind for kind,_ in client.write_calls} == {"cancel_order","close","cancel_stop"}


@pytest.mark.parametrize("artifact",["order","stop"])
def test_uncertain_artifact_only_closes_without_impossible_economics_wait(tmp_path,artifact):
    client,ledger=TransportHarness(),PilotLedger(tmp_path/f"{artifact}.sqlite")
    oid="cgc-fcp-artifact"
    ledger.append("ENTRY_INTENT",{"symbol":"DOGEUSDT","entry_client_oid":oid},symbol="DOGEUSDT")
    ledger.append("ENTRY_TERMINAL",{"symbol":"DOGEUSDT","entry_client_oid":oid,
        "state":"HALTED_UNCERTAIN"},symbol="DOGEUSDT")
    if artifact=="order": client.orders=[{"symbol":"DOGEUSDT","orderId":"o1","clientOid":oid}]
    else:
        ledger.append("STOP_ACK",{"symbol":"DOGEUSDT","entry_client_oid":oid,
            "stop_order_id":"s1"},symbol="DOGEUSDT")
        client.plans=[{"symbol":"DOGEUSDT","orderId":"s1","clientOid":oid}]
    adapter=BitgetPilotExchangePort(client,ledger,armed_live=True)
    config=replace(PilotConfig(SPEC,ledger.path),orders_enabled=True)
    canonical=CanonicalFundingPilot(PilotRuntime(config,ledger,adapter),MagicMock(),MagicMock())
    assert canonical.reconcile_uncertain_lifecycles()==[oid]
    assert ledger.events("EXIT_PENDING")==[]
    assert adapter._owned()=={}


def test_two_partial_closes_have_distinct_exactly_once_identities(tmp_path):
    client, ledger = TransportHarness(), PilotLedger(tmp_path / "pilot.sqlite")
    ledger.append("CANONICAL_OPEN", {"symbol":"DOGEUSDT","entry_client_oid":"cgc-fcp-entry",
        "exchange_position_id":"p1"}, symbol="DOGEUSDT")
    client.history = [
        {"positionId":"p1","closeOrderId":"partial-A","pnl":"1","openFee":"-.1","closeFee":"-.2"},
        {"positionId":"p1","closeOrderId":"partial-B","pnl":"2","openFee":"-.1","closeFee":"-.3"},
    ]
    adapter=BitgetPilotExchangePort(client, ledger)
    adapter.truth(); adapter.truth()
    source_ids={row[0] for row in ledger.db.execute("SELECT source_id FROM economic_sources")}
    assert {"realized_pnl:partial-A","realized_pnl:partial-B","closing_fee:partial-A","closing_fee:partial-B"} <= source_ids
    economics=ledger.economics(adapter.truth())
    assert economics["realized_pnl"] == pytest.approx(3)
    assert economics["fees"] == pytest.approx(.6)


def test_delayed_close_economics_survive_restart_and_finalize_once(tmp_path):
    path=tmp_path/"pilot.sqlite"
    client, ledger = TransportHarness(), PilotLedger(path)
    oid="cgc-fcp-entry"; position_id="position-7"
    ledger.append("CANONICAL_OPEN", {"symbol":"DOGEUSDT","entry_client_oid":oid,
        "exchange_position_id":position_id,"scheduled_exit_at_ms":1}, symbol="DOGEUSDT")
    local=[{"symbol":"DOGEUSDT","status":"OPEN","protection_mode":"STOP_ONLY_TIME_EXIT",
            "exchange_entry_client_oid":oid,"exchange_position_id":position_id}]
    manager=MagicMock(); manager.store.load.return_value=local
    manager.process_stop_only_time_exits.return_value=[{
        "symbol":"DOGEUSDT","status":"POSITION_CLOSED_STOP_CANCELLED","close_order_id":"close-response-id"}]
    execution=MagicMock()
    adapter=BitgetPilotExchangePort(client, ledger)
    runtime=PilotRuntime(PilotConfig(SPEC,path), ledger, adapter)
    canonical=CanonicalFundingPilot(runtime, execution, manager)
    canonical.process_time_exits(now_ms=2)
    assert len(ledger.events("EXIT_PENDING")) == 1
    assert ledger.events("CANONICAL_TIME_EXIT") == []

    # Process restart precedes delayed history. The exchange history identity
    # intentionally differs from the close response identity.
    restarted_ledger=PilotLedger(path)
    client.history=[{"positionId":position_id,"closeOrderId":"history-partial-id",
                     "pnl":"1.25","openFee":"0","closeFee":"-.05"}]
    restarted_manager=MagicMock(); restarted_manager.store.load.return_value=[]
    restarted_manager.process_stop_only_time_exits.return_value=[]
    restarted=CanonicalFundingPilot(
        PilotRuntime(PilotConfig(SPEC,path), restarted_ledger,
                     BitgetPilotExchangePort(client,restarted_ledger)),
        MagicMock(), restarted_manager)
    restarted.process_time_exits(now_ms=3)
    assert len(restarted_ledger.events("CANONICAL_TIME_EXIT")) == 1
    restarted.process_time_exits(now_ms=4)
    assert len(restarted_ledger.events("CANONICAL_TIME_EXIT")) == 1
    assert restarted_ledger.economics(restarted.runtime.exchange.truth())["realized_pnl"] == pytest.approx(1.25)


def test_prior_partial_close_cannot_finalize_delayed_final_exit(tmp_path):
    path=tmp_path/"pilot.sqlite"; client=TransportHarness(); ledger=PilotLedger(path)
    oid="cgc-fcp-entry"; pid="p1"
    ledger.append("CANONICAL_OPEN", {"symbol":"DOGEUSDT","entry_client_oid":oid,
        "exchange_position_id":pid}, symbol="DOGEUSDT")
    ledger.append_economic_once("realized_pnl:partial-A", {"realized_pnl":1,"fees":0,"funding":0,
        "other_costs":0,"exchange_position_id":pid}, symbol="DOGEUSDT")
    checkpoint=max(row["id"] for row in ledger.events("ECONOMICS"))
    ledger.append("EXIT_PENDING", {"symbol":"DOGEUSDT","entry_client_oid":oid,
        "exchange_position_id":pid,"economics_checkpoint_event_id":checkpoint}, symbol="DOGEUSDT")
    manager=MagicMock(); manager.store.load.return_value=[]; manager.process_stop_only_time_exits.return_value=[]
    canonical=CanonicalFundingPilot(PilotRuntime(PilotConfig(SPEC,path),ledger,
        BitgetPilotExchangePort(client,ledger)),MagicMock(),manager)
    canonical.process_time_exits(now_ms=1)
    assert ledger.events("CANONICAL_TIME_EXIT") == []
    client.history=[{"positionId":pid,"closeOrderId":"final-B","pnl":"2","openFee":"0","closeFee":"-.2"}]
    canonical.process_time_exits(now_ms=2)
    assert len(ledger.events("CANONICAL_TIME_EXIT")) == 1


def test_authoritative_nav_drives_dynamic_single_and_gross_caps(tmp_path):
    client, ledger = TransportHarness(), PilotLedger(tmp_path/"pilot.sqlite")
    ledger.append_economic_once("profit:1", {"realized_pnl":2,"fees":.2,"funding":.1,"other_costs":.1})
    adapter=BitgetPilotExchangePort(client,ledger)
    runtime=PilotRuntime(PilotConfig(SPEC,ledger.path),ledger,adapter)
    state=runtime.reconcile()
    assert state["economics"]["nav"] == pytest.approx(29.04)
    signal=PilotSignal("s",1,"DOGEUSDT","LONG",100,{})
    assert runtime.size(signal,state) == pytest.approx(2.904)
    assert state["economics"]["nav"]*.20 == pytest.approx(5.808)
