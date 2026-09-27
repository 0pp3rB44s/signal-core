"""Runtime health analyzer for the Audit Engine."""

from __future__ import annotations

from typing import Any
from collections import Counter


MAX_RECENT_LINES = 500

INCIDENT_PATTERNS = {
    "dns_resolution": ["bitget_dns_resolution_failure", "failed to resolve", "name or service not known"],
    "request_exception": ["bitget_request_exception", "request exception"],
    "rate_limit": ["429", "rate limit", "too many requests"],
    "timeout": ["timeout", "timed out"],
    "disconnect": ["disconnect", "connection lost", "reconnect", "reconnected"],
}


def build_runtime_health_summary(logs: dict[str, str]) -> dict[str, Any]:
    combined = "\n".join(logs.values()) if logs else ""
    lines = combined.splitlines()
    recent_lines = lines[-MAX_RECENT_LINES:]
    recent_text = "\n".join(recent_lines)

    def count(*tokens: str) -> int:
        text = recent_text.lower()
        return sum(text.count(token.lower()) for token in tokens)

    errors = count("error", "traceback", "exception")
    warnings = count("warning", "warn")
    rate_limits = count("429", "rate limit", "too many requests")
    reconnects = count("reconnect", "reconnected", "disconnect", "connection lost")
    timeouts = count("timeout", "timed out")

    error_lines = [
        line.strip()
        for line in recent_lines
        if any(token in line.lower() for token in ("error", "exception", "traceback"))
    ]
    grouped_errors = Counter(error_lines)
    top_errors = grouped_errors.most_common(5)


    incident_counts: dict[str, int] = {}
    for incident, patterns in INCIDENT_PATTERNS.items():
        incident_counts[incident] = sum(
            1
            for line in recent_lines
            if any(pattern in line.lower() for pattern in patterns)
        )

    active_incidents = {k: v for k, v in incident_counts.items() if v > 0}
    dominant_issue = max(active_incidents.items(), key=lambda x: x[1])[0] if active_incidents else None

    score = 100
    score -= min(len(active_incidents) * 12, 36)
    score -= min(warnings * 2, 10)
    score -= min(rate_limits * 5, 20)
    score -= min(reconnects * 3, 15)
    score -= min(timeouts * 3, 15)
    score = max(0, score)

    if score >= 98:
        grade = "A+"
    elif score >= 90:
        grade = "A"
    elif score >= 80:
        grade = "B"
    elif score >= 70:
        grade = "C"
    else:
        grade = "D"

    return {
        "score": score,
        "grade": grade,
        "errors": errors,
        "warnings": warnings,
        "rate_limits": rate_limits,
        "reconnects": reconnects,
        "timeouts": timeouts,
        "incident_types": active_incidents,
        "dominant_issue": dominant_issue,
        "lines_analyzed": len(recent_lines),
        "top_errors": top_errors,
    }
