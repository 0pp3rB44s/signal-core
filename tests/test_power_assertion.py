"""Tests for the collection power assertion.

Between 2026-07-20 and 2026-07-25 the archiver captured ~2% of its nominal
volume: 4,280 orderbook rows/hour while the host was awake, 20-90 rows/hour
while it slept. The collector code was correct; the host was not. `pmset -g`
reported `sleep 1` (idle sleep after one minute) with 767 sleep/wakes since
boot, and the only sleep-blocking assertion was powerd's "display is on",
which disappears as soon as the screen turns off.

The launchers now hold a caffeinate assertion bound to the collector's pid.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
LIB = SCRIPTS / "lib" / "power_assertion.sh"


def _run(snippet: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["bash", "-c", f"source {LIB}\n{snippet}"],
        capture_output=True, text=True, env=full_env,
    )


def test_power_assertion_library_parses() -> None:
    subprocess.run(["bash", "-n", str(LIB)], check=True)


@pytest.mark.parametrize("script", ["start_archiver.sh", "start_forward_paper.sh"])
def test_launchers_source_and_hold_the_assertion(script: str) -> None:
    text = (SCRIPTS / script).read_text(encoding="utf-8")
    assert "power_assertion.sh" in text, f"{script} must source the helper"
    assert "hold_power_assertion" in text, f"{script} must hold an assertion"


def test_assertion_is_bound_to_the_collector_pid() -> None:
    """caffeinate must use -w so it dies with the collector, never outliving it."""
    text = LIB.read_text(encoding="utf-8")
    assert "caffeinate -ims -w" in text
    # -d/-u would keep the display awake; collection does not need that.
    assert "caffeinate -dimsu" not in text


def test_opt_out_is_respected() -> None:
    result = _run('hold_power_assertion $$ "test"', {"POWER_ASSERTION_ENABLED": "false"})
    assert result.returncode == 0
    assert "overgeslagen" in result.stdout


def test_never_fails_when_pid_is_dead() -> None:
    """A failed assertion must never take the collector down with it."""
    result = _run('hold_power_assertion 999999 "test"')
    assert result.returncode == 0
    assert "WAARSCHUWING" in result.stdout


def test_never_fails_when_pid_is_empty() -> None:
    result = _run('hold_power_assertion "" "test"')
    assert result.returncode == 0


def test_settings_probe_is_read_only() -> None:
    """assert_power_settings_sane must never mutate system power settings.

    Writing pmset needs sudo and is a deliberate owner action, so the helper
    may only detect and report, never change.
    """
    executable_lines = [
        line.strip() for line in LIB.read_text(encoding="utf-8").splitlines()
        if "pmset" in line
        and not line.strip().startswith("#")
        and "echo" not in line  # advisory text shown to the operator
    ]
    assert executable_lines, "expected at least one pmset probe"
    for line in executable_lines:
        assert ("command -v pmset" in line) or ("pmset -g" in line), (
            f"pmset call is not a read: {line}"
        )
        for write_flag in (" -a ", " -b ", " -c ", " -u "):
            assert write_flag not in line, f"pmset write flag in: {line}"


@pytest.mark.skipif(shutil.which("caffeinate") is None, reason="macOS only")
def test_assertion_starts_and_dies_with_target() -> None:
    """End-to-end: caffeinate appears while the target lives and exits with it."""
    target = subprocess.Popen(["sleep", "30"])
    try:
        result = _run(f'hold_power_assertion {target.pid} "itest"')
        assert result.returncode == 0
        assert "power-assertion actief" in result.stdout

        caffeinate_pid = int(result.stdout.split("caffeinate_pid=")[1].split()[0])
        assert os.kill(caffeinate_pid, 0) is None  # alive
    finally:
        target.terminate()
        target.wait()

    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            os.kill(caffeinate_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.2)
    else:
        pytest.fail("caffeinate outlived its target process")
