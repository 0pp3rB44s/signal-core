from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Sequence

from clients.schemas import Candle

STRATEGY = "failed_range_escape_reversal_v1"
TIMEFRAME_MS = 900_000
HTF_MS = 3_600_000
RANGE_LOOKBACK = 20
ATR_PERIOD = 14
MIN_ESCAPE_ATR = 0.10
MIN_REENTRY_ATR = 0.15
MIN_BODY_FRACTION = 0.50
MAX_STOP_ATR = 2.0
MIN_TP1_BPS = 72.0
TP1_R = 1.2
FINAL_R = 2.0
MAX_HOLD_CANDLES = 8


@dataclass(frozen=True)
class FailedEscapeCandidate:
    strategy: str
    symbol: str
    direction: str
    signal_timestamp_ms: int
    entry_timestamp_ms: int
    signal_close: float
    requested_entry: float
    prior_range_high: float
    prior_range_low: float
    atr14: float
    escape_threshold: float
    escape_close: float
    escape_extreme: float
    escape_distance_atr: float
    reentry_threshold: float
    reentry_close: float
    reentry_distance_atr: float
    body_fraction: float
    stop: float
    stop_distance: float
    stop_distance_pct: float
    stop_distance_atr: float
    tp1_distance_bps: float
    htf_ema20: float
    htf_ema50: float
    htf_relationship: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PatternDecision:
    status: str
    reason: str
    direction: str | None = None
    candidate: FailedEscapeCandidate | None = None


def validate_candles(candles: Sequence[Candle]) -> None:
    previous = None
    for candle in candles:
        if candle.timestamp_ms % TIMEFRAME_MS or previous is not None and candle.timestamp_ms != previous + TIMEFRAME_MS:
            raise ValueError("candles must be ordered, unique, gap-free 15m candles")
        if not (0 < candle.low <= min(candle.open, candle.close) <= max(candle.open, candle.close) <= candle.high):
            raise ValueError("invalid OHLC candle")
        previous = candle.timestamp_ms


def atr14(candles: Sequence[Candle], index: int) -> float:
    """SMA of 14 true ranges ending at index, using only candles <= index."""
    if index < ATR_PERIOD - 1:
        raise ValueError("insufficient ATR history")
    ranges = []
    for position in range(index - ATR_PERIOD + 1, index + 1):
        candle = candles[position]
        previous_close = candles[position - 1].close if position else candle.open
        ranges.append(max(candle.high - candle.low, abs(candle.high - previous_close), abs(candle.low - previous_close)))
    value = sum(ranges) / ATR_PERIOD
    if value <= 0:
        raise ValueError("ATR must be positive")
    return value


def _ema(values: Sequence[float], period: int) -> float:
    if len(values) < period:
        raise ValueError("insufficient EMA history")
    alpha = 2.0 / (period + 1)
    value = float(values[0])
    for item in values[1:]:
        value = alpha * float(item) + (1.0 - alpha) * value
    return value


def closed_hourly_context(candles: Sequence[Candle], signal_index: int) -> tuple[float, float, str]:
    """Aggregate complete UTC hours whose close is no later than signal close."""
    as_of = candles[signal_index].timestamp_ms + TIMEFRAME_MS
    grouped: dict[int, list[Candle]] = {}
    for candle in candles[: signal_index + 1]:
        bucket = candle.timestamp_ms - candle.timestamp_ms % HTF_MS
        grouped.setdefault(bucket, []).append(candle)
    closes = [
        rows[-1].close
        for bucket, rows in sorted(grouped.items())
        if len(rows) == 4
        and [row.timestamp_ms for row in rows] == [bucket + offset * TIMEFRAME_MS for offset in range(4)]
        and bucket + HTF_MS <= as_of
    ]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    relationship = "EMA20_ABOVE_EMA50" if ema20 > ema50 else "EMA20_BELOW_EMA50" if ema20 < ema50 else "EMA_EQUAL"
    return ema20, ema50, relationship


def hourly_context_series(candles: Sequence[Candle]) -> list[tuple[float, float, str] | None]:
    """Return the latest fully closed 1h context at every 15m close in O(n)."""
    result: list[tuple[float, float, str] | None] = []
    closes: list[float] = []
    ema20 = ema50 = None
    alpha20 = 2.0 / 21.0
    alpha50 = 2.0 / 51.0
    for index, candle in enumerate(candles):
        if candle.timestamp_ms % HTF_MS == 3 * TIMEFRAME_MS:
            bucket = candle.timestamp_ms - 3 * TIMEFRAME_MS
            rows = candles[max(0, index - 3):index + 1]
            if len(rows) == 4 and [row.timestamp_ms for row in rows] == [bucket + offset * TIMEFRAME_MS for offset in range(4)]:
                closes.append(candle.close)
                ema20 = candle.close if ema20 is None else alpha20 * candle.close + (1 - alpha20) * ema20
                ema50 = candle.close if ema50 is None else alpha50 * candle.close + (1 - alpha50) * ema50
        if len(closes) >= 50 and ema20 is not None and ema50 is not None:
            relationship = "EMA20_ABOVE_EMA50" if ema20 > ema50 else "EMA20_BELOW_EMA50" if ema20 < ema50 else "EMA_EQUAL"
            result.append((ema20, ema50, relationship))
        else:
            result.append(None)
    return result


