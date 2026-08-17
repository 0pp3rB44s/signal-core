"""TrendFlow V1.1 — the horizon-mismatch correction.

V1 was blocked at the replay gate: 153 trades, average hold 6.6 h against a 72 h ceiling, and fees
(420.67) larger than the whole gross result (295.21). The cause was structural, not a bad number.
The trend was *defined* on 4H but *invalidated* on a closed 1H candle through EMA20, so ordinary
pullbacks — the thing a medium-horizon trend must be allowed to breathe through — kept closing the
position and paying the round trip again.

V1.1 puts every horizon on the same footing:

* **4H defines the trend.** Three conditions, no score, no weights: UPTREND / DOWNTREND / NO_TREND.
* **1H only times the entry** inside an already-qualified 4H trend.
* **Invalidation is 4H**, on a closed candle. A 1H pullback is no longer an exit.
* **Re-entry needs a new closed 4H candle**, which is what stops 1H exit/re-entry cycling.

The 0-100 weighted score is gone. It gave the appearance of nuance while being five booleans in a
trench coat, and an interpretable UPTREND/DOWNTREND/NO_TREND is both honest and testable.

Everything already proven correct in V1 is imported unchanged: closed-candle enforcement, the
indicators, the cost gate, risk-based sizing.
"""

from __future__ import annotations

from dataclasses import dataclass

from strategies.trendflow_v1 import (Candle, CostModel, DataQualityError, atr, cost_gate,  # noqa: F401
                                     ema, percentile_rank, require_closed, size_position,
                                     volume_ratio)

SPEC_ID = "trendflow-v1.1-20260817"


@dataclass(frozen=True)
class TrendFlowV11Spec:
    """Frozen and pre-registered. No value here was chosen by looking at a replay."""

    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    ema_fast: int = 20
    ema_slow: int = 50
    atr_period: int = 14
    volume_lookback: int = 20
    min_volume_ratio: float = 1.20
    atr_pct_low: float = 30.0
    atr_pct_high: float = 95.0
    max_atr_extension: float = 1.5
    cost_multiple_required: float = 3.0
    risk_pct_equity: float = 0.25
    stop_atr_multiple: float = 1.5
    #: Pre-registered. Wider than V1's 1.5 because the runner must survive pullbacks, not be
    #: shaken out by them. Not grid searched.
    trail_atr_multiple: float = 2.0
    max_positions: int = 2
    max_portfolio_risk_pct: float = 0.50
    tp1_r: float = 1.0
    tp1_close_pct: float = 25.0
    #: No TP2. The strategy exists to capture a right tail, not to manufacture a win rate.
    max_hold_hours: float = 72.0
    daily_halt_pct: float = -1.0
    weekly_halt_pct: float = -3.0
    consecutive_losses_halt: int = 3
    spec_id: str = SPEC_ID

    @property
    def min_1h_candles(self) -> int:
        return max(self.ema_slow, self.atr_period + 1, self.volume_lookback + 1) + 5

    @property
    def min_4h_candles(self) -> int:
        return self.ema_slow + 2  # +2 so the EMA20 slope has a prior value


@dataclass(frozen=True)
class TrendState:
    """Interpretable by design: three states, not a number between 0 and 100."""

    regime: str            # UPTREND | DOWNTREND | NO_TREND
    direction: str | None  # LONG | SHORT | None — set only when entry timing also agrees
    block_reason: str | None
    values: dict[str, float]
    conditions: dict[str, bool]
    spec_id: str = SPEC_ID

    @property
    def is_entry(self) -> bool:
        return self.direction is not None


