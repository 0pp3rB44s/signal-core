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


def _settings_extended(extended_enabled, extended_seconds, base_wait):
    return SimpleNamespace(
        maker_entry_offset_bps=1.0,
        maker_entry_wait_seconds=base_wait,
        maker_entry_poll_seconds=0.25,
        maker_entry_extended_wait_enabled=extended_enabled,
        maker_entry_extended_wait_seconds=extended_seconds,
    )


def _outcome_logged(log):
    return any(
        call.args and "MAKER_ENTRY_OUTCOME" in str(call.args[0])
        for call in log.warning.call_args_list
    )


def test_extended_wait_used_when_enabled():
    # base wait 99s would hang if used; extended 0.05s means it completes fast,
    # proving the extended window is the one in effect.
    client = MagicMock()
    client.place_futures_limit_order.return_value = {"data": {"orderId": "9"}}
    client.extract_order_id.return_value = "9"
    client.get_order_detail.return_value = {"data": {}}
    client.extract_fill_metrics.return_value = {"filled_qty": 0.0, "state": "live"}
    client.get_all_positions.return_value = {"data": []}
    log = _log()
    r = attempt_maker_entry(client, _settings_extended(True, 0.05, 99.0), "BTCUSDT", "LONG", 5.0, 100.0, "long", log)
    assert r["status"] == "UNFILLED_CANCELLED"
    assert _outcome_logged(log)


def test_base_wait_used_when_extended_disabled():
    client = MagicMock()
    client.place_futures_limit_order.return_value = {"data": {"orderId": "10"}}
    client.extract_order_id.return_value = "10"
    client.get_order_detail.return_value = {"data": {}}
    client.extract_fill_metrics.return_value = {"filled_qty": 0.0, "state": "live"}
    client.get_all_positions.return_value = {"data": []}
    r = attempt_maker_entry(client, _settings_extended(False, 99.0, 0.05), "BTCUSDT", "LONG", 5.0, 100.0, "long", _log())
    assert r["status"] == "UNFILLED_CANCELLED"  # base 0.05s used, not extended 99s


def test_outcome_line_logged_on_fill_with_latency():
    client = MagicMock()
    client.place_futures_limit_order.return_value = {"data": {"orderId": "11"}}
    client.extract_order_id.return_value = "11"
    client.get_order_detail.return_value = {"data": {}}
    client.extract_fill_metrics.return_value = {"filled_qty": 5.0, "state": "filled", "avg_price": 99.9}
    log = _log()
    r = attempt_maker_entry(client, _settings_extended(True, 0.05, 4.0), "BTCUSDT", "LONG", 5.0, 100.0, "long", log)
    assert r["status"] == "FILLED"
    assert _outcome_logged(log)
    assert any("fill_latency_s" in str(c.args[0]) for c in log.warning.call_args_list if c.args)


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


def test_cancel_race_detects_filled_position_and_protects():
    # De order vult in de race tussen laatste poll en cancel: cancel faalt
    # (43001), maar er staat een positie open -> MOET FILLED teruggeven zodat
    # execution hem beschermt, niet skippen.
    client = MagicMock()
    client.place_futures_limit_order.return_value = {"data": {"orderId": "9"}}
    client.extract_order_id.return_value = "9"
    client.get_order_detail.return_value = {"data": {}}
    client.extract_fill_metrics.return_value = {"filled_qty": 0.0, "state": "live"}
    client.cancel_futures_order.side_effect = RuntimeError('43001 order not found')
    client.get_all_positions.return_value = {"data": [
        {"symbol": "BTCUSDT", "total": 5.0, "openPriceAvg": 100.05, "holdSide": "short"}
    ]}
    r = attempt_maker_entry(client, _settings(), "BTCUSDT", "SHORT", 5.0, 100.0, "short", _log())
    assert r["status"] == "FILLED", "onbeschermde positie na cancel-race moet beschermd worden"
    assert r["fill_entry"] == 100.05


def test_cancel_success_no_position_is_unfilled():
    client = MagicMock()
    client.place_futures_limit_order.return_value = {"data": {"orderId": "10"}}
    client.extract_order_id.return_value = "10"
    client.get_order_detail.return_value = {"data": {}}
    client.extract_fill_metrics.return_value = {"filled_qty": 0.0, "state": "live"}
    client.get_all_positions.return_value = {"data": []}
    r = attempt_maker_entry(client, _settings(), "BTCUSDT", "SHORT", 5.0, 100.0, "short", _log())
    assert r["status"] == "UNFILLED_CANCELLED"


def test_maker_place_failure_is_error_no_position():
    client = MagicMock()
    client.place_futures_limit_order.side_effect = RuntimeError("400 post_only rejected")
    r = attempt_maker_entry(client, _settings(), "BTCUSDT", "LONG", 5.0, 100.0, "long", _log())
    assert r["status"] == "ERROR"
    assert r["filled_qty"] == 0.0
    client.cancel_futures_order.assert_not_called()
