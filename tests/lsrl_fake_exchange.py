"""In-memory Bitget-like client for LSRL live-executor tests. Records every mutating call.

It has NO market-order method on purpose: if the executor ever tried one, the test would fail with
AttributeError instead of silently 'working'.

Behaviours are limited to what the OFFICIAL Bitget documentation (classic futures API, read 2026-09-20) or this
repository's own incident notes establish:
  * hedge mode: a close order carries side = the position side being closed (long -> buy) + tradeSide=close;
  * order `state`: live / partially_filled / filled / canceled; cancelling a filled order fails with
    'order does not exist' (43001);
  * a post-only order that would cross is refused; (repo incident) a post-only order can also be ACCEPTED and then
    cancelled by the exchange;
  * a stop plan the position-level model cancels ~1s after accepting it (repo incident 2026-08-01): the real client
    then returns verified=False / reason_detail=replacement_absent_after_settle;
  * a triggered stop is a MARKET order (orderSource loss_market) whose fill is visible in order history with its
    own orderId and priceAvg;
  * hedge mode: resting reduce-only close limits OCCUPY the position; a market close is sized against what is
    left after them (docs: Place Order notes);
  * PRESET STOP (docs: Place Order `presetStopLossPrice`, echoed by Get Order Detail). The documentation does NOT say how it
    materialises at a fill, so the fake models EACH possibility and the tests must hold under all of them:
        preset_model = "NONE"            the exchange ignores it (an attestation that lied)
                       "PLAN_PER_ORDER"  a loss_plan per entry order, sized to what that order has filled
                       "POSITION_LEVEL"  ONE pos_loss for the whole position (extra fills change nothing)
                       "REJECT"          the order is refused
    and, from this repo's 2026-08-01 incident, `move_futures_stop_loss` follows the real client: an active pos_loss is
    modified in place (POSITION_LEVEL), otherwise loss plans are replaced (PLAN_LEVEL).
"""
from __future__ import annotations

from typing import Any

from clients.bitget_base_client import BitgetOrderRejected
from clients.bitget_order_client import BitgetOrderClientMixin
from clients.clock_safety import ClockUnsafe
from lsrl.adapters.process_down import ProcessDownProtection
from lsrl.adapters.authority import _authority_for_tests

#: What a verified attestation grants, for tests that are not about the attestation itself.
TEST_PROTECTION = ProcessDownProtection("PLAN_LEVEL", "BITGET_DEMO", "0" * 40, "0" * 64)

#: A TRADE authority for fixtures whose NAV is far above the micro-live band (its capital veto is off). The real capital bounds are tested with a
#: consumed ceremony identity in tests/test_lsrl_capital_authority.py.
TEST_AUTHORITY = _authority_for_tests()


def contract_row_for(rules) -> dict[str, Any]:
    """A Bitget contract-config row whose tick/step/minimums equal `rules` (pricePlace x priceEndStep)."""
    place = 0
    while abs(round(rules.tick_size * 10 ** place) / 10 ** place - rules.tick_size) > 1e-12 and place < 8:
        place += 1
    step = round(rules.tick_size * 10 ** place)
    vol_place = 0
    while abs(round(rules.qty_step * 10 ** vol_place) / 10 ** vol_place - rules.qty_step) > 1e-12 and vol_place < 8:
        vol_place += 1
    return {"symbol": rules.symbol, "baseCoin": "SOL", "quoteCoin": "USDT", "makerFeeRate": "0.0002",
            "takerFeeRate": "0.0006", "minTradeNum": str(rules.min_qty), "priceEndStep": str(step),
            "volumePlace": str(vol_place), "pricePlace": str(place), "sizeMultiplier": str(rules.qty_step),
            "symbolType": "perpetual", "minTradeUSDT": str(rules.min_notional_usdt), "symbolStatus": "normal",
            "offTime": "-1", "limitOpenTime": "-1", "maxOrderQty": "100000", "maxMarketOrderQty": "20000"}


