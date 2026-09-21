"""Conservative simulated exchange for BACKTEST / SHADOW / PAPER (pure, stdlib only).

Execution assumptions (all deliberately pessimistic, all recorded in `assumptions()`):
  * a passive limit fills only when a LATER completed 1m bar trades THROUGH its
    price by at least `fill_through_ticks` (default 1) -- a mere touch never fills;
  * an order is eligible only for bars whose OPEN is at/after its placement time:
    no fill inside the bar in which the decision was taken (no same-bar lookahead);
  * entries fill before the stop is evaluated in the same bar, and a bar that
    reaches the stop fills the STOP, never a target (worst-case intrabar order);
  * a take-profit can never fill in the same bar in which the position was
    (re)sized, and only after its own placement;
  * the stop is a stop-market: it fills at the stop price moved against the
    position by `stop_slippage_bps`, and pays the taker fee; passive fills pay maker.
Nothing here reads a clock, a network or an exchange.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from lsrl.core.config import LSRLConfig
from lsrl.core.types import (BarClosed, CancelOrder, Direction, FillEvent, Intent, OrderEvent,
                             PlaceEntry, SetStop, SetTargets, SymbolRules)


@dataclass(slots=True)
class SimOrder:
    tag: str
    kind: str                     # ENTRY | TP1 | TP2 | TP3
    setup_id: str
    direction: Direction
    price: float
    qty: float
    placed_t_ms: int
    rung: int | None = None
    live: bool = True


@dataclass(slots=True)
class SimStop:
    setup_id: str
    direction: Direction
    price: float
    qty: float


class SimExchange:
    def __init__(self, rules: SymbolRules, cfg: LSRLConfig, *, fill_through_ticks: int = 1,
                 maker_fee_rate: float | None = None, taker_fee_rate: float | None = None,
                 stop_slippage_bps: float | None = None) -> None:
        self.rules, self.cfg = rules, cfg
        self.through = fill_through_ticks * rules.tick_size
        self.maker = cfg.assumed_maker_fee_rate if maker_fee_rate is None else maker_fee_rate
        self.taker = cfg.assumed_taker_fee_rate if taker_fee_rate is None else taker_fee_rate
        self.slip_bps = cfg.assumed_stop_slippage_bps if stop_slippage_bps is None else stop_slippage_bps
        self.orders: dict[str, SimOrder] = {}
        self.stop: SimStop | None = None
        self.position_qty = 0.0
        self.direction: Direction | None = None

    def snapshot(self) -> dict:
        return {"orders": {tag: {**asdict(o), "direction": o.direction.value} for tag, o in self.orders.items()},
                "stop": None if self.stop is None else {**asdict(self.stop), "direction": self.stop.direction.value},
                "position_qty": self.position_qty, "direction": None if self.direction is None else self.direction.value}

    def restore(self, state: dict) -> None:
        self.orders = {tag: SimOrder(**{**o, "direction": Direction(o["direction"])})
                       for tag, o in state["orders"].items()}
        st = state["stop"]
        self.stop = None if st is None else SimStop(**{**st, "direction": Direction(st["direction"])})
        self.position_qty = state["position_qty"]
        self.direction = None if state["direction"] is None else Direction(state["direction"])

    def assumptions(self) -> dict:
        return {"fill_rule": f"THROUGH_BY_{self.through}", "same_bar_fills": False,
                "intrabar_order": "ENTRIES_THEN_STOP_THEN_TARGETS", "maker_fee_rate": self.maker,
                "taker_fee_rate": self.taker, "stop_slippage_bps": self.slip_bps,
                "tape_fills": False}

    # ------------------------------------------------------------------ intents
    def apply(self, intents: list[Intent], t_ms: int) -> list[OrderEvent]:
        events: list[OrderEvent] = []
        for it in intents:
            if isinstance(it, PlaceEntry):
                self.orders[it.tag] = SimOrder(it.tag, "ENTRY", it.setup_id, it.direction, it.price,
                                               it.qty, t_ms, rung=it.rung)
                events.append(OrderEvent(t_ms, it.setup_id, it.tag, "ACCEPTED"))
            elif isinstance(it, CancelOrder):
                order = self.orders.get(it.tag)
                if order is not None and order.live:
                    order.live = False
                    events.append(OrderEvent(t_ms, it.setup_id, it.tag, "CANCELLED", it.reason))
            elif isinstance(it, SetStop):
                if it.qty <= 0:
                    self.stop = None
                else:
                    self.stop = SimStop(it.setup_id, it.direction, it.price, it.qty)
            elif isinstance(it, SetTargets):
                for o in self.orders.values():
                    if o.kind in ("TP1", "TP2", "TP3") and o.setup_id == it.setup_id:
                        o.live = False
                for tgt in it.targets:
                    self.orders[tgt.tag] = SimOrder(tgt.tag, tgt.level, it.setup_id, it.direction,
                                                    tgt.price, tgt.qty, t_ms)
        return events

    # ------------------------------------------------------------------ bar processing
    def entry_fills(self, bar: BarClosed) -> list[FillEvent]:
        fills: list[FillEvent] = []
        entries = [o for o in self.orders.values()
                   if o.kind == "ENTRY" and o.live and bar.open_ts_ms >= o.placed_t_ms]
        for o in sorted(entries, key=lambda x: x.rung or 0):
            long_ = o.direction is Direction.LONG
            crossed = bar.low <= o.price - self.through if long_ else bar.high >= o.price + self.through
            if not crossed:
                continue
            o.live = False
            self.position_qty += o.qty
            self.direction = o.direction
            fills.append(FillEvent(bar.t_ms, o.setup_id, o.tag, "ENTRY", o.price, o.qty,
                                   fee_usdt=o.price * o.qty * self.maker, liquidity="maker", rung=o.rung))
        return fills

    def exit_fills(self, bar: BarClosed) -> list[FillEvent]:
        if self.position_qty <= 1e-12 or self.direction is None:
            return []
        long_ = self.direction is Direction.LONG
        if self.stop is not None:
            hit = bar.low <= self.stop.price if long_ else bar.high >= self.stop.price
            if hit:
                slip = self.stop.price * self.slip_bps / 10_000.0
                price = self.stop.price - slip if long_ else self.stop.price + slip
                qty = self.position_qty
                self.position_qty = 0.0
                for o in self.orders.values():
                    if o.kind != "ENTRY":
                        o.live = False
                tag = f"lsrl-{self.stop.setup_id}-stop"
                fill = FillEvent(bar.t_ms, self.stop.setup_id, tag, "SL", price, qty,
                                 fee_usdt=price * qty * self.taker, liquidity="taker")
                self.stop = None
                return [fill]
        fills: list[FillEvent] = []
        targets = [o for o in self.orders.values()
                   if o.kind in ("TP1", "TP2", "TP3") and o.live and bar.open_ts_ms >= o.placed_t_ms]
        for o in sorted(targets, key=lambda x: (x.price if long_ else -x.price)):
            reached = bar.high >= o.price + self.through if long_ else bar.low <= o.price - self.through
            if not reached:
                continue
            qty = min(o.qty, self.position_qty)
            if qty <= 1e-12:
                continue
            o.live = False
            self.position_qty -= qty
            fills.append(FillEvent(bar.t_ms, o.setup_id, o.tag, o.kind, o.price, qty,
                                   fee_usdt=o.price * qty * self.maker, liquidity="maker"))
        return fills
