"""PROCESS-DOWN PROTECTION: a passive entry that fills while no process is alive must be stopped BY THE EXCHANGE.

Judged only by what the fake EXCHANGE holds (position and plan orders), never by the executor's beliefs, under every way
the exchange might materialise Bitget's order-level `presetStopLossPrice` (the documentation does not say which):
  PLAN_PER_ORDER  a loss_plan per entry order      POSITION_LEVEL  one whole-position pos_loss
  NONE            the exchange ignores the preset (an attestation that lied)      REJECT   the order is refused
"""
from __future__ import annotations

import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from clients.bitget_order_client import BitgetOrderClientMixin  # noqa: F401  (the real client body builder is exercised below)
from execution.maker_entry import submit_persistent_maker_entry
from lsrl.adapters.live import LiveNotArmed, LSRLLiveExecutor
from lsrl.adapters.process_down import (REQUIRED_CHECKS, AttestationRefused, ProcessDownProtection, load_from_settings,
                                        verify_attestation)
from lsrl.core.config import DEFAULT_CONFIG as CFG
from lsrl.core.engine import LSRLEngine
from lsrl.core.types import Direction, FillEvent, SetStop
from tests.lsrl_fake_exchange import TEST_PROTECTION
from tests.lsrl_fixtures import RULES, long_sweep
from tests.test_bitget_minimum_size import FakeOrderClient
from tests.test_lsrl_central_loss_visibility import read_rows
from tests.test_lsrl_executor_hardening import entries, kinds
from tests.test_lsrl_exposure_recovery import EXPOSURE_NONE, EXPOSURE_PROTECTED, LifeRig, converge, frozen_stop, verdict
from tests.test_lsrl_live_executor import q_of
from tests.test_lsrl_risk_integration import _isolate  # noqa: F401  (autouse isolation fixture)

SHA = "a" * 40
NOW = 1_800_000_000.0


# ==================================================================================== the attestation (pure)
def attestation(tmp_path, **over):
    doc = {"kind": "PRESET_STOP_SEMANTICS", "schema": 1, "decision": "PASS", "environment": "BITGET_DEMO",
           "code_sha": SHA, "created_at": NOW - 60, "expires_at": NOW + 3600, "observed_model": "PLAN_LEVEL",
           "checks": [{"id": c, "passed": True, "observed": {}} for c in REQUIRED_CHECKS]}
    doc.update(over)
    path = tmp_path / "attestation.json"
    raw = json.dumps(doc).encode()
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def test_a_complete_attestation_bound_to_this_release_and_pinned_outside_the_file_is_accepted(tmp_path):
    path, digest = attestation(tmp_path)
    got = verify_attestation(path, digest, code_sha=SHA, now=NOW)
    assert got.observed_model == "PLAN_LEVEL" and got.environment == "BITGET_DEMO" and got.digest == digest


@pytest.mark.parametrize("over,reason", [
    ({"decision": "FAIL"}, "DECISION_NOT_PASS"),
    ({"kind": "OTHER"}, "KIND_OR_SCHEMA"),
    ({"schema": 2}, "KIND_OR_SCHEMA"),
    ({"environment": "SOMEWHERE"}, "ENVIRONMENT"),
    ({"observed_model": "GUESS"}, "MODEL_UNKNOWN"),
    ({"code_sha": "b" * 40}, "NOT_BOUND_TO_THIS_RELEASE"),
    ({"expires_at": NOW - 1}, "EXPIRED_OR_UNBOUNDED"),
    ({"expires_at": NOW + 60 * 24 * 3600}, "EXPIRED_OR_UNBOUNDED"),      # a far-future expiry is not accepted
    ({"created_at": NOW + 3600, "expires_at": NOW + 7200}, "EXPIRED_OR_UNBOUNDED"),   # created in the future
    ({"created_at": "x"}, "TIMES_INVALID"),
    ({"expires_at": float("inf")}, "TIMES_INVALID"),
    ({"checks": "nope"}, "CHECKS_MISSING"),
])
def test_every_way_an_attestation_can_be_wrong_refuses(tmp_path, over, reason):
    path, digest = attestation(tmp_path, **over)
    with pytest.raises(AttestationRefused, match=reason):
        verify_attestation(path, digest, code_sha=SHA, now=NOW)


