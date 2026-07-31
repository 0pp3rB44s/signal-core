"""Deterministic contract tests for the position-model migration."""

from __future__ import annotations

import inspect
import json
import logging
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from dashboard_v2 import data_provider
from execution.position_manager import PositionManager
from execution.position_model import (
    BE_PLUS_FEES_CONFIRMED,
    CONFIGURED_FALLBACK,
    EXCHANGE_ACTUAL,
    EXCHANGE_RATE,
    LEGACY_FALLBACK,
    PROTECTION_UPDATE_FAILED,
    AuthoritativeEntryUnavailable,
    CriticalLegacyRead,
    OpeningFeeSelection,
    PositionLifecycleMismatch,
    calculate_break_even_plus_fees,
    confirm_exchange_position,
    confirmed_position_size,
    legacy_avg_entry,
    migrate_planned_entry,
    position_prices,
    select_opening_fee,
    stop_is_legal,
)


D = Decimal
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "position_model_forensic_replay.json"


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        break_even_open_fee_fallback_rate=0.0006,
        break_even_expected_close_fee_rate=0.0006,
        break_even_spread_buffer_pct=0.02,
        break_even_slippage_buffer_pct=0.03,
        break_even_extra_buffer_pct=0.01,
        break_even_fee_buffer_pct=0.12,
        break_even_mark_safety_ticks=2,
        symbol_cooldown_minutes=30,
        default_leverage=3.0,
        execution_mode="PAPER",
        app_env="test",
        position_model_dev_assertions=True,
    )


def _position(
    *,
    direction: str = "LONG",
    planned: str = "90",
    executed: str | None = "100",
    size: str = "2",
    stop: str | None = None,
    lifecycle_id: str = "life-1",
) -> dict:
    default_stop = "99" if direction == "LONG" else "101"
    row = {
        "symbol": "BTCUSDT",
        "direction": direction,
        "status": "OPEN",
        "planned_avg_entry": float(planned),
        "avg_entry": float(planned),
        "actual_entry": 97.0,
        "position_lifecycle_id": lifecycle_id,
        "confirmed_position_size": float(size),
        "confirmed_fill_quantity": float(size),
        "confirmed_remaining_size": float(size),
        "exchange_tick_size": 0.01,
        "confirmed_stop": float(stop or default_stop),
        "stop_loss": float(stop or default_stop),
        "exchange_stop_loss": float(stop or default_stop),
        "exchange_stop_loss_order_id": "old-sl",
        "active_stop_loss_order_ids": ["old-sl"],
        "leverage": 3,
    }
    if executed is not None:
        row.update(
            {
                "exchange_avg_entry": float(executed),
                "exchange_avg_entry_source": "BITGET_OPEN_POSITION_TEST",
                "exchange_avg_entry_confirmed_at": "2026-07-11T00:00:00+00:00",
                "exchange_entry_order_id": "entry-order",
                "exchange_entry_client_oid": "entry-client",
            }
        )
    return row


def _live(
    *,
    direction: str = "LONG",
    entry: str = "100",
    size: str = "2",
    mark: str = "101",
    lifecycle_id: str | None = None,
) -> dict:
    payload = {
        "symbol": "BTCUSDT",
        "holdSide": "long" if direction == "LONG" else "short",
        "openPriceAvg": entry,
        "total": size,
        "markPrice": mark,
    }
    if lifecycle_id is not None:
        payload["positionLifecycleId"] = lifecycle_id
    return payload


def _manager() -> PositionManager:
    manager = PositionManager.__new__(PositionManager)
    manager.settings = _settings()
    manager.log = logging.getLogger("position-model-test")
    manager.client = MagicMock()
    manager.client.move_futures_stop_loss.return_value = {
        "placed": {"data": {"orderId": "new-sl"}},
    }
    manager.client.verify_active_stop_loss.return_value = {"verified": True}
    manager.client.cancel_futures_plan_order.return_value = {"code": "00000"}
    manager.client._contract_price_scale.return_value = 2
    manager.client.get_active_protection_snapshot.return_value = {
        "stop_orders": [],
        "take_profit_orders": [],
    }
    manager.be_move_retries = 3
    manager.failed_continuation_sl_buffer_pct = 0.06
    manager.failed_continuation_min_age_minutes = 10.0
    manager.failed_continuation_min_unrealized_pct = -0.35
    manager.tp_miss_near_pct = 0.18
    return manager


