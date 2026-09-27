"""BLOCKER 2: BOOK COVERAGE. NOT OBSERVED != ABSENT.

The public WebSocket book lists a fixed window of levels; the frozen pool definition reaches 1.5 x ATR15 and the window does
not (measured: 39 % / 48 % of the reach). A pool whose run ends at the OUTERMOST listed level may continue in the part of the
book nobody observed: executed volume over that fragment overstates consumption (Agent C: a visible fragment 'consumed 26.7 %'
while the true pool consumption was 5.3 %). The same holds for the target search (TP1..TP3 are near edges of the three nearest
eligible opposite pools).

Nothing here changes the reach, the strength definition, the targets, the ladder or any other frozen value; it only refuses
to conclude from a truncated observation, and persists the evidence. Vendored from Agent C's s4_coverage_attack.py (the
scenario builders below `# ---- vendored`), driven through the production path.
"""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

import lsrl.core.sweep as sweep_module
from lsrl.core.config import DEFAULT_CONFIG as CFG
from lsrl.core.coverage import INSUFFICIENT_BOOK_COVERAGE, entry_coverage, pool_is_truncated, target_coverage
from lsrl.core.pools import PoolTracker, cluster_pools
from lsrl.core.sweep import SweepDetector
from lsrl.core.types import BarClosed, DepthSnapshot, Direction, Side, TradeTick
from tests.lsrl_fixtures import ASK_POOLS, CENTER, GRID, RULES, S, Harness, bar, long_sweep, mirror, px
from tests.lsrl_prod_rig import ProdRig
from tests.lsrl_raw_feed import Truth, mirror_raw, to_raw

# ------------------------------------------------------------------------------------------- windowed synthetic books
def windowed(events, *, bids=None, asks=None, only_from_ms=None):
    """The same scenario as the exchange would PUBLISH it through a fixed-size window (levels are best-first)."""
    out = []
    for e in events:
        if isinstance(e, DepthSnapshot) and (only_from_ms is None or e.t_ms >= only_from_ms):
            e = replace(e, bids=e.bids[:bids] if bids else e.bids, asks=e.asks[:asks] if asks else e.asks)
        out.append(e)
    return out


def rejections(h):
    return [r.data for r in h.records if r.kind == "setup_rejected"]


def reasons(h):
    return [d.get("reason") for d in rejections(h)]


# ------------------------------------------------------------------------------------------- unit level: the predicate
def levels(n_base_before, n_pool, n_base_after, *, side=Side.BID, start=150.0, base=10.0, pool=40.0):
    step = -GRID if side is Side.BID else GRID
    out, price = [], start
    for size in [base] * n_base_before + [pool] * n_pool + [base] * n_base_after:
        out.append((px(price), size))
        price += step
    return tuple(out)


def snap_of(bids, asks):
    return DepthSnapshot(t_ms=1_000, exchange_ts_ms=1_000, bids=bids, asks=asks)


def only_pool(lv, side):
    pools = cluster_pools(lv, side, 1_000, CFG.pool_min_relative_strength)
    assert len(pools) == 1
    return pools[0]


@pytest.mark.parametrize("side", [Side.BID, Side.ASK], ids=["BID_lower_boundary", "ASK_upper_boundary"])
def test_a_pool_that_ends_at_the_outermost_listed_level_is_truncated_and_one_that_is_followed_by_a_level_is_not(side):
    truncated = levels(8, 6, 0, side=side)
    complete = levels(8, 6, 1, side=side)
    other = levels(12, 0, 0, side=Side.ASK if side is Side.BID else Side.BID, start=150.05)
    snap = lambda lv: snap_of(lv, other) if side is Side.BID else snap_of(other, lv)
    assert pool_is_truncated(only_pool(truncated, side), snap(truncated)) is True
    assert pool_is_truncated(only_pool(complete, side), snap(complete)) is False


