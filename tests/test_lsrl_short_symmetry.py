"""LONG and SHORT are ONE rule set seen through a price mirror about 150.00 -- proven on whole lifecycles.

Each scenario is run twice through the full engine + simulated exchange: the LONG scenario, and the exact
mirror of its market events (bids<->asks, high<->low). Every intent must be the mirror image (direction
flipped, prices reflected, quantities identical) and every close must have the same outcome.
"""
from __future__ import annotations

import pytest

from lsrl.core.types import Direction, PlaceEntry, SetStop, SetTargets, intent_to_dict
from lsrl.modes import Mode
from tests.lsrl_fixtures import long_sweep, mirror
from tests.test_lsrl_modes_and_replay import FILL_ALL, Q, TP1, TP2, TP3, driver

FLIP = {"LONG": "SHORT", "SHORT": "LONG"}
STOP_OUT = (149.6, 149.65, 149.10, 149.5)                 # fills the ladder and trades through the stop
THREE_RUNGS = (149.6, 149.65, 149.385, 149.5)             # through E1..E3, not E4 / E5

SCENARIOS = {
    "full_tp_round_trip": [Q, FILL_ALL, Q, TP1, TP2, TP3],
    "stop_out": [Q, STOP_OUT, Q],
    "partial_fill_then_expiry": [Q, THREE_RUNGS] + [Q] * 16,
    "no_fill_then_expiry": [Q] * 17,
}


def reflect(p: float) -> float:
    return round(300.0 - p, 3)


def reflected(intent) -> dict:
    """The LONG intent as it must look on the SHORT side."""
    body = intent_to_dict(intent)
    body.pop("setup_id"), body.pop("tag", None)
    if "direction" in body:
        body["direction"] = FLIP[body["direction"]]
    if "price" in body:
        body["price"] = reflect(body["price"])
    if "targets" in body:
        body["targets"] = [{**{k: v for k, v in t.items() if k != "tag"}, "price": reflect(t["price"])}
                           for t in body["targets"]]
    return body


def plain(intent) -> dict:
    body = intent_to_dict(intent)
    body.pop("setup_id"), body.pop("tag", None)
    if "targets" in body:
        body["targets"] = [{k: v for k, v in t.items() if k != "tag"} for t in body["targets"]]
    return body


def run_pair(bars):
    long_d, short_d = driver(Mode.PAPER), driver(Mode.PAPER)
    long_d.run(long_sweep(extra_bars=tuple(bars), tail_snapshots=2))
    short_d.run(mirror(long_sweep(extra_bars=tuple(bars), tail_snapshots=2)))
    return long_d, short_d


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_intent_of_the_short_lifecycle_is_the_mirror_of_the_long_one(name):
    long_d, short_d = run_pair(SCENARIOS[name])
    assert [i.direction for i in long_d.intents if hasattr(i, "direction")][0] is Direction.LONG
    assert [i.direction for i in short_d.intents if hasattr(i, "direction")][0] is Direction.SHORT
    assert len(long_d.intents) > 5                                         # a real lifecycle, not an empty stream
    long_side, short_side = [reflected(i) for i in long_d.intents], [plain(i) for i in short_d.intents]
    assert len(long_side) == len(short_side)
    for mirrored, actual in zip(long_side, short_side):
        if mirrored.get("reason") == "TP1_COST_ADJUSTED_BE":
            # break-even adds FEES, which are proportional to price (149.4 vs 150.6): a few ticks, nothing else
            assert actual["price"] == pytest.approx(mirrored["price"], abs=0.004)
            assert {k: v for k, v in actual.items() if k != "price"} == {k: v for k, v in mirrored.items() if k != "price"}
        else:
            assert mirrored == actual


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_short_setup_ends_in_the_same_state_with_the_same_outcome(name):
    long_d, short_d = run_pair(SCENARIOS[name])
    assert (long_d.engine.setup is None) == (short_d.engine.setup is None)
    if long_d.engine.setup is not None:
        ls, ss = long_d.engine.setup, short_d.engine.setup
        assert ls.state == ss.state and ls.ledger.entry_qty == pytest.approx(ss.ledger.entry_qty)
        assert ls.ledger.remaining_qty == pytest.approx(ss.ledger.remaining_qty)
    lk = [(r.kind, r.data.get("event") or r.data.get("close_reason") or r.data.get("reason")) for r in long_d.records]
    sk = [(r.kind, r.data.get("event") or r.data.get("close_reason") or r.data.get("reason")) for r in short_d.records]
    assert lk == sk
    assert (long_d.sim.position_qty, short_d.sim.position_qty) == (pytest.approx(short_d.sim.position_qty),) * 2


@pytest.mark.parametrize("name", ["full_tp_round_trip", "stop_out"])
def test_closed_setups_have_the_same_close_reason_quantities_and_r(name):
    long_d, short_d = run_pair(SCENARIOS[name])
    lc, sc = long_d.records[-1].data, short_d.records[-1].data
    assert long_d.records[-1].kind == short_d.records[-1].kind == "setup_closed"
    assert lc["close_reason"] == sc["close_reason"] and sc["direction"] == "SHORT" and lc["direction"] == "LONG"
    assert lc["actual"]["entry_qty"] == sc["actual"]["entry_qty"] and lc["actual"]["exit_qty"] == sc["actual"]["exit_qty"]
    assert reflect(lc["actual"]["avg_entry"]) == pytest.approx(sc["actual"]["avg_entry"], abs=0.011)
    assert lc["realized_r"] == pytest.approx(sc["realized_r"], rel=0.02, abs=0.02)      # fees scale with price only
    assert (lc["actual"]["stop_slippage_usdt"] > 0) == (sc["actual"]["stop_slippage_usdt"] > 0)
    assert (lc["realized_net_usdt"] > 0) == (sc["realized_net_usdt"] > 0)


def test_short_stop_is_above_the_sweep_extreme_and_targets_are_below_entry():
    _long_d, short_d = run_pair([Q])
    s = short_d.engine.setup
    assert s.direction is Direction.SHORT
    g = s.geometry
    assert g["stop"] > g["extreme"] > g["boundary"] and g["rungs"][0] == g["boundary"] and g["rungs"][-1] == g["extreme"]
    assert g["rungs"] == sorted(g["rungs"])                                                  # E1 lowest ... E5 highest
    assert all(t["price"] < min(g["rungs"]) for t in s.targets)                              # targets are BELOW entry
    place = [i for i in short_d.intents if isinstance(i, PlaceEntry)]
    assert [i.price for i in place] == g["rungs"] and all(i.direction is Direction.SHORT for i in place)


def test_short_tp_lifecycle_moves_the_stop_down_through_break_even_then_tp1():
    _long_d, short_d = run_pair(SCENARIOS["full_tp_round_trip"])
    stops = [i for i in short_d.intents if isinstance(i, SetStop) and i.qty > 0]
    reasons = [s.reason for s in stops]
    assert reasons.index("TP1_COST_ADJUSTED_BE") < reasons.index("TP2_STOP_TO_TP1")
    original, be, to_tp1 = stops[0], next(s for s in stops if s.reason == "TP1_COST_ADJUSTED_BE"), \
        next(s for s in stops if s.reason == "TP2_STOP_TO_TP1")
    assert original.price > be.price > to_tp1.price                                          # ratchets DOWN for a short
    targets = [i for i in short_d.intents if isinstance(i, SetTargets) and i.targets][0].targets
    assert [t.level for t in targets] == ["TP1", "TP2", "TP3"]
    assert targets[0].price > targets[1].price > targets[2].price                            # nearest first, falling
