"""Bounded micro-live capital. Pure constants and validation: NOTHING here chooses the amount, and nothing here can raise a risk.

THE OWNER selects the micro-live capital ceiling, inside a fixed band, in a reviewed release policy. This module only:
  * defines the band (CAPITAL_MIN_USDT..CAPITAL_MAX_USDT) and the largest exposure ceiling relative to the capital;
  * refuses everything outside it (a bool, NaN, a string, zero, below the floor, above the cap);
  * defines the SIZING NAV the strategy sees: min(authenticated equity, capital ceiling) - so capital above the ceiling can never enlarge a
    trade, and equity below it shrinks the trade (the frozen 0.5 % risk fraction is applied to whatever is smaller).

The frozen sizing (`lsrl.core.ladder.size_ladder`) rounds quantity DOWN and never raises it to reach an exchange minimum: a setup whose risk budget cannot
buy the minimum quantity is a NO-TRADE, not a bigger position. `lsrl.tools.capital_feasibility` shows the owner, per capital, which stop distances that
leaves tradeable. It does not recommend a number.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

#: The owner-selectable micro-live capital band (USDT). Below the floor the exchange minimum quantity makes the frozen ladder untradeable
#: for most geometries; above the cap the pilot is no longer "micro".
CAPITAL_MIN_USDT = 100.0
CAPITAL_MAX_USDT = 150.0
#: The largest total exposure (all rungs, filled or resting) relative to the capital: the repository's own default `MAX_LEVERAGE`
#: (app/config.py). A pilot ceiling can be lower, never higher; a test pins this constant to the setting so they cannot drift apart.
EXPOSURE_TO_CAPITAL_MAX = 5.0
#: The frozen LSRL risk fraction per full trade (0.50 % of NAV). Never increased.
RISK_FRACTION = 0.005


class CapitalRefused(ValueError):
    """The capital or exposure ceiling is missing, malformed or outside the permitted band."""


def _number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CapitalRefused(f"{name}_NOT_A_FINITE_NUMBER")
    return float(value)


@dataclass(frozen=True)
class CapitalPolicy:
    capital_ceiling_usdt: float
    exposure_ceiling_usdt: float

    def __post_init__(self) -> None:
        capital = _number("CAPITAL", self.capital_ceiling_usdt)
        exposure = _number("EXPOSURE_CEILING", self.exposure_ceiling_usdt)
        if not CAPITAL_MIN_USDT <= capital <= CAPITAL_MAX_USDT:
            raise CapitalRefused(f"CAPITAL_OUTSIDE_BAND:{capital}:{CAPITAL_MIN_USDT}-{CAPITAL_MAX_USDT}")
        if not 0 < exposure <= capital * EXPOSURE_TO_CAPITAL_MAX:
            raise CapitalRefused(f"EXPOSURE_CEILING_OUTSIDE_BAND:{exposure}:max={capital * EXPOSURE_TO_CAPITAL_MAX}")

    @property
    def max_risk_per_full_trade_usdt(self) -> float:
        return self.capital_ceiling_usdt * RISK_FRACTION


def sizing_nav(equity_usdt: float, capital_ceiling_usdt: float) -> float:
    """The NAV the strategy sizes against: never more than the owner-approved ceiling, never more than the account holds."""
    equity = _number("EQUITY", equity_usdt)
    ceiling = _number("CAPITAL", capital_ceiling_usdt)
    if equity <= 0:
        raise CapitalRefused("EQUITY_NOT_POSITIVE")
    return min(equity, ceiling)
