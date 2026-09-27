"""Rule-based audit checks for CGC Audit Engine V2."""

from __future__ import annotations

from typing import Any

import csv
from io import StringIO
from agents.shared.audit.trade_integrity import (
    analyze_headers,
    analyze_rows,
    summarize_findings,
    analyze_lifecycle,
    build_trade_integrity_summary,
)
from agents.shared.audit.performance import build_performance_summary
from agents.shared.audit.runtime_health import build_runtime_health_summary
from agents.shared.audit.scoring import build_overall_score

ERROR_KEYWORDS = ("ERROR", "Traceback", "Exception")
WARNING_KEYWORDS = ("WARNING", "WARN")


def run_rule_audit(context: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Run deterministic audit checks without using AI."""

    critical: list[str] = []
    high: list[str] = []
    medium: list[str] = []
    low: list[str] = []
    trade_integrity_summary: dict[str, Any] = {}
    performance_summary: dict[str, Any] = {}
    runtime_health_summary: dict[str, Any] = {}
    overall_summary: dict[str, Any] = {}

    logs = context.get("logs", {})
    combined_logs = "\n".join(logs.values())
    runtime_health_summary = build_runtime_health_summary(logs)

    if any(token in combined_logs for token in ERROR_KEYWORDS):
        critical.append("Runtime logs contain ERROR/Traceback/Exception entries.")

    if any(token in combined_logs for token in WARNING_KEYWORDS):
        medium.append("Runtime logs contain warning entries.")

    if not context.get("dataset"):
        high.append("Trade dataset is missing.")
    else:
        # Try to parse and analyze the first available dataset
        datasets = context.get("dataset", {})
        csv_text = None
        if isinstance(datasets, dict):
            for k, v in datasets.items():
                if v:
                    csv_text = v
                    break
        elif isinstance(datasets, str):
            csv_text = datasets
        findings = []
        try:
            if csv_text:
                lines = csv_text.splitlines()

                while lines and (
                    not lines[0].strip()
                    or lines[0].startswith("=====")
                ):
                    lines.pop(0)

                cleaned_csv = "\n".join(lines)

                reader = csv.DictReader(StringIO(cleaned_csv))
                
                if not reader.fieldnames:
                    raise ValueError("Dataset header could not be detected.")
                rows = list(reader)
                
                findings += analyze_headers(reader.fieldnames or [])
                findings += analyze_rows(rows)
                lifecycle_findings = analyze_lifecycle(rows)
                findings += lifecycle_findings
                trade_integrity_summary = build_trade_integrity_summary(rows, findings)
                performance_summary = build_performance_summary(rows)
                overall_summary = build_overall_score(
                    trade_integrity_summary,
                    performance_summary,
                    runtime_health_summary,
                )
                summarized = summarize_findings(findings)
                critical.extend(summarized.get("critical", []))
                high.extend(summarized.get("high", []))
                medium.extend(summarized.get("medium", []))
                low.extend(summarized.get("low", []))
        except Exception:
            high.append("Trade dataset could not be parsed.")

    if not context.get("roadmap"):
        medium.append("Roadmap documentation is missing.")

    if not context.get("settings"):
        medium.append("Runtime/settings configuration is missing.")

    summary = (
        f"Rule audit completed: {len(critical)} critical, "
        f"{len(high)} high, {len(medium)} medium, {len(low)} low findings."
    )

    return {
        "summary": summary,
        "critical": critical,
        "high": high,
        "medium": medium,
        "low": low,
        "root_cause": "Rule-based audit completed.",
        "patch_candidates": [],
        "files_to_review": [],
        "risk_if_unchanged": "Operational issues may remain undetected until runtime.",
        "confidence": 1.0,
        "audit_source": "rule_engine_v1",
        "trade_integrity": trade_integrity_summary,
        "performance": performance_summary,
        "runtime_health": runtime_health_summary,
        "overall": overall_summary,
    }