def _itemised_be(
    *,
    direction: str = "LONG",
    entry: str = "100",
    size: str = "2",
    opening_fee: str = "0.12",
    tick: str = "0.01",
):
    return calculate_break_even_plus_fees(
        direction=direction,
        exchange_entry=D(entry),
        remaining_quantity=D(size),
        tick_size=D(tick),
        opening_fee=OpeningFeeSelection(D(opening_fee), EXCHANGE_ACTUAL, D("0")),
        expected_close_fee_rate=D("0.0006"),
        spread_buffer_pct=D("0.02"),
        slippage_buffer_pct=D("0.03"),
        extra_buffer_pct=D("0.01"),
        legacy_fee_buffer_pct=D("0.12"),
    )


def _forensic_trade(trade_id: int) -> dict:
    payload = json.loads(FIXTURE_PATH.read_text())
    return next(row for row in payload["trades"] if row["id"] == trade_id)


def test_01_planned_and_executed_entry_remain_distinct():
    prices = position_prices(_position(planned="90", executed="100"))
    assert prices.planned == D("90")
    assert prices.require_executed().price == D("100")


def test_02_actual_entry_is_never_authoritative():
    position = _position(executed=None)
    position["actual_entry"] = 123.45
    migrate_planned_entry(position)
    with pytest.raises(AuthoritativeEntryUnavailable):
        position_prices(position, require_executed=True)
    assert "exchange_avg_entry" not in position


def test_03_be_uses_exchange_avg_entry():
    manager = _manager()
    position = _position(planned="80", executed="100")
    result = manager._break_even_plus_fees(position, live_position=_live())
    assert result.target > D("100")
    assert result.target < D("101")


def test_04_live_price_return_uses_exchange_avg_entry():
    manager = _manager()
    economics = manager._live_economics(
        _position(planned="80", executed="100"),
        current_price=110,
    )
    assert economics.price_return_pct == D("10.0")


def test_05_margin_roi_uses_exchange_avg_entry():
    manager = _manager()
    position = _position(planned="80", executed="100")
    position["exchange_opening_fee_usdt"] = 0.12
    position["exchange_opening_fee_source"] = EXCHANGE_ACTUAL
    economics = manager._live_economics(position, current_price=110)
    expected_margin = D("200") / D("3")
    expected_net = D("20") - D("0.12") - (D("110") * D("2") * D("0.0006"))
    assert economics.margin_roi_pct == (expected_net / expected_margin) * D("100")


def test_06_failed_continuation_uses_exchange_avg_entry():
    manager = _manager()
    position = _position(planned="80", executed="100")
    target = manager._failed_continuation_target_stop(
        position,
        "LONG",
        101.0,
        live_position=_live(mark="101"),
    )
    assert target > 100


def test_07_cooldown_uses_explicit_margin_roi_metric():
    manager = _manager()
    manager.cooldowns = MagicMock()
    manager.cooldowns.set_cooldown.side_effect = [
        SimpleNamespace(
            symbol="BTCUSDT",
            reason="loss_stop_loss",
            until="2026-07-31T00:00:00+00:00",
        ),
        SimpleNamespace(
            symbol="recent_close::BTCUSDT",
            reason="recent_close_loss_stop_loss",
            until="2026-07-31T00:00:00+00:00",
        ),
    ]
    assert "margin_roi_pct" in inspect.signature(
        manager._register_symbol_cooldown
    ).parameters
    manager._register_symbol_cooldown("BTCUSDT", "stop_loss", margin_roi_pct=-1)
    assert manager.cooldowns.set_cooldown.call_args_list[0].kwargs["minutes"] == 45


def test_08_size_coverage_prefers_current_exchange_size():
    position = _position(size="5")
    size = confirmed_position_size(
        position,
        live_position=_live(size="1.25"),
        critical=True,
    )
    assert size.quantity == D("1.25")
    assert size.source == "CURRENT_EXCHANGE_OPEN_SIZE"


def test_09_skipped_plan_cannot_alter_authoritative_entry():
    position = _position(planned="90", executed="100")
    position["status"] = "SKIPPED"
    position["planned_avg_entry"] = 250
    position["avg_entry"] = 250
    migrate_planned_entry(position)
    assert position_prices(position).require_executed().price == D("100")


def test_10_new_lifecycle_cannot_inherit_old_entry():
    position = _position(lifecycle_id="old-life")
    with pytest.raises(PositionLifecycleMismatch):
        confirm_exchange_position(
            position,
            _live(lifecycle_id="new-life"),
            source="BITGET_RESTART",
        )
    assert position["exchange_avg_entry"] == 100


