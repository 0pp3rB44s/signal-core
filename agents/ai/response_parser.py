"""Parse and validate JSON-first AI audit responses."""

from __future__ import annotations

import json
from typing import Any

FORBIDDEN_TERMS = {
    "1234567890",
    "bot.log",
    "trade.log",
    "ADA/USDT",
    "BTC/USDT",
    "BTCUSD",
}

REQUIRED_FIELDS = {
    "summary": str,
    "critical": list,
    "high": list,
    "medium": list,
    "low": list,
    "root_cause": str,
    "patch_candidates": list,
    "files_to_review": list,
    "risk_if_unchanged": str,
    "confidence": (int, float),
}

LIST_FIELDS = {
    "critical",
    "high",
    "medium",
    "low",
    "patch_candidates",
    "files_to_review",
}


def normalize(text: str) -> str:
    return text.strip()


def _extract_json_object(text: str) -> str:
    stripped = normalize(text)

    if stripped.startswith("```json"):
        stripped = stripped.removeprefix("```json").strip()
    if stripped.startswith("```"):
        stripped = stripped.removeprefix("```").strip()
    if stripped.endswith("```"):
        stripped = stripped.removesuffix("```").strip()

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return stripped

    return stripped[start : end + 1]


def parse_json_response(text: str) -> tuple[dict[str, Any] | None, list[str]]:
    raw_json = _extract_json_object(text)

    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        return None, [f"json_parse_error:{exc.msg}"]

    if not isinstance(parsed, dict):
        return None, ["json_root_not_object"]

    return parsed, []


def validate_audit(audit: dict[str, Any] | None, allowed_files: set[str]) -> tuple[bool, list[str]]:
    if audit is None:
        return False, ["audit_missing"]

    issues: list[str] = []

    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in audit:
            issues.append(f"missing_field:{field}")
            continue
        if not isinstance(audit[field], expected_type):
            issues.append(f"invalid_type:{field}")

    for field in LIST_FIELDS:
        value = audit.get(field)
        if isinstance(value, list) and not all(isinstance(item, str) for item in value):
            issues.append(f"invalid_list_items:{field}")

    confidence = audit.get("confidence")
    if isinstance(confidence, (int, float)) and not 0.0 <= float(confidence) <= 1.0:
        issues.append("confidence_out_of_range")

    files_to_review = audit.get("files_to_review", [])
    if isinstance(files_to_review, list):
        unauthorized_files = sorted(set(files_to_review) - allowed_files)
        issues.extend(f"unauthorized_file:{filename}" for filename in unauthorized_files)

    serialized = json.dumps(audit, ensure_ascii=False)
    issues.extend(f"forbidden_term:{term}" for term in FORBIDDEN_TERMS if term in serialized)

    return len(issues) == 0, issues


def invalid_audit(issues: list[str]) -> dict[str, Any]:
    return {
        "summary": "INVALID_AI_OUTPUT",
        "critical": ["The AI audit output failed validation."],
        "high": [],
        "medium": [],
        "low": [],
        "root_cause": "The model returned malformed JSON, missing fields, forbidden terms, or unauthorized file references.",
        "patch_candidates": [],
        "files_to_review": [],
        "risk_if_unchanged": "Invalid audit output must not be used for patch decisions.",
        "confidence": 0.0,
        "validation_issues": issues,
    }
