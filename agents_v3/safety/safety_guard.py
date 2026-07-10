from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


FORBIDDEN_FILES = {
    ".env",
    ".env.local",
    ".env.production",
}

DANGEROUS_KEYWORDS = (
    "api_key",
    "secret",
    "password",
    "leverage",
    "disable_sl",
    "disable_tp",
    "remove_stop",
    "live_trade",
)

# Paths the agent may patch autonomously (strategy/setup tuning, docs,
# its own code). Everything else — risk limits, execution safeguards,
# exchange clients, app config — needs an explicit human approval step,
# per AGENTS.md.
AUTONOMOUS_PREFIXES = (
    "strategies/",
    "planning/",
    "docs/",
    "agents_v3/",
    "tests/",
)


def files_requiring_human_approval(files: list[str]) -> list[str]:
    return [f for f in files if not f.startswith(AUTONOMOUS_PREFIXES)]


@dataclass
class SafetyResult:
    allowed: bool
    reasons: list[str]


def _is_forbidden_file(file: str) -> bool:
    name = Path(file).name
    return (
        file in FORBIDDEN_FILES
        or name == ".env"
        or name.startswith(".env.")
        or file.endswith(".env")
    )


def _lines_to_scan(patch_text: str) -> list[str]:
    """For unified diffs only scan added lines; existing bot code legitimately
    contains words like 'leverage', and context lines would block every patch
    that touches core execution files. Non-diff text is scanned in full."""
    lines = patch_text.splitlines()
    is_diff = any(line.startswith(("--- ", "+++ ", "@@")) for line in lines)
    if not is_diff:
        return lines
    return [
        line[1:]
        for line in lines
        if line.startswith("+") and not line.startswith("+++")
    ]


def check_patch_safety(files_to_modify: list[str], patch_text: str = "") -> SafetyResult:
    reasons: list[str] = []

    for file in files_to_modify:
        if _is_forbidden_file(file):
            reasons.append(f"Forbidden file: {file}")

    scan_text = "\n".join(_lines_to_scan(patch_text)).lower()
    for keyword in DANGEROUS_KEYWORDS:
        if keyword in scan_text:
            reasons.append(f"Dangerous keyword in patch: {keyword}")

    return SafetyResult(
        allowed=len(reasons) == 0,
        reasons=reasons,
    )
