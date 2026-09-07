from decimal import Decimal

from research.cross_venue.canonical import CanonicalBookState
from research.cross_venue.continuity import ContinuityStatus
from research.cross_venue.transitions import TransitionType, extract_transitions


def make_state(
    *,
    best_bid,
    best_ask,
    imbalance_base_l1,
    receive_monotonic_ms,
    exchange_event_ms=1_000,
    connection_id="conn-1",
    continuity_status=ContinuityStatus.VALID,
    venue="binance",
    symbol="BTCUSDT",
):
    return CanonicalBookState(
        venue=venue,
        symbol=symbol,
        exchange_event_ms=exchange_event_ms,
        receive_monotonic_ms=receive_monotonic_ms,
        connection_id=connection_id,
        source_schema="test",
        best_bid=Decimal(str(best_bid)) if best_bid is not None else None,
        best_ask=Decimal(str(best_ask)) if best_ask is not None else None,
        mid=None,
        spread_bps=None,
        bid_size_base_l1=None,
        ask_size_base_l1=None,
        bid_depth_base_l1_l5=None,
        ask_depth_base_l1_l5=None,
        bid_depth_quote_l1_l5=None,
        ask_depth_quote_l1_l5=None,
        imbalance_base_l1=Decimal(str(imbalance_base_l1)) if imbalance_base_l1 is not None else None,
        imbalance_base_l1_l5=None,
        imbalance_quote_l1=None,
        imbalance_quote_l1_l5=None,
        sequence_id=None,
        prev_sequence_id=None,
        continuity_status=continuity_status,
        quality_flags=frozenset(),
    )


def test_best_bid_change_detected():
    states = [
        make_state(best_bid=100, best_ask=101, imbalance_base_l1=0, receive_monotonic_ms=1.0),
        make_state(best_bid=100.5, best_ask=101, imbalance_base_l1=0, receive_monotonic_ms=2.0),
    ]
    transitions = list(extract_transitions(states))
    bid_transitions = [t for t in transitions if t.transition_type == TransitionType.BEST_BID_PRICE_CHANGE]
    assert len(bid_transitions) == 1
    assert bid_transitions[0].old_value == Decimal("100")
    assert bid_transitions[0].new_value == Decimal("100.5")
    assert bid_transitions[0].direction == "UP"


def test_best_ask_change_detected():
    states = [
        make_state(best_bid=100, best_ask=101, imbalance_base_l1=0, receive_monotonic_ms=1.0),
        make_state(best_bid=100, best_ask=100.9, imbalance_base_l1=0, receive_monotonic_ms=2.0),
    ]
    ask_transitions = [
        t for t in extract_transitions(states) if t.transition_type == TransitionType.BEST_ASK_PRICE_CHANGE
    ]
    assert len(ask_transitions) == 1
    assert ask_transitions[0].direction == "DOWN"


def test_imbalance_sign_change_detected():
    states = [
        make_state(best_bid=100, best_ask=101, imbalance_base_l1="0.2", receive_monotonic_ms=1.0),
        make_state(best_bid=100, best_ask=101, imbalance_base_l1="-0.3", receive_monotonic_ms=2.0),
    ]
    imb_transitions = [
        t for t in extract_transitions(states) if t.transition_type == TransitionType.IMBALANCE_SIGN_CHANGE
    ]
    assert len(imb_transitions) == 1
    assert imb_transitions[0].old_value == "POSITIVE"
    assert imb_transitions[0].new_value == "NEGATIVE"
    assert imb_transitions[0].direction == "POSITIVE_TO_NEGATIVE"


def test_identical_state_produces_no_transition():
    states = [
        make_state(best_bid=100, best_ask=101, imbalance_base_l1="0.1", receive_monotonic_ms=1.0),
        make_state(best_bid=100, best_ask=101, imbalance_base_l1="0.1", receive_monotonic_ms=2.0),
        make_state(best_bid=100, best_ask=101, imbalance_base_l1="0.1", receive_monotonic_ms=3.0),
    ]
    assert list(extract_transitions(states)) == []


def test_repeated_pushes_of_unchanged_book_collapse_to_zero_transitions():
    # Simulates Binance depth5@100ms firing on a fixed timer even when the
    # book has not moved: many identical messages must not become many
    # independent "events" (Objective 8 debiasing).
    states = [make_state(best_bid=100, best_ask=101, imbalance_base_l1=0, receive_monotonic_ms=float(i)) for i in range(50)]
    assert list(extract_transitions(states)) == []


def test_malformed_row_is_skipped_but_does_not_reset_last_known_value():
    states = [
        make_state(best_bid=100, best_ask=101, imbalance_base_l1=0, receive_monotonic_ms=1.0),
        make_state(
            best_bid=None, best_ask=None, imbalance_base_l1=None,
            receive_monotonic_ms=2.0, continuity_status=ContinuityStatus.MALFORMED_BOOK,
        ),
        make_state(best_bid=105, best_ask=106, imbalance_base_l1=0, receive_monotonic_ms=3.0),
    ]
    transitions = list(extract_transitions(states))
    bid_transitions = [t for t in transitions if t.transition_type == TransitionType.BEST_BID_PRICE_CHANGE]
    # Comparison should be against the last VALID state (100), not against
    # the malformed row's None, and not silently skipped entirely.
    assert len(bid_transitions) == 1
    assert bid_transitions[0].old_value == Decimal("100")
    assert bid_transitions[0].new_value == Decimal("105")


def test_transition_receive_monotonic_ms_is_non_decreasing_across_output():
    states = [
        make_state(best_bid=100 + i, best_ask=101 + i, imbalance_base_l1=0, receive_monotonic_ms=float(i))
        for i in range(10)
    ]
    transitions = list(extract_transitions(states))
    times = [t.transition_receive_monotonic_ms for t in transitions]
    assert times == sorted(times)


def test_transitions_carry_continuity_quality_and_connection_id():
    states = [
        make_state(best_bid=100, best_ask=101, imbalance_base_l1=0, receive_monotonic_ms=1.0, connection_id="conn-9"),
        make_state(
            best_bid=100.5, best_ask=101, imbalance_base_l1=0, receive_monotonic_ms=2.0,
            connection_id="conn-9", continuity_status=ContinuityStatus.GAP_DETECTED,
        ),
    ]
    transitions = list(extract_transitions(states))
    assert transitions[0].connection_id == "conn-9"
    assert transitions[0].continuity_quality == ContinuityStatus.GAP_DETECTED


def test_no_forward_looking_fields_exist_on_state_transition():
    # Guards against future scope creep: a StateTransition must never carry a
    # PnL, return, or label field.
    from dataclasses import fields

    from research.cross_venue.transitions import StateTransition

    field_names = {f.name for f in fields(StateTransition)}
    forbidden_substrings = ("pnl", "return", "label", "reward", "signal_strength")
    for name in field_names:
        lowered = name.lower()
        for forbidden in forbidden_substrings:
            assert forbidden not in lowered, f"StateTransition field {name!r} looks alpha-adjacent"
