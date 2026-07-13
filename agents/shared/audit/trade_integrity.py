"""Trade lifecycle integrity checks for the CGC Audit Platform."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


Finding = dict[str, Any]

REQUIRED_COLUMNS = {
    "event_type",
    "strategy",
    "status",
    "opened_at",
    "closed_at",
    "stop_loss",
    "take_profits",
    "snapshot_link_key",
}


def _clean(value: Any) -> str:
    """Normalize nullable CSV values."""
    return str(value or "").strip()


def _is_test_row(row: dict[str, Any]) -> bool:
    """Return True for dataset write tests or synthetic test rows."""
    symbol = _clean(row.get("symbol")).upper()
    strategy = _clean(row.get("strategy")).lower()
    status = _clean(row.get("status")).lower()
    notes = " ".join(
        [
            _clean(row.get("quality_notes")).lower(),
            _clean(row.get("process_verdict")).lower(),
            _clean(row.get("failure_type")).lower(),
        ]
    )
    return (
        symbol.startswith("TEST")
        or "dataset_write_test" in strategy
        or "test_only" in status
        or "dataset_write_test" in notes
        or "test_only" in notes
    )


def _production_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter out synthetic/test rows for dataset classification."""
    return [row for row in rows if not _is_test_row(row)]


def detect_dataset_type(rows: list[dict[str, Any]]) -> str:
    """Classify the dataset lifecycle shape using production rows and majority logic."""
    production_rows = _production_rows(rows)
    rows_to_classify = production_rows or rows

    event_values = [
        _clean(row.get("event_type")).upper()
        for row in rows_to_classify
        if _clean(row.get("event_type"))
    ]
    if not event_values:
        return "UNKNOWN"

    close_count = sum(1 for event in event_values if event == "CLOSE")
    open_count = sum(1 for event in event_values if event == "OPEN")
    total = len(event_values)
    close_ratio = close_count / total if total else 0.0

    if close_ratio >= 0.90:
        return "CLOSE_ONLY_SYNC"
    if open_count > 0 and close_count > 0:
        return "FULL_LIFECYCLE"
    return "MIXED_EVENTS"


def analyze_headers(columns: list[str]) -> list[Finding]:
    """Validate required dataset columns."""
    findings: list[Finding] = []
    missing = sorted(REQUIRED_COLUMNS.difference(columns))
    if missing:
        findings.append(
            {
                "severity": "HIGH",
                "issue": "Missing required dataset columns.",
                "count": len(missing),
                "examples": missing,
            }
        )
    return findings


def analyze_rows(rows: list[dict[str, Any]]) -> list[Finding]:
    """Validate row-level dataset integrity."""
    findings: list[Finding] = []
    duplicate_keys: dict[str, int] = {}

    dataset_type = detect_dataset_type(rows)
    production_rows = _production_rows(rows) or rows
    rows_for_required_fields = production_rows

    for row in rows:
        key = str(row.get("snapshot_link_key", "")).strip()
        if key:
            duplicate_keys[key] = duplicate_keys.get(key, 0) + 1

    duplicates = [key for key, count in duplicate_keys.items() if count > 1]
    if duplicates:
        findings.append(
            {
                "severity": "LOW",
                "issue": "Duplicate snapshot_link_key values detected; verify whether these are expected sync duplicates.",
                "count": len(duplicates),
                "examples": duplicates[:10],
            }
        )

    # Aggregated checks for empty fields
    opened_at_missing = sum(1 for row in rows_for_required_fields if not _clean(row.get("opened_at")))
    closed_at_missing = sum(1 for row in rows_for_required_fields if not _clean(row.get("closed_at")))
    stop_loss_missing = sum(1 for row in rows_for_required_fields if not _clean(row.get("stop_loss")))
    take_profits_missing = sum(1 for row in rows_for_required_fields if not _clean(row.get("take_profits")))

    has_process_verdict = any("process_verdict" in row for row in rows_for_required_fields)
    process_verdict_missing = 0
    if has_process_verdict:
        process_verdict_missing = sum(1 for row in rows_for_required_fields if not _clean(row.get("process_verdict")))

    has_data_confidence = any("data_confidence" in row for row in rows_for_required_fields)
    data_confidence_missing = 0
    if has_data_confidence:
        data_confidence_missing = sum(1 for row in rows_for_required_fields if not _clean(row.get("data_confidence")))

    if opened_at_missing:
        findings.append(
            {
                "severity": "HIGH",
                "issue": "Missing opened_at values.",
                "count": opened_at_missing,
                "examples": [],
            }
        )
    if closed_at_missing:
        if dataset_type == "CLOSE_ONLY_SYNC":
            findings.append(
                {
                    "severity": "LOW",
                    "issue": "Missing closed_at values in CLOSE_ONLY_SYNC records; verify sync completeness.",
                    "count": closed_at_missing,
                    "examples": [],
                }
            )
        else:
            findings.append(
                {
                    "severity": "HIGH",
                    "issue": "Missing closed_at values.",
                    "count": closed_at_missing,
                    "examples": [],
                }
            )
    if stop_loss_missing:
        if dataset_type == "CLOSE_ONLY_SYNC":
            findings.append(
                {
                    "severity": "LOW",
                    "issue": "Missing stop_loss values in CLOSE_ONLY_SYNC records; live protection check skipped.",
                    "count": stop_loss_missing,
                    "examples": [],
                }
            )
        else:
            findings.append(
                {
                    "severity": "HIGH",
                    "issue": "Missing stop_loss values.",
                    "count": stop_loss_missing,
                    "examples": [],
                }
            )
    if take_profits_missing:
        if dataset_type == "CLOSE_ONLY_SYNC":
            findings.append(
                {
                    "severity": "LOW",
                    "issue": "Missing take_profits values in CLOSE_ONLY_SYNC records; live target check skipped.",
                    "count": take_profits_missing,
                    "examples": [],
                }
            )
        else:
            findings.append(
                {
                    "severity": "MEDIUM",
                    "issue": "Missing take_profits values.",
                    "count": take_profits_missing,
                    "examples": [],
                }
            )
    if process_verdict_missing:
        findings.append(
            {
                "severity": "MEDIUM",
                "issue": "Missing process_verdict values.",
                "count": process_verdict_missing,
                "examples": [],
            }
        )
    if data_confidence_missing:
        findings.append(
            {
                "severity": "MEDIUM",
                "issue": "Missing data_confidence values.",
                "count": data_confidence_missing,
                "examples": [],
            }
        )

    return findings


