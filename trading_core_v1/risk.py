"""Position sizing, separate from strategy logic.

RISK_AMOUNT = EQUITY * RISK_PCT
position size = RISK_AMOUNT / stop_distance (in quote currency terms), rejected
outright if the resulting notional is below the exchange's minimum notional --
never inflated to force a trade through.

V1 scope: MAX_OPEN_POSITIONS = 1 (enforced by the backtester, which only ever
tracks a single open position), no hedging, no portfolio allocator, no
pyramiding, no multi-leg execution, no martingale / no doubling losses / no
recovery sizing. Leverage never defines risk -- it is not a parameter here.
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_OPEN_POSITIONS = 1


@dataclass(frozen=True)
class RiskResult:
    approved: bool
    position_size: float = 0.0  # units of the base asset
    risk_amount: float = 0.0  # quote currency
    reason: str = ""  # "" when approved; "TRADE_REJECTED_MIN_NOTIONAL" etc. otherwise


def compute_position_size(
    *,
    equity: float,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
    min_notional: float,
) -> RiskResult:
    """Deterministic risk-based position sizing.

    Fees/slippage are not added to the sizing formula itself (they are applied
    to the fill and the resulting PnL in backtest.py); they DO count towards
    whether the position clears `min_notional`, since a position sized right at
    the notional floor could otherwise be silently unfillable in practice. V1
    keeps this simple: min_notional is compared directly to the raw notional.
    """
    if equity <= 0:
        raise ValueError("equity must be positive")
    if not (0 < risk_pct <= 1):
        raise ValueError("risk_pct must be in (0, 1]")
    if entry_price <= 0 or stop_price <= 0:
        raise ValueError("entry_price and stop_price must be positive")

    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        return RiskResult(approved=False, reason="ZERO_STOP_DISTANCE")

    risk_amount = equity * risk_pct
    position_size = risk_amount / stop_distance
    notional = position_size * entry_price

    if notional < min_notional:
        return RiskResult(
            approved=False,
            risk_amount=risk_amount,
            reason="TRADE_REJECTED_MIN_NOTIONAL",
        )

    return RiskResult(approved=True, position_size=position_size, risk_amount=risk_amount)
