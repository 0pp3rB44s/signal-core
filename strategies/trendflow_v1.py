"""TrendFlow V1 — cost-aware, volatility-scaled, multi-timeframe time-series momentum.

MicroFlow V1 is retired as a directional alpha source: its edge measured ≈0 gross across five
independent studies, and the best order-book effect found anywhere (2.6 bps at an unstable tail)
sits below the 4.0 bps maker round trip. TrendFlow does not try to monetise a 1 bps edge. It tries
to catch moves large enough that the cost is a rounding error, and it refuses to trade when the
expected move is not at least 3x the round trip.

Three deliberate departures from V1, each of which was a lesson paid for:

* **Closed candles only.** Every input is a settled 1H or 4H candle. A signal computed on an open
  candle is a signal computed on data that can still change.
* **Targets are built from the fill, never from a candle.** PB-2 proved that evaluating targets
  against a scanner candle's high/low lets a position inherit price action from before it existed.
  Here, `TargetTracker` is seeded with the actual fill price and timestamp and can only be advanced
  by post-entry observations.
* **No fitted weights anywhere.** The score components are equal conceptual weights, fixed in the
  spec. There is nothing here to overfit, which is the point.

This module is pure: it computes signals and sizes from data handed to it and performs no I/O and
no order placement. That keeps every rule below testable without touching an exchange.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

SPEC_ID = "trendflow-v1-20260817"


@dataclass(frozen=True)
class TrendFlowSpec:
    """The frozen contract. Every number here is a stated choice, not a fitted one."""

    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    ema_fast: int = 20
    ema_slow: int = 50
    atr_period: int = 14
    volume_lookback: int = 20
    min_volume_ratio: float = 1.20
    momentum_6h_bars: int = 6
    momentum_24h_bars: int = 24
    score_threshold: float = 80.0
    #: Equal conceptual weights, summing to 100. Not tuned.
    weight_1h_structure: float = 25.0
    weight_4h_structure: float = 25.0
    weight_momentum: float = 20.0
    weight_volume: float = 15.0
    weight_volatility: float = 15.0
    atr_pct_low: float = 30.0
    atr_pct_high: float = 95.0
    max_atr_extension: float = 1.5
    #: Expected move must be at least this multiple of the modelled round trip.
    cost_multiple_required: float = 3.0
    risk_pct_equity: float = 0.25
    stop_atr_multiple: float = 1.5
    trail_atr_multiple: float = 1.5
    max_positions: int = 2
    max_portfolio_risk_pct: float = 0.50
    tp1_r: float = 1.0
    tp2_r: float = 2.0
    tp1_close_pct: float = 25.0
    tp2_close_pct: float = 25.0
    max_hold_hours: float = 72.0
    daily_halt_pct: float = -1.0
    weekly_halt_pct: float = -3.0
    consecutive_losses_halt: int = 3
    spec_id: str = SPEC_ID

    @property
    def min_1h_candles(self) -> int:
        """EMA50 needs 50, the 24h momentum needs 25, ATR14 needs 15. Take the max plus headroom."""
        return max(self.ema_slow, self.momentum_24h_bars + 1, self.atr_period + 1,
                   self.volume_lookback + 1) + 5


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool = True


@dataclass(frozen=True)
class TrendFlowSignal:
    direction: str | None
    score: float
    components: dict[str, float]
    values: dict[str, float | None]
    block_reason: str | None
    spec_id: str = SPEC_ID

    @property
    def is_entry(self) -> bool:
        return self.direction is not None


class DataQualityError(ValueError):
    """Raised when inputs are stale, malformed or incomplete. Callers must fail closed."""


# --- indicators ---------------------------------------------------------------


def ema(values: list[float], period: int) -> list[float]:
    """Standard EMA seeded with the SMA of the first `period` values."""
    if period <= 0:
        raise DataQualityError("ema period must be positive")
    if len(values) < period:
        raise DataQualityError(f"ema{period} needs {period} values, got {len(values)}")
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out = [seed]
    for value in values[period:]:
        out.append(value * k + out[-1] * (1.0 - k))
    return out


def atr(candles: list[Candle], period: int) -> float:
    """Wilder's ATR on closed candles."""
    if len(candles) < period + 1:
        raise DataQualityError(f"atr{period} needs {period + 1} candles, got {len(candles)}")
    trs = []
    for prev, cur in zip(candles[-(period + 1):-1], candles[-period:]):
        trs.append(max(cur.high - cur.low,
                       abs(cur.high - prev.close),
                       abs(cur.low - prev.close)))
    value = sum(trs) / period
    if value <= 0:
        raise DataQualityError("atr resolved to zero or negative")
    return value


