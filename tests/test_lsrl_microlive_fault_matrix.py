"""MICRO-LIVE FAULT MATRIX (weekend readiness, tasks 2 and 3).

Every fault the mission lists, injected at the boundary where it hurts, under EVERY way the exchange might handle the preset stop
(one whole-position pos_loss, a loss_plan per order, and the fallback where the exchange ignores the preset). After every
fault the EXCHANGE - not the executor's beliefs - must hold either a stop at the frozen price covering the whole position
(PROTECTED) or no position at all (VERIFIED_FLAT), it must stay that way, and no emergency may be left running.

  process kill before fill / during fill · network loss · websocket loss · REST timeout (reads AND writes that succeeded
  but whose reply was lost) · partial fill · restart after fill · stop rejection · stop replacement failure (including a
  death between the cancel and the place) · duplicate events · stale state · host restart

Then the startup audit: the exchange is authoritative, everything on the symbol is classified against it.
"""
from __future__ import annotations

import shutil

import pytest

from clients.bitget_base_client import BitgetOrderRejected
from lsrl.adapters.live import (EXPECTED_ABSENT_GRACE_POLLS, ENGINE_ACK_GRACE_MS, EXPOSURE_NONE, LSRLLiveExecutor)
from lsrl.adapters.process_down import ProcessDownProtection
from lsrl.core.config import STRATEGY_ID
from lsrl.core.types import Direction, PlaceEntry, SetStop
from tests.lsrl_fixtures import RULES, long_sweep
from tests.test_lsrl_executor_hardening import entries, kinds
from tests.test_lsrl_exposure_recovery import Death, LifeRig, converge, frozen_stop, safe, verdict
from tests.test_lsrl_live_executor import q_of
from tests.test_lsrl_process_down_protection import down_for, restart_everything
from tests.test_lsrl_risk_integration import _isolate  # noqa: F401  (autouse isolation fixture)

MODELS = ("PLAN_PER_ORDER", "POSITION_LEVEL", "NONE")


def build(tmp_path, model: str) -> LifeRig:
    rig = LifeRig(tmp_path)
    rig.protection = ProcessDownProtection("POSITION_LEVEL" if model == "POSITION_LEVEL" else "PLAN_LEVEL",
                                           "BITGET_DEMO", "0" * 40, "0" * 64)
    rig.exec.process_down = rig.protection
    rig.ex.preset_model = model
    return rig.run(long_sweep())


@pytest.fixture(params=MODELS)
def mrig(request, tmp_path):
    return build(tmp_path, request.param)


def move_to(rig, delta: float = 0.4) -> list:
    """The engine asks for a stop move (what TP1 does): through the real entry point, so failures take the real path."""
    if rig.engine.setup is None or rig.ex.position["long"] <= 1e-9:
        return []                                       # already flat (the executor flattened): there is nothing left to move
    new = round(frozen_stop(rig) + delta, 3)
    object.__setattr__(rig.engine.setup, "stop_price", new)          # in the real flow the ENGINE decides every move: keep it in step
    return rig.exec.apply([SetStop(rig.engine.setup.setup_id, Direction.LONG, new, q_of(rig), "TEST_MOVE")], rig.ex.now_ms)


# =========================================================================================== the twelve faults
def fault_process_kill_before_fill(rig):
    down_for(rig, 30)
    rig.ex.fill(entries(rig)[0])                       # fills with nothing running (naked until restart if the exchange ignored the preset)
    down_for(rig, 120)
    restart_everything(rig)


def fault_process_kill_during_fill(rig):
    first, second = entries(rig)[:2]
    rig.ex.fill(first, qty=q_of(rig) / 2)              # a partial fill is visible ...
    real = rig.ex.get_tpsl_orders

    def die(*a, **k):
        rig.ex.get_tpsl_orders = real
        raise Death()                                  # ... and the process dies at its first look at the stop
    rig.ex.get_tpsl_orders = die
    rig.drive(2)
    rig.ex.fill(second)                                # another rung fills right after the restart


