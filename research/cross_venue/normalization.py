"""Deterministic price and size normalization (Objectives 3 and 4).

Every function here is a pure function of its inputs plus the frozen
``contract.py`` table — no network calls, no mutable global state, no future
information (nothing here ever looks at a row that has not been passed in).
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from .contract import SymbolContract, canonical_grid_tick, get_contract


def to_decimal(value: object) -> Decimal:
    """Parse a raw JSON-sourced numeric value (str, int, or float) into a
    Decimal without inventing precision. Values that arrived as JSON strings
    (as every venue's WS payload does for price/size) are converted via
    ``str()`` first so no float rounding error is introduced; a value that
    already arrived as a Python float (e.g. from a test fixture) is also
    routed through ``str()`` for the same reason, which is lossy only in the
    sense that it reproduces exactly what ``repr()``/JSON already implied.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        raw = value
    else:
        raw = str(value)
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"cannot parse {value!r} as a decimal price/size") from exc


def native_price(symbol: str, venue: str, raw_price: object) -> Decimal:
    """Preserve native precision. No rounding, no grid alignment. Valid for
    all 12 symbols on both venues — this is Objective 3's rule for the 10
    DIRECTLY_COMPARABLE symbols, and also the correct *default* view for the
    2 NORMALIZABLE symbols (their native precision is not itself wrong, only
    not directly grid-comparable across venues without an explicit step)."""
    get_contract(symbol)  # raises if symbol is outside the frozen contract
    return to_decimal(raw_price)


def grid_aligned_price(symbol: str, raw_price: object) -> Decimal:
    """Round a native price onto the common (coarser) cross-venue tick grid.

    Identity transform for the 10 DIRECTLY_COMPARABLE symbols (their ticks
    already match). For the 2 NORMALIZABLE symbols (SOL, NEAR) this snaps to
    the coarser of the two venues' own real, exchange-published ticks — never
    an invented precision. Only use this when a study specifically needs
    discrete grid alignment (e.g. counting tick moves); for continuous
    comparisons (mid-price differencing, returns) use ``native_price``
    instead, since rounding here can only ever discard information.
    """
    contract = get_contract(symbol)
    price = to_decimal(raw_price)
    tick = canonical_grid_tick(contract)
    if tick == 0:
        return price
    steps = (price / tick).to_integral_value(rounding=ROUND_HALF_EVEN)
    return steps * tick


def canonical_base_size(symbol: str, venue: str, raw_size: object) -> Decimal:
    """Convert a raw reported quantity to base-asset coin units.

    ``base_size = raw_quantity * size_multiplier``. size_multiplier is 1 for
    every symbol/venue pair in the current frozen contract (verified
    per-symbol, not assumed — see contract.py's docstring), so this collapses
    to the identity for all 12 symbols today. The multiplier is still applied
    explicitly, rather than skipped, so a future symbol whose venue actually
    uses a contract multiplier != 1 is handled correctly instead of silently
    mis-converted.
    """
    contract = get_contract(symbol)
    venue = venue.lower()
    if venue == "binance":
        multiplier = contract.size_multiplier_binance
    elif venue == "bitget":
        multiplier = contract.size_multiplier_bitget
    else:
        raise ValueError(f"unknown venue {venue!r}; expected 'binance' or 'bitget'")
    return to_decimal(raw_size) * multiplier


def canonical_quote_notional(base_size: Decimal, price: Decimal) -> Decimal:
    """quote_notional = base_size x price. Universal formula for this
    12-symbol universe — see contract.py and the normalization-contract
    report for why no venue/symbol-specific exception is needed."""
    return base_size * price


def grid_aligned_base_size(symbol: str, raw_size: object) -> Decimal:
    """Same grid-alignment idea as ``grid_aligned_price`` but for lot size.
    Snaps to the coarser of the two venues' lot steps. Only 6/12 symbols
    (BTC, ETH, SOL, XRP, LINK, AVAX) actually move under this — see
    contract.py's per-symbol lot ratios."""
    contract = get_contract(symbol)
    size = to_decimal(raw_size)
    lot = max(contract.lot_binance, contract.lot_bitget)
    if lot == 0:
        return size
    steps = (size / lot).to_integral_value(rounding=ROUND_HALF_EVEN)
    return steps * lot
