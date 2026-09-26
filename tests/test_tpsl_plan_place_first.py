"""`move_plan_level_stop_place_first`: move a plan-level stop with NO moment in which the position has no stop.

The shared `move_futures_stop_loss` cancels first and places second; a request that fails in between leaves an open position
bare. This method places the newcomer BESIDE the old stop(s) and cancels the old one(s) only after the newcomer is confirmed to
have survived the settle window. If it did not, nothing was cancelled and the old stop is untouched.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from clients.bitget_tpsl_client import BitgetTPSLClientMixin

#: The loss-plan request the legacy move sends (`move_futures_stop_loss`): the new method must send exactly the same one.
LOSS_PLAN_BODY_KEYS = {"symbol", "productType", "marginCoin", "marginMode", "planType", "triggerPrice", "triggerType",
                       "executePrice", "holdSide", "size"}


class Book(BitgetTPSLClientMixin):
    """An in-memory exchange plan book behind the real mixin method."""
    STOP_REPLACEMENT_SETTLE_SECONDS = 0.0
    settings = SimpleNamespace(bitget_product_type="USDT-FUTURES")
    log = logging.getLogger("book")

    def __init__(self, plans, *, position=3.0, position_level=False, newcomer_vanishes=False, fail_place=None, fail_cancel=()):
        self.plans = [dict(p) for p in plans]
        self.position, self.position_level = position, position_level
        self.newcomer_vanishes, self.fail_place, self.fail_cancel = newcomer_vanishes, fail_place, set(fail_cancel)
        self.events: list[str] = []
        self.min_loss_plans = 10 ** 6
        self.bodies: list[dict] = []
        self._n = 100

    def _track(self, hold="long"):
        n = len([p for p in self.plans if p["holdSide"] == hold and p["planType"] == "loss_plan"])
        self.min_loss_plans = min(self.min_loss_plans, n)

    # -- what the mixin calls
    def _format_trigger_price(self, symbol, price):
        return round(float(price), 3)

    def _format_size(self, symbol, size):
        return round(float(size), 1)

    def _validate_futures_order_flags(self, body):
        return None

    def _live_position_size_for_symbol(self, symbol, hold_side=None):
        return self.position

    def _fetch_tpsl_orders_broad(self, symbol, product_type=None):
        return [dict(p) for p in self.plans], {}

    def _active_position_stop(self, symbol, hold_side, product_type):
        if self.position_level:
            return {"plan_type": "pos_loss", "trigger_price": 149.0, "order_id": "P", "size": "3", "hold_side": hold_side}
        loss = [p for p in self.plans if p["holdSide"] == hold_side and p["planType"] == "loss_plan"]
        if not loss:
            return None
        p = loss[-1]
        return {"plan_type": "loss_plan", "trigger_price": float(p["triggerPrice"]), "order_id": p["planOrderId"],
                "size": p["size"], "hold_side": hold_side}

    def verify_active_stop_loss(self, symbol, hold_side, expected_trigger_price, product_type=None):
        ok = any(abs(float(p["triggerPrice"]) - expected_trigger_price) < 0.01 for p in self.plans
                 if p["holdSide"] == hold_side and p["planType"] == "loss_plan")
        return {"verified": ok}

    def _request(self, method, path, body=None, private=False, params=None):
        assert path == "/api/v2/mix/order/place-tpsl-order" and method == "POST"
        if self.fail_place:
            raise self.fail_place
        self.events.append("place")
        self.bodies.append(dict(body))
        self._n += 1
        row = {"planOrderId": str(self._n), "planType": "loss_plan", "holdSide": body["holdSide"],
               "triggerPrice": body["triggerPrice"], "size": body["size"]}
        if self.newcomer_vanishes:
            self._vanish = row["planOrderId"]
        self.plans.append(row)
        self._track(body["holdSide"])
        return {"code": "00000", "data": {"orderId": row["planOrderId"]}}

    def cancel_futures_plan_order(self, symbol, order_id, plan_type="loss_plan", product_type=None, margin_coin="USDT"):
        if order_id in self.fail_cancel:
            raise RuntimeError("cancel refused")
        self.events.append(f"cancel:{order_id}")
        self.plans = [p for p in self.plans if p["planOrderId"] != order_id]
        self._track()
        return {"code": "00000"}

    # the exchange cancels a newcomer about a second later: it is gone by the time the settle window has passed
    def _loss_plan_rows(self, symbol, hold_side, product):
        if getattr(self, "_vanish", None):
            self.plans = [p for p in self.plans if p["planOrderId"] != self._vanish]
            self._vanish = None
        return super()._loss_plan_rows(symbol, hold_side, product)


def old(pid="1", price=149.0, size="3.0", side="long", ptype="loss_plan"):
    return {"planOrderId": pid, "planType": ptype, "holdSide": side, "triggerPrice": str(price), "size": size}


def test_the_new_stop_is_placed_beside_the_old_one_and_the_old_one_is_cancelled_only_afterwards():
    b = Book([old()])
    res = b.move_plan_level_stop_place_first("SOLUSDT", "long", 150.2, reason="TP1_BE")
    assert res["verified"] is True and res["place_first_survived"] is True and res["model"] == "PLAN_LEVEL_PLACE_FIRST"
    assert b.events == ["place", "cancel:1"]                                  # place FIRST, cancel SECOND
    assert b.min_loss_plans >= 1, "at no moment did the position have no stop"
    assert [p["triggerPrice"] for p in b.plans] == ["150.2"] and b.plans[0]["size"] == "3.0"
    assert res["cancelled"] == ["1"]


def test_the_request_is_exactly_the_loss_plan_the_legacy_move_sends_sized_to_the_whole_live_position():
    b = Book([old(size="2.0")], position=3.0)
    b.move_plan_level_stop_place_first("SOLUSDT", "long", 150.2)
    (body,) = b.bodies
    assert set(body) == LOSS_PLAN_BODY_KEYS
    assert body["planType"] == "loss_plan" and body["triggerType"] == "mark_price" and body["executePrice"] == "0"
    assert body["size"] == "3.0" and body["holdSide"] == "long" and body["triggerPrice"] == "150.2"


def test_every_old_stop_goes_when_several_per_order_stops_exist_and_the_other_side_is_untouched():
    b = Book([old("1", 149.0, "1.0"), old("2", 148.5, "1.0"), old("3", 149.0, "1.0"), old("9", 155.0, "2.0", side="short")])
    res = b.move_plan_level_stop_place_first("SOLUSDT", "long", 150.2)
    assert res["verified"] is True and sorted(res["cancelled"]) == ["1", "2", "3"]
    assert sorted((p["holdSide"], p["triggerPrice"]) for p in b.plans) == [("long", "150.2"), ("short", "155.0")]
    assert b.min_loss_plans >= 1


def test_a_newcomer_the_exchange_cancels_leaves_the_old_stop_untouched_and_says_so():
    b = Book([old()], newcomer_vanishes=True)
    res = b.move_plan_level_stop_place_first("SOLUSDT", "long", 150.2)
    assert res["verified"] is False and res["place_first_survived"] is False
    assert res["reason_detail"] == "newcomer_absent_after_settle_old_stops_untouched" and res["old_stops_retained"] == ["1"]
    assert b.events == ["place"] and [p["planOrderId"] for p in b.plans] == ["1"]        # NOTHING was cancelled


def test_a_failed_placement_leaves_the_old_stop_untouched():
    b = Book([old()], fail_place=RuntimeError("HTTP 500"))
    res = b.move_plan_level_stop_place_first("SOLUSDT", "long", 150.2)
    assert res["verified"] is False and "place_loss_plan_failed" in res["reason_detail"] and res["old_stops_retained"] == ["1"]
    assert b.events == [] and [p["planOrderId"] for p in b.plans] == ["1"]
    assert res["place_first_survived"] is None                                # unknown, NOT "not tolerated": the caller may retry


def test_when_the_old_stop_cannot_be_cancelled_both_stay_live_and_the_move_is_reported_unverified():
    b = Book([old()], fail_cancel={"1"})
    res = b.move_plan_level_stop_place_first("SOLUSDT", "long", 150.2)
    assert res["verified"] is False and res["cancel_failed"] and res["place_first_survived"] is True
    assert sorted(p["planOrderId"] for p in b.plans) == ["1", "101"] and b.min_loss_plans >= 1     # protected throughout


def test_an_exact_stop_that_is_already_there_is_not_replaced_only_worse_extras_are_removed():
    b = Book([old("1", 150.2, "3.0"), old("2", 148.0, "1.0")])
    res = b.move_plan_level_stop_place_first("SOLUSDT", "long", 150.2)
    assert res["verified"] is True and b.events == ["cancel:2"] and "placed" not in res
    assert [p["planOrderId"] for p in b.plans] == ["1"]


def test_an_exact_price_stop_that_is_too_small_for_the_position_is_not_kept_a_full_size_one_is_placed():
    b = Book([old("1", 150.2, "2.0")], position=3.0)                          # a later fill grew the position
    res = b.move_plan_level_stop_place_first("SOLUSDT", "long", 150.2)
    assert res["verified"] is True and b.events[0] == "place" and [p["size"] for p in b.plans] == ["3.0"]


def test_a_position_level_stop_is_refused_and_nothing_is_sent():
    b = Book([], position_level=True)
    res = b.move_plan_level_stop_place_first("SOLUSDT", "long", 150.2)
    assert res["verified"] is False and res["reason_detail"] == "position_level_stop_present_place_first_not_applicable"
    assert b.events == []


def test_no_live_position_and_an_invalid_trigger_are_refused_before_anything_is_sent():
    b = Book([old()], position=0.0)
    assert b.move_plan_level_stop_place_first("SOLUSDT", "long", 150.2)["reason_detail"] == "no_live_position_to_protect"
    b = Book([old()])
    assert b.move_plan_level_stop_place_first("SOLUSDT", "long", 0)["reason_detail"] == "invalid_trigger_price"
    assert b.events == [] and [p["planOrderId"] for p in b.plans] == ["1"]


@pytest.mark.parametrize("hold,price", [("short", 150.2), ("long", 150.2)])
def test_both_sides_use_the_same_mechanism(hold, price):
    b = Book([old(side=hold)])
    res = b.move_plan_level_stop_place_first("SOLUSDT", hold, price)
    assert res["verified"] is True and b.events == ["place", "cancel:1"] and b.bodies[0]["holdSide"] == hold
