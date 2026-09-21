"""BOOK COVERAGE: NOT OBSERVED != ABSENT (pure).

The WebSocket book lists a fixed number of levels per side, so it spans only part of the frozen 1.5 x ATR15 reach.
Everything the frozen definition concludes from the book is a statement about what is LISTED; three of those conclusions
would silently turn 'not observed' into 'absent' if the pool touches the outermost listed level:

  ENTRY   a pool is a maximal run of listed levels each >= 2.0 x the local median. A run that ends only because the list
          ends may continue beyond the window: its `depth` is a fragment, and executed volume divided by a fragment
          overstates the consumed fraction (a 5.3 % consumption of the true pool reads as 26.7 %). Such a pool is not
          complete and may not qualify.
  TARGET  TP1..TP3 are the near edges of the three nearest eligible opposite pools. A selected pool cut off by the edge of
          the window is not a complete pool; and 'fewer than three pools' can only be claimed for a region that was
          actually observed, so the reason is coverage, not absence.

No extrapolation, no synthetic depth, no candle proxy, and nothing here changes the reach, the strength definition, the
targets or any other frozen value: it only refuses to conclude from a truncated observation.

The persisted evidence (per candidate, accepted or rejected) is: visible lower/upper bound of the side, required
lower/upper bound (the frozen reach around the market), coverage fraction of the reach, whether the pool is complete,
and the coverage rejection reason.
"""
from __future__ import annotations

from typing import Any

from lsrl.core.config import LSRLConfig
from lsrl.core.pools import PoolState
from lsrl.core.types import DepthSnapshot, Direction, Side

INSUFFICIENT_BOOK_COVERAGE = "INSUFFICIENT_BOOK_COVERAGE"
_EPS = 1e-9


def side_levels(snap: DepthSnapshot, side: Side) -> tuple[tuple[float, float], ...]:
    return snap.bids if side is Side.BID else snap.asks


def visible_bounds(snap: DepthSnapshot, side: Side) -> tuple[float, float] | None:
    """(lower, upper) price of the listed levels of `side`; None when nothing is listed."""
    levels = side_levels(snap, side)
    if not levels:
        return None
    first, last = levels[0][0], levels[-1][0]              # levels are best-first, so the ends ARE the bounds: O(1) per snapshot
    return min(first, last), max(first, last)


def pool_is_truncated(state: PoolState, snap: DepthSnapshot) -> bool:
    """True when the run's far-edge level IS the outermost listed level of its side: the run ended because the
    list ended, so it may continue in the part of the book that is not observed. A pool followed by a listed
    non-pool level (which is what ends a run) is complete."""
    levels = side_levels(snap, state.side)
    if not levels:
        return True
    outermost = levels[-1][0]                       # levels are best-first: the last one is the farthest from the market
    far = state.lower if state.side is Side.BID else state.upper
    return abs(far - outermost) < _EPS


def _reach_coverage(snap: DepthSnapshot, side: Side, reach: float | None) -> dict[str, Any]:
    """How much of the frozen reach around the market the listed window covers on `side` (informational)."""
    bounds = visible_bounds(snap, side)
    mid = snap.mid
    out: dict[str, Any] = {"visible_lower": None if bounds is None else bounds[0],
                           "visible_upper": None if bounds is None else bounds[1],
                           "required_lower": None, "required_upper": None, "coverage_fraction": None}
    if bounds is None or reach is None or reach <= 0 or mid <= 0:
        return out
    if side is Side.BID:
        req_lo, req_hi = mid - reach, mid
    else:
        req_lo, req_hi = mid, mid + reach
    lo, hi = bounds
    if side is Side.BID:
        hi = max(hi, mid)              # the spread between the best bid and the mid is OBSERVED (empty), not unknown
    else:
        lo = min(lo, mid)
    covered = max(0.0, min(hi, req_hi) - max(lo, req_lo))
    out.update(required_lower=req_lo, required_upper=req_hi, coverage_fraction=min(1.0, covered / reach))
    return out


def entry_coverage(state: PoolState, snap: DepthSnapshot, atr15: float | None, cfg: LSRLConfig) -> dict[str, Any]:
    """Evidence + verdict for the ENTRY pool at one pre-touch snapshot."""
    reach = None if atr15 is None or atr15 <= 0 else cfg.pool_max_distance_atr15 * atr15
    ev = _reach_coverage(snap, state.side, reach)
    truncated = pool_is_truncated(state, snap)
    ev.update(entry_pool_complete=not truncated, entry_coverage_fraction=ev.pop("coverage_fraction"),
              entry_pool_far_edge=state.lower if state.side is Side.BID else state.upper,
              entry_coverage_reason=INSUFFICIENT_BOOK_COVERAGE if truncated else None)
    return ev


def target_coverage(direction: Direction, snap: DepthSnapshot, selected_pools: list[PoolState], n_eligible: int,
                    atr15: float | None, cfg: LSRLConfig) -> dict[str, Any]:
    """Evidence + verdict for the OPPOSITE-side search that selects TP1..TP3 from `snap`.

    * every selected pool must be complete (not cut off by the edge of the window);
    * with fewer than three eligible pools, 'no third pool' is only a statement about what was OBSERVED unless the
      window covers the whole frozen reach on that side -> coverage, not absence.
    The unobserved region beyond the third pool cannot change WHICH three pools are the nearest (it can only hold
    farther ones), so it is not required when three complete pools were found."""
    side = Side.ASK if direction is Direction.LONG else Side.BID
    reach = None if atr15 is None or atr15 <= 0 else cfg.pool_max_distance_atr15 * atr15
    ev = _reach_coverage(snap, side, reach)
    fraction = ev.pop("coverage_fraction")
    truncated = [p for p in selected_pools if pool_is_truncated(p, snap)]
    reach_covered = fraction is not None and fraction >= 1.0 - _EPS
    reason = None
    if truncated:
        reason = INSUFFICIENT_BOOK_COVERAGE
    elif n_eligible < 3 and not reach_covered:
        reason = INSUFFICIENT_BOOK_COVERAGE
    ev.update(target_coverage_fraction=fraction, target_pools_complete=not truncated, target_pools_selected=len(selected_pools),
              target_reach_covered=reach_covered, target_coverage_reason=reason)
    return ev
