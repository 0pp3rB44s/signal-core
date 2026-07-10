from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agents_v3.safety.safety_guard import files_requiring_human_approval
from agents_v3.tools.apply_tool import apply_unified_diff
from agents_v3.tools.rollback_manager import rollback_files
from agents_v3.tools.runtime_manager import restart_bot
from agents_v3.tools.test_runner import run_tests


PENDING_PATCH = Path(".cgcagent_pending.patch")
JOURNAL_PATH = Path("docs/JOURNAL.md")


@dataclass
class LifecycleResult:
    success: bool
    applied: bool
    tests_passed: bool
    rollback_performed: bool
    runtime_restarted: bool
    patched_files: list[str]
    message: str


def extract_files_from_diff(diff_text: str) -> list[str]:
    files: list[str] = []

    for line in diff_text.splitlines():
        match = re.match(r"^\+\+\+ b/(.+)$", line)
        if not match:
            continue

        file_path = match.group(1).strip()

        if file_path == "/dev/null":
            continue

        if file_path not in files:
            files.append(file_path)

    return files


def _write_journal(
    *,
    status: str,
    files: list[str],
    tests_passed: bool,
    runtime_restarted: bool,
    details: str,
) -> None:
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    file_list = ", ".join(files) if files else "none"

    entry = (
        f"\n## CGCAgent Patch Lifecycle — {timestamp}\n\n"
        f"- Status: {status}\n"
        f"- Files: {file_list}\n"
        f"- Tests passed: {tests_passed}\n"
        f"- Runtime restarted: {runtime_restarted}\n"
        f"- Details: {details}\n"
    )

    with JOURNAL_PATH.open("a", encoding="utf-8") as handle:
        handle.write(entry)


def run_pending_patch_lifecycle(human_approved: bool = False) -> LifecycleResult:
    if not PENDING_PATCH.exists():
        return LifecycleResult(
            success=False,
            applied=False,
            tests_passed=False,
            rollback_performed=False,
            runtime_restarted=False,
            patched_files=[],
            message="No pending patch found.",
        )

    diff_text = PENDING_PATCH.read_text(encoding="utf-8")
    patched_files = extract_files_from_diff(diff_text)

    if not patched_files:
        return LifecycleResult(
            success=False,
            applied=False,
            tests_passed=False,
            rollback_performed=False,
            runtime_restarted=False,
            patched_files=[],
            message="Pending patch contains no valid target files.",
        )

    if not human_approved:
        gated = files_requiring_human_approval(patched_files)
        if gated:
            _write_journal(
                status="BLOCKED_NEEDS_HUMAN_APPROVAL",
                files=patched_files,
                tests_passed=False,
                runtime_restarted=False,
                details=f"Autonomous apply refused for protected paths: {', '.join(gated)}. Run: python -m agents_v3.cli patch apply --approve",
            )
            return LifecycleResult(
                success=False,
                applied=False,
                tests_passed=False,
                rollback_performed=False,
                runtime_restarted=False,
                patched_files=patched_files,
                message=(
                    "Patch touches protected paths and needs human approval: "
                    + ", ".join(gated)
                    + ". Review .cgcagent_pending.patch and run: python -m agents_v3.cli patch apply --approve"
                ),
            )

    apply_result = apply_unified_diff(diff_text)

    if not apply_result.applied:
        _write_journal(
            status="APPLY_FAILED",
            files=patched_files,
            tests_passed=False,
            runtime_restarted=False,
            details=apply_result.output or "git apply failed",
        )

        return LifecycleResult(
            success=False,
            applied=False,
            tests_passed=False,
            rollback_performed=False,
            runtime_restarted=False,
            patched_files=patched_files,
            message=apply_result.output or "Patch apply failed.",
        )

    test_result = run_tests([])

    if not test_result.success:
        rollback = rollback_files(patched_files)

        _write_journal(
            status="TESTS_FAILED_ROLLBACK",
            files=patched_files,
            tests_passed=False,
            runtime_restarted=False,
            details=(
                f"Tests failed with return code {test_result.return_code}. "
                f"Rollback success: {rollback.success}."
            ),
        )

        if rollback.success:
            PENDING_PATCH.unlink(missing_ok=True)

        return LifecycleResult(
            success=False,
            applied=True,
            tests_passed=False,
            rollback_performed=rollback.success,
            runtime_restarted=False,
            patched_files=patched_files,
            message="Tests failed; rollback executed.",
        )

    runtime = restart_bot()

    _write_journal(
        status="SUCCESS" if runtime.success else "RUNTIME_RESTART_FAILED",
        files=patched_files,
        tests_passed=True,
        runtime_restarted=runtime.success,
        details=runtime.output or "No runtime output.",
    )

    if runtime.success:
        PENDING_PATCH.unlink(missing_ok=True)

    return LifecycleResult(
        success=runtime.success,
        applied=True,
        tests_passed=True,
        rollback_performed=False,
        runtime_restarted=runtime.success,
        patched_files=patched_files,
        message=(
            "Patch applied, tests passed and bot restarted."
            if runtime.success
            else "Patch applied and tests passed, but bot restart failed."
        ),
    )
