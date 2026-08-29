"""Execution mode interface: BACKTEST / PAPER / LIVE.

LIVE is structurally disabled in this module -- there is no code path here
that can submit a real order. This is the ONLY executor Trading Core V1 ships
with; it is intentionally a stub for PAPER and LIVE.
"""
from __future__ import annotations

from enum import Enum


class ExecutionMode(str, Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class LiveTradingNotAuthorized(RuntimeError):
    """Raised unconditionally for any attempt to submit a real order via
    Trading Core V1. There is no authorization token, flag, or override that
    can suppress this -- enabling real LIVE trading requires code that does not
    exist in this module, by design."""


class Executor:
    """Mode-gated order submission surface.

    BACKTEST fills are computed directly by trading_core_v1/backtest.py (it
    does not call through this class) -- Executor exists so that a future,
    separately-authorized live/paper implementation has one interface a
    strategy-driving loop can call regardless of mode, matching the
    "same code, different mode" requirement for strategies.
    """

    def __init__(self, mode: ExecutionMode) -> None:
        self.mode = ExecutionMode(mode)

    def submit_order(self, *args, **kwargs):
        if self.mode is ExecutionMode.LIVE:
            raise LiveTradingNotAuthorized(
                "Trading Core V1 LIVE execution is hard-disabled. No real-order "
                "submission path exists in this module."
            )
        raise NotImplementedError(
            f"Executor.submit_order is not implemented for mode={self.mode.value}. "
            "BACKTEST fills are computed by trading_core_v1.backtest directly; "
            "PAPER execution is out of scope for Trading Core V1."
        )
