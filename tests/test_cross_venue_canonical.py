from decimal import Decimal

from research.cross_venue.canonical import (
    canonicalize_binance_depth5_row,
    canonicalize_bitget_state_row,
)
from research.cross_venue.continuity import ContinuityStatus


def binance_row(
    *,
    symbol="BTCUSDT",
    bids=None,
    asks=None,
    connection_id="conn-1",
    transaction_time_ms=1_000,
    receive_monotonic_ms=500.0,
    final_update_id=110,
    prev_final_update_id=90,
):
    bids = bids if bids is not None else [
        ["100.00", "1.0"], ["99.99", "2.0"], ["99.98", "3.0"], ["99.97", "4.0"], ["99.96", "5.0"],
    ]
    asks = asks if asks is not None else [
        ["100.01", "1.5"], ["100.02", "2.5"], ["100.03", "3.5"], ["100.04", "4.5"], ["100.05", "5.5"],
    ]
    return {
        "symbol": symbol,
        "connection_id": connection_id,
        "transaction_time_ms": transaction_time_ms,
        "event_timestamp_ms": transaction_time_ms,
        "receive_monotonic_ms": receive_monotonic_ms,
        "schema_version": "research_binance_v2_depth",
        "bids": bids,
        "asks": asks,
        "first_update_id": (final_update_id - 10) if final_update_id is not None else None,
        "final_update_id": final_update_id,
        "prev_final_update_id": prev_final_update_id,
    }


def bitget_row(
    *,
    symbol="BTCUSDT",
    bid_levels=None,
    ask_levels=None,
    connection_id="conn-1",
    timestamp_exchange=1_000,
    receive_monotonic_ms=500.0,
    sequence_valid=True,
):
    bid_levels = bid_levels if bid_levels is not None else [
        [100.00, 1.0], [99.99, 2.0], [99.98, 3.0], [99.97, 4.0], [99.96, 5.0],
    ]
    ask_levels = ask_levels if ask_levels is not None else [
        [100.01, 1.5], [100.02, 2.5], [100.03, 3.5], [100.04, 4.5], [100.05, 5.5],
    ]
    return {
        "symbol": symbol,
        "schema_version": "microflow_state_v2",
        "timestamp_exchange": timestamp_exchange,
        "receive_monotonic_ms": receive_monotonic_ms,
        "freshness": {"connection_id": connection_id, "sequence_valid": sequence_valid},
        "book": {"bid_levels": bid_levels, "ask_levels": ask_levels},
    }


# --- happy path: L1/L5 depth + imbalance -------------------------------------


def test_binance_l1_l5_depth_and_imbalance():
    record = canonicalize_binance_depth5_row(binance_row(), prev=None)
    assert record.best_bid == Decimal("100.00")
    assert record.best_ask == Decimal("100.01")
    assert record.mid == Decimal("100.005")
    assert record.bid_size_base_l1 == Decimal("1.0")
    assert record.ask_size_base_l1 == Decimal("1.5")
    assert record.bid_depth_base_l1_l5 == Decimal("15.0")  # 1+2+3+4+5
    assert record.ask_depth_base_l1_l5 == Decimal("17.5")  # 1.5+2.5+3.5+4.5+5.5
    expected_bid_quote = (
        Decimal("100.00") * Decimal("1.0")
        + Decimal("99.99") * Decimal("2.0")
        + Decimal("99.98") * Decimal("3.0")
        + Decimal("99.97") * Decimal("4.0")
        + Decimal("99.96") * Decimal("5.0")
    )
    assert record.bid_depth_quote_l1_l5 == expected_bid_quote
    assert record.imbalance_base_l1 == (Decimal("1.0") - Decimal("1.5")) / (Decimal("1.0") + Decimal("1.5"))
    assert record.continuity_status == ContinuityStatus.VALID_AFTER_SESSION_START


