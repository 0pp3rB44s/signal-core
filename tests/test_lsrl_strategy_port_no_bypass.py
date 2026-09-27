"""NO BYPASS PATH: any strategy that speaks the intent vocabulary reaches the exchange only through RiskManager, order ownership, exchange reconciliation, the
clock gate, the capacity gate, loss limits, the kill switches and the owner ceremony.

Two kinds of proof. STRUCTURAL: the source tree is scanned for any other route to an order, a runtime, a trade authority or a promotion. BEHAVIOURAL: a toy
strategy (nothing like LSRL) is run through the real driver-side executor on the fake exchange while each gate is closed in turn; the exchange must hold nothing.
"""
from __future__ import annotations

import ast
import dataclasses as d
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from lsrl.adapters.authority import PROTECT_ONLY, TRADE, AuthorityRefused, LiveAuthority, admit_strategy
from lsrl.adapters.live import LiveNotArmed
from lsrl.adapters.strategy_port import INTENT_TYPES, IntentRefused, LsrlStrategyPort, StrategyPort, check_intents
from lsrl.core.config import DEFAULT_CONFIG as CFG
from lsrl.core.gates import RiskProposal
from lsrl.core.types import CancelOrder, Direction, PlaceEntry, SetStop, SetTargets, TargetOrder
from lsrl.tools import owner_ceremony as c
from tests.test_lsrl_capital_authority import Gate, consumed
from tests.test_lsrl_live_executor import Rig
from tests.test_lsrl_risk_integration import _isolate  # noqa: F401  (autouse isolation fixture)

REPO = Path(__file__).resolve().parents[1]

#: Every method that can create, change or remove an order/plan on the exchange (the shared clients' mutating surface).
MUTATING = {
    "place_futures_limit_order", "place_futures_limit_close_order", "place_futures_market_order", "place_futures_ioc_order", "place_futures_stop_loss",
    "place_futures_take_profit", "cancel_futures_order", "cancel_futures_plan_order", "cancel_all_futures_tpsl_orders", "move_futures_stop_loss",
    "move_position_level_stop_only", "move_plan_level_stop_place_first", "close_futures_position", "close_futures_position_full", "set_leverage",
    "submit_persistent_maker_entry", "place_pos_tpsl", "place_tpsl_order", "flash_close_position", "change_hold_mode", "set_position_mode",
}
PRODUCTION_DIRS = ("lsrl", "app", "scripts")


def production_files(*dirs):
    for base in dirs or PRODUCTION_DIRS:
        for path in sorted((REPO / base).rglob("*.py")):
            yield path.relative_to(REPO).as_posix(), ast.parse(path.read_text())


def calls_named(tree, names):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
            if name in names:
                yield node, name


# ==================================================================================== structural: there is no other route
def test_only_the_live_executor_calls_an_order_mutating_client_method_inside_lsrl():
    offenders = {rel: sorted({n for _c, n in calls_named(tree, MUTATING)}) for rel, tree in production_files("lsrl")}
    assert {rel for rel, names in offenders.items() if names} == {"lsrl/adapters/live.py"}, {k: v for k, v in offenders.items() if v}


