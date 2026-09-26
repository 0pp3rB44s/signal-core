"""LSRL_V1 MICRO_LIVE adapter against a FAKE exchange (never a real one).

Covers: arming, mandatory central risk authorization (incl. bypass attempts), clock gate,
foreign-position refusal, no-chase limit-only placement, fills/reconciliation, exchange-side
protection, fail-closed handling, restart safety, and full engine -> executor wiring.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from clients.schemas import RiskVerdict
from execution.entry_submitter import EntryOrderSubmitter
from execution.order_intent_store import OrderIntentStore
from execution.state_store import JsonStateStore
from execution.strategy_ownership import StrategyOwnershipRegistry
from lsrl.adapters.central_ledger import CentralCloseRecorder
from lsrl.adapters.live import LiveNotArmed, LSRLLiveExecutor, assert_armed
from lsrl.adapters.riskgate import CentralRiskGate
from lsrl.core.config import DEFAULT_CONFIG as CFG
from lsrl.core.engine import LSRLEngine
from lsrl.core.types import FillEvent, OrderEvent, PlaceEntry, SetStop, SetTargets
from risk.mandatory_authorization import RiskAuthorizationStore
from tests.lsrl_fake_exchange import TEST_AUTHORITY, TEST_PROTECTION, FakeExchange
from tests.lsrl_fixtures import RULES, S, feed_quiet, long_sweep, mirror
from tests.test_lsrl_risk_integration import _isolate, _rm  # noqa: F401  (autouse isolation fixture)

REPO = Path(__file__).resolve().parents[1]


def armed_settings(**over):
    base = dict(forward_paper_only=False, execution_enabled=True, execution_mode="LIVE",
                lsrl_microlive_armed=True, lsrl_symbol="SOLUSDT")
    base.update(over)
    return SimpleNamespace(**base)


class Rig:
    def __init__(self, tmp_path, *, settings=None, rm=None, dataset=None):
        self.tmp = tmp_path
        self.dataset = dataset or tmp_path / "trade_dataset_v2.csv"
        self.ex = FakeExchange(bid=149.65, ask=149.75, rules=RULES)
        self.settings = settings or armed_settings()
        rm = rm or _rm()
        self.gate = CentralRiskGate(rm, rm.settings)
        self.engine = LSRLEngine(CFG, RULES, risk_gate=self.gate)
        self.intent_store = OrderIntentStore(str(tmp_path / "lsrl_intents.json"))
        self.submitter = EntryOrderSubmitter(client=self.ex, intent_store=self.intent_store,
                                             session_id="test", execution_mode="LIVE")
        self.auth = RiskAuthorizationStore(str(tmp_path / "auth.json"))
        self.state = JsonStateStore(str(tmp_path / "lsrl_live_state.json"))
        self.exec = self.build_executor()
        self.records: list = []
        self.intents: list = []

    def build_executor(self) -> LSRLLiveExecutor:
        return LSRLLiveExecutor(
            settings=self.settings, client=self.ex, submitter=self.submitter, auth_store=self.auth, gate=self.gate,
            state_store=self.state, rules=RULES, sleep=lambda s: None,
            ownership=StrategyOwnershipRegistry(self.tmp / "ownership.json"),
            central=CentralCloseRecorder(client=self.ex, dataset_path=str(self.dataset)),
            clock_ms=lambda: self.ex.now_ms, process_down=TEST_PROTECTION, authority=TEST_AUTHORITY)

    def feed(self, ev):
        queue = [ev]
        while queue:
            e = queue.pop(0)
            self.ex.now_ms = max(self.ex.now_ms, e.t_ms)                # the exchange clock follows the market clock
            intents, records = self.engine.on_event(e)
            self.intents.extend(intents)
            self.records.extend(records)
            queue.extend(self.exec.apply(intents, e.t_ms))

    def run(self, events):
        for ev in events:
            self.feed(ev)
        return self

    def poll(self, dt_ms: int = 1_000):
        t = self.engine.now_ms + dt_ms
        self.ex.now_ms = max(self.ex.now_ms, t)
        for e in self.exec.poll(t):
            self.feed(e)

    def entries(self):
        return [o for o in self.ex.orders.values() if o["tradeSide"] == "open"]

    def kinds(self, k):
        return [r for r in self.records if r.kind == k]

    def rejected(self):
        return [r.data.get("reason") for r in self.kinds("setup_rejected")]


@pytest.fixture()
def rig(tmp_path):
    return Rig(tmp_path).run(long_sweep())


# ------------------------------------------------------------------ arming
@pytest.mark.parametrize("field,value,fragment", [
    ("forward_paper_only", True, "FORWARD_PAPER_ONLY"),
    ("execution_enabled", False, "EXECUTION_ENABLED"),
    ("execution_mode", "DRY_RUN", "EXECUTION_MODE"),
    ("lsrl_microlive_armed", False, "LSRL_MICROLIVE_ARMED"),
    ("lsrl_symbol", "BTCUSDT", "LSRL_SYMBOL"),
])
def test_the_executor_cannot_be_constructed_unless_every_arming_switch_is_on(tmp_path, field, value, fragment):
    with pytest.raises(LiveNotArmed, match=fragment):
        Rig(tmp_path, settings=armed_settings(**{field: value}))


def test_an_unarmed_default_settings_object_is_refused():
    with pytest.raises(LiveNotArmed):
        assert_armed(SimpleNamespace())


def test_disabling_execution_at_runtime_stops_every_further_action(rig):
    calls = len(rig.ex.calls)
    rig.settings.execution_enabled = False
    with pytest.raises(LiveNotArmed):
        rig.exec.apply([PlaceEntry("x", "t", 1, rig.engine.setup.direction, 149.5, 0.1, 1)], 1)
    assert len(rig.ex.calls) == calls


# ------------------------------------------------------------------ passive, authorised placement
def test_the_ladder_is_placed_as_five_authorised_post_only_limit_entries(rig):
    assert rig.engine.setup is not None and rig.engine.setup.state == "LADDER_PENDING"
    placed = rig.ex.by_kind("place_entry")
    assert len(placed) == 5 and all(c[5] is True for c in placed)                 # post_only everywhere
    assert [c[4] for c in placed] == [149.60, 149.50, 149.40, 149.30, 149.20]
    assert len({c[1] for c in placed}) == 5                                       # distinct deterministic clientOids
    assert len(rig.ex.leverage_calls) == 1 and rig.ex.leverage_calls[0][2] == "long"
    assert all(o["state"] == "live" and o["tradeSide"] == "open" for o in rig.entries())
    assert all(r.status == "OPEN" for r in rig.engine.setup.rungs.values())


def test_every_entry_carries_a_recorded_and_required_central_authorization(rig):
    records = rig.auth._load()
    assert len(records) == 5 and all(r["allowed"] for r in records.values())


def test_entries_go_through_the_audited_submitter_with_a_write_ahead_intent(rig):
    intents = rig.intent_store.all()
    assert len(intents) == 5 and {i["state"] for i in intents} == {"SUBMITTED"}
    assert {i["strategy"] for i in intents} == {f"lsrl_v1_e{n}" for n in range(1, 6)}


def test_the_ladder_never_contains_a_market_or_non_post_only_entry(rig):
    assert not hasattr(rig.ex, "place_futures_market_order")
    assert all(o["post_only"] for o in rig.entries())


def test_lsrl_uses_its_own_intent_store_not_the_execution_services(rig):
    assert Path(rig.intent_store.store.path).name == "lsrl_intents.json"


# ------------------------------------------------------------------ bypass attempts
def test_no_evaluated_central_verdict_means_no_order_can_be_sent(tmp_path):
    rig = Rig(tmp_path)
    original = rig.gate.check

    def evaluate_then_forget(proposal):                            # the executor never sees a verdict
        decision = original(proposal)
        rig.gate.verdicts.clear()
        return decision
    rig.gate.check = evaluate_then_forget
    rig.run(long_sweep())
    assert rig.ex.by_kind("place_entry") == [] and rig.ex.orders == {}
    assert rig.auth._load() == {}
    assert any("RISK_AUTHORIZATION_REFUSED" in (r or "") for r in rig.rejected())


def test_a_denied_central_verdict_cannot_be_turned_into_an_order(tmp_path):
    rig = Rig(tmp_path)
    original = rig.gate.check

    def deny_after_the_fact(proposal):
        decision = original(proposal)
        verdict, cand = rig.gate.verdicts[proposal.setup_id]
        rig.gate.verdicts[proposal.setup_id] = (RiskVerdict(False, "BLOCKED", ["tampered"], 0.5, 5.0, 2), cand)
        return decision
    rig.gate.check = deny_after_the_fact
    rig.engine.risk_gate = rig.gate
    rig.run(long_sweep())
    assert rig.ex.orders == {}, "a denied verdict must never reach the exchange"
    assert any("RISK_AUTHORIZATION_REFUSED" in (r or "") for r in rig.rejected())


def test_a_corrupt_authorization_store_fails_closed(tmp_path):
    rig = Rig(tmp_path)
    Path(rig.auth.path).write_text("{ not json")
    rig.run(long_sweep())
    assert rig.ex.orders == {} and rig.exec.halt_reason


def test_the_executor_module_never_calls_the_raw_entry_transport_directly():
    source = (REPO / "lsrl" / "adapters" / "live.py").read_text()
    tree = ast.parse(source)
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "place_futures_limit_order" not in called and "place_futures_market_order" not in called
    assert "place_futures_ioc_order" not in called and "close_futures_position" not in called
    assert "submit_persistent_maker_entry" in {n.func.id for n in ast.walk(tree)
                                               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def test_every_exposure_adding_call_is_preceded_by_the_authorization_gate():
    source = (REPO / "lsrl" / "adapters" / "live.py").read_text()
    body = source.split("def _place_entry", 1)[1].split("def _clock_gate", 1)[0]
    assert body.index("self._authorize(plan)") < body.index("submit_persistent_maker_entry(")


# ------------------------------------------------------------------ clock gate / foreign position
def test_a_stale_exchange_clock_rejects_every_entry_without_creating_a_blocking_intent(tmp_path):
    rig = Rig(tmp_path)
    rig.ex.clock_ok = False
    rig.run(long_sweep())
    assert rig.ex.orders == {}
    assert rig.engine.setup is None
    assert any((r or "").startswith("ENTRY_ORDERS_REJECTED:CLOCK_GATE") for r in rig.rejected())
    assert rig.intent_store.blocking() == [] and rig.intent_store.all() == []


def test_a_foreign_position_on_the_symbol_prevents_the_setup(tmp_path):
    rig = Rig(tmp_path)
    rig.ex.position["long"] = 2.0
    rig.run(long_sweep())
    assert rig.ex.orders == {} and rig.engine.setup is None
    assert any("FOREIGN_POSITION_PRESENT" in (r or "") for r in rig.rejected())
    assert rig.exec.halt_reason and "FOREIGN_POSITION_PRESENT" in rig.exec.halt_reason


def test_unknown_position_state_fails_closed(tmp_path):
    rig = Rig(tmp_path)
    rig.ex.get_all_positions = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("positions endpoint down"))
    rig.run(long_sweep())
    assert rig.ex.orders == {} and rig.engine.setup is None and rig.exec.halt_reason


def test_a_post_only_rejection_drops_that_rung_only_and_does_not_halt_the_executor(tmp_path):
    rig = Rig(tmp_path)
    rig.ex.ask = 149.55                                            # E1 (149.60) would now cross
    rig.run(long_sweep())
    assert rig.exec.halt_reason is None
    placed = [c[4] for c in rig.ex.by_kind("place_entry")]
    assert 149.60 not in placed and placed == [149.50, 149.40, 149.30, 149.20]
    assert rig.engine.setup.rungs[1].status == "REJECTED"


# ------------------------------------------------------------------ fills, protection, targets
def q_of(rig) -> float:
    return rig.engine.setup.sizing["rung_qty"]


def test_a_partial_fill_is_reconciled_and_the_position_is_protected_exchange_side(rig):
    q = q_of(rig)
    oid = rig.ex.by_kind("place_entry")[0][1]
    rig.ex.fill(oid, qty=0.6 * q, fee=0.03)
    rig.poll()
    led = rig.engine.setup.ledger
    assert led.entry_qty == pytest.approx(0.6 * q) and rig.engine.setup.state == "IN_POSITION"
    stop = rig.ex.by_kind("stop")[-1]
    assert stop[1] == pytest.approx(rig.engine.setup.stop_price) and stop[2] == pytest.approx(0.6 * q)
    rig.ex.fill(oid, qty=0.4 * q, fee=0.04)                        # the rest of E1
    rig.poll()
    assert rig.engine.setup.ledger.entry_qty == pytest.approx(q)
    assert rig.ex.stop_plan["size"] == pytest.approx(q)            # stop re-synced to the larger position


def test_reconciliation_is_idempotent_it_never_double_reports_a_fill(rig):
    oid = rig.ex.by_kind("place_entry")[0][1]
    rig.ex.fill(oid)
    rig.poll()
    rig.poll()
    rig.poll()
    assert rig.engine.setup.ledger.entry_qty == pytest.approx(q_of(rig))


def test_targets_are_reduce_only_post_only_limits_sized_30_40_30_and_rescaled_on_new_fills(rig):
    oids = [c[1] for c in rig.ex.by_kind("place_entry")]
    rig.ex.fill(oids[0])
    rig.poll()
    tps = rig.ex.by_kind("place_tp")
    assert len(tps) == 3 and all(c[5] is True for c in tps)
    assert sorted(c[4] for c in tps) == [150.30, 150.70, 151.10]
    q = q_of(rig)
    assert sum(c[3] for c in tps) == pytest.approx(q)
    assert all(o["reduceOnly"] == "YES" for o in rig.ex.orders.values() if o["tradeSide"] == "close")
    rig.ex.fill(oids[1])
    rig.poll()
    live_tp = [o for o in rig.ex.orders.values() if o["tradeSide"] == "close" and o["state"] == "live"]
    assert len(live_tp) == 3 and sum(o["size"] for o in live_tp) == pytest.approx(2 * q)
    assert len(rig.ex.by_kind("cancel")) >= 3                      # the old sizing was replaced, not stacked


def test_full_engine_to_exchange_lifecycle_tp1_break_even_tp2_tp1_stop_tp3_flat(rig):
    for c in rig.ex.by_kind("place_entry"):
        rig.ex.fill(c[1], fee=c[3] * c[4] * 0.0002)
    rig.poll()
    assert rig.engine.setup.ledger.entry_qty == pytest.approx(5 * q_of(rig))
    tp = {c[4]: c[1] for c in rig.ex.by_kind("place_tp")[-3:]}
    rig.ex.fill(tp[150.30], fee=0.1)
    rig.poll()
    be = rig.ex.by_kind("stop")[-1]
    assert be[1] > 149.40 and be[3] == "TP1_COST_ADJUSTED_BE"
    rig.ex.fill(tp[150.70], fee=0.1)
    rig.poll()
    assert rig.ex.by_kind("stop")[-1][1] == 150.30
    rig.ex.fill(tp[151.10], fee=0.1)
    rig.poll()
    assert rig.engine.setup is None and rig.kinds("setup_closed")[-1].data["close_reason"] == "TP3_FLAT"
    assert rig.ex.position["long"] == pytest.approx(0.0) and rig.ex.by_kind("cancel_stop")


def test_a_stop_out_is_taken_from_the_exchange_order_history_with_its_real_price_and_fee(rig):
    for c in rig.ex.by_kind("place_entry"):
        rig.ex.fill(c[1])
    rig.poll()
    stop_price = rig.engine.setup.stop_price
    order_id = rig.ex.trigger_stop(slippage=0.05)                  # market stop fills 0.05 worse than the trigger
    rig.poll()
    closed = rig.kinds("setup_closed")[-1].data
    assert closed["close_reason"] == "STOP_LOSS" and rig.engine.setup is None
    row = rig.ex.history[-1]
    assert row["orderId"] == order_id and row["orderSource"] == "loss_market"
    qty = closed["actual"]["exit_qty"]
    assert qty == pytest.approx(float(row["baseVolume"]))
    assert closed["actual"]["stop_slippage_usdt"] == pytest.approx(0.05 * qty)       # the REAL fill price, not the stop price
    assert closed["actual"]["exit_fees_usdt"] == pytest.approx(abs(float(row["fee"])))
    assert closed["realized_r"] < -1.0                                                # slippage + taker fee are real losses
    assert stop_price - float(row["priceAvg"]) == pytest.approx(0.05)


def test_entry_expiry_cancels_unfilled_rungs_on_the_exchange(rig):
    oids = [c[1] for c in rig.ex.by_kind("place_entry")]
    rig.ex.fill(oids[0])
    rig.poll()
    feed_quiet_rig(rig, rig.engine.setup.entry_expires_ms + 20_000)
    cancelled = {c[1] for c in rig.ex.by_kind("cancel")}
    assert set(oids[1:]) <= cancelled
    assert all(rig.ex.orders[o]["state"] == "canceled" for o in oids[1:])


def feed_quiet_rig(rig: Rig, until_ms: int) -> None:
    from tests.lsrl_fixtures import quiet_book
    t = rig.engine.now_ms + 5_000
    while t <= until_ms:
        rig.feed(quiet_book(t, 149.70))
        rig.poll(0)
        t += 10_000


# ------------------------------------------------------------------ fail-closed behaviour
def test_a_position_whose_stop_cannot_be_placed_is_flattened_immediately(rig):
    oid = rig.ex.by_kind("place_entry")[0][1]
    rig.ex.fail["move_futures_stop_loss"] = RuntimeError("exchange refused the stop")
    rig.ex.fill(oid)
    rig.poll()
    assert rig.ex.by_kind("emergency_close") and rig.ex.position["long"] == 0.0
    assert rig.exec.halt_reason and "STOP_PLACEMENT_FAILED" in rig.exec.halt_reason
    assert rig.engine.setup is None
    assert rig.kinds("setup_closed")[-1].data["close_reason"] == "CLOSED_EXTERNALLY"


def test_unknown_order_state_halts_new_entries_but_never_places_anything(rig):
    placed = len(rig.ex.by_kind("place_entry"))
    rig.ex.lookup_unknown = True
    rig.poll()
    assert rig.exec.halt_reason and "ORDER_STATE_UNKNOWN" in rig.exec.halt_reason
    assert len(rig.ex.by_kind("place_entry")) == placed
    r = rig.exec.apply([PlaceEntry(rig.engine.setup.setup_id, "late", 9, rig.engine.setup.direction, 149.1, 0.1, 1)], 1)
    assert r and r[0].status == "REJECTED" and "EXECUTOR_HALTED" in r[0].reason


def test_an_order_that_vanished_from_the_exchange_halts_and_is_reported_cancelled(rig):
    oid = rig.ex.by_kind("place_entry")[0][1]
    del rig.ex.orders[oid]
    rig.poll()
    assert rig.exec.halt_reason and "ORDER_ABSENT" in rig.exec.halt_reason
    assert rig.engine.setup.rungs[1].status == "CANCELLED"


def test_a_late_fill_on_a_replaced_target_is_still_reported(rig):
    oids = [c[1] for c in rig.ex.by_kind("place_entry")]
    rig.ex.fill(oids[0])
    rig.poll()
    first_tp = rig.ex.by_kind("place_tp")[0][1]
    rig.ex.fill(oids[1])                                           # triggers a resize (cancel + replace)
    rig.ex.orders[first_tp]["baseVolume"] = 0.0                    # ...but the old TP filled before the cancel
    rig.ex.fill(first_tp, qty=rig.ex.orders[first_tp]["size"])
    rig.poll()
    assert any(e["kind"].endswith("_FILL") and e["kind"] != "ENTRY_FILL" for e in rig.engine.setup.events)


# ------------------------------------------------------------------ restart safety
def test_executor_state_survives_a_restart_without_duplicating_orders_or_fills(tmp_path):
    rig = Rig(tmp_path).run(long_sweep())
    oid = rig.ex.by_kind("place_entry")[0][1]
    rig.ex.fill(oid)
    rig.poll()
    n_entries = len(rig.ex.by_kind("place_entry"))
    restarted = rig.build_executor()                               # new process, same state file
    assert restarted.orders.keys() == rig.exec.orders.keys()
    assert restarted.poll(rig.engine.now_ms + 1_000) == []         # nothing new happened: no duplicate fills
    dup = [PlaceEntry(rig.engine.setup.setup_id, t, n, rig.engine.setup.direction, r.price, r.qty,
                      rig.engine.setup.entry_expires_ms) for n, (t, r) in
           enumerate(((r.tag, r) for r in rig.engine.setup.rungs.values()), start=1)]
    assert restarted.apply(dup, rig.engine.now_ms) == []           # duplicate delivery is a no-op
    assert len(rig.ex.by_kind("place_entry")) == n_entries


def test_a_halt_persists_across_a_restart_and_needs_an_explicit_resume(tmp_path):
    rig = Rig(tmp_path).run(long_sweep())
    rig.ex.lookup_unknown = True
    rig.poll()
    restarted = rig.build_executor()
    assert restarted.halt_reason
    restarted.resume()
    assert restarted.halt_reason is None


def test_engine_and_executor_agree_after_a_restart_mid_position(tmp_path):
    rig = Rig(tmp_path).run(long_sweep())
    rig.ex.fill(rig.ex.by_kind("place_entry")[0][1])
    rig.poll()
    snapshot = rig.engine.snapshot()
    rig.engine = LSRLEngine(CFG, RULES, risk_gate=rig.gate)
    rig.engine.restore(snapshot)
    rig.exec = rig.build_executor()
    feed_quiet_rig(rig, rig.engine.now_ms + 30_000)
    q = q_of(rig)
    assert rig.engine.setup.state == "IN_POSITION" and rig.engine.setup.ledger.entry_qty == pytest.approx(q)
    assert rig.ex.stop_plan["size"] == pytest.approx(q)


# ------------------------------------------------------------------ SHORT symmetry through the live path
def test_short_setup_places_sell_limits_and_a_short_side_stop(tmp_path):
    rig = Rig(tmp_path)
    rig.ex.bid, rig.ex.ask = 150.25, 150.35
    rig.run(mirror(long_sweep()))
    placed = rig.ex.by_kind("place_entry")
    assert len(placed) == 5 and {c[2] for c in placed} == {"SHORT"}
    assert [c[4] for c in placed] == [150.40, 150.50, 150.60, 150.70, 150.80]
    assert rig.ex.leverage_calls[0][2] == "short"
    rig.ex.fill(placed[0][1])
    rig.poll()
    assert rig.ex.stop_plan["hold"] == "short" and rig.ex.stop_plan["price"] > 150.80
