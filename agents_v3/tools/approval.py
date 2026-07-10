from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ApprovalResult:
    approved: bool
    reason: str


def require_approval(approved: bool) -> ApprovalResult:
    if approved:
        return ApprovalResult(
            approved=True,
            reason="Human approval provided.",
        )

    return ApprovalResult(
        approved=False,
        reason="Human approval missing. Patch may not be applied.",
    )
