from research.cross_venue.continuity import (
    STALE_STATE_GAP_MS,
    ContinuityStatus,
    TRANSITION_ELIGIBLE_STATUSES,
    classify_continuity,
)

BASE_KW = dict(
    is_malformed=False,
    exchange_event_ms=10_000,
    prev_exchange_event_ms=9_000,
    connection_id="conn-1",
    prev_connection_id="conn-1",
    sequence_ok=True,
)


def _classify(**overrides):
    kw = dict(BASE_KW)
    kw.update(overrides)
    return classify_continuity(**kw)


def test_malformed_takes_priority_over_everything_else():
    status = _classify(is_malformed=True, connection_id="conn-2", sequence_ok=False)
    assert status == ContinuityStatus.MALFORMED_BOOK


def test_zero_exchange_time_is_timestamp_invalid():
    assert _classify(exchange_event_ms=0) == ContinuityStatus.TIMESTAMP_INVALID


def test_negative_exchange_time_is_timestamp_invalid():
    assert _classify(exchange_event_ms=-5) == ContinuityStatus.TIMESTAMP_INVALID


def test_time_reversal_within_same_connection_is_timestamp_invalid():
    status = _classify(exchange_event_ms=100, prev_exchange_event_ms=200, connection_id="conn-1", prev_connection_id="conn-1")
    assert status == ContinuityStatus.TIMESTAMP_INVALID


def test_time_reversal_across_a_reconnect_is_not_timestamp_invalid():
    # A new connection legitimately restarts the exchange's own clock context
    # (a fresh snapshot); this must be classified as a reconnect, not corruption.
    status = _classify(exchange_event_ms=100, prev_exchange_event_ms=200, connection_id="conn-2", prev_connection_id="conn-1")
    assert status == ContinuityStatus.RECONNECT_BOUNDARY


def test_first_ever_record_is_valid_after_session_start():
    status = _classify(prev_connection_id=None, prev_exchange_event_ms=None)
    assert status == ContinuityStatus.VALID_AFTER_SESSION_START


def test_connection_id_change_is_reconnect_boundary():
    status = _classify(connection_id="conn-2", prev_connection_id="conn-1")
    assert status == ContinuityStatus.RECONNECT_BOUNDARY


def test_sequence_ok_false_is_gap_detected():
    assert _classify(sequence_ok=False) == ContinuityStatus.GAP_DETECTED


def test_sequence_ok_none_is_sequence_unverified():
    assert _classify(sequence_ok=None) == ContinuityStatus.SEQUENCE_UNVERIFIED


def test_large_time_gap_with_verified_sequence_is_stale_state():
    status = _classify(
        exchange_event_ms=9_000 + STALE_STATE_GAP_MS + 1,
        prev_exchange_event_ms=9_000,
        sequence_ok=True,
    )
    assert status == ContinuityStatus.STALE_STATE


def test_small_time_gap_with_verified_sequence_is_valid():
    status = _classify(
        exchange_event_ms=9_000 + STALE_STATE_GAP_MS - 1,
        prev_exchange_event_ms=9_000,
        sequence_ok=True,
    )
    assert status == ContinuityStatus.VALID


def test_reconnect_boundary_takes_priority_over_sequence_check():
    # A broken sequence right after a reconnect is expected (new session) and
    # must not be double-reported as a GAP_DETECTED on top of the boundary.
    status = _classify(connection_id="conn-2", prev_connection_id="conn-1", sequence_ok=False)
    assert status == ContinuityStatus.RECONNECT_BOUNDARY


def test_transition_eligible_statuses_exclude_malformed_and_timestamp_invalid():
    assert ContinuityStatus.MALFORMED_BOOK not in TRANSITION_ELIGIBLE_STATUSES
    assert ContinuityStatus.TIMESTAMP_INVALID not in TRANSITION_ELIGIBLE_STATUSES
    assert ContinuityStatus.VALID in TRANSITION_ELIGIBLE_STATUSES
    assert ContinuityStatus.GAP_DETECTED in TRANSITION_ELIGIBLE_STATUSES
