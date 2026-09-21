"""BLOCKER 3: entry must be SELF-PROTECTING. Every LSRL quantity the exchange holds converges to a VERIFIED protective stop
or to VERIFIED FLAT, from every fault boundary, and never settles as 'unprotected + the executor gave up'.

Agent C reproduced three paths (X1, X3, X4). Each has its own test; then a fault-injection matrix kills the process,
drops the network and takes the exchange down at every boundary between 'the entry fills' and 'the stop is confirmed'.
Judged ONLY by what the fake EXCHANGE holds afterwards (position and plan orders), never by the executor's beliefs.
"""
from __future__ import annotations

import statistics

import pytest

from lsrl.adapters.live import (ENGINE_ACK_GRACE_MS, EXPOSURE_CRITICAL, EXPOSURE_NONE, EXPOSURE_PROTECTED,
                                FAST_WATCH_MS, LiveExecutorError)
from lsrl.core.config import DEFAULT_CONFIG as CFG
from lsrl.core.engine import LSRLEngine
from lsrl.core.types import PlaceEntry
from tests.lsrl_fixtures import RULES, long_sweep
from tests.test_lsrl_executor_hardening import entries, kinds
from tests.test_lsrl_live_executor import Rig, q_of
from tests.test_lsrl_risk_integration import _isolate  # noqa: F401  (autouse isolation fixture)


class Death(BaseException):
    """A simulated process death. BaseException: nothing in the executor may swallow it."""


class LifeRig(Rig):
    """The executor's sleeps and the exchange clock are ONE fake clock; `drive` is the runtime loop."""

    def build_executor(self):
        from execution.strategy_ownership import StrategyOwnershipRegistry
        from lsrl.adapters.central_ledger import CentralCloseRecorder
        from lsrl.adapters.live import LSRLLiveExecutor
        return LSRLLiveExecutor(
            settings=self.settings, client=self.ex, submitter=self.submitter, auth_store=self.auth, gate=self.gate,
            state_store=self.state, rules=RULES, sleep=lambda s: self.advance(s * 1000),
            ownership=StrategyOwnershipRegistry(self.tmp / "ownership.json"),
            central=CentralCloseRecorder(client=self.ex, dataset_path=str(self.dataset)),
            clock_ms=lambda: self.ex.now_ms)

    def advance(self, ms: float) -> None:
        self.ex.now_ms += int(ms)

    def restart(self) -> None:
        self.exec = self.build_executor()

    def drive(self, seconds: float, dt: float = 1.0, deliver: bool = True, until=None):
        end = self.ex.now_ms + int(seconds * 1000)
        while self.ex.now_ms < end:
            self.ex.now_ms += int(dt * 1000)
            try:
                events = self.exec.poll(self.ex.now_ms)
            except Death:
                self.restart()
                continue
            if deliver:
                for e in events:
                    self.feed(e)
            if until is not None and until(self):
                break
        return self

    def xstate(self) -> str:
        return self.exec.exposure["state"]


@pytest.fixture()
def rig(tmp_path):
    return LifeRig(tmp_path).run(long_sweep())


def frozen_stop(rig) -> float:
    return rig.engine.setup.stop_price


def verdict(rig) -> str:
    """What the EXCHANGE holds: PROTECTED (a stop at the frozen price covering the whole position), FLAT, or UNSAFE."""
    ex = rig.ex
    pos = ex.position["long"]
    if pos <= 1e-9:
        return "FLAT"
    plans = [p for p in ex.plans if "loss" in p["planType"] and p["posSide"] == "long"]
    covered = sum(float(p["size"]) for p in plans)
    if plans and covered >= pos - 1e-9 and all(abs(float(p["triggerPrice"]) - frozen_stop(rig)) < 0.006 for p in plans):
        return "PROTECTED"
    return "UNSAFE"


def safe(rig) -> bool:
    return verdict(rig) in ("PROTECTED", "FLAT") and rig.exec.emergency is None


def converge(rig, seconds: float = 240.0):
    rig.drive(seconds, until=safe)
    assert verdict(rig) in ("PROTECTED", "FLAT"), f"exposure still UNSAFE: {verdict(rig)} state={rig.xstate()}"
    rig.drive(60)                                                # and it stays that way
    assert verdict(rig) in ("PROTECTED", "FLAT")
    return rig


