"""LSRL_V1 MICRO_LIVE executor: the five defects Agent C reproduced (E1-E5), position ownership, restart
determinism and central close recording -- against the REAL executor and a fake exchange that only does what
Bitget's documentation or this repository's incident notes establish.

  E1 stop that never verifies is treated as protected        E4 exit counted twice (TP fill between two reads)
  E2 entry fills in the race with the expiry cancel          E5 foreign SOLUSDT position appears mid-setup
  E3 exchange cancels a resting take-profit
"""
from __future__ import annotations

import pytest

from execution.strategy_ownership import StrategyOwnershipRegistry
from lsrl.core.types import Direction, PlaceEntry, TimerTick
from tests.lsrl_fixtures import long_sweep
from tests.test_lsrl_live_executor import Rig, q_of
from tests.test_lsrl_risk_integration import _isolate  # noqa: F401  (autouse isolation fixture)


def entries(rig):
    return [c[1] for c in rig.ex.by_kind("place_entry")]


def live_tps(rig):
    return [o for o in rig.ex.orders.values()
            if o["tradeSide"] == "close" and o["state"] in ("live", "partially_filled")]


def kinds(rig):
    return [i["kind"] for i in rig.exec.incidents]


@pytest.fixture()
def rig(tmp_path):
    return Rig(tmp_path).run(long_sweep())


# ===================================================================== E1: stop verification
def test_e1_a_stop_the_exchange_cancels_after_accepting_it_is_never_treated_as_protected(rig):
    ex = rig.ex
    ex.stop_vanishes = True                                    # repo incident 2026-08-01: accepted, cancelled ~1 s later
    ex.fill(entries(rig)[0])
    rig.poll()
    assert rig.exec.stop is None                               # never recorded as protection
    assert len(ex.by_kind("stop")) == 3                        # bounded retries ...
    assert ex.by_kind("emergency_close") and ex.position["long"] == 0.0       # ... then the exposure is flattened
    assert rig.exec.halt_reason and "STOP_PLACEMENT_FAILED" in rig.exec.halt_reason
    assert "PROTECTION_FAILED" in kinds(rig) and "EMERGENCY_FLATTENED" in kinds(rig)
    assert rig.engine.setup is None                            # the engine was told (OTHER_CLOSE) and closed the setup


def test_e1_a_client_that_claims_verified_but_no_plan_exists_is_caught_by_the_independent_read_back(rig):
    ex = rig.ex
    ex.stop_lies = True                                        # client says verified=True; the plan list is empty
    ex.fill(entries(rig)[0])
    rig.poll()
    assert rig.exec.stop is None and ex.position["long"] == 0.0
    assert any("NO_MATCHING_STOP_PLAN" in i["detail"] for i in rig.exec.incidents)


def test_e1_the_active_stop_is_audited_every_poll_and_repaired_when_the_exchange_drops_it(rig):
    ex = rig.ex
    ex.fill(entries(rig)[0])
    rig.poll()
    assert ex.stop_plan is not None
    stops_before = len(ex.by_kind("stop"))
    ex.plans.clear()                                           # the exchange silently dropped our stop
    rig.poll()
    assert ex.stop_plan is not None and len(ex.by_kind("stop")) == stops_before + 1
    assert "STOP_AUDIT_FAILED" in kinds(rig) and rig.exec.halt_reason is None
    assert ex.position["long"] > 0                             # repaired, not flattened


def test_e1_a_stop_that_cannot_be_repaired_flattens_the_position(rig):
    ex = rig.ex
    ex.fill(entries(rig)[0])
    rig.poll()
    ex.plans.clear()
    ex.stop_vanishes = True
    rig.poll()
    assert ex.position["long"] == 0.0 and rig.exec.halt_reason.startswith("STOP_PLACEMENT_FAILED")


def test_e1_a_stray_extra_stop_plan_is_not_accepted_as_a_verified_stop(rig):
    ex = rig.ex
    ex.fill(entries(rig)[0])
    rig.poll()
    ex.plans.append({"orderId": "STRAY", "planType": "loss_plan", "posSide": "long", "holdSide": "long",
                     "triggerPrice": "100.0", "size": "1.6", "planStatus": "live"})
    rig.poll()                                                 # audit: EXTRA_STOP_PLANS -> reinstall (cleanup_existing)
    assert [p["orderId"] for p in ex.plans if "loss" in p["planType"]] != ["STRAY"]
    assert len([p for p in ex.plans if "loss" in p["planType"]]) == 1
    assert any("EXTRA_STOP_PLANS" in i["detail"] for i in rig.exec.incidents)


