"""turtle_long_strong_breakout_v1 -- external research candidate, LONG-only.

PROSPECTIVE_FREEZE_2026-09-14: this specification is frozen. Author's own
verdict (Agent C, external offline research, n=91 historical trades) was
REJECT for real-money execution at the current ~$27.44 account NAV -- every
qualifying trade's risk-sized notional ($1.00-$4.79, median $1.75) falls
under Bitget's exchange minimum, and risk was never inflated to force
executability. This module exists to collect NORMALIZED, non-executable
prospective R evidence only. Do not alter any threshold below based on
observed shadow results during the prospective epoch.

Formula provenance: reproduced from Agent C's (peer session
bitget-ai-agent-45) verbal specification and an 11-trade sample of actual
signals, independently re-derived against fresh Binance public OHLCV --
not copied from any shared file, since none existed (Agent C's own research
lived only in /tmp/turtle_research/ on a separate machine).
"""

from __future__ import annotations

from dataclasses import dataclass


BREAKOUT_STRENGTH_THRESHOLD = 0.910359
DONCHIAN_ENTRY_LOOKBACK = 20
DONCHIAN_EXIT_LOOKBACK = 10
SMA_TREND_PERIOD = 200
ATR_PERIOD = 20
ADX_PERIOD = 14
ADX_MIN = 20.0
STOP_ATR_MULTIPLE = 2.0


@dataclass(frozen=True, slots=True)
class Candle:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float


