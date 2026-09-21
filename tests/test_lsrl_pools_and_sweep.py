"""LSRL_V1: pool qualification, sweep detection, reclaim window (deterministic)."""
from __future__ import annotations

from lsrl.core.pools import cluster_pools, eligibility_failures
from lsrl.core.config import DEFAULT_CONFIG
from lsrl.core.setup import STATE_LADDER_PENDING
from lsrl.core.types import PlaceEntry, Side
from tests.lsrl_fixtures import ASK_POOLS, BID_POOL, RULES, Harness, long_sweep, make_book, S


def _placed(h: Harness) -> list[PlaceEntry]:
    return h.intents_of(PlaceEntry)


# ------------------------------------------------------------------ baseline
def test_valid_long_setup_places_a_five_rung_ladder():
    h = Harness().run(long_sweep())
    assert h.engine.setup is not None and h.engine.setup.state == STATE_LADDER_PENDING
    assert [i.price for i in _placed(h)] == [149.60, 149.50, 149.40, 149.30, 149.20]
    assert len({i.qty for i in _placed(h)}) == 1            # equal 20% rungs
    assert h.rejected() == []


# ------------------------------------------------------------------ pool rules
def test_cluster_pools_groups_only_adjacent_levels_at_or_above_two_times_median():
    levels = tuple((round(150 - i * 0.05, 2), 10.0) for i in range(30))
    levels = tuple((p, 40.0) if i in (5, 6, 7, 15) else (p, s) for i, (p, s) in enumerate(levels))
    pools = cluster_pools(levels, Side.BID, 1, 2.0)
    assert [(p.n_levels, round(p.strength, 2)) for p in pools] == [(3, 4.0), (1, 4.0)]
    assert pools[0].upper > pools[0].lower and pools[0].depth == 120.0


def test_pool_too_weak_is_not_a_pool_and_never_sweeps():
    # 1.5x the median is below MIN_RELATIVE_STRENGTH=2.0: nothing to sweep.
    h = Harness().run(long_sweep(pool=(149.60, 6, 15.0)))
    assert h.engine.setup is None and h.intents == []
    assert not any(r.data.get("reason") == "SWEEP_CONFIRMED" for r in h.records)


def test_pool_too_young_is_rejected():
    h = Harness().run(long_sweep(pool_first_seen_s=-120))
    assert h.engine.setup is None and h.intents == []
    assert "POOL_TOO_YOUNG" in h.rejected()


def test_pool_exactly_at_min_preexistence_is_accepted():
    # frozen at S+30s -> a pool first seen at S-150s is exactly 180s old
    h = Harness().run(long_sweep(pool_first_seen_s=-150))
    assert h.engine.setup is not None, h.rejected()


def test_pool_too_old_is_rejected():
    h = Harness().run(long_sweep(pool_first_seen_s=-3700))
    assert h.engine.setup is None and "POOL_TOO_OLD" in h.rejected()


def test_pool_too_far_is_rejected():
    # ATR15 = 1.0 -> max distance 1.5. The pool sits ~1.9 below the last pre-touch mid.
    path = ((10, 150.0), (20, 149.8), (30, 149.5), (40, 147.6), (50, None), (60, None))
    h = Harness().run(long_sweep(pool=(147.60, 6, 40.0), path=path, sweep_low=147.00, sweep_close=147.65))
    assert h.engine.setup is None and "POOL_TOO_FAR" in h.rejected()


def test_eligibility_failures_reports_each_rule_independently():
    from lsrl.core.pools import PoolState
    st = PoolState(Side.BID, 0, 149.35, 149.60, 240.0, 6, 4.0, 10.0, ())
    cfg = DEFAULT_CONFIG
    assert eligibility_failures(st, 0, 181_000, 150.0, 1.0, cfg) == []
    assert eligibility_failures(st, 0, 179_000, 150.0, 1.0, cfg) == ["POOL_TOO_YOUNG"]
    assert eligibility_failures(st, 0, 3_601_000, 150.0, 1.0, cfg) == ["POOL_TOO_OLD"]
    assert eligibility_failures(st, 0, 181_000, 152.0, 1.0, cfg) == ["POOL_TOO_FAR"]
    assert eligibility_failures(st, 0, 181_000, 150.0, None, cfg) == ["ATR15_UNAVAILABLE"]


