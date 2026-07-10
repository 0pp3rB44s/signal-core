from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass


@dataclass
class TestResult:
    success: bool
    command: str
    output: str
    return_code: int


def run_tests(test_targets: list[str] | None = None) -> TestResult:
    targets = test_targets or ["tests"]
    # sys.executable keeps tests inside the same interpreter/venv as the
    # agent; a bare "python" can resolve to a system install without the
    # bot's dependencies and make every auto-run rollback.
    command_parts = [sys.executable, "-m", "pytest", *targets, "-q"]
    command = " ".join(command_parts)

    try:
        completed = subprocess.run(
            command_parts,
            capture_output=True,
            text=True,
            timeout=120,
        )

        output = (completed.stdout or "") + (completed.stderr or "")

        return TestResult(
            success=completed.returncode == 0,
            command=command,
            output=output.strip(),
            return_code=completed.returncode,
        )

    except Exception as exc:
        return TestResult(
            success=False,
            command=command,
            output=f"Test runner error: {exc}",
            return_code=1,
        )
