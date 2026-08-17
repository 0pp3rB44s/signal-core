"""ExpansionFlow V1 execution and position lifecycle.

Two things here are load-bearing and everything else is bookkeeping.

**Maker-first with no taker fallback.** `MakerOrder` models a post-only order with a TTL, at most
one reprice, and then cancellation. There is deliberately no path that crosses the spread — the
project's own evidence is that turnover, not signal, destroyed the previous strategies. A missed
trade costs nothing; a chased one costs the spread plus the adverse selection that follows it.

**The lifecycle starts at the confirmed fill.** PB-2 was caused by pre-entry candle range setting
post-entry target state. `ExpansionPosition.observe()` raises on any observation at or before the
fill rather than ignoring it, and a position restored after a restart is held to the same rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from strategies.expansionflow_v1 import STRATEGY_ID, ExpansionSpec


class ExpansionLifecycleError(ValueError):
    """Ambiguous or impossible state. Callers must fail closed."""


@dataclass
class MakerOrder:
    """Post-only, TTL-bounded, one reprice, then gone. No taker fallback exists."""

    symbol: str
    side: str
    desired_price: float
    quantity: float
    created_ms: int
    spec: ExpansionSpec
    atr15: float
    post_only: bool = True
    reprices: int = 0
    status: str = "WORKING"          # WORKING | FILLED | CANCELLED
    filled_price: float | None = None
    filled_ms: int | None = None
    filled_qty: float = 0.0
    cancel_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.post_only:
            raise ExpansionLifecycleError("ExpansionFlow entries must be post-only")
        if self.side not in ("LONG", "SHORT"):
            raise ExpansionLifecycleError("side must be LONG or SHORT")
        if self.quantity <= 0 or self.desired_price <= 0:
            raise ExpansionLifecycleError("quantity and price must be positive")

    @property
    def ttl_seconds(self) -> int:
        return self.spec.order_ttl_seconds if self.reprices == 0 else self.spec.reprice_ttl_seconds

    def expired(self, now_ms: int) -> bool:
        return (now_ms - self.created_ms) >= self.ttl_seconds * 1000

    def would_cross(self, *, best_bid: float, best_ask: float) -> bool:
        """A post-only order that would take liquidity must never be sent."""
        return (self.desired_price >= best_ask if self.side == "LONG"
                else self.desired_price <= best_bid)

    def fill(self, *, price: float, quantity: float, timestamp_ms: int) -> None:
        if self.status != "WORKING":
            raise ExpansionLifecycleError("cannot fill an order that is not working")
        if quantity <= 0 or quantity > self.quantity + 1e-12:
            raise ExpansionLifecycleError("fill quantity outside the order")
        self.filled_price = price
        self.filled_ms = timestamp_ms
        self.filled_qty = quantity
        self.status = "FILLED"

    def reprice(self, *, new_price: float, now_ms: int, setup_still_valid: bool) -> bool:
        """At most one reprice, only if the setup holds and the price has not run away."""
        if self.reprices >= self.spec.max_reprices:
            self.cancel("MAX_REPRICES")
            return False
        if not setup_still_valid:
            self.cancel("SETUP_INVALIDATED")
            return False
        drift = abs(new_price - self.desired_price)
        if drift > self.spec.reprice_tolerance_atr * self.atr15:
            self.cancel("REPRICE_BEYOND_TOLERANCE")
            return False
        self.desired_price = new_price
        self.created_ms = now_ms
        self.reprices += 1
        return True

    def cancel(self, reason: str) -> None:
        if self.status == "WORKING":
            self.status = "CANCELLED"
            self.cancel_reason = reason


@dataclass
class ExpansionPosition:
    """Structural stop, 2R target, 48 h fail-safe. No partials, break-even or trailing in V1."""

    symbol: str
    side: str
    setup_ts: int
    fill_ts: int
    fill_price: float
    quantity: float
    initial_stop: float
    spec: ExpansionSpec
    risk_usdt: float = 0.0
    position_lifecycle_id: str = ""
    strategy_id: str = STRATEGY_ID
    closed: bool = False
    exit_reason: str | None = None
    exit_price: float | None = None
    mfe_bps: float = 0.0
    mae_bps: float = 0.0
    last_observation_ms: int = 0
    events: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.side not in ("LONG", "SHORT"):
            raise ExpansionLifecycleError("side must be LONG or SHORT")
        if self.fill_price <= 0 or self.quantity <= 0:
            raise ExpansionLifecycleError("fill price and quantity must be positive")
        if self.fill_ts <= 0:
            raise ExpansionLifecycleError("a real fill timestamp is required")
        if self.side == "LONG" and self.initial_stop >= self.fill_price:
            raise ExpansionLifecycleError("LONG stop must sit below the fill")
        if self.side == "SHORT" and self.initial_stop <= self.fill_price:
            raise ExpansionLifecycleError("SHORT stop must sit above the fill")

    @property
    def r_distance(self) -> float:
        return abs(self.fill_price - self.initial_stop)

    @property
    def target(self) -> float:
        """Always from the ACTUAL fill, so slippage cannot leave a stale 2R."""
        sign = 1.0 if self.side == "LONG" else -1.0
        return self.fill_price + sign * self.spec.reward_multiple * self.r_distance

    def observe(self, *, high: float, low: float, timestamp_ms: int) -> str | None:
        if timestamp_ms <= self.fill_ts:
            raise ExpansionLifecycleError(
                f"observation at {timestamp_ms} is not after the fill at {self.fill_ts}; "
                "pre-fill price action may never touch TP, SL, MFE or MAE (PB-2)")
        if timestamp_ms < self.last_observation_ms:
            raise ExpansionLifecycleError("observations must not go backwards in time")
        if high < low or low <= 0:
            raise ExpansionLifecycleError("impossible observation geometry")
        if self.closed:
            return self.exit_reason
        self.last_observation_ms = timestamp_ms
        if self.side == "LONG":
            self.mfe_bps = max(self.mfe_bps, (high - self.fill_price) / self.fill_price * 10_000.0)
            self.mae_bps = min(self.mae_bps, (low - self.fill_price) / self.fill_price * 10_000.0)
            hit_sl, hit_tp = low <= self.initial_stop, high >= self.target
        else:
            self.mfe_bps = max(self.mfe_bps, (self.fill_price - low) / self.fill_price * 10_000.0)
            self.mae_bps = min(self.mae_bps, (self.fill_price - high) / self.fill_price * 10_000.0)
            hit_sl, hit_tp = high >= self.initial_stop, low <= self.target
        # A bar spanning both resolves as the stop. Optimism flatters every replay it touches.
        if hit_sl:
            return self._close("SL", self.initial_stop)
        if hit_tp:
            return self._close("TARGET_2R", self.target)
        return None

    def check_max_hold(self, now_ms: int, *, mark_price: float) -> str | None:
        if self.closed:
            return self.exit_reason
        if now_ms - self.fill_ts >= self.spec.max_hold_hours * 3_600_000:
            return self._close("MAX_HOLD_48H", mark_price)
        return None

    def _close(self, reason: str, price: float) -> str:
        self.closed = True
        self.exit_reason = reason
        self.exit_price = price
        self.events.append(reason)
        return reason

    def realized_r(self) -> float:
        if not self.closed or self.exit_price is None:
            return 0.0
        sign = 1.0 if self.side == "LONG" else -1.0
        return sign * (self.exit_price - self.fill_price) / self.r_distance


@dataclass
class ExpansionPortfolio:
    """Concurrency, portfolio risk and the daily breaker. Gates entries, never exits."""

    spec: ExpansionSpec
    day_start_equity: float
    open_symbols: tuple[str, ...] = ()
    committed_risk_pct: float = 0.0
    daily_realized: float = 0.0

    def record_close(self, net_pnl_usdt: float) -> None:
        self.daily_realized += net_pnl_usdt

    def entry_blocked(self, symbol: str) -> str | None:
        if symbol in self.open_symbols:
            return "POSITION_ALREADY_OPEN"
        if len(self.open_symbols) >= self.spec.max_concurrent_positions:
            return "MAX_CONCURRENT_POSITIONS"
        if self.committed_risk_pct >= self.spec.max_total_initial_risk_pct:
            return "PORTFOLIO_RISK_EXHAUSTED"
        if self.day_start_equity > 0:
            pct = self.daily_realized / self.day_start_equity * 100.0
            if pct <= -self.spec.max_daily_loss_pct:
                return "DAILY_LOSS_LIMIT"
        return None
