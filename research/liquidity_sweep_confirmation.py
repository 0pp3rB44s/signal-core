from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from backtesting.execution_contract import BacktestExecutionContract, ExecutionRecord
from clients.schemas import Candle

INTERVAL_MS = 900_000
CONTROL_STRATEGY = "liquidity_sweep_reversal_v1"
EXPERIMENTAL_STRATEGY = "liquidity_sweep_reversal_confirmation_v1"
CONFIRMATION_WINDOW_CANDLES = 2


@dataclass(frozen=True)
class ConfirmationDecision:
    status: str
    confirmation_offset: int | None
    confirmation_timestamp_ms: int | None
    entry_index: int | None


def confirmation_entry(
    candles: Sequence[Candle], signal_index: int, direction: str,
    *, window_candles: int = CONFIRMATION_WINDOW_CANDLES,
) -> ConfirmationDecision:
    """Return the first closed-candle confirmation and following entry candle."""
    if window_candles != CONFIRMATION_WINDOW_CANDLES:
        raise ValueError("Phase 3A confirmation window is frozen at two candles")
    if signal_index < 0 or signal_index >= len(candles):
        raise ValueError("signal candle is unavailable")
    direction = direction.upper()
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")
    signal = candles[signal_index]
    for offset in range(1, window_candles + 1):
        index = signal_index + offset
        if index >= len(candles) or candles[index].timestamp_ms != signal.timestamp_ms + offset * INTERVAL_MS:
            return ConfirmationDecision("CONFIRMATION_EXPIRED", None, None, None)
        candle = candles[index]
        confirmed = candle.close > signal.high if direction == "LONG" else candle.close < signal.low
        if confirmed:
            entry_index = index + 1
            if entry_index >= len(candles) or candles[entry_index].timestamp_ms != candle.timestamp_ms + INTERVAL_MS:
                return ConfirmationDecision("CONFIRMATION_EXPIRED", None, None, None)
            return ConfirmationDecision("CONFIRMED", offset, candle.timestamp_ms, entry_index)
    return ConfirmationDecision("CONFIRMATION_EXPIRED", None, None, None)


def original_geometry(direction: str, entry_hint: float, invalidation: float) -> tuple[float, float]:
    """Keep the frozen absolute signal-time 0.8R/1.5R targets."""
    risk = abs(entry_hint - invalidation)
    sign = 1.0 if direction.upper() == "LONG" else -1.0
    return entry_hint + sign * risk * 0.8, entry_hint + sign * risk * 1.5


def execute_confirmation(
    execution: BacktestExecutionContract, *, symbol: str, direction: str,
    signal_timestamp: int, entry_hint: float, invalidation: float,
    candles: Sequence[Candle], entry_index: int, equity: float,
) -> ExecutionRecord:
    tp1, final = original_geometry(direction, entry_hint, invalidation)
    return execution.execute(
        strategy=EXPERIMENTAL_STRATEGY, symbol=symbol, timeframe="15m",
        direction=direction, signal_timestamp=signal_timestamp,
        requested_entry=entry_hint, stop=invalidation, targets=[tp1, final],
        candles=candles[entry_index:], equity=equity,
        risk_policy="HISTORICAL_CONSERVATIVE_PROXY",
    )
