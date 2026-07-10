from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass
class GitResult:
    success: bool
    command: str
    output: str
    return_code: int


def run_git(args: list[str]) -> GitResult:
    command_parts = ["git", *args]
    command = " ".join(command_parts)

    completed = subprocess.run(
        command_parts,
        capture_output=True,
        text=True,
        timeout=60,
    )

    output = (completed.stdout or "") + (completed.stderr or "")

    return GitResult(
        success=completed.returncode == 0,
        command=command,
        output=output.strip(),
        return_code=completed.returncode,
    )


def git_status() -> GitResult:
    return run_git(["status", "--short"])


def git_diff_stat() -> GitResult:
    return run_git(["diff", "--stat"])


def git_diff_name_only() -> GitResult:
    return run_git(["diff", "--name-only"])