def fault_network_loss_at_the_fill(rig):
    rig.ex.fill(entries(rig)[0])
    rig.ex.outage = True
    rig.drive(75)
    rig.ex.outage = False


def fault_rest_timeout_on_every_read(rig):
    rig.ex.fill(entries(rig)[0])
    names = ("get_all_positions", "get_pending_orders", "get_tpsl_orders", "find_order_by_client_oid", "get_order_history")
    for n in names:
        rig.ex.fail[n] = TimeoutError("read timed out")
    rig.drive(30)
    for n in names:
        del rig.ex.fail[n]


def fault_rest_timeout_after_the_stop_request_succeeded(rig):
    """The exchange APPLIED the request; the reply was lost. The executor must not assume, and must not double-place."""
    real_stop, real_pos = rig.ex.move_futures_stop_loss, rig.ex.move_position_level_stop_only

    def applied_then_timeout(fn):
        def wrapper(*a, **k):
            fn(*a, **k)
            raise TimeoutError("read timed out after the request was accepted")
        return wrapper
    rig.ex.move_futures_stop_loss = applied_then_timeout(real_stop)
    rig.ex.move_position_level_stop_only = applied_then_timeout(real_pos)
    rig.ex.fill(entries(rig)[0])
    rig.drive(4)
    move_to(rig)                                       # and a move whose reply is lost too
    rig.drive(2)
    rig.ex.move_futures_stop_loss, rig.ex.move_position_level_stop_only = real_stop, real_pos


def fault_rest_timeout_after_the_close_succeeded(rig):
    rig.ex.stop_vanishes = True                        # protection cannot be proven: the executor must flatten ...
    real = rig.ex.close_futures_position_full

    def applied_then_timeout(*a, **k):
        real(*a, **k)
        raise TimeoutError("read timed out after the close was accepted")   # ... and the close's reply is lost
    rig.ex.close_futures_position_full = applied_then_timeout
    rig.ex.fill(entries(rig)[0])
    rig.drive(6)
    rig.ex.stop_vanishes = False
    rig.ex.close_futures_position_full = real


def fault_partial_fill_then_kill_then_rest(rig):
    first = entries(rig)[0]
    rig.ex.fill(first, qty=q_of(rig) * 0.4)
    rig.drive(3)
    restart_everything(rig)
    rig.ex.fill(first, qty=q_of(rig) * 0.6)


def fault_restart_after_fill(rig):
    rig.ex.fill(entries(rig)[0])
    rig.drive(6)
    rig.restart()


def fault_stop_rejection(rig):
    rig.ex.fail["move_futures_stop_loss"] = BitgetOrderRejected("status=REJECTED stop refused")
    rig.ex.fail["move_position_level_stop_only"] = BitgetOrderRejected("status=REJECTED stop refused")
    rig.ex.fill(entries(rig)[0])
    rig.drive(5)
    move_to(rig)                                       # a rejected MOVE must never leave the position worse than it was
    rig.drive(10)
    rig.ex.fail.pop("move_futures_stop_loss", None)
    rig.ex.fail.pop("move_position_level_stop_only", None)


def fault_stop_replacement_dies_between_cancel_and_place(rig):
    rig.ex.fill(entries(rig)[0])
    rig.drive(5)
    real_move, real_pos = rig.ex.move_futures_stop_loss, rig.ex.move_position_level_stop_only

    def half_a_move(*a, **k):
        rig.ex.plans = [p for p in rig.ex.plans if "loss" not in p["planType"]]      # the cancel half happened, the place did not
        rig.ex.move_futures_stop_loss, rig.ex.move_position_level_stop_only = real_move, real_pos
        raise ConnectionError("connection dropped between the cancel and the place")
    rig.ex.move_futures_stop_loss = half_a_move
    rig.ex.move_position_level_stop_only = half_a_move
    move_to(rig)
    rig.drive(2)