def test_11_immutable_entry_survives_restart_roundtrip():
    restored = json.loads(json.dumps(_position(planned="90", executed="100")))
    confirm_exchange_position(
        restored,
        _live(entry="100", lifecycle_id="life-1"),
        source="BITGET_RESTART",
    )
    assert restored["exchange_avg_entry"] == 100
    with pytest.raises(PositionLifecycleMismatch):
        confirm_exchange_position(
            restored,
            _live(entry="101", lifecycle_id="life-1"),
            source="BITGET_RESTART",
        )


def test_12_long_be_includes_every_cost_exactly_once():
    result = _itemised_be(direction="LONG")
    assert result.required_recovery_usdt == (
        result.opening_fee_usdt
        + result.expected_closing_fee_usdt
        + result.spread_allowance_usdt
        + result.slippage_allowance_usdt
        + result.extra_safety_allowance_usdt
    )


def test_13_short_be_includes_every_cost_exactly_once():
    result = _itemised_be(direction="SHORT")
    assert result.required_recovery_usdt == (
        result.opening_fee_usdt
        + result.expected_closing_fee_usdt
        + result.spread_allowance_usdt
        + result.slippage_allowance_usdt
        + result.extra_safety_allowance_usdt
    )


def test_14_actual_opening_fee_has_precedence():
    position = _position()
    position["exchange_opening_fee_usdt"] = -0.123
    position["exchange_opening_fee_source"] = EXCHANGE_ACTUAL
    position["confirmed_opening_fee_usdt"] = 9
    position["confirmed_opening_fee_source"] = "PERSISTED_CONFIRMED_EXECUTION_FEE"
    position["exchange_open_fee_rate"] = 0.01
    selected = select_opening_fee(
        position,
        exchange_entry=D("100"),
        remaining_quantity=D("2"),
        configured_fallback_rate=D("0.0006"),
    )
    assert selected.amount_usdt == D("0.123")
    assert selected.source == EXCHANGE_ACTUAL


def test_15_closing_fee_uses_expected_exit_notional():
    result = _itemised_be(direction="LONG")
    assert result.expected_closing_fee_usdt == result.target * D("2") * D("0.0006")


def test_16_legacy_fallback_is_exclusive():
    result = calculate_break_even_plus_fees(
        direction="LONG",
        exchange_entry=D("100"),
        remaining_quantity=D("2"),
        tick_size=D("0.01"),
        opening_fee=OpeningFeeSelection(D("0"), LEGACY_FALLBACK, D("0")),
        expected_close_fee_rate=D("0.0006"),
        spread_buffer_pct=D("0.02"),
        slippage_buffer_pct=D("0.03"),
        extra_buffer_pct=D("0.01"),
        legacy_fee_buffer_pct=D("0.12"),
    )
    assert result.used_legacy_fallback is True
    assert result.target == D("100.12")
    assert result.expected_closing_fee_usdt == 0
    assert result.spread_allowance_usdt == 0


def test_17_long_rounding_remains_cost_covering():
    result = _itemised_be(direction="LONG", tick="0.1")
    assert result.target % D("0.1") == 0
    assert result.expected_net_usdt >= 0


def test_18_short_rounding_remains_cost_covering():
    result = _itemised_be(direction="SHORT", tick="0.1")
    assert result.target % D("0.1") == 0
    assert result.expected_net_usdt >= 0


def test_owner_approved_be_plus_fees_semantic_at_entry_100():
    long_result = _itemised_be(direction="LONG", entry="100", size="1", opening_fee="0.06")
    short_result = _itemised_be(direction="SHORT", entry="100", size="1", opening_fee="0.06")

    assert long_result.target == D("100.19")
    assert long_result.opening_fee_usdt == D("0.06")
    assert long_result.expected_closing_fee_usdt == D("0.060114")
    assert long_result.spread_allowance_usdt == D("0.020038")
    assert long_result.slippage_allowance_usdt == D("0.030057")
    assert long_result.extra_safety_allowance_usdt == D("0.010019")
    assert long_result.expected_net_usdt == D("0.009772")

    assert short_result.target == D("99.82")
    assert short_result.opening_fee_usdt == D("0.06")
    assert short_result.expected_closing_fee_usdt == D("0.059892")
    assert short_result.spread_allowance_usdt == D("0.019964")
    assert short_result.slippage_allowance_usdt == D("0.029946")
    assert short_result.extra_safety_allowance_usdt == D("0.009982")
    assert short_result.expected_net_usdt == D("0.000216")
    assert long_result.expected_net_usdt >= 0
    assert short_result.expected_net_usdt >= 0