@pytest.mark.parametrize("side", [Side.BID, Side.ASK])
def test_entry_evidence_names_the_visible_and_required_bounds_the_fraction_and_the_reason(side):
    lv_ok, lv_cut = levels(8, 6, 1, side=side), levels(8, 6, 0, side=side)
    other = levels(30, 0, 0, side=Side.ASK if side is Side.BID else Side.BID, start=150.05)
    for lv, reason in ((lv_ok, None), (lv_cut, INSUFFICIENT_BOOK_COVERAGE)):
        snap = snap_of(lv, other) if side is Side.BID else snap_of(other, lv)
        ev = entry_coverage(only_pool(lv, side), snap, atr15=1.0, cfg=CFG)
        assert ev["entry_coverage_reason"] == reason and ev["entry_pool_complete"] is (reason is None)
        for key in ("visible_lower", "visible_upper", "required_lower", "required_upper", "entry_coverage_fraction"):
            assert ev[key] is not None
        assert ev["required_upper"] - ev["required_lower"] == pytest.approx(1.5)      # the FROZEN reach: 1.5 x ATR15
        assert 0.0 < ev["entry_coverage_fraction"] < 1.0                               # a window smaller than the reach


def test_the_reach_and_every_frozen_threshold_are_untouched_by_the_coverage_gate():
    assert CFG.pool_max_distance_atr15 == 1.5 and CFG.pool_min_relative_strength == 2.0
    assert CFG.pool_min_preexistence_s == 180 and CFG.sweep_min_consumed_fraction == 0.25
    assert CFG.digest().startswith("32ac85b9")                                         # the frozen config digest, unchanged


# ------------------------------------------------------------------------------------------- detector level (entry)
class Bench:
    """Static book for 200 s (the pool ages past 180 s), then a fast sweep: the first print is ON the pool and the pool is
    executed level by level; the published window is a FIXED number of levels."""

    def __init__(self, *, true_pool_levels, visible_levels, exec_units, side=Side.BID, before=25):
        self.side = side
        self.det = SweepDetector(CFG, RULES.tick_size)
        self.tracker = PoolTracker(CFG, RULES.tick_size)
        self.before, self.true_pool, self.window, self.exec = before, true_pool_levels, visible_levels, exec_units
        self.t = 0
        self.mid = 150.025

    def true_bids(self, consumed=0):
        lv = levels(self.before, self.true_pool, 60, side=Side.BID, start=150.0)
        return tuple(lv[consumed:])

    def publish(self, t, bids, asks=None):
        asks = asks or tuple((px(150.05 + i * GRID), 10.0) for i in range(80))
        snap = DepthSnapshot(t_ms=t, exchange_ts_ms=t, bids=bids[:self.window], asks=asks[:self.window])
        self.tracker.update(snap, atr15=1.0)
        self.det.on_snapshot(snap, self.tracker, 1.0)
        return snap

    def run(self):
        snap = None
        for t in range(0, 210_000, 10_000):
            snap = self.publish(t, self.true_bids())
        pool_top = px(150.0 - self.before * GRID)
        # the sweep: market SELLs eat the non-pool levels and then execute `exec_units` inside the pool
        first_ts = 211_000
        for k in range(self.before):
            self.det.on_trade(TradeTick(t_ms=first_ts + k + 40, exchange_ts_ms=first_ts + k,
                                        price=px(150.0 - k * GRID), size=10.0, aggressor="SELL"))
        left, j = self.exec, 0
        while left > 0:
            take = min(40.0, left)
            self.det.on_trade(TradeTick(t_ms=first_ts + 600 + j * 10, exchange_ts_ms=first_ts + 560 + j * 10,
                                        price=px(pool_top - j * GRID), size=take, aggressor="SELL"))
            left -= take
            j += 1
        after = self.publish(first_ts + 2_000, self.true_bids(consumed=self.before + j))
        outcomes = self.det.on_bar_1m(BarClosed(t_ms=first_ts + 3_000, tf_s=60, open_ts_ms=first_ts - 30_000,
                                                close_ts_ms=first_ts + 2_500, open=150.0, high=150.05,
                                                low=px(pool_top - j * GRID), close=150.0, volume=1.0), after)
        return outcomes


def outcome_reasons(outcomes):
    return [(o.kind, o.reason) for o in outcomes]


def test_a_complete_pool_fully_visible_is_eligible_and_confirmed():
    bench = Bench(true_pool_levels=10, visible_levels=40, exec_units=120.0)            # 10 x 40 = 400; 120 = 30 %; a level lies beyond
    outcomes = bench.run()
    assert outcome_reasons(outcomes) == [("CONFIRMED", "SWEEP_CONFIRMED")]
    w = outcomes[0].watch
    assert w.coverage["entry_pool_complete"] is True and w.coverage["entry_coverage_reason"] is None
    assert outcomes[0].consumption.fraction == pytest.approx(0.30)


