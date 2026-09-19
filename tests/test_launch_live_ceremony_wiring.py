"""Adversarial tests that drive the REAL scripts/launch_live.sh ceremony
script itself, not just the env_guard.sh validator function in isolation.

Reproduced finding (2026-09-19): tests/test_launch_guard_authorization_binding.py
only ever sources env_guard.sh and calls guard_assert_authorization_binding()
directly. It never runs scripts/launch_live.sh, so it could not have caught --
and did not catch -- that launch_live.sh's own LAYER 2 still called the old
bare `[ -f "$AUTH_FILE" ]` check and never invoked the new function at all.
Reproduced directly against the unfixed script before this file existed: with
production's real stale authorisation (commit=aa42c3a..., HEAD=8c51d1f...),
launch_live.sh printed "layer 2: authorisation token present" and proceeded
into LAYER 3 -- the bare existence check passed. These tests run the actual
script, in a disposable git worktree checkout of this worktree's own current
HEAD, so a regression in the WIRING (not just the validator) fails a test.

No real owner authorization is generated or touched anywhere -- every
authorisation file here lives inside a throwaway checkout under pytest's
tmp_path and is fabricated only to match that checkout's own, non-secret,
test-only environment. The disposable checkout has no .venv (gitignored, not
part of any git worktree checkout), so LAYER 3 always stops at
guard_assert_venv before LAYER 4's interactive confirmation or any engine
launch -- these tests never reach, and structurally cannot reach, the point
where an order could be placed.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.test_launch_guard_authorization_binding import (
    _account_identity,
    _current_runtime_hash,
    _risk_config_hash,
)

REPO = Path(__file__).resolve().parents[1]

PRODUCTION_STALE_COMMIT = "aa42c3aac52ef67d6d20efac2901f22581a60877"

BASE_ENV = {
    "MAX_LEVERAGE": "5", "MAX_OPEN_POSITIONS": "2", "EXECUTION_MAX_PER_CYCLE": "2",
    "PRODUCTION_SYMBOL_ALLOWLIST": "BTCUSDT,ETHUSDT",
    "ENABLED_STRATEGIES": "microflow_scalper_v1",
    "HARD_DAILY_STOP_PCT": "2.0", "WEEKLY_FREEZE_LOSS_PCT": "7.0",
    "ADAPTIVE_TREND_LIVE_ENTRY_ENABLED": "false", "MICROFLOW_SCALPER_ENABLED": "true",
    "APP_ENV": "production", "BITGET_API_KEY": "test-key-not-real",
}


def _current_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
                          text=True, check=True).stdout.strip()


def _matching_fields(env: dict) -> dict:
    return dict(
        commit=_current_head(),
        risk_config_hash=_risk_config_hash(env),
        runtime_hash=_current_runtime_hash(),
        account_environment=_account_identity(env),
        authorized_at="2026-09-19T00:00:00Z",
        authorised_for="LIVE",
    )


@pytest.fixture()
def ceremony_checkout(tmp_path):
    """A disposable, detached git-worktree checkout of this repo's own current
    HEAD. scripts/launch_live.sh and scripts/lib/env_guard.sh are therefore
    byte-identical to the real files -- this runs the actual ceremony script,
    not a copy or a re-implementation of its logic."""
    checkout = tmp_path / "ceremony_checkout"
    subprocess.run(["git", "worktree", "add", "--detach", str(checkout), "HEAD"],
                    cwd=REPO, check=True, capture_output=True, text=True)
    try:
        yield checkout
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(checkout)],
                        cwd=REPO, check=False, capture_output=True, text=True)


def _write_auth_file(checkout: Path, **fields) -> Path:
    p = checkout / "state" / "LIVE_PILOT_AUTHORISATION"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(f"{k}={v}" for k, v in fields.items()) + "\n")
    return p


def run_ceremony(checkout: Path, **env_overrides) -> subprocess.CompletedProcess:
    env = {**os.environ, **BASE_ENV, **env_overrides}
    return subprocess.run(
        ["bash", "scripts/launch_live.sh"],
        cwd=checkout, env=env, input="\n",
        capture_output=True, text=True, timeout=30,
    )


def _never_reached_layer4_or_beyond(checkout: Path, result: subprocess.CompletedProcess) -> None:
    """Hard safety backstop for every test in this file, not just an
    assertion about messages: whatever failed, the ceremony must not have
    reached the interactive confirmation, must not have written a bot pid,
    and must not have started any process."""
    assert "About to start LIVE trading" not in result.stdout
    assert result.returncode != 0
    assert not (checkout / "state" / "bot.pid").exists()
    assert not (checkout / "state" / "live_runtime.state").exists()


def _rejected_at_layer2(checkout: Path, result: subprocess.CompletedProcess) -> None:
    """Stricter than 'died somewhere': proves the rejection happened AT LAYER
    2 specifically, before LAYER 3 ever started. Without this, a test could
    pass for the wrong reason -- e.g. the old bare `-f` check letting a bad
    file through LAYER 2, only for the run to later die at
    guard_assert_venv anyway, which would make an adversarial test look
    green while the actual vulnerability remained open. That exact failure
    mode is why this file exists: it is what the previous, validator-only
    test suite could not have detected."""
    assert "LAYER 2" in result.stderr
    assert "layer 2: authorisation token present" not in result.stdout
    assert "layer 2: authorisation content-bound and verified" not in result.stdout
    assert "guard: repository clean" not in result.stdout
    _never_reached_layer4_or_beyond(checkout, result)


# --- reproduction -------------------------------------------------------------

def test_reproduces_real_stale_auth_no_longer_bypasses_the_real_script(ceremony_checkout):
    """The exact scenario found on production, driven through the real script
    end to end (not the validator in isolation): the bare-existence check that
    used to live in launch_live.sh itself is gone."""
    _write_auth_file(ceremony_checkout, commit=PRODUCTION_STALE_COMMIT, authorised_for="LIVE")
    result = run_ceremony(ceremony_checkout)
    assert PRODUCTION_STALE_COMMIT in result.stderr
    _rejected_at_layer2(ceremony_checkout, result)


# --- adversarial: every required mismatch, through the real script -----------

def test_missing_authorization_file_fails_closed_through_real_script(ceremony_checkout):
    result = run_ceremony(ceremony_checkout)
    _rejected_at_layer2(ceremony_checkout, result)


def test_stale_commit_fails_closed_through_real_script(ceremony_checkout):
    fields = _matching_fields(BASE_ENV)
    fields["commit"] = "0" * 40
    _write_auth_file(ceremony_checkout, **fields)
    result = run_ceremony(ceremony_checkout)
    _rejected_at_layer2(ceremony_checkout, result)


def test_wrong_runtime_hash_fails_closed_through_real_script(ceremony_checkout):
    fields = _matching_fields(BASE_ENV)
    fields["runtime_hash"] = "0" * 64
    _write_auth_file(ceremony_checkout, **fields)
    result = run_ceremony(ceremony_checkout)
    _rejected_at_layer2(ceremony_checkout, result)


def test_wrong_risk_config_fails_closed_through_real_script(ceremony_checkout):
    fields = _matching_fields(BASE_ENV)
    _write_auth_file(ceremony_checkout, **fields)
    result = run_ceremony(ceremony_checkout, MAX_LEVERAGE="9")
    _rejected_at_layer2(ceremony_checkout, result)


def test_wrong_account_environment_fails_closed_through_real_script(ceremony_checkout):
    fields = _matching_fields(BASE_ENV)
    _write_auth_file(ceremony_checkout, **fields)
    result = run_ceremony(ceremony_checkout, BITGET_API_KEY="a-totally-different-key")
    _rejected_at_layer2(ceremony_checkout, result)


def test_malformed_authorization_fails_closed_through_real_script(ceremony_checkout):
    p = ceremony_checkout / "state" / "LIVE_PILOT_AUTHORISATION"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("this is not a key=value file at all\n\xff\xfe binary garbage")
    result = run_ceremony(ceremony_checkout)
    _rejected_at_layer2(ceremony_checkout, result)


def test_old_format_production_shaped_file_fails_closed_through_real_script(ceremony_checkout):
    """Exactly today's real production file shape: only commit= and
    authorised_for=, nothing else -- even bound to the CURRENT commit, an
    old-format file must still be rejected for missing binding fields."""
    _write_auth_file(ceremony_checkout, commit=_current_head(), authorised_for="LIVE")
    result = run_ceremony(ceremony_checkout)
    assert "old-format or malformed" in result.stderr
    _rejected_at_layer2(ceremony_checkout, result)


# --- positive path: Layer 2 is genuinely exercised, not merely absent --------

def test_correctly_bound_authorization_clears_layer2_of_the_real_script(ceremony_checkout):
    """A content-matching authorization must pass LAYER 2 and reach LAYER 3 --
    proving LAYER 2 is genuinely being evaluated, not skipped some other way.
    The checkout has no .venv (gitignored), so LAYER 3 stops at
    guard_assert_venv -- a controlled, unrelated, expected failure -- well
    before LAYER 4's interactive confirmation or any engine launch."""
    fields = _matching_fields(BASE_ENV)
    _write_auth_file(ceremony_checkout, **fields)
    result = run_ceremony(ceremony_checkout)
    assert "layer 2: authorisation content-bound and verified" in result.stdout
    assert "guard: LAYER 1 OK" in result.stdout
    assert ".venv/bin/python is unavailable" in result.stderr
    _never_reached_layer4_or_beyond(ceremony_checkout, result)