def test_opening_fee_precedence_is_actual_persisted_rate_then_configured_fallback():
    base = _position()
    fallback = select_opening_fee(
        base,
        exchange_entry=D("100"),
        remaining_quantity=D("2"),
        configured_fallback_rate=D("0.0006"),
    )
    assert fallback.source == CONFIGURED_FALLBACK
    assert fallback.amount_usdt == D("0.12")

    by_rate = dict(base, exchange_open_fee_rate=0.0005)
    selected_rate = select_opening_fee(
        by_rate,
        exchange_entry=D("100"),
        remaining_quantity=D("2"),
        configured_fallback_rate=D("0.0006"),
    )
    assert selected_rate.source == EXCHANGE_RATE
    assert selected_rate.amount_usdt == D("0.10000")

    persisted = dict(
        by_rate,
        confirmed_opening_fee_usdt=0.09,
        confirmed_opening_fee_source="PERSISTED_CONFIRMED_EXECUTION_FEE",
    )
    selected_persisted = select_opening_fee(
        persisted,
        exchange_entry=D("100"),
        remaining_quantity=D("2"),
        configured_fallback_rate=D("0.0006"),
    )
    assert selected_persisted.source == EXCHANGE_ACTUAL
    assert selected_persisted.amount_usdt == D("0.09")

    actual = dict(
        persisted,
        exchange_opening_fee_usdt=-0.08,
        exchange_opening_fee_source=EXCHANGE_ACTUAL,
    )
    selected_actual = select_opening_fee(
        actual,
        exchange_entry=D("100"),
        remaining_quantity=D("2"),
        configured_fallback_rate=D("0.0006"),
    )
    assert selected_actual.source == EXCHANGE_ACTUAL
    assert selected_actual.amount_usdt == D("0.08")


def test_exchange_confirmed_zero_opening_fee_still_precedes_fallback():
    position = dict(
        _position(),
        exchange_opening_fee_usdt=0,
        exchange_opening_fee_source=EXCHANGE_ACTUAL,
        exchange_open_fee_rate=0.01,
    )

    selected = select_opening_fee(
        position,
        exchange_entry=D("100"),
        remaining_quantity=D("2"),
        configured_fallback_rate=D("0.0006"),
    )

    assert selected.source == EXCHANGE_ACTUAL
    assert selected.amount_usdt == D("0")


def test_19_illegal_be_target_is_not_submitted():
    manager = _manager()
    position = _position()
    manager.client.get_all_positions.return_value = {
        "data": [_live(mark="100.10")]
    }
    assert manager._move_exchange_stop_loss_with_retries(
        position,
        100.19,
        "PROFIT_LOCK_BE",
    ) is False
    manager.client.move_futures_stop_loss.assert_not_called()


def test_20_illegal_target_is_not_labelled_be():
    manager = _manager()
    position = _position()
    manager.client.get_all_positions.return_value = {
        "data": [_live(mark="100.10")]
    }
    manager._move_exchange_stop_loss_with_retries(
        position,
        100.19,
        "PROFIT_LOCK_BE",
    )
    assert position["protection_state"] == PROTECTION_UPDATE_FAILED
    assert position.get("break_even_active") is not True
    assert position["be_plus_fees_status"] == "BE_WINDOW_MISSED"


def test_21_retry_refreshes_mark():
    manager = _manager()
    position = _position()
    manager.client.get_all_positions.side_effect = [
        {"data": [_live(mark="101.0")]},
        {"data": [_live(mark="102.0")]},
    ]
    manager.client.move_futures_stop_loss.side_effect = [
        RuntimeError("first submit rejected"),
        {"placed": {"data": {"orderId": "new-sl"}}},
    ]
    assert manager._move_exchange_stop_loss_with_retries(
        position,
        100.19,
        "PROFIT_LOCK_BE",
    )
    assert manager.client.get_all_positions.call_count == 2
    assert position["last_protection_retry_mark"] == 102.0


