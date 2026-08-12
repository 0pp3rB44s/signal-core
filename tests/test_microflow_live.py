from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from clients.bitget_order_client import BitgetOrderClientMixin
from clients.bitget_account_client import BitgetAccountClientMixin
from microflow.candidates import CandidateEpisodeSampler, FrozenResearchSpec
from microflow.live import MicroflowLiveRuntime, MicroflowPhase, size_microflow_position
from execution.execution_service import (
    ioc_order_is_confirmed_unfilled,
    normal_entry_policy,
)


def _snapshot(*, direction="LONG", now=1_000_000, trade_age=0, book_age=0,
              sequence_valid=True):
    sign = 1 if direction == "LONG" else -1
    return {
        "timestamp_local": now,
        "trade_flow": {
            "1s": {"ofi": 0.1 * sign},
            "5s": {"ofi": 0.30 * sign},
            "15s": {"ofi": 0.28 * sign},
            "30s": {"ofi": 0.26 * sign},
            "60s": {"ofi": 0.24 * sign, "realized_range_bps": 12.0},
        },
        "book": {
            "best_bid": 99.99, "best_ask": 100.0, "spread_bps": 1.0,
            "book_imbalance_top1": 0.3 * sign,
            "book_imbalance_top5": 0.25 * sign,
        },
        "microprice": {"mid_price": 99.995, "microprice": 99.997,
                       "microprice_vs_mid_bps": 0.2 * sign},
        "freshness": {"sequence_valid": sequence_valid,
                      "trade_stream_age_ms": trade_age,
                      "book_stream_age_ms": book_age},
    }


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_frozen_signal_is_symmetric(direction):
    assert CandidateEpisodeSampler(FrozenResearchSpec())._direction(
        _snapshot(direction=direction)
    ) == direction


@pytest.mark.parametrize("field", ["trade", "book"])
def test_stale_feed_fails_closed(field):
    kwargs = {"trade_age": 1_001} if field == "trade" else {"book_age": 1_001}
    assert CandidateEpisodeSampler()._direction(_snapshot(**kwargs)) is None


def test_sequence_error_fails_closed():
    assert CandidateEpisodeSampler()._direction(
        _snapshot(sequence_valid=False)
    ) is None


def test_risk_sizing_uses_stop_fees_and_slippage_not_leverage_multiplier():
    sized = size_microflow_position(
        equity_usdt=46.674882, risk_pct=0.5, notional_cap_usdt=35,
        leverage=5, taker_fee_rate=0.0006, slippage_bps=1,
    )
    assert sized.notional_usdt == 35
    assert sized.total_loss_usdt == pytest.approx(0.1155)
    assert sized.total_loss_pct_equity < 0.5
    assert sized.risk_budget_usdt == pytest.approx(0.23337441)


def test_pre_submit_guard_enforces_one_bps_cap_and_fresh_signal():
    runtime = MicroflowLiveRuntime.__new__(MicroflowLiveRuntime)
    runtime.settings = SimpleNamespace(
        microflow_max_slippage_bps=1.0, microflow_leverage=3,
        default_leverage=3, max_leverage=3, account_risk_per_trade_pct=0.5,
        execution_max_live_notional_per_trade_usdt=35,
    )
    runtime.client = MagicMock()
    runtime.client.get_accounts.return_value = {
        "data": [{"marginCoin": "USDT", "accountEquity": "46.674882"}]
    }
    runtime.client.get_trade_fee_rate.return_value = {
        "data": {"makerFeeRate": "0.0002", "takerFeeRate": "0.0006"}
    }
    runtime._validator = CandidateEpisodeSampler()
    runtime.collector = MagicMock()
    runtime.collector.latest_snapshot.return_value = _snapshot(now=1_000_000)
    plan = SimpleNamespace(
        symbol="BTCUSDT", direction="LONG", geometry_entry=99.995,
        take_profits=[100.395], candidate_candle_open_timestamp_ms=1_000_000,
        position_notional_usdt=35,
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("microflow.live.time.time", lambda: 1_000.0)
        result = runtime.pre_submit_guard(plan)
    assert result["allowed"] is True
    assert result["limit_price"] <= 99.995 * 1.0001


def test_state_machine_contains_every_required_phase():
    assert {phase.value for phase in MicroflowPhase} == {
        "IDLE", "PRESSURE_FORMING", "CANDIDATE", "ENTRY_PENDING",
        "OPEN", "EXIT_PENDING", "COOLDOWN",
    }


class _IOCClient:
    place_futures_ioc_order = BitgetOrderClientMixin.place_futures_ioc_order

    def __init__(self):
        self.settings = SimpleNamespace(bitget_product_type="USDT-FUTURES")
        self.log = MagicMock()
        self.body = None

    def _assert_order_transport_allowed(self):
        return None

    def _format_trigger_price(self, _symbol, price):
        return price

    def validate_entry_size(self, _symbol, size, reference_price=None):
        return size, None

    def _validate_futures_order_flags(self, body):
        assert body["marginMode"] == "isolated"

    def _request(self, **kwargs):
        self.body = kwargs["body"]
        return {"data": {"orderId": "ioc-1"}}


def test_ioc_transport_is_price_capped_and_never_market():
    client = _IOCClient()
    client.place_futures_ioc_order(
        "BTCUSDT", "LONG", 0.001, 100.01, client_oid="bgai-i-test"
    )
    assert client.body["orderType"] == "limit"
    assert client.body["force"] == "ioc"
    assert client.body["marginMode"] == "isolated"
    assert client.body["price"] == "100.01"


def test_global_maker_flag_cannot_intercept_microflow_ioc_route():
    settings = SimpleNamespace(
        maker_entry_enabled=True, maker_entry_fallback_market=False,
    )
    assert normal_entry_policy(settings, "microflow_scalper_v1") == (False, True)


def test_ioc_intent_retires_only_on_terminal_explicit_zero_fill():
    assert ioc_order_is_confirmed_unfilled({
        "state": "canceled", "raw": {"baseVolume": "0", "size": "99"}
    }) is True
    assert ioc_order_is_confirmed_unfilled({
        "state": "canceled", "raw": {"baseVolume": "0.01", "size": "99"}
    }) is False


class _MarginClient:
    set_futures_margin_mode = BitgetAccountClientMixin.set_futures_margin_mode

    def __init__(self):
        self.settings = SimpleNamespace(bitget_product_type="USDT-FUTURES")
        self.request = None

    def _assert_order_transport_allowed(self):
        return None

    def _request(self, method, path, **kwargs):
        self.request = (method, path, kwargs)
        return {"code": "00000"}


def test_margin_mode_transport_sets_isolated_explicitly():
    client = _MarginClient()
    client.set_futures_margin_mode("HYPEUSDT", "isolated")
    method, path, kwargs = client.request
    assert method == "POST"
    assert path == "/api/v2/mix/account/set-margin-mode"
    assert kwargs["body"] == {
        "symbol": "HYPEUSDT", "productType": "USDT-FUTURES",
        "marginCoin": "USDT", "marginMode": "isolated",
    }


def test_margin_mode_transport_rejects_unknown_mode_before_network():
    client = _MarginClient()
    with pytest.raises(ValueError, match="unsupported futures margin mode"):
        client.set_futures_margin_mode("HYPEUSDT", "portfolio")
    assert client.request is None
    assert ioc_order_is_confirmed_unfilled({
        "state": "live", "raw": {"baseVolume": "0"}
    }) is False
