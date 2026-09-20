"""Exchange-valid rounding. Pure Decimal arithmetic, direction explicit.

Conservative conventions used by LSRL (each applied by the caller, not here):
  * passive entry limits round AWAY from the market (LONG down, SHORT up)
  * stop-loss rounds AWAY from the entries (LONG down, SHORT up)
  * take-profit rounds TOWARD the entries (LONG down, SHORT up)  [never overstate reward]
  * quantities always round DOWN to the step

Float noise guard: values are first snapped to 1e-10 so that binary artefacts
such as 149.39999999999998 (true value 149.40) cannot fall on the wrong side of a
tick boundary. Real exchange prices/quantities carry far fewer than 10 decimals,
so the snap never alters a genuine value.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

_MODES = {"down": ROUND_FLOOR, "up": ROUND_CEILING, "nearest": ROUND_HALF_EVEN}
_SNAP = Decimal("1e-10")


def to_dec(value: float | int | str | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _snap(value: Decimal) -> Decimal:
    return value.quantize(_SNAP, rounding=ROUND_HALF_EVEN)


def round_to_tick(price: float | Decimal, tick: float, mode: str) -> float:
    """Round `price` to a multiple of `tick`. mode: 'down' | 'up' | 'nearest'."""
    t = to_dec(tick)
    if t <= 0:
        raise ValueError("tick must be positive")
    steps = (_snap(to_dec(price)) / t).to_integral_value(rounding=_MODES[mode])
    return float(steps * t)


def floor_to_step(qty: float | Decimal, step: float) -> float:
    """Round a quantity DOWN to the step (never inflates risk)."""
    s = to_dec(step)
    if s <= 0:
        raise ValueError("step must be positive")
    q = to_dec(qty)
    if q <= 0:
        return 0.0
    steps = (_snap(q) / s).to_integral_value(rounding=ROUND_DOWN)
    return float(steps * s)


def ticks_between(a: float, b: float, tick: float) -> int:
    return int(((to_dec(a) - to_dec(b)).copy_abs() / to_dec(tick)).to_integral_value(rounding=ROUND_FLOOR))