# =========================================================================================== the normal path
def test_a_fill_is_protected_by_the_executor_in_the_same_cycle_and_the_ladder_is_left_alone(rig):
    q = q_of(rig)
    first, rest = entries(rig)[0], entries(rig)[1:]
    rig.ex.fill(first)
    rig.drive(3)
    assert verdict(rig) == "PROTECTED" and rig.xstate() == EXPOSURE_PROTECTED
    assert rig.exec.halt_reason is None                          # a normal fill is not an incident
    assert all(rig.ex.orders[o]["state"] == "live" for o in rest)   # E2..E5 still rest: the frozen ladder is untouched
    assert rig.engine.setup.ledger.entry_qty == pytest.approx(q)


def test_the_engines_stop_after_executor_first_protection_is_adopted_not_placed_twice(rig):
    rig.ex.fill(entries(rig)[0])
    rig.drive(5)
    assert len(rig.ex.by_kind("stop")) == 1                      # cancel-then-place is a window with NO stop: never repeat it
    assert rig.exec.setups[rig.engine.setup.setup_id]["ack_qty"] == pytest.approx(q_of(rig))


def test_a_normal_fill_the_engine_acknowledges_is_never_flattened_by_the_ack_watchdog(rig):
    rig.ex.fill(entries(rig)[0])
    rig.drive(ENGINE_ACK_GRACE_MS / 1000 * 3)
    assert verdict(rig) == "PROTECTED" and "ENGINE_DID_NOT_ACKNOWLEDGE_FILL" not in kinds(rig)


# =========================================================================================== X1
def test_x1_a_fill_the_engine_never_handles_is_protected_by_the_executor_then_flattened(rig):
    """C: 'no stop is ever placed, no incident is raised, the executor is not halted' (position 1.6, plans [], incidents [])."""
    stop = frozen_stop(rig)
    rig.ex.fill(entries(rig)[0])
    rig.drive(4, deliver=False)                                  # the FillEvent is lost: bus drained / engine threw
    plan = rig.ex.stop_plan
    assert plan is not None and plan["price"] == pytest.approx(stop) and plan["size"] == pytest.approx(rig.ex.position["long"])
    assert rig.xstate() == EXPOSURE_PROTECTED
    rig.drive(ENGINE_ACK_GRACE_MS / 1000 + 10, deliver=False)
    assert rig.ex.position["long"] == 0.0                        # verified FLAT, not left running unmanaged
    assert "ENGINE_DID_NOT_ACKNOWLEDGE_FILL" in kinds(rig) and rig.exec.halt_reason
    assert rig.exec.emergency is None and rig.xstate() == EXPOSURE_NONE


def test_x1_the_protective_stop_price_is_recorded_write_ahead_with_the_entry(rig):
    setup = rig.exec.setups[rig.engine.setup.setup_id]
    assert setup["protect_price"] == pytest.approx(frozen_stop(rig))
    assert rig.xstate() == EXPOSURE_NONE and rig.exec.stop is None      # nothing to protect, nothing invented


def test_x1_after_a_restart_the_same_unacknowledged_fill_is_still_found_protected_and_flattened(rig):
    rig.ex.fill(entries(rig)[0])
    rig.drive(2, deliver=False)
    rig.restart()                                                # the persisted state carries the fill AND the missing ack
    rig.engine = LSRLEngine(CFG, RULES, risk_gate=rig.gate)      # the engine restarted with no memory of it at all
    rig.drive(ENGINE_ACK_GRACE_MS / 1000 + 10, deliver=False)
    assert verdict(rig) == "FLAT" and "ENGINE_DID_NOT_ACKNOWLEDGE_FILL" in kinds(rig)


