"""Validate Strategy Funnel JSON/CSV outputs without runtime imports."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from analysis.strategy_funnel import ACTIVE_STRATEGIES, FUNNEL_FIELDS


def validate_report(json_path: str | Path, csv_path: str | Path) -> list[str]:
    errors: list[str] = []
    report = json.loads(Path(json_path).read_text(encoding="utf-8"))
    expected_hash = report.get("analysis_hash")
    reproducible = {
        key: value for key, value in report.items()
        if key not in {"generated_at_utc", "analysis_hash"}
    }
    actual_hash = hashlib.sha256(
        json.dumps(reproducible, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if expected_hash != actual_hash:
        errors.append("analysis_hash mismatch")

    if tuple(report.get("active_strategies", ())) != ACTIVE_STRATEGIES:
        errors.append("active strategy list mismatch")
    policy = report.get("analysis_policy", {})
    if policy.get("read_only") is not True or policy.get("imports_runtime_modules") is not False:
        errors.append("read-only analysis policy missing")
    if policy.get("datasets_kept_separate") is not True:
        errors.append("dataset separation policy missing")

    source_manifest = report.get("data_quality", {}).get("source_manifest", [])
    if not source_manifest or not any(source.get("status") == "read" for source in source_manifest):
        errors.append("no analyzer source was successfully read")

    views = report.get("dataset_views", [])
    expected_rows = len(ACTIVE_STRATEGIES) * 3
    if len(views) != expected_rows:
        errors.append(f"dataset view count {len(views)} != {expected_rows}")
    view_keys = {(row.get("dataset_scope"), row.get("strategy")) for row in views}
    primary_scopes = {
        str(row.get("dataset_scope")) for row in views
        if str(row.get("dataset_scope", "")).startswith("backtest_funnel_")
        or row.get("dataset_scope") == "structured_funnel_current"
    }
    if len(primary_scopes) != 1:
        errors.append("expected exactly one structured or legacy funnel scope")
    expected_scopes = primary_scopes | {"forward_paper_current", "exchange_internal_attribution"}
    if view_keys != {(scope, strategy) for scope in expected_scopes for strategy in ACTIVE_STRATEGIES}:
        errors.append("dataset view scope/strategy matrix incomplete")

    with Path(csv_path).open(encoding="utf-8", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    if len(csv_rows) != expected_rows:
        errors.append(f"CSV row count {len(csv_rows)} != {expected_rows}")
    required_columns = {"dataset_scope", "strategy", *FUNNEL_FIELDS, "reject_reasons", "missing_stages", "provenance"}
    actual_columns = set(csv_rows[0].keys()) if csv_rows else set()
    if required_columns != actual_columns:
        errors.append("CSV column set mismatch")
    csv_keys = {(row.get("dataset_scope"), row.get("strategy")) for row in csv_rows}
    if csv_keys != view_keys:
        errors.append("CSV and JSON dataset views differ")
    for row in csv_rows:
        for field in ("reject_reasons", "missing_stages", "provenance"):
            try:
                value: Any = json.loads(row.get(field) or "null")
            except json.JSONDecodeError:
                errors.append(f"invalid embedded JSON in {field} for {row.get('dataset_scope')}/{row.get('strategy')}")
                continue
            if not isinstance(value, list):
                errors.append(f"embedded {field} is not a list")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate strategy funnel outputs")
    parser.add_argument("--json", type=Path, default=Path("reports/strategy_funnel_report.json"))
    parser.add_argument("--csv", type=Path, default=Path("reports/strategy_funnel.csv"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    errors = validate_report(args.json, args.csv)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("strategy_funnel_validation=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
