"""Signal model: the only object a Strategy is allowed to produce.

Deterministic and auditable. A Signal carries no hidden mutable state and has
no side effects -- creating one does not touch the exchange, place an order,
size a position, or send a notification.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


@dataclass(frozen=True)
class Signal:
    """A strategy's opinion at one point in time. Nothing more.

    timestamp: candle timestamp (ms, UTC) the signal was generated on -- i.e.
        the close time of `current_bar` passed to Strategy.evaluate().
    symbol: instrument the signal concerns.
    side: LONG / SHORT / FLAT. FLAT means "no new position" (not "close").
    entry_reference_price: the price the strategy observed when forming its
        opinion (informational only -- the backtester decides the actual fill).
    stop_reference: the strategy's proposed stop-loss level, if any. Position
        sizing (risk.py) and the backtester's stop logic use this.
    strategy_name: identifies which strategy produced the signal.
    reason: short human-readable explanation, for the trade journal.
    metadata: free-form extra fields a strategy may want to carry through to
        the journal. Never used by the backtester for execution decisions
        unless a field is explicitly documented as such by that strategy.
    """

    timestamp: int
    symbol: str
    side: Side
    entry_reference_price: float | None = None
    stop_reference: float | None = None
    strategy_name: str = ""
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