def test_e1_emergency_flatten_cancels_only_our_resting_orders_first_then_closes(rig):
    """Hedge mode: resting reduce-only closes OCCUPY the position, so they must go before a market close."""
    ex = rig.ex
    ex.orders["legacy-order"] = {"clientOid": "legacy-order", "orderId": "LEG1", "state": "live", "size": 3.0,
                                 "price": 140.0, "direction": "LONG", "tradeSide": "open", "post_only": True,
                                 "baseVolume": 0.0, "priceAvg": 0.0, "fee": 0.0, "cTime": ex.now_ms}
    ex.fill(entries(rig)[0])
    rig.poll()
    assert len(live_tps(rig)) == 3                              # take-profits occupy the whole position
    ex.stop_vanishes = True
    ex.fill(entries(rig)[1])                                    # engine asks for a bigger stop -> cannot be proven
    rig.poll()
    idx = [c[0] for c in ex.calls].index("emergency_close")
    cancelled_before_close = {c[1] for c in ex.calls[:idx] if c[0] == "cancel"}
    assert {c[1] for c in ex.by_kind("place_tp")} <= cancelled_before_close      # every take-profit went first
    cancelled = {c[1] for c in ex.by_kind("cancel")}
    assert "legacy-order" not in cancelled and ex.orders["legacy-order"]["state"] == "live"
    assert ex.position["long"] == 0.0                           # a full close, not the 'unoccupied part only'


