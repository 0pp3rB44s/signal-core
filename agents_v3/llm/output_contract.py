from __future__ import annotations

import json
from dataclasses import dataclass


REQUIRED_FIELDS = {
    "summary",
    "root_cause",
    "files_to_modify",
    "tests_to_run",
    "risk",
    "diff",
    "approval_required",
}


@dataclass
class ContractValidation:
    valid: bool
    reasons: list[str]


def validate_contract(json_text: str) -> ContractValidation:
    reasons: list[str] = []

    try:
        data = json.loads(json_text)
    except Exception as exc:
        return ContractValidation(False, [f"Invalid JSON: {exc}"])

    missing = REQUIRED_FIELDS - set(data.keys())
    if missing:
        reasons.append(f"Missing fields: {sorted(missing)}")

    if not isinstance(data.get("files_to_modify", []), list):
        reasons.append("files_to_modify must be a list")

    if not isinstance(data.get("tests_to_run", []), list):
        reasons.append("tests_to_run must be a list")

    if data.get("risk") not in {"LOW", "MEDIUM", "HIGH", ""}:
        reasons.append("risk must be LOW, MEDIUM, HIGH, or empty")

    if not isinstance(data.get("approval_required", True), bool):
        reasons.append("approval_required must be boolean")

    edit_plan = data.get("edit_plan", {})
    if edit_plan is not None and not isinstance(edit_plan, dict):
        reasons.append("edit_plan must be an object")

    if isinstance(edit_plan, dict) and edit_plan:
        required_edit_fields = {"operation", "file_path", "old_text", "new_text"}
        missing_edit_fields = required_edit_fields - set(edit_plan.keys())
        if missing_edit_fields:
            reasons.append(
                f"edit_plan missing fields: {sorted(missing_edit_fields)}"
            )

    return ContractValidation(len(reasons) == 0, reasons)