@pytest.mark.parametrize("missing", REQUIRED_CHECKS)
def test_each_required_observation_is_individually_required(tmp_path, missing):
    checks = [{"id": c, "passed": True} for c in REQUIRED_CHECKS if c != missing]
    path, digest = attestation(tmp_path, checks=checks)
    with pytest.raises(AttestationRefused, match="CHECK_MISSING:" + missing):
        verify_attestation(path, digest, code_sha=SHA, now=NOW)


def test_one_failed_observation_refuses_even_when_the_decision_says_pass(tmp_path):
    checks = [{"id": c, "passed": c != "stop_survives_settle"} for c in REQUIRED_CHECKS]
    path, digest = attestation(tmp_path, checks=checks)
    with pytest.raises(AttestationRefused, match="CHECK_FAILED:stop_survives_settle"):
        verify_attestation(path, digest, code_sha=SHA, now=NOW)


def test_an_attestation_cannot_vouch_for_itself_the_digest_is_pinned_outside_the_file(tmp_path):
    path, digest = attestation(tmp_path)
    with pytest.raises(AttestationRefused, match="DIGEST_MISMATCH"):
        verify_attestation(path, "0" * 64, code_sha=SHA, now=NOW)
    for bad in ("", "abc", "g" * 64):
        with pytest.raises(AttestationRefused, match="DIGEST_NOT_PINNED"):
            verify_attestation(path, bad, code_sha=SHA, now=NOW)
    path.write_bytes(path.read_bytes() + b" ")                                  # any edit breaks the pin
    with pytest.raises(AttestationRefused, match="DIGEST_MISMATCH"):
        verify_attestation(path, digest, code_sha=SHA, now=NOW)


@pytest.mark.parametrize("sha", ["UNKNOWN", SHA + "+dirty", "", "A" * 40, "abc123"])
def test_a_dirty_unknown_or_malformed_release_sha_never_verifies(tmp_path, sha):
    path, digest = attestation(tmp_path)
    with pytest.raises(AttestationRefused, match="RELEASE_SHA_NOT_CLEAN_40_HEX"):
        verify_attestation(path, digest, code_sha=sha, now=NOW)


def test_missing_symlinked_and_non_json_files_refuse(tmp_path):
    path, digest = attestation(tmp_path)
    with pytest.raises(AttestationRefused, match="FILE_MISSING"):
        verify_attestation(tmp_path / "nope.json", digest, code_sha=SHA, now=NOW)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(AttestationRefused, match="FILE_MISSING"):
        verify_attestation(link, digest, code_sha=SHA, now=NOW)
    junk = tmp_path / "junk.json"
    junk.write_bytes(b"not json")
    with pytest.raises(AttestationRefused, match="NOT_JSON"):
        verify_attestation(junk, hashlib.sha256(b"not json").hexdigest(), code_sha=SHA, now=NOW)


def test_load_from_settings_refuses_when_nothing_is_configured():
    with pytest.raises(AttestationRefused, match="NOT_CONFIGURED"):
        load_from_settings(SimpleNamespace(), code_sha=SHA)
    with pytest.raises(AttestationRefused, match="NOT_CONFIGURED"):
        load_from_settings(SimpleNamespace(lsrl_process_down_attestation_path="x"), code_sha=SHA)


# ==================================================================================== MICRO_LIVE cannot exist without it
def test_the_executor_cannot_be_constructed_without_a_verified_protection_object(tmp_path):
    rig = LifeRig(tmp_path)
    for bad in (None, "PLAN_LEVEL", object()):
        with pytest.raises(LiveNotArmed, match="PROCESS_DOWN_PROTECTION_NOT_ATTESTED"):
            LSRLLiveExecutor(settings=rig.settings, client=rig.ex, submitter=rig.submitter, auth_store=rig.auth,
                             gate=rig.gate, state_store=rig.state, rules=RULES, process_down=bad)


def test_the_production_runtime_builder_refuses_micro_live_without_the_attestation(tmp_path):
    from lsrl.runtime import build_runtime
    from tests.test_lsrl_feed_and_runtime import MetaClient, settings, spec
    st = settings(lsrl_mode="MICRO_LIVE", lsrl_data_dir=str(tmp_path), forward_paper_only=False, execution_enabled=True,
                  execution_mode="LIVE", lsrl_microlive_armed=True)
    with pytest.raises(AttestationRefused, match="NOT_CONFIGURED"):
        build_runtime(st, client=MetaClient(spec()), execution_service=object(), risk_manager=None)


