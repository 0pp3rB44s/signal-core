from __future__ import annotations

import json


def normalize_json_text(text: str) -> str:
    raw = (text or "").strip()

    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    try:
        data = json.loads(raw)
        return json.dumps(data)
    except Exception:
        pass

    start = raw.find("{")
    if start < 0:
        return raw

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(raw)):
        char = raw[index]

        if escaped:
            escaped = False
            continue

        if char == "\\" and in_string:
            escaped = True
            continue

        if char == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = raw[start:index + 1]
                try:
                    data = json.loads(candidate)
                    return json.dumps(data)
                except Exception:
                    return candidate

    return raw
