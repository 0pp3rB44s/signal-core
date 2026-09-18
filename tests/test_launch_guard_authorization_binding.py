"""Stale-authorization vulnerability fix: Layer 2 must bind to CONTENT, not
just file existence.

Reproduced finding: the old `guard_assert_authorization_binding`-equivalent
was `[ -f "$AUTH_FILE" ]` in scripts/launch_live.sh. Production's actual
state/LIVE_PILOT_AUTHORISATION is, right now, dated for commit
aa42c3aac52ef67d6d20efac2901f22581a60877 while production HEAD is
4d2fb7182afc49687cfa27cde3fcc7c4783f22ae -- a DIFFERENT commit -- and the old
check would still pass. These tests drive the REAL bash function via
subprocess (matching tests/test_launch_guard_leverage.py's own convention),
not a Python re-implementation, so a change to the shell is what is actually
tested. No real owner authorization is generated or touched anywhere here.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GUARD = REPO / "scripts" / "lib" / "env_guard.sh"

PRODUCTION_STALE_COMMIT = "aa42c3aac52ef67d6d20efac2901f22581a60877"
CURRENT_HEAD_AT_WRITE_TIME = "4d2fb7182afc49687cfa27cde3fcc7c4783f22ae"

BASE_ENV = {
    "MAX_LEVERAGE": "5", "MAX_OPEN_POSITIONS": "2", "EXECUTION_MAX_PER_CYCLE": "2",
    "PRODUCTION_SYMBOL_ALLOWLIST": "BTCUSDT,ETHUSDT",
    "ENABLED_STRATEGIES": "microflow_scalper_v1",
    "HARD_DAILY_STOP_PCT": "2.0", "WEEKLY_FREEZE_LOSS_PCT": "7.0",
    "ADAPTIVE_TREND_LIVE_ENTRY_ENABLED": "false", "MICROFLOW_SCALPER_ENABLED": "true",
    "APP_ENV": "production", "BITGET_API_KEY": "test-key-not-real",
}


def _risk_config_hash(env: dict) -> str:
    material = "|".join([
        env.get("MAX_LEVERAGE", ""), env.get("MAX_OPEN_POSITIONS", ""),
        env.get("EXECUTION_MAX_PER_CYCLE", ""), env.get("PRODUCTION_SYMBOL_ALLOWLIST", ""),
        env.get("ENABLED_STRATEGIES", ""), env.get("HARD_DAILY_STOP_PCT", ""),
        env.get("WEEKLY_FREEZE_LOSS_PCT", ""), env.get("ADAPTIVE_TREND_LIVE_ENTRY_ENABLED", ""),
        env.get("MICROFLOW_SCALPER_ENABLED", ""),
    ]) + "\n"
    return hashlib.sha256(material.encode()).hexdigest()


def _account_identity(env: dict) -> str:
    key = env.get("BITGET_API_KEY", "")
    key_fp = hashlib.sha256(key.encode()).hexdigest()[:16] if key else "N/A"
    return f"{env.get('APP_ENV', 'unset')}:{key_fp}"


def _current_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
                          text=True, check=True).stdout.strip()


def _current_runtime_hash() -> str:
    """Mirror guard_compute_runtime_hash()'s exact fallback order, so tests
    reflect THIS host's real state (an installed launchd plist, if any)
    rather than assuming a clean host with nothing installed."""
    installed = Path.home() / "Library" / "LaunchAgents" / "com.cgc.live.plist"
    if installed.is_file():
        return hashlib.sha256(installed.read_bytes()).hexdigest()
    template = REPO / "deploy" / "launchd" / "com.cgc.live.plist.template"
    if template.is_file():
        return hashlib.sha256(template.read_bytes()).hexdigest()
    return "N/A"


def _write_auth_file(tmp_path, name: str = "LIVE_PILOT_AUTHORISATION", **fields) -> Path:
    p = tmp_path / name
    p.write_text("\n".join(f"{k}={v}" for k, v in fields.items()) + "\n")
    return p


def _matching_fields(env: dict, *, commit: str | None = None, runtime_hash: str | None = None) -> dict:
    return dict(
        commit=commit or _current_head(),
        risk_config_hash=_risk_config_hash(env),
        runtime_hash=runtime_hash if runtime_hash is not None else _current_runtime_hash(),
        account_environment=_account_identity(env),
        authorized_at="2026-09-18T00:00:00Z",
        authorised_for="LIVE",
    )


def run_guard(auth_file: Path, **overrides) -> subprocess.CompletedProcess:
    env = dict(BASE_ENV)
    env.update({k: v for k, v in overrides.items() if v is not None})
    for key, value in list(overrides.items()):
        if value is None:
            env.pop(key, None)
    exports = "\n".join(f"export {k}={v!r}" for k, v in env.items())
    script = f"cd {REPO}\nset -e\n{exports}\nsource {GUARD}\nguard_assert_authorization_binding {auth_file}\n"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def guard_passes(auth_file: Path, **overrides) -> bool:
    return run_guard(auth_file, **overrides).returncode == 0


# --- reproduction: the exact real-world vulnerability -----------------------

def test_reproduces_the_actual_production_stale_authorization(tmp_path):
    """The literal scenario found on production: an authorization file that
    plainly EXISTS, for a real prior commit, while HEAD has since moved.
    The OLD guard (bare -f check) would pass this. The new one must not."""
    auth = _write_auth_file(
        tmp_path, commit=PRODUCTION_STALE_COMMIT, risk_config_hash=_risk_config_hash(BASE_ENV),
        runtime_hash="N/A", account_environment=_account_identity(BASE_ENV),
        authorized_at="2026-08-08T01:19:00Z", authorised_for="LIVE",
    )
    result = run_guard(auth)
    assert result.returncode != 0
    assert "commit" in result.stderr.lower()
    assert PRODUCTION_STALE_COMMIT in result.stderr


# --- correct authorization + matching identity -------------------------------

def test_correct_authorization_with_matching_identity_passes(tmp_path):
    fields = _matching_fields(BASE_ENV)
    auth = _write_auth_file(tmp_path, **fields)
    result = run_guard(auth)
    assert result.returncode == 0, result.stderr
    assert "content-bound and verified" in result.stdout


# --- each required mismatch, individually ------------------------------------

def test_stale_commit_fails(tmp_path):
    fields = _matching_fields(BASE_ENV, commit="0" * 40)
    auth = _write_auth_file(tmp_path, **fields)
    assert not guard_passes(auth)


def test_wrong_risk_config_hash_fails(tmp_path):
    fields = _matching_fields(BASE_ENV)
    fields["risk_config_hash"] = "0" * 64
    auth = _write_auth_file(tmp_path, **fields)
    result = run_guard(auth)
    assert result.returncode != 0
    assert "risk_config_hash" in result.stderr


def test_wrong_runtime_hash_fails(tmp_path):
    fields = _matching_fields(BASE_ENV, runtime_hash="0" * 64)
    auth = _write_auth_file(tmp_path, **fields)
    result = run_guard(auth)
    assert result.returncode != 0
    assert "runtime_hash" in result.stderr


def test_wrong_account_environment_fails(tmp_path):
    fields = _matching_fields(BASE_ENV)
    fields["account_environment"] = "production:" + "f" * 16
    auth = _write_auth_file(tmp_path, **fields)
    result = run_guard(auth)
    assert result.returncode != 0
    assert "account" in result.stderr.lower()


def test_different_bitget_api_key_changes_required_identity(tmp_path):
    """A different (even if equally 'present') credential must not reuse an
    authorization issued for a different account."""
    fields = _matching_fields(BASE_ENV)
    auth = _write_auth_file(tmp_path, **fields)
    assert not guard_passes(auth, BITGET_API_KEY="a-totally-different-key")


# --- missing / malformed -----------------------------------------------------

def test_missing_authorization_file_fails(tmp_path):
    assert not guard_passes(tmp_path / "does_not_exist")


def test_missing_commit_field_fails(tmp_path):
    fields = _matching_fields(BASE_ENV)
    del fields["commit"]
    auth = _write_auth_file(tmp_path, **fields)
    result = run_guard(auth)
    assert result.returncode != 0
    assert "no commit=" in result.stderr


def test_missing_risk_config_hash_field_fails(tmp_path):
    fields = _matching_fields(BASE_ENV)
    del fields["risk_config_hash"]
    auth = _write_auth_file(tmp_path, **fields)
    assert not guard_passes(auth)


def test_old_format_file_with_only_commit_and_authorised_for_fails_closed(tmp_path):
    """The EXACT current production file format (just commit= and
    authorised_for=) must be rejected outright as missing required fields,
    not silently accepted because commit happens to be present."""
    auth = tmp_path / "LIVE_PILOT_AUTHORISATION"
    auth.write_text(f"commit={_current_head()}\nauthorised_for=LIVE\n")
    result = run_guard(auth)
    assert result.returncode != 0
    assert "old-format or malformed" in result.stderr


def test_malformed_authorization_garbage_content_fails(tmp_path):
    auth = tmp_path / "LIVE_PILOT_AUTHORISATION"
    auth.write_text("this is not a key=value file at all\n\xff\xfe binary garbage")
    assert not guard_passes(auth)


def test_empty_file_fails(tmp_path):
    auth = tmp_path / "LIVE_PILOT_AUTHORISATION"
    auth.write_text("")
    assert not guard_passes(auth)


# --- replay / obsolete --------------------------------------------------------

def test_replayed_obsolete_authorization_from_a_prior_valid_deployment_fails(tmp_path):
    """A file that WAS valid for an earlier, different commit (not just
    production's one known-stale example) must still fail today -- proves
    this isn't a special-case fix for one hash, but a general binding."""
    fields = _matching_fields(BASE_ENV, commit="1111111111111111111111111111111111111a")
    auth = _write_auth_file(tmp_path, **fields)
    assert not guard_passes(auth)


# --- HEAD changes after authorization -----------------------------------------

def test_head_changing_after_authorization_invalidates_it(tmp_path):
    """Simulates the exact production timeline: authorize for commit X, then
    HEAD moves to Y (a merge, a hotfix, anything) -- the old authorization
    must not silently keep working. This worktree's HEAD cannot be safely
    moved mid-test-run, so "HEAD moved" is simulated the same way production's
    real incident looks from the guard's perspective: an authorization file
    whose recorded commit no longer equals the current HEAD."""
    fields = _matching_fields(BASE_ENV)  # bound to the CURRENT real HEAD
    auth = _write_auth_file(tmp_path, **fields)
    assert guard_passes(auth)  # sanity: valid right now, for the real current HEAD

    moved = _write_auth_file(tmp_path, name="LIVE_PILOT_AUTHORISATION_moved",
                             **{**fields, "commit": "f" * 40})
    assert not guard_passes(moved)