def test_the_settings_validator_requires_a_pinned_attestation_for_micro_live():
    from tests.test_microflow_leverage_validator import _live
    base = dict(LSRL_ENABLED=True, LSRL_MODE="MICRO_LIVE", LSRL_MICROLIVE_ARMED=True)
    with pytest.raises(ValidationError, match="PROCESS_DOWN_ATTESTATION"):
        _live(**base)
    with pytest.raises(ValidationError, match="PROCESS_DOWN_ATTESTATION"):
        _live(LSRL_PROCESS_DOWN_ATTESTATION_PATH="/x/att.json", LSRL_PROCESS_DOWN_ATTESTATION_SHA256="zz", **base)
    ok = _live(LSRL_PROCESS_DOWN_ATTESTATION_PATH="/x/att.json", LSRL_PROCESS_DOWN_ATTESTATION_SHA256="a" * 64, **base)
    assert ok.lsrl_process_down_attestation_sha256 == "a" * 64


# ==================================================================================== the request itself
def test_the_real_client_puts_the_preset_stop_on_the_order_and_only_when_asked():
    c = FakeOrderClient()
    c.place_futures_limit_order("BTCUSDT", direction="LONG", size=0.0004, price=60000.0, client_oid="a",
                                preset_stop_loss_price=59000.0)
    c.place_futures_limit_order("BTCUSDT", direction="LONG", size=0.0004, price=60000.0, client_oid="b")
    with_stop, without = c.requests[0][2]["body"], c.requests[1][2]["body"]
    assert float(with_stop["presetStopLossPrice"]) == 59000.0 and with_stop["force"] == "post_only"
    assert "presetStopLossPrice" not in without                      # every existing caller sends exactly what it always sent


@pytest.mark.parametrize("direction,stop", [("LONG", 60000.0), ("LONG", 61000.0), ("SHORT", 60000.0), ("SHORT", 59000.0),
                                            ("LONG", 0.0)])
def test_a_stop_on_the_wrong_side_of_the_entry_is_refused_before_the_network(direction, stop):
    c = FakeOrderClient()
    with pytest.raises(ValueError, match="losing side|Invalid|refusing"):
        c.place_futures_limit_order("BTCUSDT", direction=direction, size=0.0004, price=60000.0, client_oid="x",
                                    preset_stop_loss_price=stop)
    assert c.requests == []


def test_the_maker_entry_boundary_forwards_the_preset_only_when_given():
    calls = []

    class Submitter:
        def submit_entry(self, *, place, **kw):
            place("oid")
            return SimpleNamespace(status="ACCEPTED", classification="", order_id="1", client_oid="oid")

    class Client:
        def place_futures_limit_order(self, **kw):
            calls.append(kw)

    plan = SimpleNamespace(symbol="SOLUSDT")
    submit_persistent_maker_entry(submitter=Submitter(), client=Client(), plan=plan, size=1, price=100.0, notional_usdt=100.0)
    submit_persistent_maker_entry(submitter=Submitter(), client=Client(), plan=plan, size=1, price=100.0, notional_usdt=100.0,
                                  preset_stop_loss_price=99.0)
    assert "preset_stop_loss_price" not in calls[0] and calls[1]["preset_stop_loss_price"] == 99.0


# ==================================================================================== every entry carries the frozen stop
@pytest.fixture(params=["PLAN_PER_ORDER", "POSITION_LEVEL"])
def prig(request, tmp_path):
    rig = LifeRig(tmp_path).run(long_sweep())
    rig.ex.preset_model = request.param
    return rig


def test_every_resting_entry_carries_the_frozen_protective_stop(prig):
    stop = frozen_stop(prig)
    resting = [o for o in prig.ex.orders.values() if o["tradeSide"] == "open"]
    assert len(resting) == 5
    assert all(o["presetStopLossPrice"] == pytest.approx(stop) for o in resting)
    setup = prig.exec.setups[prig.engine.setup.setup_id]
    assert setup["protect_price"] == pytest.approx(stop)
    assert all(r["preset_stop"] == pytest.approx(stop) for r in prig.exec.orders.values())


