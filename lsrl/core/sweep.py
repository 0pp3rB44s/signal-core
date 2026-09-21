"""Sweep detection: price interaction with a FROZEN pre-existing pool, and consumed-vs-cancelled
discrimination from EXECUTED VOLUME ONLY.

Owner decision (2026-09-20): consumption is never inferred from depth disappearance. Cancellation can
never satisfy the frozen >= 25 % consumption condition; only the public trade tape can.

Causality rules enforced here (events are processed in ARRIVAL order = what the bot could know):
  * the pool state used for the sweep is the LAST depth state in which the market touch was still
    strictly away from the pool (`best_bid > upper` for a BID pool, `best_ask < lower` for an ASK pool).
    Once the touch reaches the pool that state is frozen and never refreshed;
  * a pool must have qualified (age / strength / distance) AT that frozen state;
  * a pool that leaves the book while the touch is still away was cancelled, not swept;
  * CONSUMED = taker-side volume executed at price levels that belonged to the frozen pool, from the first
    interaction until the bar-close confirmation. Depth updates establish that the pool existed, its resting
    size, and (for the record only) cancellation / replenishment;
  * TRADE / DEPTH ORDERING: the two channels have independent latency. Exchange timestamps are the only
    common clock, so wherever arrival order and exchange-time order could disagree about the moment the
    market first touched the pool, the candidate is dropped as ORDER_AMBIGUOUS (NO SIGNAL). Nothing is
    inferred. The rules:
      R1  a trade at/through the pool must have an exchange time STRICTLY AFTER the frozen state's exchange
          time (a tie or an earlier time means the "pre-touch" state may not be pre-touch);
      R2  the same must hold for every trade counted as consumption;
      R3  when the first interaction was a TRADE, any depth update arriving afterwards whose exchange time
          is <= that trade's may belong BEFORE it (e.g. a cancellation) and the frozen state may be stale;
      R4  a taker trade of the wrong side at/through the pool contradicts the frozen book;
      R5  missing exchange timestamps make ordering unknowable.
    The age of the frozen state at the first interaction is recorded on every candidate so the residual
    ambiguity (a cancel within one update interval before the first print) is measurable.

    NO TOTAL ORDER IS ASSUMED. Bitget guarantees no order between the depth and the trade channel (independent
    latency: the depth update that reflects a trade is stamped a median ~50 ms AFTER it, tails -400/+500 ms), so an
    exchange-stamp inequality can never PROVE that a cancel came after a print. R1-R5 only refuse; what decides
    acceptance is order-free conservation of volume:
      R6  CONSERVATION. Every unit that left a frozen pool level while the market was interacting with it must be
          accounted for by executed volume at that price. A level that lost more than was executed there (a pull /
          cancel) cannot be told apart from 'cancelled first, then ordinary flow printed at those prices' without a
          cross-channel order that does not exist -> POOL_REMOVAL_NOT_EXPLAINED_BY_EXECUTION, NO SIGNAL. This needs no
          timestamp comparison between channels and no tuned margin.
  * BOOK COVERAGE (`lsrl.core.coverage`): a pool whose run ends at the outermost listed level may continue beyond the
    window; executed volume over a fragment overstates consumption. Not observed != absent: INSUFFICIENT_BOOK_COVERAGE.
  * bars never establish interaction. A bar that reached the pool while no depth/trade evidence of the
    interaction exists means the feed missed it: rejected, not inferred.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from lsrl.core.config import LSRLConfig
from lsrl.core.coverage import INSUFFICIENT_BOOK_COVERAGE, entry_coverage
from lsrl.core.pools import PoolState, PoolTracker, eligibility_failures
from lsrl.core.types import BarClosed, DepthSnapshot, Direction, Side, TradeTick

_KEY = 8
CONSERVATION_TOLERANCE = 1e-9      # exact: sizes are decimals; no tuned slack (any unexplained removal is ambiguity)


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
    """`consumed` is EXECUTED tape volume at frozen pool prices. The `book_*` fields are observability
    only (what the depth stream shows happened to the pool); they never enter the decision."""
    method: str                    # TAPE
    frozen_depth: float
    consumed: float
    removed_total: float           # book_removed_est: frozen size no longer resting (consumed + cancelled)
    cancelled_est: float           # removed_total not explained by executed volume
    fraction: float
    trades: int = 0
    residual_depth: float | None = None
    replenished_est: float = 0.0   # executed volume that the book no longer shows as removed
    max_removed: float = 0.0       # most that ever left the frozen levels while interacting (order-free)
    unexplained_removal: float = 0.0   # R6: that part of it NOT accounted for by executed volume at the same prices


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
    frozen_recv_ms: int | None = None
    frozen_exchange_ts_ms: int | None = None
    frozen_seq: int | None = None
    frozen_epoch: int = 0
    frozen_window: tuple[float, float] | None = None       # (bid span, ask span) of the reconstructed book at the freeze
    interaction_start_ms: int | None = None
    interaction_exchange_ts_ms: int | None = None
    interaction_source: str = ""
    pre_touch_state_age_ms: int | None = None
    interaction_bars: int = 0
    extreme: float | None = None
    tape_consumed: float = 0.0
    tape_trades: int = 0
    tape_first_ts_ms: int | None = None
    tape_last_ts_ms: int | None = None
    ambiguous_reason: str | None = None
    depth_series: list[tuple[int, float]] = field(default_factory=list)
    rejected_disappearance_logged: bool = False
    coverage: dict = field(default_factory=dict)             # entry coverage evidence at the freeze
    level_min: dict = field(default_factory=dict)            # price key -> smallest size seen at that frozen level since the freeze
    tape_by_level: dict = field(default_factory=dict)        # price key -> executed (wanted-side) volume at that level

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


def evaluate_consumption(w: PoolWatch, snap: DepthSnapshot | None) -> ConsumptionEstimate:
    """Consumption from executed volume only. The depth view supplies the (non-decisive) observability."""
    assert w.frozen is not None
    frozen = w.frozen
    depth = frozen.depth
    residual: float | None = None
    removed = 0.0
    if snap is not None:
        book = dict((_k(p), s) for p, s in (snap.bids if frozen.side is Side.BID else snap.asks))
        residual = sum(book.get(_k(p), 0.0) for p, _ in frozen.levels)
        removed = max(0.0, sum(max(0.0, s - book.get(_k(p), 0.0)) for p, s in frozen.levels))
    consumed = w.tape_consumed
    fraction = consumed / depth if depth > 0 else 0.0
    max_removed, unexplained = 0.0, 0.0
    now_book = None if snap is None else dict((_k(p), s) for p, s in (snap.bids if frozen.side is Side.BID else snap.asks))
    for price, size0 in frozen.levels:
        key = _k(price)
        low = w.level_min.get(key, size0)
        if now_book is not None:
            low = min(low, now_book.get(key, 0.0))
        gone = max(0.0, size0 - low)
        max_removed += gone
        unexplained += max(0.0, gone - w.tape_by_level.get(key, 0.0))
    return ConsumptionEstimate(
        method="TAPE", frozen_depth=depth, consumed=consumed, removed_total=removed,
        cancelled_est=max(0.0, removed - min(consumed, depth)), fraction=fraction, trades=w.tape_trades,
        residual_depth=residual, replenished_est=max(0.0, min(consumed, depth) - removed),
        max_removed=max_removed, unexplained_removal=unexplained)


def bar_touches(watch: PoolWatch, bar: BarClosed) -> bool:
    assert watch.frozen is not None
    if watch.side is Side.BID:
        return bar.low <= watch.frozen.upper
    return bar.high >= watch.frozen.lower


class SweepDetector:
    def __init__(self, cfg: LSRLConfig, tick_size: float, *, tape_available: bool = True) -> None:
        self.cfg = cfg
        self.tick = tick_size
        self.tape_available = tape_available
        self.watches: dict[str, PoolWatch] = {}

    # ---------------------------------------------------------------- helpers
    def _away(self, watch: PoolWatch, pool: PoolState, snap: DepthSnapshot) -> bool:
        if watch.side is Side.BID:
            return snap.best_bid > pool.upper
        return snap.best_ask < pool.lower

    def _start_interaction(self, w: PoolWatch, *, recv_ms: int, exchange_ts_ms: int | None, source: str) -> None:
        w.phase = WatchPhase.INTERACTING
        w.interaction_start_ms = recv_ms
        w.interaction_exchange_ts_ms = exchange_ts_ms
        w.interaction_source = source
        if w.frozen_recv_ms is not None:
            w.pre_touch_state_age_ms = recv_ms - w.frozen_recv_ms

    @staticmethod
    def _note_levels(w: PoolWatch, snap: DepthSnapshot) -> None:
        """R6 bookkeeping: the lowest size each frozen level has shown, INCLUDING the snapshot that starts the interaction
        (a pool pulled and then re-posted before the bar closes must not hide behind the final book)."""
        assert w.frozen is not None
        book = dict(((_k(p), s) for p, s in (snap.bids if w.side is Side.BID else snap.asks)))
        for p, size0 in w.frozen.levels:
            key = _k(p)
            w.level_min[key] = min(w.level_min.get(key, size0), book.get(key, 0.0))

    @staticmethod
    def _ambiguous(w: PoolWatch, reason: str) -> None:
        if w.ambiguous_reason is None:
            w.ambiguous_reason = reason

    def _in_pool(self, w: PoolWatch, price: float) -> bool:
        assert w.frozen is not None
        return any(abs(price - p) < self.tick / 2.0 for p, _ in w.frozen.levels)

    def reset(self) -> list[PoolWatch]:
        """The market view became uncertain: forget every watch. Returns the qualified ones that were
        mid-interaction so the caller can record why they were dropped."""
        dropped = [w for w in self.watches.values()
                   if w.phase is WatchPhase.INTERACTING and w.frozen is not None and not w.frozen_failures]
        self.watches = {}
        return dropped

    # ---------------------------------------------------------------- depth
    def on_snapshot(self, snap: DepthSnapshot, tracker: PoolTracker, atr15: float | None) -> list[dict]:
        """Refresh pre-touch state / detect book-level interaction.

        Returns observability notes (dicts with a `reason` key) for pools that disappeared before any
        price interaction."""
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
                assert w.frozen is not None
                # R3: a depth update that may belong BEFORE the first (trade) interaction
                if w.interaction_source == "TAPE" and (
                        snap.exchange_ts_ms is None or w.interaction_exchange_ts_ms is None
                        or snap.exchange_ts_ms <= w.interaction_exchange_ts_ms):
                    self._ambiguous(w, "R3_DEPTH_UPDATE_NOT_AFTER_FIRST_TRADE")
                book = dict(((_k(p), s) for p, s in (snap.bids if w.side is Side.BID else snap.asks)))
                w.depth_series.append((snap.t_ms, sum(book.get(_k(p), 0.0) for p, _ in w.frozen.levels)))
                del w.depth_series[:-120]
                self._note_levels(w, snap)                      # R6 bookkeeping: how low did each frozen level ever go?
                continue
            # ---- WATCHING
            if tp is not None:
                if self._away(w, tp.state, snap):
                    w.frozen = tp.state
                    w.frozen_mid = snap.mid
                    w.frozen_best_bid, w.frozen_best_ask = snap.best_bid, snap.best_ask
                    w.frozen_atr15 = atr15
                    w.frozen_in_range_since_ms = tp.in_range_since_ms
                    w.frozen_recv_ms, w.frozen_exchange_ts_ms = snap.t_ms, snap.exchange_ts_ms
                    w.frozen_seq, w.frozen_epoch = snap.seq, snap.epoch
                    w.frozen_window = (snap.bids[0][0] - snap.bids[-1][0], snap.asks[-1][0] - snap.asks[0][0])
                    w.frozen_failures = eligibility_failures(
                        tp.state, tp.first_seen_ms, snap.t_ms, snap.mid, atr15, self.cfg)
                    w.coverage = entry_coverage(tp.state, snap, atr15, self.cfg)
                    if w.coverage["entry_coverage_reason"]:            # NOT OBSERVED != ABSENT: a truncated pool never qualifies
                        w.frozen_failures = [INSUFFICIENT_BOOK_COVERAGE, *w.frozen_failures]
                    w.level_min = {_k(p): s for p, s in tp.state.levels}
                    w.tape_by_level = {}
                    w.depth_series.append((snap.t_ms, tp.state.depth))
                    del w.depth_series[:-24]
                elif w.frozen is not None and w.touch_reached(snap):
                    self._start_interaction(w, recv_ms=snap.t_ms, exchange_ts_ms=snap.exchange_ts_ms,
                                            source="BOOK")
                    self._note_levels(w, snap)
                continue
            # pool no longer in the book. If the market touch has reached it, the pool was traded into
            # (interaction); if the touch is still away, it was CANCELLED.
            if w.frozen is not None and w.touch_reached(snap):
                self._start_interaction(w, recv_ms=snap.t_ms, exchange_ts_ms=snap.exchange_ts_ms, source="BOOK")
                self._note_levels(w, snap)
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
            wanted = "SELL" if w.side is Side.BID else "BUY"
            reached = trade.price <= f.upper if w.side is Side.BID else trade.price >= f.lower
            if w.phase is WatchPhase.WATCHING:
                if not reached:
                    continue
                # first trade at/through the pool: R5 / R1 / R4 before it may start an interaction
                if trade.exchange_ts_ms is None or w.frozen_exchange_ts_ms is None:
                    self._ambiguous(w, "R5_EXCHANGE_TIME_UNKNOWN")
                elif trade.exchange_ts_ms <= w.frozen_exchange_ts_ms:
                    self._ambiguous(w, "R1_FIRST_TRADE_NOT_AFTER_FROZEN_STATE")
                if trade.aggressor.upper() != wanted:
                    self._ambiguous(w, "R4_WRONG_SIDE_TRADE_AT_POOL")
                self._start_interaction(w, recv_ms=trade.t_ms, exchange_ts_ms=trade.exchange_ts_ms, source="TAPE")
            elif reached and self._in_pool(w, trade.price):
                # R2: every counted trade must be strictly after the frozen state
                if (trade.exchange_ts_ms is None or w.frozen_exchange_ts_ms is None
                        or trade.exchange_ts_ms <= w.frozen_exchange_ts_ms):
                    self._ambiguous(w, "R2_POOL_TRADE_NOT_AFTER_FROZEN_STATE")
            w.tape_trades += 1
            if trade.aggressor.upper() == wanted and self._in_pool(w, trade.price):
                w.tape_consumed += trade.size
                level = min((p for p, _ in f.levels), key=lambda p: abs(p - trade.price))
                w.tape_by_level[_k(level)] = w.tape_by_level.get(_k(level), 0.0) + trade.size
                if w.tape_first_ts_ms is None:
                    w.tape_first_ts_ms = trade.exchange_ts_ms
                w.tape_last_ts_ms = trade.exchange_ts_ms
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
                # A bar never establishes interaction. If it reached a pool that the depth/trade
                # evidence says was not touched, the feed missed something: reject, do not infer.
                if w.first_seen_ms > bar.open_ts_ms or not bar_touches(w, bar):
                    continue
                if not w.frozen_failures:
                    outcomes.append(SweepOutcome("REJECTED", "BAR_TOUCH_WITHOUT_FEED_EVIDENCE", w, bar))
                w.phase = WatchPhase.DONE
                continue
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
            if not self.tape_available:
                outcomes.append(SweepOutcome("REJECTED", "TAPE_UNAVAILABLE", w, bar))
                w.phase = WatchPhase.DONE
                continue
            if w.ambiguous_reason:
                outcomes.append(SweepOutcome("REJECTED", "ORDER_AMBIGUOUS", w, bar,
                                             evaluate_consumption(w, snap)))
                w.phase = WatchPhase.DONE
                continue
            if snap is None or (bar.t_ms - snap.t_ms) / 1000.0 > self.cfg.max_snapshot_age_s:
                outcomes.append(SweepOutcome("REJECTED", "LIQUIDITY_DATA_STALE", w, bar))
                w.phase = WatchPhase.DONE
                continue
            est = evaluate_consumption(w, snap)
            boundary = boundary_of(w.frozen)
            reclaimed_bar = bar.close > boundary if w.side is Side.BID else bar.close < boundary
            if est.fraction >= self.cfg.sweep_min_consumed_fraction:
                if est.unexplained_removal > CONSERVATION_TOLERANCE:
                    outcomes.append(SweepOutcome("REJECTED", "POOL_REMOVAL_NOT_EXPLAINED_BY_EXECUTION", w, bar, est))
                else:
                    outcomes.append(SweepOutcome("CONFIRMED", "SWEEP_CONFIRMED", w, bar, est))
                w.phase = WatchPhase.DONE
            elif reclaimed_bar:
                outcomes.append(SweepOutcome("REJECTED", "TOUCH_WITHOUT_SWEEP", w, bar, est))
                w.phase = WatchPhase.DONE
            elif w.interaction_bars >= self.cfg.sweep_confirm_max_bars:
                outcomes.append(SweepOutcome("REJECTED", "SWEEP_NOT_CONFIRMED_TIMEOUT", w, bar, est))
                w.phase = WatchPhase.DONE
        return outcomes
