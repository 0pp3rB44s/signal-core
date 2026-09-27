"""LSRL_V1 is OFF by default and can never reach production LIVE from this change.

Covers: Settings defaults + validators, the production-LIVE strategy allow-list (Python validator AND the
real shell launch guard), the runner hook (default OFF, fail-quiet for observational modes, fail-loud for
MICRO_LIVE) and the fact that no LSRL flag can change what counts as live execution.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.runner import StartupRunner
from tests.test_launch_guard_leverage import run_guard
from tests.test_microflow_leverage_validator import _live


def strict(tmp_path, **overrides) -> Settings:
    values = {"FORWARD_PAPER_ONLY": True, "BITGET_RATE_LIMIT_STATE_PATH": str(tmp_path / "rate-limit.json"),
              "LSRL_DATA_DIR": str(tmp_path / "lsrl")}
    values.update(overrides)
    return Settings(_env_file=None, **values)


# ------------------------------------------------------------------ defaults
def test_every_lsrl_switch_defaults_to_off(tmp_path):
    s = strict(tmp_path)
    assert s.lsrl_enabled is False and s.lsrl_mode == "OFF" and s.lsrl_microlive_armed is False
    assert s.lsrl_symbol == "SOLUSDT" and s.lsrl_shadow_nav_usdt == 100.0


def test_no_lsrl_flag_can_make_the_process_live_capable(tmp_path):
    s = strict(tmp_path, LSRL_ENABLED=True, LSRL_MODE="SHADOW")
    assert s.is_live_execution is False and s.execution_enabled is False and s.execution_mode == "DRY_RUN"
    p = strict(tmp_path, LSRL_ENABLED=True, LSRL_MODE="PAPER")
    assert p.is_live_execution is False


# ------------------------------------------------------------------ validators
@pytest.mark.parametrize("overrides, message", [
    ({"LSRL_MODE": "SHADOW"}, "requires LSRL_ENABLED"),                                   # a mode without the master switch
    ({"LSRL_ENABLED": True, "LSRL_MODE": "OFF"}, None),                          # enabled+OFF is inert, allowed
    ({"LSRL_ENABLED": True, "LSRL_MODE": "GAMBLE"}, "LSRL_MODE must be"),
    ({"LSRL_ENABLED": True, "LSRL_MODE": "BACKTEST"}, "LSRL_MODE must be"),              # backtest is replay-only
    ({"LSRL_ENABLED": True, "LSRL_MODE": "SHADOW", "LSRL_SYMBOL": "BTCUSDT"}, "frozen to SOLUSDT"),
    ({"LSRL_ENABLED": True, "LSRL_MODE": "SHADOW", "LSRL_SHADOW_NAV_USDT": 0}, "must be positive"),
    ({"LSRL_ENABLED": True, "LSRL_MODE": "SHADOW", "LSRL_MIN_FREE_GB": -1}, "must be positive"),
    ({"LSRL_ENABLED": True, "LSRL_MODE": "SHADOW", "LSRL_MICROLIVE_ARMED": True}, "requires LSRL_MODE=MICRO_LIVE"),
    ({"LSRL_ENABLED": True, "LSRL_MODE": "MICRO_LIVE"}, "global LIVE execution gate"),           # forward-paper-only forbids it
])
def test_settings_validators(tmp_path, overrides, message):
    if message is None:
        strict(tmp_path, **overrides)
        return
    with pytest.raises(ValidationError, match=message):
        strict(tmp_path, **overrides)


def test_micro_live_needs_the_global_live_gate_and_the_arming_flag_and_still_not_production_allowlist():
    base = dict(LSRL_ENABLED=True, LSRL_MODE="MICRO_LIVE")
    with pytest.raises(ValidationError, match="LSRL_MICROLIVE_ARMED"):
        _live(**base)                                              # live gate on, arming flag off
    # arming flag on, but production LIVE only admits microflow / adaptive-trend: lsrl_v1 is refused
    with pytest.raises(ValidationError, match="allowlist"):
        _live(ENABLED_STRATEGIES="lsrl_v1", LSRL_MICROLIVE_ARMED=True, **base)


# ------------------------------------------------------------------ production allow-list is unchanged
@pytest.mark.parametrize("strategies", ["lsrl_v1", "lsrl_v1,microflow_scalper_v1", "microflow_scalper_v1,lsrl_v1"])
def test_python_validator_refuses_lsrl_in_production_live(strategies):
    with pytest.raises(ValidationError):
        _live(ENABLED_STRATEGIES=strategies)


def test_python_validator_still_accepts_the_approved_microflow_config():
    assert _live().enabled_strategy_set == {"microflow_scalper_v1"}


LEVERAGE = {"MICROFLOW_LEVERAGE": "10", "MAX_LEVERAGE": "10"}      # a fully valid config apart from the strategy set


def test_the_real_shell_launch_guard_still_passes_the_approved_config():
    assert run_guard(**LEVERAGE).returncode == 0


@pytest.mark.parametrize("strategies", ["lsrl_v1", "lsrl_v1,microflow_scalper_v1", "microflow_scalper_v1,lsrl_v1"])
def test_the_real_shell_launch_guard_refuses_lsrl_in_production_live_for_the_right_reason(strategies):
    result = run_guard(ENABLED_STRATEGIES=strategies, **LEVERAGE)
    assert result.returncode == 90
    assert "production LIVE enables only microflow_scalper_v1 or adaptive_trend_tsmom_v1" in result.stderr


def test_lsrl_flags_in_the_environment_do_not_change_the_guard_outcome():
    assert run_guard(LSRL_ENABLED="true", LSRL_MODE="SHADOW", **LEVERAGE).returncode == 0


# ------------------------------------------------------------------ runner hook
def test_runner_builds_nothing_by_default(tmp_path):
    runner = StartupRunner(strict(tmp_path))
    assert runner.lsrl is None


def test_runner_default_off_does_not_even_import_the_runtime_module(tmp_path):
    import importlib
    import sys
    sys.modules.pop("lsrl.runtime", None)
    StartupRunner(strict(tmp_path))
    assert "lsrl.runtime" not in sys.modules
    importlib.invalidate_caches()


def test_runner_builds_a_shadow_runtime_when_enabled(tmp_path):
    fake_rt = SimpleNamespace(mode=SimpleNamespace(value="SHADOW"), start=MagicMock())
    with patch("lsrl.runtime.build_runtime", return_value=fake_rt) as build:
        runner = StartupRunner(strict(tmp_path, LSRL_ENABLED=True, LSRL_MODE="SHADOW"))
    assert runner.lsrl is fake_rt
    kwargs = build.call_args.kwargs
    assert kwargs["execution_service"] is None and kwargs["risk_manager"] is runner.risk_manager
    assert kwargs["client"] is runner.client


def test_a_failed_shadow_build_is_logged_and_does_not_take_the_engine_down(tmp_path, caplog):
    with patch("lsrl.runtime.build_runtime", side_effect=RuntimeError("CONTRACT_METADATA_UNAVAILABLE")):
        with caplog.at_level(logging.CRITICAL):
            runner = StartupRunner(strict(tmp_path, LSRL_ENABLED=True, LSRL_MODE="PAPER"))
    assert runner.lsrl is None
    assert "LSRL_START_FAILED | mode=PAPER" in caplog.text


def test_run_starts_the_lsrl_runtime_after_startup_checks(tmp_path):
    runner = StartupRunner(strict(tmp_path, SCAN_ON_START=False, SCAN_LOOP_ENABLED=False))
    runner._startup_checks = MagicMock()
    runner.lsrl = SimpleNamespace(mode=SimpleNamespace(value="SHADOW"), start=MagicMock())
    with patch("app.runner.time.sleep", side_effect=KeyboardInterrupt):    # leave the keep-alive loop
        with pytest.raises(KeyboardInterrupt):
            runner.run()
    runner._startup_checks.assert_called_once_with()
    runner.lsrl.start.assert_called_once_with()


def test_micro_live_start_is_blocked_by_the_close_recovery_gate(tmp_path):
    runner = StartupRunner(strict(tmp_path, SCAN_ON_START=False, SCAN_LOOP_ENABLED=False))
    runner._startup_checks = MagicMock()
    runner.lsrl = SimpleNamespace(mode=SimpleNamespace(value="MICRO_LIVE"), start=MagicMock())
    runner._ensure_startup_close_recovery = MagicMock(return_value=False)
    with pytest.raises(RuntimeError, match="close-recovery gate"):
        runner.run()
    runner.lsrl.start.assert_not_called()


def test_micro_live_build_failure_is_fatal_not_swallowed(tmp_path):
    """Observational modes may degrade quietly; a half-armed live executor must not."""
    live = SimpleNamespace(lsrl_enabled=True, lsrl_mode="MICRO_LIVE")
    runner = StartupRunner.__new__(StartupRunner)
    runner.settings, runner.client, runner.execution_service = live, object(), object()
    runner.risk_manager, runner.log = object(), logging.getLogger("t")
    with patch("lsrl.runtime.build_runtime", side_effect=RuntimeError("not armed")):
        with pytest.raises(RuntimeError, match="not armed"):
            runner._build_lsrl()


def test_the_default_runner_still_has_no_microflow_or_lsrl_side_effects(tmp_path):
    runner = StartupRunner(strict(tmp_path))
    assert runner.lsrl is None and runner.microflow_runtime is None


def test_a_failed_shadow_start_is_logged_and_does_not_take_the_engine_down(tmp_path, caplog):
    runner = StartupRunner(strict(tmp_path, SCAN_ON_START=False, SCAN_LOOP_ENABLED=False))
    runner._startup_checks = MagicMock()
    boom = MagicMock(side_effect=ValueError("engine snapshot does not match this frozen configuration"))
    runner.lsrl = SimpleNamespace(mode=SimpleNamespace(value="SHADOW"), start=boom)
    with caplog.at_level(logging.CRITICAL):
        runner.run()                                                    # returns normally: the engine was NOT taken down
    assert "LSRL_START_FAILED | mode=SHADOW" in caplog.text and runner.lsrl is None
    boom.assert_called_once_with()


def test_a_failed_micro_live_start_is_fatal(tmp_path):
    runner = StartupRunner(strict(tmp_path, SCAN_ON_START=False, SCAN_LOOP_ENABLED=False))
    runner._startup_checks = MagicMock()
    runner._ensure_startup_close_recovery = MagicMock(return_value=True)
    runner.lsrl = SimpleNamespace(mode=SimpleNamespace(value="MICRO_LIVE"), start=MagicMock(side_effect=ValueError("x")))
    with pytest.raises(ValueError):
        runner.run()
