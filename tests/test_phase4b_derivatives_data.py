from __future__ import annotations

import importlib.util
from dataclasses import replace
from pathlib import Path

import pytest

from research.derivatives_data import (
    FundingObservation, OpenInterestObservation, canonical_hash,
    canonicalize_bitget_funding, canonicalize_tardis_ticker, detect_duplicates,
    detect_gaps, funding_event_window, split_analysis_families,
    stable_page_walk, synchronize_positioning, validate_primary_hypotheses,
    write_atomic_json,
)
from research.market_edge_discovery import apply_frozen_bin, benjamini_hochberg, development_boundaries, exclusions_stable

ROOT = Path(__file__).parents[1]


def funding(timestamp: int, rate: float = 0.001) -> FundingObservation:
    return FundingObservation("BTCUSDT", "BITGET", "USDT-FUTURES", timestamp, rate, 8,
                              "REALISED_SETTLEMENT", 2_000_000, "raw.json")


def oi(timestamp: int, value: float = 2.0) -> OpenInterestObservation:
    return OpenInterestObservation("BTCUSDT", "BITGET", "USDT-FUTURES", timestamp, value,
                                   "BASE_ASSET", 1.0, "base * mark", value * 100.0,
                                   2_000_000, "raw.csv.gz")


def test_funding_timestamp_semantics() -> None:
    rows = [{"fundingTime": "0", "fundingRate": "0.001"}, {"fundingTime": "28800000", "fundingRate": "0.002"}]
    result = canonicalize_bitget_funding("btcusdt", rows, 1, "raw")
    assert result[1].funding_timestamp_ms == 28_800_000 and result[1].funding_interval_hours == 8


def test_realised_and_predicted_funding_are_separate() -> None:
    item = replace(funding(10), predicted_funding_rate=0.002)
    assert item.funding_rate == 0.001 and item.predicted_funding_rate == 0.002 and item.source_type == "REALISED_SETTLEMENT"


def test_oi_unit_normalization() -> None:
    item = canonicalize_tardis_ticker({"symbol":"BTCUSDT","timestamp":"1000000","open_interest":"2","mark_price":"100"}, 2, "raw", .5)
    assert item.unit == "BASE_ASSET" and item.notional_oi_usdt == 100


def test_historical_pagination() -> None:
    pages = {1:[{"x":1},{"x":2}], 2:[{"x":3}]}
    assert stable_page_walk(lambda page: pages[page], 2) == [{"x":1},{"x":2},{"x":3}]


def test_duplicate_detection() -> None:
    assert detect_duplicates([1, 1, 2]) == 1


def test_gap_detection() -> None:
    assert detect_gaps([0, 10, 30], 10) == [{"after_ms":10,"before_ms":30,"missing_intervals":1}]


def test_no_future_funding_leakage() -> None:
    result = synchronize_positioning(0, 900_000, [funding(900_001)], [], 900_000)
    assert not result.funding_available


def test_no_future_oi_leakage() -> None:
    result = synchronize_positioning(0, 900_000, [], [oi(900_001)], 900_000)
    assert not result.oi_available


def test_stale_oi_handling() -> None:
    result = synchronize_positioning(900_000, 900_000, [], [oi(0)], 899_999)
    assert result.oi_stale and not result.oi_available and result.open_interest_value is None


def test_fifteen_minute_synchronization_uses_candle_close() -> None:
    result = synchronize_positioning(0, 900_000, [funding(900_000)], [oi(900_000)], 1)
    assert result.funding_available and result.oi_available and result.funding_age_seconds == 0


def test_development_only_bins() -> None:
    assert development_boundaries(range(101)) == (10, 25, 50, 75, 90)


def test_frozen_replication_bins() -> None:
    boundaries = development_boundaries(range(101))
    assert apply_frozen_bin(10_000, boundaries) == "TOP_10"


def test_preregistered_hypothesis_enforcement() -> None:
    base={"id":"h1","family":"funding","feature":"rate","direction":"SHORT","horizon":8,"expected_sign":"positive","rationale":"crowding","minimum_sample":100,"contradiction_rule":"sign reversal","analysis_family":"PRIMARY_PREREGISTERED"}
    validate_primary_hypotheses([base])
    with pytest.raises(ValueError): validate_primary_hypotheses([base]*9)


def test_primary_and_exploratory_separation() -> None:
    primary, exploratory = split_analysis_families([{"analysis_family":"PRIMARY_PREREGISTERED"},{"analysis_family":"SECONDARY_EXPLORATORY"}])
    assert len(primary) == len(exploratory) == 1


def test_fdr_adjustment() -> None:
    assert benjamini_hochberg([.01,.04,.03,.20]) == pytest.approx([.04,.0533333333,.0533333333,.20])


def test_funding_event_windows() -> None:
    assert funding_event_window(4, 21) == {"before":[0,1,2,3],"funding":4,"after":[5,6,8,12,20]}


def test_symbol_stability_exclusions() -> None:
    assert exclusions_stable(1.0, [.8,.7,.6,.5]) and not exclusions_stable(1.0,[.8,-.1])


def test_no_strategy_execution_import() -> None:
    source=(ROOT/"research/derivatives_data.py").read_text()+(ROOT/"scripts/phase4b_acquire_funding.py").read_text()
    assert "execution" not in source.lower() and "TradePlan" not in source


def test_no_trade_pnl_calculation() -> None:
    source=(ROOT/"research/derivatives_data.py").read_text()
    assert "net_pnl" not in source and "profit_factor" not in source


def test_deterministic_repeated_artifacts(tmp_path: Path) -> None:
    payload={"b":2,"a":[1]};first=tmp_path/"first.json";second=tmp_path/"second.json"
    write_atomic_json(first,payload);write_atomic_json(second,payload)
    assert first.read_bytes()==second.read_bytes() and canonical_hash(payload)==canonical_hash(payload)


def test_no_production_registry_changes() -> None:
    registry=(ROOT/"strategies/registry.py") if (ROOT/"strategies/registry.py").exists() else (ROOT/"strategies/__init__.py")
    assert "phase4b" not in registry.read_text().lower()


def test_live_and_paper_do_not_import_research_foundation() -> None:
    files=list((ROOT/"execution").glob("*.py"))+list((ROOT/"forward_paper").glob("*.py"))
    assert all("research.derivatives_data" not in path.read_text() for path in files)