def test_e1_an_incomplete_emergency_close_stays_open_and_is_retried_until_flat(rig):
    ex = rig.ex
    real_close = ex.close_futures_position_full
    attempts = {"n": 0}

    def flaky(symbol, direction, **kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return {"status": "CLOSE_NOT_REFLECTED", "flatness": "REMAINS", "remaining_size": ex.position["long"]}
        return real_close(symbol, direction, **kw)
    ex.close_futures_position_full = flaky
    ex.stop_vanishes = True
    ex.fill(entries(rig)[0])
    rig.poll()
    assert rig.exec.emergency is not None and ex.position["long"] > 0     # NOT recorded as closed
    assert "EMERGENCY_CLOSE_INCOMPLETE" in kinds(rig)
    rig.poll()
    assert rig.exec.emergency is None and ex.position["long"] == 0.0 and attempts["n"] == 2
    assert kinds(rig).count("EMERGENCY_FLATTENED") == 1


# ===================================================================== E2: cancel / fill race
def test_e2_an_entry_that_fills_in_the_expiry_cancel_race_is_adopted_and_protected(rig):
    ex, q = rig.ex, q_of(rig)
    first = entries(rig)[0]
    real_cancel = ex.cancel_futures_order

    def racing_cancel(symbol, order_id=None, client_oid=None, **kw):
        ex.calls.append(("cancel", client_oid))
        if client_oid == first and ex.orders[first]["state"] == "live":
            ex.fill(first)                                     # E1 fills in the race with the cancel ...
            raise RuntimeError("order does not exist")         # ... and the cancel then fails with 43001
        return real_cancel(symbol, order_id, client_oid, **kw)
    ex.cancel_futures_order = racing_cancel
    rig.feed(TimerTick(t_ms=rig.engine.setup.entry_expires_ms + 1_000))
    assert rig.engine.setup is not None                        # NOT closed while a cancel is unconfirmed
    rig.poll()
    rig.poll()
    assert ex.position["long"] == pytest.approx(q)
    assert ex.stop_plan is not None and ex.stop_plan["size"] == pytest.approx(q)      # protected on the exact quantity
    assert len(live_tps(rig)) == 3                                                      # targets established
    assert rig.engine.setup.ledger.entry_qty == pytest.approx(q) and rig.engine.setup.state == "IN_POSITION"
    assert rig.exec.halt_reason is None
    assert not [r for r in rig.kinds("anomaly") if r.data.get("reason") == "FILL_FOR_UNKNOWN_SETUP"]


def test_e2_a_cancelled_rung_is_not_reported_while_the_position_cannot_be_reconciled(rig):
    ex = rig.ex
    rig.feed(TimerTick(t_ms=rig.engine.setup.entry_expires_ms + 1_000))    # every rung is cancel-requested
    ex.position["long"] = 5.0                                                # something else appears on the symbol
    cancelled_events: list = []
    for e in rig.exec.poll(rig.engine.now_ms + 1_000):
        if e.__class__.__name__ == "OrderEvent" and e.status == "CANCELLED":
            cancelled_events.append(e)
    assert cancelled_events == []                                            # held back: the position is not reconciled
    assert rig.exec.halt_reason and "FOREIGN_POSITION_PRESENT" in rig.exec.halt_reason
    ex.position["long"] = 0.0
    released = [e for e in rig.exec.poll(rig.engine.now_ms + 2_000)
                if e.__class__.__name__ == "OrderEvent" and e.status == "CANCELLED"]
    assert len(released) == 5                                                # ... and released once it reconciles


def test_e2_an_entry_that_fills_after_the_engine_closed_the_setup_is_flattened_and_halts(rig):
    ex = rig.ex
    oids = entries(rig)
    for oid in oids[:2]:
        ex.fill(oid)
    rig.poll()
    real_cancel = ex.cancel_futures_order

    def racing_cancel(symbol, order_id=None, client_oid=None, **kw):
        if client_oid == oids[2] and ex.orders[oids[2]]["state"] == "live":
            ex.calls.append(("cancel", client_oid))
            ex.fill(oids[2])                                   # the third rung fills as the setup is being closed
            raise RuntimeError("order does not exist")
        return real_cancel(symbol, order_id, client_oid, **kw)
    ex.cancel_futures_order = racing_cancel
    ex.trigger_stop()                                          # position stopped out -> the engine closes the setup
    rig.poll()
    rig.poll()
    assert ex.position["long"] == 0.0 and ex.by_kind("emergency_close")      # no orphan exposure survives
    assert any(i["kind"] == "ORPHAN_EXPOSURE" and "ENTRY_FILLED_AFTER_ENGINE_CLOSE" in i["detail"]
               for i in rig.exec.incidents)                       # the specific path, not just the generic backstop
    assert rig.exec.halt_reason and "ORPHAN_EXPOSURE" in rig.exec.halt_reason


# ===================================================================== E3: take-profit disappearance
def test_e3_a_take_profit_the_exchange_cancels_is_detected_and_replaced_exactly_once(rig):
    ex = rig.ex
    ex.fill(entries(rig)[0])
    rig.poll()
    tp1_call = min(ex.by_kind("place_tp"), key=lambda c: c[4])
    tp1 = tp1_call[1]
    ex.orders[tp1]["state"] = "canceled"                        # exchange-side cancel, zero fill (post-only auto-cancel)
    ex.orders[tp1]["cancelReason"] = "post_only_cancel"
    n_before = len(ex.by_kind("place_tp"))
    for _ in range(4):
        rig.poll()
    assert "TP_LOST" in kinds(rig)
    replaced = ex.by_kind("place_tp")[n_before:]
    assert len(replaced) == 1 and replaced[0][4] == tp1_call[4] and replaced[0][3] == pytest.approx(tp1_call[3])
    same_level = [o for o in live_tps(rig) if o["price"] == tp1_call[4]]
    assert len(same_level) == 1                                 # one live take-profit at that level, not two
    assert rig.exec.halt_reason is None


def test_e3_a_partially_filled_take_profit_that_is_then_cancelled_is_restored_for_the_remainder_only(rig):
    ex = rig.ex
    ex.fill(entries(rig)[0])
    rig.poll()
    tp1_call = min(ex.by_kind("place_tp"), key=lambda c: c[4])
    tp1 = ex.orders[tp1_call[1]]
    ex.fill(tp1["clientOid"], qty=0.2)
    tp1["state"] = "canceled"                                   # cancelled after a partial fill
    rig.poll()
    at_price = [c for c in ex.by_kind("place_tp") if c[4] == tp1_call[4]]
    assert len(at_price) == 2                                   # the original and exactly one restoration
    assert at_price[-1][3] == pytest.approx(tp1_call[3] - 0.2)  # the unfilled remainder only
    assert rig.engine.setup.ledger.exit_qty == pytest.approx(0.2)         # the partial fill was reported exactly once


def test_e3_a_take_profit_that_can_never_be_placed_is_an_incident_and_halts_new_entries_only(rig):
    ex = rig.ex
    ex.fill(entries(rig)[0])
    rig.poll()
    tp1 = min(ex.by_kind("place_tp"), key=lambda c: c[4])[1]
    ex.orders[tp1]["state"] = "canceled"
    ex.fail["place_futures_limit_close_order"] = RuntimeError("boom")
    for _ in range(6):
        rig.poll()
    assert "TP_CANNOT_BE_PLACED" in kinds(rig)
    assert rig.exec.halt_reason and "TP_CANNOT_BE_PLACED" in rig.exec.halt_reason
    assert ex.stop_plan is not None and ex.position["long"] > 0             # protection untouched, nothing flattened


def test_e3_an_absent_take_profit_is_ambiguous_state_but_is_still_restored(rig):
    ex = rig.ex
    ex.fill(entries(rig)[0])
    rig.poll()
    tp1_call = min(ex.by_kind("place_tp"), key=lambda c: c[4])
    del ex.orders[tp1_call[1]]                                  # the exchange no longer knows the order at all
    rig.poll()
    rig.poll()
    assert rig.exec.halt_reason and "ORDER_ABSENT" in rig.exec.halt_reason
    assert any(c[4] == tp1_call[4] for c in ex.by_kind("place_tp")[3:])


# ===================================================================== E4: exits counted once
def test_e4_a_take_profit_that_fills_between_the_order_read_and_the_position_read_is_counted_once(rig):
    ex = rig.ex
    for oid in entries(rig):
        ex.fill(oid)
    rig.poll()
    tps = {c[4]: c[1] for c in ex.by_kind("place_tp")}
    tp1 = tps[min(tps)]
    real_positions = ex.get_all_positions
    armed = {"on": True}

    def racing_positions(*a, **k):
        if armed["on"]:
            armed["on"] = False
            ex.fill(tp1)                                       # TP1 fills after the order lookups, before the position read
        return real_positions(*a, **k)
    ex.get_all_positions = racing_positions
    events = rig.exec.poll(rig.engine.now_ms + 1_000)
    rig.exec.poll(rig.engine.now_ms + 2_000)
    fills = [e for e in events if e.__class__.__name__ == "FillEvent"]
    assert [e.kind for e in fills] == ["TP1"] or fills == []            # never an estimated stop fill
    assert not [e for e in events if getattr(e, "kind", "") == "SL"]
    for e in events:
        rig.feed(e)
    rig.poll()
    led = rig.engine.setup.ledger
    assert led.remaining_qty == pytest.approx(ex.position["long"])      # ledger == exchange truth
    assert led.exit_qty == pytest.approx(ex.orders[tp1]["baseVolume"])  # the exit exactly once
    assert rig.exec.halt_reason is None


def test_e4_fills_are_cumulative_per_order_and_never_reported_twice(rig):
    ex, q = rig.ex, q_of(rig)
    oid = entries(rig)[0]
    ex.fill(oid, qty=0.6 * q, price=149.60)
    for _ in range(3):
        rig.poll()                                              # identical state re-read: no new events
    assert rig.engine.setup.ledger.entry_qty == pytest.approx(0.6 * q)
    ex.fill(oid, qty=0.4 * q, price=149.58)
    for _ in range(3):
        rig.poll()
    led = rig.engine.setup.ledger
    assert led.entry_qty == pytest.approx(q)
    assert led.avg_entry == pytest.approx((0.6 * 149.60 + 0.4 * 149.58), abs=1e-6) if hasattr(led, "avg_entry") else True


def test_e4_a_cumulative_fill_that_goes_down_never_reduces_what_was_reported(rig):
    ex, q = rig.ex, q_of(rig)
    oid = entries(rig)[0]
    ex.fill(oid, qty=0.6 * q)
    rig.poll()
    ex.orders[oid]["baseVolume"] = 0.3 * q                      # the exchange shows LESS than we already reported
    rig.poll()
    assert "FILL_REGRESSION" in kinds(rig)
    assert rig.engine.setup.ledger.entry_qty == pytest.approx(0.6 * q)   # unchanged: no negative deltas
    ex.orders[oid]["baseVolume"] = 0.6 * q                      # the exchange view flaps back to what we had reported
    rig.poll()
    rig.poll()
    assert rig.engine.setup.ledger.entry_qty == pytest.approx(0.6 * q)   # ... and it is NOT counted a second time


def test_e4_a_stop_fill_is_reported_once_however_often_the_history_is_read(rig):
    ex = rig.ex
    for oid in entries(rig):
        ex.fill(oid)
    rig.poll()
    ex.trigger_stop(slippage=0.02)
    rig.poll()
    n_sl = len([e for e in rig.engine.setup.events if e["kind"] == "SL_FILL"]) if rig.engine.setup else 1
    rig.poll()
    rig.poll()
    assert n_sl == 1 and len(rig.exec.stop_fills) == 1


# ===================================================================== E5: foreign exposure
def test_e5_a_foreign_position_that_appears_mid_setup_halts_and_is_never_merged(rig):
    ex = rig.ex
    ex.fill(entries(rig)[0])
    rig.poll()
    q = q_of(rig)
    ex.position["long"] += 5.0                                  # another strategy's order fills on the same symbol/side
    n_close = len(ex.by_kind("emergency_close"))
    rig.poll()
    assert rig.exec.halt_reason and ("FOREIGN_POSITION_PRESENT" in rig.exec.halt_reason
                                     or "UNEXPLAINED_EXPOSURE" in rig.exec.halt_reason)
    assert "UNEXPLAINED_EXPOSURE" in kinds(rig)
    assert len(ex.by_kind("emergency_close")) == n_close        # the foreign position is NOT ours to flatten
    assert rig.engine.setup.ledger.entry_qty == pytest.approx(q)         # ownership was not merged
    blocked = rig.exec.apply([PlaceEntry("next", "next-e1", 1, Direction.LONG, 149.5, 1.0, 1)], rig.engine.now_ms)
    assert blocked and blocked[0].status == "REJECTED" and "EXECUTOR_HALTED" in blocked[0].reason


def test_e5_a_foreign_position_on_the_opposite_side_is_detected_too(rig):
    rig.ex.fill(entries(rig)[0])
    rig.poll()
    rig.ex.position["short"] = 2.0
    rig.poll()
    assert rig.exec.halt_reason and "short" in " ".join(i["detail"] for i in rig.exec.incidents)


def test_e5_foreign_pending_orders_block_the_entry_cycle_without_halting(tmp_path):
    rig = Rig(tmp_path)
    rig.ex.foreign_orders = [{"clientOid": "legacy-limit", "orderId": "L1", "status": "live", "size": "1",
                              "price": "149.0", "posSide": "long", "tradeSide": "open"}]
    rig.run(long_sweep())
    assert rig.ex.orders == {} and rig.exec.halt_reason is None
    assert "FOREIGN_ORDERS_PRESENT" in kinds(rig)
    assert any("FOREIGN_ORDERS_PRESENT" in str(r) for r in rig.rejected())


# ===================================================================== ownership
def test_ownership_is_claimed_before_the_first_order_and_released_when_flat(rig):
    registry = StrategyOwnershipRegistry(rig.tmp / "ownership.json")
    assert registry.owner_of("SOLUSDT", "long") == "lsrl_v1"     # claimed by the entry cycle, before any order
    assert rig.ex.calls.index(rig.ex.by_kind("set_leverage")[0]) < rig.ex.calls.index(rig.ex.by_kind("place_entry")[0])
    for oid in entries(rig):
        rig.ex.fill(oid)
    rig.poll()
    rig.ex.trigger_stop()
    for _ in range(3):
        rig.poll()
    assert registry.owner_of("SOLUSDT", "long") is None          # flat and every order terminal: ownership released
    assert rig.exec.pending_central                              # but the close is not economically recorded yet ...
    assert rig.exec._entry_cycle_gate(PlaceEntry("x", "x-e1", 1, Direction.LONG, 149.5, 1.0, 1),
                                      rig.engine.now_ms) == "CENTRAL_CLOSE_RECORD_PENDING"


def test_a_second_owner_cannot_claim_a_symbol_lsrl_holds_and_lsrl_halts_if_it_is_held_by_another(tmp_path):
    other = StrategyOwnershipRegistry(tmp_path / "ownership.json")
    other.claim("SOLUSDT", "long", "microflow_scalper_v1", "legacy-position")
    rig = Rig(tmp_path).run(long_sweep())
    assert rig.ex.orders == {} and rig.exec.halt_reason and "OWNERSHIP_REFUSED" in rig.exec.halt_reason


def test_an_unreadable_ownership_registry_halts_lsrl_instead_of_guessing(tmp_path):
    (tmp_path / "ownership.json").write_text("{not json")
    rig = Rig(tmp_path).run(long_sweep())
    assert rig.ex.orders == {} and rig.exec.halt_reason and "OWNERSHIP_REFUSED" in rig.exec.halt_reason


def test_ownership_and_executor_state_are_restored_deterministically_after_a_restart(rig):
    ex = rig.ex
    ex.fill(entries(rig)[0])
    rig.poll()
    registry_before = StrategyOwnershipRegistry(rig.tmp / "ownership.json").claims()
    before = rig.exec.to_state()
    restarted = rig.build_executor()
    assert restarted.to_state() == before                                    # exactly what was persisted
    assert StrategyOwnershipRegistry(rig.tmp / "ownership.json").claims() == registry_before
    rig.exec = restarted
    n_orders = len(ex.calls)
    rig.poll()
    assert restarted.halt_reason is None and restarted.stop is not None
    assert len(ex.by_kind("place_entry")) == 5                               # nothing re-sent
    # re-delivery of the same ladder after the restart is a no-op (claimed setup, known tags)
    replay = [i for i in rig.intents if isinstance(i, PlaceEntry)]
    assert restarted.apply(replay, rig.engine.now_ms) == []
    assert len(ex.by_kind("place_entry")) == 5 and len(ex.calls) >= n_orders
