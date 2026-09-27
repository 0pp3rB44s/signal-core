"""BLOCKER 1: CROSS-CHANNEL ORDERING. No total order is assumed; the acceptance rule needs none.

Bitget guarantees no order between the `books` and the `trade` channel: the depth update that reflects a trade is stamped a
median ~50 ms AFTER it (p95 +105 ms, tails -403 / +496 ms, Agent C, 544 exact matches on the live feed). The engine used to
compare the two exchange stamps at 1 ms resolution and therefore accepted a cancel that truly came BEFORE the first print
whenever its depth update was stamped even 1 ms after it (16 of C's 64 rows).

Adding a tuned offset (+50 / +105 ms) would be a magic number. Instead, acceptance now rests on ORDER-FREE conservation of
volume (rule R6): every unit that left a frozen pool level while the market was interacting with it must be accounted for by
executed volume at that price, whichever stamp or arrival order the two channels show. What cannot be accounted for is
ambiguity, and ambiguity is NO SIGNAL. These tests sweep the stamp offset across the whole measured uncertainty band (and
beyond) and require that the verdict never depends on it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import lsrl.core.sweep as sweep_module
from lsrl.core.config import DEFAULT_CONFIG as CFG
from lsrl.core.pools import PoolTracker
from lsrl.core.sweep import CONSERVATION_TOLERANCE, SweepDetector
from lsrl.core.types import BarClosed, DepthSnapshot, Side, TradeTick
from tests.lsrl_fixtures import GRID, RULES, px

REASON = "POOL_REMOVAL_NOT_EXPLAINED_BY_EXECUTION"
POOL_TOP, N_POOL, POOL_SIZE, BASE = 149.60, 6, 40.0, 10.0
FIRST_PRINT = 211_000
OFFSETS = list(range(-500, 501, 25)) + [1, 2, 3, 5, 10, 20, 50, 105, 400, 496]


def bids_with(pool_sizes: dict[float, float] | None = None, *, gone=()):
    """Bids best-first from 149.95; the pool prices carry `pool_sizes` (default 40 each); `gone` levels are absent."""
    out = []
    for i in range(70):
        price = px(149.95 - i * GRID)
        size = (pool_sizes or {}).get(price, POOL_SIZE if 149.35 - 1e-9 <= price <= POOL_TOP + 1e-9 else BASE)
        if price in gone:
            continue
        out.append((price, size))
    return tuple(out)


ASKS = tuple((px(150.05 + i * GRID), BASE) for i in range(70))
POOL_PRICES = [px(POOL_TOP - i * GRID) for i in range(N_POOL)]


class Run:
    def __init__(self):
        self.det = SweepDetector(CFG, RULES.tick_size)
        self.tracker = PoolTracker(CFG, RULES.tick_size)
        self.last = None
        self.notes: list = []

    def snap(self, t_ms, exchange_ts, bids):
        s = DepthSnapshot(t_ms=t_ms, exchange_ts_ms=exchange_ts, bids=bids, asks=ASKS)
        self.tracker.update(s, atr15=1.0)
        self.notes += self.det.on_snapshot(s, self.tracker, 1.0)
        self.last = s
        return s

    def prime(self):
        for t in range(0, 210_000, 10_000):
            self.snap(t, t, bids_with())

    def sell(self, price, size, exchange_ts, recv):
        self.det.on_trade(TradeTick(t_ms=recv, exchange_ts_ms=exchange_ts, price=price, size=size, aggressor="SELL"))

    def close(self, low):
        bar = BarClosed(t_ms=FIRST_PRINT + 3_000, tf_s=60, open_ts_ms=FIRST_PRINT - 30_000, close_ts_ms=FIRST_PRINT + 2_500,
                        open=150.0, high=150.05, low=low, close=150.0, volume=1.0)
        return self.det.on_bar_1m(bar, self.last)


def cancel_before_touch(offset_ms: int, *, baseline=BASE, depth_first: bool = False):
    """TRUTH: the pool is cancelled (down to `baseline` per level) just BEFORE the market reaches it, then ordinary flow prints at the
    pool's prices (baseline per level = exactly 25 % of the frozen 240). The depth update that carries the cancel is STAMPED
    first_print + offset (channel skew) and ARRIVES after the trade frames (or before, if depth_first)."""
    r = Run()
    r.prime()
    cancelled = bids_with({p: baseline for p in POOL_PRICES})
    if depth_first:
        r.snap(FIRST_PRINT + 10, FIRST_PRINT + offset_ms, cancelled)
    for k, price in enumerate(POOL_PRICES):
        r.sell(price, baseline, FIRST_PRINT + k * 3, FIRST_PRINT + 40 + k * 3)
    if not depth_first:
        r.snap(FIRST_PRINT + 300, FIRST_PRINT + offset_ms, cancelled)
    r.snap(FIRST_PRINT + 2_000, FIRST_PRINT + 2_000, bids_with(gone=tuple(POOL_PRICES)))
    out = r.close(POOL_PRICES[-1])
    if not out:                                     # the pool left the book while the market was still away: refused as such
        assert [n["reason"] for n in r.notes] == ["POOL_DISAPPEARED_BEFORE_INTERACTION"]
    return out


def genuine_execution(offset_ms: int, *, share=1.0):
    """TRUTH: the pool is executed (`share` of every level) by aggressive sells. The depth update that reflects it is stamped
    first_print + offset."""
    r = Run()
    r.prime()
    for k, price in enumerate(POOL_PRICES):
        r.sell(price, POOL_SIZE * share, FIRST_PRINT + k * 3, FIRST_PRINT + 40 + k * 3)
    after = bids_with({p: POOL_SIZE * (1 - share) for p in POOL_PRICES}, gone=tuple(POOL_PRICES) if share == 1.0 else ())
    r.snap(FIRST_PRINT + 300, FIRST_PRINT + offset_ms, after)
    r.snap(FIRST_PRINT + 2_000, FIRST_PRINT + 2_000, after)
    return r.close(POOL_PRICES[-1])


def verdict(outcomes):
    return [(o.kind, o.reason) for o in outcomes]


# =============================================================================================== the cancel is never accepted
@pytest.mark.parametrize("offset", OFFSETS)
@pytest.mark.parametrize("depth_first", [False, True], ids=["trade_frames_first", "depth_frame_first"])
def test_a_cancel_before_the_touch_is_never_confirmed_whatever_the_stamp_offset(offset, depth_first):
    out = cancel_before_touch(offset, depth_first=depth_first)
    assert all(kind == "REJECTED" for kind, _ in verdict(out)), verdict(out)
    if not depth_first:
        assert verdict(out)                                                 # decided at the bar close, not silently dropped


@pytest.mark.parametrize("offset", [1, 2, 3, 5, 10, 20, 50, 105, 400])
def test_the_rows_the_1ms_ordering_used_to_accept_are_refused_by_conservation_not_by_a_timestamp(offset):
    out = cancel_before_touch(offset)
    assert verdict(out) == [("REJECTED", REASON)]
    est = out[0].consumption
    assert est.fraction >= 0.25 - 1e-9                                       # the TAPE ALONE would have confirmed it
    assert est.max_removed == pytest.approx(N_POOL * POOL_SIZE)             # everything left the frozen levels...
    assert est.consumed == pytest.approx(N_POOL * BASE)                     # ...but only the baseline was executed there
    assert est.unexplained_removal == pytest.approx(N_POOL * (POOL_SIZE - BASE))


def test_the_verdict_is_independent_of_which_stamp_the_cancel_carries():
    """A sweep across the whole measured band and beyond: the set of rejection reasons is one of two safe ones, and once the
    timestamp checks (R1-R5) have nothing to say, conservation says the same thing for every offset."""
    reasons = {offset: verdict(cancel_before_touch(offset))[0][1] for offset in OFFSETS}   # (trade frames arrive first)
    assert set(reasons.values()) <= {"ORDER_AMBIGUOUS", REASON}
    assert {reasons[o] for o in (1, 2, 3, 5, 10, 20, 50, 105, 400, 496)} == {REASON}


# =============================================================================================== a genuine execution stays classifiable
@pytest.mark.parametrize("share", [1.0, 0.5, 0.25])
@pytest.mark.parametrize("offset", [1, 5, 50, 105, 400])
def test_a_genuine_execution_is_confirmed_whatever_the_positive_stamp_offset(offset, share):
    out = genuine_execution(offset, share=share)
    assert verdict(out) == [("CONFIRMED", "SWEEP_CONFIRMED")]
    est = out[0].consumption
    assert est.unexplained_removal <= CONSERVATION_TOLERANCE and est.fraction == pytest.approx(share)


@pytest.mark.parametrize("offset", [-500, -105, -25, -1, 0])
def test_when_the_stamps_cannot_order_the_channels_the_result_is_a_safe_refusal_never_a_conservation_failure(offset):
    out = genuine_execution(offset)
    kinds = verdict(out)
    assert kinds in ([("CONFIRMED", "SWEEP_CONFIRMED")], [("REJECTED", "ORDER_AMBIGUOUS")])


def test_conservation_ignores_the_stamps_entirely():
    a, b = genuine_execution(3), genuine_execution(400)
    assert a[0].consumption.unexplained_removal == b[0].consumption.unexplained_removal == 0.0


# =============================================================================================== edge cases of the rule itself
def test_a_pull_that_happens_after_enough_volume_was_executed_is_still_ambiguous_and_refused():
    """Executed 60 % of every level, then the remaining 40 % is pulled: the order pull/execution cannot be known -> no signal
    (an opportunity cost, the safe direction; the rule never invents an order)."""
    r = Run()
    r.prime()
    for k, price in enumerate(POOL_PRICES):
        r.sell(price, POOL_SIZE * 0.6, FIRST_PRINT + k * 3, FIRST_PRINT + 40 + k * 3)
    r.snap(FIRST_PRINT + 300, FIRST_PRINT + 300, bids_with(gone=tuple(POOL_PRICES)))          # everything gone: 40 % never executed
    r.snap(FIRST_PRINT + 2_000, FIRST_PRINT + 2_000, bids_with(gone=tuple(POOL_PRICES)))
    assert verdict(r.close(POOL_PRICES[-1])) == [("REJECTED", REASON)]


def test_a_replenished_pool_is_judged_on_the_lowest_size_it_ever_had_not_on_the_final_book():
    r = Run()
    r.prime()
    for k, price in enumerate(POOL_PRICES):
        r.sell(price, POOL_SIZE * 0.7, FIRST_PRINT + k * 3, FIRST_PRINT + 40 + k * 3)
    r.snap(FIRST_PRINT + 300, FIRST_PRINT + 300, bids_with({p: POOL_SIZE * 0.3 for p in POOL_PRICES}))   # 70 % executed: consistent
    r.snap(FIRST_PRINT + 2_000, FIRST_PRINT + 2_000, bids_with())                                       # refilled to 40 (replenishment)
    out = r.close(POOL_PRICES[-1])
    assert verdict(out) == [("CONFIRMED", "SWEEP_CONFIRMED")] and out[0].consumption.replenished_est > 0
    # ...but a cancel that is then refilled to hide it is NOT hidden: the minimum is what counts
    r = Run()
    r.prime()
    for k, price in enumerate(POOL_PRICES):
        r.sell(price, BASE, FIRST_PRINT + k * 3, FIRST_PRINT + 40 + k * 3)
    r.snap(FIRST_PRINT + 300, FIRST_PRINT + 300, bids_with({p: BASE for p in POOL_PRICES}))             # pulled to baseline
    r.snap(FIRST_PRINT + 2_000, FIRST_PRINT + 2_000, bids_with())                                       # then re-posted
    assert verdict(r.close(POOL_PRICES[-1])) == [("REJECTED", REASON)]


def test_the_conservation_tolerance_is_exact_not_a_tuned_slack():
    assert CONSERVATION_TOLERANCE <= 1e-6


# =============================================================================================== structural: no total order, no magic offset
def _compare_nodes_with_exchange_stamps(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            names = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            if "exchange_ts_ms" in names or "frozen_exchange_ts_ms" in names or "interaction_exchange_ts_ms" in names:
                yield node


def test_no_acceptance_decision_compares_exchange_stamps_across_channels_only_refusals_do():
    tree = ast.parse(Path(sweep_module.__file__).read_text())
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    for cmp in _compare_nodes_with_exchange_stamps(tree):
        node, branch = cmp, None
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.If, ast.IfExp)) and not isinstance(node, ast.IfExp):
                branch = node
                break
        assert branch is not None, f"line {cmp.lineno}: a stamp comparison outside an if"
        called = {n.func.attr for stmt in branch.body for n in ast.walk(stmt)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert "_ambiguous" in called, f"line {cmp.lineno}: a stamp comparison that does not merely refuse"


def test_no_numeric_offset_is_applied_to_an_exchange_stamp_anywhere_in_the_decision_path():
    for module in (sweep_module,):
        tree = ast.parse(Path(module.__file__).read_text())
        for cmp in _compare_nodes_with_exchange_stamps(tree):
            constants = [n.value for n in ast.walk(cmp) if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
                         and not isinstance(n.value, bool)]
            assert constants == [], f"line {cmp.lineno}: numeric literal {constants} in a timestamp comparison"
        for binop in [n for n in ast.walk(tree) if isinstance(n, ast.BinOp)]:
            names = {n.attr for n in ast.walk(binop) if isinstance(n, ast.Attribute)}
            if "exchange_ts_ms" in names:
                pytest.fail(f"line {binop.lineno}: arithmetic on an exchange stamp")


# =============================================================================================== the interaction-starting snapshot counts
def _book_started(after_start):
    """The depth update that shows the touch (pool gone) arrives BEFORE any trade frame (book-first interaction); then the pool is
    re-posted before the bar closes. `executed` is what actually printed."""
    r = Run()
    r.prime()
    start = tuple((p, sz) for p, sz in bids_with() if p < POOL_PRICES[-1] - 1e-9)   # everything above is gone: best bid below the pool
    r.snap(FIRST_PRINT + 10, FIRST_PRINT + 10, start)                              # the touch is seen as a BOOK update
    for k, price in enumerate(POOL_PRICES):
        if after_start["executed"]:
            r.sell(price, after_start["executed"], FIRST_PRINT + 40 + k * 3, FIRST_PRINT + 80 + k * 3)
    r.snap(FIRST_PRINT + 2_000, FIRST_PRINT + 2_000, bids_with())                  # the pool is back (re-posted / replenished)
    return r.close(POOL_PRICES[-1])


def test_a_pool_pulled_at_the_touch_and_re_posted_before_the_bar_closes_cannot_hide_behind_the_final_book():
    assert verdict(_book_started({"executed": BASE})) == [("REJECTED", REASON)]      # only baseline printed: 75 % never traded


def test_the_same_shape_with_every_removed_unit_executed_is_confirmed():
    out = _book_started({"executed": POOL_SIZE})
    assert verdict(out) == [("CONFIRMED", "SWEEP_CONFIRMED")] and out[0].consumption.unexplained_removal == 0.0


def test_a_pool_partly_pulled_in_the_snapshot_that_shows_the_touch_and_then_refilled_is_still_caught():
    r = Run()
    r.prime()
    pulled = tuple((p, 25.0 if p in POOL_PRICES else sz) for p, sz in bids_with() if p <= POOL_PRICES[0] + 1e-9)   # best bid = pool top
    r.snap(FIRST_PRINT + 10, FIRST_PRINT + 10, pulled)                                # pool still listed (25 >= 2 x median), touch reached
    for k, price in enumerate(POOL_PRICES):
        r.sell(price, BASE, FIRST_PRINT + 40 + k * 3, FIRST_PRINT + 80 + k * 3)      # 60 executed = 25 % of the frozen 240
    r.snap(FIRST_PRINT + 2_000, FIRST_PRINT + 2_000, bids_with())                     # refilled to 40: the final book looks intact
    assert verdict(r.close(POOL_PRICES[-1])) == [("REJECTED", REASON)]                # 15 pulled per level, only 10 executed
