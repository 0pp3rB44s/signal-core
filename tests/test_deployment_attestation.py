from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from clients.bitget_account_client import BitgetAccountClientMixin
from clients.bitget_market_client import BitgetMarketClientMixin
from deployment.config_attestation import ConfigExpectations, attest_config_file
from deployment.exchange_attestation import (
    BitgetReadOnlyAttestationAdapter,
    READ_ONLY_CLIENT_METHODS,
    attest_exchange,
)


from app.symbol_allowlist import OWNER_APPROVED_PRODUCTION_SYMBOLS

OWNER_SYMBOLS = OWNER_APPROVED_PRODUCTION_SYMBOLS


class _ExchangeAdapter:
    def __init__(self, symbols=OWNER_SYMBOLS):
        self.positions = []
        self.pending = []
        self.protections = []
        self.triggers = {"normal_plan": [], "track_plan": []}
        self.intents = []
        self.quarantine_count = 0
        self.fail_plan_symbols = set()
        self.contracts = {
            symbol: {
                "symbol": symbol,
                "symbolStatus": "normal",
                "minTradeNum": "0.001",
                "minTradeUSDT": "5",
                "sizeMultiplier": "0.001",
                "volumePlace": "3",
                "pricePlace": "2",
                "maxLever": "20",
            }
            for symbol in symbols
        }
        self.accounts = {
            symbol: {
                "symbol": symbol,
                "marginMode": "isolated",
                "isolatedLongLever": "3",
                "isolatedShortLever": "3",
            }
            for symbol in self.contracts
        }
        self.prices = {
            symbol: {"symbol": symbol, "markPrice": "100"}
            for symbol in self.contracts
        }
        self.books = {
            symbol: {"best_bid": 99.99, "best_ask": 100.01, "spread_bps": 2.0}
            for symbol in self.contracts
        }

    def open_positions(self):
        return {"data": self.positions}

    def pending_orders(self):
        return {"data": {"entrustedList": self.pending}}

    def protection_orders(self, symbol=None):
        if symbol in self.fail_plan_symbols:
            raise RuntimeError("plan read unavailable")
        return {"data": self.protections}

    def trigger_orders(self, plan_type):
        return {"data": {"entrustedList": self.triggers[plan_type]}}

    def contract_metadata(self, symbol):
        return {"data": [self.contracts[symbol]]}

    def symbol_account(self, symbol):
        return {"data": self.accounts[symbol]}

    def symbol_price(self, symbol):
        return {"data": [self.prices[symbol]]}

    def orderbook(self, symbol):
        return self.books[symbol]

    def local_order_intents(self):
        return self.intents

    def state_quarantine_count(self):
        return self.quarantine_count


def test_flat_exchange_with_complete_metadata_passes_read_only_attestation():
    result = attest_exchange(
        _ExchangeAdapter(), symbols=OWNER_SYMBOLS, required_leverage=3
    )

    assert result["deployment_gate"] == "PASS"
    assert result["read_only"] is True
    assert result["open_positions"] == []
    assert result["pending_entries"] == []
    assert result["orphan_stop_orders"] == []
    assert result["orphan_take_profit_orders"] == []
    assert result["account_checks"]["flat"] is True
    assert result["all_symbols_approved"] is True
    assert all(row["attested"] for row in result["contracts"])
    assert result["contracts"][0]["minimum_notional"] == 5.0
    assert result["contracts"][0]["tick_size"] == 0.01
    assert result["contracts"][0]["size_multiplier"] == 0.001


def test_positions_pending_entries_and_orphan_protection_block_attestation():
    adapter = _ExchangeAdapter()
    adapter.positions = [{
        "symbol": "BTCUSDT", "holdSide": "long", "total": "0.1"
    }]
    adapter.pending = [{
        "symbol": "SOLUSDT", "orderId": "entry-1", "tradeSide": "open",
        "reduceOnly": "NO",
    }]
    adapter.protections = [
        {"symbol": "BTCUSDT", "holdSide": "long", "planType": "loss_plan",
         "planOrderId": "btc-sl", "triggerPrice": "62000"},
        {"symbol": "SOLUSDT", "holdSide": "short", "planType": "profit_plan",
         "planOrderId": "orphan-tp", "triggerPrice": "70"},
    ]

    result = attest_exchange(
        adapter, symbols=OWNER_SYMBOLS, required_leverage=3
    )

    assert result["deployment_gate"] == "FAIL"
    assert result["open_positions"][0]["symbol"] == "BTCUSDT"
    assert result["pending_entries"][0]["order_id"] == "entry-1"
    assert result["active_stop_orders"][0]["order_id"] == "btc-sl"
    assert result["orphan_take_profit_orders"][0]["order_id"] == "orphan-tp"