def test_an_entry_that_could_rest_without_its_exchange_side_stop_is_never_placed(tmp_path):
    rig = LifeRig(tmp_path)
    plan_ident = rig.exec._rung_plan

    def no_stop(it):
        plan, ident = plan_ident(it)
        plan.stop_loss = 0.0
        return plan, ident

    with patch.object(rig.exec, "_rung_plan", no_stop):
        rig.run(long_sweep())
    assert rig.ex.orders == {} and any("PRESET_STOP_UNAVAILABLE" in r for r in rig.rejected())


def test_when_the_exchange_refuses_the_preset_no_entry_rests_at_all(tmp_path):
    rig = LifeRig(tmp_path)
    rig.ex.preset_model = "REJECT"
    rig.run(long_sweep())
    assert rig.ex.orders == {} and rig.ex.position == {"long": 0.0, "short": 0.0}


# ==================================================================================== the exchange holds the stop from the fill
def test_a_fill_is_stopped_by_the_exchange_with_no_process_involved(prig):
    """Exchange truth immediately after the fill and BEFORE the executor has done anything at all."""
    prig.ex.fill(entries(prig)[0])
    assert verdict(prig) == "PROTECTED"
    assert prig.ex.by_kind("stop") == [] and prig.ex.by_kind("cancel_stop") == []      # nobody asked for it


def test_the_executor_proves_and_adopts_the_exchange_stop_and_never_replaces_it(prig):
    prig.ex.fill(entries(prig)[0])
    prig.drive(6)
    assert verdict(prig) == "PROTECTED" and prig.xstate() == EXPOSURE_PROTECTED
    assert prig.exec.stop and prig.exec.stop.get("adopted") is True
    assert prig.ex.by_kind("stop") == [] and prig.ex.by_kind("cancel_stop") == []       # a replacement would be a stop-less gap
    assert prig.exec.halt_reason is None and "UNPROTECTED" not in " ".join(kinds(prig))


def test_more_rungs_filling_never_open_a_gap_and_never_cancel_the_stop(prig):
    first, second, third = entries(prig)[:3]
    for oid in (first, second, third):
        prig.ex.fill(oid)
        assert verdict(prig) == "PROTECTED"                     # at every moment, on the exchange
        prig.drive(4)
        assert verdict(prig) == "PROTECTED"
    assert prig.ex.by_kind("cancel_stop") == [] and prig.ex.by_kind("stop") == []
    assert prig.exec.setups[prig.engine.setup.setup_id]["ack_qty"] == pytest.approx(3 * q_of(prig))


def test_a_partial_fill_is_stopped_for_what_has_filled_and_the_rest_keeps_resting(prig):
    oid = entries(prig)[0]
    half = q_of(prig) / 2
    prig.ex.fill(oid, qty=half)
    assert verdict(prig) == "PROTECTED"
    prig.drive(5)
    assert verdict(prig) == "PROTECTED" and prig.ex.orders[oid]["state"] == "partially_filled"
    prig.ex.fill(oid, qty=half)
    prig.drive(5)
    assert verdict(prig) == "PROTECTED" and prig.exec.halt_reason is None


def test_a_later_stop_move_leaves_exactly_one_stop_at_the_new_price(prig):
    prig.ex.fill(entries(prig)[0])
    prig.drive(5)
    setup_id = prig.engine.setup.setup_id
    new = round(frozen_stop(prig) + 0.5, 3)
    prig.exec.apply([SetStop(setup_id, Direction.LONG, new, q_of(prig), "TEST_MOVE")], prig.ex.now_ms)
    losses = [p for p in prig.ex.plans if "loss" in p["planType"]]
    assert len(losses) == 1 and float(losses[0]["triggerPrice"]) == pytest.approx(new)
    if prig.ex.preset_model == "POSITION_LEVEL":
        assert losses[0]["planType"] == "pos_loss"               # moved in place, the way the shared client does it
    prig.drive(5)
    assert prig.xstate() == EXPOSURE_PROTECTED and prig.exec.emergency is None


def test_a_stop_that_fires_is_a_normal_exit_recorded_by_the_executor(prig):
    sid = prig.engine.setup.setup_id
    prig.ex.fill(entries(prig)[0])
    prig.drive(5)
    prig.ex.trigger_stop()
    prig.drive(30)
    assert verdict(prig) == "FLAT" and prig.exec.emergency is None
    assert [v["setup_id"] for v in prig.exec.stop_fills.values()] == [sid]
    assert prig.exec.setups[sid]["finalized"] is True
    assert [r["event_type"] for r in read_rows(prig.dataset)][:1] == ["CLOSE_PROVISIONAL"]     # the loss is visible centrally


