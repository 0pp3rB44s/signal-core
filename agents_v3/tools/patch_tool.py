from __future__ import annotations

from dataclasses import dataclass

from agents_v3.safety.safety_guard import check_patch_safety


@dataclass
class PatchResult:
    success: bool
    applied: bool
    message: str
    safety_reasons: list[str]


def dry_run_patch(files_to_modify: list[str], patch_text: str) -> PatchResult:
    safety = check_patch_safety(files_to_modify, patch_text)

    if not safety.allowed:
        return PatchResult(
            success=False,
            applied=False,
            message="Patch blocked by safety guard.",
            safety_reasons=safety.reasons,
        )

    return PatchResult(
        success=True,
        applied=False,
        message="Patch accepted in dry-run. No files changed.",
        safety_reasons=[],
    )
