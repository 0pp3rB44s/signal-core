"""The shell launch guard and the Python validator must agree on the leverage ceiling.

`scripts/lib/env_guard.sh` runs before Python loads, so the two are separate
implementations of one policy. When they disagree, one of two things happens and
both are bad: LIVE refuses an approved configuration, or it accepts an unapproved
one. That is not hypothetical — on 2026-08-14 the guard still carried `<=5` after
the Python validator moved to 10, and the owner's relaunch aborted at the guard
with an approved config.

These tests drive the real `guard_assert_live_mode` through bash rather
than re-implementing its logic, so a change to the shell is what is actually
tested.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from app.config import MICROFLOW_MAX_ALLOWED_LEVERAGE

REPO = Path(__file__).resolve().parents[1]
GUARD = REPO / "scripts" / "lib" / "env_guard.sh"

#: Minimal production-LIVE environment that satisfies every other invariant, so
#: only the leverage rule decides the outcome.
BASE_ENV = {
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
    # Presence-only placeholders. The guard checks that credentials exist, never
    # what they are, so no real value belongs in a test.
    "BITGET_API_KEY": "test-key-not-real",
    "BITGET_API_SECRET": "test-secret-not-real",
    "BITGET_API_PASSPHRASE": "test-passphrase-not-real",
}


def run_guard(**overrides) -> subprocess.CompletedProcess:
    env = dict(BASE_ENV)
    env.update({k: v for k, v in overrides.items() if v is not None})
    for key, value in list(overrides.items()):
        if value is None:
            env.pop(key, None)
    exports = "\n".join(f"export {k}={v!r}" for k, v in env.items())
    script = f"set -e\n{exports}\nsource {GUARD}\nguard_assert_live_mode\n"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def guard_passes(**overrides) -> bool:
    return run_guard(**overrides).returncode == 0


# --- the drift guard itself --------------------------------------------------


def test_shell_ceiling_equals_the_python_ceiling():
    """The whole point: one policy, two implementations, never allowed to diverge."""
    text = GUARD.read_text()
    match = re.search(r"GUARD_MICROFLOW_MAX_LEVERAGE:?=(\d+(?:\.\d+)?)", text)
    assert match, "named leverage ceiling missing from env_guard.sh"
    assert float(match.group(1)) == float(MICROFLOW_MAX_ALLOWED_LEVERAGE)


def test_the_ceiling_is_not_hardcoded_at_the_comparison_sites():
    """Both checks must read the constant, or one can be raised alone."""
    text = GUARD.read_text()
    assert text.count("$GUARD_MICROFLOW_MAX_LEVERAGE") >= 4  # 2 comparisons + 2 messages
    assert "v <= 5 }" not in text, "a literal 5 ceiling survives somewhere"


# --- accepted values ---------------------------------------------------------


@pytest.mark.parametrize("leverage", ["3", "5", "10"])
def test_approved_leverage_passes(leverage):
    assert guard_passes(MICROFLOW_LEVERAGE=leverage, MAX_LEVERAGE=leverage), \
        f"guard rejected approved leverage {leverage}"


# --- rejected values ---------------------------------------------------------


@pytest.mark.parametrize("leverage", ["10.01", "11", "20", "125"])
def test_leverage_above_the_ceiling_fails(leverage):
    result = run_guard(MICROFLOW_LEVERAGE=leverage, MAX_LEVERAGE=leverage)
    assert result.returncode != 0
    assert "must be >0 and <=10" in result.stderr


@pytest.mark.parametrize("leverage", ["0", "-1", "-10"])
def test_zero_or_negative_leverage_fails(leverage):
    assert not guard_passes(MICROFLOW_LEVERAGE=leverage, MAX_LEVERAGE=leverage)


@pytest.mark.parametrize("leverage", ["", "abc", "1e9", "ten"])
def test_invalid_leverage_fails(leverage):
    assert not guard_passes(MICROFLOW_LEVERAGE=leverage, MAX_LEVERAGE="10")


def test_missing_leverage_fails_closed():
    assert not guard_passes(MICROFLOW_LEVERAGE=None, MAX_LEVERAGE="10")
    assert not guard_passes(MICROFLOW_LEVERAGE="10", MAX_LEVERAGE=None)


def test_each_variable_is_checked_independently():
    """One being valid must not carry the other."""
    assert not guard_passes(MICROFLOW_LEVERAGE="10", MAX_LEVERAGE="11")
    assert not guard_passes(MICROFLOW_LEVERAGE="11", MAX_LEVERAGE="10")


# --- neighbouring guards must be untouched -----------------------------------


def test_slippage_cap_still_enforced():
    assert guard_passes(MICROFLOW_LEVERAGE="10", MAX_LEVERAGE="10",
                        MICROFLOW_MAX_SLIPPAGE_BPS="1")
    assert not guard_passes(MICROFLOW_LEVERAGE="10", MAX_LEVERAGE="10",
                            MICROFLOW_MAX_SLIPPAGE_BPS="2")


def test_strategy_isolation_still_enforced():
    assert not guard_passes(MICROFLOW_LEVERAGE="10", MAX_LEVERAGE="10",
                            ENABLED_STRATEGIES="microflow_scalper_v1,momentum_breakout")
    assert not guard_passes(MICROFLOW_LEVERAGE="10", MAX_LEVERAGE="10",
                            OLD_STRATEGIES_NEW_ENTRIES_ENABLED="true")
