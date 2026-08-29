"""ONE simple, transparent, NOT-optimized baseline strategy.

Purpose: exercise the Trading Core V1 pipeline end-to-end, nothing else. This
is a plain SMA fast/slow crossover with an ATR-based stop. It is deliberately
undramatic, has not been tuned, and is NOT claimed to be production-worthy. If
it loses money after realistic costs, that is an expected, honest, acceptable
outcome for a V1 pipeline demonstration.

Rule: go LONG when the fast SMA crosses above the slow SMA; go SHORT when the
fast SMA crosses below the slow SMA; otherwise stay FLAT. The stop distance is
`stop_atr_multiple` * ATR(atr_period), computed from CLOSED history plus the
current (also-closed) bar only -- no future bars are used.
"""
from __future__ import annotations

from typing import Sequence

from trading_core_v1.data import Candle
from trading_core_v1.models import Side, Signal
from trading_core_v1.strategy import Strategy


def _sma(values: Sequence[float], period: int) -> float | None:
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def _atr(bars: Sequence[Candle], period: int) -> float | None:
    if len(bars) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(bars)):
        high, low, prev_close = bars[i].high, bars[i].low, bars[i - 1].close
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    window = true_ranges[-period:]
    return sum(window) / period


class BaselineMACrossoverStrategy(Strategy):
    name = "baseline_ma_crossover"

    def __init__(
        self,
        *,
        fast_period: int = 20,
        slow_period: int = 50,
        atr_period: int = 14,
        stop_atr_multiple: float = 2.0,
    ) -> None:
        if fast_period >= slow_period:
            raise ValueError("fast_period must be < slow_period")
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.atr_period = atr_period
        self.stop_atr_multiple = stop_atr_multiple

    def evaluate(self, history: Sequence[Candle], current_bar: Candle) -> Signal:
        bars = list(history) + [current_bar]
        closes = [b.close for b in bars]

        # Need at least slow_period+1 closes so we can compare "before this
        # bar" vs "including this bar" to detect an actual crossover, not just
        # a static ordering.
        if len(closes) < self.slow_period + 1:
            return Signal(
                timestamp=current_bar.timestamp,
                symbol="",
                side=Side.FLAT,
                strategy_name=self.name,
                reason="WARMUP",
            )

        fast_now = _sma(closes, self.fast_period)
        slow_now = _sma(closes, self.slow_period)
        fast_prev = _sma(closes[:-1], self.fast_period)
        slow_prev = _sma(closes[:-1], self.slow_period)
        atr = _atr(bars, self.atr_period)

        if None in (fast_now, slow_now, fast_prev, slow_prev, atr) or atr <= 0:
            return Signal(
                timestamp=current_bar.timestamp,
                symbol="",
                side=Side.FLAT,
                strategy_name=self.name,
                reason="INDICATOR_UNAVAILABLE",
            )

        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now

        if crossed_up:
            stop = current_bar.close - self.stop_atr_multiple * atr
            return Signal(
                timestamp=current_bar.timestamp,
                symbol="",
                side=Side.LONG,
                entry_reference_price=current_bar.close,
                stop_reference=stop,
                strategy_name=self.name,
                reason=f"fast_sma({self.fast_period}) crossed above slow_sma({self.slow_period})",
            )
        if crossed_down:
            stop = current_bar.close + self.stop_atr_multiple * atr
            return Signal(
                timestamp=current_bar.timestamp,
                symbol="",
                side=Side.SHORT,
                entry_reference_price=current_bar.close,
                stop_reference=stop,
                strategy_name=self.name,
                reason=f"fast_sma({self.fast_period}) crossed below slow_sma({self.slow_period})",
            )

        return Signal(
            timestamp=current_bar.timestamp,
            symbol="",
            side=Side.FLAT,
            strategy_name=self.name,
            reason="NO_CROSSOVER",
        )
