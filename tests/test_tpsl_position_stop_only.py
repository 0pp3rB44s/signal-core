"""`move_position_level_stop_only`: rewrite a LONE position-level stop (one that came from an order-level preset stop) in place.

The shared `move_futures_stop_loss` refuses that case on purpose (it must re-send a take-profit it does not have). The new
method must NEVER send a take-profit leg, must refuse unless there is a position-level stop and no position-level
take-profit, and must judge success only from a read AFTER the settle window (2026-08-01).
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from clients.bitget_tpsl_client import BitgetTPSLClientMixin


class Stub(BitgetTPSLClientMixin):
    STOP_REPLACEMENT_SETTLE_SECONDS = 0.0
    settings = SimpleNamespace(bitget_product_type="USDT-FUTURES")
    log = logging.getLogger("stub")

    def __init__(self, before, after=None, take_profit=None, fail=None):
        self._reads = [before, after if after is not None else before]
        self._tp, self._fail, self.sent = take_profit, fail, []

    def _format_trigger_price(self, symbol, price):
        return round(float(price), 3)

    def _validate_futures_order_flags(self, body):
        return None

    def _active_position_stop(self, symbol, hold_side, product_type):
        return self._reads.pop(0) if len(self._reads) > 1 else self._reads[0]

    def _active_position_take_profit(self, symbol, hold_side, product_type):
        return self._tp

    def _request(self, **kw):
        if self._fail:
            raise self._fail
        self.sent.append(kw)
        return {"code": "00000", "data": {}}


def stop(kind="pos_loss", price=149.0, side="long"):
    return {"plan_type": kind, "trigger_price": price, "order_id": "1", "size": "1", "hold_side": side}


def test_a_lone_position_level_stop_is_rewritten_in_place_with_the_stop_leg_only():
    c = Stub(before=stop(price=149.0), after=stop(price=150.2))
    res = c.move_position_level_stop_only("SOLUSDT", "long", 150.2, reason="TP1_BE")
    assert res["verified"] is True and res["model"] == "POSITION_LEVEL_STOP_ONLY"
    (call,) = c.sent
    assert call["path"] == "/api/v2/mix/order/place-pos-tpsl" and call["private"] is True
    body = call["body"]
    assert body["stopLossTriggerPrice"] == "150.2" and body["holdSide"] == "long"
    assert not [k for k in body if "Surplus" in k or "profit" in k.lower()]        # a take-profit leg is NEVER sent


@pytest.mark.parametrize("existing,reason", [
    (None, "no_position_level_stop_to_move"),
    (stop("loss_plan"), "no_position_level_stop_to_move"),           # a plan-level stop is not this method's business
])
def test_it_refuses_unless_the_position_carries_a_position_level_stop_and_sends_nothing(existing, reason):
    c = Stub(before=existing)
    res = c.move_position_level_stop_only("SOLUSDT", "long", 150.2)
    assert res["verified"] is False and res["reason_detail"] == reason and c.sent == []


def test_it_refuses_when_a_position_level_take_profit_exists_so_it_can_never_drop_one():
    c = Stub(before=stop(), take_profit=155.0)
    res = c.move_position_level_stop_only("SOLUSDT", "long", 150.2)
    assert res["verified"] is False and "take_profit_present" in res["reason_detail"] and c.sent == []


def test_a_stop_that_is_gone_after_the_settle_window_is_not_success():
    c = Stub(before=stop(), after=None)
    c._reads = [stop(), None]                                        # the read AFTER the settle finds nothing
    res = c.move_position_level_stop_only("SOLUSDT", "long", 150.2)
    assert res["verified"] is False and res["reason_detail"] == "replacement_absent_after_settle"


@pytest.mark.parametrize("after", [stop(price=149.0), stop("loss_plan", 150.2), stop(price=150.2, side="short")])
def test_a_stop_that_diverged_from_the_request_is_not_success(after):
    c = Stub(before=stop(), after=after)
    assert c.move_position_level_stop_only("SOLUSDT", "long", 150.2)["verified"] is False


def test_a_failed_request_leaves_the_existing_stop_and_says_so():
    c = Stub(before=stop(), fail=RuntimeError("HTTP 500"))
    res = c.move_position_level_stop_only("SOLUSDT", "long", 150.2)
    assert res["verified"] is False and "place_pos_tpsl_failed" in res["reason_detail"]


def test_an_invalid_trigger_is_refused_before_the_network():
    c = Stub(before=stop())
    assert c.move_position_level_stop_only("SOLUSDT", "long", 0)["verified"] is False and c.sent == []