# =========================================================================================== X3
def test_x3_the_emergency_close_never_gives_up_and_the_backoff_is_bounded(rig):
    """C: 'retries 10 times (30 s), then stops; 20 polls after the exchange recovered the position is still open'."""
    ex = rig.ex
    ex.stop_vanishes = True
    ex.fail["close_futures_position_full"] = ConnectionError("exchange down")
    ex.fill(entries(rig)[0])
    rig.drive(900)
    em = rig.exec.emergency
    assert em is not None and em["attempts"] > 10                # the old cap
    assert em["attempts"] < 150                                  # ...and the gap stops growing at 8 polls: bounded, not a hammer
    assert 80 < len(ex.by_kind("emergency_close")) < 150
    assert rig.xstate() == EXPOSURE_CRITICAL and ex.position["long"] > 0
    assert kinds(rig).count("EMERGENCY_CLOSE_FAILED") <= 14      # incidents are throttled, not one per retry
    del ex.fail["close_futures_position_full"]
    ex.stop_vanishes = False
    rig.drive(120, until=lambda r: r.ex.position["long"] == 0.0)
    assert ex.position["long"] == 0.0 and rig.exec.emergency is None
    assert "EXPOSURE_RECOVERED" in kinds(rig) and rig.xstate() == EXPOSURE_NONE


def test_x3_a_critical_state_survives_a_restart_and_keeps_retrying(rig):
    ex = rig.ex
    ex.stop_vanishes = True
    ex.fail["close_futures_position_full"] = ConnectionError("down")
    ex.fill(entries(rig)[0])
    rig.drive(60)
    assert rig.xstate() == EXPOSURE_CRITICAL
    rig.restart()
    assert rig.xstate() == EXPOSURE_CRITICAL and rig.exec.emergency is not None      # durable across the restart
    del ex.fail["close_futures_position_full"]
    rig.drive(60, until=lambda r: r.ex.position["long"] == 0.0)
    assert ex.position["long"] == 0.0 and rig.exec.emergency is None


def test_x3_owner_resume_is_refused_while_exposure_is_unresolved(rig):
    rig.ex.stop_vanishes = True
    rig.ex.fail["close_futures_position_full"] = ConnectionError("down")
    rig.ex.fill(entries(rig)[0])
    rig.drive(30)
    with pytest.raises(LiveExecutorError, match="unresolved"):
        rig.exec.resume()


def test_unproven_protection_cancels_the_rest_of_the_ladder_and_blocks_new_entries(rig):
    ex = rig.ex
    ex.stop_vanishes = True
    ex.fail["close_futures_position_full"] = ConnectionError("down")
    ex.fill(entries(rig)[0])
    rig.drive(10)
    assert rig.exec.halt_reason and "UNPROTECTED_EXPOSURE" in kinds(rig)
    assert all(ex.orders[o]["state"] == "canceled" for o in entries(rig)[1:])      # no ADDITIONAL exposure
    rejected = rig.exec.apply([PlaceEntry("s2", "t", 1, rig.engine.setup.direction, 149.5, 0.1, 1)], rig.ex.now_ms)
    assert rejected and rejected[0].status == "REJECTED"


def test_escalation_itself_cancels_the_resting_rungs_and_halts_independently_of_the_flatten(rig):
    """Blocking ADDITIONAL exposure is part of escalating, not a side effect of the flatten that usually follows."""
    rig.ex.fill(entries(rig)[0])
    rig.drive(3)
    ex = rig.exec
    ex.exposure.update({"state": "UNPROTECTED_EXPOSURE", "hold": "long", "setup_id": rig.engine.setup.setup_id,
                        "qty": 1.6, "escalated": False})
    ex._escalate("STOP_PLACEMENT_FAILED:test", rig.ex.now_ms)
    assert ex.halt_reason == "STOP_PLACEMENT_FAILED:test" and "UNPROTECTED_EXPOSURE" in kinds(rig)
    assert all(rig.ex.orders[o]["state"] == "canceled" for o in entries(rig)[1:])


def test_a_new_entry_is_refused_while_the_exposure_state_is_unresolved_even_before_any_halt(rig):
    rig.exec.exposure["state"] = "UNPROTECTED_EXPOSURE"
    assert rig.exec.halt_reason is None
    out = rig.exec.apply([PlaceEntry("s9", "t9", 1, rig.engine.setup.direction, 149.5, 0.1, 1)], rig.ex.now_ms)
    assert out and out[0].status == "REJECTED" and out[0].reason == "UNPROTECTED_EXPOSURE"
    assert len(rig.ex.by_kind("place_entry")) == 5                        # nothing new reached the exchange