def test_flat_account_rejects_orphan_sl_tp_and_reduce_only_open_order():
    adapter = _ExchangeAdapter()
    adapter.pending = [{
        "symbol": "BTCUSDT",
        "orderId": "stale-close",
        "tradeSide": "close",
        "reduceOnly": "YES",
    }]
    adapter.protections = [
        {
            "symbol": "BTCUSDT",
            "holdSide": "long",
            "planType": "loss_plan",
            "planOrderId": "orphan-sl",
            "triggerPrice": "90",
        },
        {
            "symbol": "SOLUSDT",
            "holdSide": "short",
            "planType": "profit_plan",
            "planOrderId": "orphan-tp",
            "triggerPrice": "90",
        },
    ]

    result = attest_exchange(
        adapter, symbols=OWNER_SYMBOLS, required_leverage=3
    )

    assert result["deployment_gate"] == "FAIL"
    assert result["account_checks"]["no_open_orders"] is False
    assert result["account_checks"]["no_pending_entries"] is True
    assert [row["order_id"] for row in result["orphan_stop_orders"]] == ["orphan-sl"]
    assert [row["order_id"] for row in result["orphan_take_profit_orders"]] == ["orphan-tp"]


def test_pending_normal_or_trailing_trigger_order_blocks_deployment():
    for plan_type in ("normal_plan", "track_plan"):
        adapter = _ExchangeAdapter()
        adapter.triggers[plan_type] = [{
            "symbol": "BTCUSDT",
            "orderId": f"pending-{plan_type}",
            "tradeSide": "open",
        }]

        result = attest_exchange(
            adapter, symbols=OWNER_SYMBOLS, required_leverage=3
        )

        assert result["deployment_gate"] == "FAIL", plan_type
        assert result["account_checks"]["no_pending_trigger_orders"] is False
        assert result["account_checks"]["no_open_orders"] is False
        assert result["pending_trigger_orders"][0]["plan_type"] == plan_type


def test_unverified_isolated_support_or_insufficient_leverage_blocks():
    adapter = _ExchangeAdapter()
    adapter.accounts["BTCUSDT"] = {"marginMode": "crossed", "leverage": "3"}
    adapter.contracts["SOLUSDT"]["maxLever"] = "2"

    result = attest_exchange(
        adapter, symbols=OWNER_SYMBOLS, required_leverage=3
    )

    assert result["deployment_gate"] == "FAIL"
    by_symbol = {row["symbol"]: row for row in result["contracts"]}
    assert by_symbol["BTCUSDT"]["isolated_support"] is None
    assert by_symbol["SOLUSDT"]["required_leverage_supported"] is False


def test_both_isolated_long_and_short_leverage_must_match():
    adapter = _ExchangeAdapter()
    adapter.accounts["BTCUSDT"]["isolatedShortLever"] = "5"

    result = attest_exchange(
        adapter, symbols=OWNER_SYMBOLS, required_leverage=3
    )

    btc = {row["symbol"]: row for row in result["contracts"]}["BTCUSDT"]
    assert result["deployment_gate"] == "FAIL"
    assert btc["current_long_leverage"] == 3.0
    assert btc["current_short_leverage"] == 5.0
    assert btc["required_leverage_configured"] is False


def test_inactive_symbol_is_blocked_even_when_all_metadata_is_complete():
    adapter = _ExchangeAdapter()
    adapter.contracts["BTCUSDT"]["symbolStatus"] = "off"

    result = attest_exchange(
        adapter, symbols=OWNER_SYMBOLS, required_leverage=3
    )

    btc = {row["symbol"]: row for row in result["contracts"]}["BTCUSDT"]
    assert result["deployment_gate"] == "FAIL"
    assert btc["symbol_active"] is False
    assert btc["classification"] == "BLOCKED"
    assert "symbol_inactive" in btc["reasons"]