class FakeExchange:
    extract_fill_metrics = BitgetOrderClientMixin.extract_fill_metrics

    def __init__(self, *, bid: float = 149.95, ask: float = 150.05, rules=None) -> None:
        self.bid, self.ask, self.mark = bid, ask, 150.0
        self.orders: dict[str, dict[str, Any]] = {}
        self.position = {"long": 0.0, "short": 0.0}
        self.plans: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self.position_history: list[dict[str, Any]] = []
        self.calls: list[tuple] = []
        self.clock_ok = True
        self.lookup_unknown = False
        self.fail: dict[str, Exception] = {}
        self.leverage_calls: list[tuple] = []
        self.now_ms = 1_800_000_000_000
        self.contract = contract_row_for(rules) if rules is not None else None
        self.account = {"posMode": "hedge_mode", "marginMode": "isolated", "accountEquity": "1000"}
        self.fee_rates = {"makerFeeRate": "0.0002", "takerFeeRate": "0.0006"}
        # fault injection
        self.stop_vanishes = False           # the exchange cancels the accepted plan right away
        self.place_first_tolerated = True    # a loss_plan placed BESIDE another one survives (else the newcomer is cancelled)
        self.stop_lies = False               # the client claims verified=True but no plan exists
        self.strict_occupancy = True         # limit closes occupy the position for market closes
        self.foreign_orders: list[dict[str, Any]] = []
        self.outage = False                  # every read and write fails (network loss / exchange outage)
        self.preset_model = "NONE"           # how a preset stop materialises at a fill (see the module docstring)
        self.fail_counts: dict[str, int] = {}                 # method -> number of upcoming calls that fail transiently
        self._n = 0

    # ---------------------------------------------------------------- stub accessors (compat with old tests)
    @property
    def stop_plan(self) -> dict[str, Any] | None:
        losses = [p for p in self.plans if "loss" in p["planType"]]
        if not losses:
            return None
        p = losses[-1]                                            # numeric convenience view of the raw plan row
        return {**p, "price": float(p["triggerPrice"]), "size": float(p["size"]), "hold": p["posSide"]}

    @stop_plan.setter
    def stop_plan(self, value) -> None:
        if value is None:
            self.plans = [p for p in self.plans if "loss" not in p["planType"]]

    # ---------------------------------------------------------------- gates / helpers
    def _require_safe_clock_for_order(self) -> None:
        if not self.clock_ok:
            raise ClockUnsafe("exchange clock reference stale")

    def _maybe_fail(self, name: str) -> None:
        if self.outage:
            raise ConnectionError(f"exchange unreachable ({name})")
        if name in self.fail:
            raise self.fail[name]
        if self.fail_counts.get(name, 0) > 0:
            self.fail_counts[name] -= 1
            raise ConnectionError(f"transient failure ({name})")

    def _new_id(self) -> str:
        self._n += 1
        return f"ORD{self._n:05d}"

    @staticmethod
    def extract_order_id(payload: dict | None) -> str:
        return str(((payload or {}).get("data") or {}).get("orderId") or "")

    @staticmethod
    def _row(o: dict[str, Any]) -> dict[str, Any]:
        row = dict(o)
        row["status"] = o["state"]
        row["posSide"] = "long" if o["direction"] == "LONG" else "short"
        row["cancelReason"] = o.get("cancelReason", "")
        if o.get("presetStopLossPrice"):
            row["presetStopLossPrice"] = str(o["presetStopLossPrice"])
        return row

    def occupied(self, hold: str) -> float:
        return sum(o["size"] - o["baseVolume"] for o in self.orders.values()
                   if o["tradeSide"] == "close" and o["state"] in ("live", "partially_filled")
                   and ("long" if o["direction"] == "LONG" else "short") == hold)

    # ---------------------------------------------------------------- public / read API
    def get_contracts(self, product_type, symbol=None, **_):
        self.calls.append(("get_contracts", symbol))
        self._maybe_fail("get_contracts")
        return {"code": "00000", "data": [self.contract] if self.contract else []}

    def get_symbol_account(self, symbol, product_type=None, margin_coin="USDT", **_):
        self._maybe_fail("get_symbol_account")
        return {"code": "00000", "data": dict(self.account)}

    def get_trade_fee_rate(self, symbol, business_type="mix", **_):
        self._maybe_fail("get_trade_fee_rate")
        return {"code": "00000", "data": dict(self.fee_rates)}

    def get_all_positions(self, *_a, **_k):
        self._maybe_fail("get_all_positions")
        rows = []
        for hold, qty in self.position.items():
            if qty > 1e-12:
                rows.append({"symbol": "SOLUSDT", "holdSide": hold, "total": qty, "markPrice": self.mark})
        return {"data": rows}

    def get_pending_orders(self, symbol=None, product_type=None, limit=100, **_):
        self._maybe_fail("get_pending_orders")
        if self.lookup_unknown:
            raise RuntimeError("boom")
        rows = [self._row(o) for o in self.orders.values() if o["state"] in ("live", "partially_filled")]
        rows += self.foreign_orders
        return {"data": {"entrustedList": rows, "endId": None}}

    def find_order_by_client_oid(self, symbol, client_oid, product_type=None):
        if self.lookup_unknown or self.outage or self.fail_counts.get("find_order_by_client_oid", 0) > 0:
            if self.fail_counts.get("find_order_by_client_oid", 0) > 0:
                self.fail_counts["find_order_by_client_oid"] -= 1
            return {"status": "UNKNOWN", "order": None, "errors": ["boom"]}
        order = self.orders.get(client_oid)
        if order is None:
            return {"status": "ABSENT", "order": None, "errors": []}
        return {"status": "FOUND", "order": self._row(order), "source": "fake", "errors": []}

    def get_tpsl_orders(self, product_type=None, symbol=None, plan_type="profit_loss"):
        self._maybe_fail("get_tpsl_orders")
        return {"code": "00000", "data": {"entrustedList": [dict(p) for p in self.plans], "endId": None}}

    def get_order_history(self, product_type=None, symbol=None, limit=50):
        self._maybe_fail("get_order_history")
        return {"data": {"entrustedList": [dict(r) for r in self.history[-limit:]], "endId": None}}

    def get_position_history(self, product_type=None, symbol=None, start_time_ms=None, end_time_ms=None,
                             limit=100, id_less_than=None):
        self._maybe_fail("get_position_history")
        return {"code": "00000", "data": {"list": [dict(r) for r in self.position_history][:limit], "endId": None}}

    # ---------------------------------------------------------------- mutating API
    def set_futures_leverage(self, symbol, leverage, hold_side, margin_mode="isolated", **_):
        self._require_safe_clock_for_order()
        self.calls.append(("set_leverage", symbol, leverage, hold_side))
        self.leverage_calls.append((symbol, leverage, hold_side))
        return {"data": {}}

    def place_futures_limit_order(self, symbol, direction, size, price, margin_mode="isolated",
                                  client_oid=None, post_only=True, preset_stop_loss_price=None, **_):
        self._require_safe_clock_for_order()
        self._maybe_fail("place_futures_limit_order")
        crosses = price >= self.ask if direction == "LONG" else price <= self.bid
        if post_only and crosses:
            raise BitgetOrderRejected("post_only order would cross the book")
        if preset_stop_loss_price is not None:
            losing = preset_stop_loss_price < price if direction == "LONG" else preset_stop_loss_price > price
            if not losing:
                raise ValueError("preset stop is not on the losing side of the entry")
            if self.preset_model == "REJECT":
                raise BitgetOrderRejected("status=REJECTED presetStopLossPrice not supported")
        oid = self._new_id()
        self.orders[client_oid] = {"clientOid": client_oid, "orderId": oid, "state": "live", "size": size,
                                   "price": price, "direction": direction, "tradeSide": "open",
                                   "post_only": post_only, "baseVolume": 0.0, "priceAvg": 0.0, "fee": 0.0,
                                   "cTime": self.now_ms, "presetStopLossPrice": preset_stop_loss_price}
        self.calls.append(("place_entry", client_oid, direction, size, price, post_only))
        return {"data": {"orderId": oid, "clientOid": client_oid}}

    def place_futures_limit_close_order(self, symbol, hold_side, size, price, margin_mode="isolated",
                                        client_oid=None, post_only=True, **_):
        self._require_safe_clock_for_order()
        self._maybe_fail("place_futures_limit_close_order")
        hold = hold_side.lower()
        if size > self.position[hold] + 1e-9:
            raise RuntimeError("22002 No position to close")
        oid = self._new_id()
        self.orders[client_oid] = {"clientOid": client_oid, "orderId": oid, "state": "live", "size": size,
                                   "price": price, "direction": hold_side.upper(), "tradeSide": "close",
                                   "reduceOnly": "YES", "post_only": post_only, "baseVolume": 0.0,
                                   "priceAvg": 0.0, "fee": 0.0, "cTime": self.now_ms}
        self.calls.append(("place_tp", client_oid, hold_side, size, price, post_only))
        return {"data": {"orderId": oid, "clientOid": client_oid}}

    def cancel_futures_order(self, symbol, order_id=None, client_oid=None, **_):
        self._require_safe_clock_for_order()
        self._maybe_fail("cancel_futures_order")
        self.calls.append(("cancel", client_oid))
        order = self.orders.get(client_oid)
        if order is None:
            raise RuntimeError("order does not exist")
        if order["state"] == "filled":
            raise RuntimeError("order does not exist")            # 43001: cancelling a filled order
        if order["state"] in ("live", "partially_filled"):
            order["state"] = "canceled"
            order["cancelReason"] = "normal_cancel"
        return {"data": {}}

    def move_futures_stop_loss(self, symbol, hold_side=None, trigger_price=None, direction=None,
                               cleanup_existing=True, reason=None, **_):
        self._maybe_fail("move_futures_stop_loss")
        size = self.position[hold_side]
        if size <= 0:
            raise RuntimeError("no position to protect")
        active_pos = next((p for p in self.plans if p["planType"] == "pos_loss" and p["posSide"] == hold_side), None)
        if active_pos is not None:                                # the real client's POSITION_LEVEL branch
            self.calls.append(("stop", trigger_price, size, reason))
            has_tp = any(p["planType"] == "pos_profit" and p["posSide"] == hold_side for p in self.plans)
            if not has_tp:                                        # it rewrites BOTH legs, so it refuses without a TP to keep
                return {"symbol": symbol, "hold_side": hold_side, "verified": False, "model": "POSITION_LEVEL",
                        "reason_detail": "no_active_take_profit_to_preserve"}
            if self.stop_vanishes:
                return {"symbol": symbol, "hold_side": hold_side, "verified": False,
                        "reason_detail": "replacement_absent_after_settle", "placed": {"data": {}}}
            active_pos["triggerPrice"], active_pos["size"] = str(trigger_price), str(size)
            return {"symbol": symbol, "hold_side": hold_side, "trigger_price": trigger_price, "verified": True,
                    "model": "POSITION_LEVEL", "size": size,
                    "confirmed_stop": {"plan_type": "pos_loss", "trigger_price": float(trigger_price),
                                       "order_id": active_pos["orderId"], "size": str(size), "hold_side": hold_side}}
        if cleanup_existing:
            self.plans = [p for p in self.plans if not ("loss" in p["planType"] and p["posSide"] == hold_side)]
        plan = {"orderId": self._new_id(), "planType": "loss_plan", "posSide": hold_side, "holdSide": hold_side,
                "triggerPrice": str(trigger_price), "size": str(size), "planStatus": "live", "triggerType": "mark_price"}
        self.calls.append(("stop", trigger_price, size, reason))
        if self.stop_vanishes:                                   # accepted, then cancelled by the exchange
            return {"symbol": symbol, "hold_side": hold_side, "verified": False,
                    "reason_detail": "replacement_absent_after_settle", "placed": {"data": {}}}
        if not self.stop_lies:
            self.plans.append(plan)
        return {"symbol": symbol, "hold_side": hold_side, "trigger_price": trigger_price, "verified": True,
                "confirmed_stop": {"plan_type": "loss_plan", "trigger_price": float(trigger_price),
                                   "order_id": plan["orderId"], "size": str(size), "hold_side": hold_side},
                "placed": {"data": {"orderId": plan["orderId"]}}, "size": size}

    def move_plan_level_stop_place_first(self, symbol, hold_side, trigger_price, reason=None, **_):
        """The new shared-client method: place the newcomer BESIDE the old stop(s), cancel the old ones only afterwards."""
        self._maybe_fail("move_plan_level_stop_place_first")
        size = self.position[hold_side]
        if size <= 0:
            return {"verified": False, "reason_detail": "no_live_position_to_protect", "place_first_survived": None}
        if any(p["planType"] == "pos_loss" and p["posSide"] == hold_side for p in self.plans):
            return {"verified": False, "reason_detail": "position_level_stop_present_place_first_not_applicable",
                    "place_first_survived": None}
        self.calls.append(("stop_place_first", trigger_price, size, reason))
        old = [p for p in self.plans if "loss" in p["planType"] and p["posSide"] == hold_side]
        keep = next((p for p in old if abs(float(p["triggerPrice"]) - trigger_price) < 1e-6 and float(p["size"]) >= size - 1e-9), None)
        if keep is None:
            if self.stop_vanishes or not self.place_first_tolerated:
                return {"verified": False, "place_first_survived": False, "old_stops_retained": [p["orderId"] for p in old],
                        "reason_detail": "newcomer_absent_after_settle_old_stops_untouched"}
            keep = {"orderId": self._new_id(), "planType": "loss_plan", "posSide": hold_side, "holdSide": hold_side,
                    "triggerPrice": str(trigger_price), "size": str(size), "planStatus": "live", "triggerType": "mark_price"}
            self.plans.append(keep)
        self.plans = [p for p in self.plans
                      if not ("loss" in p["planType"] and p["posSide"] == hold_side and p is not keep)]
        return {"symbol": symbol, "hold_side": hold_side, "trigger_price": trigger_price, "verified": True,
                "model": "PLAN_LEVEL_PLACE_FIRST", "place_first_survived": True, "size": size,
                "confirmed_stop": {"plan_type": "loss_plan", "trigger_price": float(trigger_price),
                                   "order_id": keep["orderId"], "size": str(size), "hold_side": hold_side}}

    def move_position_level_stop_only(self, symbol, hold_side, trigger_price, reason=None, **_):
        """The new shared-client method: rewrite a LONE position-level stop in place (nothing to preserve)."""
        self._maybe_fail("move_position_level_stop_only")
        size = self.position[hold_side]
        plan = next((p for p in self.plans if p["planType"] == "pos_loss" and p["posSide"] == hold_side), None)
        has_tp = any(p["planType"] == "pos_profit" and p["posSide"] == hold_side for p in self.plans)
        self.calls.append(("stop_pos", trigger_price, size, reason))
        if plan is None or has_tp:
            return {"verified": False, "reason_detail": "no_position_level_stop_to_move" if plan is None
                    else "position_level_take_profit_present_use_move_futures_stop_loss"}
        if self.stop_vanishes:
            return {"verified": False, "reason_detail": "replacement_absent_after_settle"}
        plan["triggerPrice"], plan["size"] = str(trigger_price), str(size)
        return {"symbol": symbol, "hold_side": hold_side, "trigger_price": trigger_price, "verified": True,
                "model": "POSITION_LEVEL_STOP_ONLY", "size": size}

    def cancel_all_futures_tpsl_orders(self, symbol, hold_side=None, **_):
        self._maybe_fail("cancel_all_futures_tpsl_orders")
        self.calls.append(("cancel_stop", hold_side))
        self.plans = [p for p in self.plans if hold_side and p["posSide"] != hold_side]
        return {"data": {}}

    def close_futures_position_full(self, symbol, direction, size=None, margin_mode="isolated", reason=None,
                                    cleanup_tpsl=False, **_):
        self.calls.append(("emergency_close", direction, size, reason))
        self._maybe_fail("close_futures_position_full")
        hold = "long" if direction == "LONG" else "short"
        live = self.position[hold]
        if live <= 1e-12:
            return {"status": "NO_POSITION", "flatness": "FLAT", "remaining_size": 0.0}
        closable = live - (self.occupied(hold) if self.strict_occupancy else 0.0)
        self.position[hold] = live - closable
        remaining = self.position[hold]
        if remaining > 1e-9:
            return {"status": "PARTIALLY_CLOSED", "flatness": "REMAINS", "remaining_size": remaining,
                    "live_size": live}
        self.plans = [p for p in self.plans if p["posSide"] != hold]
        return {"status": "CLOSED", "flatness": "FLAT", "remaining_size": 0.0, "avg_price": self.mark,
                "live_size": live}

    # ---------------------------------------------------------------- test-side simulation
    def fill(self, client_oid: str, qty: float | None = None, price: float | None = None, fee: float = 0.0) -> None:
        order = self.orders[client_oid]
        qty = order["size"] - order["baseVolume"] if qty is None else qty
        price = order["price"] if price is None else price
        total_notional = order["baseVolume"] * order["priceAvg"] + qty * price
        order["baseVolume"] += qty
        order["priceAvg"] = total_notional / order["baseVolume"]
        order["fee"] += fee
        order["uTime"] = self.now_ms
        order["state"] = "filled" if order["baseVolume"] >= order["size"] - 1e-9 else "partially_filled"
        hold = "long" if order["direction"] == "LONG" else "short"
        if order["tradeSide"] == "open":
            self.position[hold] += qty
            self._materialise_preset(order, hold)
        else:
            self.position[hold] -= qty

    def _materialise_preset(self, order: dict[str, Any], hold: str) -> None:
        """What the EXCHANGE does with a preset stop at a fill - with no client involved."""
        price = order.get("presetStopLossPrice")
        if price is None or self.preset_model in ("NONE", "REJECT"):
            return
        if self.preset_model == "POSITION_LEVEL":
            plan = next((p for p in self.plans if p["planType"] == "pos_loss" and p["posSide"] == hold), None)
            if plan is None:
                self.plans.append({"orderId": self._new_id(), "planType": "pos_loss", "posSide": hold, "holdSide": hold,
                                   "triggerPrice": str(price), "size": str(self.position[hold]), "planStatus": "live",
                                   "triggerType": "fill_price", "fromClientOid": order["clientOid"]})
            else:
                plan["size"] = str(self.position[hold])
            return
        plan = next((p for p in self.plans if p.get("fromClientOid") == order["clientOid"]), None)   # PLAN_PER_ORDER
        if plan is None:
            self.plans.append({"orderId": self._new_id(), "planType": "loss_plan", "posSide": hold, "holdSide": hold,
                               "triggerPrice": str(price), "size": str(order["baseVolume"]), "planStatus": "live",
                               "triggerType": "fill_price", "fromClientOid": order["clientOid"]})
        else:
            plan["size"] = str(order["baseVolume"])

    def trigger_stop(self, price: float | None = None, slippage: float = 0.0) -> str:
        """The stop plan fires: a MARKET order fills the whole position (orderSource loss_market)."""
        plan = self.stop_plan
        assert plan is not None
        hold = plan["posSide"]
        size = self.position[hold]
        fill_px = (price if price is not None else float(plan["triggerPrice"])) - slippage * (1 if hold == "long" else -1)
        oid = self._new_id()
        source = "pos_loss_market" if plan["planType"] == "pos_loss" else "loss_market"
        self.history.append({"orderId": oid, "clientOid": "", "status": "filled", "state": "filled",
                             "orderSource": source, "posSide": hold, "tradeSide": "close",
                             "baseVolume": str(size), "priceAvg": str(fill_px), "fee": str(-size * fill_px * 0.0006),
                             "size": str(size), "cTime": str(self.now_ms)})
        self.position[hold] = 0.0
        self.plans = [p for p in self.plans if p["posSide"] != hold]
        for o in self.orders.values():                            # reduce-only closes die with the position
            if o["tradeSide"] == "close" and o["state"] in ("live", "partially_filled"):
                o["state"] = "canceled"
                o["cancelReason"] = "normal_cancel"
        return oid

    def by_kind(self, kind: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == kind]

    def mutating_calls(self) -> list[tuple]:
        return [c for c in self.calls if c[0] not in ("get_contracts",)]
