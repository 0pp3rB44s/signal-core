"""Objective 7 + 8 — frozen state-transition primitives and cadence
debiasing.

Exactly three primitive types are implemented, per this mission's explicit
instruction not to add more "because easy":

  1. BEST_BID_PRICE_CHANGE
  2. BEST_ASK_PRICE_CHANGE
  3. IMBALANCE_SIGN_CHANGE (top-of-book, i.e. imbalance_base_l1's sign)

No PnL, no forward-return labels, no trading decisions appear anywhere in
this module or are computable from its output alone.

Cadence debiasing (Objective 8) is structural, not a special case bolted on:
- A transition is only emitted when a value actually differs from the last
  *comparable state* for that (venue, symbol) — never from raw message
  arrival. A stream that pushes the same book on every tick (Binance's
  ``depth5@100ms`` timer fires regardless of whether anything changed)
  therefore produces zero transitions for those repeated pushes, with no
  extra bookkeeping required — the comparison is against the last accepted
  *value*, not the last *message*.
- Binance's much higher-frequency ``bookTicker`` stream is not accepted as
  canonicalizer input at all (see canonical.py's module docstring) — it
  structurally cannot dominate a comparison it is never part of.
- Records whose continuity_status leaves best_bid/best_ask undefined
  (MALFORMED_BOOK) are skipped for comparison purposes but do not reset the
  "last known value" — a single malformed row does not manufacture a
  spurious transition against whatever valid value preceded it, nor does it
  erase that value from consideration when the next valid row arrives.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Iterable, Iterator

from .canonical import CanonicalBookState
from .continuity import ContinuityStatus, TRANSITION_ELIGIBLE_STATUSES


class TransitionType(str, Enum):
    BEST_BID_PRICE_CHANGE = "BEST_BID_PRICE_CHANGE"
    BEST_ASK_PRICE_CHANGE = "BEST_ASK_PRICE_CHANGE"
    IMBALANCE_SIGN_CHANGE = "IMBALANCE_SIGN_CHANGE"


@dataclass(frozen=True)
class StateTransition:
    venue: str
    symbol: str
    transition_type: TransitionType
    prior_state_time: float  # receive_monotonic_ms of the previous compared state
    transition_exchange_time: int
    transition_receive_monotonic_ms: float
    old_value: Decimal | str
    new_value: Decimal | str
    direction: str | None
    connection_id: str
    continuity_quality: ContinuityStatus


def _sign(value: Decimal) -> str:
    if value > 0:
        return "POSITIVE"
    if value < 0:
        return "NEGATIVE"
    return "ZERO"


def extract_transitions(records: Iterable[CanonicalBookState]) -> Iterator[StateTransition]:
    """Stream state transitions from an ordered, single-(venue,symbol)
    sequence of canonical records.

    Callers with a mixed multi-symbol/multi-venue stream must group by
    (venue, symbol) themselves and call this once per group, in
    ``receive_monotonic_ms`` order — this function does not do that grouping
    itself so it stays a simple, bounded-memory, single-pass generator (only
    ever holding the single "last comparable state" in memory, matching
    Objective 12).
    """
    last: CanonicalBookState | None = None

    for record in records:
        if record.continuity_status not in TRANSITION_ELIGIBLE_STATUSES:
            # Malformed/timestamp-invalid rows carry no usable best_bid/ask;
            # skip comparison but do not clear `last` — see module docstring.
            continue

        if last is not None and last.venue == record.venue and last.symbol == record.symbol:
            if (
                last.best_bid is not None
                and record.best_bid is not None
                and record.best_bid != last.best_bid
            ):
                yield StateTransition(
                    venue=record.venue,
                    symbol=record.symbol,
                    transition_type=TransitionType.BEST_BID_PRICE_CHANGE,
                    prior_state_time=last.receive_monotonic_ms,
                    transition_exchange_time=record.exchange_event_ms,
                    transition_receive_monotonic_ms=record.receive_monotonic_ms,
                    old_value=last.best_bid,
                    new_value=record.best_bid,
                    direction="UP" if record.best_bid > last.best_bid else "DOWN",
                    connection_id=record.connection_id,
                    continuity_quality=record.continuity_status,
                )

            if (
                last.best_ask is not None
                and record.best_ask is not None
                and record.best_ask != last.best_ask
            ):
                yield StateTransition(
                    venue=record.venue,
                    symbol=record.symbol,
                    transition_type=TransitionType.BEST_ASK_PRICE_CHANGE,
                    prior_state_time=last.receive_monotonic_ms,
                    transition_exchange_time=record.exchange_event_ms,
                    transition_receive_monotonic_ms=record.receive_monotonic_ms,
                    old_value=last.best_ask,
                    new_value=record.best_ask,
                    direction="UP" if record.best_ask > last.best_ask else "DOWN",
                    connection_id=record.connection_id,
                    continuity_quality=record.continuity_status,
                )

            if last.imbalance_base_l1 is not None and record.imbalance_base_l1 is not None:
                old_sign = _sign(last.imbalance_base_l1)
                new_sign = _sign(record.imbalance_base_l1)
                if old_sign != new_sign:
                    yield StateTransition(
                        venue=record.venue,
                        symbol=record.symbol,
                        transition_type=TransitionType.IMBALANCE_SIGN_CHANGE,
                        prior_state_time=last.receive_monotonic_ms,
                        transition_exchange_time=record.exchange_event_ms,
                        transition_receive_monotonic_ms=record.receive_monotonic_ms,
                        old_value=old_sign,
                        new_value=new_sign,
                        direction=f"{old_sign}_TO_{new_sign}",
                        connection_id=record.connection_id,
                        continuity_quality=record.continuity_status,
                    )

        last = record