def test_missing_mark_orderbook_or_plan_read_capability_blocks_symbol():
    scenarios = ("mark", "orderbook", "plan")
    for scenario in scenarios:
        adapter = _ExchangeAdapter()
        if scenario == "mark":
            adapter.prices["BTCUSDT"]["markPrice"] = ""
        elif scenario == "orderbook":
            adapter.books["BTCUSDT"] = {
                "best_bid": 0,
                "best_ask": 0,
                "spread_bps": 0,
            }
        else:
            adapter.fail_plan_symbols.add("BTCUSDT")

        result = attest_exchange(
            adapter, symbols=OWNER_SYMBOLS, required_leverage=3
        )

        btc = {row["symbol"]: row for row in result["contracts"]}["BTCUSDT"]
        assert result["deployment_gate"] == "FAIL", scenario
        assert btc["classification"] == "BLOCKED", scenario


def test_each_required_contract_minimum_field_fails_closed():
    for field in ("minTradeNum", "minTradeUSDT", "sizeMultiplier", "pricePlace", "volumePlace", "maxLever"):
        adapter = _ExchangeAdapter()
        adapter.contracts["BTCUSDT"].pop(field)

        result = attest_exchange(
            adapter, symbols=OWNER_SYMBOLS, required_leverage=3
        )

        btc = {row["symbol"]: row for row in result["contracts"]}["BTCUSDT"]
        assert result["deployment_gate"] == "FAIL", field
        assert btc["classification"] == "BLOCKED", field


def test_wide_spread_is_conditional_and_cannot_pass_deployment():
    adapter = _ExchangeAdapter()
    adapter.books["SOLUSDT"]["spread_bps"] = 7.5

    result = attest_exchange(
        adapter, symbols=OWNER_SYMBOLS, required_leverage=3
    )

    sol = {row["symbol"]: row for row in result["contracts"]}["SOLUSDT"]
    assert result["deployment_gate"] == "FAIL"
    assert sol["classification"] == "CONDITIONAL"
    assert sol["quarantined"] is False


def test_unresolved_intent_or_quarantine_artifact_blocks_flat_account():
    adapter = _ExchangeAdapter()
    adapter.intents = [{"symbol": "BTCUSDT", "state": "AMBIGUOUS"}]
    adapter.quarantine_count = 1

    result = attest_exchange(
        adapter, symbols=OWNER_SYMBOLS, required_leverage=3
    )

    assert result["deployment_gate"] == "FAIL"
    assert result["account_checks"]["no_unresolved_local_order_intents"] is False
    assert result["account_checks"]["no_state_quarantine_artifacts"] is False
    assert result["unresolved_local_order_intents"] == [
        {"symbol": "BTCUSDT", "state": "AMBIGUOUS"}
    ]


def test_all_owner_allowlist_symbols_must_be_individually_approved():
    symbols = OWNER_SYMBOLS
    result = attest_exchange(
        _ExchangeAdapter(symbols), symbols=symbols, required_leverage=3
    )

    assert result["deployment_gate"] == "PASS"
    assert result["allowlist_count"] == len(OWNER_SYMBOLS)
    assert [row["symbol"] for row in result["contracts"]] == list(symbols)
    assert {row["classification"] for row in result["contracts"]} == {"APPROVED"}


def test_exchange_gate_rejects_any_non_owner_allowlist_even_if_symbols_are_healthy():
    symbols = ("BTCUSDT", "SOLUSDT")
    result = attest_exchange(
        _ExchangeAdapter(symbols), symbols=symbols, required_leverage=3
    )

    assert result["deployment_gate"] == "FAIL"
    assert result["owner_allowlist_match"] is False
    assert result["all_symbols_approved"] is False


def test_exchange_adapter_has_only_get_capabilities():
    source = inspect.getsource(BitgetReadOnlyAttestationAdapter)
    assert READ_ONLY_CLIENT_METHODS
    assert all(name.startswith("get_") for name in READ_ONLY_CLIENT_METHODS)
    for forbidden in (
        "place_", "cancel_", "close_", "set_futures_leverage", "emergency_flatten"
    ):
        assert forbidden not in source


