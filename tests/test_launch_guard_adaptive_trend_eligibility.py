"""scripts/lib/env_guard.sh: production LIVE now accepts adaptive_trend_tsmom_v1
as ENABLED_STRATEGIES, but only when ADAPTIVE_TREND_LIVE_ENTRY_ENABLED=true --
mirroring the same coupling enforced one layer down in
ExecutionService.execute()'s HYBRID SAFE MODE gate (see
tests/test_hybrid_gate_adaptive_trend_eligibility.py).

Drives the real guard_assert_live_mode through bash, same pattern as
tests/test_launch_guard_leverage.py, so a change to the shell is what is
actually tested -- not a Python reimplementation of its logic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GUARD = REPO / "scripts" / "lib" / "env_guard.sh"

MICROFLOW_BASE_ENV = {
    "APP_ENV": "production",
    "APP_MODE": "live",
    "EXECUTION_ENABLED": "true",
    "EXECUTION_MODE": "LIVE",
    "EXECUTION_REQUIRE_CONFIRMATION": "true",
    "EXECUTION_CONFIRM_SYMBOLS": "BTCUSDT",
    "FORWARD_PAPER_ONLY": "false",
    "STRATEGY_ISOLATION_ENABLED": "true",
    "ENABLED_STRATEGIES": "microflow_scalper_v1",
    "MICROFLOW_SCALPER_ENABLED": "true",
    "OLD_STRATEGIES_NEW_ENTRIES_ENABLED": "false",
    "DYNAMIC_GRID_ENABLED": "false",
    "DYNAMIC_GRID_MODE": "OFF",
    "MICROFLOW_SYMBOLS": "BTCUSDT,ETHUSDT",
    "PRODUCTION_SYMBOL_ALLOWLIST": "BTCUSDT,ETHUSDT",
    "MICROFLOW_MAX_SLIPPAGE_BPS": "1",
    "MICROFLOW_LEVERAGE": "10",
    "MAX_LEVERAGE": "10",
    "BITGET_API_KEY": "test-key-not-real",
    "BITGET_API_SECRET": "test-secret-not-real",
    "BITGET_API_PASSPHRASE": "test-passphrase-not-real",
}

ADAPTIVE_BASE_ENV = {
    "APP_ENV": "production",
    "APP_MODE": "live",
    "EXECUTION_ENABLED": "true",
    "EXECUTION_MODE": "LIVE",
    "EXECUTION_REQUIRE_CONFIRMATION": "true",
    "EXECUTION_CONFIRM_SYMBOLS": "BTCUSDT",
    "FORWARD_PAPER_ONLY": "false",
    "STRATEGY_ISOLATION_ENABLED": "true",
    "ENABLED_STRATEGIES": "adaptive_trend_tsmom_v1",
    "ADAPTIVE_TREND_LIVE_ENTRY_ENABLED": "true",
    "OLD_STRATEGIES_NEW_ENTRIES_ENABLED": "false",
    "DYNAMIC_GRID_ENABLED": "false",
    "DYNAMIC_GRID_MODE": "OFF",
    "MAX_LEVERAGE": "10",
    "BITGET_API_KEY": "test-key-not-real",
    "BITGET_API_SECRET": "test-secret-not-real",
    "BITGET_API_PASSPHRASE": "test-passphrase-not-real",
}


def run_guard(base: dict, **overrides) -> subprocess.CompletedProcess:
    env = dict(base)
    env.update({k: v for k, v in overrides.items() if v is not None})
    for key, value in list(overrides.items()):
        if value is None:
            env.pop(key, None)
    exports = "\n".join(f"export {k}={v!r}" for k, v in env.items())
    script = f"set -e\n{exports}\nsource {GUARD}\nguard_assert_live_mode\n"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def guard_passes(base: dict, **overrides) -> bool:
    return run_guard(base, **overrides).returncode == 0


# --- 1. AdaptiveTrend accepted only with the flag true --------------------


def test_adaptive_trend_passes_with_flag_true():
    assert guard_passes(ADAPTIVE_BASE_ENV)


def test_adaptive_trend_rejected_with_flag_false():
    result = run_guard(ADAPTIVE_BASE_ENV, ADAPTIVE_TREND_LIVE_ENTRY_ENABLED="false")
    assert result.returncode != 0
    assert "ADAPTIVE_TREND_LIVE_ENTRY_ENABLED=true" in result.stderr


def test_adaptive_trend_rejected_with_flag_missing():
    result = run_guard(ADAPTIVE_BASE_ENV, ADAPTIVE_TREND_LIVE_ENTRY_ENABLED=None)
    assert result.returncode != 0
    assert "ADAPTIVE_TREND_LIVE_ENTRY_ENABLED=true" in result.stderr


# --- 2. MicroFlow's own path is completely unaffected ----------------------


def test_microflow_still_passes_with_its_own_full_invariant_set():
    assert guard_passes(MICROFLOW_BASE_ENV)


def test_microflow_still_rejected_when_its_own_invariants_fail():
    assert not guard_passes(MICROFLOW_BASE_ENV, MICROFLOW_SCALPER_ENABLED="false")
    assert not guard_passes(MICROFLOW_BASE_ENV, MICROFLOW_SYMBOLS="BTCUSDT")


def test_microflow_does_not_require_the_adaptive_trend_flag():
    """MicroFlow's branch must never touch/require ADAPTIVE_TREND_LIVE_ENTRY_ENABLED."""
    assert guard_passes(MICROFLOW_BASE_ENV, ADAPTIVE_TREND_LIVE_ENTRY_ENABLED=None)


