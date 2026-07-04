"""Write CGC Audit Engine V2 reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPORT_DIR = Path("agents_v2/reports")
JSON_REPORT = REPORT_DIR / "audit.json"
MARKDOWN_REPORT = REPORT_DIR / "audit.md"


def _list_section(items: list[str]) -> str:
    if not items:
        return "- NOT PROVEN\n"
    return "".join(f"- {item}\n" for item in items)


def _kv_section(title: str, data: dict[str, Any]) -> str:
    if not data:
        return ""
    lines = [f"# {title}\n"]
    for key, value in data.items():
        lines.append(f"- **{key}**: {value}\n")
    lines.append("\n")
    return "".join(lines)


def _format_mapping(title: str, data: dict[str, Any]) -> str:
    if not data:
        return ""
    lines = [f"### {title}\n"]
    width = max(len(str(k)) for k in data.keys()) if data else 0
    for key, value in data.items():
        lines.append(f"- {str(key).replace('_', ' ').title():<{width}} : {value}\n")
    lines.append("\n")
    return "".join(lines)


def _format_pairs(title: str, items: Any) -> str:
    if not items:
        return ""
    lines = [f"### {title}\n"]
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            lines.append(f"- {item[0]}: {item[1]}\n")
        else:
            lines.append(f"- {item}\n")
    lines.append("\n")
    return "".join(lines)


def _format_module(title: str, data: dict[str, Any]) -> str:
    if not data:
        return ""
    special = {"contributors", "incident_types", "best_symbols", "worst_symbols", "best_strategies", "worst_strategies", "top_errors"}
    lines = [f"# {title}\n"]
    for key, value in data.items():
        if key in special:
            continue
        lines.append(f"- **{key.replace('_', ' ').title()}**: {value}\n")
    lines.append("\n")
    if "contributors" in data:
        lines.append(_format_mapping("Contributors", data["contributors"]))
    if "incident_types" in data:
        lines.append(_format_mapping("Incident Types", data["incident_types"]))
    for k,label in [("best_strategies","Best Strategies"),("worst_strategies","Worst Strategies"),("best_symbols","Best Symbols"),("worst_symbols","Worst Symbols"),("top_errors","Top Errors")]:
        if k in data:
            lines.append(_format_pairs(label, data[k]))
    return "".join(lines)


# Morning Briefing helpers

def _status_banner(score: int) -> str:
    if score >= 90:
        return "🟢 HEALTHY"
    if score >= 70:
        return "🟡 WARNING"
    return "🔴 CRITICAL"


def _morning_briefing(audit: dict[str, Any]) -> str:
    overall = audit.get("overall", {})
    performance = audit.get("performance", {})
    runtime = audit.get("runtime_health", {})
    trade = audit.get("trade_integrity", {})

    # Parse overall_score as int, fallback to 0 if missing or not int
    try:
        overall_score = int(overall.get("overall_score", 0))
    except Exception:
        overall_score = 0
    overall_grade = overall.get("overall_grade", "-")
    primary_risk = overall.get("primary_risk", "Unknown")
    primary_strength = overall.get("primary_strength", "Unknown")

    # Executive Summary: build 3 sentences
    trade_integrity_score = trade.get("score", 0)
    try:
        trade_integrity_score = int(trade_integrity_score)
    except Exception:
        trade_integrity_score = 0
    profit_factor = performance.get("profit_factor", 0)
    try:
        profit_factor = float(profit_factor)
    except Exception:
        profit_factor = 0.0
    runtime_score = runtime.get("score", 0)
    try:
        runtime_score = int(runtime_score)
    except Exception:
        runtime_score = 0

    # 1. Trade integrity
    if trade_integrity_score >= 90:
        integrity_sentence = "✅ Dataset integrity is healthy and suitable for analysis."
    else:
        integrity_sentence = "⚠️ Dataset integrity requires attention."
    # 2. Performance
    if profit_factor >= 1:
        perf_sentence = "✅ Trading performance is profitable."
    else:
        perf_sentence = "❌ Trading performance is currently losing money."
    # 3. Runtime
    if runtime_score >= 80:
        runtime_sentence = "✅ Runtime stability is good."
    else:
        runtime_sentence = "⚠️ Runtime stability requires attention."

    # Recommended Actions
    actions = []
    if profit_factor < 1:
        actions.append("Improve exit logic and risk/reward before optimizing entries.")
    dominant_issue = runtime.get("dominant_issue")
    if dominant_issue:
        actions.append(f"Investigate runtime issue: {dominant_issue}.")
    actions.append("Continue monitoring daily audit trends.")

    # Build recommended actions as numbered list
    recommended_actions = ""
    for idx, action in enumerate(actions, 1):
        recommended_actions += f"{idx}. {action}\n"

    # Build the full morning briefing
    lines = [
        "# CGC BOT MORNING BRIEFING\n\n",
        f"{_status_banner(overall_score)}\n\n",
        f"Overall Health: {overall_score}/100 ({overall_grade})\n\n",
        "Primary Risk\n",
        f"{primary_risk}\n\n",
        "Primary Strength\n",
        f"{primary_strength}\n\n",
        "Executive Summary\n",
        f"{integrity_sentence}\n",
        f"{perf_sentence}\n",
        f"{runtime_sentence}\n\n",
        "Recommended Actions\n",
        f"{recommended_actions}",
        "--------------------------------------------------\n",
    ]
    return "".join(lines)


def render_markdown(audit: dict[str, Any]) -> str:
    """Render an audit dictionary as Markdown."""
    validation_issues = audit.get("validation_issues", [])
    validation_block = ""
    if validation_issues:
        validation_block = "\n# VALIDATION_ISSUES\n" + _list_section(validation_issues)

    trade_integrity = audit.get("trade_integrity", {})
    performance = audit.get("performance", {})
    runtime_health = audit.get("runtime_health", {})
    overall = audit.get("overall", {})

    return (
        _morning_briefing(audit)
        + "# CGC AI AUDIT REPORT V2\n\n"
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
        f"{_list_section(audit.get('patch_candidates', []))}"
        "# FILES_TO_REVIEW\n"
        f"{_list_section(audit.get('files_to_review', []))}"
        "# RISK_IF_UNCHANGED\n"
        f"{audit.get('risk_if_unchanged', 'NOT PROVEN')}\n\n"
        + _format_module("OVERALL", overall)
        + _format_module("TRADE_INTEGRITY", trade_integrity)
        + _format_module("PERFORMANCE", performance)
        + _format_module("RUNTIME_HEALTH", runtime_health)
        + "# CONFIDENCE\n"
        + f"{audit.get('confidence', 0.0)}\n"
        + validation_block
    )


def write_reports(audit: dict[str, Any]) -> tuple[Path, Path]:
    """Write JSON and Markdown audit reports."""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    JSON_REPORT.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
    MARKDOWN_REPORT.write_text(render_markdown(audit), encoding="utf-8")
    return JSON_REPORT, MARKDOWN_REPORT