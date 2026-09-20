"""scripts/start_bot.sh must not be a way around the live ceremony.

Agent C (2026-09-19) found start_bot.sh starts app.main with none of the four ceremony layers, gated
only by whatever `.env` (or already-exported shell variables) happen to contain. These tests drive the
REAL script (and the real scripts/lib/env_guard.sh) in a hermetic temporary project whose `pkill`,
`nohup` and `sleep` are shims, so a refusal is proven to happen BEFORE anything is killed or started.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BASE_PATH = "/usr/bin:/bin"


def make_project(tmp_path: Path, env_text: str | None = "EXECUTION_MODE=DRY_RUN\nEXECUTION_ENABLED=false\n"):
    proj = tmp_path / "proj"
    (proj / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "start_bot.sh", proj / "scripts" / "start_bot.sh")
    shutil.copy(REPO / "scripts" / "lib" / "env_guard.sh", proj / "scripts" / "lib" / "env_guard.sh")
    (proj / ".venv" / "bin").mkdir(parents=True)
    (proj / ".venv" / "bin" / "activate").write_text("")
    if env_text is not None:
        (proj / ".env").write_text(env_text)
    shims = tmp_path / "shims"
    shims.mkdir()
    log = tmp_path / "shim_calls.log"
    for name in ("pkill", "nohup", "sleep"):
        shim = shims / name
        shim.write_text(f"#!/bin/bash\necho \"{name} $*\" >> {log}\nexit 0\n")
        shim.chmod(0o755)
    return proj, shims, log


def run_start_bot(tmp_path, *, env_text="EXECUTION_MODE=DRY_RUN\nEXECUTION_ENABLED=false\n", exported=None,
                  live_state: str | None = None):
    proj, shims, log = make_project(tmp_path, env_text)
    if live_state is not None:
        (proj / "state").mkdir()
        (proj / "state" / "live_runtime.state").write_text(live_state)
    env = {"PATH": f"{shims}:{BASE_PATH}", "HOME": str(tmp_path)}
    env.update(exported or {})
    result = subprocess.run(["/bin/bash", str(proj / "scripts" / "start_bot.sh")], cwd=proj, env=env,
                            capture_output=True, text=True, timeout=30)
    calls = log.read_text() if log.exists() else ""
    if result.returncode != 90:                        # the shimmed `nohup` is a background job: let it land
        deadline = time.time() + 3
        while "nohup" not in calls and time.time() < deadline:
            time.sleep(0.05)
            calls = log.read_text() if log.exists() else ""
    return result, calls, proj


def refused(result, calls, proj) -> bool:
    return (result.returncode == 90 and "pkill" not in calls and "nohup" not in calls
            and not (proj / "state" / "bot.pid").exists() and not (proj / "logs").exists())


# ------------------------------------------------------------------ the safe path still works
def test_a_clean_paper_environment_is_not_refused(tmp_path):
    result, calls, _proj = run_start_bot(tmp_path)
    assert result.returncode != 90
    assert "pkill" in calls and "nohup" in calls           # it reached the normal start sequence


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
    """.env is ABSENT, and the operator sourced .env.live into their shell instead: the old script
    aborted only because .env was missing; pydantic would still have read the exported variables."""
    result, calls, proj = run_start_bot(
        tmp_path, env_text=None,
        exported={"EXECUTION_MODE": "LIVE", "EXECUTION_ENABLED": "true", "BITGET_API_KEY": "k"})
    assert refused(result, calls, proj)
    assert "live-capable" in result.stderr and ".env not found" not in result.stderr


def test_resolution_follows_pydantic_precedence_os_environment_beats_dotenv(tmp_path):
    result, calls, proj = run_start_bot(tmp_path, env_text="EXECUTION_MODE=DRY_RUN\n",
                                        exported={"EXECUTION_MODE": "LIVE"})
    assert refused(result, calls, proj)
    # the reverse: the file says LIVE but the OS environment wins with DRY_RUN -> resolved value is safe
    result, calls, _proj = run_start_bot(tmp_path / "b", env_text="EXECUTION_MODE=LIVE\n",
                                         exported={"EXECUTION_MODE": "DRY_RUN"})
    assert result.returncode != 90 and "nohup" in calls


def test_secret_values_are_never_printed(tmp_path):
    result, _calls, _proj = run_start_bot(tmp_path, env_text="BITGET_API_KEY=abc123\nBITGET_API_SECRET=s3cr3t\n")
    assert "abc123" not in result.stdout + result.stderr and "s3cr3t" not in result.stdout + result.stderr
    assert "BITGET_API_KEY(non-blank)" in result.stderr


# ------------------------------------------------------------------ never kill a running live engine
def test_a_running_live_engine_is_never_killed_by_this_script(tmp_path):
    state = f"mode=LIVE\npid={os.getpid()}\ncommit=abc\n"                    # a real, alive pid
    result, calls, proj = run_start_bot(tmp_path, live_state=state)
    assert result.returncode == 90 and "pkill" not in calls
    assert "LIVE engine" in result.stderr and "will not kill it" in result.stderr


def test_a_stale_or_non_live_runtime_state_does_not_block_the_paper_launcher(tmp_path):
    dead = run_start_bot(tmp_path / "a", live_state="mode=LIVE\npid=999999\n")
    forward = run_start_bot(tmp_path / "b", live_state=f"mode=FORWARD\npid={os.getpid()}\n")
    for result, calls, _proj in (dead, forward):
        assert result.returncode != 90 and "nohup" in calls


# ------------------------------------------------------------------ structural
def test_the_guards_run_before_dotenv_is_read_and_before_anything_is_killed():
    text = (REPO / "scripts" / "start_bot.sh").read_text()
    guard_live = text.index("guard_assert_not_live_capable_env")
    guard_engine = text.index("guard_assert_no_live_engine_running")
    assert guard_live < text.index('[ ! -f ".env" ]') < text.index("pkill")
    assert guard_engine < text.index("pkill")


def test_the_ceremony_scripts_are_untouched_by_this_hardening():
    launch = (REPO / "scripts" / "launch_live.sh").read_text()
    assert "guard_assert_authorization_binding" in launch and "guard_assert_not_live_capable_env" not in launch
