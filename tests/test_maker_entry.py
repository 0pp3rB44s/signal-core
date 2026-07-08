"""Maker-entry: prijslogica + place/poll/cancel discipline."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from execution.maker_entry import attempt_maker_entry, compute_limit_price


def test_limit_price_long_below_market():
    # LONG maker: limit onder de markt (koper wacht op dip).
    assert compute_limit_price("LONG", 100.0, 10.0) < 100.0
    assert compute_limit_price("LONG", 100.0, 10.0) == 99.9


def test_limit_price_short_above_market():
    # SHORT maker: limit boven de markt (verkoper wacht op tik omhoog).
    assert compute_limit_price("SHORT", 100.0, 10.0) > 100.0
    assert compute_limit_price("SHORT", 100.0, 10.0) == 100.1


def test_limit_price_zero_anchor_is_zero():
    assert compute_limit_price("LONG", 0.0, 1.0) == 0.0


def _settings():
    return SimpleNamespace(maker_entry_offset_bps=1.0, maker_entry_wait_seconds=0.05,
                           maker_entry_poll_seconds=0.25)


def _log():
    return MagicMock()


def test_maker_fill_returns_filled():
    client = MagicMock()
    client.place_futures_limit_order.return_value = {"data": {"orderId": "1"}}
    client.extract_order_id.return_value = "1"
    client.get_order_detail.return_value = {"data": {}}
    client.extract_fill_metrics.return_value = {"filled_qty": 5.0, "state": "filled", "avg_price": 99.9}
    r = attempt_maker_entry(client, _settings(), "BTCUSDT", "LONG", 5.0, 100.0, "long", _log())
    assert r["status"] == "FILLED"
    assert r["fill_entry"] == 99.9
    client.cancel_futures_order.assert_not_called()


def test_maker_unfilled_cancels_and_skips():
    client = MagicMock()
    client.place_futures_limit_order.return_value = {"data": {"orderId": "2"}}
    client.extract_order_id.return_value = "2"
    client.get_order_detail.return_value = {"data": {}}
    client.extract_fill_metrics.return_value = {"filled_qty": 0.0, "state": "live"}
    r = attempt_maker_entry(client, _settings(), "BTCUSDT", "SHORT", 5.0, 100.0, "short", _log())
    assert r["status"] == "UNFILLED_CANCELLED"
    client.cancel_futures_order.assert_called_once()


def test_maker_place_failure_is_error_no_position():
    client = MagicMock()
    client.place_futures_limit_order.side_effect = RuntimeError("400 post_only rejected")
    r = attempt_maker_entry(client, _settings(), "BTCUSDT", "LONG", 5.0, 100.0, "long", _log())
    assert r["status"] == "ERROR"
    assert r["filled_qty"] == 0.0
    client.cancel_futures_order.assert_not_called()
