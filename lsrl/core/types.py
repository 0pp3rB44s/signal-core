"""LSRL_V1 events, intents and records. Pure data; stdlib only.

Time convention: every event carries `t_ms`, the moment the information became
AVAILABLE to the engine (local receipt time). The engine is only ever driven in
non-decreasing `t_ms` order and never looks ahead of the event it is processing.
That single rule is what makes replay of a recorded stream reproduce live
decisions exactly.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, ClassVar


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sgn(self) -> int:
        return 1 if self is Direction.LONG else -1

    @property
    def opposite(self) -> "Direction":
        return Direction.SHORT if self is Direction.LONG else Direction.LONG


class Side(str, Enum):
    """Order-book side. A LONG setup sweeps a BID pool; a SHORT sweeps an ASK pool."""
    BID = "BID"
    ASK = "ASK"


@dataclass(frozen=True, slots=True)
class SymbolRules:
    symbol: str
    tick_size: float
    qty_step: float
    min_qty: float
    min_notional_usdt: float


# ------------------------------------------------------------------ events
@dataclass(frozen=True, slots=True)
class DepthSnapshot:
    TYPE: ClassVar[str] = "depth"
    t_ms: int
    exchange_ts_ms: int | None
    bids: tuple[tuple[float, float], ...]   # (price, size), best first
    asks: tuple[tuple[float, float], ...]   # (price, size), best first
    seq: int | None = None                  # exchange sequence of the update this state reflects
    epoch: int = 0                          # increments at every causal reconstruction of the book

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0 if self.bids and self.asks else 0.0

    @property
    def spread_bps(self) -> float:
        m = self.mid
        return (self.best_ask - self.best_bid) / m * 10_000.0 if m > 0 else float("inf")

    @property
    def crossed(self) -> bool:
        return bool(self.bids and self.asks and self.best_bid >= self.best_ask)


@dataclass(frozen=True, slots=True)
class TradeTick:
    TYPE: ClassVar[str] = "trade"
    t_ms: int
    exchange_ts_ms: int
    price: float
    size: float
    aggressor: str          # "BUY" (taker lifted asks) | "SELL" (taker hit bids)
    trade_id: str = ""


@dataclass(frozen=True, slots=True)
class BarClosed:
    TYPE: ClassVar[str] = "bar"
    t_ms: int               # when the engine learned the bar was complete (>= close_ts_ms)
    tf_s: int               # 60 or 900
    open_ts_ms: int
    close_ts_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True, slots=True)
class NavEvent:
    TYPE: ClassVar[str] = "nav"
    t_ms: int
    nav_usdt: float
    source: str = ""


@dataclass(frozen=True, slots=True)
class FillEvent:
    TYPE: ClassVar[str] = "fill"
    t_ms: int
    setup_id: str
    tag: str
    kind: str               # ENTRY | TP1 | TP2 | TP3 | SL | OTHER_CLOSE
    price: float
    qty: float
    fee_usdt: float = 0.0
    liquidity: str = "maker"
    rung: int | None = None


@dataclass(frozen=True, slots=True)
class OrderEvent:
    TYPE: ClassVar[str] = "order"
    t_ms: int
    setup_id: str
    tag: str
    status: str             # ACCEPTED | REJECTED | CANCELLED | EXPIRED
    reason: str = ""


@dataclass(frozen=True, slots=True)
class TimerTick:
    TYPE: ClassVar[str] = "tick"
    t_ms: int


@dataclass(frozen=True, slots=True)
class FeedHealth:
    """Derived by the observation layer. ok=False means the market view is UNCERTAIN: the engine must
    forget everything that depended on it and may not act on new signals until ok=True again."""
    TYPE: ClassVar[str] = "feed_health"
    t_ms: int
    ok: bool
    reason: str = ""
    epoch: int = 0


# ------------------------------------------------------------------ raw feed inputs (what is RECORDED)
@dataclass(frozen=True, slots=True)
class RawBook:
    """One raw depth push, exactly as received (price/size strings preserved). action: snapshot | update."""
    TYPE: ClassVar[str] = "raw_book"
    t_ms: int                               # local receipt
    exchange_ts_ms: int | None
    action: str
    seq: int | None
    pseq: int | None
    bids: tuple[tuple[str, str], ...]
    asks: tuple[tuple[str, str], ...]
    conn_id: str = ""


@dataclass(frozen=True, slots=True)
class RawTrade:
    """One raw public trade, exactly as received. side is the TAKER (aggressor) side: buy | sell."""
    TYPE: ClassVar[str] = "raw_trade"
    t_ms: int                               # local receipt
    exchange_ts_ms: int | None
    action: str                             # snapshot (history at subscribe) | update (live)
    trade_id: str
    price: str
    size: str
    side: str
    conn_id: str = ""


@dataclass(frozen=True, slots=True)
class FeedMarker:
    """Connection lifecycle / session boundary. kind: SESSION_START | CONNECT | SUBSCRIBED | DISCONNECT | RESET."""
    TYPE: ClassVar[str] = "feed_marker"
    t_ms: int
    kind: str
    conn_id: str = ""
    detail: str = ""                        # free text or a JSON string (SESSION_START carries the restored state)


EVENT_TYPES: dict[str, type] = {
    cls.TYPE: cls for cls in (DepthSnapshot, TradeTick, BarClosed, NavEvent, FillEvent, OrderEvent, TimerTick,
                              FeedHealth, RawBook, RawTrade, FeedMarker)
}

RAW_FEED_TYPES = (RawBook, RawTrade, FeedMarker)


def event_to_dict(event: Any) -> dict[str, Any]:
    body = asdict(event)
    body["type"] = event.TYPE
    return body


def event_from_dict(data: dict[str, Any]) -> Any:
    body = dict(data)
    cls = EVENT_TYPES[body.pop("type")]
    if cls is DepthSnapshot:
        body["bids"] = tuple((float(p), float(s)) for p, s in body["bids"])
        body["asks"] = tuple((float(p), float(s)) for p, s in body["asks"])
    elif cls is RawBook:
        body["bids"] = tuple((str(p), str(s)) for p, s in body["bids"])
        body["asks"] = tuple((str(p), str(s)) for p, s in body["asks"])
    return cls(**body)


# ------------------------------------------------------------------ intents
@dataclass(frozen=True, slots=True)
class PlaceEntry:
    TYPE: ClassVar[str] = "place_entry"
    setup_id: str
    tag: str
    rung: int
    direction: Direction
    price: float
    qty: float
    expires_at_ms: int


@dataclass(frozen=True, slots=True)
class CancelOrder:
    TYPE: ClassVar[str] = "cancel_order"
    setup_id: str
    tag: str
    reason: str


@dataclass(frozen=True, slots=True)
class SetStop:
    """Desired exchange-side protective stop for the WHOLE current position."""
    TYPE: ClassVar[str] = "set_stop"
    setup_id: str
    direction: Direction
    price: float
    qty: float
    reason: str


@dataclass(frozen=True, slots=True)
class TargetOrder:
    tag: str
    level: str              # TP1 | TP2 | TP3
    price: float
    qty: float


@dataclass(frozen=True, slots=True)
class SetTargets:
    """Desired resting reduce-only take-profit orders (replaces the previous set)."""
    TYPE: ClassVar[str] = "set_targets"
    setup_id: str
    direction: Direction
    targets: tuple[TargetOrder, ...]
    reason: str


Intent = PlaceEntry | CancelOrder | SetStop | SetTargets


def intent_to_dict(intent: Intent) -> dict[str, Any]:
    body = asdict(intent)
    body["type"] = intent.TYPE
    if "direction" in body:
        body["direction"] = intent.direction.value
    return body


@dataclass(slots=True)
class Record:
    """A persisted observability row (setups, rejections, lifecycle, decisions)."""
    kind: str
    t_ms: int
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "t_ms": self.t_ms, "data": self.data}