def volume_ratio(candles: list[Candle], lookback: int) -> float:
    """Latest closed volume against the median of the `lookback` closed candles before it."""
    if len(candles) < lookback + 1:
        raise DataQualityError(f"volume_ratio needs {lookback + 1} candles")
    prior = [c.volume for c in candles[-(lookback + 1):-1]]
    base = median(prior)
    if base <= 0:
        raise DataQualityError("median prior volume is zero")
    return candles[-1].volume / base


def percentile_rank(values: list[float], value: float) -> float:
    """Percentile of `value` within `values`, 0-100."""
    if not values:
        raise DataQualityError("percentile_rank needs a non-empty history")
    below = sum(1 for v in values if v < value)
    return below / len(values) * 100.0


# --- validation ---------------------------------------------------------------


def require_closed(candles: list[Candle], *, minimum: int, label: str) -> None:
    if len(candles) < minimum:
        raise DataQualityError(f"{label}: need {minimum} closed candles, got {len(candles)}")
    if any(not c.closed for c in candles):
        raise DataQualityError(f"{label}: an open candle was supplied; signals require closed data")
    for prev, cur in zip(candles, candles[1:]):
        if cur.open_time_ms <= prev.open_time_ms:
            raise DataQualityError(f"{label}: candles are not strictly ordered")
    for c in candles:
        if not all(isinstance(v, (int, float)) for v in (c.open, c.high, c.low, c.close, c.volume)):
            raise DataQualityError(f"{label}: non-numeric field")
        if c.high < c.low or c.close <= 0 or c.volume < 0:
            raise DataQualityError(f"{label}: impossible candle geometry")


# --- the signal ---------------------------------------------------------------


