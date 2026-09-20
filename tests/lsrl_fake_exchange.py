"""In-memory Bitget-like client for LSRL live-executor tests. Records every mutating call.

It has NO market-order method on purpose: if the executor ever tried one, the test would
fail with AttributeError instead of silently 'working'.
"""
from __future__ import annotations

from typing import Any

from clients.bitget_base_client import BitgetOrderRejected
from clients.bitget_order_client import BitgetOrderClientMixin
from clients.clock_safety import ClockUnsafe


class FakeExchange:
    extract_fill_metrics = BitgetOrderClientMixin.extract_fill_metrics

    def __init__(self, *, bid: float = 149.95, ask: float = 150.05) -> None:
        self.bid, self.ask, self.mark = bid, ask, 150.0
        self.orders: dict[str, dict[str, Any]] = {}
        self.position = {"long": 0.0, "short": 0.0}
        self.stop_plan: dict[str, Any] | None = None
        self.calls: list[tuple] = []
        self.clock_ok = True
        self.lookup_unknown = False
        self.fail: dict[str, Exception] = {}
        self.leverage_calls: list[tuple] = []
        self._n = 0

    # ---------------------------------------------------------------- gates / helpers
    def _require_safe_clock_for_order(self) -> None:
        if not self.clock_ok:
            raise ClockUnsafe("exchange clock reference stale")

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise self.fail[name]

    def _new_id(self) -> str:
        self._n += 1
        return f"ORD{self._n:05d}"

    @staticmethod
    def extract_order_id(payload: dict | None) -> str:
        return str(((payload or {}).get("data") or {}).get("orderId") or "")

    # ---------------------------------------------------------------- mutating API
    def set_futures_leverage(self, symbol, leverage, hold_side, margin_mode="isolated", **_):
        self._require_safe_clock_for_order()
        self.calls.append(("set_leverage", symbol, leverage, hold_side))
        self.leverage_calls.append((symbol, leverage, hold_side))
        return {"data": {}}

    def place_futures_limit_order(self, symbol, direction, size, price, margin_mode="isolated",
                                  client_oid=None, post_only=True, **_):
        self._require_safe_clock_for_order()
        self._maybe_fail("place_futures_limit_order")
        crosses = price >= self.ask if direction == "LONG" else price <= self.bid
        if post_only and crosses:
            raise BitgetOrderRejected("post_only order would cross the book")
        oid = self._new_id()
        self.orders[client_oid] = {"clientOid": client_oid, "orderId": oid, "state": "live", "size": size,
                                   "price": price, "direction": direction, "tradeSide": "open",
                                   "post_only": post_only, "baseVolume": 0.0, "priceAvg": 0.0, "fee": 0.0}
        self.calls.append(("place_entry", client_oid, direction, size, price, post_only))
        return {"data": {"orderId": oid, "clientOid": client_oid}}

    def place_futures_limit_close_order(self, symbol, hold_side, size, price, margin_mode="isolated",
                                        client_oid=None, post_only=True, **_):
        self._require_safe_clock_for_order()
        self._maybe_fail("place_futures_limit_close_order")
        oid = self._new_id()
        self.orders[client_oid] = {"clientOid": client_oid, "orderId": oid, "state": "live", "size": size,
                                   "price": price, "direction": hold_side.upper(), "tradeSide": "close",
                                   "reduceOnly": "YES", "post_only": post_only, "baseVolume": 0.0,
                                   "priceAvg": 0.0, "fee": 0.0}
        self.calls.append(("place_tp", client_oid, hold_side, size, price, post_only))
        return {"data": {"orderId": oid, "clientOid": client_oid}}

    def cancel_futures_order(self, symbol, order_id=None, client_oid=None, **_):
        self._require_safe_clock_for_order()
        self.calls.append(("cancel", client_oid))
        order = self.orders.get(client_oid)
        if order is None:
            raise RuntimeError("order does not exist")
        if order["state"] == "live":
            order["state"] = "canceled" if order["baseVolume"] == 0 else "canceled"
        return {"data": {}}

    def move_futures_stop_loss(self, symbol, hold_side=None, trigger_price=None, direction=None,
                               cleanup_existing=True, reason=None, **_):
        self._maybe_fail("move_futures_stop_loss")
        size = self.position[hold_side]
        if size <= 0:
            raise RuntimeError("no position to protect")
        self.stop_plan = {"price": trigger_price, "size": size, "hold": hold_side}
        self.calls.append(("stop", trigger_price, size, reason))
        return {"data": {}}

    def cancel_all_futures_tpsl_orders(self, symbol, hold_side=None, **_):
        self.calls.append(("cancel_stop",))
        self.stop_plan = None
        return {"data": {}}

    def close_futures_position_full(self, symbol, direction, size, margin_mode="isolated", reason=None,
                                    cleanup_tpsl=False, **_):
        self.calls.append(("emergency_close", direction, size, reason))
        hold = "long" if direction == "LONG" else "short"
        self.position[hold] = 0.0
        self.stop_plan = None
        return {"status": "CLOSED", "avg_price": self.mark}

    # ---------------------------------------------------------------- read API
    def get_all_positions(self, *_a, **_k):
        rows = []
        for hold, qty in self.position.items():
            if qty > 0:
                rows.append({"symbol": "SOLUSDT", "holdSide": hold, "total": qty, "markPrice": self.mark})
        return {"data": rows}

    def find_order_by_client_oid(self, symbol, client_oid, product_type=None):
        if self.lookup_unknown:
            return {"status": "UNKNOWN", "order": None, "errors": ["boom"]}
        order = self.orders.get(client_oid)
        if order is None:
            return {"status": "ABSENT", "order": None, "errors": []}
        return {"status": "FOUND", "order": dict(order), "source": "fake", "errors": []}

    # ---------------------------------------------------------------- test-side simulation
    def fill(self, client_oid: str, qty: float | None = None, price: float | None = None, fee: float = 0.0) -> None:
        order = self.orders[client_oid]
        qty = order["size"] - order["baseVolume"] if qty is None else qty
        price = order["price"] if price is None else price
        total_notional = order["baseVolume"] * order["priceAvg"] + qty * price
        order["baseVolume"] += qty
        order["priceAvg"] = total_notional / order["baseVolume"]
        order["fee"] += fee
        order["state"] = "filled" if order["baseVolume"] >= order["size"] - 1e-9 else "partially_filled"
        hold = "long" if order["direction"] == "LONG" else "short"
        if order["tradeSide"] == "open":
            self.position[hold] += qty
        else:
            self.position[hold] -= qty

    def trigger_stop(self) -> None:
        assert self.stop_plan is not None
        self.position[self.stop_plan["hold"]] = 0.0
        self.stop_plan = None

    def by_kind(self, kind: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == kind]

    def mutating_calls(self) -> list[tuple]:
        return [c for c in self.calls if c[0] not in ()]
