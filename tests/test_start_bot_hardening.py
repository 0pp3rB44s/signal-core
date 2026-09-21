"""scripts/start_bot.sh must not be a way around the live ceremony.

Agent C (2026-09-19) found start_bot.sh starts app.main with none of the four ceremony layers, gated only by whatever
`.env` (or already-exported shell variables) happen to contain; a second review (2026-09-20) found three constructed
cases in which the shell re-implementation of the settings rules disagreed with the app. The launcher now asks the
APP (app/launch_guard.py builds the very same `Settings`), so there is exactly one parser.

These tests drive the REAL script (and the real scripts/lib/env_guard.sh + app resolver) in a hermetic temporary
project whose `nohup` and `sleep` are shims, so a refusal is proven to happen BEFORE anything is started or stopped.
"""
from __future__ import annotations

import os
import re
import subprocess

import pytest

from tests.start_bot_harness import (REPO, live_capable, make_project, refused, run_start_bot, spawn_decoy)


# ------------------------------------------------------------------ the safe path still works
def test_a_clean_paper_environment_is_not_refused(tmp_path):
    result, calls, _proj = run_start_bot(tmp_path)
    assert result.returncode != 90 and "nohup" in calls           # it reached the normal start sequence


def test_blank_credentials_and_disabled_execution_are_fine(tmp_path):
    env = "EXECUTION_MODE=DRY_RUN\nEXECUTION_ENABLED=false\nBITGET_API_KEY=\nBITGET_API_SECRET=\n"
    result, calls, _proj = run_start_bot(tmp_path, env_text=env)
    assert result.returncode != 90 and "nohup" in calls


# ------------------------------------------------------------------ every live-capable route is refused
@pytest.mark.parametrize("env_text", [
    "EXECUTION_MODE=LIVE\n",
    'EXECUTION_MODE="live"\n',
    "EXECUTION_MODE='LIVE'   # armed\n",
    "EXECUTION_ENABLED=true\n",
    "export EXECUTION_ENABLED=1\n",
    "EXECUTION_ENABLED=Yes\n",
    "BITGET_API_KEY=abc123\n",
    "BITGET_API_SECRET='s3cr3t'  # comment\n",
    "BITGET_API_PASSPHRASE=\"pass phrase\"\n",
    "EXECUTION_MODE=DRY_RUN\nEXECUTION_MODE=LIVE\n",                # the LAST assignment wins
])
def test_a_live_capable_dotenv_is_refused_before_anything_is_started_or_stopped(tmp_path, env_text):
    result, calls, proj = run_start_bot(tmp_path, env_text=env_text)
    assert refused(result, calls, proj), (result.returncode, calls, result.stderr)
    assert "live-capable" in result.stderr and "launch_live.sh" in result.stderr


@pytest.mark.parametrize("exported", [
    {"EXECUTION_MODE": "LIVE"},
    {"execution_mode": "live"},                                    # pydantic-settings is case-insensitive
    {"EXECUTION_ENABLED": "true"},
    {"BITGET_API_KEY": "k"},
    {"bitget_api_secret": "s"},
    {"BITGET_API_PASSPHRASE": "p"},
])
def test_live_capable_variables_already_exported_into_the_shell_are_refused(tmp_path, exported):
    result, calls, proj = run_start_bot(tmp_path, exported=exported)      # harmless .env, live shell
    assert refused(result, calls, proj), (result.returncode, calls, result.stderr)


def test_the_exact_bypass_agent_c_described_no_dotenv_but_a_sourced_live_env(tmp_path):
    """.env is ABSENT, and the operator sourced .env.live into their shell instead."""
    result, calls, proj = run_start_bot(
        tmp_path, env_text=None,
        exported={"EXECUTION_MODE": "LIVE", "EXECUTION_ENABLED": "true", "BITGET_API_KEY": "k"})
    assert refused(result, calls, proj)
    # a half-configured LIVE environment is refused either as live-capable or, when the app's production-LIVE
    # validators cannot even build a Settings from it, as unresolvable (fail closed) -- never as ".env not found"
    assert ("live-capable" in result.stderr or "fail closed" in result.stderr) and ".env not found" not in result.stderr


