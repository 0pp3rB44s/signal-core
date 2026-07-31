from __future__ import annotations

import hashlib
import inspect
import json
from types import SimpleNamespace

from clients.bitget_account_client import BitgetAccountClientMixin
from deployment.config_attestation import ConfigExpectations, attest_config_file
from deployment.exchange_attestation import (
    BitgetReadOnlyAttestationAdapter,
    READ_ONLY_CLIENT_METHODS,
    attest_exchange,
)


class _ExchangeAdapter:
    def __init__(self):
        self.positions = []
        self.pending = []
        self.protections = []
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
            for symbol in ("BTCUSDT", "SOLUSDT")
        }
        self.accounts = {
            symbol: {"symbol": symbol, "marginMode": "isolated", "leverage": "3"}
            for symbol in self.contracts
        }

    def open_positions(self):
        return {"data": self.positions}

    def pending_orders(self):
        return {"data": {"entrustedList": self.pending}}

    def protection_orders(self):
        return {"data": self.protections}

    def contract_metadata(self, symbol):
        return {"data": [self.contracts[symbol]]}

    def symbol_account(self, symbol):
        return {"data": self.accounts[symbol]}


def test_flat_exchange_with_complete_metadata_passes_read_only_attestation():
    result = attest_exchange(
        _ExchangeAdapter(), symbols=("BTCUSDT", "SOLUSDT"), required_leverage=3
    )

    assert result["deployment_gate"] == "PASS"
    assert result["read_only"] is True
    assert result["open_positions"] == []
    assert result["pending_entries"] == []
    assert result["orphan_protection_orders"] == []
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
        adapter, symbols=("BTCUSDT", "SOLUSDT"), required_leverage=3
    )

    assert result["deployment_gate"] == "BLOCKED"
    assert result["open_positions"][0]["symbol"] == "BTCUSDT"
    assert result["pending_entries"][0]["order_id"] == "entry-1"
    assert result["active_stop_orders"][0]["order_id"] == "btc-sl"
    assert result["orphan_protection_orders"][0]["order_id"] == "orphan-tp"


def test_unverified_isolated_support_or_insufficient_leverage_blocks():
    adapter = _ExchangeAdapter()
    adapter.accounts["BTCUSDT"] = {"marginMode": "crossed", "leverage": "3"}
    adapter.contracts["SOLUSDT"]["maxLever"] = "2"

    result = attest_exchange(
        adapter, symbols=("BTCUSDT", "SOLUSDT"), required_leverage=3
    )

    assert result["deployment_gate"] == "BLOCKED"
    by_symbol = {row["symbol"]: row for row in result["contracts"]}
    assert by_symbol["BTCUSDT"]["isolated_support"] is None
    assert by_symbol["SOLUSDT"]["required_leverage_supported"] is False


def test_exchange_adapter_has_only_get_capabilities():
    source = inspect.getsource(BitgetReadOnlyAttestationAdapter)
    assert READ_ONLY_CLIENT_METHODS
    assert all(name.startswith("get_") for name in READ_ONLY_CLIENT_METHODS)
    for forbidden in (
        "place_", "cancel_", "close_", "set_futures_leverage", "emergency_flatten"
    ):
        assert forbidden not in source


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
        "MAX_OPEN_POSITIONS=1",
        "EXECUTION_MAX_PER_CYCLE=1",
        "PRODUCTION_SYMBOL_ALLOWLIST=BTCUSDT,SOLUSDT",
        "MAX_SYMBOLS=2",
        "ALLOW_AUTO_WATCHLIST_REFRESH=false",
        "EXECUTION_REQUIRE_CONFIRMATION=true",
        "BREAK_EVEN_OPEN_FEE_FALLBACK_RATE=0.0006",
        "BREAK_EVEN_EXPECTED_CLOSE_FEE_RATE=0.0006",
        "BREAK_EVEN_SPREAD_BUFFER_PCT=0.02",
        "BREAK_EVEN_SLIPPAGE_BUFFER_PCT=0.03",
        "BREAK_EVEN_EXTRA_BUFFER_PCT=0.01",
        "BREAK_EVEN_FEE_BUFFER_PCT=0.12",
        "BREAK_EVEN_MARK_SAFETY_TICKS=2",
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
        symbols=("BTCUSDT", "SOLUSDT"),
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
    assert result["portfolio"]["max_open_positions"] == 1
    assert result["allowlist"] == ["BTCUSDT", "SOLUSDT"]
    assert result["allowlist_count"] == 2
    assert result["secrets_redacted"] is True
    assert result["redacted_key_count"] == 2
    assert result["comparisons"]["full_settings_schema"] is True
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
        symbols=("BTCUSDT", "SOLUSDT"),
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