# ==================================================================================== the exchange did NOT honour the preset
def test_if_the_exchange_silently_ignores_the_preset_the_executor_still_protects_it_as_soon_as_it_runs(tmp_path):
    rig = LifeRig(tmp_path).run(long_sweep())
    rig.ex.preset_model = "NONE"                                # the attestation lied
    rig.ex.fill(entries(rig)[0])
    assert verdict(rig) == "UNSAFE"                             # honest: nothing protects it until a process looks
    rig.drive(15)
    assert verdict(rig) == "PROTECTED" and rig.ex.by_kind("stop")


# ==================================================================================== THE PROCESS IS DOWN
def down_for(rig, seconds: float) -> None:
    """No executor call at all for `seconds`; only the exchange's own clock moves."""
    rig.ex.now_ms += int(seconds * 1000)


def restart_everything(rig) -> None:
    """A host restart: a new executor from persisted state AND a new engine restored from its snapshot."""
    snapshot = rig.engine.snapshot()
    rig.restart()
    rig.engine = LSRLEngine(CFG, RULES, risk_gate=rig.gate)
    rig.engine.restore(snapshot)


def test_an_entry_fills_while_the_process_is_dead_the_exchange_holds_the_stop_and_the_restart_adopts_it(prig):
    oid = entries(prig)[0]
    down_for(prig, 20)
    prig.ex.fill(oid)
    down_for(prig, 3600)                                        # an hour with nothing running
    assert verdict(prig) == "PROTECTED"                         # bounded: never naked for one second of it
    restart_everything(prig)
    prig.drive(10)
    assert verdict(prig) == "PROTECTED" and prig.xstate() == EXPOSURE_PROTECTED
    assert prig.exec.stop and prig.exec.stop.get("adopted") is True
    assert prig.ex.by_kind("stop") == [] and prig.ex.by_kind("cancel_stop") == []
    assert prig.engine.setup is not None and prig.engine.setup.ledger.entry_qty == pytest.approx(q_of(prig))
    assert prig.ex.by_kind("place_tp")                          # the restarted process manages it again: targets are placed
    assert prig.exec.halt_reason is None


def test_a_kill_between_the_exchange_accepting_an_entry_and_the_executor_recording_it_is_recovered(tmp_path):
    rig = LifeRig(tmp_path).run(long_sweep())
    rig.ex.preset_model = "PLAN_PER_ORDER"
    oid = entries(rig)[0]
    setup = rig.exec.setups[rig.engine.setup.setup_id]
    setup["expected_entries"][oid] = {"tag": "x", "rung": 1, "price": 149.6, "qty": q_of(rig), "direction": "LONG", "t_ms": 1}
    del rig.exec.orders[oid]                                    # the process died before it wrote the order down
    rig.ex.fill(oid)
    assert verdict(rig) == "PROTECTED"                          # the exchange already holds the stop
    rig.restart()
    rig.drive(10)
    assert verdict(rig) == "PROTECTED" and rig.exec.exposure["state"] in (EXPOSURE_PROTECTED, EXPOSURE_NONE)


def test_the_stop_fires_while_the_process_is_dead_and_the_restart_still_learns_of_it(prig):
    """Without this the outage's LOSS would be invisible: an unexplained flat position, no engine event, no central record."""
    q, sid = q_of(prig), prig.engine.setup.setup_id
    prig.ex.fill(entries(prig)[0])
    prig.drive(5)                                               # protected, adopted, targets placed
    down_for(prig, 30)
    prig.ex.trigger_stop()                                      # the exchange stops the position out on its own
    down_for(prig, 600)
    assert verdict(prig) == "FLAT"
    restart_everything(prig)
    prig.drive(40)
    assert verdict(prig) == "FLAT" and prig.exec.emergency is None
    assert "LEDGER_EXCEEDS_EXCHANGE" not in kinds(prig) and "UNEXPLAINED_EXPOSURE" not in kinds(prig)
    assert sum(v["qty"] for v in prig.exec.stop_fills.values()) == pytest.approx(q)
    assert prig.exec._ledger("long") == pytest.approx(0.0)
    assert [v["setup_id"] for v in prig.exec.stop_fills.values()] == [sid]
    assert prig.exec.setups[sid]["finalized"] is True
    assert [r["event_type"] for r in read_rows(prig.dataset)][:1] == ["CLOSE_PROVISIONAL"]     # the OUTAGE'S loss is on the books


