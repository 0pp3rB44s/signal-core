from pathlib import Path

import pytest

from app.config import Settings
from backtesting.execution_contract import BacktestExecutionConfig, BacktestExecutionContract
from clients.schemas import Candle
from research.liquidity_sweep_confirmation import (
    CONFIRMATION_WINDOW_CANDLES, EXPERIMENTAL_STRATEGY,
    confirmation_entry, execute_confirmation, original_geometry,
)
from scripts.phase3a_confirmation_entry_analysis import (
    bootstrap_mean_difference, max_drawdown, opportunity_cost, paired_statistics,
)


def candles(closes=(100, 102, 101, 103), highs=(101, 103, 104, 104), lows=(99, 98, 97, 100)):
    return [Candle(i * 900_000, 100, highs[i], lows[i], closes[i], 10) for i in range(len(closes))]


def test_long_requires_closed_close_above_signal_high():
    result = confirmation_entry(candles(), 0, "LONG")
    assert result.status == "CONFIRMED" and result.confirmation_offset == 1


def test_short_requires_closed_close_below_signal_low():
    result = confirmation_entry(candles(closes=(100, 98, 101, 100)), 0, "SHORT")
    assert result.status == "CONFIRMED" and result.confirmation_offset == 1


def test_intrabar_touch_does_not_confirm():
    result = confirmation_entry(candles(closes=(100, 100, 100, 100), highs=(101, 105, 106, 101)), 0, "LONG")
    assert result.status == "CONFIRMATION_EXPIRED"


def test_confirmation_expires_after_exactly_two_candles():
    series = candles(closes=(100, 100, 100, 105), highs=(101, 102, 102, 106))
    assert confirmation_entry(series, 0, "LONG").status == "CONFIRMATION_EXPIRED"
    with pytest.raises(ValueError, match="frozen at two"):
        confirmation_entry(series, 0, "LONG", window_candles=3)


def test_entry_is_candle_after_confirmation():
    result = confirmation_entry(candles(), 0, "LONG")
    assert result.entry_index == 2
    assert candles()[result.entry_index].timestamp_ms == result.confirmation_timestamp_ms + 900_000


def test_no_future_leakage_beyond_two_confirmation_candles():
    first = candles(closes=(100, 100, 100, 100), highs=(101, 102, 102, 102))
    second = first + [Candle(3_600_000, 100, 110, 99, 109, 10)]
    assert confirmation_entry(first, 0, "LONG") == confirmation_entry(second, 0, "LONG")


def test_gap_fails_closed_instead_of_skipping_to_future_confirmation():
    series = candles(); series[1] = Candle(1_800_000, 100, 103, 99, 102, 10)
    assert confirmation_entry(series, 0, "LONG").status == "CONFIRMATION_EXPIRED"


def test_original_absolute_stop_and_target_geometry_is_unchanged():
    assert original_geometry("LONG", 100, 98) == (101.6, 103.0)
    assert original_geometry("SHORT", 100, 102) == (98.4, 97.0)


def test_confirmation_uses_existing_fee_spread_slippage_contract():
    config = BacktestExecutionConfig()
    execution = BacktestExecutionContract(config)
    record = execute_confirmation(execution, symbol="BTCUSDT", direction="LONG", signal_timestamp=0, entry_hint=100, invalidation=98, candles=candles(), entry_index=2, equity=1000)
    assert record.strategy == EXPERIMENTAL_STRATEGY
    assert config.spread_bps == 4 and config.entry_slippage_bps == 2 and config.taker_fee_bps == 6
    assert record.initial_stop == 98 and record.tp1_price == 101.6


def test_experimental_variant_is_absent_from_settings_and_rejected_in_research_status():
    settings = Settings(_env_file=None)
    assert EXPERIMENTAL_STRATEGY not in repr(settings)
    root = Path(__file__).resolve().parents[1]
    import json
    status = json.loads((root / "research/strategy_status.json").read_text())
    assert status[EXPERIMENTAL_STRATEGY] == "HYPOTHESIS_REJECTED_RESEARCH_ONLY"


def test_production_paper_and_live_sources_do_not_register_variant():
    root = Path(__file__).resolve().parents[1]
    for directory in (root / "app", root / "forward_paper", root / "execution", root / "strategies"):
        for path in directory.rglob("*.py"):
            assert EXPERIMENTAL_STRATEGY not in path.read_text(encoding="utf-8")


def test_paired_comparison_matches_candidate_identity_not_row_position():
    left=[{"symbol":"BTC","direction":"LONG","signal_timestamp":1,"fill_status":"FILLED","final_exit_reason":"STOP","net_pnl":-1}]
    right=[{"symbol":"BTC","direction":"LONG","signal_timestamp":1,"fill_status":"FILLED","final_exit_reason":"TP","net_pnl":2}]
    result=paired_statistics(left,right)
    assert result["paired_candidates"]==1 and result["mean_difference"]==3


def test_unpaired_bootstrap_difference_is_deterministic():
    left=[{"fill_status":"FILLED","final_exit_reason":"STOP","net_pnl":-1}]
    right=[{"fill_status":"FILLED","final_exit_reason":"TP","net_pnl":2}]
    assert bootstrap_mean_difference(left,right)==bootstrap_mean_difference(left,right)


def test_drawdown_uses_realised_order_without_sorting():
    assert max_drawdown([1,-2,1])==2


def test_confirmation_window_constant_is_frozen():
    assert CONFIRMATION_WINDOW_CANDLES == 2


def test_control_execution_defaults_are_not_mutated():
    assert BacktestExecutionConfig() == BacktestExecutionConfig.from_settings(Settings(_env_file=None))


def test_opportunity_cost_reconstructs_avoided_loser(tmp_path):
    canonical=tmp_path/"canonical";canonical.mkdir()
    rows=[{"timestamp":i*900_000,"open":100,"high":101,"low":99,"close":100,"volume_base":10,"volume_quote":1000} for i in range(20)]
    import json
    (canonical/"BTCUSDT.json").write_text(json.dumps(rows))
    control=[{"symbol":"BTCUSDT","direction":"LONG","signal_timestamp":0,"fill_status":"FILLED","final_exit_reason":"STOP_LOSS","net_pnl":-1,"executed_entry":100,"tp1_price":101}]
    decisions=[{"symbol":"BTCUSDT","direction":"LONG","signal_timestamp":0,"status":"CONFIRMATION_EXPIRED"}]
    summary,details=opportunity_cost(control,decisions,canonical)
    assert summary["losers_avoided"]==1 and summary["net_pnl_avoided"]==1
    assert len(details)==1
