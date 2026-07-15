from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEVELOPMENT_START_MS = 1721001600000
DEVELOPMENT_END_MS = 1752537600000
VALIDATION_START_MS = DEVELOPMENT_END_MS
VALIDATION_END_MS = 1784073600000

FORBIDDEN_PATH_PARTS = {"reports", "trade_level.csv", "trade_log.json", "execution_records.json"}
FORBIDDEN_PERFORMANCE_FIELDS = {
    "net_pnl", "gross_pnl", "profit_factor", "expectancy", "fees", "trade_return",
    "ending_equity", "win_rate", "drawdown",
}

REQUIRED_SPECIFICATION_SECTIONS = (
    "setup", "entry", "invalidation", "exit", "risk", "suppression",
)

LOCKED_ACCEPTANCE_CRITERIA = (
    "closed_trades >= 30",
    "gross_price_expectancy > 0",
    "net_expectancy > 0",
    "profit_factor > 1.15",
    "net_result_without_best_trade > 0",
    "largest_profitable_symbol_share <= 0.50",
    "largest_profitable_month_share <= 0.40",
    "total_costs / gross_profit < 0.70",
    "maximum_drawdown_pct <= 0.05",
    "maximum_drawdown / total_net_profit <= 1.50",
    "development_and_validation_gross_expectancy_have_same_sign",
    "bootstrap_95pct_mean_net_expectancy_lower_bound_r >= -0.05",
    "at_least_3_symbols_each_have_5_trades_and_positive_gross_price_expectancy",
)


@dataclass(frozen=True)
class Parameter:
    name: str
    category: str
    value: float | int | str
    unit: str
    formula: str
    rationale: str
    plausible_range: str
    expected_effect: str


def validate_split() -> None:
    if DEVELOPMENT_END_MS != VALIDATION_START_MS or DEVELOPMENT_START_MS >= DEVELOPMENT_END_MS or VALIDATION_START_MS >= VALIDATION_END_MS:
        raise ValueError("development and locked-validation boundaries must be contiguous and non-overlapping")


def assert_descriptive_source(path: Path) -> None:
    lowered = {part.lower() for part in path.parts}
    if lowered & FORBIDDEN_PATH_PARTS or path.name.lower() in FORBIDDEN_PATH_PARTS:
        raise PermissionError("preregistration cannot load reports, trades or execution output")
    if "canonical" not in lowered or "historical" not in lowered:
        raise PermissionError("preregistration accepts canonical historical candles only")


def assert_no_performance_fields(value: Any) -> None:
    if isinstance(value, dict):
        forbidden = {str(key).lower() for key in value} & FORBIDDEN_PERFORMANCE_FIELDS
        if forbidden:
            raise ValueError(f"performance fields forbidden during preregistration: {sorted(forbidden)}")
        for item in value.values():
            assert_no_performance_fields(item)
    elif isinstance(value, list):
        for item in value:
            assert_no_performance_fields(item)


def enforce_parameter_limits(parameters: list[Parameter]) -> None:
    limits = {"setup": 5, "entry_timing": 1, "stop": 1, "target": 2, "max_hold": 1}
    counts = {category: sum(parameter.category == category for parameter in parameters) for category in limits}
    unknown = {parameter.category for parameter in parameters} - set(limits)
    if unknown or any(counts[category] > limit for category, limit in limits.items()):
        raise ValueError(f"parameter-count limit exceeded: counts={counts} unknown={sorted(unknown)}")


def canonical_hash(value: Any) -> str:
    assert_no_performance_fields(value)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def preregistration_hash(value: dict[str, Any]) -> str:
    unhashed = dict(value)
    unhashed.pop("document_hash", None)
    return canonical_hash(unhashed)


def validate_preregistration(value: dict[str, Any]) -> None:
    validate_split()
    specification = value.get("deterministic_specification", {})
    missing = set(REQUIRED_SPECIFICATION_SECTIONS) - set(specification)
    if missing:
        raise ValueError(f"missing specification sections: {sorted(missing)}")
    if tuple(value.get("phase3c_evaluation", {}).get("acceptance_criteria", ())) != LOCKED_ACCEPTANCE_CRITERIA:
        raise ValueError("acceptance criteria differ from the immutable Phase 3C contract")
    parameters = [Parameter(**item) for item in value.get("parameter_register", [])]
    enforce_parameter_limits(parameters)
    if value.get("document_hash") != preregistration_hash(value):
        raise ValueError("document hash mismatch")