def test_a_pool_truncated_at_the_lower_boundary_is_rejected_for_coverage():
    bench = Bench(true_pool_levels=10, visible_levels=33, exec_units=120.0)            # window ends INSIDE the pool (25 + 8 levels)
    outcomes = bench.run()
    assert outcome_reasons(outcomes) == [("REJECTED", INSUFFICIENT_BOOK_COVERAGE)]
    assert outcomes[0].watch.coverage["entry_pool_complete"] is False


def test_apparent_consumption_of_at_least_25_percent_caused_by_truncation_is_rejected_not_accepted(monkeypatch):
    """TRUE pool: 20 levels x 40 = 800. VISIBLE fragment: 12 levels = 480. 120 executed = 25 % of the visible fragment but only
    15 % of the true pool. Without the coverage gate the engine confirms it (that is the defect Agent C reproduced)."""
    bench = Bench(true_pool_levels=20, visible_levels=25 + 12, exec_units=120.0)
    outcomes = bench.run()
    assert outcome_reasons(outcomes) == [("REJECTED", INSUFFICIENT_BOOK_COVERAGE)]
    assert outcomes[0].consumption is None or outcomes[0].consumption.fraction >= 0.25       # apparent fraction would have passed
    # the counter-factual: the same events with the gate switched off
    monkeypatch.setattr(sweep_module, "entry_coverage", lambda *a, **k: {"entry_coverage_reason": None,
                                                                          "entry_pool_complete": True})
    again = Bench(true_pool_levels=20, visible_levels=25 + 12, exec_units=120.0).run()
    assert outcome_reasons(again) == [("CONFIRMED", "SWEEP_CONFIRMED")]
    assert again[0].consumption.fraction == pytest.approx(0.25) and again[0].consumption.frozen_depth == pytest.approx(480.0)


def test_the_ask_side_mirror_truncated_at_the_upper_boundary_is_rejected_too():
    """SHORT: the pool is an ASK pool cut by the highest listed ask. Exact price mirror of the LONG bench."""
    bench = Bench(true_pool_levels=20, visible_levels=25 + 12, exec_units=120.0)
    events = []

    class Mirror(Bench):
        def publish(self, t, bids, asks=None):
            mirrored = tuple((px(300 - p), s) for p, s in bids)
            far = tuple((px(300 - (150.05 + i * GRID)), 10.0) for i in range(80))
            snap = DepthSnapshot(t_ms=t, exchange_ts_ms=t, bids=far[:self.window], asks=mirrored[:self.window])
            self.tracker.update(snap, atr15=1.0)
            self.det.on_snapshot(snap, self.tracker, 1.0)
            return snap

        def run(self):
            for t in range(0, 210_000, 10_000):
                self.publish(t, self.true_bids())
            first_ts, pool_top = 211_000, px(150.0 - self.before * GRID)
            for k in range(self.before):
                self.det.on_trade(TradeTick(t_ms=first_ts + k + 40, exchange_ts_ms=first_ts + k, price=px(300 - (150.0 - k * GRID)),
                                            size=10.0, aggressor="BUY"))
            left, j = self.exec, 0
            while left > 0:
                take = min(40.0, left)
                self.det.on_trade(TradeTick(t_ms=first_ts + 600 + j * 10, exchange_ts_ms=first_ts + 560 + j * 10,
                                            price=px(300 - (pool_top - j * GRID)), size=take, aggressor="BUY"))
                left -= take
                j += 1
            after = self.publish(first_ts + 2_000, self.true_bids(consumed=self.before + j))
            return self.det.on_bar_1m(BarClosed(t_ms=first_ts + 3_000, tf_s=60, open_ts_ms=first_ts - 30_000,
                                                close_ts_ms=first_ts + 2_500, open=150.0, high=px(300 - (pool_top - j * GRID)),
                                                low=149.95, close=150.0, volume=1.0), after)
    outcomes = Mirror(true_pool_levels=20, visible_levels=25 + 12, exec_units=120.0).run()
    assert outcome_reasons(outcomes) == [("REJECTED", INSUFFICIENT_BOOK_COVERAGE)]
    assert outcomes[0].watch.coverage["entry_pool_complete"] is False and outcomes[0].watch.side is Side.ASK
    control = Mirror(true_pool_levels=10, visible_levels=40, exec_units=120.0).run()
    assert outcome_reasons(control) == [("CONFIRMED", "SWEEP_CONFIRMED")]