def classify_4h(candles_4h: list[Candle], spec: TrendFlowV11Spec) -> tuple[str, dict, dict]:
    """The trend definition. Three conditions on closed 4H candles."""
    require_closed(candles_4h, minimum=spec.min_4h_candles, label="4H")
    closes = [c.close for c in candles_4h]
    e20 = ema(closes, spec.ema_fast)
    e50 = ema(closes, spec.ema_slow)
    close, fast, slow, fast_prev = closes[-1], e20[-1], e50[-1], e20[-2]
    up = {"close_above_ema20": close > fast, "ema20_above_ema50": fast > slow,
          "ema20_rising": fast > fast_prev}
    down = {"close_above_ema20": close < fast, "ema20_above_ema50": fast < slow,
            "ema20_rising": fast < fast_prev}
    values = {"close_4h": close, "ema20_4h": fast, "ema50_4h": slow, "ema20_4h_prev": fast_prev}
    if all(up.values()):
        return "UPTREND", values, up
    if all(down.values()):
        return "DOWNTREND", values, down
    return "NO_TREND", values, up


def evaluate(candles_1h: list[Candle], candles_4h: list[Candle], spec: TrendFlowV11Spec,
             *, atr_pct_history: list[float] | None = None) -> TrendState:
    """4H decides the trend; 1H only decides when to step in."""
    require_closed(candles_1h, minimum=spec.min_1h_candles, label="1H")
    regime, values, conds = classify_4h(candles_4h, spec)

    closes_1h = [c.close for c in candles_1h]
    e20_1h = ema(closes_1h, spec.ema_fast)
    close_1h, fast_1h, fast_1h_prev = closes_1h[-1], e20_1h[-1], e20_1h[-2]
    atr14 = atr(candles_1h, spec.atr_period)
    atr_pct = atr14 / close_1h
    vol_ratio = volume_ratio(candles_1h, spec.volume_lookback)
    extension = abs(close_1h - fast_1h) / atr14
    history = atr_pct_history if atr_pct_history is not None else _atr_pct_history(candles_1h, spec)
    atr_rank = percentile_rank(history, atr_pct)

    values.update({"close_1h": close_1h, "ema20_1h": fast_1h, "ema20_1h_prev": fast_1h_prev,
                   "atr14_1h": atr14, "atr_pct": atr_pct, "atr_percentile": atr_rank,
                   "volume_ratio": vol_ratio, "extension_atr": extension})

    if regime == "NO_TREND":
        return TrendState(regime, None, "NO_4H_TREND", values, conds)

    timing_ok = ((close_1h > fast_1h and fast_1h > fast_1h_prev) if regime == "UPTREND"
                 else (close_1h < fast_1h and fast_1h < fast_1h_prev))
    conds["entry_timing_1h"] = timing_ok
    if not timing_ok:
        return TrendState(regime, None, "WAIT_1H_TIMING", values, conds)
    if atr_rank < spec.atr_pct_low:
        return TrendState(regime, None, "NO_TRADE_LOW_VOL", values, conds)
    if atr_rank > spec.atr_pct_high:
        return TrendState(regime, None, "NO_TRADE_SHOCK", values, conds)
    if vol_ratio < spec.min_volume_ratio:
        return TrendState(regime, None, "VOLUME_BELOW_MINIMUM", values, conds)
    if extension > spec.max_atr_extension:
        return TrendState(regime, None, "WAIT_OVEREXTENDED", values, conds)

    return TrendState(regime, "LONG" if regime == "UPTREND" else "SHORT", None, values, conds)


def trend_invalidated_4h(direction: str, *, closed_4h_close: float, ema20_4h: float) -> bool:
    """The only trend exit. Closed 4H evidence, never intrabar, never 1H."""
    return closed_4h_close < ema20_4h if direction == "LONG" else closed_4h_close > ema20_4h


def _atr_pct_history(candles: list[Candle], spec: TrendFlowV11Spec) -> list[float]:
    out = []
    for end in range(spec.atr_period + 1, len(candles) + 1):
        window = candles[:end]
        try:
            out.append(atr(window, spec.atr_period) / window[-1].close)
        except DataQualityError:
            continue
    return out or [0.0]