def fault_stop_replacement_vanishes(rig):
    rig.ex.fill(entries(rig)[0])
    rig.drive(5)
    rig.ex.stop_vanishes = True                        # a replacement is accepted, then cancelled about a second later
    move_to(rig)
    rig.drive(4)
    rig.ex.stop_vanishes = False


def fault_duplicate_events_and_rows(rig):
    def doubled(fn):
        def wrapper(*a, **k):
            res = fn(*a, **k)
            data = dict(res.get("data") or {})
            if isinstance(data.get("entrustedList"), list):
                data["entrustedList"] = data["entrustedList"] * 2
            return {**res, "data": data}
        return wrapper
    for name in ("get_pending_orders", "get_order_history", "get_tpsl_orders"):
        setattr(rig.ex, name, doubled(getattr(rig.ex, name)))
    rig.ex.fill(entries(rig)[0])
    rig.drive(3)
    rig.exec.apply(list(rig.intents[:5]), rig.ex.now_ms)   # the engine's ladder is DELIVERED AGAIN (restart / retry)
    rig.ex.fill(entries(rig)[1])
    rig.drive(3)


def fault_stale_executor_state(rig):
    """The executor state file is rolled back to BEFORE the fill (a lost write, a restored backup): the exchange is truth."""
    before = rig.state.path.read_bytes() if hasattr(rig.state, "path") else None
    rig.ex.fill(entries(rig)[0])
    rig.drive(6)
    if before is not None:
        rig.state.path.write_bytes(before)
    rig.restart()


def fault_host_restart(rig):
    rig.ex.fill(entries(rig)[0])
    rig.drive(6)
    down_for(rig, 900)
    restart_everything(rig)


MATRIX = [fault_process_kill_before_fill, fault_process_kill_during_fill, fault_network_loss_at_the_fill,
          fault_rest_timeout_on_every_read, fault_rest_timeout_after_the_stop_request_succeeded,
          fault_rest_timeout_after_the_close_succeeded, fault_partial_fill_then_kill_then_rest, fault_restart_after_fill,
          fault_stop_rejection, fault_stop_replacement_dies_between_cancel_and_place, fault_stop_replacement_vanishes,
          fault_duplicate_events_and_rows, fault_stale_executor_state, fault_host_restart]


@pytest.mark.parametrize("fault", MATRIX, ids=lambda f: f.__name__.removeprefix("fault_"))
def test_every_fault_ends_protected_or_verified_flat_under_every_exchange_model(mrig, fault):
    fault(mrig)
    converge(mrig)
    assert mrig.exec.emergency is None
    assert verdict(mrig) in ("PROTECTED", "FLAT")


@pytest.mark.parametrize("fault", MATRIX, ids=lambda f: f.__name__.removeprefix("fault_"))
def test_every_fault_also_converges_when_the_engine_never_learns_of_the_fill(mrig, fault):
    fault(mrig)
    mrig.drive(400, deliver=False, until=safe)
    assert verdict(mrig) in ("PROTECTED", "FLAT")
    mrig.drive(ENGINE_ACK_GRACE_MS / 1000 + 30, deliver=False)
    assert verdict(mrig) in ("PROTECTED", "FLAT") and mrig.exec.emergency is None


def test_a_fault_never_double_places_a_stop_or_an_entry(mrig):
    """Duplicated rows and re-delivered intents must not multiply anything on the exchange."""
    fault_duplicate_events_and_rows(mrig)
    converge(mrig)
    assert len([o for o in mrig.ex.orders.values() if o["tradeSide"] == "open"]) == 5
    assert len([p for p in mrig.ex.plans if "loss" in p["planType"]]) <= 2          # one per filled order at most, never a pile


