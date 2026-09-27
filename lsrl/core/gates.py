"""Risk-gate contract consumed by the engine (pure interface).

The engine never imports the project's RiskManager. Instead it is handed a
RiskGate. In live execution (and in shadow/paper when run inside the bot) the gate
is the real central RiskManager behind an adapter; offline backtest/replay uses
NullRiskGate, whose decisions are stamped `source="NULL_OFFLINE"` so a result
produced without the central gate can never be mistaken for one produced with it.
The engine only ever REDUCES risk on the gate's say-so, never raises it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from lsrl.core.types import Direction


@dataclass(frozen=True, slots=True)
class RiskProposal:
    setup_id: str
    symbol: str
    direction: Direction
    t_ms: int
    nav_usdt: float
    entry_prices: tuple[float, ...]
    stop: float
    total_qty: float
    planned_notional_usdt: float
    planned_loss_usdt: float
    strategy_risk_fraction: float
    spread_bps: float = 0.0


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reasons: tuple[str, ...] = ()
    # Central risk fraction the gate is willing to allow; the engine uses
    # min(strategy fraction, this). None = no additional reduction.
    applied_risk_fraction: float | None = None
    source: str = ""


class RiskGate(Protocol):
    def check(self, proposal: RiskProposal) -> RiskDecision: ...


class NullRiskGate:
    """Offline-only gate: allows everything, and says so explicitly."""

    def check(self, proposal: RiskProposal) -> RiskDecision:
        return RiskDecision(allowed=True, reasons=("NULL_OFFLINE_GATE",), source="NULL_OFFLINE")