def detect_at(
    symbol: str, candles: Sequence[Candle], reentry_index: int, *,
    validated: bool = False, htf_context: tuple[float, float, str] | None = None,
) -> PatternDecision:
    if not validated:
        validate_candles(candles[: reentry_index + 2])
    escape_index = reentry_index - 1
    range_start = escape_index - RANGE_LOOKBACK
    if range_start < 0:
        return PatternDecision("REJECTED", "INSUFFICIENT_HISTORY")
    if reentry_index + 1 >= len(candles):
        return PatternDecision("REJECTED", "NO_ENTRY_CANDLE")
    try:
        volatility = atr14(candles, reentry_index)
        if htf_context is None:
            htf20, htf50, htf_relationship = closed_hourly_context(candles, reentry_index)
        else:
            htf20, htf50, htf_relationship = htf_context
    except ValueError as exc:
        return PatternDecision("REJECTED", str(exc).upper().replace(" ", "_"))

    prior = candles[range_start:escape_index]
    if len(prior) != RANGE_LOOKBACK:
        return PatternDecision("REJECTED", "INSUFFICIENT_HISTORY")
    high = max(candle.high for candle in prior)
    low = min(candle.low for candle in prior)
    escape = candles[escape_index]
    reentry = candles[reentry_index]
    entry = candles[reentry_index + 1]

    if escape.close >= high + MIN_ESCAPE_ATR * volatility:
        direction = "SHORT"
        escape_threshold = high + MIN_ESCAPE_ATR * volatility
        reentry_threshold = high - MIN_REENTRY_ATR * volatility
        if reentry.close > reentry_threshold:
            return PatternDecision("RAW_ESCAPE", "REENTRY_DEPTH_FAILED", direction)
        body_correct = reentry.close < reentry.open
        escape_distance = (escape.close - high) / volatility
        reentry_distance = (high - reentry.close) / volatility
        stop = escape.high
    elif escape.close <= low - MIN_ESCAPE_ATR * volatility:
        direction = "LONG"
        escape_threshold = low - MIN_ESCAPE_ATR * volatility
        reentry_threshold = low + MIN_REENTRY_ATR * volatility
        if reentry.close < reentry_threshold:
            return PatternDecision("RAW_ESCAPE", "REENTRY_DEPTH_FAILED", direction)
        body_correct = reentry.close > reentry.open
        escape_distance = (low - escape.close) / volatility
        reentry_distance = (reentry.close - low) / volatility
        stop = escape.low
    else:
        return PatternDecision("NO_ESCAPE", "ESCAPE_THRESHOLD_FAILED")

    candle_range = reentry.high - reentry.low
    body_fraction = abs(reentry.close - reentry.open) / candle_range if candle_range > 0 else 0.0
    if body_fraction < MIN_BODY_FRACTION:
        return PatternDecision("RAW_ESCAPE", "BODY_FRACTION_FAILED", direction)
    if not body_correct:
        return PatternDecision("RAW_ESCAPE", "REVERSAL_BODY_DIRECTION_FAILED", direction)

    requested = entry.open
    stop_distance = abs(requested - stop)
    geometry_valid = stop < requested if direction == "LONG" else stop > requested
    stop_atr = stop_distance / volatility
    tp1_bps = TP1_R * stop_distance / requested * 10_000
    candidate = FailedEscapeCandidate(
        strategy=STRATEGY, symbol=symbol.upper(), direction=direction,
        signal_timestamp_ms=reentry.timestamp_ms, entry_timestamp_ms=entry.timestamp_ms,
        signal_close=reentry.close, requested_entry=requested,
        prior_range_high=high, prior_range_low=low, atr14=volatility,
        escape_threshold=escape_threshold, escape_close=escape.close,
        escape_extreme=stop, escape_distance_atr=escape_distance,
        reentry_threshold=reentry_threshold, reentry_close=reentry.close,
        reentry_distance_atr=reentry_distance, body_fraction=body_fraction,
        stop=stop, stop_distance=stop_distance,
        stop_distance_pct=stop_distance / requested * 100,
        stop_distance_atr=stop_atr, tp1_distance_bps=tp1_bps,
        htf_ema20=htf20, htf_ema50=htf50, htf_relationship=htf_relationship,
    )
    if not geometry_valid:
        return PatternDecision("VALID_REENTRY", "INVALID_STOP_GEOMETRY", direction, candidate)
    if stop_atr > MAX_STOP_ATR:
        return PatternDecision("VALID_REENTRY", "STOP_DISTANCE_GT_2_ATR", direction, candidate)
    if tp1_bps < MIN_TP1_BPS:
        return PatternDecision("VALID_REENTRY", "TP1_DISTANCE_LT_72_BPS", direction, candidate)
    return PatternDecision("CANDIDATE", "PASS", direction, candidate)
