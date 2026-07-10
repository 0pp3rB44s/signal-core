from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass
class RollbackResult:
    success: bool
    output: str
    return_code: int


def rollback_files(files: list[str]) -> RollbackResult:
    if not files:
        return RollbackResult(False, "No files provided for rollback.", 1)

    completed = subprocess.run(
        ["git", "checkout", "--", *files],
        capture_output=True,
        text=True,
        timeout=60,
    )

    output = (completed.stdout or "") + (completed.stderr or "")

    return RollbackResult(
        success=completed.returncode == 0,
        output=output.strip(),
        return_code=completed.returncode,
    )
