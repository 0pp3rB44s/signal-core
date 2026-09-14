"""Position sizing and exchange-minimum verification for the Turtle shadow.

PROSPECTIVE_FREEZE_2026-09-14: risk % and leverage below are frozen for the
prospective epoch, same as the strategy geometry in turtle_long_strong_breakout_v1.

This module NEVER inflates a risk-derived size to clear an exchange minimum.
Reuses clients.bitget_precision.BitgetPrecisionMixin.validate_entry_size --
the same exchange-floor logic the production planner already relies on --
rather than re-implementing minimum-notional/minimum-size enforcement.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

SHADOW_NAV_USDT = 27.443667453963
RISK_PCT_PER_TRADE = 0.0075
LEVERAGE = 5.0

# Frozen research symbol universe (Agent C's own 14-symbol backtest set).
# Not a config knob -- changing this set mid-epoch would be a specification
# change forbidden by the prospective freeze.
ENABLED_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "ATOMUSDT", "ADAUSDT", "DOGEUSDT",
    "FILUSDT", "NEARUSDT", "XLMUSDT", "UNIUSDT", "AAVEUSDT", "INJUSDT",
    "TIAUSDT", "SEIUSDT",
)


@dataclass(frozen=True, slots=True)
class SizingResult:
    symbol: str
    entry_price: float
    initial_stop: float
    risk_usdt: float
    stop_distance: float
    position_units_raw: float          # risk_usdt / stop_distance, unrounded
    risk_sized_notional: float         # position_units_raw * entry_price
    exchange_min_notional_or_min_order: float | None  # None == metadata unavailable
    exchange_normalized_units: float   # size after exchange precision rounding-down
    exchange_executable: bool
    skip_realistic_execution: bool
    skip_reason: str | None            # e.g. "MIN_NOTIONAL", "CONTRACT_METADATA_UNAVAILABLE", None
    required_margin_usdt: float        # informational only, not a sizing input


def compute_risk_sized_position(
    symbol: str,
    entry_price: float,
    initial_stop: float,
    *,
    nav_usdt: float = SHADOW_NAV_USDT,
    risk_pct: float = RISK_PCT_PER_TRADE,
    leverage: float = LEVERAGE,
    precision_mixin=None,  # an object exposing validate_entry_size()/_min_notional(); None => metadata unavailable
) -> SizingResult:
    stop_distance = entry_price - initial_stop
    if stop_distance <= 0:
        raise ValueError(f"non-positive stop distance for {symbol}: entry={entry_price} stop={initial_stop}")

    risk_usdt = nav_usdt * risk_pct
    position_units_raw = risk_usdt / stop_distance
    risk_sized_notional = position_units_raw * entry_price
    required_margin_usdt = risk_sized_notional / leverage

    if precision_mixin is None:
        return SizingResult(
            symbol=symbol, entry_price=entry_price, initial_stop=initial_stop,
            risk_usdt=risk_usdt, stop_distance=stop_distance,
            position_units_raw=position_units_raw, risk_sized_notional=risk_sized_notional,
            exchange_min_notional_or_min_order=None, exchange_normalized_units=0.0,
            exchange_executable=False, skip_realistic_execution=True,
            skip_reason="CONTRACT_METADATA_UNAVAILABLE", required_margin_usdt=required_margin_usdt,
        )

    normalized, reason = precision_mixin.validate_entry_size(symbol, position_units_raw, reference_price=entry_price)
    min_notional = precision_mixin._min_notional(symbol)
    min_notional_f = float(min_notional) if min_notional is not None else None

    executable = reason is None
    skip_reason = None
    if not executable:
        # Map the precision layer's reason vocabulary onto this mission's
        # required SKIP_REASON vocabulary without inventing new semantics.
        if reason == "EXCHANGE_MIN_NOTIONAL_INCOMPATIBLE_WITH_RISK_LIMITS":
            skip_reason = "MIN_NOTIONAL"
        elif reason == "EXCHANGE_MINIMUM_INCOMPATIBLE_WITH_RISK_LIMITS":
            skip_reason = "MIN_ORDER_SIZE"
        elif reason == "CONTRACT_METADATA_UNAVAILABLE":
            skip_reason = "CONTRACT_METADATA_UNAVAILABLE"
        else:
            skip_reason = reason

    return SizingResult(
        symbol=symbol, entry_price=entry_price, initial_stop=initial_stop,
        risk_usdt=risk_usdt, stop_distance=stop_distance,
        position_units_raw=position_units_raw, risk_sized_notional=risk_sized_notional,
        exchange_min_notional_or_min_order=min_notional_f,
        exchange_normalized_units=float(normalized),
        exchange_executable=executable,
        skip_realistic_execution=not executable,
        skip_reason=skip_reason,
        required_margin_usdt=required_margin_usdt,
    )