def test_local_intent_reader_verifies_wrapped_state_checksum(tmp_path):
    path = tmp_path / "order-intents-fixture"
    rows = [{"symbol": "BTCUSDT", "state": "PROTECTED"}]
    checksum = hashlib.sha256(
        json.dumps(rows, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    path.write_text(
        json.dumps({"_state_metadata": {"checksum": checksum}, "data": rows}),
        encoding="utf-8",
    )
    adapter = BitgetReadOnlyAttestationAdapter(
        SimpleNamespace(),
        product_type="USDT-FUTURES",
        intent_path=path,
    )

    assert adapter.local_order_intents() == rows

    path.write_text(
        json.dumps({"_state_metadata": {"checksum": "bad"}, "data": rows}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="checksum mismatch"):
        adapter.local_order_intents()


def test_symbol_account_endpoint_is_get_only():
    class Client(BitgetAccountClientMixin):
        def __init__(self):
            self.settings = SimpleNamespace(bitget_product_type="USDT-FUTURES")
            self.calls = []

        def _request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            return {"data": {}}

    client = Client()
    client.get_symbol_account("BTCUSDT")
    method, path, kwargs = client.calls[0]
    assert method == "GET"
    assert path == "/api/v2/mix/account/account"
    assert kwargs["private"] is True


def test_symbol_price_endpoint_is_public_get_only():
    class Client(BitgetMarketClientMixin):
        def __init__(self):
            self.settings = SimpleNamespace(bitget_product_type="USDT-FUTURES")
            self.calls = []

        def _request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            return {"data": []}

    client = Client()
    client.get_symbol_price("BTCUSDT")
    method, path, kwargs = client.calls[0]
    assert method == "GET"
    assert path == "/api/v2/mix/market/symbol-price"
    assert kwargs["private"] is False


def _config_text() -> str:
    return "\n".join([
        "APP_ENV=production",
        "EXECUTION_ENABLED=true",
        "EXECUTION_MODE=LIVE",
        "EXECUTION_MARGIN_MODE=isolated",
        "DEFAULT_LEVERAGE=3",
        "MAX_LEVERAGE=3",
        "ACCOUNT_RISK_PER_TRADE_PCT=0.75",
        "EXECUTION_MAX_LIVE_NOTIONAL_PER_TRADE_USDT=35",
        "MAX_OPEN_POSITIONS=2",
        "EXECUTION_MAX_PER_CYCLE=2",
        f"PRODUCTION_SYMBOL_ALLOWLIST={','.join(OWNER_SYMBOLS)}",
        "MAX_SYMBOLS=12",
        "ALLOW_AUTO_WATCHLIST_REFRESH=false",
        "EXECUTION_REQUIRE_CONFIRMATION=true",
        "BREAK_EVEN_OPEN_FEE_FALLBACK_RATE=0.0006",
        "BREAK_EVEN_EXPECTED_CLOSE_FEE_RATE=0.0006",
        "BREAK_EVEN_SPREAD_BUFFER_PCT=0.02",
        "BREAK_EVEN_SLIPPAGE_BUFFER_PCT=0.03",
        "BREAK_EVEN_EXTRA_BUFFER_PCT=0.01",
        "BREAK_EVEN_FEE_BUFFER_PCT=0.12",
        "BREAK_EVEN_MARK_SAFETY_TICKS=2",
        "EXECUTOR_ID=runner01",
        "HOST_ID=runner-mba01",
        "STRATEGY_ISOLATION_ENABLED=true",
        "ENABLED_STRATEGIES=microflow_scalper_v1",
        "MICROFLOW_SCALPER_ENABLED=true",
        f"MICROFLOW_SYMBOLS={','.join(OWNER_SYMBOLS)}",
        "MICROFLOW_LEVERAGE=3",
        "MICROFLOW_MAX_SLIPPAGE_BPS=1",
        "OLD_STRATEGIES_NEW_ENTRIES_ENABLED=false",
        "DYNAMIC_GRID_ENABLED=false",
        "DYNAMIC_GRID_MODE=OFF",
        "MAKER_ENTRY_FALLBACK_MARKET=false",
        "BITGET_API_SECRET=fake-never-print-this-value",
        "DASHBOARD_PASSWORD=fake-dashboard-value",
        "",
    ])


def test_config_attestation_validates_safe_values_and_never_returns_secrets(tmp_path):
    path = tmp_path / "deployment-config-fixture"
    content = _config_text()
    path.write_text(content, encoding="utf-8")
    checksum = hashlib.sha256(content.encode()).hexdigest()
    expected = ConfigExpectations(
        checksum_sha256=checksum,
        release_sha="a" * 40,
        default_leverage=3,
        max_leverage=3,
        risk_per_trade_pct=0.75,
        notional_cap_usdt=35,
        symbols=OWNER_SYMBOLS,
        break_even_open_fee_fallback_rate=0.0006,
        break_even_expected_close_fee_rate=0.0006,
        break_even_spread_buffer_pct=0.02,
        break_even_slippage_buffer_pct=0.03,
        break_even_extra_buffer_pct=0.01,
        break_even_fee_buffer_pct=0.12,
        break_even_mark_safety_ticks=2,
    )

    result = attest_config_file(path, expected, actual_release_sha="a" * 40)
    rendered = json.dumps(result)

    assert result["deployment_gate"] == "PASS"
    assert result["checksum_sha256"] == checksum
    assert result["portfolio"]["max_open_positions"] == 2
    assert result["allowlist"] == list(OWNER_SYMBOLS)
    assert result["allowlist_count"] == len(OWNER_SYMBOLS)
    assert result["secrets_redacted"] is True
    assert result["redacted_key_count"] == 2
    assert result["comparisons"]["full_settings_schema"] is True
    example = result["break_even"]["semantic_example"]
    assert example["semantic"] == "BE_PLUS_FEES"
    assert example["before_legacy"] == {
        "long_target": 100.12,
        "short_target": 99.88,
    }
    assert example["after_itemised"]["long_target"] == 100.19
    assert example["after_itemised"]["short_target"] == 99.82
    assert example["cost_covering"] is True
    assert "fake-never-print-this-value" not in rendered
    assert "fake-dashboard-value" not in rendered


def test_config_attestation_blocks_checksum_allowlist_and_risk_drift(tmp_path):
    path = tmp_path / "deployment-config-fixture"
    path.write_text(_config_text(), encoding="utf-8")
    expected = ConfigExpectations(
        checksum_sha256="0" * 64,
        release_sha="a" * 40,
        default_leverage=3,
        max_leverage=3,
        risk_per_trade_pct=0.5,
        notional_cap_usdt=35,
        symbols=("BTCUSDT",),
        break_even_open_fee_fallback_rate=0.0006,
        break_even_expected_close_fee_rate=0.0006,
        break_even_spread_buffer_pct=0.02,
        break_even_slippage_buffer_pct=0.03,
        break_even_extra_buffer_pct=0.01,
        break_even_fee_buffer_pct=0.12,
        break_even_mark_safety_ticks=2,
    )

    result = attest_config_file(path, expected, actual_release_sha="a" * 40)

    assert result["deployment_gate"] == "FAIL"
    assert "expectation_mismatch:checksum_sha256" in result["errors"]
    assert "expectation_mismatch:risk_per_trade_pct" in result["errors"]
    assert "expectation_mismatch:symbols" in result["errors"]


def _expected_config(content: str) -> ConfigExpectations:
    return ConfigExpectations(
        checksum_sha256=hashlib.sha256(content.encode()).hexdigest(),
        release_sha="b" * 40,
        default_leverage=3,
        max_leverage=3,
        risk_per_trade_pct=0.75,
        notional_cap_usdt=35,
        symbols=OWNER_SYMBOLS,
        break_even_open_fee_fallback_rate=0.0006,
        break_even_expected_close_fee_rate=0.0006,
        break_even_spread_buffer_pct=0.02,
        break_even_slippage_buffer_pct=0.03,
        break_even_extra_buffer_pct=0.01,
        break_even_fee_buffer_pct=0.12,
        break_even_mark_safety_ticks=2,
    )


def test_config_attestation_binds_to_exact_release_sha(tmp_path):
    content = _config_text()
    path = tmp_path / "deployment-config-fixture"
    path.write_text(content, encoding="utf-8")

    result = attest_config_file(
        path,
        _expected_config(content),
        actual_release_sha="c" * 40,
    )

    assert result["deployment_gate"] == "FAIL"
    assert result["release_sha"] == "c" * 40
    assert "expectation_mismatch:release_sha" in result["errors"]


def test_config_attestation_blocks_btc_only_legacy_override(tmp_path):
    content = _config_text() + "WATCHLIST=BTCUSDT\n"
    path = tmp_path / "deployment-config-fixture"
    path.write_text(content, encoding="utf-8")

    result = attest_config_file(
        path,
        _expected_config(content),
        actual_release_sha="b" * 40,
    )

    assert result["deployment_gate"] == "FAIL"
    assert result["comparisons"]["btc_only_override_absent"] is False


def test_each_required_live_and_break_even_field_fails_closed_when_missing(tmp_path):
    required = (
        "APP_ENV",
        "EXECUTION_ENABLED",
        "EXECUTION_MODE",
        "EXECUTION_MARGIN_MODE",
        "DEFAULT_LEVERAGE",
        "MAX_LEVERAGE",
        "ACCOUNT_RISK_PER_TRADE_PCT",
        "EXECUTION_MAX_LIVE_NOTIONAL_PER_TRADE_USDT",
        "MAX_OPEN_POSITIONS",
        "EXECUTION_MAX_PER_CYCLE",
        "PRODUCTION_SYMBOL_ALLOWLIST",
        "MAX_SYMBOLS",
        "ALLOW_AUTO_WATCHLIST_REFRESH",
        "EXECUTION_REQUIRE_CONFIRMATION",
        "BREAK_EVEN_OPEN_FEE_FALLBACK_RATE",
        "BREAK_EVEN_EXPECTED_CLOSE_FEE_RATE",
        "BREAK_EVEN_SPREAD_BUFFER_PCT",
        "BREAK_EVEN_SLIPPAGE_BUFFER_PCT",
        "BREAK_EVEN_EXTRA_BUFFER_PCT",
        "BREAK_EVEN_FEE_BUFFER_PCT",
        "BREAK_EVEN_MARK_SAFETY_TICKS",
    )
    lines = _config_text().splitlines()
    for key in required:
        content = "\n".join(
            line for line in lines if not line.startswith(f"{key}=")
        ) + "\n"
        path = tmp_path / f"fixture-{key.lower()}"
        path.write_text(content, encoding="utf-8")

        result = attest_config_file(
            path,
            _expected_config(content),
            actual_release_sha="b" * 40,
        )

        assert result["deployment_gate"] == "FAIL", key
        if key.startswith("BREAK_EVEN_") or key == "EXECUTION_MARGIN_MODE":
            assert result["comparisons"]["full_settings_schema"] is False, key


def test_invalid_value_fails_full_schema_without_serializing_input(tmp_path):
    content = _config_text().replace("DEFAULT_LEVERAGE=3", "DEFAULT_LEVERAGE=private-invalid")
    path = tmp_path / "deployment-config-fixture"
    path.write_text(content, encoding="utf-8")

    result = attest_config_file(
        path,
        _expected_config(content),
        actual_release_sha="b" * 40,
    )

    rendered = json.dumps(result)
    assert result["deployment_gate"] == "FAIL"
    assert result["comparisons"]["full_settings_schema"] is False
    assert "private-invalid" not in rendered


def test_invalid_break_even_rates_cannot_be_approved_as_expected_values(tmp_path):
    content = _config_text().replace(
        "BREAK_EVEN_SPREAD_BUFFER_PCT=0.02",
        "BREAK_EVEN_SPREAD_BUFFER_PCT=-0.02",
    )
    path = tmp_path / "deployment-config-fixture"
    path.write_text(content, encoding="utf-8")
    expected = _expected_config(content)
    expected = replace(expected, break_even_spread_buffer_pct=-0.02)

    result = attest_config_file(
        path,
        expected,
        actual_release_sha="b" * 40,
    )

    assert result["deployment_gate"] == "FAIL"
    assert result["comparisons"]["break_even_spread_buffer_pct"] is True
    assert result["comparisons"]["break_even_rates_valid"] is False
