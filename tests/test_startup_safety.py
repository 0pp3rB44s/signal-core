from __future__ import annotations

from unittest.mock import MagicMock

from app.config import Settings
from execution.execution_service import ExecutionService


def test_default_settings_are_observe_only_and_local_dashboard():
    settings = Settings(_env_file=None)

    assert settings.execution_enabled is False
    assert settings.execution_mode == "DRY_RUN"
    assert settings.is_live_execution is False
    assert settings.dashboard_host == "127.0.0.1"
    assert settings.position_loop_enabled is True
    assert settings.break_even_fee_buffer_pct >= settings.planner_estimated_roundtrip_fee_bps / 100.0


def test_disabled_execution_never_queries_exchange_or_places_orders():
    settings = Settings(_env_file=None)
    service = ExecutionService(settings=settings)
    service.client = MagicMock()

    assert service.execute([]) == []
    service.client.get_all_positions.assert_not_called()
    service.client.place_futures_order.assert_not_called()
