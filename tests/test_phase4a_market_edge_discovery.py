from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from clients.schemas import Candle
from research.market_edge_discovery import (
    MAX_INTERACTIONS,
    apply_frozen_bin,
    assert_descriptive_artifact,
    benjamini_hochberg,
    development_boundaries,
    effect_size,
    enforce_sample_size,
    exclusions_stable,
    forward_outcome,
    validate_interactions,
)


SCRIPT = Path(__file__).parents[1] / "scripts" / "phase4a_market_edge_discovery.py"
SPEC = importlib.util.spec_from_file_location("phase4a_script", SCRIPT)
assert SPEC and SPEC.loader
phase4a = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(phase4a)


def candles() -> list[Candle]:
    return [
        Candle(0, 100, 101, 99, 100, 10),
        Candle(900_000, 100, 103, 99, 102, 11),
        Candle(1_800_000, 102, 104, 98, 99, 12),
        Candle(2_700_000, 99, 106, 97, 105, 13),
    ]


def frame(symbol: str, closes: list[float], start: int = 0) -> pd.DataFrame:
    close = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "timestamp_ms": start + np.arange(len(close)) * 900_000,
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume_base": np.arange(len(close), dtype=float) + 100,
        "symbol": symbol,
    })


def test_forward_horizon_correctness() -> None:
    result = forward_outcome(candles(), 0, 2, "LONG")
    assert result.horizon == 2
    assert result.close_return_pct == pytest.approx(-1.0)


def test_mfe_and_mae_correctness() -> None:
    result = forward_outcome(candles(), 0, 2, "LONG")
    assert result.mfe_pct == pytest.approx(4.0)
    assert result.mae_pct == pytest.approx(2.0)
    assert result.mfe_minus_mae_pct == pytest.approx(2.0)


def test_favourable_first_ordering() -> None:
    ordered = [
        Candle(0, 100, 100, 100, 100, 10),
        Candle(900_000, 100, 102, 100, 101, 11),
        Candle(1_800_000, 101, 102, 98, 99, 12),
    ]
    result = forward_outcome(ordered, 0, 2, "LONG")
    assert result.favourable_first[3] is True
    assert result.adverse_first[3] is False


def test_development_only_bin_creation() -> None:
    assert development_boundaries(range(101)) == (10, 25, 50, 75, 90)


def test_frozen_bins_apply_to_replication_extremes() -> None:
    boundaries = development_boundaries(range(101))
    assert apply_frozen_bin(-100, boundaries) == "BOTTOM_10"
    assert apply_frozen_bin(10_000, boundaries) == "TOP_10"


def test_replication_values_cannot_change_development_boundaries() -> None:
    development = development_boundaries(range(101))
    _ = [apply_frozen_bin(value, development) for value in (-1_000, 1_000)]
    assert development == development_boundaries(range(101))


def test_closed_one_hour_context_updates_only_after_fourth_quarter() -> None:
    data = frame("BTCUSDT", list(np.linspace(100, 200, 240)))
    derived = phase4a.features(data)
    changes = derived.trend1h.ne(derived.trend1h.shift()).fillna(False)
    change_timestamps = derived.loc[changes & derived.trend1h.notna(), "timestamp_ms"]
    assert all(timestamp % 3_600_000 == 2_700_000 for timestamp in change_timestamps)
    assert derived.loc[data.timestamp_ms % 3_600_000 != 2_700_000, "trend1h"].notna().any()


def test_cross_symbol_context_is_timestamp_synchronised() -> None:
    frames = {
        "BTCUSDT": frame("BTCUSDT", list(np.linspace(100, 120, 40))),
        "ETHUSDT": frame("ETHUSDT", list(np.linspace(200, 180, 40))),
    }
    combined = phase4a.add_cross_context(frames)
    for _, group in combined.groupby("timestamp_ms"):
        assert group.btc_return_4.nunique(dropna=False) == 1
        assert group.broad_fraction_up.nunique(dropna=False) == 1


def test_sample_size_enforcement() -> None:
    with pytest.raises(ValueError, match="insufficient"):
        enforce_sample_size(499)
    enforce_sample_size(500)


def test_effect_size() -> None:
    assert effect_size(0.5, 0.1, 0.2) == pytest.approx(2.0)


def test_false_discovery_adjustment() -> None:
    adjusted = benjamini_hochberg([0.01, 0.04, 0.03, 0.20])
    assert adjusted == pytest.approx([0.04, 0.0533333333, 0.0533333333, 0.20])


def test_stability_exclusions() -> None:
    assert exclusions_stable(1.0, [0.5, 2.0, 0.25])
    assert not exclusions_stable(1.0, [0.5, -0.1])
    market = pd.concat([
        frame("BTCUSDT", [100, 110, 90, 120]),
        frame("ETHUSDT", [100, 110, 90, 120]),
        frame("SOLUSDT", [100, 110, 90, 120]),
    ], ignore_index=True)
    labels = pd.DataFrame({"state": ["IN", "OUT", "IN", "OUT"] * 3})
    result = phase4a.stability(
        {"feature": "state", "bin": "IN", "orientation": "LONG", "horizon": 1},
        market,
        labels,
    )
    # The first selected row must use the actual next candle (110), not the
    # next selected row (90).
    assert result["full_mean"] == pytest.approx((10 + (120 / 90 - 1) * 100) / 2)


def test_maximum_ten_pairwise_interactions() -> None:
    validate_interactions([(f"a{i}", f"b{i}") for i in range(MAX_INTERACTIONS)])
    with pytest.raises(ValueError, match="ten"):
        validate_interactions([(f"a{i}", f"b{i}") for i in range(MAX_INTERACTIONS + 1)])


def test_strategy_pnl_fields_are_forbidden() -> None:
    with pytest.raises(ValueError, match="forbidden"):
        assert_descriptive_artifact({"net_pnl": 1.0})


def test_analysis_does_not_import_execution_or_backtest() -> None:
    source = SCRIPT.read_text()
    assert "import execution" not in source
    assert "import backtest" not in source
    assert "TradePlan" not in source


def test_analysis_has_no_production_registry_mutation() -> None:
    source = SCRIPT.read_text()
    assert "strategy_registry" not in source
    assert "StrategyRunner" not in source
    assert "failed_range_escape_reversal" not in source


def test_canonical_artifact_hash_is_deterministic() -> None:
    first = {"bins": [1, 2], "result": {"b": 2, "a": 1}}
    second = json.loads(json.dumps(first))
    assert phase4a.canonical_hash(first) == phase4a.canonical_hash(second)
