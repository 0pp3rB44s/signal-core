"""Frozen 12-symbol cross-venue normalization contract.

Source of truth: ``reports/CROSS_VENUE_NORMALIZATION_CONTRACT_2026-09-07.md``,
whose own numbers were fetched live from each venue's public, unauthenticated
instrument-metadata endpoint on 2026-09-07 UTC:

  - Binance: GET https://fapi.binance.com/fapi/v1/exchangeInfo
  - Bitget:  GET https://api.bitget.com/api/v2/mix/market/contracts?productType=usdt-futures

Per that report's own instruction ("Do NOT call live exchange APIs during
normal analysis execution if stable metadata can be frozen from this
verified audit"), the values below are a frozen snapshot, not a live lookup.
If a 13th symbol or a metadata change is ever needed, re-fetch from the same
two endpoints and update ``METADATA_SOURCE_DATE`` alongside the new values —
do not silently interpolate or guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Mapping

NORMALIZATION_CONTRACT_VERSION = "2026-09-07-v1"
METADATA_SOURCE_DATE = "2026-09-07"
METADATA_SOURCE_BINANCE = "https://fapi.binance.com/fapi/v1/exchangeInfo"
METADATA_SOURCE_BITGET = "https://api.bitget.com/api/v2/mix/market/contracts?productType=usdt-futures"


class PriceComparability(str, Enum):
    """Objective 3 classification. NOT_COMPARABLE does not occur in this
    12-symbol universe (see the normalization-contract report) and is kept
    here only so a future symbol addition has somewhere honest to land if it
    turns out not to hold."""

    DIRECTLY_COMPARABLE = "DIRECTLY_COMPARABLE"
    NORMALIZABLE = "NORMALIZABLE"
    NOT_COMPARABLE = "NOT_COMPARABLE"


@dataclass(frozen=True)
class SymbolContract:
    symbol: str
    binance_symbol: str
    bitget_symbol: str
    base_asset: str
    quote_asset: str
    settlement_asset: str
    instrument_type: str
    tick_binance: Decimal
    tick_bitget: Decimal
    lot_binance: Decimal
    lot_bitget: Decimal
    # Both venues report raw quantity as base-asset coin units directly for
    # every symbol in this contract (confirmed per-symbol in the
    # normalization-contract report: Bitget's sizeMultiplier == minTradeNum
    # for all 12; Binance's LOT_SIZE.stepSize is a base-coin step by
    # definition of a USDS-M linear perpetual). This multiplier exists so the
    # formula stays correct and honest if a future symbol ever violates that
    # (multiplier != 1) rather than assuming it silently.
    size_multiplier_binance: Decimal
    size_multiplier_bitget: Decimal
    price_comparability: PriceComparability

    @property
    def price_tick_ratio_binance_over_bitget(self) -> Decimal:
        return self.tick_binance / self.tick_bitget

    @property
    def lot_ratio_binance_over_bitget(self) -> Decimal:
        return self.lot_binance / self.lot_bitget


def _d(value: str) -> Decimal:
    return Decimal(value)


# fmt: off
_CONTRACTS: tuple[SymbolContract, ...] = (
    SymbolContract("BTCUSDT", "BTCUSDT", "BTCUSDT", "BTC", "USDT", "USDT", "linear_perpetual_usdt_margined",
                   _d("0.10"), _d("0.1"), _d("0.001"), _d("0.0001"), _d("1"), _d("1"), PriceComparability.DIRECTLY_COMPARABLE),
    SymbolContract("ETHUSDT", "ETHUSDT", "ETHUSDT", "ETH", "USDT", "USDT", "linear_perpetual_usdt_margined",
                   _d("0.01"), _d("0.01"), _d("0.001"), _d("0.01"), _d("1"), _d("1"), PriceComparability.DIRECTLY_COMPARABLE),
    SymbolContract("SOLUSDT", "SOLUSDT", "SOLUSDT", "SOL", "USDT", "USDT", "linear_perpetual_usdt_margined",
                   _d("0.0100"), _d("0.001"), _d("0.01"), _d("0.1"), _d("1"), _d("1"), PriceComparability.NORMALIZABLE),
    SymbolContract("XRPUSDT", "XRPUSDT", "XRPUSDT", "XRP", "USDT", "USDT", "linear_perpetual_usdt_margined",
                   _d("0.0001"), _d("0.0001"), _d("0.1"), _d("1"), _d("1"), _d("1"), PriceComparability.DIRECTLY_COMPARABLE),
    SymbolContract("DOGEUSDT", "DOGEUSDT", "DOGEUSDT", "DOGE", "USDT", "USDT", "linear_perpetual_usdt_margined",
                   _d("0.000010"), _d("0.00001"), _d("1"), _d("1"), _d("1"), _d("1"), PriceComparability.DIRECTLY_COMPARABLE),
    SymbolContract("BNBUSDT", "BNBUSDT", "BNBUSDT", "BNB", "USDT", "USDT", "linear_perpetual_usdt_margined",
                   _d("0.010"), _d("0.01"), _d("0.01"), _d("0.01"), _d("1"), _d("1"), PriceComparability.DIRECTLY_COMPARABLE),
    SymbolContract("LINKUSDT", "LINKUSDT", "LINKUSDT", "LINK", "USDT", "USDT", "linear_perpetual_usdt_margined",
                   _d("0.001"), _d("0.001"), _d("0.01"), _d("1"), _d("1"), _d("1"), PriceComparability.DIRECTLY_COMPARABLE),
    SymbolContract("AVAXUSDT", "AVAXUSDT", "AVAXUSDT", "AVAX", "USDT", "USDT", "linear_perpetual_usdt_margined",
                   _d("0.0010"), _d("0.001"), _d("1"), _d("0.1"), _d("1"), _d("1"), PriceComparability.DIRECTLY_COMPARABLE),
    SymbolContract("SUIUSDT", "SUIUSDT", "SUIUSDT", "SUI", "USDT", "USDT", "linear_perpetual_usdt_margined",
                   _d("0.000100"), _d("0.0001"), _d("0.1"), _d("0.1"), _d("1"), _d("1"), PriceComparability.DIRECTLY_COMPARABLE),
    SymbolContract("HYPEUSDT", "HYPEUSDT", "HYPEUSDT", "HYPE", "USDT", "USDT", "linear_perpetual_usdt_margined",
                   _d("0.00100"), _d("0.001"), _d("0.01"), _d("0.01"), _d("1"), _d("1"), PriceComparability.DIRECTLY_COMPARABLE),
    SymbolContract("ZECUSDT", "ZECUSDT", "ZECUSDT", "ZEC", "USDT", "USDT", "linear_perpetual_usdt_margined",
                   _d("0.01"), _d("0.01"), _d("0.001"), _d("0.001"), _d("1"), _d("1"), PriceComparability.DIRECTLY_COMPARABLE),
    SymbolContract("NEARUSDT", "NEARUSDT", "NEARUSDT", "NEAR", "USDT", "USDT", "linear_perpetual_usdt_margined",
                   _d("0.0010"), _d("0.0001"), _d("1"), _d("1"), _d("1"), _d("1"), PriceComparability.NORMALIZABLE),
)
# fmt: on

SYMBOL_CONTRACTS: Mapping[str, SymbolContract] = {c.symbol: c for c in _CONTRACTS}
SUPPORTED_SYMBOLS: tuple[str, ...] = tuple(c.symbol for c in _CONTRACTS)

assert len(SUPPORTED_SYMBOLS) == 12, "frozen contract must cover exactly the 12-symbol research universe"


def get_contract(symbol: str) -> SymbolContract:
    try:
        return SYMBOL_CONTRACTS[symbol.upper()]
    except KeyError as exc:
        raise KeyError(
            f"{symbol!r} is not in the frozen 12-symbol normalization contract "
            f"({NORMALIZATION_CONTRACT_VERSION}); no venue API call will be made "
            f"automatically to resolve it"
        ) from exc


def canonical_grid_tick(contract: SymbolContract) -> Decimal:
    """The common (coarser) price tick across both venues.

    Identity for the 10 DIRECTLY_COMPARABLE symbols (both ticks equal). For
    the 2 NORMALIZABLE symbols this is the larger (coarser) of the two ticks
    — always the Binance tick in this frozen contract, but computed rather
    than hardcoded so the rule stays correct if that ever flips.
    """
    return max(contract.tick_binance, contract.tick_bitget)


def canonical_grid_lot(contract: SymbolContract) -> Decimal:
    """The common (coarser) lot step across both venues, same reasoning as
    ``canonical_grid_tick``."""
    return max(contract.lot_binance, contract.lot_bitget)