def test_resolution_follows_the_apps_own_precedence_os_environment_beats_dotenv(tmp_path):
    result, calls, proj = run_start_bot(tmp_path, env_text="EXECUTION_MODE=DRY_RUN\n", exported={"EXECUTION_MODE": "LIVE"})
    assert refused(result, calls, proj)
    # the reverse: the file says LIVE but the OS environment wins with DRY_RUN -> resolved value is safe
    result, calls, _proj = run_start_bot(tmp_path / "b", env_text="EXECUTION_MODE=LIVE\n",
                                         exported={"EXECUTION_MODE": "DRY_RUN"})
    assert result.returncode != 90 and "nohup" in calls


def test_secret_values_are_never_printed(tmp_path):
    result, _calls, _proj = run_start_bot(tmp_path, env_text="BITGET_API_KEY=abc123\nBITGET_API_SECRET=s3cr3t\n")
    assert "abc123" not in result.stdout + result.stderr and "s3cr3t" not in result.stdout + result.stderr
    assert "BITGET_API_KEY(non-blank)" in result.stderr


# ------------------------------------------------------------------ the three constructed bypasses Agent C found
@pytest.mark.parametrize("order", [
    [("EXECUTION_MODE", "DRY_RUN"), ("execution_mode", "LIVE")],
    [("execution_mode", "LIVE"), ("EXECUTION_MODE", "DRY_RUN")],
    [("execution_mode", "DRY_RUN"), ("EXECUTION_MODE", "LIVE")],
    [("EXECUTION_MODE", "LIVE"), ("execution_mode", "DRY_RUN")],
], ids=["upper-safe-then-lower-live", "lower-live-then-upper-safe", "lower-safe-then-upper-live",
        "upper-live-then-lower-safe"])
def test_bypass_1_duplicate_variable_names_differing_only_by_case_follow_the_apps_choice(tmp_path, order):
    """The shell guard took the FIRST match, the app the LAST. Whatever the APP resolves is what the guard decides,
    in every ordering -- checked against the app's own resolution, not against an expectation about which one wins."""
    result, calls, proj, truth = run_start_bot(tmp_path, env_order=order, with_truth=True)
    app_says_live = live_capable(truth)
    assert app_says_live is not None, truth
    assert (result.returncode == 90) == app_says_live
    assert ("nohup" in calls) == (not app_says_live)


def test_bypass_2_dotenv_interpolation_is_resolved_by_the_apps_own_dotenv_reader(tmp_path):
    result, calls, proj, truth = run_start_bot(tmp_path, env_text="X=LIVE\nEXECUTION_MODE=${X}\n", with_truth=True)
    assert live_capable(truth) is True and truth["mode"].upper() == "LIVE"        # the app really reads LIVE here
    assert refused(result, calls, proj) and "EXECUTION_MODE=LIVE" in result.stderr


@pytest.mark.parametrize("token", ["t", "T", "on", "ON", "yes", "Y", "1", "true", "TRUE"])
def test_bypass_3_every_boolean_token_the_app_accepts_is_a_live_capable_enable(tmp_path, token):
    result, calls, proj, truth = run_start_bot(tmp_path / token, env_text=f"EXECUTION_ENABLED={token}\n",
                                               with_truth=True)
    assert live_capable(truth) is True
    assert refused(result, calls, proj) and "EXECUTION_ENABLED=true" in result.stderr


@pytest.mark.parametrize("token", ["f", "F", "off", "no", "n", "0", "false", "FALSE"])
def test_every_boolean_token_the_app_reads_as_false_is_not_refused(tmp_path, token):
    result, calls, _proj, truth = run_start_bot(tmp_path / token, env_text=f"EXECUTION_ENABLED={token}\n",
                                                with_truth=True)
    assert live_capable(truth) is False
    assert result.returncode != 90 and "nohup" in calls


def test_a_configuration_the_app_cannot_resolve_is_refused_fail_closed(tmp_path):
    result, calls, proj = run_start_bot(tmp_path, env_text="EXECUTION_ENABLED=maybe\n")
    assert refused(result, calls, proj) and "could not resolve" in result.stderr and "fail closed" in result.stderr
    assert "maybe" not in result.stderr                                    # field NAMES only, never values