# ------------------------------------------------------------------------------------------- engine level (target side)
def test_the_baseline_scenario_with_a_full_window_is_accepted_and_persists_both_coverage_records():
    h = Harness().run(long_sweep())
    assert h.engine.setup is not None and h.engine.setup.targets and len(h.engine.setup.targets) == 3
    ev = h.engine.setup.feed_evidence
    assert ev["coverage"]["entry_pool_complete"] is True
    assert ev["coverage_target"]["target_pools_complete"] is True and ev["coverage_target"]["target_coverage_reason"] is None


@pytest.mark.parametrize("mirrored", [False, True], ids=["LONG", "SHORT"])
def test_entry_covered_but_the_target_side_truncated_is_rejected_for_coverage(mirrored):
    """The third opposite pool sits on the outermost listed level: its far part is unobserved -> no targets are selected."""
    # the window only NARROWS after the sweep (the ratified median-over-all-listed-levels rule lets a pool exist only while it is
    # a minority of the listing): at the reclaim (best ask ~149.75) the third pool shows 2 of its 5 levels
    events = windowed(windowed(long_sweep(), asks=38, only_from_ms=S + 45_000), asks=29, only_from_ms=S + 55_000)
    h = Harness().run(mirror(events) if mirrored else events)
    assert h.engine.setup is None and INSUFFICIENT_BOOK_COVERAGE in reasons(h)
    row = next(d for d in rejections(h) if d.get("reason") == INSUFFICIENT_BOOK_COVERAGE)
    cov = row["feed_evidence"]["coverage_target"]
    assert cov["target_pools_complete"] is False and cov["target_coverage_reason"] == INSUFFICIENT_BOOK_COVERAGE
    assert row["feed_evidence"]["coverage"]["entry_pool_complete"] is True               # the ENTRY side was fine
    assert cov["target_coverage_fraction"] is not None and cov["required_upper"] - cov["required_lower"] == pytest.approx(1.5)


@pytest.mark.parametrize("mirrored", [False, True], ids=["LONG", "SHORT"])
def test_fewer_than_three_pools_over_an_unobserved_reach_is_a_coverage_failure_not_an_absence(mirrored):
    events = windowed(long_sweep(ask_pools=ASK_POOLS[:2]), asks=20)                      # window 1.0 < the 1.5 reach
    h = Harness().run(mirror(events) if mirrored else events)
    assert INSUFFICIENT_BOOK_COVERAGE in reasons(h) and "INSUFFICIENT_OPPOSITE_POOLS" not in reasons(h)


@pytest.mark.parametrize("mirrored", [False, True], ids=["LONG", "SHORT"])
def test_fewer_than_three_pools_over_a_fully_observed_reach_stays_an_ordinary_no_trade(mirrored):
    events = long_sweep(ask_pools=ASK_POOLS[:2])                                         # 50-level window covers the reach
    h = Harness().run(mirror(events) if mirrored else events)
    assert "INSUFFICIENT_OPPOSITE_POOLS" in reasons(h) and INSUFFICIENT_BOOK_COVERAGE not in reasons(h)


def test_target_coverage_predicate_directly():
    lv = levels(8, 6, 1, side=Side.ASK, start=150.05)
    other = levels(30, 0, 0, side=Side.BID, start=150.0)
    snap = snap_of(other, lv)
    pool = only_pool(lv, Side.ASK)
    ok = target_coverage(Direction.LONG, snap, [pool, pool, pool], 3, 1.0, CFG)
    assert ok["target_coverage_reason"] is None and ok["target_pools_complete"] is True
    cut = levels(8, 6, 0, side=Side.ASK, start=150.05)
    snap_cut = snap_of(other, cut)
    bad = target_coverage(Direction.LONG, snap_cut, [only_pool(cut, Side.ASK)], 3, 1.0, CFG)
    assert bad["target_coverage_reason"] == INSUFFICIENT_BOOK_COVERAGE and bad["target_pools_complete"] is False
    assert INSUFFICIENT_BOOK_COVERAGE not in reasons(Harness())                          # the helper is inert on its own


# ------------------------------------------------------------------------------------------- Agent C's attack, production path
T = lambda sec: S + int(round(sec * 1000))
WINDOW = 30