# ------------------------------------------------------------------ consumed vs cancelled
def test_cancellation_before_price_touches_is_not_a_sweep():
    """The pool is pulled while price is still far above it. No interaction, no sweep."""
    flat = ((10, 150.0), (20, 150.0), (30, 150.0), (40, 150.0), (50, 150.0), (60, 150.0))
    h = Harness().run(long_sweep(pool_cancel_at_s=30, path=flat, sweep_low=149.90, sweep_close=150.0))
    assert h.engine.setup is None and h.intents == []
    assert "POOL_DISAPPEARED_BEFORE_INTERACTION" in h.rejected()
    assert not any(r.data.get("event") == "SWEEP_CONFIRMED" for r in h.records)


def test_touch_that_consumes_too_little_is_not_a_sweep():
    path = ((10, 150.0), (20, 149.85), (30, 149.70), (40, 149.62), (50, 149.62), (60, 149.75))
    h = Harness().run(long_sweep(consume=False, path=path, sweep_low=149.58, sweep_close=149.75))
    assert h.engine.setup is None and "TOUCH_WITHOUT_SWEEP" in h.rejected()


def test_depth_pulled_at_levels_price_never_reached_is_cancellation_not_consumption():
    """Price only touches the pool's top level; the deeper levels vanish. Depth that leaves the book
    without executing is cancellation: it is recorded as such and can NEVER count as consumption."""
    path = ((10, 150.0), (20, 149.85), (30, 149.70), (40, 149.62), (50, 149.62), (60, 149.75))
    h = Harness().run(long_sweep(pool_after_touch=(149.60, 1, 40.0), path=path,
                                 sweep_low=149.58, sweep_close=149.75))
    assert h.engine.setup is None
    rejection = next(r.data for r in h.records if r.data.get("reason") == "TOUCH_WITHOUT_SWEEP")
    assert rejection["consumption"]["consumed"] == 0.0
    assert rejection["consumption"]["cancelled_est"] > 0.0
    assert rejection["consumption"]["method"] == "TAPE"


def test_consumption_estimate_is_recorded_on_a_valid_sweep():
    h = Harness().run(long_sweep())
    c = h.engine.setup.consumption
    assert c["fraction"] >= 0.25 and c["consumed"] > 0 and c["method"] == "TAPE" and c["trades"] > 0


# ------------------------------------------------------------------ reclaim window
_NO_RECLAIM = (149.50, 149.55, 149.45, 149.50)
_RECLAIM = (149.55, 149.80, 149.55, 149.75)


def test_sweep_without_reclaim_is_rejected():
    h = Harness().run(long_sweep(sweep_close=149.50, extra_bars=(_NO_RECLAIM,) * 5))
    assert h.engine.setup is None and h.intents == []
    assert "NO_RECLAIM_WITHIN_5_BARS" in h.rejected()


def test_reclaim_on_the_sixth_bar_is_too_late():
    h = Harness().run(long_sweep(sweep_close=149.50, extra_bars=(_NO_RECLAIM,) * 5 + (_RECLAIM,)))
    assert h.engine.setup is None and "NO_RECLAIM_WITHIN_5_BARS" in h.rejected()


def test_reclaim_on_the_fifth_bar_after_the_sweep_is_valid():
    h = Harness().run(long_sweep(sweep_close=149.50, extra_bars=(_NO_RECLAIM,) * 4 + (_RECLAIM,)))
    assert h.engine.setup is not None and h.engine.setup.bars_after_sweep == 5, h.rejected()
    assert len(_placed(h)) == 5


def test_same_bar_reclaim_is_valid():
    h = Harness().run(long_sweep())                # sweep bar itself closes back above the pool
    assert h.engine.setup.bars_after_sweep == 0