# --- 3. unknown strategies remain blocked ----------------------------------


@pytest.mark.parametrize("unknown", ["low_vol_reclaim", "momentum_breakout", "some_new_strategy"])
def test_unknown_strategy_still_rejected(unknown):
    result = run_guard(ADAPTIVE_BASE_ENV, ENABLED_STRATEGIES=unknown)
    assert result.returncode != 0
    assert "enables only microflow_scalper_v1 or adaptive_trend_tsmom_v1" in result.stderr


# --- 4. multiple/unapproved strategy strings remain blocked ----------------


def test_multiple_strategies_still_rejected():
    result = run_guard(ADAPTIVE_BASE_ENV,
                       ENABLED_STRATEGIES="adaptive_trend_tsmom_v1,microflow_scalper_v1")
    assert result.returncode != 0
    assert "enables only microflow_scalper_v1 or adaptive_trend_tsmom_v1" in result.stderr


def test_empty_strategy_still_rejected():
    result = run_guard(ADAPTIVE_BASE_ENV, ENABLED_STRATEGIES="")
    assert result.returncode != 0


# --- neighbouring guards remain untouched for AdaptiveTrend too -----------


def test_strategy_isolation_still_enforced_for_adaptive_trend():
    assert not guard_passes(ADAPTIVE_BASE_ENV, STRATEGY_ISOLATION_ENABLED="false")


def test_dynamic_grid_guards_still_enforced_for_adaptive_trend():
    assert not guard_passes(ADAPTIVE_BASE_ENV, DYNAMIC_GRID_ENABLED="true")
    assert not guard_passes(ADAPTIVE_BASE_ENV, DYNAMIC_GRID_MODE="ON")


def test_legacy_new_entry_paths_still_blocked_for_adaptive_trend():
    assert not guard_passes(ADAPTIVE_BASE_ENV, OLD_STRATEGIES_NEW_ENTRIES_ENABLED="true")


def test_max_leverage_ceiling_still_applies_to_adaptive_trend():
    assert not guard_passes(ADAPTIVE_BASE_ENV, MAX_LEVERAGE="11")


def test_adaptive_trend_does_not_require_microflow_specific_settings():
    """AdaptiveTrend's branch must never require MICROFLOW_SCALPER_ENABLED,
    MICROFLOW_SYMBOLS, or MICROFLOW_LEVERAGE -- those stay MicroFlow-only."""
    assert guard_passes(ADAPTIVE_BASE_ENV, MICROFLOW_SCALPER_ENABLED=None,
                        MICROFLOW_SYMBOLS=None, MICROFLOW_LEVERAGE=None)
