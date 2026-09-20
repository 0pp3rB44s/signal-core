"""Liquidity pool engine (point-in-time, causal).

A POOL is a maximal run of adjacent book levels on one side whose individual
size is >= MIN_RELATIVE_STRENGTH x the median level size of that side of the
snapshot ("local median"). Pools are derived ONLY from depth snapshots that had
already been received; nothing here ever consults a later candle or snapshot.

Identity: a pool observed in consecutive snapshots is the same pool when its
price range overlaps (within one tick). `first_seen_ms` is the receipt time of
the first snapshot in which it was observed, so pre-existence and age are
measured from real observation, never inferred. A pool that vanishes from a
snapshot is dead; if it reappears it is a NEW pool with a fresh age.

Operationalization decisions (documented in the implementation report):
  * "local median" = median of all listed level sizes on that side of the snapshot;
  * pool strength   = mean cluster level size / local median (>= 2.0 by construction);
  * distance        = distance from the snapshot mid to the pool's NEAR edge.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from lsrl.core.config import LSRLConfig
from lsrl.core.types import DepthSnapshot, Side

MIN_LEVELS_FOR_MEDIAN = 5


@dataclass(frozen=True, slots=True)
class PoolState:
    """One pool as observed in ONE snapshot."""
    side: Side
    t_ms: int
    lower: float
    upper: float
    depth: float
    n_levels: int
    strength: float
    median: float
    levels: tuple[tuple[float, float], ...]

    def near_edge(self) -> float:
        return self.upper if self.side is Side.BID else self.lower

    def far_edge(self) -> float:
        return self.lower if self.side is Side.BID else self.upper

    def to_dict(self) -> dict:
        return {"side": self.side.value, "t_ms": self.t_ms, "lower": self.lower,
                "upper": self.upper, "depth": self.depth, "n_levels": self.n_levels,
                "strength": round(self.strength, 6), "median": self.median,
                "levels": [list(x) for x in self.levels]}


def cluster_pools(levels: tuple[tuple[float, float], ...], side: Side, t_ms: int,
                  min_relative_strength: float) -> list[PoolState]:
    sizes = [s for _, s in levels if s > 0]
    if len(sizes) < MIN_LEVELS_FOR_MEDIAN:
        return []
    median = statistics.median(sizes)
    if median <= 0:
        return []
    threshold = min_relative_strength * median
    runs: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for price, size in levels:
        if size >= threshold and size > 0:
            current.append((price, size))
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    pools: list[PoolState] = []
    for run in runs:
        prices = [p for p, _ in run]
        depth = sum(s for _, s in run)
        pools.append(PoolState(
            side=side, t_ms=t_ms, lower=min(prices), upper=max(prices), depth=depth,
            n_levels=len(run), strength=(depth / len(run)) / median, median=median,
            levels=tuple(run),
        ))
    return pools


@dataclass(slots=True)
class TrackedPool:
    pool_id: str
    side: Side
    first_seen_ms: int
    last_seen_ms: int
    state: PoolState
    history: list[PoolState] = field(default_factory=list)
    alive: bool = True
    vanished_ms: int | None = None
    # OBSERVABLE ONLY (never gates): start of the current continuous stretch in
    # which the pool was within MAX_DISTANCE of price. Recorded so a reviewer can
    # judge later whether a stricter continuity reading would have mattered.
    in_range_since_ms: int | None = None

    def age_s(self, now_ms: int) -> float:
        return (now_ms - self.first_seen_ms) / 1000.0


def pool_distance(state: PoolState, mid: float) -> float:
    """Distance from `mid` to the pool's NEAR edge (0 once price is inside/through it)."""
    d = (mid - state.upper) if state.side is Side.BID else (state.lower - mid)
    return max(d, 0.0)


def _overlap(a: PoolState, b: PoolState, tolerance: float) -> float:
    return min(a.upper, b.upper) - max(a.lower, b.lower) + tolerance


