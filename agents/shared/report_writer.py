"""Write AI audit reports in JSON and Markdown formats."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPORT_DIR = Path("agents/reports")
JSON_REPORT = REPORT_DIR / "ai_audit_latest.json"
MARKDOWN_REPORT = REPORT_DIR / "ai_audit_latest.md"


def _list_section(items: list[str]) -> str:
    if not items:
        return "- NOT PROVEN\n"
    return "".join(f"- {item}\n" for item in items)


def render_markdown(audit: dict[str, Any]) -> str:
    validation_issues = audit.get("validation_issues", [])
    validation_block = ""
    if validation_issues:
        validation_block = "\n# VALIDATION_ISSUES\n" + _list_section(validation_issues)

    return (
        "# CGC AI AUDIT REPORT\n\n"
        "# SUMMARY\n"
        f"{audit.get('summary', 'NOT PROVEN')}\n\n"
        "# CRITICAL\n"
        f"{_list_section(audit.get('critical', []))}\n"
        "# HIGH\n"
        f"{_list_section(audit.get('high', []))}\n"
        "# MEDIUM\n"
        f"{_list_section(audit.get('medium', []))}\n"
        "# LOW\n"
        f"{_list_section(audit.get('low', []))}\n"
        "# ROOT_CAUSE\n"
        f"{audit.get('root_cause', 'NOT PROVEN')}\n\n"
        "# PATCH_CANDIDATES\n"
        f"{_list_section(audit.get('patch_candidates', []))}\n"
        "# FILES_TO_REVIEW\n"
        f"{_list_section(audit.get('files_to_review', []))}\n"
        "# RISK_IF_UNCHANGED\n"
        f"{audit.get('risk_if_unchanged', 'NOT PROVEN')}\n\n"
        "# CONFIDENCE\n"
        f"{audit.get('confidence', 0.0)}\n"
        f"{validation_block}"
    )


def write_reports(audit: dict[str, Any]) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    JSON_REPORT.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
    MARKDOWN_REPORT.write_text(render_markdown(audit), encoding="utf-8")
    return JSON_REPORT, MARKDOWN_REPORT
