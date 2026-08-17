"""V0 position lifecycle. Starts at the confirmed fill and nowhere else.

PB-2 is the reason this file is separate and this small: in MicroFlow, pre-entry candle range was
allowed to set post-entry target state, and a position was marked as having hit a 40 bps target at a
measured excursion of 1.34 bps. Here the tracker is constructed from the *actual* fill price and
fill timestamp, and `observe()` raises on any observation stamped at or before it. Nothing before
the fill can touch TP, SL, MFE or MAE.

There is exactly one exit path per outcome: TP closes 100%, SL closes 100%, and a 48 h fail-safe
closes whatever remains. No partials, no break-even, no trailing — a second exit mechanism would
blur the only question V0 exists to answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from strategies.simple_trend_reversal_v0 import STRATEGY_ID, V0Spec


class V0LifecycleError(ValueError):
    """Ambiguous or impossible lifecycle state. Callers must fail closed."""


@dataclass
class V0Position:
    symbol: str
    side: str
    signal_candle_ts: int
    signal_price: float
    fill_ts: int
    fill_price: float
    initial_stop: float
    take_profit: float
    position_size: float
    risk_usdt: float
    spec: V0Spec
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
            raise V0LifecycleError("side must be LONG or SHORT")
        if self.fill_price <= 0 or self.position_size <= 0:
            raise V0LifecycleError("fill price and size must be positive")
        if self.fill_ts <= 0:
            raise V0LifecycleError("a real fill timestamp is required")
        # Targets are recomputed from the ACTUAL fill, so slippage cannot leave a stale 2R.
        r = abs(self.fill_price - self.initial_stop)
        if r <= 0:
            raise V0LifecycleError("stop cannot equal the fill price")
        if self.side == "LONG" and self.initial_stop >= self.fill_price:
            raise V0LifecycleError("LONG stop must sit below the fill")
        if self.side == "SHORT" and self.initial_stop <= self.fill_price:
            raise V0LifecycleError("SHORT stop must sit above the fill")

    @property
    def initial_r(self) -> float:
        return abs(self.fill_price - self.initial_stop)

    def recomputed_take_profit(self) -> float:
        sign = 1.0 if self.side == "LONG" else -1.0
        return self.fill_price + sign * self.spec.reward_multiple * self.initial_r

    def observe(self, *, high: float, low: float, timestamp_ms: int) -> str | None:
        """One post-fill observation. Returns an exit reason, or None.

        An observation at or before the fill is **refused**, not ignored: a caller feeding
        pre-entry data is a bug, and tolerating it quietly is exactly how PB-2 happened.
        """
        if timestamp_ms <= self.fill_ts:
            raise V0LifecycleError(
                f"observation at {timestamp_ms} is not after the fill at {self.fill_ts}; "
                "pre-fill price action may never touch TP, SL, MFE or MAE (PB-2)")
        if timestamp_ms < self.last_observation_ms:
            raise V0LifecycleError("observations must not go backwards in time")
        if high < low or low <= 0:
            raise V0LifecycleError("impossible observation geometry")
        if self.closed:
            return self.exit_reason
        self.last_observation_ms = timestamp_ms

        if self.side == "LONG":
            self.mfe_bps = max(self.mfe_bps, (high - self.fill_price) / self.fill_price * 10_000.0)
            self.mae_bps = min(self.mae_bps, (low - self.fill_price) / self.fill_price * 10_000.0)
            hit_sl, hit_tp = low <= self.initial_stop, high >= self.take_profit
        else:
            self.mfe_bps = max(self.mfe_bps, (self.fill_price - low) / self.fill_price * 10_000.0)
            self.mae_bps = min(self.mae_bps, (self.fill_price - high) / self.fill_price * 10_000.0)
            hit_sl, hit_tp = high >= self.initial_stop, low <= self.take_profit

        # When one candle spans both, the stop is assumed first. Optimism here would flatter
        # every replay it touches.
        if hit_sl:
            return self._close("SL", self.initial_stop)
        if hit_tp:
            return self._close("TP", self.take_profit)
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
        return sign * (self.exit_price - self.fill_price) / self.initial_r


@dataclass
class V0Portfolio:
    """Concurrency and the daily breaker. It gates entries and can never gate an exit."""

    spec: V0Spec
    day_start_equity: float
    open_symbols: tuple[str, ...] = ()
    daily_realized: float = 0.0

    def record_close(self, net_pnl_usdt: float) -> None:
        self.daily_realized += net_pnl_usdt

    def entry_blocked(self, symbol: str) -> str | None:
        if symbol in self.open_symbols:
            return "POSITION_ALREADY_OPEN"          # one position per symbol, no pyramiding
        if len(self.open_symbols) >= self.spec.max_concurrent_positions:
            return "MAX_CONCURRENT_POSITIONS"
        if self.day_start_equity > 0:
            pct = self.daily_realized / self.day_start_equity * 100.0
            if pct <= -self.spec.max_daily_loss_pct:
                return "DAILY_LOSS_LIMIT"
        return None


@dataclass
class V0SignalLedger:
    """Idempotency. A given reclaim candle may produce at most one entry, ever."""

    seen: set[tuple[str, str, str, int]] = field(default_factory=set)

    def key(self, *, strategy_id: str, symbol: str, side: str, signal_candle_ts: int):
        return (strategy_id, symbol, side, signal_candle_ts)

    def already_taken(self, **kw) -> bool:
        return self.key(**kw) in self.seen

    def record(self, **kw) -> None:
        self.seen.add(self.key(**kw))