class Deep:
    """True deep book; the published state is its top-`window` levels per side."""
    def __init__(self, truth, pool_upper, pool_true_lower, base=10.0, pool=40.0, window=WINDOW, asks_pools=ASK_POOLS, extra_ask=()):
        self.truth, self.pu, self.pl, self.base, self.pool, self.window = truth, pool_upper, pool_true_lower, base, pool, window
        self.asks_pools, self.extra_ask = asks_pools, extra_ask
        self.set_price(149.95)

    def set_price(self, best_bid):
        self.bids = {px(best_bid - i * GRID): (self.pool if self.pl <= px(best_bid - i * GRID) <= self.pu else self.base) for i in range(200)}
        ba = px(best_bid + 0.10)
        self.asks = {px(ba + i * GRID): self.base for i in range(200)}
        for edge, n, size in tuple(self.asks_pools) + tuple(self.extra_ask):
            for i in range(n):
                self.asks[px(edge + i * GRID)] = size

    def mark(self, ts):
        b = dict(sorted(self.bids.items(), key=lambda kv: -kv[0])[:self.window])
        a = dict(sorted(self.asks.items(), key=lambda kv: kv[0])[:self.window])
        self.truth.state(ts, b, a)

    def sell_silent(self, ts, price, size):
        price = px(price); have = self.bids[price]; assert size <= have + 1e-9, (price, size, have)
        if have - size <= 1e-9: del self.bids[price]
        else: self.bids[price] = round(have - size, 6)
        self.truth.trade(ts, price, size, "sell")

    def sell(self, ts, price, size):
        price = px(price); have = self.bids[price]; assert size <= have + 1e-9, (price, size, have)
        if have - size <= 1e-9: del self.bids[price]
        else: self.bids[price] = round(have - size, 6)
        self.truth.trade(ts, price, size, "sell"); self.mark(ts)


def scenario(pool_true_levels, exec_units, near_level=25, window=40, extra_ask=(), asks_pools=ASK_POOLS, batch=False):
    """Static market at 149.95 until a FAST sweep at t=33 s. The pool starts `near_level` levels below the touch (so at freeze it is a
    MINORITY of the published window: the ratified median-over-all-listed-levels rule only lets a pool exist while it is)
    and runs `pool_true_levels` levels deep. The sweep eats the non-pool levels in ~250 ms and then executes `exec_units` inside the pool."""
    truth = Truth()
    pool_upper = px(149.95 - near_level * GRID); pool_true_lower = px(pool_upper - (pool_true_levels - 1) * GRID)
    d = Deep(truth, pool_upper, pool_true_lower, window=window, extra_ask=extra_ask, asks_pools=asks_pools)
    t = -240.0
    while t <= 30.0 + 1e-9:
        d.mark(T(t)); t += 5.0
    price = 149.95; i = 0
    if batch:                                              # ONE market order eats every non-pool level; the book is published once, after it
        while price > pool_upper + 1e-9:
            d.sell_silent(T(33.0) + i, price, 10.0); price = px(price - GRID); i += 1
        d.mark(T(33.0) + i + 5)
    else:
        while price > pool_upper + 1e-9:                   # the non-pool levels: 10 units each, one every 10 ms
            d.sell(T(33.0) + i * 10, price, 10.0); price = px(price - GRID); i += 1
    left = exec_units; j = 0
    while left > 0:
        take = min(40.0, left); d.sell(T(33.5) + j * 100, px(pool_upper - j * GRID), take); left -= take; j += 1
    for k in range(1, 30):
        d.mark(T(34.0 + k * 5.0))
    return truth, pool_upper


