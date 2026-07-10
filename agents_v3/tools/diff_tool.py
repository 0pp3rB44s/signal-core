from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class DiffValidationResult:
    valid: bool
    files: list[str]
    reasons: list[str]


def extract_diff(text: str) -> str:
    match = re.search(r"```diff\s*(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()

    if text.strip().startswith("diff --git"):
        return text.strip()

    return ""


def validate_unified_diff(diff_text: str) -> DiffValidationResult:
    reasons: list[str] = []
    files: list[str] = []

    if not diff_text.strip():
        reasons.append("No unified diff found.")
        return DiffValidationResult(False, files, reasons)

    if "diff --git" not in diff_text and "--- " not in diff_text and "+++ " not in diff_text:
        reasons.append("Diff does not look like a unified diff.")

    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            file_path = line.replace("+++ b/", "").strip()
            if file_path not in files:
                files.append(file_path)

    if not files:
        reasons.append("No target files found in diff.")

    return DiffValidationResult(
        valid=len(reasons) == 0,
        files=files,
        reasons=reasons,
    )