# =========================================================================================== websocket loss (runtime level)
def test_a_websocket_loss_does_not_stop_the_executor_protecting_fills_that_happen_during_it(tmp_path):
    """The runtime drives the executor by its own poll, not by the market-data feed: with the feed down (observer unsafe,
    reconnect requested) a rung that fills is still protected and the earlier protection is kept."""
    from dataclasses import replace
    from lsrl.core.types import FeedMarker
    from tests.test_lsrl_feed_and_runtime import LiveWire, timeline_events
    wire = LiveWire(tmp_path)
    wire.live.ex.preset_model = "PLAN_PER_ORDER"
    wire.run(timeline_events())
    ex, engine = wire.live.ex, wire.live.engine
    first, second = sorted((o for o in ex.orders.values() if o["tradeSide"] == "open"), key=lambda o: -o["price"])[:2]
    ex.fill(first["clientOid"])
    wire.bus_clock.t += 5_000
    ex.now_ms = wire.bus_clock.t
    wire.step()
    wire.step()
    assert ex.stop_plan is not None                                                  # protected, feed healthy
    wire.rt.ws.connected = False                                                     # the WebSocket dies
    wire.rt.bus.push(lambda t: FeedMarker(t, "DISCONNECT", "c1"))
    wire.step()
    assert wire.observer.state != "OK"                                                # the observer refuses new signals ...
    ex.fill(second["clientOid"])                                                     # ... and another rung fills during the outage
    for _ in range(3):
        wire.bus_clock.t += 4_000
        ex.now_ms = wire.bus_clock.t
        wire.step()
        wire.step()
    losses = [p for p in ex.plans if "loss" in p["planType"] and p["posSide"] == "long"]
    assert sum(float(p["size"]) for p in losses) >= ex.position["long"] - 1e-9 and losses
    assert all(abs(float(p["triggerPrice"]) - engine.setup.stop_price) < 0.006 for p in losses)
    assert wire.live.exec.exposure["state"] in ("PROTECTED", "NONE") and wire.live.exec.emergency is None


# =========================================================================================== task 3: the startup audit
def lost_state(rig, *, keep_claim: bool = True) -> None:
    rig.state.save({})
    if not keep_claim:
        from execution.strategy_ownership import StrategyOwnershipRegistry
        rig.exec.ownership.release(rig.ex.contract["symbol"], "long", STRATEGY_ID)
    rig.restart()


def place_untracked(rig, oid: str, *, close: bool = False, price: float = 149.0, size: float = 1.0) -> None:
    if close:
        rig.ex.position["long"] = max(rig.ex.position["long"], size)
        rig.ex.place_futures_limit_close_order("SOLUSDT", "long", size, price + 3, client_oid=oid, post_only=True)
    else:
        rig.ex.place_futures_limit_order("SOLUSDT", "LONG", size, price, client_oid=oid, post_only=True)


def test_the_startup_audit_reads_positions_orders_and_plans_and_records_a_durable_report(tmp_path):
    rig = LifeRig(tmp_path).run(long_sweep())
    rig.ex.preset_model = "PLAN_PER_ORDER"
    rig.ex.fill(entries(rig)[0])
    rig.drive(5)
    rig.restart()
    rig.drive(3)
    report = rig.exec.startup_report
    assert report["positions"]["long"] == pytest.approx(q_of(rig))
    assert set(entries(rig)[1:]) <= set(report["tracked_live"])                       # the four untouched rungs are recognised
    assert report["orphan_orders_cancelled"] == [] and report["unclassified_orders"] == []
    assert [s["type"] for s in report["stops"]] == ["loss_plan"] and report["cancel_failures"] == []
    rig.restart()                                                                   # the report itself survives a restart
    assert rig.exec.startup_report["positions"]["long"] == pytest.approx(q_of(rig))


def test_an_untracked_take_profit_named_for_lsrl_is_cancelled_at_startup(tmp_path):
    rig = LifeRig(tmp_path).run(long_sweep())
    rig.ex.preset_model = "PLAN_PER_ORDER"
    rig.ex.fill(entries(rig)[0])
    rig.drive(5)
    place_untracked(rig, "lsrlDEADBEEFtp1v9", close=True)
    lost_state(rig)                                                                  # it is no longer in the executor's records
    rig.drive(4)
    assert rig.ex.orders["lsrlDEADBEEFtp1v9"]["state"] == "canceled"
    assert "lsrlDEADBEEFtp1v9" in rig.exec.startup_report["orphan_orders_cancelled"]
    assert "ORPHAN_ORDER_CANCELLED" in kinds(rig)


