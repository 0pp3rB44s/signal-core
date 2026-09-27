from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings
from app.runner import StartupRunner
from clients.bitget_base_client import PrivateExchangeCallBlocked
from clients.bitget_public_client import BitgetPublicClient
from clients.bitget_rest import BitgetRestClient
from execution.execution_service import ExecutionService
from execution.position_manager import PositionManager
from forward_paper.service import ForwardPaperService
from planning.trade_planner import TradePlanner


def _strict_settings(tmp_path, **overrides) -> Settings:
    values = {
        "FORWARD_PAPER_ONLY": True,
        "BITGET_RATE_LIMIT_STATE_PATH": str(tmp_path / "rate-limit.json"),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_strict_mode_overrides_all_conflicting_private_settings(tmp_path):
    settings = _strict_settings(
        tmp_path,
        EXECUTION_ENABLED=True,
        EXECUTION_MODE="LIVE",
        FORWARD_PAPER_ENABLED=False,
        POSITION_MANAGER_ENABLED=True,
        POSITION_LOOP_ENABLED=True,
        POSITION_SYNC_ON_START=True,
    )

    assert settings.forward_paper_only is True
    assert settings.execution_enabled is False
    assert settings.execution_mode == "DRY_RUN"
    assert settings.forward_paper_enabled is True
    assert settings.position_manager_enabled is False
    assert settings.position_loop_enabled is False
    assert settings.position_sync_on_start is False
    assert settings.is_live_execution is False


@pytest.mark.parametrize(
    ("method_name", "kwargs"),
    [
        ("ping_private_account", {}),
        ("get_accounts", {}),
        ("get_all_positions", {}),
        ("place_futures_market_order", {"symbol": "BTCUSDT", "direction": "LONG", "size": 0.001}),
    ],
)
def test_central_guard_blocks_account_equity_position_and_order_calls(
    tmp_path, method_name, kwargs
):
    client = BitgetRestClient(_strict_settings(tmp_path))

    with patch("clients.bitget_base_client.requests.request") as request:
        with pytest.raises(PrivateExchangeCallBlocked, match="FORWARD_PAPER_ONLY"):
            getattr(client, method_name)(**kwargs)
    # Order preparation may consult the public contract-precision endpoint,
    # but no authenticated account/position/order transport may occur.
    for call in request.call_args_list:
        assert call.kwargs["headers"] == {}
        assert not any(
            private_path in call.kwargs["url"]
            for private_path in ("/account/", "/position/", "/order/place-order")
        )


def test_public_market_transport_remains_available(tmp_path):
    client = BitgetPublicClient(_strict_settings(tmp_path))
    response = MagicMock(status_code=200)
    response.raise_for_status.return_value = None
    response.json.return_value = {"code": "00000", "data": [["1", "1", "2", "0.5", "1.5", "10"]]}

    with patch("clients.bitget_base_client.requests.request", return_value=response) as request:
        payload = client.get_candles("BTCUSDT", "USDT-FUTURES", limit=1)

    assert payload["code"] == "00000"
    assert request.call_count == 1
    assert request.call_args.kwargs["headers"] == {}


def test_strict_runner_initializes_only_public_and_paper_components(tmp_path):
    settings = _strict_settings(tmp_path)

    with (
        patch("app.runner.BitgetRestClient") as private_client,
        patch("app.runner.ExecutionService") as execution_service,
        patch("app.runner.PositionManager") as position_manager,
    ):
        runner = StartupRunner(settings)

    private_client.assert_not_called()
    execution_service.assert_not_called()
    position_manager.assert_not_called()
    assert isinstance(runner.client, BitgetPublicClient)
    assert runner.execution_service is None
    assert runner.position_manager is None
    assert isinstance(runner.trade_planner, TradePlanner)
    assert isinstance(runner.forward_paper, ForwardPaperService)


def test_strict_startup_skips_private_account_probe(tmp_path):
    runner = StartupRunner(_strict_settings(tmp_path))
    runner.fetcher.fetch_contracts = MagicMock(return_value=[])

    runner._startup_checks()

    runner.fetcher.fetch_contracts.assert_called_once_with(force_refresh=True)
    assert runner.client.has_credentials is False
    assert not hasattr(runner.client, "ping_private_account")
    assert not hasattr(runner.client, "get_accounts")
    assert not hasattr(runner.client, "get_all_positions")
    assert not hasattr(runner.client, "place_futures_order")


def test_strict_runtime_logs_explicit_safety_mode(tmp_path, caplog):
    settings = _strict_settings(
        tmp_path,
        SCAN_ON_START=False,
        SCAN_LOOP_ENABLED=False,
    )
    runner = StartupRunner(settings)
    runner._startup_checks = MagicMock()

    with caplog.at_level(logging.WARNING):
        runner.run()

    assert "FORWARD_PAPER_ONLY ACTIVE" in caplog.text
    assert "PRIVATE EXCHANGE CALLS DISABLED" in caplog.text
    runner._startup_checks.assert_called_once_with()


def test_normal_runtime_construction_is_unchanged_outside_strict_mode(tmp_path):
    settings = Settings(
        _env_file=None,
        FORWARD_PAPER_ONLY=False,
        BITGET_RATE_LIMIT_STATE_PATH=str(tmp_path / "rate-limit.json"),
    )
    runner = StartupRunner(settings)

    assert isinstance(runner.client, BitgetRestClient)
    assert isinstance(runner.execution_service, ExecutionService)
    assert isinstance(runner.position_manager, PositionManager)
