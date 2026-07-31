"""Canonical production symbol allowlist parsing.

LIVE execution must have one explicit owner-controlled source of truth.  The
runtime may derive scanner and confirmation sets from this value, but it must
never fall back to the broad development watchlist or dynamically add markets.
"""

from __future__ import annotations

import re


_SYMBOL = re.compile(r"^[A-Z0-9]{2,24}USDT$")


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
    "SymbolAllowlistError",
    "canonical_symbol_csv",
    "parse_symbol_allowlist",
]