# =========================================================================================== X4 / restart
def test_x4_an_entry_that_fills_while_the_process_is_down_is_found_and_protected_on_restart(rig):
    snapshot = rig.engine.snapshot()
    rig.ex.fill(entries(rig)[0])                                 # fills while nobody is running
    rig.restart()
    rig.engine = LSRLEngine(CFG, RULES, risk_gate=rig.gate)
    rig.engine.restore(snapshot)
    rig.drive(6)
    assert verdict(rig) == "PROTECTED" and rig.xstate() == EXPOSURE_PROTECTED
    assert rig.engine.setup.ledger.entry_qty == pytest.approx(q_of(rig))       # the engine learned of it, exactly once


def test_x4_restart_with_no_fill_disturbs_nothing(rig):
    calls = len(rig.ex.calls)
    rig.restart()
    rig.drive(10)
    assert rig.xstate() == EXPOSURE_NONE and rig.exec.halt_reason is None
    assert not rig.ex.by_kind("cancel") and not rig.ex.by_kind("stop") and len(rig.ex.calls) >= calls


def test_x4_an_ambiguous_order_state_blocks_new_exposure_but_a_visible_fill_is_still_protected(rig):
    rig.ex.fill(entries(rig)[0])
    rig.restart()
    rig.ex.lookup_unknown = True                                 # cannot tell what happened to the resting orders
    rig.drive(4, deliver=False)
    assert rig.exec.halt_reason and "ORDER_STATE_UNKNOWN" in rig.exec.halt_reason
    assert verdict(rig) == "PROTECTED"                           # the exposure is on the exchange: protect it regardless
    rig.ex.lookup_unknown = False
    rig.drive(10)
    assert verdict(rig) == "PROTECTED" and rig.exec.orders[entries(rig)[0]]["reported_qty"] == pytest.approx(q_of(rig))


def test_an_order_accepted_just_before_a_crash_is_adopted_by_its_clientoid_after_restart(tmp_path):
    rig = LifeRig(tmp_path)
    real = rig.ex.place_futures_limit_order

    def accept_then_die(*a, **k):
        real(*a, **k)
        raise Death()                                            # the exchange has the order; the executor never wrote it down
    rig.ex.place_futures_limit_order = accept_then_die
    with pytest.raises(Death):
        rig.run(long_sweep())
    assert rig.ex.orders and not rig.exec.orders                 # the hole this closes
    persisted = rig.state.load({})
    assert any(s["expected_entries"] for s in persisted["setups"].values())     # ...but the intent was written AHEAD
    rig.ex.place_futures_limit_order = real
    rig.restart()
    rig.exec.apply([], rig.ex.now_ms)
    assert set(rig.exec.orders) & set(rig.ex.orders)             # found again by its deterministic clientOid
    assert "ENTRY_ORDER_ADOPTED_AFTER_RESTART" in kinds(rig)
    rig.ex.fill(next(iter(rig.exec.orders)))
    rig.drive(6, deliver=False)
    assert verdict(rig) == "PROTECTED"


def test_lost_executor_state_with_a_registry_claim_and_exposure_is_flattened_not_ignored(rig):
    rig.ex.fill(entries(rig)[0])
    rig.drive(3)
    assert verdict(rig) == "PROTECTED"
    rig.state.save({})                                           # the state file is gone (disk fault / operator error)
    rig.restart()
    assert rig.exec.setups == {}
    rig.drive(30, deliver=False)
    assert verdict(rig) == "FLAT" and "UNPROTECTED_EXPOSURE" in kinds(rig)


def test_a_foreign_position_is_still_never_touched_by_the_protection_machinery(tmp_path):
    rig = LifeRig(tmp_path)
    rig.ex.position["short"] = 3.0                               # somebody else's, no LSRL claim on that side
    rig.run(long_sweep())
    rig.drive(20)
    assert rig.ex.position["short"] == 3.0 and not rig.ex.by_kind("stop") and not rig.ex.by_kind("emergency_close")