def run_case(name, truth, low, mirrored, *, ask_extra=None):
    events = to_raw(truth, bars=[bar(S, CENTER, 150.05, low, 148.80)], keepalive_until_ms=T(95))
    if mirrored: events = mirror_raw(events)
    tmp = Path(tempfile.mkdtemp(prefix="c_cov_"))
    try:
        def truth_at(t):
            best = truth.states[0]
            for st in truth.states:
                if st[0] <= t: best = st
            b, a = best[1], best[2]
            return ({px(300 - p): s for p, s in a.items()}, {px(300 - p): s for p, s in b.items()}) if mirrored else (dict(b), dict(a))
        rig = ProdRig(tmp, truth_at=truth_at, start_ms=events[0].t_ms - 1000).run(events, tail_ms=2500)
        st = rig.engine.setup
        c = rig.consumption()
        return rig, dict(ladders=int(rig.sweep_confirmed()), reasons=[r for r in rig.reasons() if r][:3], cons=None if not c else (round(c["fraction"], 3), c["consumed"], c["frozen_depth"]),
                         targets=None if st is None or not st.targets else [(t["level"], t["price"]) for t in st.targets],
                         rr=None if st is None else round(st.expected_rr_tp2 or 0, 2), replay=rig.verify()["genesis_replay"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)




def run_cov_case(truth, low, mirrored):
    events = to_raw(truth, bars=[bar(S, CENTER, 150.05, low, 148.80)], keepalive_until_ms=T(95))
    if mirrored:
        events = mirror_raw(events)
    tmp = Path(tempfile.mkdtemp(prefix="c_cov_"))
    try:
        def truth_at(t):
            best = truth.states[0]
            for st in truth.states:
                if st[0] <= t:
                    best = st
            b, a = best[1], best[2]
            return ({px(300 - p): s for p, s in a.items()}, {px(300 - p): s for p, s in b.items()}) if mirrored else (dict(b), dict(a))
        rig = ProdRig(tmp, truth_at=truth_at, start_ms=events[0].t_ms - 1000).run(events, tail_ms=2500)
        return dict(confirmed=rig.sweep_confirmed(), reasons=[r for r in rig.reasons() if r], replay=rig.verify()["genesis_replay"],
                    coverage=next((r.data["feed_evidence"].get("coverage") for r in rig.driver.records
                                   if r.kind == "setup_rejected" and r.data.get("feed_evidence")), None))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.parametrize("mirrored", [False, True], ids=["LONG", "SHORT"])
def test_agent_c_E_truncated_BATCH_is_rejected_for_coverage_not_accepted_at_26_7_percent(mirrored):
    t, pu = scenario(pool_true_levels=75, exec_units=160.0, batch=True)        # visible 600, true 3000: 26.7 % vs 5.3 %
    row = run_cov_case(t, px(pu - 3 * GRID), mirrored)
    assert not row["confirmed"] and INSUFFICIENT_BOOK_COVERAGE in row["reasons"] and row["replay"] == "EQUIVALENT"
    assert row["coverage"] and row["coverage"]["entry_pool_complete"] is False


@pytest.mark.parametrize("mirrored", [False, True], ids=["LONG", "SHORT"])
def test_agent_c_E_truncated_incremental_is_not_accepted(mirrored):
    t, pu = scenario(pool_true_levels=75, exec_units=160.0)
    assert not run_cov_case(t, px(pu - 3 * GRID), mirrored)["confirmed"]


@pytest.mark.parametrize("mirrored", [False, True], ids=["LONG", "SHORT"])
def test_agent_c_controls_that_are_fully_observable_are_accepted_and_the_deep_window_rejects_5_3_percent(mirrored):
    t, pu = scenario(pool_true_levels=15, exec_units=160.0)                     # C's control A (incremental): accepted, unchanged
    assert run_cov_case(t, px(pu - 3 * GRID), mirrored)["confirmed"]
    t, pu = scenario(pool_true_levels=14, exec_units=160.0, batch=True)          # one observed non-pool level beyond the edge
    row = run_cov_case(t, px(pu - 3 * GRID), mirrored)
    assert row["confirmed"] and INSUFFICIENT_BOOK_COVERAGE not in row["reasons"]
    t, pu = scenario(pool_true_levels=75, exec_units=160.0, window=140, near_level=100)      # C's control B: 5.3 % of the true pool
    assert not run_cov_case(t, px(pu - 3 * GRID), mirrored)["confirmed"]


@pytest.mark.parametrize("mirrored", [False, True], ids=["LONG", "SHORT"])
def test_a_pool_that_ends_exactly_on_the_window_edge_is_unprovable_and_rejected(mirrored):
    """Agent C's 'control A-BATCH' has a 15-level pool whose last level IS the last published level (levels 25..39 of 40). Nothing
    published proves the pool does not continue, so by C's own rule ('a pool whose run touches the outermost listed level') it is
    a coverage failure. With one observed non-pool level beyond the edge (14 levels, previous test) it is accepted."""
    t, pu = scenario(pool_true_levels=15, exec_units=160.0, batch=True)
    row = run_cov_case(t, px(pu - 3 * GRID), mirrored)
    assert not row["confirmed"] and INSUFFICIENT_BOOK_COVERAGE in row["reasons"]