def summarize_findings(findings: list[Finding]) -> dict[str, list[str]]:
    """Group findings by severity for the rule audit."""
    grouped: dict[str, list[str]] = {
        "critical": [],
        "high": [],
        "medium": [],
        "low": [],
    }

    for finding in findings:
        severity = str(finding.get("severity", "LOW")).lower()
        issue = str(finding.get("issue", "Unknown issue"))
        count = finding.get("count", 0)
        examples = finding.get("examples", [])
        message = f"{issue} (count={count})"
        if examples:
            message += f" examples={examples[:3]}"
        grouped.setdefault(severity, []).append(message)

    return grouped


def _row_key(row: dict[str, Any]) -> str:
    """Return the best available stable key for a trade row."""
    snapshot_key = str(row.get("snapshot_link_key", "")).strip()
    if snapshot_key:
        return snapshot_key

    symbol = str(row.get("symbol", "UNKNOWN_SYMBOL")).strip()
    opened_at = str(row.get("opened_at", "UNKNOWN_OPENED_AT")).strip()
    strategy = str(row.get("strategy", "UNKNOWN_STRATEGY")).strip()
    return f"{symbol}|{opened_at}|{strategy}"


def analyze_lifecycle(rows: list[dict[str, Any]]) -> list[Finding]:
    """Analyze OPEN/CLOSE lifecycle consistency."""
    findings: list[Finding] = []

    dataset_type = detect_dataset_type(rows)
    rows_for_lifecycle = _production_rows(rows) or rows
    has_open_events = dataset_type in {"FULL_LIFECYCLE", "MIXED_EVENTS"}

    event_counts_by_key: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows_for_lifecycle:
        key = _row_key(row)
        event_type = str(row.get("event_type", "")).strip().upper()
        if event_type:
            event_counts_by_key[key][event_type] += 1

    duplicate_open: list[str] = []
    duplicate_close: list[str] = []
    close_without_open: list[str] = []
    open_without_close: list[str] = []

    for key, counts in event_counts_by_key.items():
        open_count = counts.get("OPEN", 0)
        close_count = counts.get("CLOSE", 0)

        # Always check for duplicate OPENs
        if open_count > 1:
            duplicate_open.append(key)
        # Only check for duplicate CLOSEs if OPEN events exist
        if has_open_events and close_count > 1:
            duplicate_close.append(key)
        # Only check for OPEN/CLOSE pairing if OPEN events exist
        if has_open_events:
            if close_count > 0 and open_count == 0:
                close_without_open.append(key)
            if open_count > 0 and close_count == 0:
                open_without_close.append(key)

    # If there are no OPEN events, skip pairing/duplicate CLOSE checks and add a LOW finding
    if not has_open_events:
        findings.append(
            {
                "severity": "LOW",
                "issue": "Dataset type CLOSE_ONLY_SYNC detected; OPEN/CLOSE pairing checks skipped.",
                "count": len(rows_for_lifecycle),
                "examples": [],
            }
        )
    else:
        if close_without_open:
            findings.append(
                {
                    "severity": "MEDIUM",
                    "issue": "CLOSE events without matching OPEN event.",
                    "count": len(close_without_open),
                    "examples": close_without_open[:10],
                }
            )
        if open_without_close:
            findings.append(
                {
                    "severity": "HIGH",
                    "issue": "OPEN events without matching CLOSE event.",
                    "count": len(open_without_close),
                    "examples": open_without_close[:10],
                }
            )
        if duplicate_close:
            findings.append(
                {
                    "severity": "MEDIUM",
                    "issue": "Duplicate CLOSE events detected.",
                    "count": len(duplicate_close),
                    "examples": duplicate_close[:10],
                }
            )
    # Always report duplicate OPEN events if present
    if duplicate_open:
        findings.append(
            {
                "severity": "HIGH",
                "issue": "Duplicate OPEN events detected.",
                "count": len(duplicate_open),
                "examples": duplicate_open[:10],
            }
        )

    return findings


def build_trade_integrity_summary(rows: list[dict[str, Any]], findings: list[Finding]) -> dict[str, Any]:
    """Build a compact trade integrity summary."""
    dataset_type = detect_dataset_type(rows)
    production_row_count = len(_production_rows(rows) or rows)

    penalty = 0
    for finding in findings:
        severity = str(finding.get("severity", "")).upper()
        count = int(finding.get("count", 1) or 1)
        if severity == "HIGH":
            penalty += 10 * min(count, 3)
        elif severity == "MEDIUM":
            penalty += 5 * min(count, 3)

    if dataset_type == "CLOSE_ONLY_SYNC":
        penalty = min(penalty, 10)
    else:
        penalty = min(penalty, 25)

    score = max(0, 100 - penalty)

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
        "rows_analyzed": len(rows),
        "production_rows_analyzed": production_row_count,
        "finding_count": len(findings),
        "score": score,
        "grade": grade,
        "dataset_type": dataset_type,
    }