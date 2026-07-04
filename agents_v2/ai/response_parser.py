"""Parse and validate JSON-first CGC Audit Engine V2 responses."""

import json
from typing import Any

def normalize(text: str) -> str:
    return text.strip()

def _extract_json_object(text: str) -> str:
    stripped = normalize(text)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return stripped
    return stripped[start:end + 1]

def parse_json_response(text: str) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        parsed = json.loads(_extract_json_object(text))
    except json.JSONDecodeError as exc:
        return None, [f"json_parse_error:{exc.msg}"]
    if not isinstance(parsed, dict):
        return None, ["json_root_not_object"]
    return parsed, []

def validate_audit(audit: dict[str, Any] | None, allowed_files: set[str]) -> tuple[bool, list[str]]:
    if audit is None:
        return False, ["audit_missing"]

    issues = []
    required = {
        "summary": str,
        "critical": list,
        "high": list,
        "medium": list,
        "low": list,
        "root_cause": str,
        "patch_candidates": list,
        "files_to_review": list,
        "risk_if_unchanged": str,
    }

    for field, expected in required.items():
        if field not in audit:
            issues.append(f"missing_field:{field}")
        elif not isinstance(audit[field], expected):
            issues.append(f"invalid_type:{field}")

    for field in ["critical", "high", "medium", "low", "patch_candidates", "files_to_review"]:
        if audit.get(field) == ["NOT PROVEN"]:
            audit[field] = []

    confidence = audit.get("confidence", 0.0)
    if isinstance(confidence, str):
        if confidence == "NOT PROVEN":
            confidence = 0.0
        else:
            try:
                confidence = float(confidence)
            except ValueError:
                issues.append("invalid_type:confidence")
                confidence = 0.0

    audit["confidence"] = confidence
    if not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
        issues.append("confidence_out_of_range")

    unauthorized = sorted(set(audit.get("files_to_review", [])) - allowed_files)
    issues.extend(f"unauthorized_file:{f}" for f in unauthorized)

    return len(issues) == 0, issues

def invalid_audit(issues: list[str]) -> dict[str, Any]:
    return {
        "summary": "INVALID_AI_OUTPUT",
        "critical": ["The AI audit output failed validation."],
        "high": [],
        "medium": [],
        "low": [],
        "root_cause": "Malformed JSON, missing fields, invalid types, or unauthorized file references.",
        "patch_candidates": [],
        "files_to_review": [],
        "risk_if_unchanged": "Do not use this audit for patch decisions.",
        "confidence": 0.0,
        "validation_issues": issues,
    }