# =========================================================================================== the fault-injection matrix
def die_once(obj, name):
    real = getattr(obj, name)
    state = {"armed": True}

    def wrapper(*a, **k):
        if state["armed"]:
            state["armed"] = False
            raise Death()
        return real(*a, **k)
    setattr(obj, name, wrapper)


def die_after(obj, name):
    real = getattr(obj, name)
    state = {"armed": True}

    def wrapper(*a, **k):
        out = real(*a, **k)
        if state["armed"]:
            state["armed"] = False
            raise Death()
        return out
    setattr(obj, name, wrapper)


def fault_before_fill_poll(rig):
    rig.ex.fill(entries(rig)[0])
    rig.advance(20_000)                                          # nobody looked for a long time


def fault_process_death_right_after_the_fill_is_visible(rig):
    die_once(rig.exec, "_exposure_check")                        # the fill was read (and persisted); the process dies now
    rig.ex.fill(entries(rig)[0])


def fault_process_death_before_the_stop_request(rig):
    die_once(rig.ex, "move_futures_stop_loss")
    rig.ex.fill(entries(rig)[0])


def fault_process_death_after_the_stop_request_before_verification(rig):
    die_after(rig.ex, "move_futures_stop_loss")                  # the stop exists on the exchange; nobody verified it
    rig.ex.fill(entries(rig)[0])


def fault_network_loss_during_protection(rig):
    rig.ex.fill(entries(rig)[0])
    rig.ex.fail_counts["move_futures_stop_loss"] = 6
    rig.ex.fail_counts["get_tpsl_orders"] = 4


def fault_total_network_loss_for_a_minute(rig):
    rig.ex.fill(entries(rig)[0])
    rig.ex.outage = True
    rig.drive(60)
    rig.ex.outage = False


def fault_exchange_outage_covering_both_protect_and_flatten(rig):
    rig.ex.stop_vanishes = True
    rig.ex.fail["close_futures_position_full"] = ConnectionError("exchange down")
    rig.ex.fill(entries(rig)[0])
    rig.drive(400)
    rig.ex.stop_vanishes = False
    del rig.ex.fail["close_futures_position_full"]


def fault_positions_unreadable_then_readable(rig):
    rig.ex.fill(entries(rig)[0])
    rig.ex.fail_counts["get_all_positions"] = 8


def fault_orders_unreadable_while_the_position_is_visible(rig):
    rig.ex.fill(entries(rig)[0])
    rig.ex.fail_counts["get_pending_orders"] = 6
    rig.ex.fail_counts["find_order_by_client_oid"] = 6


def fault_restart_in_the_middle_of_a_critical_recovery(rig):
    rig.ex.stop_vanishes = True
    rig.ex.fail["close_futures_position_full"] = ConnectionError("down")
    rig.ex.fill(entries(rig)[0])
    rig.drive(45)
    rig.restart()
    rig.ex.stop_vanishes = False
    del rig.ex.fail["close_futures_position_full"]


def fault_two_rungs_fill_and_the_second_stop_update_dies(rig):
    rig.ex.fill(entries(rig)[0])
    rig.drive(4)
    die_after(rig.ex, "move_futures_stop_loss")                  # the larger stop is placed, the process dies before verifying
    rig.ex.fill(entries(rig)[1])


def fault_stop_service_down_the_position_can_still_be_closed(rig):
    rig.ex.fail_counts["move_futures_stop_loss"] = 40
    rig.ex.fill(entries(rig)[0])


MATRIX = [
    fault_before_fill_poll,
    fault_process_death_right_after_the_fill_is_visible,
    fault_process_death_before_the_stop_request,
    fault_process_death_after_the_stop_request_before_verification,
    fault_network_loss_during_protection,
    fault_total_network_loss_for_a_minute,
    fault_exchange_outage_covering_both_protect_and_flatten,
    fault_positions_unreadable_then_readable,
    fault_orders_unreadable_while_the_position_is_visible,
    fault_restart_in_the_middle_of_a_critical_recovery,
    fault_two_rungs_fill_and_the_second_stop_update_dies,
    fault_stop_service_down_the_position_can_still_be_closed,
]


