from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ApplyResult:
    success: bool
    applied: bool
    output: str
    return_code: int


def apply_unified_diff(diff_text: str) -> ApplyResult:
    patch_file = Path(".cgcagent_pending.patch")
    patch_file.write_text(diff_text)

    completed = subprocess.run(
        ["git", "apply", str(patch_file)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    output = (completed.stdout or "") + (completed.stderr or "")

    return ApplyResult(
        success=completed.returncode == 0,
        applied=completed.returncode == 0,
        output=output.strip(),
        return_code=completed.returncode,
    )