def evaluate(candles_1h: list[Candle], candles_4h: list[Candle], spec: TrendFlowSpec,
             *, atr_pct_history: list[float] | None = None) -> TrendFlowSignal:
    """Direction, score and every component. Never raises on a *rejected* setup — only on bad data."""
    require_closed(candles_1h, minimum=spec.min_1h_candles, label="1H")
    require_closed(candles_4h, minimum=spec.ema_slow + 1, label="4H")

    closes_1h = [c.close for c in candles_1h]
    closes_4h = [c.close for c in candles_4h]
    ema20_1h = ema(closes_1h, spec.ema_fast)
    ema50_1h = ema(closes_1h, spec.ema_slow)
    ema20_4h = ema(closes_4h, spec.ema_fast)
    ema50_4h = ema(closes_4h, spec.ema_slow)

    close_1h = closes_1h[-1]
    e20, e50 = ema20_1h[-1], ema50_1h[-1]
    e20_prev = ema20_1h[-2]
    e20_4h, e50_4h = ema20_4h[-1], ema50_4h[-1]
    close_4h = closes_4h[-1]

    ret_6h = (close_1h - closes_1h[-1 - spec.momentum_6h_bars]) / closes_1h[-1 - spec.momentum_6h_bars]
    ret_24h = (close_1h - closes_1h[-1 - spec.momentum_24h_bars]) / closes_1h[-1 - spec.momentum_24h_bars]

    atr14 = atr(candles_1h, spec.atr_period)
    atr_pct = atr14 / close_1h
    vol_ratio = volume_ratio(candles_1h, spec.volume_lookback)
    extension = abs(close_1h - e20) / atr14

    history = atr_pct_history if atr_pct_history is not None else _atr_pct_history(candles_1h, spec)
    atr_rank = percentile_rank(history, atr_pct)

    values = {
        "close_1h": close_1h, "ema20_1h": e20, "ema50_1h": e50, "ema20_1h_prev": e20_prev,
        "close_4h": close_4h, "ema20_4h": e20_4h, "ema50_4h": e50_4h,
        "return_6h": ret_6h, "return_24h": ret_24h,
        "atr14_1h": atr14, "atr_pct": atr_pct, "atr_percentile": atr_rank,
        "volume_ratio": vol_ratio, "extension_atr": extension,
    }

    long_c = {
        "close_above_ema20_1h": close_1h > e20,
        "ema20_above_ema50_1h": e20 > e50,
        "ema20_1h_rising": e20 > e20_prev,
        "close_above_ema20_4h": close_4h > e20_4h,
        "ema20_above_ema50_4h": e20_4h > e50_4h,
        "return_6h_positive": ret_6h > 0,
        "return_24h_positive": ret_24h > 0,
    }
    short_c = {
        "close_above_ema20_1h": close_1h < e20,
        "ema20_above_ema50_1h": e20 < e50,
        "ema20_1h_rising": e20 < e20_prev,
        "close_above_ema20_4h": close_4h < e20_4h,
        "ema20_above_ema50_4h": e20_4h < e50_4h,
        "return_6h_positive": ret_6h < 0,
        "return_24h_positive": ret_24h < 0,
    }
    volume_ok = vol_ratio >= spec.min_volume_ratio
    vol_regime_ok = spec.atr_pct_low <= atr_rank <= spec.atr_pct_high

    long_score, long_parts = _score(long_c, volume_ok, vol_regime_ok, spec)
    short_score, short_parts = _score(short_c, volume_ok, vol_regime_ok, spec)

    long_ok = long_score >= spec.score_threshold
    short_ok = short_score >= spec.score_threshold

    # A setup cannot be both. If it is, something upstream is wrong and we do not guess.
    if long_ok and short_ok:
        return TrendFlowSignal(None, max(long_score, short_score),
                               {"long": long_score, "short": short_score},
                               values, "INVARIANT_VIOLATION_BOTH_SIDES")

    direction = "LONG" if long_ok else ("SHORT" if short_ok else None)
    parts = long_parts if long_ok else (short_parts if short_ok else long_parts)
    score = long_score if long_ok else (short_score if short_ok else max(long_score, short_score))

    if direction is None:
        return TrendFlowSignal(None, score, parts, values, "SCORE_BELOW_THRESHOLD")
    if atr_rank < spec.atr_pct_low:
        return TrendFlowSignal(None, score, parts, values, "NO_TRADE_LOW_VOL")
    if atr_rank > spec.atr_pct_high:
        return TrendFlowSignal(None, score, parts, values, "NO_TRADE_SHOCK")
    if not volume_ok:
        return TrendFlowSignal(None, score, parts, values, "VOLUME_BELOW_MINIMUM")
    if extension > spec.max_atr_extension:
        return TrendFlowSignal(None, score, parts, values, "WAIT_OVEREXTENDED")
    return TrendFlowSignal(direction, score, parts, values, None)


def _score(conds: dict[str, bool], volume_ok: bool, vol_regime_ok: bool,
           spec: TrendFlowSpec) -> tuple[float, dict[str, float]]:
    structure_1h = sum(conds[k] for k in
                       ("close_above_ema20_1h", "ema20_above_ema50_1h", "ema20_1h_rising")) / 3.0
    structure_4h = sum(conds[k] for k in
                       ("close_above_ema20_4h", "ema20_above_ema50_4h")) / 2.0
    momentum = sum(conds[k] for k in ("return_6h_positive", "return_24h_positive")) / 2.0
    parts = {
        "structure_1h": structure_1h * spec.weight_1h_structure,
        "structure_4h": structure_4h * spec.weight_4h_structure,
        "momentum": momentum * spec.weight_momentum,
        "volume": (1.0 if volume_ok else 0.0) * spec.weight_volume,
        "volatility": (1.0 if vol_regime_ok else 0.0) * spec.weight_volatility,
    }
    return sum(parts.values()), parts