def test_a_missing_interpreter_is_refused_not_guessed(tmp_path):
    proj, shims, log = make_project(tmp_path)
    (proj / ".venv" / "bin" / "python3").unlink()
    result = subprocess.run(["/bin/bash", str(proj / "scripts" / "start_bot.sh")], cwd=proj, capture_output=True,
                            text=True, env={"PATH": f"{shims}:/usr/bin:/bin", "HOME": str(tmp_path)}, timeout=30)
    assert result.returncode == 90 and "fail closed" in result.stderr and not log.exists()


# ------------------------------------------------------------------ never kill a running live engine
def test_a_running_live_engine_is_never_killed_by_this_script(tmp_path):
    state = f"mode=LIVE\npid={os.getpid()}\ncommit=abc\n"                    # a real, alive pid
    result, calls, proj = run_start_bot(tmp_path, live_state=state)
    assert result.returncode == 90 and "nohup" not in calls
    assert "LIVE engine" in result.stderr and "will not kill it" in result.stderr


def test_a_lowercase_live_state_is_recognised_too(tmp_path):
    result, _calls, _proj = run_start_bot(tmp_path, live_state=f"mode=live\npid={os.getpid()}\n")
    assert result.returncode == 90 and "LIVE engine" in result.stderr


def test_a_stale_or_non_live_runtime_state_does_not_block_the_paper_launcher(tmp_path):
    dead = run_start_bot(tmp_path / "a", live_state="mode=LIVE\npid=999999\n")
    forward = run_start_bot(tmp_path / "b", live_state=f"mode=FORWARD\npid={os.getpid()}\n")
    for result, calls, _proj in (dead, forward):
        assert result.returncode != 90 and "nohup" in calls


def test_only_app_main_processes_of_this_project_directory_are_killed(tmp_path):
    """The old `pkill -9 -f app.main` killed every app.main on the host: a live engine or the shadow runner in another
    checkout included, with no state file to warn us."""
    other = tmp_path / "other_checkout"
    other.mkdir()
    (tmp_path / "proj").mkdir(exist_ok=True)
    mine = spawn_decoy(tmp_path / "proj")
    foreign = spawn_decoy(other)
    try:
        result, calls, _proj = run_start_bot(tmp_path)
        assert result.returncode != 90 and "nohup" in calls
        assert mine.wait(timeout=5) is not None                            # this project's previous bot: stopped
        assert foreign.poll() is None                                      # another directory's engine: untouched
        assert f"pid {foreign.pid} belongs to another directory" in result.stdout
        assert f"stopped previous app.main (pid {mine.pid}) of this project" in result.stdout
    finally:
        for proc in (mine, foreign):
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def test_a_refused_start_kills_nothing_at_all(tmp_path):
    (tmp_path / "proj").mkdir(exist_ok=True)
    mine = spawn_decoy(tmp_path / "proj")
    try:
        result, calls, proj = run_start_bot(tmp_path, env_text="EXECUTION_MODE=LIVE\n")
        assert refused(result, calls, proj)
        assert mine.poll() is None
    finally:
        mine.kill()
        mine.wait()


# ------------------------------------------------------------------ structure: ONE parser, guards first
def test_the_guards_run_before_dotenv_is_read_and_before_anything_is_killed():
    text = (REPO / "scripts" / "start_bot.sh").read_text()
    guard_live = text.index("guard_assert_not_live_capable_env")
    guard_engine = text.index("guard_assert_no_live_engine_running")
    assert guard_live < text.index('[ ! -f ".env" ]') < text.index("guard_kill_own_app_main")
    assert guard_engine < text.index("guard_kill_own_app_main")
    assert "pkill" not in text


def test_there_is_exactly_one_settings_parser_the_launcher_does_not_reimplement_it():
    guard = (REPO / "scripts" / "lib" / "env_guard.sh").read_text()
    assert "app.launch_guard" in guard
    assert "guard_resolve_setting" not in guard
    body = guard[guard.index("guard_assert_not_live_capable_env()"):guard.index("guard_assert_no_live_engine_running()")]
    assert not re.search(r"\bawk\b|\bgrep\b|\bsed\b", body)               # no text parsing of .env in the decision
    launcher = (REPO / "scripts" / "start_bot.sh").read_text()
    assert "guard_resolve_setting" not in launcher


def test_the_ceremony_scripts_are_untouched_by_this_hardening():
    launch = (REPO / "scripts" / "launch_live.sh").read_text()
    assert "guard_assert_authorization_binding" in launch and "guard_assert_not_live_capable_env" not in launch