def test_an_untracked_entry_on_a_side_we_own_is_cancelled_but_on_a_side_we_do_not_is_left_alone(tmp_path):
    rig = LifeRig(tmp_path).run(long_sweep())
    for oid in entries(rig):                                                         # the ladder is gone: state is about to be lost
        rig.ex.orders[oid]["state"] = "canceled"
    place_untracked(rig, "bgai-k-untracked-long")
    lost_state(rig)                                                                  # the registry still says LONG is LSRL's
    rig.drive(3)
    assert rig.ex.orders["bgai-k-untracked-long"]["state"] == "canceled"

    rig2 = LifeRig(tmp_path / "unowned").run(long_sweep())
    for oid in entries(rig2):
        rig2.ex.orders[oid]["state"] = "canceled"
    place_untracked(rig2, "bgai-k-someone-elses")
    lost_state(rig2, keep_claim=False)                                               # LSRL has no claim on the side any more
    rig2.exec.setups.clear()
    rig2.drive(3)
    assert rig2.ex.orders["bgai-k-someone-elses"]["state"] == "live"                 # never touched
    assert "UNCLASSIFIED_ORDER_LEFT_ALONE" in kinds(rig2)


def test_a_leftover_plan_order_on_a_flat_side_we_own_is_cancelled_so_it_cannot_protect_the_next_position_at_a_stale_price(tmp_path):
    rig = LifeRig(tmp_path).run(long_sweep())
    rig.ex.preset_model = "PLAN_PER_ORDER"
    rig.ex.fill(entries(rig)[0])
    rig.drive(5)
    rig.ex.position["long"] = 0.0                                                    # closed while nothing ran; the plan order stayed
    for o in rig.ex.orders.values():
        o["state"] = "canceled" if o["state"] in ("live", "partially_filled") else o["state"]
    assert [p["planType"] for p in rig.ex.plans] == ["loss_plan"]
    lost_state(rig)
    rig.drive(3)
    assert rig.ex.plans == [] and rig.exec.startup_report["orphan_plans_cancelled"]
    assert "ORPHAN_PLAN_ORDERS_CANCELLED" in kinds(rig)


def test_a_plan_order_that_is_not_ours_is_left_alone_and_reported(tmp_path):
    rig = LifeRig(tmp_path)
    rig.ex.plans.append({"orderId": "F1", "planType": "loss_plan", "posSide": "short", "holdSide": "short",
                         "triggerPrice": "160.0", "size": "1.0"})                    # a SHORT-side stop; LSRL has never traded SHORT
    rig.restart()
    rig.drive(3)
    assert len(rig.ex.plans) == 1 and rig.exec.startup_report["foreign_plans"]
    assert "FOREIGN_PLAN_ORDER_LEFT_ALONE" in kinds(rig)


@pytest.mark.parametrize("unreadable", ["get_all_positions", "get_pending_orders", "get_tpsl_orders"])
def test_an_unreadable_exchange_at_startup_is_never_an_empty_account(tmp_path, unreadable):
    rig = LifeRig(tmp_path).run(long_sweep())
    rig.restart()
    rig.ex.fail[unreadable] = TimeoutError("read timed out")
    rig.drive(6, deliver=False)
    assert rig.exec._recovered is False and rig.exec.halt_reason.startswith("STARTUP_EXCHANGE_UNREADABLE")
    assert "unreadable" in rig.exec.startup_report
    assert rig.exec.apply([PlaceEntry("x", "t", 1, Direction.LONG, 149.5, 0.1, 1)], rig.ex.now_ms)[0].status == "REJECTED"
    del rig.ex.fail[unreadable]
    rig.drive(4)
    assert rig.exec._recovered is True and "unreadable" not in rig.exec.startup_report        # retried until readable


