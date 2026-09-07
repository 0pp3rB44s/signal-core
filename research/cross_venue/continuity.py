"""Objective 6 — continuity quality classification.

This module never invents integrity the collector did not evidence. For
Binance, the raw ``U``/``u``/``pu`` fields are honestly evaluated *here*, in
the research layer — the running collector stores them but never cross-checks
them (confirmed by code read; see the normalization-contract report), so any
GAP_DETECTED/VALID distinction for Binance is this module's own, newly-honest
computation over evidence the collector faithfully preserved, not a claim
about what the collector itself enforced live. For Bitget, this module
*consumes* the collector's own already-computed ``sequence_valid`` /
``stream_gaps`` diagnostics rather than recomputing them from raw seq/pseq
(the running Bitget state snapshots do not carry raw seq/pseq values, only
the check's result) — see ``canonical.py`` for exactly which fields feed in.
"""

from __future__ import annotations

from enum import Enum

# Reused, not invented: this is the exact same 2,000ms exchange-timestamp gap
# threshold already active in microflow/collector.py's own
# _record_channel_time() for books5 (see the normalization-contract report,
# Objective 5). Applying the identical, already-evidenced threshold
# symmetrically to Binance keeps this module from inventing a new arbitrary
# constant for the venue that lacks one.
STALE_STATE_GAP_MS = 2_000


class ContinuityStatus(str, Enum):
    VALID = "VALID"
    VALID_AFTER_SESSION_START = "VALID_AFTER_SESSION_START"
    GAP_DETECTED = "GAP_DETECTED"
    RECONNECT_BOUNDARY = "RECONNECT_BOUNDARY"
    SEQUENCE_UNVERIFIED = "SEQUENCE_UNVERIFIED"
    TIMESTAMP_INVALID = "TIMESTAMP_INVALID"
    MALFORMED_BOOK = "MALFORMED_BOOK"
    STALE_STATE = "STALE_STATE"


#: Statuses under which a canonical record's best_bid/best_ask/derived fields
#: are still numerically trustworthy enough to compare against a neighboring
#: record when extracting state transitions (Objective 7). MALFORMED_BOOK and
#: TIMESTAMP_INVALID are excluded — their price/size fields are None by
#: construction (see canonical.py) and cannot be compared at all.
TRANSITION_ELIGIBLE_STATUSES = frozenset(
    {
        ContinuityStatus.VALID,
        ContinuityStatus.VALID_AFTER_SESSION_START,
        ContinuityStatus.RECONNECT_BOUNDARY,
        ContinuityStatus.GAP_DETECTED,
        ContinuityStatus.SEQUENCE_UNVERIFIED,
        ContinuityStatus.STALE_STATE,
    }
)


def classify_continuity(
    *,
    is_malformed: bool,
    exchange_event_ms: int,
    prev_exchange_event_ms: int | None,
    connection_id: str,
    prev_connection_id: str | None,
    sequence_ok: bool | None,
) -> ContinuityStatus:
    """Pure classification function. No I/O, no mutation, no future
    information: every argument must come from the current row and, at most,
    one already-processed previous row for the same (venue, symbol).

    ``sequence_ok``: True if a real continuity check ran and passed, False if
    it ran and detected a break, None if no such check could be performed at
    all for this row (distinct from False — SEQUENCE_UNVERIFIED is not a gap,
    it is an honest "cannot tell").
    """
    if is_malformed:
        return ContinuityStatus.MALFORMED_BOOK

    if exchange_event_ms <= 0:
        return ContinuityStatus.TIMESTAMP_INVALID
    if (
        prev_exchange_event_ms is not None
        and prev_connection_id == connection_id
        and exchange_event_ms < prev_exchange_event_ms
    ):
        return ContinuityStatus.TIMESTAMP_INVALID

    if prev_connection_id is None:
        return ContinuityStatus.VALID_AFTER_SESSION_START
    if connection_id != prev_connection_id:
        return ContinuityStatus.RECONNECT_BOUNDARY

    if sequence_ok is False:
        return ContinuityStatus.GAP_DETECTED
    if sequence_ok is None:
        return ContinuityStatus.SEQUENCE_UNVERIFIED

    if (
        prev_exchange_event_ms is not None
        and (exchange_event_ms - prev_exchange_event_ms) > STALE_STATE_GAP_MS
    ):
        return ContinuityStatus.STALE_STATE

    return ContinuityStatus.VALID
