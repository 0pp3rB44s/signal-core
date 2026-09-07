"""Objective 1 + Objective 5 — canonical market-state record and book
validation.

Two entry points build a ``CanonicalBookState`` from the two raw stream types
that carry full L1-L5 depth on their respective venues:

  - ``canonicalize_binance_depth5_row`` for ``research_binance_v2_depth`` rows
  - ``canonicalize_bitget_state_row`` for ``microflow_state_v2`` rows

Binance's ``bookTicker`` stream is deliberately NOT given a canonicalizer
here. It carries only L1 (no L1-L5 depth, so most of the required canonical
fields would be permanently None), and — more importantly — the
normalization-contract report measured its native update rate at ~500-700x
Bitget's books5/state cadence (median 0.2ms vs ~149ms). Feeding it into the
same canonical stream as Bitget's L5 snapshots is exactly the "high-frequency
Binance messages mechanically dominate Bitget" failure mode Objective 8 warns
against. The comparable, already-recommended pairing is Binance depth5
against Bitget books5/state, both event/throttle-driven at a similar order of
magnitude.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence

from .continuity import ContinuityStatus, classify_continuity
from .contract import get_contract

Level = tuple[Decimal, Decimal]

_MALFORMED_FLAGS = frozenset(
    {
        "MISSING_BID_SIDE",
        "MISSING_ASK_SIDE",
        "INVALID_PRICE",
        "NON_POSITIVE_SIZE",
        "DUPLICATE_LEVELS",
        "UNSORTED_BIDS",
        "UNSORTED_ASKS",
        "UNPARSEABLE_LEVEL",
        "CROSSED_BOOK",
    }
)


@dataclass(frozen=True)
class CanonicalBookState:
    venue: str
    symbol: str
    exchange_event_ms: int
    receive_monotonic_ms: float
    connection_id: str
    source_schema: str

    best_bid: Decimal | None
    best_ask: Decimal | None
    mid: Decimal | None
    spread_bps: Decimal | None

    bid_size_base_l1: Decimal | None
    ask_size_base_l1: Decimal | None
    bid_depth_base_l1_l5: Decimal | None
    ask_depth_base_l1_l5: Decimal | None
    bid_depth_quote_l1_l5: Decimal | None
    ask_depth_quote_l1_l5: Decimal | None

    imbalance_base_l1: Decimal | None
    imbalance_base_l1_l5: Decimal | None
    imbalance_quote_l1: Decimal | None
    imbalance_quote_l1_l5: Decimal | None

    sequence_id: int | None
    prev_sequence_id: int | None

    continuity_status: ContinuityStatus
    quality_flags: frozenset[str] = field(default_factory=frozenset)

    def as_dict(self) -> dict:
        def conv(v):
            if isinstance(v, Decimal):
                return str(v)
            if isinstance(v, ContinuityStatus):
                return v.value
            if isinstance(v, frozenset):
                return sorted(v)
            return v

        return {k: conv(v) for k, v in self.__dict__.items()}


def _parse_levels(raw_levels: Sequence, side: str) -> tuple[list[Level], set[str]]:
    flags: set[str] = set()
    parsed: list[Level] = []
    seen_prices: set[Decimal] = set()
    for level in raw_levels or []:
        try:
            price = Decimal(str(level[0]))
            size = Decimal(str(level[1]))
        except (InvalidOperation, IndexError, TypeError, ValueError):
            flags.add("UNPARSEABLE_LEVEL")
            continue
        if price <= 0:
            flags.add("INVALID_PRICE")
        if size <= 0:
            flags.add("NON_POSITIVE_SIZE")
        if price in seen_prices:
            flags.add("DUPLICATE_LEVELS")
        seen_prices.add(price)
        parsed.append((price, size))

    if not parsed:
        flags.add(f"MISSING_{side.upper()}_SIDE")
        return parsed, flags

    prices = [p for p, _ in parsed]
    if side == "bid" and prices != sorted(prices, reverse=True):
        flags.add("UNSORTED_BIDS")
    if side == "ask" and prices != sorted(prices):
        flags.add("UNSORTED_ASKS")
    if len(parsed) < 5:
        flags.add("INSUFFICIENT_LEVELS")

    return parsed, flags


def _level_sums(levels: list[Level]) -> tuple[Decimal, Decimal, Decimal]:
    top5 = levels[:5]
    if not top5:
        return Decimal(0), Decimal(0), Decimal(0)
    l1_base = top5[0][1]
    l1_l5_base = sum((size for _, size in top5), Decimal(0))
    l1_l5_quote = sum((price * size for price, size in top5), Decimal(0))
    return l1_base, l1_l5_base, l1_l5_quote


def _imbalance(bid_value: Decimal | None, ask_value: Decimal | None) -> Decimal | None:
    if bid_value is None or ask_value is None:
        return None
    total = bid_value + ask_value
    if total == 0:
        return None
    return (bid_value - ask_value) / total


def _build(
    *,
    venue: str,
    symbol: str,
    exchange_event_ms: int,
    receive_monotonic_ms: float,
    connection_id: str,
    source_schema: str,
    bids_raw: Sequence,
    asks_raw: Sequence,
    sequence_id: int | None,
    sequence_ok: bool | None,
    prev,
) -> CanonicalBookState:
    contract = get_contract(symbol)
    multiplier = (
        contract.size_multiplier_binance if venue == "binance" else contract.size_multiplier_bitget
    )

    bids, bid_flags = _parse_levels(bids_raw, "bid")
    asks, ask_flags = _parse_levels(asks_raw, "ask")
    flags = bid_flags | ask_flags

    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    if best_bid is not None and best_ask is not None and best_bid >= best_ask:
        flags = flags | {"CROSSED_BOOK"}

    is_malformed = bool(flags & _MALFORMED_FLAGS)

    prev_exchange_event_ms = prev.exchange_event_ms if prev is not None else None
    prev_connection_id = prev.connection_id if prev is not None else None

    status = classify_continuity(
        is_malformed=is_malformed,
        exchange_event_ms=exchange_event_ms,
        prev_exchange_event_ms=prev_exchange_event_ms,
        connection_id=connection_id,
        prev_connection_id=prev_connection_id,
        sequence_ok=sequence_ok,
    )

    if is_malformed:
        return CanonicalBookState(
            venue=venue,
            symbol=symbol,
            exchange_event_ms=exchange_event_ms,
            receive_monotonic_ms=receive_monotonic_ms,
            connection_id=connection_id,
            source_schema=source_schema,
            best_bid=None,
            best_ask=None,
            mid=None,
            spread_bps=None,
            bid_size_base_l1=None,
            ask_size_base_l1=None,
            bid_depth_base_l1_l5=None,
            ask_depth_base_l1_l5=None,
            bid_depth_quote_l1_l5=None,
            ask_depth_quote_l1_l5=None,
            imbalance_base_l1=None,
            imbalance_base_l1_l5=None,
            imbalance_quote_l1=None,
            imbalance_quote_l1_l5=None,
            sequence_id=sequence_id,
            prev_sequence_id=prev.sequence_id if prev is not None else None,
            continuity_status=status,
            quality_flags=frozenset(flags),
        )

    bid_l1_raw, bid_l1_l5_raw, bid_quote_l1_l5_raw = _level_sums(bids)
    ask_l1_raw, ask_l1_l5_raw, ask_quote_l1_l5_raw = _level_sums(asks)

    bid_size_base_l1 = bid_l1_raw * multiplier
    ask_size_base_l1 = ask_l1_raw * multiplier
    bid_depth_base_l1_l5 = bid_l1_l5_raw * multiplier
    ask_depth_base_l1_l5 = ask_l1_l5_raw * multiplier
    bid_depth_quote_l1_l5 = bid_quote_l1_l5_raw * multiplier
    ask_depth_quote_l1_l5 = ask_quote_l1_l5_raw * multiplier

    mid = (best_bid + best_ask) / 2
    spread_bps = ((best_ask - best_bid) / mid) * Decimal(10_000) if mid != 0 else None

    bid_quote_l1 = bid_size_base_l1 * best_bid
    ask_quote_l1 = ask_size_base_l1 * best_ask

    return CanonicalBookState(
        venue=venue,
        symbol=symbol,
        exchange_event_ms=exchange_event_ms,
        receive_monotonic_ms=receive_monotonic_ms,
        connection_id=connection_id,
        source_schema=source_schema,
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        spread_bps=spread_bps,
        bid_size_base_l1=bid_size_base_l1,
        ask_size_base_l1=ask_size_base_l1,
        bid_depth_base_l1_l5=bid_depth_base_l1_l5,
        ask_depth_base_l1_l5=ask_depth_base_l1_l5,
        bid_depth_quote_l1_l5=bid_depth_quote_l1_l5,
        ask_depth_quote_l1_l5=ask_depth_quote_l1_l5,
        imbalance_base_l1=_imbalance(bid_size_base_l1, ask_size_base_l1),
        imbalance_base_l1_l5=_imbalance(bid_depth_base_l1_l5, ask_depth_base_l1_l5),
        imbalance_quote_l1=_imbalance(bid_quote_l1, ask_quote_l1),
        imbalance_quote_l1_l5=_imbalance(bid_depth_quote_l1_l5, ask_depth_quote_l1_l5),
        sequence_id=sequence_id,
        prev_sequence_id=prev.sequence_id if prev is not None else None,
        continuity_status=status,
        quality_flags=frozenset(flags),
    )


def canonicalize_binance_depth5_row(
    row: Mapping, prev: CanonicalBookState | None
) -> CanonicalBookState:
    """Build a canonical record from one ``research_binance_v2_depth`` row.

    Sequence continuity (``prev_final_update_id`` chaining to the prior row's
    ``final_update_id``) is checked here, honestly, as new research-layer
    logic — the collector itself stores these fields but never compares them
    (see module docstring / continuity.py).
    """
    symbol = str(row["symbol"]).upper()
    connection_id = str(row.get("connection_id") or "")
    exchange_event_ms = int(row.get("transaction_time_ms") or row.get("event_timestamp_ms") or 0)
    receive_monotonic_ms = float(row["receive_monotonic_ms"])
    sequence_id = row.get("final_update_id")
    sequence_id = int(sequence_id) if sequence_id is not None else None
    prev_final_update_id = row.get("prev_final_update_id")

    if (
        prev is not None
        and prev.connection_id == connection_id
        and prev.sequence_id is not None
        and prev_final_update_id is not None
    ):
        sequence_ok: bool | None = int(prev_final_update_id) == int(prev.sequence_id)
    else:
        sequence_ok = None

    return _build(
        venue="binance",
        symbol=symbol,
        exchange_event_ms=exchange_event_ms,
        receive_monotonic_ms=receive_monotonic_ms,
        connection_id=connection_id,
        source_schema=str(row.get("schema_version") or ""),
        bids_raw=row.get("bids") or [],
        asks_raw=row.get("asks") or [],
        sequence_id=sequence_id,
        sequence_ok=sequence_ok,
        prev=prev,
    )


def canonicalize_bitget_state_row(
    row: Mapping, prev: CanonicalBookState | None
) -> CanonicalBookState:
    """Build a canonical record from one ``microflow_state_v2`` row.

    Sequence continuity is *consumed*, not recomputed: this trusts
    ``freshness.sequence_valid`` exactly as ``MicroflowSymbolState.update_book``
    computed it. Caveat, disclosed rather than hidden: that field defaults to
    True both when a check genuinely passed AND when no ``seq``/``pseq`` was
    ever supplied for that update (see continuity.py's Bitget discussion) —
    this canonicalizer does not attempt to distinguish those two cases beyond
    what the collector's own field already tells it, per this mission's
    instruction to consume the existing diagnostic rather than invent a new
    one.
    """
    symbol = str(row["symbol"]).upper()
    book = row.get("book") or {}
    freshness = row.get("freshness") or {}
    connection_id = str(freshness.get("connection_id") or "")
    exchange_event_ms = int(row.get("timestamp_exchange") or 0)
    receive_monotonic_ms = float(row["receive_monotonic_ms"])

    sequence_valid = freshness.get("sequence_valid")
    sequence_ok: bool | None = bool(sequence_valid) if sequence_valid is not None else None

    return _build(
        venue="bitget",
        symbol=symbol,
        exchange_event_ms=exchange_event_ms,
        receive_monotonic_ms=receive_monotonic_ms,
        connection_id=connection_id,
        source_schema=str(row.get("schema_version") or ""),
        bids_raw=book.get("bid_levels") or [],
        asks_raw=book.get("ask_levels") or [],
        sequence_id=None,  # Bitget state rows do not carry a raw seq/pseq value, only the check result
        sequence_ok=sequence_ok,
        prev=prev,
    )