def test_the_exchange_wins_over_local_state_that_is_wrong_in_every_way_at_once(tmp_path):
    """Local state says: rung orders live, a stop at price A. The exchange says: those orders are gone, the position exists and
    someone moved the stop to price B. After restart the position is PROTECTED at the frozen price or FLAT - never trusted as recorded."""
    rig = LifeRig(tmp_path).run(long_sweep())
    rig.ex.preset_model = "PLAN_PER_ORDER"
    rig.ex.fill(entries(rig)[0])
    rig.drive(5)
    for oid in entries(rig)[1:]:
        rig.ex.orders[oid]["state"] = "canceled"                                     # local state still thinks they are live
    for p in rig.ex.plans:
        p["triggerPrice"] = str(frozen_stop(rig) - 3.0)                              # the stop was moved to a worse price behind our back
    rig.restart()
    converge(rig)
    assert verdict(rig) in ("PROTECTED", "FLAT")
    if verdict(rig) == "PROTECTED":
        assert all(abs(float(p["triggerPrice"]) - frozen_stop(rig)) < 0.006 for p in rig.ex.plans if "loss" in p["planType"])


# ---- write-ahead entries: resolved at startup AND at runtime
def test_a_write_ahead_entry_the_exchange_does_not_show_is_dropped_only_after_a_grace_never_on_one_empty_answer(tmp_path):
    rig = LifeRig(tmp_path).run(long_sweep())
    setup = rig.exec.setups[rig.engine.setup.setup_id]
    setup["expected_entries"]["bgai-k-late-visible"] = {"tag": "x", "rung": 9, "price": 149.0, "qty": 0.1,
                                                       "direction": "LONG", "t_ms": 1}
    for _ in range(EXPECTED_ABSENT_GRACE_POLLS - 1):
        rig.drive(1)                                                                 # ONE poll per step
        assert "bgai-k-late-visible" in setup["expected_entries"]                    # visibility can lag: keep it
    rig.drive(1)
    assert "bgai-k-late-visible" not in setup["expected_entries"]


def test_a_write_ahead_entry_that_appears_late_is_adopted(tmp_path):
    rig = LifeRig(tmp_path).run(long_sweep())
    setup = rig.exec.setups[rig.engine.setup.setup_id]
    setup["expected_entries"]["bgai-k-late-visible"] = {"tag": "x", "rung": 9, "price": 149.0, "qty": 0.1,
                                                       "direction": "LONG", "t_ms": 1}
    rig.drive(4)
    place_untracked(rig, "bgai-k-late-visible", size=0.1)                            # the exchange finally shows it
    rig.drive(4)
    assert "bgai-k-late-visible" in rig.exec.orders and "ENTRY_ORDER_ADOPTED" in kinds(rig)


def test_an_unreadable_lookup_never_drops_a_write_ahead_entry_and_keeps_new_exposure_blocked(tmp_path):
    rig = LifeRig(tmp_path).run(long_sweep())
    setup = rig.exec.setups[rig.engine.setup.setup_id]
    setup["expected_entries"]["bgai-k-unknown"] = {"tag": "x", "rung": 9, "price": 149.0, "qty": 0.1, "direction": "LONG", "t_ms": 1}
    rig.ex.lookup_unknown = True
    rig.drive(30)
    assert "bgai-k-unknown" in setup["expected_entries"] and "ORDER_STATE_UNKNOWN" in (rig.exec.halt_reason or "")
    rig.ex.lookup_unknown = False