def test_reclaim_uses_a_completed_close_not_an_intrabar_high():
    # A bar that wicks well above the boundary but CLOSES below it is not a reclaim.
    wick = (149.50, 149.90, 149.45, 149.55)
    h = Harness().run(long_sweep(sweep_close=149.50, extra_bars=(wick,) * 5))
    assert h.engine.setup is None and "NO_RECLAIM_WITHIN_5_BARS" in h.rejected()


# ------------------------------------------------------------------ stale data
def test_stale_liquidity_data_fails_closed():
    h = Harness().run(long_sweep(drop_snapshots_after_s=30))
    assert h.engine.setup is None and h.intents == []
    assert "LIQUIDITY_DATA_STALE" in h.rejected()


def test_pool_first_seen_is_recorded_from_observation_not_inferred():
    h = Harness().run(long_sweep())
    s = h.engine.setup
    assert s.pool_first_seen_ms == S - 240_000
    assert s.pool_age_s_at_freeze >= 180
    assert s.depth_series and s.pool["strength"] >= 2.0


def test_pool_age_never_bridges_a_gap_in_depth_observation():
    """A pool that was last seen 15 hours ago is NOT the same 15-hour-old pool when a snapshot
    with the same price levels finally arrives: age means continuous observation."""
    from lsrl.core.pools import PoolTracker
    tracker = PoolTracker(DEFAULT_CONFIG, 0.01)
    book = lambda t: make_book(t, 149.95, 150.05, bid_pools=((149.60, 6, 40.0),))
    tracker.update(book(0), 1.0)
    tracker.update(book(10_000), 1.0)
    first = tracker.alive(Side.BID)[0]
    assert first.first_seen_ms == 0
    tracker.update(book(15 * 3_600_000), 1.0)                       # snapshot after a 15h data gap
    again = tracker.alive(Side.BID)[0]
    assert again.first_seen_ms == 15 * 3_600_000 and again.pool_id != first.pool_id
    tracker.update(book(15 * 3_600_000 + 10_000), 1.0)               # ...but ordinary cadence continues it
    assert tracker.alive(Side.BID)[0].first_seen_ms == 15 * 3_600_000


# ------------------------------------------------------------------ resources over a long session
def test_pool_bookkeeping_stays_bounded_past_the_retention_window():
    """A live book updates ~10x/s for days. Pool history is bookkeeping only; before it was bounded the tracker
    rebuilt ~5,000-element lists for ~90 pools on every update once the 600 s window filled (10x CPU, 10x RAM)."""
    from lsrl.core.config import DEFAULT_CONFIG as CFG
    from lsrl.core.pools import HISTORY_SAMPLE_MS, PoolTracker
    tracker = PoolTracker(CFG, 0.001)
    t0 = 1_800_000_000_000
    for i in range(7_500):                                            # 12.5 simulated minutes at 10 updates/s
        tracker.update(make_book(t0 + i * 100, 149.95, 150.05, bid_pools=(BID_POOL,), ask_pools=ASK_POOLS), 0.5)
    pools = [tp for side in (Side.BID, Side.ASK) for tp in tracker.alive(side)]
    assert pools
    limit = CFG.history_retention_s * 1_000 // HISTORY_SAMPLE_MS + 2
    assert max(len(tp.history) for tp in pools) <= limit
    assert all(tp.history[0].t_ms >= t0 + 7_499 * 100 - CFG.history_retention_s * 1_000 - 1_000 for tp in pools)


def test_driver_memory_tail_is_bounded_when_a_limit_is_set():
    from lsrl.adapters.sim import SimExchange
    from lsrl.core.config import DEFAULT_CONFIG as CFG
    from lsrl.core.engine import LSRLEngine
    from lsrl.core.gates import NullRiskGate
    from lsrl.driver import SimDriver
    from lsrl.modes import Mode
    d = SimDriver(Mode.PAPER, LSRLEngine(CFG, RULES, risk_gate=NullRiskGate()), SimExchange(RULES, CFG),
                  start_nav_usdt=1_000.0, history_limit=5)
    d.run(long_sweep(extra_bars=((149.7, 149.9, 149.65, 149.8),) * 3))
    assert len(d.records) <= 5 and len(d.intents) <= 5
    assert d.engine.setup is not None                                  # decisions are unaffected by the memory tail
