"""ExpansionFlow V1 — maker-first expansion. OPPORTUNITY → DIRECTION → LOCATION → ECONOMICS.

Built on what five failed attempts actually established, not on hope:

* Tiny directional effects do not survive execution cost, so this trades rarely and only where the
  available move is comfortably larger than friction. The economic gate is mandatory and is checked
  twice — against risk and against friction.
* Volatility has been far more predictable than direction, so *opportunity* is stage one and
  direction is only consulted once an expansion regime exists.
* Resting book structure carries real but ~1 bps information, so it is **confirmation only**: it can
  block a trade and can never create one.
* Turnover destroyed otherwise sound strategies, so entry is post-only with a TTL, one reprice and
  **no taker fallback**. A missed trade is an acceptable outcome; a chased one is not.
* Geometry cannot manufacture expectancy, so the stop is structural and the target is a fixed 2R —
  the question is whether the *entry* has edge, and nothing here is allowed to obscure that.

Pure module: it decides, it does not place orders and performs no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

STRATEGY_ID = "expansionflow_v1"
SPEC_ID = "expansionflow-v1-20260817"


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
class BookSnapshot:
    imb1: float
    microprice_edge_bps: float
    timestamp_ms: int
    stale: bool = False


@dataclass(frozen=True)
class Fees:
    """Authenticated only. Never hard-coded from public documentation."""
    maker_bps: float
    taker_bps: float
    slippage_bps: float = 1.0

    def maker_first_round_trip_bps(self) -> float:
        """Maker in, maker-or-taker out. The exit fee is assumed taker: the protective stop must
        prioritise capital protection over fee status, so pricing it as maker would be a fiction."""
        return self.maker_bps + self.taker_bps + self.slippage_bps


@dataclass(frozen=True)
class ExpansionSpec:
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    ema_fast: int = 20
    ema_slow: int = 50
    atr_period: int = 14
    atr_median_lookback: int = 50
    atr_ratio_min: float = 1.15
    atr_friction_multiple: float = 4.0
    ema_slope_bars: int = 3
    close_position_min: float = 0.60        # upper 60% of the candle range for LONG
    max_atr_extension: float = 1.5
    breakout_lookback: int = 20
    breakout_min_range_atr: float = 1.0
    breakout_max_range_atr: float = 2.5
    retest_max_bars: int = 4
    retest_tolerance_atr: float = 0.20
    stop_buffer_atr: float = 0.15
    min_risk_bps: float = 8.0
    max_risk_bps: float = 60.0
    move_to_risk_multiple: float = 2.0
    move_to_friction_multiple: float = 4.0
    reward_multiple: float = 2.0
    max_hold_hours: float = 48.0
    risk_pct_equity: float = 0.25
    max_concurrent_positions: int = 2
    max_total_initial_risk_pct: float = 0.50
    max_daily_loss_pct: float = 1.0
    order_ttl_seconds: int = 180
    reprice_ttl_seconds: int = 120
    max_reprices: int = 1
    reprice_tolerance_atr: float = 0.10
    strategy_id: str = STRATEGY_ID
    spec_id: str = SPEC_ID

    @property
    def min_1h_candles(self) -> int:
        return self.ema_slow + self.atr_median_lookback + self.atr_period + 2

    @property
    def min_15m_candles(self) -> int:
        return self.breakout_lookback + self.atr_period + 2


class ExpansionDataError(ValueError):
    """Stale, unfinished or malformed input. Callers must fail closed."""


# --- indicators ---------------------------------------------------------------


def ema(values: list[float], period: int) -> list[float]:
    if period <= 0 or len(values) < period:
        raise ExpansionDataError(f"ema{period} needs {period} values, got {len(values)}")
    k = 2.0 / (period + 1.0)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def atr_series(candles: list[Candle], period: int) -> list[float]:
    """ATR at each bar from `period` onward, using only bars at or before that point."""
    if len(candles) < period + 1:
        raise ExpansionDataError(f"atr{period} needs {period + 1} candles")
    trs = []
    for prev, cur in zip(candles, candles[1:]):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    out = []
    for i in range(period, len(trs) + 1):
        out.append(sum(trs[i - period:i]) / period)
    return out


def validate(candles: list[Candle], minimum: int, label: str) -> None:
    if len(candles) < minimum:
        raise ExpansionDataError(f"{label}: need {minimum} closed candles, got {len(candles)}")
    if any(not c.closed for c in candles):
        raise ExpansionDataError(f"{label}: an unfinished candle was supplied")
    for prev, cur in zip(candles, candles[1:]):
        if cur.open_time_ms <= prev.open_time_ms:
            raise ExpansionDataError(f"{label}: candles are not strictly ordered")
    for c in candles:
        if c.high < c.low or c.close <= 0 or c.low <= 0:
            raise ExpansionDataError(f"{label}: impossible candle geometry")


# --- stage results ------------------------------------------------------------


@dataclass
class Setup:
    """One evaluated setup, whether it traded or not. Every field is journalled."""
    symbol: str
    timestamp_ms: int
    side: str | None = None
    stage: str = "OPPORTUNITY"
    block_reason: str | None = None
    values: dict = field(default_factory=dict)
    breakout_level: float | None = None
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    risk_bps: float | None = None
    available_move_bps: float | None = None
    strategy_id: str = STRATEGY_ID
    spec_id: str = SPEC_ID

    @property
    def is_tradeable(self) -> bool:
        return self.side is not None and self.block_reason is None and self.entry is not None


# --- stage 1: opportunity -----------------------------------------------------


def opportunity(candles_1h: list[Candle], spec: ExpansionSpec, fees: Fees | None) -> tuple[bool, dict, str | None]:
    """Magnitude, not direction. Fails closed when the authenticated fee is unavailable."""
    validate(candles_1h, spec.min_1h_candles, "1H")
    if fees is None:
        return False, {}, "NO_AUTHENTICATED_FEES"
    atrs = atr_series(candles_1h, spec.atr_period)
    if len(atrs) < spec.atr_median_lookback + 1:
        return False, {}, "INSUFFICIENT_ATR_HISTORY"
    current = atrs[-1]
    baseline = median(atrs[-(spec.atr_median_lookback + 1):-1])
    if baseline <= 0:
        return False, {}, "ZERO_ATR_BASELINE"
    ratio = current / baseline
    atr_bps = current / candles_1h[-1].close * 10_000.0
    friction = fees.maker_first_round_trip_bps()
    values = {"atr_ratio": ratio, "atr_bps": atr_bps, "friction_bps": friction,
              "atr14_1h": current, "atr_median_1h": baseline}
    if ratio < spec.atr_ratio_min:
        return False, values, "NO_EXPANSION"
    if atr_bps < spec.atr_friction_multiple * friction:
        return False, values, "ATR_BELOW_FRICTION_MULTIPLE"
    return True, values, None


# --- stage 2: direction -------------------------------------------------------


def direction(candles_1h: list[Candle], spec: ExpansionSpec) -> tuple[str | None, dict, str | None]:
    closes = [c.close for c in candles_1h]
    e_fast = ema(closes, spec.ema_fast)
    e_slow = ema(closes, spec.ema_slow)
    if len(e_fast) < spec.ema_slope_bars + 1:
        return None, {}, "INSUFFICIENT_EMA_HISTORY"
    fast, slow = e_fast[-1], e_slow[-1]
    fast_then = e_fast[-1 - spec.ema_slope_bars]
    last = candles_1h[-1]
    rng = last.high - last.low
    close_pos = (last.close - last.low) / rng if rng > 0 else 0.5
    atr14 = atr_series(candles_1h, spec.atr_period)[-1]
    ext = abs(last.close - fast) / atr14 if atr14 > 0 else 99.0
    values = {"ema20_1h": fast, "ema50_1h": slow, "ema20_3bars_ago": fast_then,
              "close_position": close_pos, "extension_atr": ext}

    long_ok = (last.close > fast and fast > slow and fast > fast_then
               and close_pos >= spec.close_position_min)
    short_ok = (last.close < fast and fast < slow and fast < fast_then
                and close_pos <= 1.0 - spec.close_position_min)
    if long_ok and short_ok:
        return None, values, "INVARIANT_VIOLATION_BOTH_SIDES"
    if not long_ok and not short_ok:
        return None, values, "NO_DIRECTIONAL_CONTEXT"
    if ext > spec.max_atr_extension:
        return None, values, "OVEREXTENDED"
    return ("LONG" if long_ok else "SHORT"), values, None


# --- stage 3: breakout --------------------------------------------------------


def breakout(candles_15m: list[Candle], side: str, spec: ExpansionSpec) -> tuple[float | None, dict, str | None]:
    validate(candles_15m, spec.min_15m_candles, "15m")
    atr15 = atr_series(candles_15m, spec.atr_period)[-1]
    window = candles_15m[-(spec.breakout_lookback + 1):-1]
    level = max(c.high for c in window) if side == "LONG" else min(c.low for c in window)
    bar = candles_15m[-1]
    rng = bar.high - bar.low
    values = {"atr15": atr15, "breakout_level": level, "breakout_range": rng,
              "breakout_range_atr": rng / atr15 if atr15 > 0 else 0.0}
    broke = bar.close > level if side == "LONG" else bar.close < level
    if not broke:
        return None, values, "NO_BREAKOUT"
    if atr15 <= 0:
        return None, values, "ZERO_ATR15"
    if rng < spec.breakout_min_range_atr * atr15:
        return None, values, "BREAKOUT_RANGE_TOO_SMALL"
    if rng > spec.breakout_max_range_atr * atr15:
        return None, values, "VOLATILITY_SHOCK"
    return level, values, None


# --- stage 4: retest ----------------------------------------------------------


def retest(candles_since_breakout: list[Candle], side: str, breakout_level: float,
           atr15: float, spec: ExpansionSpec) -> tuple[Candle | None, str | None]:
    """The first valid retest within the window, or an expiry. Never chases."""
    if not candles_since_breakout:
        return None, "AWAITING_RETEST"
    tol = spec.retest_tolerance_atr * atr15
    for i, bar in enumerate(candles_since_breakout[:spec.retest_max_bars]):
        if side == "LONG":
            ok = (bar.low <= breakout_level + tol and bar.close >= breakout_level
                  and bar.close > bar.open)
        else:
            ok = (bar.high >= breakout_level - tol and bar.close <= breakout_level
                  and bar.close < bar.open)
        if ok:
            return bar, None
    if len(candles_since_breakout) >= spec.retest_max_bars:
        return None, "RETEST_EXPIRED"
    return None, "AWAITING_RETEST"


# --- order book: confirmation only -------------------------------------------


def book_confirms(book: BookSnapshot | None, side: str) -> tuple[bool, str | None]:
    """May block a trade. May never create one. Stale or absent data fails closed."""
    if book is None or book.stale:
        return False, "BOOK_STALE_OR_UNAVAILABLE"
    if side == "LONG":
        ok = book.imb1 > 0 and book.microprice_edge_bps > 0
    else:
        ok = book.imb1 < 0 and book.microprice_edge_bps < 0
    return (True, None) if ok else (False, "BOOK_DISAGREES")


# --- structural stop and the economic gate -----------------------------------


def structural_stop(side: str, retest_bar: Candle, bars_since_breakout: list[Candle],
                    atr15: float, spec: ExpansionSpec) -> float:
    buf = spec.stop_buffer_atr * atr15
    if side == "LONG":
        return min([retest_bar.low] + [b.low for b in bars_since_breakout]) - buf
    return max([retest_bar.high] + [b.high for b in bars_since_breakout]) + buf


def available_move_bps(side: str, entry: float, candles_1h: list[Candle],
                       atr14_1h: float, spec: ExpansionSpec) -> float:
    """Distance to the nearest meaningful 1H structure, floored at one 1H ATR."""
    window = candles_1h[-(spec.breakout_lookback + 1):-1]
    if side == "LONG":
        level = max(c.high for c in window)
        distance = max(level - entry, 0.0)
    else:
        level = min(c.low for c in window)
        distance = max(entry - level, 0.0)
    return max(distance, atr14_1h) / entry * 10_000.0


def economic_gate(*, risk_bps: float, move_bps: float, friction_bps: float,
                  spec: ExpansionSpec) -> tuple[bool, str | None, dict]:
    detail = {"risk_bps": risk_bps, "available_move_bps": move_bps,
              "friction_bps": friction_bps,
              "move_to_risk": move_bps / risk_bps if risk_bps > 0 else 0.0,
              "move_to_friction": move_bps / friction_bps if friction_bps > 0 else 0.0}
    if risk_bps < spec.min_risk_bps:
        return False, "RISK_TOO_SMALL", detail
    if risk_bps > spec.max_risk_bps:
        return False, "RISK_TOO_LARGE", detail
    if move_bps < spec.move_to_risk_multiple * risk_bps:
        return False, "MOVE_BELOW_RISK_MULTIPLE", detail
    if move_bps < spec.move_to_friction_multiple * friction_bps:
        return False, "MOVE_BELOW_FRICTION_MULTIPLE", detail
    return True, None, detail


# --- sizing -------------------------------------------------------------------


@dataclass(frozen=True)
class Sizing:
    quantity: float
    notional: float
    risk_usdt: float
    refused_reason: str | None = None


def size_position(*, entry: float, stop: float, equity_usdt: float, spec: ExpansionSpec,
                  min_qty: float, qty_step: float, min_notional: float,
                  committed_risk_pct: float = 0.0) -> Sizing:
    if entry <= 0 or equity_usdt <= 0:
        return Sizing(0.0, 0.0, 0.0, "INVALID_INPUTS")
    distance = abs(entry - stop)
    if distance <= 0:
        return Sizing(0.0, 0.0, 0.0, "ZERO_STOP_DISTANCE")
    remaining = spec.max_total_initial_risk_pct - committed_risk_pct
    if remaining <= 0:
        return Sizing(0.0, 0.0, 0.0, "PORTFOLIO_RISK_EXHAUSTED")
    risk_pct = min(spec.risk_pct_equity, remaining)
    risk_usdt = equity_usdt * risk_pct / 100.0
    qty = (int((risk_usdt / distance) / qty_step) * qty_step) if qty_step > 0 else risk_usdt / distance
    if qty < min_qty or qty <= 0:
        return Sizing(0.0, 0.0, risk_usdt, "BELOW_EXCHANGE_MINIMUM_QTY")
    notional = qty * entry
    if notional < min_notional:
        return Sizing(0.0, notional, risk_usdt, "BELOW_EXCHANGE_MIN_NOTIONAL")
    actual = qty * distance
    if actual > risk_usdt * 1.0001:
        return Sizing(0.0, notional, risk_usdt, "ROUNDING_INCREASED_RISK")
    return Sizing(qty, notional, actual, None)
