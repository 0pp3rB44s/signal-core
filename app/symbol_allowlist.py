"""Canonical production symbol allowlist parsing.

LIVE execution must have one explicit owner-controlled source of truth.  The
runtime may derive scanner and confirmation sets from this value, but it must
never fall back to the broad development watchlist or dynamically add markets.
"""

from __future__ import annotations

import re


_SYMBOL = re.compile(r"^[A-Z0-9]{2,24}USDT$")

#: The symbols the owner has approved for LIVE execution in this release.
#: Exactly this set is accepted — not a superset, not "any allowlist".
OWNER_APPROVED_PRODUCTION_SYMBOLS = (
    "BTCUSDT",
    "SOLUSDT",
    "SUIUSDT",
    "XLMUSDT",
    "AVAXUSDT",
    "DOGEUSDT",
    "SEIUSDT",
    "TRXUSDT",
)

#: Owner-approved in principle but withheld from execution, with the reason.
#: WIFUSDT measured 6.91-7.01 bps across seven readings over twelve hours
#: against the unchanged 5.0 bps execution limit — a structural gap, not a
#: spike. It is not removed from future consideration: re-admitting it requires
#: a fresh read-only qualification plus a separate owner-approved config change.
#: The spread limit itself is deliberately left untouched.
CONDITIONAL_PRODUCTION_SYMBOLS: dict[str, str] = {
    "WIFUSDT": "CONDITIONAL_SPREAD",
}


class SymbolAllowlistError(ValueError):
    """The configured production allowlist is absent or ambiguous."""


def parse_symbol_allowlist(value: str, *, required: bool = False) -> tuple[str, ...]:
    raw = [part.strip().upper() for part in str(value or "").split(",") if part.strip()]
    if required and not raw:
        raise SymbolAllowlistError("PRODUCTION_SYMBOL_ALLOWLIST is required for LIVE execution")

    invalid = sorted({symbol for symbol in raw if not _SYMBOL.fullmatch(symbol)})
    if invalid:
        raise SymbolAllowlistError(
            "invalid production symbol(s): " + ",".join(invalid)
        )

    duplicates = sorted({symbol for symbol in raw if raw.count(symbol) > 1})
    if duplicates:
        raise SymbolAllowlistError(
            "duplicate production symbol(s): " + ",".join(duplicates)
        )

    return tuple(raw)


def canonical_symbol_csv(value: str, *, required: bool = False) -> str:
    return ",".join(parse_symbol_allowlist(value, required=required))


__all__ = [
    "OWNER_APPROVED_PRODUCTION_SYMBOLS",
    "SymbolAllowlistError",
    "canonical_symbol_csv",
    "parse_symbol_allowlist",
]