def test_22_retry_recalculates_target_from_remaining_size():
    manager = _manager()
    position = _position(size="2")
    position["exchange_opening_fee_usdt"] = 0.12
    position["exchange_opening_fee_source"] = EXCHANGE_ACTUAL
    manager.client.get_all_positions.side_effect = [
        {"data": [_live(mark="102", size="2")]},
        {"data": [_live(mark="102", size="1")]},
    ]
    manager.client.move_futures_stop_loss.side_effect = [
        RuntimeError("first submit rejected"),
        {"placed": {"data": {"orderId": "new-sl"}}},
    ]
    assert manager._move_exchange_stop_loss_with_retries(
        position,
        100.01,
        "PROFIT_LOCK_BE",
    )
    calls = manager.client.move_futures_stop_loss.call_args_list
    assert len(calls) == 2
    assert calls[1].kwargs["trigger_price"] > calls[0].kwargs["trigger_price"]


def test_23_stale_absolute_trigger_is_not_reused():
    manager = _manager()
    position = _position()
    manager.client.get_all_positions.return_value = {"data": [_live(mark="101")]}
    assert manager._move_exchange_stop_loss_with_retries(
        position,
        99.01,
        "PROFIT_LOCK_BE",
    )
    submitted = manager.client.move_futures_stop_loss.call_args.kwargs["trigger_price"]
    assert submitted != 99.01
    assert submitted == position["calculated_be_plus_fees"]


def test_24_old_sl_remains_active_during_replacement():
    manager = _manager()
    position = _position()
    manager.client.get_all_positions.return_value = {"data": [_live(mark="101")]}

    def submit_while_old_active(**kwargs):
        manager.client.cancel_futures_plan_order.assert_not_called()
        return {"placed": {"data": {"orderId": "new-sl"}}}

    manager.client.move_futures_stop_loss.side_effect = submit_while_old_active
    assert manager._move_exchange_stop_loss_with_retries(
        position,
        100.19,
        "PROFIT_LOCK_BE",
    )
    manager.client.cancel_futures_plan_order.assert_called_once_with(
        symbol="BTCUSDT",
        order_id="old-sl",
    )


def test_retry_refreshes_metadata_and_active_protection_on_every_attempt():
    manager = _manager()
    position = _position()
    manager.client.get_all_positions.side_effect = [
        {"data": [_live(mark="101")]},
        {"data": [_live(mark="101.1")]},
    ]
    manager.client.move_futures_stop_loss.side_effect = [
        RuntimeError("transient exchange error"),
        {"placed": {"data": {"orderId": "new-sl"}}},
    ]

    assert manager._move_exchange_stop_loss_with_retries(
        position,
        100.19,
        "PROFIT_LOCK_BE",
    )

    assert manager.client.get_active_protection_snapshot.call_count == 2
    assert manager.client._contract_price_scale.call_count == 2
    assert all(
        call.kwargs == {"force_refresh": True}
        for call in manager.client._contract_price_scale.call_args_list
    )


def test_retry_never_downgrades_a_tighter_exchange_active_stop():
    manager = _manager()
    position = _position(stop="99")
    manager.client.get_all_positions.return_value = {"data": [_live(mark="102")]}
    manager.client.get_active_protection_snapshot.return_value = {
        "stop_orders": [
            {
                "order_id": "exchange-tighter-sl",
                "trigger_price": 100.5,
                "hold_side": "long",
                "plan_type": "loss_plan",
            }
        ],
        "take_profit_orders": [],
    }

    assert not manager._move_exchange_stop_loss_with_retries(
        position,
        100.19,
        "PROFIT_LOCK_BE",
    )

    manager.client.move_futures_stop_loss.assert_not_called()
    assert position["confirmed_stop"] == 100.5
    assert position["exchange_stop_loss"] == 100.5
    assert position["stop_loss"] == 100.5


def test_25_long_protection_is_monotonic():
    manager = _manager()
    position = _position(stop="100.50")
    manager.client.get_all_positions.return_value = {"data": [_live(mark="101")]}
    assert not manager._move_exchange_stop_loss_with_retries(
        position,
        100.19,
        "PROFIT_LOCK_BE",
    )
    assert position["confirmed_stop"] == 100.5
    manager.client.move_futures_stop_loss.assert_not_called()


def test_26_short_protection_is_monotonic():
    manager = _manager()
    position = _position(direction="SHORT", stop="99.50")
    manager.client.get_all_positions.return_value = {
        "data": [_live(direction="SHORT", mark="99")]
    }
    assert not manager._move_exchange_stop_loss_with_retries(
        position,
        99.82,
        "PROFIT_LOCK_BE",
    )
    assert position["confirmed_stop"] == 99.5
    manager.client.move_futures_stop_loss.assert_not_called()