def test_bitget_l1_l5_depth_and_imbalance_matches_binance_formula():
    record = canonicalize_bitget_state_row(bitget_row(), prev=None)
    assert record.best_bid == Decimal("100.00")
    assert record.best_ask == Decimal("100.01")
    assert record.bid_depth_base_l1_l5 == Decimal("15.0")
    assert record.ask_depth_base_l1_l5 == Decimal("17.5")
    assert record.imbalance_base_l1_l5 == (Decimal("15.0") - Decimal("17.5")) / (Decimal("15.0") + Decimal("17.5"))


def test_spread_bps_formula():
    record = canonicalize_binance_depth5_row(binance_row(), prev=None)
    expected = (Decimal("100.01") - Decimal("100.00")) / Decimal("100.005") * Decimal(10_000)
    assert record.spread_bps == expected


# --- malformed book detection -------------------------------------------------


def test_missing_bid_side_is_malformed():
    record = canonicalize_binance_depth5_row(binance_row(bids=[]), prev=None)
    assert record.continuity_status == ContinuityStatus.MALFORMED_BOOK
    assert "MISSING_BID_SIDE" in record.quality_flags
    assert record.best_bid is None
    assert record.imbalance_base_l1 is None


def test_missing_ask_side_is_malformed():
    record = canonicalize_binance_depth5_row(binance_row(asks=[]), prev=None)
    assert record.continuity_status == ContinuityStatus.MALFORMED_BOOK
    assert "MISSING_ASK_SIDE" in record.quality_flags


def test_crossed_book_is_malformed():
    crossed_bids = [["100.05", "1.0"]]
    crossed_asks = [["100.00", "1.0"]]  # best_ask < best_bid
    record = canonicalize_binance_depth5_row(binance_row(bids=crossed_bids, asks=crossed_asks), prev=None)
    assert record.continuity_status == ContinuityStatus.MALFORMED_BOOK
    assert "CROSSED_BOOK" in record.quality_flags


def test_unsorted_bids_flagged_malformed():
    unsorted_bids = [["99.98", "1.0"], ["100.00", "1.0"]]  # should be descending
    record = canonicalize_binance_depth5_row(binance_row(bids=unsorted_bids), prev=None)
    assert record.continuity_status == ContinuityStatus.MALFORMED_BOOK
    assert "UNSORTED_BIDS" in record.quality_flags


def test_unsorted_asks_flagged_malformed():
    unsorted_asks = [["100.05", "1.0"], ["100.01", "1.0"]]  # should be ascending
    record = canonicalize_binance_depth5_row(binance_row(asks=unsorted_asks), prev=None)
    assert record.continuity_status == ContinuityStatus.MALFORMED_BOOK
    assert "UNSORTED_ASKS" in record.quality_flags


def test_negative_size_flagged_malformed():
    bad_bids = [["100.00", "-1.0"], ["99.99", "2.0"]]
    record = canonicalize_binance_depth5_row(binance_row(bids=bad_bids), prev=None)
    assert record.continuity_status == ContinuityStatus.MALFORMED_BOOK
    assert "NON_POSITIVE_SIZE" in record.quality_flags


def test_zero_price_flagged_malformed():
    bad_asks = [["0", "1.0"], ["100.02", "1.0"]]
    record = canonicalize_binance_depth5_row(binance_row(asks=bad_asks), prev=None)
    assert record.continuity_status == ContinuityStatus.MALFORMED_BOOK
    assert "INVALID_PRICE" in record.quality_flags


def test_duplicate_price_levels_flagged_malformed():
    dup_bids = [["100.00", "1.0"], ["100.00", "2.0"]]
    record = canonicalize_binance_depth5_row(binance_row(bids=dup_bids), prev=None)
    assert record.continuity_status == ContinuityStatus.MALFORMED_BOOK
    assert "DUPLICATE_LEVELS" in record.quality_flags


def test_insufficient_levels_is_flagged_but_not_malformed():
    # Fewer than 5 levels is a real-world possibility (thin book); it should
    # be flagged for downstream awareness but must not be treated as
    # equivalent to actual corruption.
    thin_bids = [["100.00", "1.0"], ["99.99", "2.0"]]
    record = canonicalize_binance_depth5_row(binance_row(bids=thin_bids), prev=None)
    assert "INSUFFICIENT_LEVELS" in record.quality_flags
    assert record.continuity_status != ContinuityStatus.MALFORMED_BOOK
    assert record.best_bid == Decimal("100.00")


