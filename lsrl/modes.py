"""Run modes. The engine never sees this enum: a mode only selects the ADAPTERS
(market-data source, execution adapter, NAV source) and labels the outputs."""
from __future__ import annotations

from enum import Enum


class Mode(str, Enum):
    BACKTEST = "BACKTEST"       # recorded inputs -> SimExchange, compounding virtual account
    SHADOW = "SHADOW"           # live inputs     -> SimExchange, FIXED reference NAV, never mixed with paper
    PAPER = "PAPER"             # live inputs     -> SimExchange, compounding virtual account
    MICRO_LIVE = "MICRO_LIVE"   # live inputs     -> real exchange adapter (unarmed by default)


SIM_MODES = (Mode.BACKTEST, Mode.SHADOW, Mode.PAPER)