def test_the_stop_fires_before_the_first_look_and_the_setup_is_closed_and_recorded(tmp_path):
    """Fill AND stop-out both happen with no process; the executor's first poll after restart finds a flat position."""
    rig = LifeRig(tmp_path).run(long_sweep())
    rig.ex.preset_model = "PLAN_PER_ORDER"
    q = q_of(rig)
    rig.ex.fill(entries(rig)[0])
    down_for(rig, 90)
    rig.ex.trigger_stop()
    restart_everything(rig)
    rig.drive(60)
    assert verdict(rig) == "FLAT" and rig.exec.emergency is None and rig.exec.halt_reason is None
    assert sum(v["qty"] for v in rig.exec.stop_fills.values()) == pytest.approx(q)
    assert not [o for o in rig.ex.orders.values() if o["state"] in ("live", "partially_filled")]   # the ladder is gone


def test_with_the_state_file_lost_a_position_that_the_exchange_stopped_ends_verified_flat(prig):
    """State is lost entirely: the ownership registry says the side is ours but nothing says what the stop should be. The
    executor does not trust an unexplained stop: it flattens (verified) rather than manage what it cannot account for."""
    prig.ex.fill(entries(prig)[0])
    down_for(prig, 120)
    assert verdict(prig) == "PROTECTED"
    prig.state.save({})                                         # the executor's state file is gone
    prig.restart()
    converge(prig)
    assert verdict(prig) == "FLAT" and prig.exec.emergency is None


def test_a_restart_with_resting_entries_and_no_fill_changes_nothing(prig):
    down_for(prig, 300)
    restart_everything(prig)
    prig.drive(10)
    assert len([o for o in prig.ex.orders.values() if o["state"] == "live"]) == 5
    assert prig.ex.plans == [] and prig.exec.halt_reason is None and prig.xstate() == EXPOSURE_NONE


# ==================================================================================== properties the mutation campaign pinned
def test_a_stop_the_exchange_already_holds_for_the_asked_quantity_is_never_cancelled_and_replaced(prig):
    """Engine asks for a stop covering BOTH rungs while the executor's own record still says one rung: the exchange already
    covers it (the second rung's preset stop), so nothing may be cancelled - that would be a stop-less gap."""
    first, second = entries(prig)[:2]
    prig.ex.fill(first)
    prig.drive(5)
    assert prig.exec.stop["qty"] == pytest.approx(q_of(prig))
    prig.ex.fill(second)                                           # the exchange covers it; the executor has not looked yet
    sid = prig.engine.setup.setup_id
    # `_set_stop` directly: `apply()` would first run the fast-watch poll, which discovers and adopts the second rung by itself
    prig.exec._set_stop(SetStop(sid, Direction.LONG, frozen_stop(prig), 2 * q_of(prig), "ENGINE_ASK"), prig.ex.now_ms)
    assert prig.ex.by_kind("stop") == [] and prig.ex.by_kind("cancel_stop") == []
    assert verdict(prig) == "PROTECTED"


def test_an_old_stop_fill_from_an_earlier_setup_is_never_attributed_to_a_new_one(prig):
    """History still lists an earlier setup's stop fill (its bookkeeping is gone): it predates this setup's fill, so it is
    not this setup's exit and must not shrink its ledger."""
    prig.ex.fill(entries(prig)[0])
    old_ms = prig.ex.now_ms - 86_400_000
    prig.ex.history.append({"orderId": "OLD-STOP", "clientOid": "", "status": "filled", "state": "filled",
                            "orderSource": "loss_market", "posSide": "long", "tradeSide": "close",
                            "baseVolume": str(q_of(prig)), "priceAvg": "140.0", "fee": "-0.1", "size": str(q_of(prig)),
                            "cTime": str(old_ms)})
    prig.drive(8)
    assert "OLD-STOP" not in prig.exec.stop_fills and verdict(prig) == "PROTECTED"
    assert prig.exec.halt_reason is None and prig.exec._ledger("long") == pytest.approx(q_of(prig))