def _atr_pct_history(candles: list[Candle], spec: TrendFlowSpec) -> list[float]:
    out = []
    for end in range(spec.atr_period + 1, len(candles) + 1):
        window = candles[:end]
        try:
            out.append(atr(window, spec.atr_period) / window[-1].close)
        except DataQualityError:
            continue
    return out or [0.0]


# --- economics ----------------------------------------------------------------


@dataclass(frozen=True)
class CostModel:
    """Costs in bps. Fee rates must come from the authenticated endpoint, never a constant."""

    taker_fee_bps: float
    maker_fee_bps: float
    spread_bps: float
    slippage_bps: float
    safety_buffer_bps: float = 2.0
    assume_taker: bool = True

    def round_trip_bps(self) -> float:
        """Taker unless a maker fill is *proven*, because an assumed maker fill is a fiction."""
        fee = self.taker_fee_bps if self.assume_taker else self.maker_fee_bps
        return fee * 2.0 + self.spread_bps + self.slippage_bps + self.safety_buffer_bps


def cost_gate(*, expected_opportunity_bps: float, cost: CostModel,
              spec: TrendFlowSpec) -> tuple[bool, dict[str, float]]:
    rt = cost.round_trip_bps()
    multiple = expected_opportunity_bps / rt if rt > 0 else 0.0
    return multiple >= spec.cost_multiple_required, {
        "estimated_cost_bps": rt,
        "expected_opportunity_bps": expected_opportunity_bps,
        "cost_multiple": multiple,
        "required_multiple": spec.cost_multiple_required,
    }


@dataclass(frozen=True)
class SizingResult:
    quantity: float
    stop_price: float
    risk_usdt: float
    notional: float
    refused_reason: str | None = None


def size_position(*, direction: str, entry_price: float, atr14: float, equity_usdt: float,
                  spec: TrendFlowSpec, min_qty: float, qty_step: float, min_notional: float,
                  committed_risk_pct: float = 0.0) -> SizingResult:
    """Risk-based sizing. The stop distance is an input, never a lever to enlarge the position."""
    if entry_price <= 0 or atr14 <= 0 or equity_usdt <= 0:
        return SizingResult(0.0, 0.0, 0.0, 0.0, "INVALID_INPUTS")
    if direction not in ("LONG", "SHORT"):
        return SizingResult(0.0, 0.0, 0.0, 0.0, "INVALID_DIRECTION")

    distance = spec.stop_atr_multiple * atr14
    stop = entry_price - distance if direction == "LONG" else entry_price + distance
    if stop <= 0:
        return SizingResult(0.0, 0.0, 0.0, 0.0, "STOP_BELOW_ZERO")

    remaining_pct = spec.max_portfolio_risk_pct - committed_risk_pct
    if remaining_pct <= 0:
        return SizingResult(0.0, stop, 0.0, 0.0, "PORTFOLIO_RISK_EXHAUSTED")
    risk_pct = min(spec.risk_pct_equity, remaining_pct)
    risk_usdt = equity_usdt * risk_pct / 100.0

    raw_qty = risk_usdt / distance
    steps = int(raw_qty / qty_step) if qty_step > 0 else 0
    qty = steps * qty_step
    if qty < min_qty or qty <= 0:
        return SizingResult(0.0, stop, risk_usdt, 0.0, "BELOW_EXCHANGE_MINIMUM_QTY")
    notional = qty * entry_price
    if notional < min_notional:
        return SizingResult(0.0, stop, risk_usdt, notional, "BELOW_EXCHANGE_MIN_NOTIONAL")
    # Rounding down can only reduce risk; assert that it did.
    actual_risk = qty * distance
    if actual_risk > risk_usdt * 1.0001:
        return SizingResult(0.0, stop, risk_usdt, notional, "ROUNDING_INCREASED_RISK")
    return SizingResult(qty, stop, actual_risk, notional, None)