def test_27_missing_exchange_truth_fails_closed():
    manager = _manager()
    position = _position(executed=None, stop="99")
    assert manager._candidate_break_even_stop(position) == 0
    assert position["confirmed_stop"] == 99
    assert position["protection_state"] == PROTECTION_UPDATE_FAILED


def test_28_lifecycle_mismatch_fails_closed():
    manager = _manager()
    position = _position(lifecycle_id="life-1")
    manager.client.get_all_positions.return_value = {
        "data": [_live(lifecycle_id="life-2")]
    }
    assert not manager._move_exchange_stop_loss_with_retries(
        position,
        100.19,
        "PROFIT_LOCK_BE",
    )
    assert position["confirmed_stop"] == 99
    manager.client.move_futures_stop_loss.assert_not_called()


def test_29_partial_fills_use_remaining_quantity():
    full = _itemised_be(direction="LONG", size="2", opening_fee="0.12")
    partial = _itemised_be(direction="LONG", size="1", opening_fee="0.12")
    assert partial.target > full.target
    assert confirmed_position_size(
        _position(size="2"),
        live_position=_live(size="1"),
    ).quantity == D("1")


def test_30_fees_are_not_double_counted():
    position = _position()
    position["exchange_opening_fee_usdt"] = 0.12
    position["exchange_opening_fee_source"] = EXCHANGE_ACTUAL
    selected = select_opening_fee(
        position,
        exchange_entry=D("100"),
        remaining_quantity=D("2"),
        configured_fallback_rate=D("0.0006"),
    )
    result = calculate_break_even_plus_fees(
        direction="LONG",
        exchange_entry=D("100"),
        remaining_quantity=D("2"),
        tick_size=D("0.01"),
        opening_fee=selected,
        expected_close_fee_rate=D("0.0006"),
        spread_buffer_pct=D("0.02"),
        slippage_buffer_pct=D("0.03"),
        extra_buffer_pct=D("0.01"),
        legacy_fee_buffer_pct=D("9"),
    )
    assert result.fee_source == EXCHANGE_ACTUAL
    assert result.opening_fee_usdt == D("0.12")
    assert result.used_legacy_fallback is False


def test_31_dashboard_uses_exchange_entry(monkeypatch):
    state = _position(planned="80", executed="100")
    state["protection_state"] = BE_PLUS_FEES_CONFIRMED
    monkeypatch.setattr(data_provider, "_read_json", lambda path: [state])
    monkeypatch.setattr(
        data_provider,
        "get_settings",
        lambda: SimpleNamespace(
            break_even_open_fee_fallback_rate=0.0006,
            break_even_expected_close_fee_rate=0.0006,
        ),
    )
    rows = data_provider._normalize_positions(
        [
            {
                "symbol": "BTCUSDT",
                "holdSide": "long",
                "total": "2",
                "openPriceAvg": "100",
                "markPrice": "110",
                "unrealizedPL": "20",
                "leverage": "3",
            }
        ],
        [],
    )
    assert rows[0]["planned_avg_entry"] == 80
    assert rows[0]["exchange_avg_entry"] == 100
    assert rows[0]["price_return_pct"] == 10
    assert rows[0]["estimated_opening_fee"] == 0.12
    assert rows[0]["opening_fee_source"] == "CONFIGURED_FALLBACK"


def _replay_result(trade: dict):
    return calculate_break_even_plus_fees(
        direction=trade["direction"],
        exchange_entry=D(trade["exchange_avg_entry"]),
        remaining_quantity=D(trade["confirmed_remaining_quantity"]),
        tick_size=D(trade["tick_size"]),
        opening_fee=OpeningFeeSelection(
            D(trade["opening_fee_usdt"]),
            EXCHANGE_ACTUAL,
            D("0"),
        ),
        expected_close_fee_rate=D("0.0006"),
        spread_buffer_pct=D("0.02"),
        slippage_buffer_pct=D("0.03"),
        extra_buffer_pct=D("0.01"),
        legacy_fee_buffer_pct=D("0.12"),
    )


