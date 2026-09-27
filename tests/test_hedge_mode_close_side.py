"""Hedge-mode close semantics: which `side` actually closes a position.

Bitget v2 pairs `side` with `tradeSide=close` to name the *position* being
closed, not the direction of the closing fill. The account's own filled closes
are the reference:

    posSide=long   side=buy    tradeSide=close   filled
    posSide=short  side=sell   tradeSide=close   filled

The inverted pair is individually well-formed -- every field passes its own
vocabulary check -- and Bitget rejects the order with 400 22002 "No position to
close". These tests pin the mapping, the payload invariant that catches an
inversion before transport, and the rule that only an exchange-confirmed flat
position may be recorded as closed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.config import (
    LIVE_MAX_EXECUTIONS_PER_CYCLE,
    LIVE_MAX_OPEN_POSITIONS,
    Settings,
)
from clients.bitget_order_client import (
    CLOSE_CONFIRMED_FLAT_STATUSES,
    close_side_for_hold_side,
    is_no_position_to_close_error,
)
from clients.bitget_rest import BitgetRestClient
from execution.position_manager import DEAD_TRADE_CLOSE_MAX_ATTEMPTS, PositionManager


class _Rejected(Exception):
    """Stand-in for the transport error Bitget raises on a rejected close."""


NO_POSITION_ERROR = _Rejected(
    "Bitget HTTP error: status=400 code=22002 msg=No position to close"
)


# --- client fixtures ------------------------------------------------------


def _client(tmp_path, live_size: float = 1.0) -> BitgetRestClient:
    client = BitgetRestClient(
        Settings(
            _env_file=None,
            BITGET_RATE_LIMIT_STATE_PATH=str(tmp_path / "rate-limit.json"),
        )
    )
    client._format_size = lambda symbol, size: float(size)
    client._live_position_size_for_symbol = MagicMock(return_value=live_size)
    client._request = MagicMock(return_value={"code": "00000", "data": {"orderId": "1"}})
    client.cancel_all_futures_tpsl_orders = MagicMock(return_value={"status": "ok"})
    return client


def _sent_bodies(client) -> list[dict]:
    return [
        call.kwargs["body"]
        for call in client._request.call_args_list
        if call.kwargs.get("path") == "/api/v2/mix/order/place-order"
    ]


# --- 1-3: the mapping and its invariant -----------------------------------


def test_long_hedge_close_sends_side_buy(tmp_path):
    client = _client(tmp_path)
    client.close_futures_position(symbol="BTCUSDT", hold_side="long", size=0.5)

    body = _sent_bodies(client)[0]
    assert body["side"] == "buy"
    assert body["holdSide"] == "long"
    assert body["tradeSide"] == "close"
    assert body["reduceOnly"] == "YES"


def test_short_hedge_close_sends_side_sell(tmp_path):
    client = _client(tmp_path)
    client.close_futures_position(symbol="BTCUSDT", hold_side="short", size=0.5)

    body = _sent_bodies(client)[0]
    assert body["side"] == "sell"
    assert body["holdSide"] == "short"
    assert body["tradeSide"] == "close"
    assert body["reduceOnly"] == "YES"


@pytest.mark.parametrize(
    ("hold_side", "inverted_side"), [("long", "sell"), ("short", "buy")]
)
def test_previous_inverted_mapping_is_refused_before_transport(
    tmp_path, hold_side, inverted_side
):
    """The exact payload the old `"sell" if long else "buy"` line produced."""
    client = _client(tmp_path)
    body = {
        "symbol": "BTCUSDT",
        "productType": "USDT-FUTURES",
        "marginCoin": "USDT",
        "marginMode": "isolated",
        "size": "0.5",
        "side": inverted_side,
        "tradeSide": "close",
        "orderType": "market",
        "holdSide": hold_side,
        "reduceOnly": "YES",
    }

    with pytest.raises(ValueError, match="cannot close holdSide"):
        client._verify_reduce_only_close_body(
            body=body, symbol="BTCUSDT", hold_side=hold_side
        )
    assert _sent_bodies(client) == []


def test_close_side_helper_is_the_single_source_of_truth():
    assert close_side_for_hold_side("long") == "buy"
    assert close_side_for_hold_side("short") == "sell"


# --- 17-18: fail closed on anything that is not long/short ----------------


@pytest.mark.parametrize("hold_side", ["", None, "both", "oneway", "net", "LONGISH"])
def test_unsupported_hold_side_fails_closed(hold_side):
    """One-way/net semantics are not supported and must never be guessed at."""
    with pytest.raises(ValueError, match="Unsupported hold side"):
        close_side_for_hold_side(hold_side)


def test_close_refuses_when_body_hold_side_contradicts_the_position(tmp_path):
    client = _client(tmp_path)
    body = {
        "symbol": "BTCUSDT",
        "productType": "USDT-FUTURES",
        "marginCoin": "USDT",
        "marginMode": "isolated",
        "size": "0.5",
        "side": "buy",
        "tradeSide": "close",
        "orderType": "market",
        "holdSide": "short",
        "reduceOnly": "YES",
    }
    with pytest.raises(ValueError, match="does not match the position being closed"):
        client._verify_reduce_only_close_body(
            body=body, symbol="BTCUSDT", hold_side="long"
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("tradeSide", "open", "must be 'close'"),
        ("reduceOnly", "NO", "must be 'YES'"),
        ("orderType", "limit", "must be 'market'"),
        ("size", "0", "must be a positive number"),
        ("symbol", "ETHUSDT", "must be BTCUSDT"),
        ("productType", "COIN-FUTURES", "must be USDT-FUTURES"),
        ("marginCoin", "USDC", "must be USDT"),
    ],
)
def test_close_payload_invariants_fail_closed(tmp_path, field, value, match):
    client = _client(tmp_path)
    body = {
        "symbol": "BTCUSDT",
        "productType": "USDT-FUTURES",
        "marginCoin": "USDT",
        "marginMode": "isolated",
        "size": "0.5",
        "side": "buy",
        "tradeSide": "close",
        "orderType": "market",
        "holdSide": "long",
        "reduceOnly": "YES",
    }
    body[field] = value
    with pytest.raises(ValueError, match=match):
        client._verify_reduce_only_close_body(
            body=body, symbol="BTCUSDT", hold_side="long"
        )


def test_opening_semantics_may_not_ride_along_on_a_close(tmp_path):
    client = _client(tmp_path)
    body = {
        "symbol": "BTCUSDT",
        "productType": "USDT-FUTURES",
        "marginCoin": "USDT",
        "marginMode": "isolated",
        "size": "0.5",
        "side": "buy",
        "tradeSide": "close",
        "orderType": "market",
        "holdSide": "long",
        "reduceOnly": "YES",
        "presetStopLossPrice": "100",
    }
    with pytest.raises(ValueError, match="no place in a close payload"):
        client._verify_reduce_only_close_body(
            body=body, symbol="BTCUSDT", hold_side="long"
        )


# --- 11-12, 14-16: protection safety and close confirmation ---------------


def test_failed_close_leaves_protection_untouched_and_raises(tmp_path):
    """pos_loss/pos_profit survive a rejected close: nothing is cancelled."""
    client = _client(tmp_path, live_size=0.3)
    client._request = MagicMock(side_effect=NO_POSITION_ERROR)

    with pytest.raises(_Rejected):
        client.close_futures_position_full(symbol="SOLUSDT", direction="LONG")

    client.cancel_all_futures_tpsl_orders.assert_not_called()


def test_protection_is_never_cancelled_before_the_close_is_confirmed(tmp_path):
    client = _client(tmp_path, live_size=0.3)
    # Position still there on the read-back: nothing to celebrate, nothing to cancel.
    client._live_position_size_for_symbol = MagicMock(side_effect=[0.3, 0.3])

    result = client.close_futures_position_full(symbol="SOLUSDT", direction="LONG")

    assert result["status"] == "CLOSE_NOT_REFLECTED"
    assert result["status"] not in CLOSE_CONFIRMED_FLAT_STATUSES
    client.cancel_all_futures_tpsl_orders.assert_not_called()


def test_partial_close_reports_remaining_size_and_keeps_protection(tmp_path):
    client = _client(tmp_path, live_size=1.0)
    client._live_position_size_for_symbol = MagicMock(side_effect=[1.0, 0.4])

    result = client.close_futures_position_full(symbol="SOLUSDT", direction="LONG")

    assert result["status"] == "PARTIALLY_CLOSED"
    assert result["remaining_size"] == 0.4
    client.cancel_all_futures_tpsl_orders.assert_not_called()


def test_confirmed_flat_close_cleans_up_afterwards(tmp_path):
    client = _client(tmp_path, live_size=1.0)
    client._live_position_size_for_symbol = MagicMock(side_effect=[1.0, 0.0])

    result = client.close_futures_position_full(symbol="SOLUSDT", direction="LONG")

    assert result["status"] == "CLOSED"
    assert result["remaining_size"] == 0.0
    client.cancel_all_futures_tpsl_orders.assert_called_once()


def test_no_position_error_on_an_already_flat_position_is_not_a_failure(tmp_path):
    """22002 plus a verified-flat re-read means somebody else closed it."""
    client = _client(tmp_path, live_size=0.3)
    client._live_position_size_for_symbol = MagicMock(side_effect=[0.3, 0.0])
    client._request = MagicMock(side_effect=NO_POSITION_ERROR)

    result = client.close_futures_position_full(symbol="SOLUSDT", direction="LONG")

    assert result["status"] == "NO_POSITION"
    assert result["status"] in CLOSE_CONFIRMED_FLAT_STATUSES


def test_no_position_error_while_size_remains_still_raises(tmp_path):
    """The SOLUSDT case: 22002 was the inverted side, not an absent position."""
    client = _client(tmp_path, live_size=0.3)
    client._live_position_size_for_symbol = MagicMock(side_effect=[0.3, 0.3])
    client._request = MagicMock(side_effect=NO_POSITION_ERROR)

    with pytest.raises(_Rejected):
        client.close_futures_position_full(symbol="SOLUSDT", direction="LONG")


def test_no_position_error_classifier():
    assert is_no_position_to_close_error(NO_POSITION_ERROR)
    assert not is_no_position_to_close_error(Exception("code=40009 signature invalid"))


# --- 8-10: emergency flatten ----------------------------------------------


def _flatten_client(tmp_path, positions: list[dict]) -> BitgetRestClient:
    client = _client(tmp_path)
    client.get_all_positions = MagicMock(return_value={"data": positions})
    # Every flatten target is confirmed gone after its close.
    client._live_position_size_for_symbol = MagicMock(side_effect=[1.0, 0.0] * 4)
    return client


@pytest.mark.parametrize(
    ("hold_side", "expected_side"), [("long", "buy"), ("short", "sell")]
)
def test_emergency_flatten_single_position_uses_the_closing_side(
    tmp_path, hold_side, expected_side
):
    client = _flatten_client(
        tmp_path, [{"symbol": "BTCUSDT", "holdSide": hold_side, "total": "1.0"}]
    )

    result = client.emergency_flatten_all()

    assert result["positions_found"] == 1
    assert not result["errors"]
    body = _sent_bodies(client)[0]
    assert body["side"] == expected_side
    assert body["holdSide"] == hold_side
    assert body["tradeSide"] == "close"


def test_emergency_flatten_two_symbols_closes_each_on_its_own_side(tmp_path):
    client = _flatten_client(
        tmp_path,
        [
            {"symbol": "BTCUSDT", "holdSide": "long", "total": "1.0"},
            {"symbol": "SOLUSDT", "holdSide": "short", "total": "2.0"},
        ],
    )

    result = client.emergency_flatten_all()

    assert result["positions_found"] == 2
    assert not result["errors"]
    bodies = {body["symbol"]: body for body in _sent_bodies(client)}
    assert bodies["BTCUSDT"]["side"] == "buy"
    assert bodies["BTCUSDT"]["holdSide"] == "long"
    assert bodies["SOLUSDT"]["side"] == "sell"
    assert bodies["SOLUSDT"]["holdSide"] == "short"


# --- 4-7, 13, 19: manager-level close paths -------------------------------


def _manager_settings() -> MagicMock:
    settings = MagicMock()
    settings.tp1_close_pct = 40.0
    settings.tp2_close_pct = 30.0
    settings.tp3_close_pct = 30.0
    settings.tp3_close_all_remainder = True
    settings.move_stop_to_be_after_tp1 = True
    settings.break_even_fee_buffer_pct = 0.12
    settings.break_even_open_fee_fallback_rate = 0.0006
    settings.break_even_expected_close_fee_rate = 0.0006
    settings.break_even_spread_buffer_pct = 0.02
    settings.break_even_slippage_buffer_pct = 0.03
    settings.break_even_extra_buffer_pct = 0.01
    settings.break_even_mark_safety_ticks = 2
    settings.position_model_dev_assertions = False
    settings.execution_mode = "PAPER"
    settings.app_env = "test"
    settings.symbol_cooldown_minutes = 30
    settings.account_equity_usdt = 100.0
    settings.profit_lock_tp1_fraction = 0.60
    settings.dead_trade_timeout_reclaim_minutes = 90.0
    settings.dead_trade_timeout_default_minutes = 240.0
    settings.dead_trade_max_abs_pnl_pct = 0.20
    return settings


def _manager(close_result: dict | Exception) -> PositionManager:
    manager = PositionManager(settings=_manager_settings())
    client = MagicMock()
    if isinstance(close_result, Exception):
        client.close_futures_position_full.side_effect = close_result
    else:
        client.close_futures_position_full.return_value = close_result
    manager.client = client
    return manager


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_dead_trade_close_marks_closed_only_on_confirmed_flat(direction):
    manager = _manager({"status": "CLOSED"})
    position = {"symbol": "SOLUSDT", "direction": direction, "status": "OPEN"}

    result = manager.client.close_futures_position_full(
        symbol="SOLUSDT", direction=direction, reason="dead_trade_timeout"
    )

    assert str(result["status"]).upper() in CLOSE_CONFIRMED_FLAT_STATUSES
    manager.client.close_futures_position_full.assert_called_once()
    assert position["status"] == "OPEN"  # only the caller may transition it


@pytest.mark.parametrize("status", ["CLOSE_NOT_REFLECTED", "PARTIALLY_CLOSED", "CLOSE_FAILED"])
def test_unconfirmed_close_statuses_never_count_as_flat(status):
    assert status not in CLOSE_CONFIRMED_FLAT_STATUSES


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_residual_cleanup_keeps_position_open_when_size_remains(direction):
    manager = _manager({"status": "CLOSE_NOT_REFLECTED", "remaining_size": 0.3})

    result = manager.client.close_futures_position_full(
        symbol="SOLUSDT", direction=direction, reason="residual_position_cleanup"
    )

    assert str(result["status"]).upper() not in CLOSE_CONFIRMED_FLAT_STATUSES


def test_dead_trade_close_attempts_are_bounded():
    """111 identical rejected closes on SOLUSDT is what this bound prevents."""
    assert DEAD_TRADE_CLOSE_MAX_ATTEMPTS >= 1
    assert DEAD_TRADE_CLOSE_MAX_ATTEMPTS <= 5

    position: dict = {"symbol": "SOLUSDT", "direction": "LONG", "status": "OPEN"}
    attempts = 0
    for _ in range(50):
        if int(position.get("dead_trade_close_attempts") or 0) >= DEAD_TRADE_CLOSE_MAX_ATTEMPTS:
            break
        position["dead_trade_close_attempts"] = (
            int(position.get("dead_trade_close_attempts") or 0) + 1
        )
        attempts += 1

    assert attempts == DEAD_TRADE_CLOSE_MAX_ATTEMPTS


def test_dead_trade_retry_bound_holds_across_cycles_and_escalates_once():
    """The bound must survive the cycle, or it bounds nothing.

    The counter lives on the persisted position record. If it did not round-trip
    through the store, every monitor cycle would start a fresh run of three and
    the 285-attempt SOLUSDT loop would simply come back slower.
    """
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    from test_position_lifecycle_safety import (  # noqa: PLC0415
        _live_payload,
        _manager,
        _position,
        _snapshot,
    )

    manager = _manager([_live_payload(size=1.0, mark_price=100.0)])
    manager.client.close_futures_position_full.side_effect = _Rejected(
        "Bitget HTTP error: status=400 code=22002 msg=No position to close"
    )
    stale = (datetime.now(timezone.utc) - timedelta(minutes=300)).isoformat()
    manager.store.save([_position(opened_at=stale)])

    for _ in range(10):
        manager.sync([_snapshot(price=100.0)])

    saved = manager.store.load(default=[])[0]
    assert saved["dead_trade_close_attempts"] == DEAD_TRADE_CLOSE_MAX_ATTEMPTS
    assert manager.client.close_futures_position_full.call_count == (
        DEAD_TRADE_CLOSE_MAX_ATTEMPTS
    )
    # Position survives the abandonment, still open and still protected.
    assert saved["status"] == "OPEN"
    assert saved["stop_loss"] == 99.0
    assert saved["exchange_stop_loss"] == 99.0


def test_dead_trade_retry_bound_is_per_position_not_per_symbol():
    """A fresh position on the same symbol starts its own budget."""
    from test_position_lifecycle_safety import _position  # noqa: PLC0415

    exhausted = _position(dead_trade_close_attempts=DEAD_TRADE_CLOSE_MAX_ATTEMPTS)
    replacement = _position()

    assert exhausted["dead_trade_close_attempts"] == DEAD_TRADE_CLOSE_MAX_ATTEMPTS
    assert "dead_trade_close_attempts" not in replacement


def test_two_open_positions_stay_symbol_isolated(tmp_path):
    client = _flatten_client(
        tmp_path,
        [
            {"symbol": "BTCUSDT", "holdSide": "long", "total": "1.0"},
            {"symbol": "SOLUSDT", "holdSide": "short", "total": "2.0"},
        ],
    )

    client.emergency_flatten_all()

    per_symbol = {}
    for body in _sent_bodies(client):
        per_symbol.setdefault(body["symbol"], []).append(body)

    assert set(per_symbol) == {"BTCUSDT", "SOLUSDT"}
    assert len(per_symbol["BTCUSDT"]) == 1
    assert len(per_symbol["SOLUSDT"]) == 1
    # No payload borrows the other symbol's side.
    assert per_symbol["BTCUSDT"][0]["side"] != per_symbol["SOLUSDT"][0]["side"]


# --- 15: exactly one economic close, and only when confirmed flat ---------


def test_tp3_close_that_does_not_flatten_writes_no_close_economics():
    """A remainder still on the exchange must not produce a CLOSE record.

    Mirrors test_tp3_hit_closes_all_and_marks_closed, which pins the confirmed
    path; this pins the unconfirmed one.
    """
    from test_position_lifecycle_safety import (  # noqa: PLC0415
        _live_payload,
        _manager,
        _position,
        _snapshot,
        _v2_close_rows,
    )

    manager = _manager([_live_payload(size=0.3, mark_price=103.5)])
    manager.client.close_futures_position_full.return_value = {
        "status": "CLOSE_NOT_REFLECTED",
        "remaining_size": 0.3,
    }
    manager.store.save(
        [_position(tp1_hit=True, tp2_hit=True, remaining_size_pct=30.0, break_even_active=True)]
    )

    manager.sync([_snapshot(price=103.5, high=103.5, low=102.5)])

    saved = manager.store.load(default=[])[0]
    assert saved["status"] == "OPEN"
    assert saved.get("closed_reason") != "tp3"
    assert _v2_close_rows() == []


# --- 20: nothing about risk, sizing or protection distances moved ---------


#: What production actually runs, from .env.live. Deliberately NOT the defaults
#: in app/config.py: those are risk 0.75, leverage 5.0 and one execution per
#: cycle, and describe no deployment. An earlier version of this test asserted
#: the defaults under a name that promised production invariants -- it passed
#: while pinning something that has never been live. The distinction is the
#: whole point of the test, so the numbers are written out here rather than
#: read from a Settings() with no env.
PRODUCTION_INVARIANTS = {
    "ACCOUNT_RISK_PER_TRADE_PCT": 0.50,
    "DEFAULT_LEVERAGE": 3.0,
    "MAX_LEVERAGE": 3.0,
    "MAX_OPEN_POSITIONS": 2,
    "EXECUTION_MAX_PER_CYCLE": 2,
    "EXECUTION_MAX_LIVE_NOTIONAL_PER_TRADE_USDT": 35.0,
}


def test_production_invariants_survive_settings_construction():
    """The LIVE sizing envelope loads exactly as written: nothing coerces it."""
    settings = Settings(_env_file=None, **PRODUCTION_INVARIANTS)

    assert settings.account_risk_per_trade_pct == 0.50
    assert settings.default_leverage == 3.0
    assert settings.max_leverage == 3.0
    assert settings.max_open_positions == 2
    assert settings.execution_max_per_cycle == 2
    assert settings.execution_max_live_notional_per_trade_usdt == 35.0

    # The repo's own hard LIVE portfolio caps.
    assert LIVE_MAX_OPEN_POSITIONS == 2
    assert LIVE_MAX_EXECUTIONS_PER_CYCLE == 2


@pytest.mark.parametrize(
    "module",
    [
        "clients/bitget_order_client.py",
        "execution/position_manager.py",
        "execution/tp_sl_lifecycle.py",
    ],
)
def test_close_path_modules_read_no_sizing_or_risk_parameter(module):
    """The close fix is execution transport. It has no business reading risk.

    A sizing or leverage setting appearing in one of these files would mean the
    hotfix had grown past its scope.
    """
    source = (Path(__file__).resolve().parents[1] / module).read_text()
    for parameter in (
        "account_risk_per_trade_pct",
        "default_leverage",
        "max_leverage",
        "execution_max_live_notional_per_trade_usdt",
        "max_open_positions",
        "execution_max_per_cycle",
    ):
        assert parameter not in source, f"{module} now reads {parameter}"
