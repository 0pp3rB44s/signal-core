from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


@dataclass
class ParsedLLMResponse:
    raw_text: str
    summary: str = ""
    root_cause: str = ""
    files_to_modify: list[str] = field(default_factory=list)
    tests_to_run: list[str] = field(default_factory=list)
    risk: str = ""
    diff: str = ""
    approval_required: bool = True
    json_valid: bool = False
    error: str | None = None


def _extract_json(text: str) -> str:
    match = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]

    return ""


def parse_response(text: str) -> ParsedLLMResponse:
    parsed = ParsedLLMResponse(raw_text=text)
    json_text = _extract_json(text)

    if not json_text:
        parsed.error = "No JSON object found."
        return parsed

    try:
        data = json.loads(json_text)
    except Exception as exc:
        parsed.error = f"Invalid JSON: {exc}"
        return parsed

    parsed.summary = str(data.get("summary", "")).strip()
    parsed.root_cause = str(data.get("root_cause", "")).strip()
    parsed.files_to_modify = list(data.get("files_to_modify", []) or [])
    parsed.tests_to_run = list(data.get("tests_to_run", []) or [])
    parsed.risk = str(data.get("risk", "")).strip()
    parsed.diff = str(data.get("diff", "") or "")
    parsed.approval_required = bool(data.get("approval_required", True))
    parsed.json_valid = True

    return parsed
