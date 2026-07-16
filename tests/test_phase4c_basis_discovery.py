from __future__ import annotations

import ast
import json
from pathlib import Path

import pandas as pd
import pytest

from research.basis_data import (
    CanonicalPriceCandle,
    basis_changes,
    basis_features,
    canonical_hash,
    canonicalize_rows,
    cooldown_events,
    split_families,
    synchronize_exact,
    validate_primary_hypotheses,
    write_atomic_json,
)
from research.market_edge_discovery import (
    apply_frozen_bin,
    benjamini_hochberg,
    development_boundaries,
    exclusions_stable,
)
from scripts.phase4c_basis_discovery import outcome, registry


TS = 1_728_000_000_000
ROOT = Path(__file__).resolve().parents[1]


def candle(kind: str, close: float, timestamp: int = TS) -> CanonicalPriceCandle:
    return CanonicalPriceCandle("BTCUSDT", "BITGET", "USDT-FUTURES", timestamp, "15m", close, close, close, close, kind, f"history-{kind.lower()}-candles", 1, "raw/1.json")


def test_01_endpoint_semantics_are_distinct():
    assert {candle(k, 100).source_endpoint for k in ("MARKET", "MARK", "INDEX")} == {"history-market-candles", "history-mark-candles", "history-index-candles"}


def test_02_canonical_price_type_separation_and_volume():
    row = [[str(TS), "100", "102", "99", "101", "7", "707"]]
    market = canonicalize_rows("BTCUSDT", "MARKET", "market", row, 1, "raw")
    mark = canonicalize_rows("BTCUSDT", "MARK", "mark", row, 1, "raw")
    assert market[0].volume_base == 7 and mark[0].volume_base is None
    assert market[0].price_type != mark[0].price_type


def test_03_exact_timestamp_synchronization():
    rows, report = synchronize_exact([candle("MARKET", 101)], [candle("MARK", 100)], [candle("INDEX", 99)])
    assert len(rows) == report["fully_synchronized"] == 1


def test_04_no_future_mark_leakage():
    rows, report = synchronize_exact([candle("MARKET", 101)], [candle("MARK", 100, TS + 900_000)], [candle("INDEX", 99)])
    assert rows == [] and report["incomplete_timestamps"] == 2


def test_05_no_future_index_leakage():
    rows, report = synchronize_exact([candle("MARKET", 101)], [candle("MARK", 100)], [candle("INDEX", 99, TS + 900_000)])
    assert rows == [] and report["incomplete_timestamps"] == 2


def test_06_market_index_basis_formula():
    assert basis_features(candle("MARKET", 102), candle("MARK", 101), candle("INDEX", 100)).market_close_basis_bps == pytest.approx(200)


def test_07_mark_index_basis_formula():
    assert basis_features(candle("MARKET", 102), candle("MARK", 101), candle("INDEX", 100)).mark_close_basis_bps == pytest.approx(100)


def test_08_market_mark_divergence_formula():
    assert basis_features(candle("MARKET", 102), candle("MARK", 100), candle("INDEX", 99)).market_mark_divergence_bps == pytest.approx(200)


def test_09_basis_change_windows():
    values = list(map(float, range(100)))
    assert basis_changes(values, 96) == {"15m": 1, "1h": 4, "4h": 16, "8h": 32, "24h": 96}


def test_10_development_only_bins():
    assert development_boundaries(pd.Series(range(100))) == pytest.approx((9.9, 24.75, 49.5, 74.25, 89.1))


def test_11_replication_uses_frozen_bins():
    frozen = development_boundaries(pd.Series(range(100)))
    assert apply_frozen_bin(1000, frozen) == "TOP_10"
    assert frozen == development_boundaries(pd.Series(range(100)))


def test_12_primary_registry_enforces_eight_maximum():
    assert len(registry()) == 8
    with pytest.raises(ValueError):
        validate_primary_hypotheses(registry() + [registry()[0]])


def test_13_primary_and_exploratory_families_separate():
    p = registry()[0]
    e = {**p, "analysis_family": "SECONDARY_EXPLORATORY"}
    assert tuple(map(len, split_families([p, e]))) == (1, 1)


def test_14_event_cooldown_requires_new_entry():
    assert cooldown_events([False, True, True, False, True, False, True], 3) == [1, 6]


def test_15_forward_outcome_uses_future_market_candles():
    frame = pd.DataFrame({"symbol": ["BTCUSDT"] * 3, "market_close": [100, 101, 102], "market_high": [100, 102, 103], "market_low": [100, 99, 101], "market_basis_bps": [0, 0, 0]})
    result = outcome(frame, 2, "LONG")
    assert result.loc[0, "return_pct"] == pytest.approx(2)
    assert result.loc[0, "mfe_pct"] == pytest.approx(3)
    assert pd.isna(result.loc[1, "return_pct"])


def test_16_bh_adjustment_is_monotone():
    assert benjamini_hochberg([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.04, 0.04])


def test_17_stability_exclusions_reject_collapse():
    assert exclusions_stable(1.0, [0.8, 0.2]) is False
    assert exclusions_stable(1.0, [0.8, 0.7]) is True


def test_18_funding_overlay_is_supplemental_only():
    source = (ROOT / "scripts/phase4c_basis_discovery.py").read_text()
    assert '"status":"SUPPLEMENTAL_ONLY"' in source
    assert '"general_edge_claim_permitted":False' in source


def test_19_no_trade_pnl_calculation():
    tree = ast.parse((ROOT / "scripts/phase4c_basis_discovery.py").read_text())
    names = {node.id.lower() for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "pnl" not in names and "net_pnl" not in names


def test_20_no_strategy_execution_imports():
    tree = ast.parse((ROOT / "scripts/phase4c_basis_discovery.py").read_text())
    modules = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any(x.startswith(("strategies", "execution", "planner", "risk")) for x in modules)


def test_21_deterministic_repeated_artifact(tmp_path: Path):
    value = {"z": [3, 2, 1], "a": "basis"}
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    write_atomic_json(first, value); write_atomic_json(second, value)
    assert first.read_bytes() == second.read_bytes()
    assert canonical_hash(json.loads(first.read_text())) == canonical_hash(value)


def test_22_production_live_paper_modules_unchanged_by_research():
    changed = {"research/basis_data.py", "scripts/phase4c_acquire_basis_prices.py", "scripts/phase4c_basis_discovery.py", "tests/test_phase4c_basis_discovery.py"}
    assert not any(path.startswith(("runtime/", "execution/", "strategies/", "risk/", "planner/")) for path in changed)
