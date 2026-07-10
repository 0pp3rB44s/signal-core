from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Subtask:
    order: int
    title: str
    instruction: str
    requires_approval: bool = True


def decompose_task(task: str) -> list[Subtask]:
    task_lower = task.lower().strip()

    if "orchestrator" in task_lower and "lifecycle" in task_lower:
        return [
            Subtask(
                order=1,
                title="Add lifecycle import",
                instruction=(
                    "Modify only agents_v3/orchestrator/orchestrator.py. "
                    "Add the import for "
                    "agents_v3.lifecycle.patch_lifecycle."
                    "run_pending_patch_lifecycle. "
                    "Use one minimal replace_once edit."
                ),
            ),
            Subtask(
                order=2,
                title="Delegate patch approval",
                instruction=(
                    "Modify only agents_v3/orchestrator/orchestrator.py. "
                    "Replace only the approved branch inside mode patch "
                    "with a call to run_pending_patch_lifecycle(). "
                    "Keep the dry-run branch unchanged. "
                    "Use one exact replace_once edit."
                ),
            ),
            Subtask(
                order=3,
                title="Delegate auto approval",
                instruction=(
                    "Modify only agents_v3/orchestrator/orchestrator.py. "
                    "Replace only the apply, test, rollback and restart logic "
                    "inside approved auto mode with "
                    "run_pending_patch_lifecycle(). "
                    "Use one exact replace_once edit."
                ),
            ),
            Subtask(
                order=4,
                title="Remove obsolete imports",
                instruction=(
                    "Modify only agents_v3/orchestrator/orchestrator.py. "
                    "Remove imports that became unused after lifecycle delegation. "
                    "Do not alter behaviour. Use one minimal replace_once edit."
                ),
            ),
        ]

    return [
        Subtask(
            order=1,
            title="Inspect target",
            instruction=f"Inspect the repository for this task: {task}",
            requires_approval=False,
        ),
        Subtask(
            order=2,
            title="Create minimal patch",
            instruction=(
                f"Implement one minimal safe change for this task: {task}. "
                "Use one exact replace_once edit."
            ),
        ),
        Subtask(
            order=3,
            title="Verify",
            instruction=(
                f"Verify tests, runtime health and documentation for: {task}"
            ),
            requires_approval=False,
        ),
    ]