def test_the_strategy_core_imports_nothing_that_can_reach_an_exchange_a_process_or_the_filesystem_outside_its_own_package():
    forbidden = ("clients", "execution", "risk", "app", "requests", "websocket", "websockets", "aiohttp", "httpx", "socket", "subprocess", "urllib",
                 "lsrl.adapters", "lsrl.tools", "lsrl.runtime", "lsrl.ws", "lsrl.io", "http", "ssl", "asyncio")
    for rel, tree in production_files("lsrl"):
        if not rel.startswith("lsrl/core/"):
            continue
        for node in ast.walk(tree):
            mods = ([a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for m in mods:
                assert not any(m == f or m.startswith(f + ".") for f in forbidden), (rel, m)


def test_the_executor_is_constructed_in_exactly_one_production_place():
    sites = {rel for rel, tree in production_files() if list(calls_named(tree, {"LSRLLiveExecutor"}))}
    assert sites == {"lsrl/runtime.py"}, sites


def test_the_runtime_is_built_for_micro_live_only_by_the_launcher_and_only_the_launcher_passes_an_authority():
    callers, with_authority = set(), set()
    for rel, tree in production_files():
        for node, _n in calls_named(tree, {"build_runtime"}):
            callers.add(rel)
            if any(k.arg == "authority" for k in node.keywords):
                with_authority.add(rel)
    assert callers <= {"app/runner.py", "lsrl/tools/live_launcher.py"} | {r for r in callers if r.startswith("scripts/")}, callers
    assert with_authority == {"lsrl/tools/live_launcher.py"}, with_authority


def test_a_trade_authority_and_a_promotion_exist_in_exactly_one_production_place_and_the_test_only_authority_in_none():
    trade, promote, test_only = set(), set(), set()
    for rel, tree in production_files():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "trade" and getattr(node.func.value, "id", "") == "LiveAuthority":
                    trade.add(rel)
                if node.func.attr == "promote" and getattr(node.func.value, "attr", "") == "runtime":
                    promote.add(rel)
            if isinstance(node, ast.Name) and node.id == "_authority_for_tests" or isinstance(node, ast.keyword) and node.arg == "unbounded" \
                    and isinstance(node.value, ast.Constant) and node.value.value is True:
                test_only.add(rel)
    assert trade == {"lsrl/tools/live_launcher.py"} and promote == {"lsrl/tools/live_launcher.py"}, (trade, promote)
    assert test_only <= {"lsrl/adapters/authority.py"}, test_only


def test_no_production_module_other_than_the_read_only_tools_and_the_launcher_builds_an_authenticated_client_inside_lsrl():
    allowed = {"lsrl/tools/live_launcher.py", "lsrl/tools/verify_exchange.py", "lsrl/tools/exposure_audit.py"}
    users = {rel for rel, tree in production_files("lsrl") if any(
        (isinstance(n, ast.ImportFrom) and (n.module or "").startswith("clients.bitget_rest")) for n in ast.walk(tree))}
    assert users <= allowed, users


def test_the_demo_probe_can_only_reach_its_allow_list_and_never_uses_the_shared_order_clients():
    tree = dict(production_files("lsrl"))["lsrl/tools/probe_preset_stop.py"]
    assert not list(calls_named(tree, MUTATING - {"close_side_for_hold_side"}))
    imports = {(n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not any(m.startswith(("clients.bitget_rest", "execution", "risk")) for m in imports)


def test_the_legacy_engine_runner_cannot_start_lsrl_micro_live_because_it_never_passes_an_authority(tmp_path, monkeypatch):
    """`app/runner.py` calls build_runtime without `authority`; for MICRO_LIVE that is a hard refusal (fail-loud), so launchd respawning the legacy engine can
    never bring LSRL up trading - and cannot double-run the launcher's runtime."""
    from lsrl.runtime import build_runtime
    from tests.test_lsrl_feed_and_runtime import MetaClient, settings, spec
    from tests.test_lsrl_process_down_protection import attestation
    import tests.test_lsrl_process_down_protection as pdp
    good = pdp.SHA
    monkeypatch.setattr("lsrl.runtime.resolve_git_sha", lambda *a, **k: good)
    path, digest = attestation(tmp_path)
    st = settings(lsrl_mode="MICRO_LIVE", lsrl_data_dir=str(tmp_path / "d"), forward_paper_only=False, execution_enabled=True, execution_mode="LIVE",
                  lsrl_microlive_armed=True, lsrl_process_down_attestation_path=str(path), lsrl_process_down_attestation_sha256=digest,
                  lsrl_accept_demo_process_down_evidence=True)
    monkeypatch.setattr("lsrl.adapters.process_down.time", SimpleNamespace(time=lambda: pdp.NOW))
    for authority in (None, "TRADE", object(), SimpleNamespace(mode="TRADE"), True):
        with pytest.raises(LiveNotArmed, match="CEREMONY_NOT_CONSUMED"):
            build_runtime(st, client=MetaClient(spec()), execution_service=object(), risk_manager=None, authority=authority)


# ==================================================================================== the intent vocabulary
def good_entry(**over):
    base = dict(setup_id="toy1", tag="toy1-e1", rung=1, direction=Direction.LONG, price=149.5, qty=0.2, expires_at_ms=1)
    base.update(over)
    return PlaceEntry(**base)


def test_the_vocabulary_is_exactly_four_intents():
    assert set(INTENT_TYPES) == {PlaceEntry, CancelOrder, SetStop, SetTargets}
    check_intents([good_entry(), CancelOrder("toy1", "toy1-e1", "x"), SetStop("toy1", Direction.LONG, 149.0, 0.2, "r"),
                   SetStop("toy1", Direction.LONG, 149.0, 0.0, "flat"), SetTargets("toy1", Direction.LONG, (TargetOrder("t", "TP1", 151.0, 0.1),), "r")])


class MarketOrder:                                         # what a strategy might try to smuggle in
    TYPE = "market_order"
    setup_id, qty = "toy1", 1.0


class EvilEntry(PlaceEntry):                               # a subclass is not the vocabulary
    pass


@pytest.mark.parametrize("bad", [
    MarketOrder(), {"type": "place_entry", "price": 1, "qty": 1}, ("place", 1), object(), None, "buy",
    good_entry(qty=float("nan")), good_entry(qty=-1.0), good_entry(qty=0.0), good_entry(price=0.0), good_entry(price=float("inf")),
    good_entry(qty=True), good_entry(tag=""), good_entry(setup_id=""), SetStop("toy1", Direction.LONG, 0.0, 0.2, "r"),
    SetStop("toy1", Direction.LONG, 149.0, -0.2, "r"), SetStop("toy1", Direction.LONG, 149.0, float("nan"), "r"),
    SetTargets("toy1", Direction.LONG, (TargetOrder("t", "TP1", 151.0, 0.0),), "r"),
])
def test_anything_outside_the_vocabulary_or_malformed_is_refused(bad):
    with pytest.raises(IntentRefused):
        check_intents([good_entry(), bad])


def test_a_subclass_of_an_intent_is_refused():
    evil = EvilEntry(setup_id="toy1", tag="t", rung=1, direction=Direction.LONG, price=149.5, qty=0.2, expires_at_ms=1)
    with pytest.raises(IntentRefused, match="UNKNOWN_INTENT_TYPE"):
        check_intents([evil])


# ==================================================================================== behavioural: a toy strategy against every gate
class Toy:
    """Nothing like LSRL: a pure function from a tick to a five-rung ladder. `consult_gate=False` is the strategy that skips its RiskGate."""
    strategy_id, spec_digest, symbol = "toy_v1", "f" * 64, "SOLUSDT"

    def __init__(self, rig, *, consult_gate=True, qty=0.2, nav=150.0):
        self.rig, self.consult_gate, self.qty, self.nav = rig, consult_gate, qty, nav

    def halt(self, reason):
        self.halted = reason

    def ladder(self, setup_id="toy1"):
        t = self.rig.ex.now_ms
        prices = (149.6, 149.5, 149.4, 149.3, 149.2)
        if self.consult_gate:
            self.rig.gate.check(RiskProposal(setup_id=setup_id, symbol="SOLUSDT", direction=Direction.LONG, t_ms=t, nav_usdt=self.nav, entry_prices=prices,
                                             stop=149.13, total_qty=self.qty * 5, planned_notional_usdt=sum(p * self.qty for p in prices),
                                             planned_loss_usdt=sum(self.qty * (p - 149.13) for p in prices), strategy_risk_fraction=0.005, spread_bps=3.0))
        return [PlaceEntry(setup_id, f"{setup_id}-e{i + 1}", i + 1, Direction.LONG, p, self.qty, t + 900_000) for i, p in enumerate(prices)]


def nothing_on_the_exchange(rig):
    assert rig.ex.orders == {} and rig.ex.plans == [] and rig.ex.position == {"long": 0.0, "short": 0.0}


def run_toy(rig, toy=None, **kw):
    toy = toy or Toy(rig, **kw)
    events = rig.exec.apply(toy.ladder(), rig.ex.now_ms)
    return events


def test_the_toy_strategy_is_a_strategy_port_and_the_good_path_places_only_post_only_limit_entries(tmp_path):
    rig = Rig(tmp_path)
    assert isinstance(Toy(rig), StrategyPort)
    run_toy(rig)
    assert len(rig.ex.orders) == 5 and rig.exec.halt_reason is None
    assert all(o["tradeSide"] == "open" and o["post_only"] for o in rig.ex.orders.values())
    assert not hasattr(rig.ex, "place_futures_market_order")


def test_a_strategy_that_skips_its_risk_gate_has_no_verdict_and_therefore_no_order(tmp_path):
    rig = Rig(tmp_path)
    run_toy(rig, consult_gate=False)
    nothing_on_the_exchange(rig)
    assert rig.exec.halt_reason and "RISK_AUTHORIZATION_REFUSED" in rig.exec.halt_reason


def test_a_denied_verdict_the_kill_switch_answer_places_nothing(tmp_path):
    from clients.schemas import RiskVerdict
    rig = Rig(tmp_path)
    toy = Toy(rig)
    ladder = toy.ladder()
    verdict, cand = rig.gate.verdicts["toy1"]
    rig.gate.verdicts["toy1"] = (RiskVerdict(False, "BLOCKED", ["kill-switch: daily defensive mode active"], 0.5, 5.0, 2), cand)
    rig.exec.apply(ladder, rig.ex.now_ms)
    nothing_on_the_exchange(rig)


@pytest.mark.parametrize("bad", [MarketOrder(), {"type": "market"}, object(), good_entry(qty=float("nan"))])
def test_an_unknown_or_malformed_intent_in_a_batch_stops_the_whole_batch_and_halts_new_entries(tmp_path, bad):
    rig = Rig(tmp_path)
    ladder = Toy(rig).ladder()
    rig.exec.apply([*ladder, bad], rig.ex.now_ms)
    nothing_on_the_exchange(rig)                                                # the GOOD rungs in the same batch were not executed either
    assert rig.exec.halt_reason.startswith("INTENT_REFUSED") and any(i["kind"] == "INTENT_REFUSED" for i in rig.exec.incidents)


def test_the_clock_gate_closed_places_nothing(tmp_path):
    rig = Rig(tmp_path)
    rig.ex.clock_ok = False
    run_toy(rig)
    nothing_on_the_exchange(rig)
    assert "CLOCK_GATE" in (rig.exec.halt_reason or "")


def test_a_position_that_is_not_ours_blocks_the_ladder_and_is_never_touched(tmp_path):
    rig = Rig(tmp_path)
    rig.ex.position["long"] = 1.5
    run_toy(rig)
    assert rig.ex.orders == {} and rig.ex.position["long"] == 1.5 and rig.exec.halt_reason == "FOREIGN_POSITION_PRESENT"


def test_a_resting_order_that_is_not_ours_blocks_the_ladder_and_is_left_alone(tmp_path):
    rig = Rig(tmp_path)
    rig.ex.foreign_orders = [{"clientOid": "legacy-limit", "orderId": "L1", "status": "live", "state": "live", "size": "1", "price": "140", "side": "buy",
                              "tradeSide": "open"}]
    run_toy(rig)
    assert rig.ex.orders == {} and any(i["kind"] == "FOREIGN_ORDERS_PRESENT" for i in rig.exec.incidents)


def test_an_unrecorded_close_a_loss_the_central_kill_switches_cannot_see_blocks_new_entries(tmp_path):
    rig = Rig(tmp_path)
    rig.exec.pending_central["setup0"] = {"setup_id": "setup0"}
    run_toy(rig)
    nothing_on_the_exchange(rig)


def test_an_unreadable_exchange_at_startup_places_nothing_and_halts(tmp_path):
    rig = Rig(tmp_path)
    rig.ex.fail["get_pending_orders"] = TimeoutError("read timed out")
    run_toy(rig)
    nothing_on_the_exchange(rig)
    assert "STARTUP_EXCHANGE_UNREADABLE" in (rig.exec.halt_reason or "")


def test_disabling_execution_stops_the_toy_too(tmp_path):
    rig = Rig(tmp_path)
    toy = Toy(rig)
    ladder = toy.ladder()
    rig.settings.execution_enabled = False
    with pytest.raises(LiveNotArmed):
        rig.exec.apply(ladder, rig.ex.now_ms)
    nothing_on_the_exchange(rig)


def test_protect_only_the_restart_mode_places_nothing_for_any_strategy(tmp_path):
    from tests.test_lsrl_exposure_recovery import LifeRig
    rig = type("R", (LifeRig,), {"authority": LiveAuthority.protect_only()})(tmp_path)
    run_toy(rig)
    nothing_on_the_exchange(rig)
    assert any(i["kind"] == "ENTRIES_BLOCKED" for i in rig.exec.incidents)


def test_a_dead_capacity_monitor_places_nothing(tmp_path):
    from tests.test_lsrl_exposure_recovery import LifeRig
    rig = type("R", (LifeRig,), {"authority": LiveAuthority.trade(consumed(), capacity_gate=Gate("MONITOR_STALE:900s"))})(tmp_path)
    run_toy(rig)
    nothing_on_the_exchange(rig)


def test_a_toy_ladder_bigger_than_the_owners_capital_band_allows_is_vetoed_whole(tmp_path):
    from tests.test_lsrl_exposure_recovery import LifeRig
    rig = type("R", (LifeRig,), {"authority": LiveAuthority.trade(consumed(150.0, 750.0), capacity_gate=Gate())})(tmp_path)
    run_toy(rig, qty=5.0, nav=150.0)                        # 25 SOL ~ 3,700 USDT of a 150 USDT pilot; risk far above 0.5 %
    nothing_on_the_exchange(rig)
    assert any(i["kind"] == "CAPITAL_GUARD_VETO" for i in rig.exec.incidents) and rig.exec.halt_reason is None


def test_the_same_ladder_inside_the_band_is_placed_by_the_same_executor_so_the_gates_are_not_simply_shut(tmp_path):
    from tests.test_lsrl_exposure_recovery import LifeRig
    rig = type("R", (LifeRig,), {"authority": LiveAuthority.trade(consumed(150.0, 750.0), capacity_gate=Gate())})(tmp_path)
    run_toy(rig, qty=0.2, nav=150.0)
    assert len(rig.ex.orders) == 5 and rig.exec.halt_reason is None


# ==================================================================================== the strategy behind the port is the approved one
def test_the_strategy_must_be_the_one_the_owner_approved_id_spec_and_symbol():
    good = LiveAuthority.trade(consumed(), capacity_gate=Gate())
    lsrl = LsrlStrategyPort(SimpleNamespace(cfg=CFG, halt=lambda r: None))
    assert lsrl.strategy_id == "lsrl_v1" and lsrl.spec_digest == CFG.digest() and isinstance(lsrl, StrategyPort)
    admit_strategy(lsrl, good)
    admit_strategy(lsrl, LiveAuthority.protect_only())                               # nothing to admit: it cannot trade
    toy = SimpleNamespace(strategy_id="toy_v1", spec_digest="f" * 64, symbol="SOLUSDT")
    with pytest.raises(AuthorityRefused, match="STRATEGY_NOT_APPROVED:strategy"):
        admit_strategy(toy, good)                                                     # a different strategy id is not approved by LSRL's ceremony
    with pytest.raises(AuthorityRefused, match="STRATEGY_NOT_APPROVED:spec_digest"):
        admit_strategy(SimpleNamespace(strategy_id="lsrl_v1", spec_digest="e" * 64, symbol="SOLUSDT"), good)
    with pytest.raises(AuthorityRefused, match="STRATEGY_NOT_APPROVED:symbol"):
        admit_strategy(SimpleNamespace(strategy_id="lsrl_v1", spec_digest=CFG.digest(), symbol="BTCUSDT"), good)
