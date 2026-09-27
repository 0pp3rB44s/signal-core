"""LAYER 1 (critical-risk gate) and the R2 power gate must fail closed.

These are deployment-gate tests, not trading tests. They run the real shell
functions from scripts/lib/env_guard.sh against synthetic registers, so the gate
can never silently drift from its documented meaning again.

Regression origin: the previous inline implementation aborted even when zero
Critical risks were open (`grep -c ... || echo 0` yields "0\\n0", which breaks
`[ -eq ]`), and it skipped the CRITICAL section entirely when no line-start
"- **Status:** OPEN" appeared anywhere in the file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
GUARD = REPO / "scripts" / "lib" / "env_guard.sh"

CRITICAL_HEADER = "# Risk register\n\n## CRITICAL\n\n"
TAIL = "\n## HIGH\n\n### R3 — some high risk\n\n- **Status:** OPEN.\n"


def run_layer1(register: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'. "{GUARD}"; guard_assert_critical_risks_cleared "{register}"'],
        capture_output=True,
        text=True,
    )


def write_register(tmp_path: Path, *critical_entries: str, tail: str = TAIL) -> Path:
    body = CRITICAL_HEADER + "\n".join(critical_entries) + tail
    path = tmp_path / "RISK_REGISTER.md"
    path.write_text(body, encoding="utf-8")
    return path


def entry(name: str, status: str) -> str:
    return f"### {name} — description here\n\n- **Status:** {status}\n"


# --- the gate blocks -----------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        "OPEN.",
        "OPEN by design — needs a live phase.",
        "PARTIALLY RESOLVED — condition X remains.",
        "MITIGATED",           # not a terminal status
        "probably fine",       # unrecognised
    ],
)
def test_non_terminal_critical_status_blocks(tmp_path, status):
    result = run_layer1(write_register(tmp_path, entry("R1", status)))
    assert result.returncode != 0, f"{status!r} must block deployment"
    assert "LAYER 1" in result.stderr


def test_critical_risk_without_any_status_line_fails_closed(tmp_path):
    register = write_register(tmp_path, "### R1 — description with no status\n")
    result = run_layer1(register)
    assert result.returncode != 0
    assert "NO_STATUS" in result.stderr


def test_one_open_among_several_still_blocks(tmp_path):
    register = write_register(
        tmp_path,
        entry("R1", "RESOLVED 2026-07-28 (commit abc)."),
        entry("R2", "OPEN."),
    )
    result = run_layer1(register)
    assert result.returncode != 0
    assert "R2:OPEN" in result.stderr
    assert "R1" not in result.stderr.split("->")[1].split("\n")[0]


def test_missing_register_fails_closed(tmp_path):
    result = run_layer1(tmp_path / "nope.md")
    assert result.returncode != 0


def test_register_without_critical_section_fails_closed(tmp_path):
    path = tmp_path / "RISK_REGISTER.md"
    path.write_text("# Risk register\n\n## HIGH\n\n### R3 — x\n\n- **Status:** OPEN.\n")
    result = run_layer1(path)
    assert result.returncode != 0


def test_open_critical_is_caught_even_without_line_start_form_anywhere(tmp_path):
    """The old gate short-circuited to PASS here — an open Critical risk unseen."""
    path = tmp_path / "RISK_REGISTER.md"
    path.write_text(
        "## CRITICAL\n\n### R1 — x\n\nSome prose. **Status:** OPEN.\n\n"
        "## HIGH\n\n### R3 — y\n\nprose **Status:** OPEN.\n"
    )
    result = run_layer1(path)
    assert result.returncode != 0, "inline Critical OPEN must still block"
    assert "R1:OPEN" in result.stderr


# --- the gate passes -----------------------------------------------------


def test_all_critical_resolved_passes_even_with_open_high_risks(tmp_path):
    """The exact regression: zero open Critical, open HIGH present -> must PASS."""
    register = write_register(
        tmp_path,
        entry("R1", "RESOLVED 2026-07-28 — commit abc, agent com.cgc.live."),
        entry("R2", "RESOLVED 2026-07-28 — pmset verified at launch."),
    )
    result = run_layer1(register)
    assert result.returncode == 0, f"must pass; stderr={result.stderr}"
    assert "LAYER 1 OK" in result.stdout


def test_explicit_owner_acceptance_passes(tmp_path):
    register = write_register(tmp_path, entry("R1", "ACCEPTED by owner 2026-07-28."))
    result = run_layer1(register)
    assert result.returncode == 0
    assert "LAYER 1 OK" in result.stdout


def test_status_bold_variants_are_understood(tmp_path):
    register = write_register(tmp_path, "### R1 — x\n\n- **Status:** **RESOLVED 2026-07-28**\n")
    assert run_layer1(register).returncode == 0


def test_the_live_repository_register_is_parseable(tmp_path):
    """Whatever the real register says, the gate must reach a decision, not crash."""
    result = run_layer1(REPO / "docs" / "RISK_REGISTER.md")
    assert result.returncode in (0, 90), f"unexpected rc={result.returncode}: {result.stderr}"
    assert "LAYER 1" in (result.stdout + result.stderr)


# --- R2 power gate -------------------------------------------------------


def _run_power(pmset_stub: str) -> subprocess.CompletedProcess:
    """Run guard_assert_power_continuous against a stubbed pmset."""
    script = f"""
    pmset() {{ printf '%s\\n' "{pmset_stub}"; }}
    export -f pmset 2>/dev/null || true
    . "{GUARD}"
    guard_assert_power_continuous
    """
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def test_power_gate_blocks_when_host_can_idle_sleep():
    result = _run_power(" sleep 1")
    assert result.returncode != 0
    assert "R2" in result.stderr
    assert "sudo pmset" in result.stderr


def test_power_gate_passes_when_idle_sleep_disabled():
    result = _run_power(" sleep 0")
    assert result.returncode == 0, result.stderr
    assert "power OK" in result.stdout


def test_power_gate_passes_when_sleep_is_disabled_outright():
    result = _run_power(" disablesleep 1")
    assert result.returncode == 0, result.stderr