@pytest.mark.parametrize("model", MODELS)
def test_an_entry_the_exchange_accepted_but_the_executor_could_not_see_never_sits_as_unmanaged_exposure(tmp_path, model):
    """Found by this matrix: the write-ahead record was only resolved at process START. An accepted-but-unknown entry that
    filled in a RUNNING process was just logged as UNEXPLAINED_EXPOSURE for ever - naked if the preset was ignored."""
    rig = build(tmp_path, model) if False else LifeRig(tmp_path)
    rig.protection = ProcessDownProtection("POSITION_LEVEL" if model == "POSITION_LEVEL" else "PLAN_LEVEL", "BITGET_DEMO", "0" * 40, "0" * 64)
    rig.exec.process_down = rig.protection
    rig.ex.preset_model = model
    real, calls = rig.ex.place_futures_limit_order, {"n": 0}

    def accepted_but_reply_lost(*a, **k):
        calls["n"] += 1
        res = real(*a, **k)
        if calls["n"] == 1:
            raise TimeoutError("read timed out")
        return res
    rig.ex.place_futures_limit_order = accepted_but_reply_lost
    rig.ex.fail_counts["find_order_by_client_oid"] = 50                              # and the lookup that would resolve it fails too
    rig.run(long_sweep())
    (setup,) = rig.exec.setups.values()
    (oid,) = setup["expected_entries"]
    rig.ex.fill(oid)
    rig.ex.fail_counts["find_order_by_client_oid"] = 0
    rig.drive(60)
    assert rig.ex.position["long"] == 0.0 or verdict(rig) == "PROTECTED"
    assert rig.exec.emergency is None and rig.exec.exposure["state"] == EXPOSURE_NONE


# ==================================================================================== properties pinned by the mutation campaign
def test_exposure_on_a_side_with_an_unresolved_write_ahead_entry_is_owned_even_with_no_ledger_no_resting_order_and_no_claim(tmp_path):
    """Rule (d) directly: nothing else says this position is ours - only the unresolved entry does."""
    rig = LifeRig(tmp_path).run(long_sweep())
    setup = rig.exec.setups[rig.engine.setup.setup_id]
    for oid in entries(rig):
        rig.ex.orders[oid]["state"] = "canceled"
    for rec in rig.exec.orders.values():
        rec["terminal"], rec["terminal_state"] = True, "canceled"
    rig.exec.ownership.release("SOLUSDT", "long", STRATEGY_ID)
    setup["expected_entries"]["bgai-k-unknown"] = {"tag": "x", "rung": 1, "price": 149.0, "qty": 1.0, "direction": "LONG", "t_ms": 1}
    positions = {"long": 1.0, "short": 0.0}
    assert rig.exec._ledger("long") == 0.0 and not rig.exec._claimed_by_us("long")
    assert "long" in rig.exec._owned_exposure(positions)
    setup["expected_entries"].clear()
    assert rig.exec._owned_exposure(positions) == {}                                 # without it the same exposure is foreign


def test_after_an_emergency_flatten_no_take_profit_is_attempted_on_the_flat_position(tmp_path):
    rig = build(tmp_path, "NONE")
    rig.ex.fail["move_futures_stop_loss"] = BitgetOrderRejected("status=REJECTED stop refused")   # protection cannot be placed
    rig.ex.fill(entries(rig)[0])
    rig.drive(20)
    assert verdict(rig) == "FLAT" and any(c[0] == "emergency_close" for c in rig.ex.calls)
    close_at = max(i for i, c in enumerate(rig.ex.calls) if c[0] == "emergency_close")
    assert not [c for c in rig.ex.calls[close_at + 1:] if c[0] == "place_tp"]        # nothing placed after the flatten
    assert "TP_CANNOT_BE_PLACED" not in kinds(rig) and "TP_CANNOT_BE_PLACED" not in (rig.exec.halt_reason or "")