@pytest.mark.parametrize("fault", MATRIX, ids=lambda f: f.__name__.removeprefix("fault_"))
def test_every_fault_boundary_ends_verified_protected_or_verified_flat(rig, fault):
    fault(rig)
    converge(rig)
    assert rig.exec.emergency is None
    assert rig.xstate() in (EXPOSURE_PROTECTED, EXPOSURE_NONE)


@pytest.mark.parametrize("fault", MATRIX, ids=lambda f: f.__name__.removeprefix("fault_"))
def test_every_fault_boundary_also_converges_when_the_engine_never_learns_of_the_fill(rig, fault):
    fault(rig)
    rig.drive(400, deliver=False, until=safe)
    assert verdict(rig) in ("PROTECTED", "FLAT")
    rig.drive(ENGINE_ACK_GRACE_MS / 1000 + 30, deliver=False)
    assert verdict(rig) in ("PROTECTED", "FLAT") and rig.exec.emergency is None


# =========================================================================================== the entry -> stop window
RTT_MS = 450            # the Runner's measured REST round trip is 340-490 ms (Agent C, 2026-09-21)
SETTLE_MS = 2000        # clients.bitget_tpsl_client.STOP_REPLACEMENT_SETTLE_SECONDS
RUNTIME_POLL_MS = 3000  # LSRLRuntime.poll_interval_s


def latent(rig):
    ex = rig.ex
    for name in ("get_all_positions", "get_pending_orders", "find_order_by_client_oid", "get_tpsl_orders",
                 "get_order_history", "cancel_futures_order", "cancel_all_futures_tpsl_orders",
                 "place_futures_limit_close_order", "close_futures_position_full"):
        real = getattr(ex, name)
        setattr(ex, name, (lambda real: lambda *a, **k: (rig.advance(RTT_MS), real(*a, **k))[1])(real))
    stop = ex.move_futures_stop_loss
    ex.move_futures_stop_loss = lambda *a, **k: (rig.advance(2 * RTT_MS + SETTLE_MS), stop(*a, **k))[1]
    return rig


def window(tmp_path, phase_ms: int, fast: bool) -> int:
    rig = latent(LifeRig(tmp_path / f"w{phase_ms}{fast}").run(long_sweep()))
    rig.exec._fast_watch_enabled = fast
    rig.exec._recovered = True
    t = rig.ex.now_ms
    next_poll = t + RUNTIME_POLL_MS
    fill_at = t + phase_ms
    filled = False
    for _ in range(400):                                         # 250 ms ticks: WS frames reach apply() several times a second
        t += 250
        rig.ex.now_ms = max(rig.ex.now_ms, t)
        if not filled and rig.ex.now_ms >= fill_at:
            rig.ex.fill(entries(rig)[0])
            filled = True
        for e in rig.exec.apply([], rig.ex.now_ms):
            rig.feed(e)
        if rig.ex.now_ms >= next_poll:
            next_poll = rig.ex.now_ms + RUNTIME_POLL_MS
            for e in rig.exec.poll(rig.ex.now_ms):
                rig.feed(e)
        t = rig.ex.now_ms
        if rig.exec.latencies:
            break
    assert rig.exec.latencies, "the stop was never confirmed"
    return rig.exec.latencies[-1]["exchange_fill_to_verified_ms"]


def test_the_entry_to_stop_window_is_measured_and_the_fast_watch_shortens_it(tmp_path, capsys):
    phases = range(0, RUNTIME_POLL_MS, 300)
    fast = [window(tmp_path, p, True) for p in phases]
    slow = [window(tmp_path, p, False) for p in phases]
    with capsys.disabled():
        print(f"\nENTRY_TO_STOP_WINDOW rtt={RTT_MS}ms settle={SETTLE_MS}ms poll={RUNTIME_POLL_MS}ms | "
              f"fast-watch min/median/max = {min(fast)}/{int(statistics.median(fast))}/{max(fast)} ms | "
              f"poll-only min/median/max = {min(slow)}/{int(statistics.median(slow))}/{max(slow)} ms")
    assert max(fast) <= 8_000 and max(slow) <= 11_500           # the budget: discovery + 4 reads + stop call + read-back
    assert max(fast) < max(slow) and statistics.median(fast) < statistics.median(slow)
    assert statistics.median(fast) <= 4_800                     # the immediate path skips two reads before the stop call