def test_unparseable_level_is_skipped_and_flagged():
    bad_bids = [["not-a-price", "1.0"], ["99.99", "2.0"], ["99.98", "3.0"], ["99.97", "4.0"], ["99.96", "5.0"]]
    record = canonicalize_binance_depth5_row(binance_row(bids=bad_bids), prev=None)
    assert "UNPARSEABLE_LEVEL" in record.quality_flags


# --- venue-specific continuity wiring ----------------------------------------


def test_binance_gap_detected_when_prev_final_update_id_does_not_chain():
    first = canonicalize_binance_depth5_row(binance_row(final_update_id=110), prev=None)
    # a real gap: the second row's prev_final_update_id should equal 110 but doesn't
    second = canonicalize_binance_depth5_row(
        binance_row(final_update_id=130, prev_final_update_id=999, transaction_time_ms=1_100),
        prev=first,
    )
    assert second.continuity_status == ContinuityStatus.GAP_DETECTED


def test_binance_valid_when_prev_final_update_id_chains_correctly():
    first = canonicalize_binance_depth5_row(binance_row(final_update_id=110), prev=None)
    second = canonicalize_binance_depth5_row(
        binance_row(final_update_id=130, prev_final_update_id=110, transaction_time_ms=1_100),
        prev=first,
    )
    assert second.continuity_status == ContinuityStatus.VALID


def test_bitget_gap_propagates_from_freshness_sequence_valid_false():
    first = canonicalize_bitget_state_row(bitget_row(sequence_valid=True), prev=None)
    second = canonicalize_bitget_state_row(
        bitget_row(sequence_valid=False, timestamp_exchange=1_100), prev=first
    )
    assert second.continuity_status == ContinuityStatus.GAP_DETECTED


def test_bitget_valid_when_sequence_valid_true_after_first_row():
    first = canonicalize_bitget_state_row(bitget_row(sequence_valid=True), prev=None)
    second = canonicalize_bitget_state_row(
        bitget_row(sequence_valid=True, timestamp_exchange=1_100), prev=first
    )
    assert second.continuity_status == ContinuityStatus.VALID


def test_binance_sequence_unverified_when_sequence_fields_absent():
    # A row that (hypothetically) arrives without final_update_id/
    # prev_final_update_id gives the canonicalizer nothing to check against
    # for the *next* row -- the next row's sequence_ok must honestly report
    # "cannot tell" (SEQUENCE_UNVERIFIED), not silently assume continuity.
    first = canonicalize_binance_depth5_row(binance_row(final_update_id=None), prev=None)
    assert first.sequence_id is None
    second = canonicalize_binance_depth5_row(
        binance_row(final_update_id=130, prev_final_update_id=110, transaction_time_ms=1_100),
        prev=first,
    )
    assert second.continuity_status == ContinuityStatus.SEQUENCE_UNVERIFIED


def test_reconnect_boundary_detected_on_connection_id_change():
    first = canonicalize_binance_depth5_row(binance_row(connection_id="conn-1"), prev=None)
    second = canonicalize_binance_depth5_row(
        binance_row(connection_id="conn-2", transaction_time_ms=1_100), prev=first
    )
    assert second.continuity_status == ContinuityStatus.RECONNECT_BOUNDARY


def test_no_function_in_the_package_accepts_a_future_row_parameter():
    """Structural proxy for 'no future information required': every public
    function's signature only ever accepts the current row plus, at most,
    one already-computed *previous* record -- never anything named after a
    future/next/lookahead row."""
    import inspect

    from research.cross_venue import canonical, continuity, streaming, transitions

    forbidden = ("next_row", "future", "lookahead", "look_ahead", "forward")
    for module in (canonical, continuity, streaming, transitions):
        for name, obj in vars(module).items():
            if inspect.isfunction(obj) and obj.__module__ == module.__name__:
                params = set(inspect.signature(obj).parameters)
                for bad in forbidden:
                    assert not any(bad in p for p in params), (
                        f"{module.__name__}.{name} accepts a future-looking parameter"
                    )
