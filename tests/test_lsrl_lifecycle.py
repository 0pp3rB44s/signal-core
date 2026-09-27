"""LSRL_V1: ladder fills, expiry / no chase, TP + stop lifecycle, daily -2R stop, restart."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from lsrl.core.config import DEFAULT_CONFIG as CFG
from lsrl.core.gates import RiskDecision
from lsrl.core.risk_state import DAY_MS
from lsrl.core.types import (CancelOrder, FillEvent, Intent, NavEvent, OrderEvent, PlaceEntry, SetStop,
                             SetTargets, TimerTick)
from tests.lsrl_fixtures import (RULES, S, Harness, ack_all, bar, fill_entry, feed_quiet, flat_bars,
                                 long_sweep, quiet_book, shift, MIN)

# scenario stop (structural): extreme 149.20 - 0.15 x ATR1m at reclaim, ATR1m = (13*0.4 + 0.85)/14 = 0.43214
# -> 149.135179, rounded AWAY (down) to the real 0.001 tick -> 149.135
STOP = 149.135


def accepted(**kw) -> Harness:
    h = Harness(**kw.pop("harness", {})).run(long_sweep(**kw))
    assert h.engine.setup is not None, h.rejected()
    ack_all(h, h.engine.now_ms + 500)
    return h


def fill(h: Harness, kind: str, price: float, qty: float, *, fee: float = 0.0, t: int | None = None, rung=None):
    s = h.engine.setup
    return h.feed(FillEvent(t_ms=t or h.engine.now_ms + 1_000, setup_id=s.setup_id, tag=f"t-{kind}", kind=kind,
                            price=price, qty=qty, fee_usdt=fee, rung=rung))


def fill_all_rungs(h: Harness, *, fee_rate: float = 0.0) -> None:
    for i in range(1, 6):
        r = h.engine.setup.rungs[i]
        fill_entry(h, i, h.engine.now_ms + 1_000, fee=r.qty * r.price * fee_rate)


def types_of(intents) -> set[str]:
    return {type(i).__name__ for i in intents}


def confirm_cancels(h: Harness, *, skip: tuple[str, ...] = ()) -> None:
    """Adapter-style confirmation that every requested entry cancel is terminal on the exchange."""
    s = h.engine.setup
    assert s is not None
    for rung in list(s.pending_cancel_rungs()):
        if rung.tag.rsplit("-", 1)[1] in skip:
            continue
        h.feed(OrderEvent(t_ms=h.engine.now_ms + 100, setup_id=s.setup_id, tag=rung.tag, status="CANCELLED"))


# ------------------------------------------------------------------ partial fill / expiry / no chase
def test_partial_ladder_fill_protects_the_position_and_expiry_cancels_the_rest_without_chasing():
    h = accepted()
    placed = len(h.intents_of(PlaceEntry))
    fill_entry(h, 1, h.engine.now_ms + 1_000)
    fill_entry(h, 2, h.engine.now_ms + 1_000)
    stops = h.intents_of(SetStop)
    assert [round(x.qty, 1) for x in stops] == [3.7, 7.4] and {x.price for x in stops} == {STOP}
    slices = {t.level: t.qty for t in h.intents_of(SetTargets)[-1].targets}
    assert slices == {"TP1": pytest.approx(2.2), "TP2": pytest.approx(2.9), "TP3": pytest.approx(2.3)}
    expires = h.engine.setup.entry_expires_ms
    feed_quiet(h, h.engine.now_ms + 5_000, expires + 20_000, mid=149.70)
    cancelled = {c.tag.rsplit("-", 1)[1] for c in h.intents_of(CancelOrder)}
    assert cancelled == {"e3", "e4", "e5"}
    assert len(h.intents_of(PlaceEntry)) == placed                     # nothing new placed after expiry
    assert h.engine.setup.state == "IN_POSITION" and h.engine.setup.ledger.entry_qty == pytest.approx(7.4)


def test_entry_expiry_is_fifteen_minutes_after_the_ladder_is_placed():
    h = accepted()
    placed_at = next(r.t_ms for r in h.kinds("setup_accepted"))
    assert h.engine.setup.entry_expires_ms - placed_at == 900_000
    assert all(i.expires_at_ms == h.engine.setup.entry_expires_ms for i in h.intents_of(PlaceEntry))


def test_no_fill_expiry_cancels_the_whole_ladder_and_closes_the_setup_without_a_position():
    h = accepted()
    feed_quiet(h, h.engine.now_ms + 5_000, h.engine.setup.entry_expires_ms + 20_000, mid=149.70)
    assert len(h.intents_of(CancelOrder)) == 5
    assert h.engine.setup is not None                                  # cancels requested, NOT yet confirmed
    confirm_cancels(h)
    assert h.engine.setup is None
    closed = h.kinds("setup_closed")[-1].data
    assert closed["close_reason"] == "ENTRY_EXPIRED_NO_FILL" and closed["realized_r"] == 0.0
    assert h.engine.daily.realized_r == 0.0


# ------------------------------------------------------------------ cancel / fill race (no orphan exposure)
def test_the_setup_stays_open_until_every_cancel_is_confirmed_terminal():
    h = accepted()
    feed_quiet(h, h.engine.now_ms + 5_000, h.engine.setup.entry_expires_ms + 20_000, mid=149.70)
    assert {r.status for r in h.engine.setup.rungs.values()} == {"CANCEL_REQUESTED"}
    confirm_cancels(h, skip=("e5",))                                   # one order not yet confirmed
    assert h.engine.setup is not None and [r.rung for r in h.engine.setup.pending_cancel_rungs()] == [5]
    confirm_cancels(h)
    assert h.engine.setup is None and h.kinds("setup_closed")[-1].data["close_reason"] == "ENTRY_EXPIRED_NO_FILL"


def test_a_rung_that_fills_while_its_cancel_is_pending_is_adopted_and_protected():
    """The exchange filled E2 in the race with the expiry cancel. The old engine had already closed the
    setup, so the fill was an unknown-setup anomaly: an orphan position with no stop."""
    h = accepted()
    feed_quiet(h, h.engine.now_ms + 5_000, h.engine.setup.entry_expires_ms + 20_000, mid=149.70)
    assert h.engine.setup is not None
    n_stops, n_targets = len(h.intents_of(SetStop)), len(h.intents_of(SetTargets))
    fill_entry(h, 2, h.engine.now_ms + 1_000)                          # the late fill
    s = h.engine.setup
    assert s is not None and s.state == "IN_POSITION" and s.ledger.entry_qty == pytest.approx(s.rungs[2].qty)
    assert not [r for r in h.records if r.kind == "anomaly" and r.data["reason"] == "FILL_FOR_UNKNOWN_SETUP"]
    stop = h.intents_of(SetStop)[n_stops]                              # protection established immediately
    assert stop.qty == pytest.approx(s.rungs[2].qty) and stop.price == s.stop_price
    assert len(h.intents_of(SetTargets)) == n_targets + 1
    confirm_cancels(h)
    assert h.engine.setup is not None and h.engine.setup.state == "IN_POSITION"     # NOT closed as "no fill"


def test_a_late_fill_after_the_tp1_cancel_request_resizes_stop_and_targets():
    h = accepted()
    fill_entry(h, 1, h.engine.now_ms + 1_000)
    fill_entry(h, 2, h.engine.now_ms + 1_000)
    fill(h, "TP1", 150.30, h.engine.setup.tp_orders["TP1"]["qty"])
    assert {r.status for r in h.engine.setup.rungs.values() if r.rung >= 3} == {"CANCEL_REQUESTED"}
    before = h.engine.setup.ledger.entry_qty
    fill_entry(h, 3, h.engine.now_ms + 1_000)                          # E3 filled in the race with the TP1 cancel
    s = h.engine.setup
    assert s.ledger.entry_qty == pytest.approx(before + s.rungs[3].qty)
    assert h.intents_of(SetStop)[-1].qty == pytest.approx(s.ledger.remaining_qty)
    assert not [r for r in h.records if r.kind == "anomaly" and r.data["reason"] == "FILL_FOR_UNKNOWN_SETUP"]


def test_engine_only_ever_emits_limit_style_intents_never_a_market_order():
    h = accepted()
    fill_all_rungs(h)
    fill(h, "TP1", 150.30, 5.5)
    fill(h, "TP2", 150.70, 7.4)
    fill(h, "TP3", 151.10, 5.6)
    assert types_of(h.intents) <= {"PlaceEntry", "CancelOrder", "SetStop", "SetTargets"}
    assert set(Intent.__args__) == {PlaceEntry, CancelOrder, SetStop, SetTargets}
    assert all(i.price > 0 and i.expires_at_ms > 0 for i in h.intents_of(PlaceEntry))


# ------------------------------------------------------------------ TP / stop lifecycle
def test_tp_lifecycle_tp1_break_even_then_tp2_stop_to_tp1_then_tp3_flat():
    h = accepted()
    fill_all_rungs(h, fee_rate=0.0002)
    entry_fees = h.engine.setup.ledger.entry_fees
    n_stops = len(h.intents_of(SetStop))

    fill(h, "TP1", 150.30, 5.5, fee=5.5 * 150.30 * 0.0002)
    be = h.intents_of(SetStop)[n_stops]
    led = h.engine.setup.ledger
    remaining = 18.5 - 5.5
    costs = entry_fees + 5.5 * 150.30 * 0.0002 + remaining * 149.40 * 0.0006 + remaining * 149.40 * 2e-4
    assert be.price == pytest.approx(round(-(-((149.40 + costs / remaining) * 1000) // 1) / 1000, 3))   # ceil to the tick
    assert be.price > 149.40 and be.qty == pytest.approx(remaining) and be.reason == "TP1_COST_ADJUSTED_BE"
    assert led.remaining_qty == pytest.approx(remaining)

    fill(h, "TP2", 150.70, 7.4)
    to_tp1 = h.intents_of(SetStop)[n_stops + 1]
    assert to_tp1.price == 150.30 and to_tp1.qty == pytest.approx(5.6)

    fill(h, "TP3", 151.10, 5.6)
    assert h.engine.setup is None
    closed = h.kinds("setup_closed")[-1].data
    assert closed["close_reason"] == "TP3_FLAT" and closed["realized_net_usdt"] > 0 and closed["realized_r"] > 0
    assert h.intents_of(SetStop)[-1].qty == 0.0                          # protective stop removed
    assert h.intents_of(SetTargets)[-1].targets == ()                    # resting targets removed


def test_stop_is_not_moved_before_tp1_fills():
    h = accepted()
    fill_all_rungs(h)
    assert {s.price for s in h.intents_of(SetStop)} == {STOP}


def test_tp1_fill_cancels_the_remaining_unfilled_entry_rungs():
    h = accepted()
    fill_entry(h, 1, h.engine.now_ms + 1_000)
    fill(h, "TP1", 150.30, 1.1)
    reasons = {c.reason for c in h.intents_of(CancelOrder)}
    assert reasons == {"ENTRIES_CANCELLED_ON_TP1"}
    assert {c.tag.rsplit("-", 1)[1] for c in h.intents_of(CancelOrder)} == {"e2", "e3", "e4", "e5"}


def test_stop_loss_closes_the_position_at_minus_one_r_before_fees():
    h = accepted()
    fill_all_rungs(h)
    fill(h, "SL", STOP, 18.5)
    closed = h.kinds("setup_closed")[-1].data
    assert closed["close_reason"] == "STOP_LOSS"
    assert closed["realized_r"] == pytest.approx(-1.0, abs=1e-6)
    assert h.engine.setup is None and h.intents_of(SetStop)[-1].qty == 0.0
    assert h.engine.daily.realized_r == pytest.approx(-1.0, abs=1e-6)


def test_actual_slippage_and_fees_are_recorded_against_the_plan():
    h = accepted()
    fill_entry(h, 1, h.engine.now_ms + 1_000, price=149.59, fee=0.11)          # price improvement
    for i in range(2, 6):
        fill_entry(h, i, h.engine.now_ms + 1_000)
    fill(h, "SL", 149.10, 18.5, fee=1.6)                                        # slipped through the stop
    rec = h.kinds("setup_closed")[-1].data
    assert rec["actual"]["entry_slippage_usdt"] == pytest.approx((149.59 - 149.60) * 3.7)
    assert rec["actual"]["stop_slippage_usdt"] == pytest.approx((STOP - 149.10) * 18.5)
    assert rec["actual"]["entry_fees_usdt"] == pytest.approx(0.11) and rec["actual"]["exit_fees_usdt"] == pytest.approx(1.6)


# ------------------------------------------------------------------ daily -2R stop
def _chain(h: Harness, offset_ms: int) -> None:
    """Run another full sweep `offset_ms` after the first, on a gap-free bar tape."""
    events = shift(long_sweep(warm=False), offset_ms) + flat_bars(h, S + offset_ms)
    events.append(NavEvent(t_ms=S + offset_ms - 100_000, nav_usdt=1_000.0))
    h.run(sorted(events, key=lambda e: e.t_ms))


def _stop_out(h: Harness) -> None:
    ack_all(h, h.engine.now_ms + 500)
    fill_all_rungs(h)
    fill(h, "SL", h.engine.setup.stop_price, h.engine.setup.ledger.remaining_qty)


def test_two_r_of_realized_loss_blocks_new_setups_until_the_next_utc_day():
    h = accepted()
    _stop_out(h)                                                      # -1R
    _chain(h, 30 * MIN)
    assert h.engine.setup is not None
    _stop_out(h)                                                      # -2R  -> daily stop
    assert h.engine.daily.realized_r == pytest.approx(-2.0, abs=1e-6)
    _chain(h, 60 * MIN)
    assert h.engine.setup is None and "DAILY_STOP_ACTIVE" in h.rejected()
    next_day = ((S // DAY_MS) + 1) * DAY_MS + 40 * MIN - S
    _chain(h, next_day)
    assert h.engine.setup is not None                                 # new UTC day: trading allowed again


def test_minus_one_r_does_not_trigger_the_daily_stop():
    h = accepted()
    _stop_out(h)
    _chain(h, 30 * MIN)
    assert h.engine.setup is not None and "DAILY_STOP_ACTIVE" not in h.rejected()


def test_daily_stop_never_interferes_with_managing_an_existing_position():
    h = accepted()
    fill_all_rungs(h)
    h.engine.daily.blocked_day = h.engine.daily.day = (S // DAY_MS)   # stop active while in a position
    fill(h, "TP1", 150.30, 5.5)
    assert h.intents_of(SetStop)[-1].reason == "TP1_COST_ADJUSTED_BE"
    fill(h, "TP2", 150.70, 7.4)
    assert h.intents_of(SetStop)[-1].reason == "TP2_STOP_TO_TP1"


# ------------------------------------------------------------------ duplicates / one at a time
def test_the_same_sweep_never_creates_a_second_setup():
    first = Harness().run(long_sweep())
    second = Harness()
    second.engine.seen_setup_ids = list(first.engine.seen_setup_ids)
    second.run(long_sweep())
    assert second.engine.setup is None and second.intents == []
    assert "DUPLICATE_SETUP" in second.rejected()


def test_only_one_lsrl_setup_is_active_at_a_time():
    h = Harness().run(long_sweep(extra_bid_pools=((149.10, 4, 40.0),), sweep_low=148.90))
    assert h.engine.setup is not None
    assert h.rejected().count("EXISTING_LSRL_SETUP_ACTIVE") == 1
    assert len(h.kinds("setup_accepted")) == 1


# ------------------------------------------------------------------ restart / reconciliation
def test_engine_state_survives_a_restart_through_a_json_snapshot():
    a = accepted()
    fill_entry(a, 1, a.engine.now_ms + 1_000)
    fill_entry(a, 2, a.engine.now_ms + 1_000)
    state = json.loads(json.dumps(a.engine.snapshot()))
    b = Harness()
    b.engine.restore(state)
    t = a.engine.now_ms + 2_000
    for h in (a, b):                                                # fresh liquidity data after the restart
        h.feed(quiet_book(t, 149.70))
    for h in (a, b):
        fill_entry(h, 3, t + 1_000)
        fill(h, "TP1", 150.30, 3.3, t=t + 2_000)
    assert [i for i in b.intents] == [i for i in a.intents[-len(b.intents):]]
    assert b.engine.setup.to_state() == a.engine.setup.to_state()
    assert b.engine.snapshot() == a.engine.snapshot()


def test_a_snapshot_from_a_different_frozen_configuration_is_refused():
    a = accepted()
    other = Harness(cfg=replace(CFG, sl_atr1m_mult=0.5))
    with pytest.raises(ValueError):
        other.engine.restore(a.engine.snapshot())


def test_stale_liquidity_data_cancels_a_pending_ladder():
    h = accepted()
    h.feed(TimerTick(t_ms=h.engine.now_ms + 40_000))                   # >30s with no depth snapshot
    assert {c.reason for c in h.intents_of(CancelOrder)} == {"LIQUIDITY_DATA_STALE"}
    confirm_cancels(h)
    assert h.engine.setup is None
    assert h.kinds("setup_closed")[-1].data["close_reason"] == "LIQUIDITY_DATA_STALE"


# ------------------------------------------------------------------ adapter-level rejections
def test_clock_gate_rejection_of_every_entry_rejects_the_setup():
    h = Harness().run(long_sweep())
    s = h.engine.setup
    for rung in list(s.rungs.values()):
        h.feed(OrderEvent(t_ms=h.engine.now_ms + 100, setup_id=s.setup_id, tag=rung.tag, status="REJECTED",
                          reason="CLOCK_GATE"))
    assert h.engine.setup is None
    rec = h.kinds("setup_rejected")[-1].data
    assert rec["reason"] == "ENTRY_ORDERS_REJECTED:CLOCK_GATE" and rec["stage"] == "EXECUTION"


def test_a_partly_rejected_ladder_keeps_working_with_the_accepted_rungs():
    h = Harness().run(long_sweep())
    s = h.engine.setup
    h.feed(OrderEvent(t_ms=h.engine.now_ms + 100, setup_id=s.setup_id, tag=s.rungs[1].tag, status="REJECTED",
                      reason="POST_ONLY_WOULD_CROSS"))
    ack_all(h, h.engine.now_ms + 200)
    assert h.engine.setup is not None and h.engine.setup.rungs[1].status == "REJECTED"
    assert len(h.engine.setup.live_rungs()) == 4


# ------------------------------------------------------------------ central risk gate
class _Gate:
    def __init__(self, decision=None, error=None):
        self.decision, self.error, self.calls = decision, error, []

    def check(self, proposal):
        self.calls.append(proposal)
        if self.error:
            raise self.error
        return self.decision


def test_risk_manager_rejection_blocks_the_entry_and_is_persisted():
    gate = _Gate(RiskDecision(False, ("kill-switch: daily defensive mode active",), None, "RiskManager"))
    h = Harness(risk_gate=gate).run(long_sweep())
    assert h.engine.setup is None and h.intents == [] and "RISK_MANAGER_REJECTED" in h.rejected()
    rec = h.kinds("setup_rejected")[-1].data
    assert rec["risk_gate"]["allowed"] is False and rec["risk_gate"]["source"] == "RiskManager"
    assert gate.calls and gate.calls[0].planned_loss_usdt <= gate.calls[0].nav_usdt * 0.005 + 1e-9


def test_risk_gate_failure_fails_closed():
    h = Harness(risk_gate=_Gate(error=RuntimeError("boom"))).run(long_sweep())
    assert h.engine.setup is None and h.intents == [] and "RISK_MANAGER_REJECTED" in h.rejected()


def test_central_gate_can_only_reduce_risk_never_raise_it():
    base = Harness().run(long_sweep()).engine.setup.sizing["total_qty"]
    cut = Harness(risk_gate=_Gate(RiskDecision(True, (), 0.0025, "RiskManager"))).run(long_sweep())
    assert cut.engine.setup.sizing["total_qty"] < base
    assert cut.engine.setup.sizing["planned_loss_usdt"] <= 1_000.0 * 0.0025 + 1e-9
    up = Harness(risk_gate=_Gate(RiskDecision(True, (), 0.02, "RiskManager"))).run(long_sweep())
    assert up.engine.setup.sizing["total_qty"] == base


# ------------------------------------------------------------------ data-integrity guards
def test_a_missing_one_minute_bar_invalidates_a_pending_reclaim():
    no_reclaim = (149.50, 149.55, 149.45, 149.50)
    h = Harness().run(long_sweep(sweep_close=149.50, extra_bars=(no_reclaim,)))
    assert h.engine.setup is not None                               # still waiting for a reclaim
    skipped = S + 3 * MIN                                           # bar S+2min is missing
    h.feed(bar(skipped, 149.5, 149.9, 149.4, 149.8))
    assert h.engine.setup is None and "BAR_FEED_GAP" in h.rejected()


def test_out_of_order_market_data_is_ignored_and_recorded():
    h = Harness().run(long_sweep())
    before = h.engine.snapshot()
    h.feed(quiet_book(S - 500_000))
    assert h.kinds("anomaly")[-1].data["reason"] == "OUT_OF_ORDER_MARKET_EVENT"
    assert h.engine.snapshot()["now_ms"] == before["now_ms"]


def test_every_required_observability_field_is_persisted_for_accepted_and_closed_setups():
    h = accepted()
    fill_all_rungs(h)
    fill(h, "SL", STOP, 18.5, fee=1.0)
    accepted_rec = h.kinds("setup_accepted")[-1].data
    closed_rec = h.kinds("setup_closed")[-1].data
    required = {"setup_id", "symbol", "direction", "pool_frozen", "pool_strength", "pool_first_seen_ms",
                "pool_age_s_at_freeze", "pool_depth_series", "sweep_t_ms", "sweep_extreme", "consumption",
                "reclaim_t_ms", "atr15", "atr1m", "geometry", "targets", "expected_rr_tp2", "spread_bps",
                "sizing", "risk_gate", "reason", "stage", "rungs"}
    assert required <= set(accepted_rec)
    assert accepted_rec["geometry"]["rungs"] and accepted_rec["geometry"]["stop"]
    assert {"est_entry_fees_usdt", "est_stop_exit_fee_usdt", "est_stop_slippage_usdt"} <= set(accepted_rec["sizing"])
    assert required <= set(closed_rec) | {"stage"}
    assert {"actual", "lifecycle_events", "close_reason", "realized_net_usdt", "realized_r"} <= set(closed_rec)
    kinds = [e["kind"] for e in closed_rec["lifecycle_events"]]
    assert "ENTRY_FILL" in kinds and "SL_FILL" in kinds and kinds[-1] == "SETUP_CLOSED"


def test_rejected_setups_are_persisted_with_full_context():
    h = Harness(cfg=replace(CFG, min_rr_to_tp2=6.0)).run(long_sweep())
    rec = h.kinds("setup_rejected")[-1].data
    assert rec["reason"] == "RR_TO_TP2_BELOW_MIN" and rec["geometry"] and rec["targets"] and rec["sizing"]
    assert rec["expected_rr_tp2"] < 6.0 and rec["pool_frozen"] and rec["consumption"]


# ------------------------------------------------------------------ a halted engine places nothing new
def test_a_halt_between_the_sweep_and_the_reclaim_stops_the_ladder_from_being_placed():
    """e.g. the raw recorder failed while a confirmed sweep was waiting for its reclaim bar."""
    h = Harness().run(long_sweep(extra_bars=(), tail_snapshots=0, sweep_close=149.50))   # sweep confirmed, NOT reclaimed
    assert h.engine.setup is not None and h.engine.setup.state == "AWAIT_RECLAIM"
    h.engine.halt("RECORDER_DISK_GUARD: 0.10 GB free < floor 1.0 GB")
    h.feed(bar(S + MIN, 149.55, 149.80, 149.55, 149.75))                                # the reclaim bar arrives
    assert h.engine.setup is None and not h.intents_of(PlaceEntry)
    assert any(str(r.data.get("reason", "")).startswith("ENGINE_HALTED") for r in h.kinds("setup_rejected"))