def test_32_trade_2_reproduces_old_long_defect_and_repair():
    trade = _forensic_trade(2)
    result = _replay_result(trade)
    mark = D(trade["decision_candle"]["close"])
    assert D(trade["old_requested_stop"]) < D(trade["exchange_avg_entry"])
    assert result.target == D(trade["corrected_be_plus_fees"])
    assert stop_is_legal(
        direction="LONG",
        target=result.target,
        current_mark=mark,
        tick_size=D(trade["tick_size"]),
        safety_ticks=2,
    )
    assert D(trade["first_later_trade_through_low"]) <= result.target
    assert result.expected_net_usdt == D(trade["expected_net_at_target_usdt"])
    assert trade["hypothetical_fill_claimed"] is False


def test_33_trade_3_recalculates_stale_rejected_trigger():
    trade = _forensic_trade(3)
    result = _replay_result(trade)
    mark = D(trade["decision_candle"]["close"])
    assert not stop_is_legal(
        direction="LONG",
        target=D(trade["old_requested_stop"]),
        current_mark=mark,
        tick_size=D(trade["tick_size"]),
        safety_ticks=2,
    )
    assert stop_is_legal(
        direction="LONG",
        target=result.target,
        current_mark=mark,
        tick_size=D(trade["tick_size"]),
        safety_ticks=2,
    )
    assert D(trade["first_later_trade_through_low"]) <= result.target
    assert result.expected_net_usdt == D(trade["expected_net_at_target_usdt"])


def test_34_trade_4_verifies_short_symmetry():
    trade = _forensic_trade(4)
    result = _replay_result(trade)
    mark = D(trade["decision_candle"]["close"])
    assert result.target == D(trade["corrected_be_plus_fees"])
    assert result.target < D(trade["exchange_avg_entry"])
    assert not stop_is_legal(
        direction="SHORT",
        target=result.target,
        current_mark=mark,
        tick_size=D(trade["tick_size"]),
        safety_ticks=2,
    )
    assert D(trade["later_minimum_low"]) > result.target
    assert trade["market_later_traded_through"] is False
    assert result.expected_net_usdt == D(trade["expected_net_at_target_usdt"])


def test_35_state_does_not_advance_before_exchange_verification():
    manager = _manager()
    position = _position()
    manager.client.get_all_positions.return_value = {"data": [_live(mark="101")]}
    manager.client.verify_active_stop_loss.return_value = {
        "verified": False,
        "reason": "not visible",
    }
    assert not manager._move_exchange_stop_loss_with_retries(
        position,
        100.19,
        "PROFIT_LOCK_BE",
    )
    assert position["protection_state"] == PROTECTION_UPDATE_FAILED
    assert position["confirmed_stop"] == 99
    manager.client.cancel_futures_plan_order.assert_not_called()


def test_development_critical_legacy_read_asserts(caplog):
    position = _position()
    logger = logging.getLogger("legacy-position-read-test")
    with pytest.raises(CriticalLegacyRead), caplog.at_level(logging.WARNING):
        legacy_avg_entry(
            position,
            module="execution.test",
            function="critical_consumer",
            logger=logger,
            critical=True,
            development_assertions=True,
        )
    assert "LEGACY_AVG_ENTRY_READ" in caplog.text


def test_configured_opening_fee_fallback_is_explicit():
    selected = select_opening_fee(
        _position(),
        exchange_entry=D("100"),
        remaining_quantity=D("2"),
        configured_fallback_rate=D("0.0006"),
    )
    assert selected.source == CONFIGURED_FALLBACK
    assert selected.amount_usdt == D("0.12")


def test_symbol_only_protection_fallback_is_forbidden():
    manager = _manager()
    fallback = manager._fallback_protection_from_execution_log(
        "BTCUSDT",
        lifecycle_id="",
    )
    assert fallback["stop_loss"] == 0
    assert fallback["source"] == "execution_log_lifecycle_required"


def test_protection_repair_requires_exchange_verification():
    manager = _manager()
    position = _position()
    position["take_profits"] = [102.0]
    position["protection_state"] = PROTECTION_UPDATE_FAILED
    manager.client.get_all_positions.return_value = {"data": [_live(mark="101")]}
    manager.client.place_futures_protection_orders.return_value = {
        "stop_loss": {"data": {"orderId": "unverified-sl"}},
        "take_profits": [{"data": {"orderId": "unverified-tp"}}],
        "take_profit_count": 1,
        "protection_verified": False,
    }
    assert manager._ensure_exchange_protection(position) is False
    assert position["protection_state"] == PROTECTION_UPDATE_FAILED
