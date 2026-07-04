"""Legacy Trade Integrity implementation.

This module contains the original Trade Integrity analysis logic,
kept for backward compatibility during migration.
"""

# Required columns for trade data integrity checks
REQUIRED_COLUMNS = [
    "trade_id",
    "timestamp",
    "symbol",
    "side",
    "price",
    "quantity",
    "status",
]

def analyze_headers(headers):
    """Check that all required columns are present in the headers."""
    missing = [col for col in REQUIRED_COLUMNS if col not in headers]
    findings = []
    if missing:
        findings.append({
            "type": "missing_columns",
            "missing": missing,
            "severity": "high",
            "message": f"Missing required columns: {', '.join(missing)}"
        })
    return findings

def analyze_rows(rows):
    """Check for malformed or incomplete rows."""
    findings = []
    for i, row in enumerate(rows):
        missing = [col for col in REQUIRED_COLUMNS if not row.get(col)]
        if missing:
            findings.append({
                "type": "incomplete_row",
                "row": i,
                "missing": missing,
                "severity": "medium",
                "message": f"Row {i} missing values for: {', '.join(missing)}"
            })
    return findings

def analyze_timestamps(rows):
    """Check for out-of-order or invalid timestamps."""
    findings = []
    last_ts = None
    for i, row in enumerate(rows):
        ts = row.get("timestamp")
        if ts is None:
            continue
        try:
            ts_val = float(ts)
        except Exception:
            findings.append({
                "type": "invalid_timestamp",
                "row": i,
                "value": ts,
                "severity": "medium",
                "message": f"Row {i} has invalid timestamp: {ts}"
            })
            continue
        if last_ts is not None and ts_val < last_ts:
            findings.append({
                "type": "out_of_order_timestamp",
                "row": i,
                "previous": last_ts,
                "current": ts_val,
                "severity": "low",
                "message": f"Row {i} timestamp {ts_val} is before previous {last_ts}"
            })
        last_ts = ts_val
    return findings

def analyze_protection(rows):
    """Check for canceled or failed trades and alert if above threshold."""
    findings = []
    canceled = [row for row in rows if row.get("status") == "canceled"]
    failed = [row for row in rows if row.get("status") == "failed"]
    total = len(rows)
    if total == 0:
        return findings
    canceled_ratio = len(canceled) / total
    failed_ratio = len(failed) / total
    if canceled_ratio > 0.05:
        findings.append({
            "type": "high_canceled_ratio",
            "count": len(canceled),
            "ratio": canceled_ratio,
            "severity": "medium",
            "message": f"High canceled trade ratio: {canceled_ratio:.1%}"
        })
    if failed_ratio > 0.01:
        findings.append({
            "type": "high_failed_ratio",
            "count": len(failed),
            "ratio": failed_ratio,
            "severity": "high",
            "message": f"High failed trade ratio: {failed_ratio:.1%}"
        })
    return findings

def build_trade_health_score(findings):
    """Build a simple health score from findings."""
    score = 100
    for f in findings:
        if f["severity"] == "high":
            score -= 30
        elif f["severity"] == "medium":
            score -= 10
        elif f["severity"] == "low":
            score -= 3
    return max(score, 0)

def summarize_findings(findings):
    """Summarize findings for reporting."""
    summary = []
    for f in findings:
        summary.append(f.get("message", str(f)))
    return "\n".join(summary)