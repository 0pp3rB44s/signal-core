"""TrendFlow position lifecycle: targets, partials, trailing, invalidation, breakers.

**This exists because of PB-2.** In the MicroFlow path, target-hit was evaluated against the
scanner candle's high/low (`_target_hit_range` fed from `snapshot.primary.candles[-1]`), so a
position opened part-way through a candle inherited price action from before it existed — one
position was marked `tp1_hit` at a measured MFE of 1.34 bps against a 40 bps target.

`TargetTracker` cannot make that mistake by construction: it is seeded with the actual fill price
and fill timestamp, and `observe()` refuses any observation stamped at or before the fill. There is
no code path that accepts a candle range. That is the whole design constraint of this module.

Nothing here places orders. It decides what *should* happen; the execution layer does it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from strategies.trendflow_v1 import TrendFlowSpec


class LifecycleError(ValueError):
    """Ambiguous or impossible lifecycle state. Callers must fail closed."""


@dataclass
class TargetTracker:
    """Post-entry target state. Seeded from the fill, advanced only by post-fill observations."""

    direction: str
    entry_price: float
    fill_timestamp_ms: int
    initial_stop: float
    spec: TrendFlowSpec
    tp1_hit: bool = False
    tp2_hit: bool = False
    remaining_pct: float = 100.0
    stop_price: float = 0.0
    max_favourable_price: float = 0.0
    last_observation_ms: int = 0
    events: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.direction not in ("LONG", "SHORT"):
            raise LifecycleError("direction must be LONG or SHORT")
        if self.entry_price <= 0:
            raise LifecycleError("entry price must be positive")
        if self.fill_timestamp_ms <= 0:
            raise LifecycleError("a real fill timestamp is required")
        r = abs(self.entry_price - self.initial_stop)
        if r <= 0:
            raise LifecycleError("initial stop cannot equal the entry")
        self.stop_price = self.initial_stop
        self.max_favourable_price = self.entry_price

    # --- geometry ---------------------------------------------------------

    @property
    def r_distance(self) -> float:
        return abs(self.entry_price - self.initial_stop)

    def target_price(self, r_multiple: float) -> float:
        sign = 1.0 if self.direction == "LONG" else -1.0
        return self.entry_price + sign * r_multiple * self.r_distance

    def _favourable(self, price: float) -> bool:
        return (price > self.max_favourable_price if self.direction == "LONG"
                else price < self.max_favourable_price)

    def _reached(self, price: float, target: float) -> bool:
        return price >= target if self.direction == "LONG" else price <= target

    # --- the PB-2 guard ---------------------------------------------------

    def observe(self, *, price: float, timestamp_ms: int, atr14: float | None = None) -> list[str]:
        """Advance state on one post-entry observation.

        Observations at or before the fill are **rejected**, not ignored quietly: a caller feeding
        pre-entry data is a bug, and PB-2 is what happens when that bug is tolerated.
        """
        if timestamp_ms <= self.fill_timestamp_ms:
            raise LifecycleError(
                f"observation at {timestamp_ms} is not after the fill at {self.fill_timestamp_ms}; "
                "pre-entry price action may never advance target state (PB-2)")
        if timestamp_ms < self.last_observation_ms:
            raise LifecycleError("observations must not go backwards in time")
        if price <= 0:
            raise LifecycleError("price must be positive")
        self.last_observation_ms = timestamp_ms
        fired: list[str] = []

        if self._favourable(price):
            self.max_favourable_price = price

        if not self.tp1_hit and self._reached(price, self.target_price(self.spec.tp1_r)):
            self.tp1_hit = True
            self.remaining_pct -= self.spec.tp1_close_pct
            fired.append("TP1_HIT")
            # Break-even only after TP1, and only from post-entry action.
            self._tighten(self.entry_price)

        if self.tp1_hit and not self.tp2_hit and self._reached(price, self.target_price(self.spec.tp2_r)):
            self.tp2_hit = True
            self.remaining_pct -= self.spec.tp2_close_pct
            fired.append("TP2_HIT")

        if self.tp2_hit and atr14 and atr14 > 0:
            trail = (self.max_favourable_price - self.spec.trail_atr_multiple * atr14
                     if self.direction == "LONG"
                     else self.max_favourable_price + self.spec.trail_atr_multiple * atr14)
            if self._tighten(trail):
                fired.append("TRAIL_ADVANCED")

        if self._reached_stop(price):
            fired.append("STOP_HIT")
        self.events.extend(fired)
        return fired

    def _tighten(self, candidate: float) -> bool:
        """A stop may only ever move toward the position. Widening is impossible here."""
        if self.direction == "LONG":
            if candidate > self.stop_price:
                self.stop_price = candidate
                return True
        elif candidate < self.stop_price:
            self.stop_price = candidate
            return True
        return False

    def _reached_stop(self, price: float) -> bool:
        return price <= self.stop_price if self.direction == "LONG" else price >= self.stop_price

    # --- exits ------------------------------------------------------------

    def trend_invalidated(self, *, closed_1h_close: float, ema20_1h: float) -> bool:
        """Full exit on a closed 1H candle closing through the fast EMA against the position."""
        return (closed_1h_close < ema20_1h if self.direction == "LONG"
                else closed_1h_close > ema20_1h)

    def max_hold_exceeded(self, now_ms: int) -> bool:
        return (now_ms - self.fill_timestamp_ms) >= self.spec.max_hold_hours * 3_600_000


@dataclass
class RiskBreakers:
    """Halts new entries. It can never block an exit — that is asserted by test, not by comment."""

    spec: TrendFlowSpec
    day_start_equity: float
    week_start_equity: float
    daily_realized: float = 0.0
    weekly_realized: float = 0.0
    consecutive_losses: int = 0
    halted_until_4h_close_ms: int | None = None

    def record_trade(self, net_pnl_usdt: float) -> None:
        self.daily_realized += net_pnl_usdt
        self.weekly_realized += net_pnl_usdt
        self.consecutive_losses = self.consecutive_losses + 1 if net_pnl_usdt < 0 else 0

    def entries_blocked(self, *, now_ms: int = 0) -> str | None:
        if self.day_start_equity > 0:
            pct = self.daily_realized / self.day_start_equity * 100.0
            if pct <= self.spec.daily_halt_pct:
                return "TREND_FLOW_DAILY_HALT"
        if self.week_start_equity > 0:
            pct = self.weekly_realized / self.week_start_equity * 100.0
            if pct <= self.spec.weekly_halt_pct:
                return "TREND_FLOW_WEEKLY_HALT"
        if self.consecutive_losses >= self.spec.consecutive_losses_halt:
            if self.halted_until_4h_close_ms is None or now_ms < self.halted_until_4h_close_ms:
                return "TREND_FLOW_CONSECUTIVE_LOSS_HALT"
        return None


@dataclass(frozen=True)
class PortfolioState:
    open_directions: tuple[str, ...]
    committed_risk_pct: float

    def can_open(self, direction: str, spec: TrendFlowSpec) -> str | None:
        if len(self.open_directions) >= spec.max_positions:
            return "MAX_POSITIONS"
        # BTC, ETH and SOL move together; three same-side positions is one leveraged bet.
        if sum(1 for d in self.open_directions if d == direction) >= spec.max_positions:
            return "CORRELATED_DIRECTION_LIMIT"
        if self.committed_risk_pct >= spec.max_portfolio_risk_pct:
            return "PORTFOLIO_RISK_EXHAUSTED"
        return None