def sma(closes: list[float], period: int) -> list[float | None]:
    """Simple moving average, causal: sma[i] uses closes[i-period+1 .. i]."""
    out: list[float | None] = [None] * len(closes)
    running = 0.0
    for i, c in enumerate(closes):
        running += c
        if i >= period:
            running -= closes[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def true_range(candles: list[Candle]) -> list[float | None]:
    """TR[i] needs candles[i] and candles[i-1]; TR[0] is None (no prior close)."""
    out: list[float | None] = [None] * len(candles)
    for i in range(1, len(candles)):
        h, l = candles[i].high, candles[i].low
        prev_close = candles[i - 1].close
        out[i] = max(h - l, abs(h - prev_close), abs(l - prev_close))
    return out


def wilder_atr(candles: list[Candle], period: int = ATR_PERIOD) -> list[float | None]:
    """Wilder-smoothed ATR. ATR[period] = mean(TR[1..period]); thereafter
    ATR[i] = (ATR[i-1]*(period-1) + TR[i]) / period. Index i's ATR reflects
    information available AT candle i's close (it is causal to use ATR[i]
    for decisions made at/after candle i's close)."""
    tr = true_range(candles)
    out: list[float | None] = [None] * len(candles)
    if len(candles) <= period:
        return out
    seed = tr[1 : period + 1]
    if any(v is None for v in seed):
        return out
    atr = sum(seed) / period  # type: ignore[arg-type]
    out[period] = atr
    for i in range(period + 1, len(candles)):
        t = tr[i]
        if t is None:
            continue
        atr = (atr * (period - 1) + t) / period
        out[i] = atr
    return out


def wilder_adx(candles: list[Candle], period: int = ADX_PERIOD) -> list[float | None]:
    """Standard Wilder DI+/DI-/DX/ADX. ADX[i] is causal for decisions at
    candle i's close: it only uses candles[0..i]."""
    n = len(candles)
    plus_dm: list[float] = [0.0] * n
    minus_dm: list[float] = [0.0] * n
    tr = true_range(candles)
    for i in range(1, n):
        up_move = candles[i].high - candles[i - 1].high
        down_move = candles[i - 1].low - candles[i].low
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0

    out: list[float | None] = [None] * n
    if n <= 2 * period:
        return out

    smoothed_tr = sum(v for v in tr[1 : period + 1] if v is not None)
    smoothed_plus_dm = sum(plus_dm[1 : period + 1])
    smoothed_minus_dm = sum(minus_dm[1 : period + 1])

    dx_values: list[float] = []

    def _dx(s_tr: float, s_pdm: float, s_mdm: float) -> float:
        if s_tr == 0:
            return 0.0
        plus_di = 100.0 * s_pdm / s_tr
        minus_di = 100.0 * s_mdm / s_tr
        denom = plus_di + minus_di
        if denom == 0:
            return 0.0
        return 100.0 * abs(plus_di - minus_di) / denom

    dx_values.append(_dx(smoothed_tr, smoothed_plus_dm, smoothed_minus_dm))

    for i in range(period + 1, n):
        t = tr[i] or 0.0
        smoothed_tr = smoothed_tr - (smoothed_tr / period) + t
        smoothed_plus_dm = smoothed_plus_dm - (smoothed_plus_dm / period) + plus_dm[i]
        smoothed_minus_dm = smoothed_minus_dm - (smoothed_minus_dm / period) + minus_dm[i]
        dx_values.append(_dx(smoothed_tr, smoothed_plus_dm, smoothed_minus_dm))
        if i - period == period - 1:
            # first ADX = simple mean of the first `period` DX values
            first_adx_index = period + period - 1
            out[first_adx_index] = sum(dx_values) / period
        elif len(dx_values) > period:
            prev_index = i - 1
            prev_adx = out[prev_index]
            if prev_adx is not None:
                out[i] = (prev_adx * (period - 1) + dx_values[-1]) / period
    return out


def donchian_high_excl_current(candles: list[Candle], i: int, lookback: int) -> float | None:
    """max(high) over the `lookback` bars strictly BEFORE index i. Causal:
    never includes candle i itself."""
    if i < lookback:
        return None
    window = candles[i - lookback : i]
    return max(c.high for c in window)


def donchian_low_excl_current(candles: list[Candle], i: int, lookback: int) -> float | None:
    if i < lookback:
        return None
    window = candles[i - lookback : i]
    return min(c.low for c in window)


@dataclass(frozen=True, slots=True)
class TurtleSignal:
    signal_index: int
    signal_timestamp_ms: int
    entry_price: float  # Donchian-20 high, causal, excl. current bar -- fill assumed here
    entry_high_level: float
    atr20_at_entry: float
    breakout_dist_atr: float
    adx14_at_entry: float
    sma200_at_entry: float
    initial_stop: float


def detect_turtle_signal(candles: list[Candle], i: int, sma200: list[float | None],
                          atr20: list[float | None], adx14: list[float | None]) -> TurtleSignal | None:
    """Evaluate candle i as a potential LONG entry bar. All inputs indexed
    at i or earlier only -- no forward-looking data. Returns None unless
    every gate (Donchian breakout, SMA200 trend, ADX>=20, strong-breakout
    threshold) passes."""
    entry_high = donchian_high_excl_current(candles, i, DONCHIAN_ENTRY_LOOKBACK)
    if entry_high is None:
        return None
    bar = candles[i]
    if bar.high <= entry_high:
        return None
    sma_val = sma200[i]
    if sma_val is None or bar.close <= sma_val:
        return None
    atr_val = atr20[i]
    if atr_val is None or atr_val <= 0:
        return None
    adx_val = adx14[i]
    if adx_val is None or adx_val < ADX_MIN:
        return None

    dist = (bar.high - entry_high) / atr_val
    if dist < BREAKOUT_STRENGTH_THRESHOLD:
        return None

    entry_price = entry_high  # stop-order style fill, causal: a resting order at this level fills when touched
    initial_stop = entry_price - STOP_ATR_MULTIPLE * atr_val

    return TurtleSignal(
        signal_index=i,
        signal_timestamp_ms=bar.timestamp_ms,
        entry_price=entry_price,
        entry_high_level=entry_high,
        atr20_at_entry=atr_val,
        breakout_dist_atr=dist,
        adx14_at_entry=adx_val,
        sma200_at_entry=sma_val,
        initial_stop=initial_stop,
    )


@dataclass(frozen=True, slots=True)
class TurtleTrade:
    signal: TurtleSignal
    exit_index: int
    exit_timestamp_ms: int
    exit_price: float
    exit_reason: str  # "INITIAL_STOP" | "DONCHIAN10_EXIT"
    result_r: float


ROUND_TRIP_FEE_BPS = 12.0  # half on entry, half on exit -- matches Agent C's methodology


def simulate_trade(candles: list[Candle], signal: TurtleSignal) -> TurtleTrade | None:
    """Walk forward from the bar AFTER the signal bar (causal guard: the
    signal/entry candle is never eligible for its own stop/exit evaluation
    -- mirrors the production runner's entry-candle-reuse fix). Exit is
    whichever triggers first: the initial 2xATR stop, or a breach of the
    prior-10-bar Donchian low. If both trigger on the same bar, the stop
    wins (Agent C's tie-break rule)."""
    n = len(candles)
    entry_i = signal.signal_index
    risk = signal.entry_price - signal.initial_stop
    if risk <= 0:
        return None

    for j in range(entry_i + 1, n):
        bar = candles[j]
        exit_low = donchian_low_excl_current(candles, j, DONCHIAN_EXIT_LOOKBACK)

        stop_hit = bar.low <= signal.initial_stop
        donchian_exit_hit = exit_low is not None and bar.low <= exit_low

        if stop_hit:
            exit_price = signal.initial_stop
            exit_reason = "INITIAL_STOP"
        elif donchian_exit_hit:
            exit_price = exit_low  # type: ignore[assignment]
            exit_reason = "DONCHIAN10_EXIT"
        else:
            continue

        gross_r = (exit_price - signal.entry_price) / risk
        fee_r = (ROUND_TRIP_FEE_BPS / 10000.0) * (signal.entry_price / risk)
        result_r = gross_r - fee_r

        return TurtleTrade(
            signal=signal,
            exit_index=j,
            exit_timestamp_ms=bar.timestamp_ms,
            exit_price=exit_price,
            exit_reason=exit_reason,
            result_r=result_r,
        )
    return None
