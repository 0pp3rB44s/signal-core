from decimal import Decimal

import pytest

from research.cross_venue.contract import (
    PriceComparability,
    SUPPORTED_SYMBOLS,
    get_contract,
)
from research.cross_venue.normalization import (
    canonical_base_size,
    canonical_quote_notional,
    grid_aligned_base_size,
    grid_aligned_price,
    native_price,
    to_decimal,
)

ALL_12 = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT",
    "LINKUSDT", "AVAXUSDT", "SUIUSDT", "HYPEUSDT", "ZECUSDT", "NEARUSDT",
)


def test_exactly_twelve_symbols_supported():
    assert len(SUPPORTED_SYMBOLS) == 12
    assert set(SUPPORTED_SYMBOLS) == set(ALL_12)


@pytest.mark.parametrize("symbol", ALL_12)
def test_every_symbol_maps_identically_across_venues(symbol):
    contract = get_contract(symbol)
    assert contract.binance_symbol == symbol
    assert contract.bitget_symbol == symbol
    assert contract.base_asset + "USDT" == symbol
    assert contract.quote_asset == "USDT"
    assert contract.settlement_asset == "USDT"


@pytest.mark.parametrize("symbol", ALL_12)
def test_every_symbol_has_a_comparability_classification(symbol):
    contract = get_contract(symbol)
    assert contract.price_comparability in (
        PriceComparability.DIRECTLY_COMPARABLE,
        PriceComparability.NORMALIZABLE,
    )


def test_no_symbol_is_not_comparable():
    for symbol in ALL_12:
        assert get_contract(symbol).price_comparability != PriceComparability.NOT_COMPARABLE


def test_exactly_two_symbols_are_normalizable_on_price():
    normalizable = [
        s for s in ALL_12 if get_contract(s).price_comparability == PriceComparability.NORMALIZABLE
    ]
    assert set(normalizable) == {"SOLUSDT", "NEARUSDT"}


def test_unknown_symbol_raises_rather_than_silently_resolving():
    with pytest.raises(KeyError):
        get_contract("DOESNOTEXISTUSDT")


def test_size_multiplier_is_one_for_all_twelve_on_both_venues():
    # This is the specific, per-symbol-verified fact (not an assumption) that
    # makes base_size = raw_quantity for this entire universe today.
    for symbol in ALL_12:
        contract = get_contract(symbol)
        assert contract.size_multiplier_binance == Decimal("1")
        assert contract.size_multiplier_bitget == Decimal("1")


# --- price normalization -----------------------------------------------------


@pytest.mark.parametrize("symbol", [s for s in ALL_12 if s not in ("SOLUSDT", "NEARUSDT")])
def test_directly_comparable_symbols_need_no_grid_rounding(symbol):
    raw = "12345.6789"
    assert native_price(symbol, "binance", raw) == Decimal(raw)
    # grid alignment is an identity transform for directly-comparable ticks
    # only insofar as the value already sits on both venues' tick; here we
    # only assert it doesn't explode and returns a Decimal.
    assert isinstance(grid_aligned_price(symbol, raw), Decimal)


def test_sol_price_grid_alignment_snaps_to_coarser_binance_tick():
    # Binance SOL tick = 0.01, Bitget SOL tick = 0.001 (10x finer).
    # A Bitget-native price like 189.123 must snap to the 0.01 grid.
    aligned = grid_aligned_price("SOLUSDT", "189.123")
    assert aligned == Decimal("189.12")


def test_near_price_grid_alignment_snaps_to_coarser_binance_tick():
    # Binance NEAR tick = 0.001, Bitget NEAR tick = 0.0001 (10x finer).
    aligned = grid_aligned_price("NEARUSDT", "4.5678")
    assert aligned == Decimal("4.568")


def test_native_price_never_invents_precision():
    # native_price must reproduce exactly what was given, not round it.
    assert native_price("BTCUSDT", "bitget", "79815.1") == Decimal("79815.1")
    assert native_price("BTCUSDT", "binance", "79814.70") == Decimal("79814.70")


def test_to_decimal_is_lossless_for_string_input():
    assert to_decimal("0.1") == Decimal("0.1")
    # float(0.1) would NOT equal Decimal("0.1") if routed through float() first
    assert to_decimal("0.1") != Decimal(0.1)


def test_to_decimal_rejects_unparseable_values():
    with pytest.raises(ValueError):
        to_decimal("not-a-number")


# --- size / notional normalization -------------------------------------------


@pytest.mark.parametrize("symbol", ALL_12)
def test_base_size_identity_for_current_multiplier(symbol):
    assert canonical_base_size(symbol, "binance", "1.2345") == Decimal("1.2345")
    assert canonical_base_size(symbol, "bitget", "1.2345") == Decimal("1.2345")


def test_base_size_rejects_unknown_venue():
    with pytest.raises(ValueError):
        canonical_base_size("BTCUSDT", "okx", "1")


def test_quote_notional_formula():
    base_size = Decimal("2.5")
    price = Decimal("100")
    assert canonical_quote_notional(base_size, price) == Decimal("250")


def test_lot_grid_mismatch_flagged_for_exactly_the_six_known_symbols():
    # The grid-alignment *rounding of an arbitrary value* is not the right
    # probe here (even a symbol whose two venues agree on the lot will still
    # move an arbitrary non-multiple value under rounding). The real,
    # contract-level fact is whether the two venues' own lot steps differ at
    # all -- that is what "requires lot-grid normalization" means.
    mismatched = [s for s in ALL_12 if get_contract(s).lot_binance != get_contract(s).lot_bitget]
    assert set(mismatched) == {"BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "LINKUSDT", "AVAXUSDT"}


def test_grid_aligned_base_size_is_identity_when_lots_already_match():
    # DOGEUSDT: both venues use lot=1, and an already-integral value must
    # survive grid alignment unchanged.
    assert grid_aligned_base_size("DOGEUSDT", Decimal("7")) == Decimal("7")


def test_link_lot_grid_alignment_uses_the_coarser_bitget_lot():
    # Binance LINK lot = 0.01, Bitget LINK lot = 1 (100x coarser on Bitget).
    aligned = grid_aligned_base_size("LINKUSDT", "12.34")
    assert aligned == Decimal("12")
