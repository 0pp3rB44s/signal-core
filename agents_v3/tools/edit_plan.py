from __future__ import annotations

import json
from dataclasses import dataclass

from agents_v3.tools.repository_editor import EditResult, replace_once


@dataclass
class EditPlan:
    operation: str
    file_path: str
    old_text: str
    new_text: str


@dataclass
class EditPlanResult:
    valid: bool
    message: str
    edit_result: EditResult | None = None


def execute_edit_plan(response_text: str, allowed_paths: list[str] | None = None) -> EditPlanResult:
    try:
        payload = json.loads(response_text)
    except Exception as exc:
        return EditPlanResult(False, f"Invalid JSON: {exc}")

    raw_plan = payload.get("edit_plan")
    if not isinstance(raw_plan, dict):
        return EditPlanResult(False, "No edit_plan object found.")

    plan = EditPlan(
        operation=str(raw_plan.get("operation", "")),
        file_path=str(raw_plan.get("file_path", "")),
        old_text=str(raw_plan.get("old_text", "")),
        new_text=str(raw_plan.get("new_text", "")),
    )

    if plan.operation != "replace_once":
        return EditPlanResult(False, "Only replace_once is currently allowed.")

    if not plan.file_path:
        return EditPlanResult(False, "edit_plan.file_path is missing.")

    if allowed_paths is not None and plan.file_path not in allowed_paths:
        return EditPlanResult(
            False,
            f"edit_plan.file_path is not selected or does not exist: {plan.file_path}",
        )

    if not plan.old_text:
        return EditPlanResult(False, "edit_plan.old_text is missing.")

    if plan.file_path.startswith((".git/", ".venv/")):
        return EditPlanResult(False, "Protected path rejected.")

    if plan.file_path in {".env", "secrets.env"}:
        return EditPlanResult(False, "Secrets file rejected.")

    result = replace_once(
        file_path=plan.file_path,
        old_text=plan.old_text,
        new_text=plan.new_text,
        apply=False,
    )

    return EditPlanResult(
        valid=result.success and result.changed and bool(result.diff.strip()),
        message=result.message,
        edit_result=result,
    )
