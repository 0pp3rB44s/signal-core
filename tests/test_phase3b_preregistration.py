from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from research.preregistration_protocol import (
    DEVELOPMENT_END_MS, DEVELOPMENT_START_MS, LOCKED_ACCEPTANCE_CRITERIA,
    VALIDATION_END_MS, VALIDATION_START_MS, Parameter, assert_descriptive_source,
    assert_no_performance_fields, enforce_parameter_limits, preregistration_hash,
    validate_preregistration, validate_split,
)

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "research/preregistrations/failed_range_escape_reversal_v1.json"
BASELINE_HASHES = {
    "app/runner.py": "1209127ecba1f60a142ce644d58885df56c39f582c74d8d98812e41061946ff3",
    "forward_paper/service.py": "6e66709c8884159bef913867050dc362212ec374266ce1184edc40777615fe67",
    "forward_paper/store.py": "8e340ac0add257b784e901f6881446e807d07ed9b75181e5cebce6e153a8a299",
    "strategies/__init__.py": "c54847234a2a0764141bcc7c9e6692f0fc5c3cbd5abe381129f1459a35ddd522",
    "strategies/liquidity_sweep.py": "f125e045ff46fd569308e55a2eee3bd7c049cea8699c38aa9b4299843d634d4e",
    "strategies/momentum_breakout.py": "7f2d8c10535192fa21a0028675b88030909cb9cdbf0bbd2519442dec9b2bcdc0",
    "strategies/strategies/continuation.py": "57eccfd6f04db78e6e746621d44261706875763f61bac684c392a4c47bc42f4e",
    "strategies/strategies/low_vol_reclaim.py": "5909764ee69d00c155bbe8cdeccb819a0a005a7e223690a6b7a01849598f978a",
}


def load_document() -> dict:
    return json.loads(DOCUMENT.read_text())


def test_development_and_validation_boundaries_do_not_overlap() -> None:
    validate_split()
    assert DEVELOPMENT_START_MS < DEVELOPMENT_END_MS == VALIDATION_START_MS < VALIDATION_END_MS


def test_validation_performance_cannot_be_loaded() -> None:
    with pytest.raises(PermissionError):
        assert_descriptive_source(ROOT / "reports/analysis/trade_level.csv")
    with pytest.raises(ValueError):
        assert_no_performance_fields({"locked_validation": {"net_pnl": 1.0}})


def test_inventory_does_not_import_or_invoke_strategy_execution() -> None:
    source = (ROOT / "scripts/phase3b_descriptive_inventory.py").read_text()
    forbidden = (
        "backtesting", "strategy_diagnosis", "ExecutionContract", "BacktestRunner",
        "TradePlanner", "RiskManager",
    )
    assert all(name not in source for name in forbidden)
    assert load_document()["status"] == "PREREGISTERED_NOT_IMPLEMENTED"


def test_parameter_count_limit_is_enforced() -> None:
    values = [Parameter(f"p{i}", "setup", i, "u", "f", "r", "p", "e") for i in range(6)]
    with pytest.raises(ValueError):
        enforce_parameter_limits(values)
    validate_preregistration(load_document())


def test_required_specification_fields_exist() -> None:
    document = load_document()
    validate_preregistration(document)
    assert set(document["deterministic_specification"]) == {
        "setup", "entry", "invalidation", "exit", "risk", "suppression"
    }


def test_acceptance_criteria_are_immutable() -> None:
    document = load_document()
    assert tuple(document["phase3c_evaluation"]["acceptance_criteria"]) == LOCKED_ACCEPTANCE_CRITERIA
    document["phase3c_evaluation"]["acceptance_criteria"][0] = "closed_trades >= 1"
    with pytest.raises(ValueError):
        validate_preregistration(document)


def test_document_hash_is_deterministic() -> None:
    document = load_document()
    expected = "e7117eefbf5e387646f2a5bceb444d5125a46c56b438eb6f2c8d2e6f69077da9"
    assert preregistration_hash(document) == preregistration_hash(json.loads(json.dumps(document))) == expected
    assert document["document_hash"] == expected


def test_rejected_strategies_remain_archived() -> None:
    statuses = json.loads((ROOT / "research/strategy_status.json").read_text())
    assert statuses["trend_continuation_v1"] == "REJECTED — ENTRY FAILURE"
    assert statuses["momentum_breakdown_v1"] == "REJECTED FOR CURRENT RESEARCH — ENTRY FAILURE"
    assert statuses["liquidity_sweep_reversal_v1"] == "REJECTED — FAILED INDEPENDENT VALIDATION"
    assert statuses["liquidity_sweep_reversal_confirmation_v1"] == "REJECTED — LATE ENTRY DESTROYED GROSS EDGE"
    assert statuses["momentum_breakout_v1"] == "INSUFFICIENT SAMPLE"
    assert statuses["low_vol_reclaim_v1"] == "NO CANDIDATES IN FROZEN SAMPLE"


@pytest.mark.parametrize("relative_path", sorted(BASELINE_HASHES))
def test_production_live_and_paper_sources_match_frozen_parent(relative_path: str) -> None:
    actual = hashlib.sha256((ROOT / relative_path).read_bytes()).hexdigest()
    assert actual == BASELINE_HASHES[relative_path]
