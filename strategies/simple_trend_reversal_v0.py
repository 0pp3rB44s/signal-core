"""Simple Trend Reversal V0 — trend → pullback → reclaim → 2R.

Deliberately small. MicroFlow and TrendFlow both failed while carrying a lot of machinery, and the
machinery made it hard to say *which part* was wrong. V0 has one entry idea, one stop rule, one
target, and one fail-safe, so if it fails the failure is attributable.

There is no scoring model, no volatility filter, no volume gate, no order-book input, no partials,
no break-even and no trailing. That is not an oversight — a second exit mechanism would blur the
only question this is built to answer: does trend → pullback → reclaim → 2R have positive
expectancy?

Two rules carried forward because they were paid for in production:

* **Closed candles only.** An entry decision on an unfinished candle is a decision on data that can
  still change.
* **The lifecycle starts at the confirmed fill.** PB-2 was caused by pre-entry candle range being
  allowed to set post-entry target state; here nothing before `fill_ts` can touch a target, a stop,
  MFE or MAE, and the tracker raises rather than ignoring such an observation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

STRATEGY_ID = "simple_trend_reversal_v0"
SPEC_ID = "simple-trend-reversal-v0-20260817"


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    closed: bool = True


@dataclass(frozen=True)
class V0Spec:
    ema_fast: int = 20
    ema_slow: int = 50
    pullback_lookback: int = 3
    reward_multiple: float = 2.0
    max_hold_hours: float = 48.0
    risk_pct_equity: float = 0.25
    max_concurrent_positions: int = 3
    max_daily_loss_pct: float = 1.0
    strategy_id: str = STRATEGY_ID
    spec_id: str = SPEC_ID

    @property
    def min_candles(self) -> int:
        return self.ema_slow + self.pullback_lookback + 2


class V0DataError(ValueError):
    """Stale, unfinished or malformed input. Callers must fail closed."""


@dataclass(frozen=True)
class V0Signal:
    direction: str | None
    signal_candle_ts: int | None
    signal_price: float | None
    stop_price: float | None
    take_profit: float | None
    r_distance: float | None
    block_reason: str | None
    values: dict[str, float] = field(default_factory=dict)
    strategy_id: str = STRATEGY_ID
    spec_id: str = SPEC_ID

    @property
    def is_entry(self) -> bool:
        return self.direction is not None


def ema(values: list[float], period: int) -> list[float]:
    if period <= 0:
        raise V0DataError("ema period must be positive")
    if len(values) < period:
        raise V0DataError(f"ema{period} needs {period} values, got {len(values)}")
    k = 2.0 / (period + 1.0)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def _validate(candles: list[Candle], spec: V0Spec) -> None:
    if len(candles) < spec.min_candles:
        raise V0DataError(f"need {spec.min_candles} closed candles, got {len(candles)}")
    if any(not c.closed for c in candles):
        raise V0DataError("an unfinished candle was supplied; entries require closed candles")
    for prev, cur in zip(candles, candles[1:]):
        if cur.open_time_ms <= prev.open_time_ms:
            raise V0DataError("candles are not strictly ordered")
    for c in candles:
        if c.high < c.low or c.close <= 0 or c.high <= 0 or c.low <= 0:
            raise V0DataError("impossible candle geometry")


def evaluate(candles: list[Candle], spec: V0Spec) -> V0Signal:
    """One direction or none, from closed candles only. Never raises on a rejected setup."""
    _validate(candles, spec)
    closes = [c.close for c in candles]
    e_fast = ema(closes, spec.ema_fast)[-1]
    e_slow = ema(closes, spec.ema_slow)[-1]
    last = candles[-1]
    window = candles[-spec.pullback_lookback:]
    values = {"ema20": e_fast, "ema50": e_slow, "close": last.close, "open": last.open}

    def blocked(reason: str) -> V0Signal:
        return V0Signal(None, last.open_time_ms, last.close, None, None, None, reason, values)

    # A. trend. Equality is not a trend — it is deterministically no-trade.
    if e_fast > e_slow:
        side = "LONG"
    elif e_fast < e_slow:
        side = "SHORT"
    else:
        return blocked("NO_TREND_EMA_EQUAL")

    # B. pullback: price must have reached the fast EMA within the lookback.
    pullback = (any(c.low <= e_fast for c in window) if side == "LONG"
                else any(c.high >= e_fast for c in window))
    values["pullback_detected"] = float(pullback)
    if not pullback:
        return blocked("NO_PULLBACK")

    # C. reclaim: the last closed candle closes back through the EMA, in the trend's colour.
    reclaim = ((last.close > e_fast and last.close > last.open) if side == "LONG"
               else (last.close < e_fast and last.close < last.open))
    values["reclaim_detected"] = float(reclaim)
    if not reclaim:
        return blocked("NO_RECLAIM")

    # Structural stop: the extreme of the pullback/reclaim sequence, known at entry time.
    swing = min(c.low for c in window) if side == "LONG" else max(c.high for c in window)
    entry = last.close
    values["swing_price"] = swing
    if (side == "LONG" and swing >= entry) or (side == "SHORT" and swing <= entry):
        return blocked("INVALID_STOP_WRONG_SIDE")
    r = abs(entry - swing)
    if r <= 0:
        return blocked("INVALID_STOP_ZERO_DISTANCE")

    tp = entry + spec.reward_multiple * r if side == "LONG" else entry - spec.reward_multiple * r
    values.update({"stop_distance_bps": r / entry * 10_000.0, "planned_R": r, "planned_TP": tp})
    return V0Signal(side, last.open_time_ms, entry, swing, tp, r, None, values)


@dataclass(frozen=True)
class V0Sizing:
    quantity: float
    notional: float
    risk_usdt: float
    refused_reason: str | None = None


def size_position(*, entry: float, stop: float, equity_usdt: float, spec: V0Spec,
                  min_qty: float, qty_step: float, min_notional: float,
                  max_notional: float | None = None) -> V0Sizing:
    """Risk on equity, never a fixed notional. The stop is an input, never a lever."""
    if entry <= 0 or equity_usdt <= 0:
        return V0Sizing(0.0, 0.0, 0.0, "INVALID_INPUTS")
    distance = abs(entry - stop)
    if distance <= 0:
        return V0Sizing(0.0, 0.0, 0.0, "INVALID_STOP_ZERO_DISTANCE")
    risk_usdt = equity_usdt * spec.risk_pct_equity / 100.0
    raw_qty = risk_usdt / distance
    qty = (int(raw_qty / qty_step) * qty_step) if qty_step > 0 else raw_qty
    if qty < min_qty or qty <= 0:
        return V0Sizing(0.0, 0.0, risk_usdt, "BELOW_EXCHANGE_MINIMUM_QTY")
    notional = qty * entry
    if notional < min_notional:
        return V0Sizing(0.0, notional, risk_usdt, "BELOW_EXCHANGE_MIN_NOTIONAL")
    if max_notional is not None and notional > max_notional:
        # Respect the existing cap; never raise a safety limit to fit an order.
        qty = (int((max_notional / entry) / qty_step) * qty_step) if qty_step > 0 else max_notional / entry
        notional = qty * entry
        if qty < min_qty:
            return V0Sizing(0.0, notional, risk_usdt, "PORTFOLIO_CAP_LEAVES_NOTHING")
    actual_risk = qty * distance
    if actual_risk > risk_usdt * 1.0001:
        return V0Sizing(0.0, notional, risk_usdt, "ROUNDING_INCREASED_RISK")
    return V0Sizing(qty, notional, actual_risk, None)