def test_the_fast_watch_looks_about_once_a_second_while_a_write_ahead_entry_is_unresolved_even_with_nothing_resting(tmp_path):
    rig = LifeRig(tmp_path).run(long_sweep())
    setup = rig.exec.setups[rig.engine.setup.setup_id]
    for rec in rig.exec.orders.values():
        rec["terminal"], rec["terminal_state"] = True, "canceled"
    setup["expected_entries"]["bgai-k-unknown"] = {"tag": "x", "rung": 1, "price": 149.0, "qty": 1.0, "direction": "LONG", "t_ms": 1}
    seen = []
    real = rig.exec.poll
    rig.exec.poll = lambda t: (seen.append(t), real(t))[1]
    rig.ex.now_ms += 5_000
    rig.exec._last_fast_ms = 0
    rig.exec.apply([], rig.ex.now_ms)
    assert len(seen) == 1                                                             # one look, triggered by apply() alone


def test_a_setup_with_an_unresolved_write_ahead_entry_is_never_finalized_and_keeps_its_ownership(tmp_path):
    rig = LifeRig(tmp_path)
    real, calls = rig.ex.place_futures_limit_order, {"n": 0}

    def accepted_but_reply_lost(*a, **k):
        calls["n"] += 1
        res = real(*a, **k)
        if calls["n"] == 1:
            raise TimeoutError("read timed out")
        return res
    rig.ex.place_futures_limit_order = accepted_but_reply_lost
    rig.ex.fail_counts["find_order_by_client_oid"] = 200
    rig.run(long_sweep())
    (setup,) = rig.exec.setups.values()
    rig.drive(40)
    assert setup["expected_entries"] and setup["finalized"] is False
    assert rig.exec.ownership.owner_of("SOLUSDT", "long") == STRATEGY_ID or rig.exec._claimed_by_us("long")


def test_an_entry_adopted_for_a_setup_the_engine_already_closed_is_cancelled_not_left_resting(tmp_path):
    rig = LifeRig(tmp_path)
    real, calls = rig.ex.place_futures_limit_order, {"n": 0}

    def accepted_but_reply_lost(*a, **k):
        calls["n"] += 1
        res = real(*a, **k)
        if calls["n"] == 1:
            raise TimeoutError("read timed out")
        return res
    rig.ex.place_futures_limit_order = accepted_but_reply_lost
    rig.ex.fail_counts["find_order_by_client_oid"] = 200
    rig.run(long_sweep())
    (setup,) = rig.exec.setups.values()
    (oid,) = setup["expected_entries"]
    assert rig.engine.setup is None and setup["engine_closed"] is False               # the engine gave up; the executor has not heard
    rig.exec.engine_finished(setup["setup_id"], rig.ex.now_ms)                        # what the live driver does on `setup_rejected`
    assert setup["engine_closed"] is True
    rig.ex.fail_counts["find_order_by_client_oid"] = 0
    rig.drive(30)                                                                     # the lookup recovers; the order is found ...
    assert rig.ex.orders[oid]["state"] == "canceled"                                  # ... and is not left resting for a dead setup
    assert not setup["expected_entries"] and "ENTRY_ORDER_ADOPTED" in kinds(rig)


def test_through_the_real_live_driver_an_entry_the_engine_gave_up_on_is_cancelled_when_it_is_finally_found(tmp_path):
    from tests.test_lsrl_feed_and_runtime import LiveWire, timeline_events
    wire = LiveWire(tmp_path)
    ex = wire.live.ex
    real, calls = ex.place_futures_limit_order, {"n": 0}

    def accepted_but_reply_lost(*a, **k):
        calls["n"] += 1
        res = real(*a, **k)
        if calls["n"] == 1:
            raise TimeoutError("read timed out")
        return res
    ex.place_futures_limit_order = accepted_but_reply_lost
    ex.fail_counts["find_order_by_client_oid"] = 200
    wire.run(timeline_events())
    (setup,) = wire.live.exec.setups.values()
    assert wire.live.engine.setup is None and setup["engine_closed"] is True          # told by the driver, no test help
    (oid,) = setup["expected_entries"]
    ex.fail_counts["find_order_by_client_oid"] = 0
    for _ in range(6):
        wire.bus_clock.t += 4_000
        ex.now_ms = wire.bus_clock.t
        wire.step()
    assert ex.orders[oid]["state"] == "canceled" and not setup["expected_entries"]
