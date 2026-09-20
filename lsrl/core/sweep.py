"""Sweep detection: price interaction with a FROZEN pre-existing pool plus
consumed-vs-cancelled discrimination.

Causality rules enforced here:
  * the pool state used for the sweep is the LAST snapshot in which the market
    touch was still strictly away from the pool (`best_bid > upper` for a BID
    pool, `best_ask < lower` for an ASK pool). Once the touch reaches the pool
    that state is frozen and never refreshed;
  * a pool must have qualified (age / strength / distance) AT that frozen state;
  * liquidity that disappears WITHOUT price interaction is never a sweep;
  * consumption is measured only from information received by the bar close.

Consumed vs cancelled:
  * with a healthy trade tape: consumed = aggressor volume traded inside the
    frozen pool range during the interaction window;
  * without a tape (documented fallback): consumed = frozen depth that has left
    the book at price levels the market traded through/at (level price beyond
    or equal to the sweep extreme). Depth removed at levels price never reached
    is counted as `cancelled`, never as consumed. This can over-count a level
    pulled just as price arrives; it is recorded as an estimate, with its method.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from lsrl.core.config import LSRLConfig
from lsrl.core.pools import PoolState, PoolTracker, eligibility_failures
from lsrl.core.types import BarClosed, DepthSnapshot, Direction, Side, TradeTick

_KEY = 8


def _k(price: float) -> float:
    return round(price, _KEY)


def direction_for(side: Side) -> Direction:
    return Direction.LONG if side is Side.BID else Direction.SHORT


def boundary_of(pool: PoolState) -> float:
    """The ORIGINAL reclaim boundary: upper edge of a BID pool, lower edge of an ASK pool."""
    return pool.upper if pool.side is Side.BID else pool.lower


class WatchPhase(str, Enum):
    WATCHING = "WATCHING"
    INTERACTING = "INTERACTING"
    DONE = "DONE"


@dataclass(frozen=True, slots=True)
class ConsumptionEstimate:
    method: str                    # TAPE | DEPTH_REMOVAL_PRICE_GATED
    frozen_depth: float
    consumed: float
    removed_total: float
    cancelled_est: float
    fraction: float


@dataclass(slots=True)
class PoolWatch:
    pool_id: str
    side: Side
    direction: Direction
    first_seen_ms: int
    phase: WatchPhase = WatchPhase.WATCHING
    frozen: PoolState | None = None
    frozen_failures: list[str] = field(default_factory=list)
    frozen_mid: float = 0.0
    frozen_atr15: float | None = None
    frozen_in_range_since_ms: int | None = None
    frozen_best_bid: float = 0.0
    frozen_best_ask: float = 0.0
    interaction_start_ms: int | None = None
    interaction_source: str = ""
    interaction_bars: int = 0
    extreme: float | None = None
    tape_consumed: float = 0.0
    tape_trades: int = 0
    depth_series: list[tuple[int, float]] = field(default_factory=list)
    rejected_disappearance_logged: bool = False

    def touch_reached(self, snap: DepthSnapshot) -> bool:
        assert self.frozen is not None
        if self.side is Side.BID:
            return snap.best_bid <= self.frozen.upper
        return snap.best_ask >= self.frozen.lower


@dataclass(slots=True)
class SweepOutcome:
    kind: str                      # CONFIRMED | REJECTED
    reason: str
    watch: PoolWatch
    bar: BarClosed
    consumption: ConsumptionEstimate | None = None


def estimate_consumption(frozen: PoolState, snap: DepthSnapshot, extreme: float,
                         tape_consumed: float, tape_used: bool) -> ConsumptionEstimate:
    book = dict(((_k(p), s) for p, s in (snap.bids if frozen.side is Side.BID else snap.asks)))
    consumed_snap = 0.0
    removed_total = 0.0
    for price, size in frozen.levels:
        removed = max(0.0, size - book.get(_k(price), 0.0))
        removed_total += removed
        reached = price >= extreme if frozen.side is Side.BID else price <= extreme
        if reached:
            consumed_snap += removed
    if tape_used:
        consumed = min(tape_consumed, frozen.depth)
        method = "TAPE"
    else:
        consumed = min(consumed_snap, frozen.depth)
        method = "DEPTH_REMOVAL_PRICE_GATED"
    fraction = consumed / frozen.depth if frozen.depth > 0 else 0.0
    return ConsumptionEstimate(method=method, frozen_depth=frozen.depth, consumed=consumed,
                               removed_total=removed_total,
                               cancelled_est=max(0.0, removed_total - consumed), fraction=fraction)


def bar_touches(watch: PoolWatch, bar: BarClosed) -> bool:
    assert watch.frozen is not None
    if watch.side is Side.BID:
        return bar.low <= watch.frozen.upper
    return bar.high >= watch.frozen.lower


class SweepDetector:
    def __init__(self, cfg: LSRLConfig, tick_size: float, *, tape_available: bool = False) -> None:
        self.cfg = cfg
        self.tick = tick_size
        self.tape_available = tape_available
        self.watches: dict[str, PoolWatch] = {}
        self.tape_ticks_since: dict[str, int] = {}

    # ---------------------------------------------------------------- helpers
    def _away(self, watch: PoolWatch, pool: PoolState, snap: DepthSnapshot) -> bool:
        if watch.side is Side.BID:
            return snap.best_bid > pool.upper
        return snap.best_ask < pool.lower

    def _start_interaction(self, w: PoolWatch, t_ms: int, source: str) -> None:
        w.phase = WatchPhase.INTERACTING
        w.interaction_start_ms = t_ms
        w.interaction_source = source

    # ---------------------------------------------------------------- snapshot
    def on_snapshot(self, snap: DepthSnapshot, tracker: PoolTracker, atr15: float | None) -> list[dict]:
        """Refresh pre-touch state / detect snapshot-level interaction.

        Returns observability notes (dicts with a `reason` key) for pools that
        disappeared before any price interaction."""
        notes: list[dict] = []
        if snap.crossed or not snap.bids or not snap.asks:
            return notes
        alive: dict[str, object] = {}
        for side in (Side.BID, Side.ASK):
            for tp in tracker.alive(side):
                alive[tp.pool_id] = tp
                if tp.pool_id not in self.watches:
                    self.watches[tp.pool_id] = PoolWatch(
                        pool_id=tp.pool_id, side=side, direction=direction_for(side),
                        first_seen_ms=tp.first_seen_ms)
        for pid in sorted(self.watches):
            w = self.watches[pid]
            tp = alive.get(pid)
            if w.phase is WatchPhase.DONE:
                continue
            if w.phase is WatchPhase.INTERACTING:
                # observe residual depth in the frozen range (a consumed pool has no tracker entry)
                assert w.frozen is not None
                book = dict(((_k(p), s) for p, s in (snap.bids if w.side is Side.BID else snap.asks)))
                w.depth_series.append((snap.t_ms, sum(book.get(_k(p), 0.0) for p, _ in w.frozen.levels)))
                del w.depth_series[:-120]
                continue
            # ---- WATCHING
            if tp is not None:
                if self._away(w, tp.state, snap):
                    w.frozen = tp.state
                    w.frozen_mid = snap.mid
                    w.frozen_best_bid, w.frozen_best_ask = snap.best_bid, snap.best_ask
                    w.frozen_atr15 = atr15
                    w.frozen_in_range_since_ms = tp.in_range_since_ms
                    w.frozen_failures = eligibility_failures(
                        tp.state, tp.first_seen_ms, snap.t_ms, snap.mid, atr15, self.cfg)
                    w.depth_series.append((snap.t_ms, tp.state.depth))
                    del w.depth_series[:-24]
                elif w.frozen is not None and w.touch_reached(snap):
                    self._start_interaction(w, snap.t_ms, "SNAPSHOT")
                continue
            # pool no longer in the book. If the market touch has reached it, the pool was
            # traded into (interaction); if the touch is still away, it was CANCELLED.
            if w.frozen is not None and w.touch_reached(snap):
                self._start_interaction(w, snap.t_ms, "SNAPSHOT")
                continue
            if w.frozen is not None and not w.frozen_failures and not w.rejected_disappearance_logged:
                w.rejected_disappearance_logged = True
                notes.append({"reason": "POOL_DISAPPEARED_BEFORE_INTERACTION", "pool_id": pid,
                              "side": w.side.value, "frozen": w.frozen.to_dict(),
                              "first_seen_ms": w.first_seen_ms})
            w.phase = WatchPhase.DONE
        # bounded memory: drop finished watches whose pool is gone
        for pid in [p for p, w in self.watches.items() if w.phase is WatchPhase.DONE and p not in alive]:
            del self.watches[pid]
        return notes

    # ---------------------------------------------------------------- tape
    def on_trade(self, trade: TradeTick) -> None:
        for w in self.watches.values():
            if w.phase is WatchPhase.DONE or w.frozen is None:
                continue
            f = w.frozen
            if w.phase is WatchPhase.WATCHING:
                touched = trade.price <= f.upper if w.side is Side.BID else trade.price >= f.lower
                if touched:
                    self._start_interaction(w, trade.t_ms, "TAPE")
                else:
                    continue
            in_range = f.lower <= trade.price <= f.upper
            wanted = "SELL" if w.side is Side.BID else "BUY"
            w.tape_trades += 1
            if in_range and trade.aggressor.upper() == wanted:
                w.tape_consumed += trade.size
            if w.extreme is None:
                w.extreme = trade.price
            elif w.side is Side.BID:
                w.extreme = min(w.extreme, trade.price)
            else:
                w.extreme = max(w.extreme, trade.price)

    # ---------------------------------------------------------------- bar close
    def on_bar_1m(self, bar: BarClosed, snap: DepthSnapshot | None) -> list[SweepOutcome]:
        outcomes: list[SweepOutcome] = []
        for pid in sorted(self.watches):
            w = self.watches[pid]
            if w.phase is WatchPhase.DONE or w.frozen is None:
                continue
            if w.phase is WatchPhase.WATCHING:
                # Causality: a bar that began BEFORE the pool was first observed cannot be
                # attributed to interaction with it (its extreme may predate the pool).
                if w.first_seen_ms > bar.open_ts_ms or not bar_touches(w, bar):
                    continue
                self._start_interaction(w, bar.open_ts_ms, "BAR")
            w.interaction_bars += 1
            bar_extreme = bar.low if w.side is Side.BID else bar.high
            if w.extreme is None:
                w.extreme = bar_extreme
            elif w.side is Side.BID:
                w.extreme = min(w.extreme, bar_extreme)
            else:
                w.extreme = max(w.extreme, bar_extreme)
            if w.frozen_failures:
                outcomes.append(SweepOutcome("REJECTED", w.frozen_failures[0], w, bar))
                w.phase = WatchPhase.DONE
                continue
            if snap is None or (bar.t_ms - snap.t_ms) / 1000.0 > self.cfg.max_snapshot_age_s:
                outcomes.append(SweepOutcome("REJECTED", "LIQUIDITY_DATA_STALE", w, bar))
                w.phase = WatchPhase.DONE
                continue
            tape_used = self.tape_available and w.tape_trades > 0
            est = estimate_consumption(w.frozen, snap, w.extreme, w.tape_consumed, tape_used)
            boundary = boundary_of(w.frozen)
            reclaimed_bar = bar.close > boundary if w.side is Side.BID else bar.close < boundary
            if est.fraction >= self.cfg.sweep_min_consumed_fraction:
                outcomes.append(SweepOutcome("CONFIRMED", "SWEEP_CONFIRMED", w, bar, est))
                w.phase = WatchPhase.DONE
            elif reclaimed_bar:
                outcomes.append(SweepOutcome("REJECTED", "TOUCH_WITHOUT_SWEEP", w, bar, est))
                w.phase = WatchPhase.DONE
            elif w.interaction_bars >= self.cfg.sweep_confirm_max_bars:
                outcomes.append(SweepOutcome("REJECTED", "SWEEP_NOT_CONFIRMED_TIMEOUT", w, bar, est))
                w.phase = WatchPhase.DONE
        return outcomes
