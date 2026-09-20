"""Take-profit targets from PRE-EXISTING opposite-side liquidity pools (pure).

LONG  -> upside liquidity  (ASK pools); SHORT -> downside liquidity (BID pools).
A target pool must satisfy the same V1 pool rules as any pool (age, strength,
distance) at the decision time, so it is by construction pre-existing and
observed causally. TP1/TP2/TP3 are the 1st/2nd/3rd nearest qualifying pools.

Operationalization decisions (flagged in the report): the target price is the
pool's NEAR edge (closest to entry = most conservative reward), rounded TOWARD
the entries; ALL THREE targets must exist or the setup is a NO-TRADE.
"""
from __future__ import annotations

from dataclasses import dataclass

from lsrl.core.config import LSRLConfig
from lsrl.core.pools import PoolTracker, eligibility_failures
from lsrl.core.rounding import round_to_tick
from lsrl.core.types import DepthSnapshot, Direction, Side, SymbolRules


@dataclass(frozen=True, slots=True)
class Target:
    level: str
    price: float
    pool_id: str
    pool_strength: float
    pool_depth: float
    pool_age_s: float


def select_targets(direction: Direction, tracker: PoolTracker, snap: DepthSnapshot, now_ms: int,
                   atr15: float | None, first_entry: float, rules: SymbolRules,
                   cfg: LSRLConfig) -> tuple[list[Target] | None, str]:
    side = Side.ASK if direction is Direction.LONG else Side.BID
    sgn = direction.sgn
    qualifying = []
    for tp in tracker.alive(side):
        if eligibility_failures(tp.state, tp.first_seen_ms, now_ms, snap.mid, atr15, cfg):
            continue
        edge = tp.state.near_edge()
        # must sit on the profit side of the market AND beyond the first entry
        if sgn * (edge - snap.mid) <= 0 or sgn * (edge - first_entry) <= 0:
            continue
        qualifying.append((sgn * edge, tp))
    qualifying.sort(key=lambda x: (x[0], x[1].pool_id))
    if len(qualifying) < 3:
        return None, "INSUFFICIENT_OPPOSITE_POOLS"
    rounding = "down" if direction is Direction.LONG else "up"     # toward entries
    targets: list[Target] = []
    for level, (_key, tp) in zip(("TP1", "TP2", "TP3"), qualifying[:3]):
        price = round_to_tick(tp.state.near_edge(), rules.tick_size, rounding)
        targets.append(Target(level, price, tp.pool_id, tp.state.strength, tp.state.depth,
                              tp.age_s(now_ms)))
    if not all(sgn * (b.price - a.price) > 0 for a, b in zip(targets, targets[1:])):
        return None, "TARGETS_NOT_DISTINCT"
    if sgn * (targets[0].price - first_entry) <= 0:
        return None, "TARGETS_NOT_BEYOND_ENTRY"
    return targets, ""


def rr_to_tp2(direction: Direction, avg_entry: float, stop: float, tp2: float) -> float:
    risk = abs(avg_entry - stop)
    if risk <= 0:
        return 0.0
    return direction.sgn * (tp2 - avg_entry) / risk
