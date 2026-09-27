"""Safety contract for the NON-PRODUCTION forward-paper smoke strategy.

The smoke strategy fabricates executable entries, so the guarantee that matters is
that it cannot exist anywhere near an order-placing runtime.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from strategies.forward_paper_smoke import SMOKE_STRATEGY_NAME, smoke_plan
from tests.test_forward_paper_production_lifecycle import _production_snapshot, _candles


def _settings(**overrides) -> Settings:
    base = {
        "_env_file": None,
        "FORWARD_PAPER_ONLY": True,
        "FORWARD_PAPER_SMOKE_STRATEGY_ENABLED": True,
        "FORWARD_PAPER_SMOKE_SYMBOL": "SOLUSDT",
        "PRODUCTION_SYMBOL_ALLOWLIST": "BTCUSDT",
        "MAX_SYMBOLS": 1,
        "MAX_OPEN_POSITIONS": 2,
        "EXECUTION_MAX_PER_CYCLE": 2,
        "ALLOW_AUTO_WATCHLIST_REFRESH": False,
        "EXECUTION_REQUIRE_CONFIRMATION": True,
    }
    base.update(overrides)
    return Settings(**base)


def test_smoke_strategy_is_disabled_by_default():
    settings = Settings(_env_file=None)
    assert settings.forward_paper_smoke_strategy_enabled is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"FORWARD_PAPER_ONLY": False},
        {"FORWARD_PAPER_ONLY": False, "EXECUTION_ENABLED": True},
        {"FORWARD_PAPER_ONLY": False, "EXECUTION_ENABLED": True, "EXECUTION_MODE": "LIVE"},
        {"FORWARD_PAPER_ONLY": False, "FORWARD_PAPER_ENABLED": False},
    ],
    ids=("not-paper-only", "execution-enabled", "live-execution", "paper-disabled"),
)
def test_smoke_strategy_cannot_activate_outside_strict_forward_paper(overrides):
    settings = _settings(**overrides)
    assert settings.forward_paper_smoke_strategy_enabled is False, (
        "smoke strategy must be force-disabled outside strict forward-paper-only"
    )
    snapshot = _production_snapshot(_candles())
    assert smoke_plan(settings, snapshot) is None


def test_live_execution_stays_impossible_when_smoke_requested():
    settings = _settings(EXECUTION_ENABLED=True, EXECUTION_MODE="LIVE")
    # forward_paper_only wins and strips live execution entirely.
    assert settings.is_live_execution is False
    assert settings.execution_enabled is False


def test_smoke_plan_is_deterministic_executable_and_marked():
    settings = _settings()
    snapshot = _production_snapshot(_candles())
    first = smoke_plan(settings, snapshot)
    second = smoke_plan(settings, snapshot)

    assert first is not None
    assert first.strategy == SMOKE_STRATEGY_NAME
    assert first.verdict == "EXECUTABLE"
    assert "NON_PRODUCTION_SMOKE_STRATEGY" in first.notes
    # Same candle must reproduce the same identity so replays dedupe.
    assert (first.candidate_id, first.plan_id) == (second.candidate_id, second.plan_id)

    fill = float(snapshot.primary.latest_close)
    assert first.entry_prices == [fill], "entry must use the real live price"
    assert first.stop_loss < fill < first.take_profits[0]


def test_smoke_plan_only_fires_for_configured_symbol():
    settings = _settings(FORWARD_PAPER_SMOKE_SYMBOL="BTCUSDT")
    snapshot = _production_snapshot(_candles())  # SOLUSDT
    assert smoke_plan(settings, snapshot) is None