class PoolTracker:
    def __init__(self, cfg: LSRLConfig, tick_size: float) -> None:
        self.cfg = cfg
        self.tick = tick_size
        self._alive: dict[Side, list[TrackedPool]] = {Side.BID: [], Side.ASK: []}
        self.last_snapshot: DepthSnapshot | None = None
        self._last_valid_t_ms: int | None = None

    # ---------------------------------------------------------------- update
    def update(self, snap: DepthSnapshot, atr15: float | None = None) -> list[TrackedPool]:
        """Advance with one snapshot. Returns pools that VANISHED in this snapshot."""
        vanished: list[TrackedPool] = []
        self.last_snapshot = snap
        if (self._last_valid_t_ms is not None
                and (snap.t_ms - self._last_valid_t_ms) / 1000.0 > self.cfg.max_snapshot_age_s):
            # Pool age means CONTINUOUS observation. A gap longer than the staleness limit
            # ends every pool's observation history: nothing is carried across it.
            for pools in self._alive.values():
                for tp in pools:
                    tp.alive, tp.vanished_ms = False, snap.t_ms
                    vanished.append(tp)
            self._alive = {Side.BID: [], Side.ASK: []}
        if snap.crossed or not snap.bids or not snap.asks:
            # A crossed or one-sided book is not a valid point-in-time picture:
            # treat every pool as unobserved this instant (it is NOT marked
            # vanished, but it also is not refreshed) -- the caller's staleness
            # rules then fail closed if this persists.
            return vanished
        self._last_valid_t_ms = snap.t_ms
        for side, levels in ((Side.BID, snap.bids), (Side.ASK, snap.asks)):
            clusters = cluster_pools(levels, side, snap.t_ms, self.cfg.pool_min_relative_strength)
            tracked = self._alive[side]
            unmatched = list(range(len(clusters)))
            matched_ids: set[str] = set()
            pairs: list[tuple[float, int, int]] = []
            for ti, tp in enumerate(tracked):
                for ci, cl in enumerate(clusters):
                    ov = _overlap(tp.state, cl, self.tick)
                    if ov >= 0:
                        pairs.append((-ov, ti, ci))
            pairs.sort()
            used_t: set[int] = set()
            used_c: set[int] = set()
            for _neg_ov, ti, ci in pairs:
                if ti in used_t or ci in used_c:
                    continue
                used_t.add(ti)
                used_c.add(ci)
                tp = tracked[ti]
                tp.state = clusters[ci]
                tp.last_seen_ms = snap.t_ms
                tp.history.append(clusters[ci])
                matched_ids.add(tp.pool_id)
            still: list[TrackedPool] = []
            for tp in tracked:
                if tp.pool_id in matched_ids:
                    still.append(tp)
                else:
                    tp.alive = False
                    tp.vanished_ms = snap.t_ms
                    vanished.append(tp)
            for ci in unmatched:
                if ci in used_c:
                    continue
                cl = clusters[ci]
                pid = f"{side.value}-{snap.t_ms}-{cl.lower}-{cl.upper}"
                still.append(TrackedPool(pool_id=pid, side=side, first_seen_ms=snap.t_ms,
                                         last_seen_ms=snap.t_ms, state=cl, history=[cl]))
            for tp in still:
                in_range = (atr15 is not None and atr15 > 0 and snap.mid > 0 and
                            pool_distance(tp.state, snap.mid) <= self.cfg.pool_max_distance_atr15 * atr15)
                if in_range:
                    if tp.in_range_since_ms is None:
                        tp.in_range_since_ms = snap.t_ms
                else:
                    tp.in_range_since_ms = None
            self._alive[side] = still
        cutoff = snap.t_ms - self.cfg.history_retention_s * 1000
        for pools in self._alive.values():
            for tp in pools:
                if len(tp.history) > 1 and tp.history[0].t_ms < cutoff:
                    tp.history = [h for h in tp.history if h.t_ms >= cutoff] or [tp.state]
        return vanished

    # ---------------------------------------------------------------- queries
    def alive(self, side: Side) -> list[TrackedPool]:
        return list(self._alive[side])

    def get(self, pool_id: str) -> TrackedPool | None:
        for pools in self._alive.values():
            for tp in pools:
                if tp.pool_id == pool_id:
                    return tp
        return None


def eligibility_failures(state: PoolState, first_seen_ms: int, now_ms: int, mid: float,
                         atr15: float | None, cfg: LSRLConfig) -> list[str]:
    """Reasons a pool fails the LSRL V1 pool rules at `now_ms` ([] = qualifies).

    Literal reading of the frozen spec: pre-existence and age are measured from
    the pool's first observation; distance is measured at `now_ms`. (Distance
    evaluated at the last pre-touch snapshot is weak for a slow approach; the
    in-range duration is therefore recorded as an observable, not gated on.)
    """
    failures: list[str] = []
    age_s = (now_ms - first_seen_ms) / 1000.0
    if age_s < cfg.pool_min_preexistence_s:
        failures.append("POOL_TOO_YOUNG")
    if age_s > cfg.pool_max_age_s:
        failures.append("POOL_TOO_OLD")
    if state.strength < cfg.pool_min_relative_strength:
        failures.append("POOL_TOO_WEAK")
    if atr15 is None or atr15 <= 0:
        failures.append("ATR15_UNAVAILABLE")
    elif mid <= 0:
        failures.append("MID_UNAVAILABLE")
    elif pool_distance(state, mid) > cfg.pool_max_distance_atr15 * atr15:
        failures.append("POOL_TOO_FAR")
    return failures
