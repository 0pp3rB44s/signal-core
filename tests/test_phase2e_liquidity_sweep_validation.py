from scripts.phase2e_liquidity_sweep_validation import (
    EXPECTED_PROXY_HASH, PRIOR_START_MS, REQUESTED_END_MS, REQUESTED_START_MS,
    SWEEP, build_universes, classification, combine_breakdowns, final_decision,
    statistics, validate_boundaries, validate_frozen_contract,
)
from app.config import Settings
from backtesting.backtest_engine import BacktestEngine


def manifest():
    return {
        "exchange": "BITGET", "market_type": "USDT-FUTURES", "timeframe": "15m",
        "requested_start_ms": REQUESTED_START_MS, "requested_end_ms_exclusive": REQUESTED_END_MS,
        "quality": [
            {"symbol": "BTCUSDT", "actual_first_ms": REQUESTED_START_MS, "actual_last_ms": REQUESTED_END_MS - 900_000, "candle_count": 35040},
            {"symbol": "NEWUSDT", "actual_first_ms": REQUESTED_START_MS + 900_000, "actual_last_ms": REQUESTED_END_MS - 900_000, "candle_count": 35039},
        ],
    }


def test_non_overlap_exchange_and_futures_contract():
    value = manifest()
    validate_boundaries(value)
    assert value["requested_end_ms_exclusive"] == PRIOR_START_MS
    for key, replacement in (("exchange", "OTHER"), ("market_type", "SPOT"), ("timeframe", "1h")):
        broken = {**value, key: replacement}
        try:
            validate_boundaries(broken)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid independent dataset accepted")


def test_objective_universe_availability_and_common_window():
    result = build_universes(manifest())
    assert result["universe_b_core_full_year"]["symbols"] == ["BTCUSDT"]
    assert result["universe_a_all_symbol_common_window"]["start_ms"] == REQUESTED_START_MS + 900_000
    assert result["objective_exclusions_from_core"] == {"NEWUSDT": "BITGET_FUTURES_HISTORY_UNAVAILABLE_FOR_FULL_REQUESTED_YEAR"}


def test_statistics_and_monte_carlo_are_deterministic():
    values = [1.0, -0.5, 0.2, -0.1]
    assert statistics(values) == statistics(values)
    assert statistics(values)["resamples"] == 5000


def test_independent_classification_threshold_is_not_lowered():
    metric = {"closed_trades": 29, "gross_price_pnl": 3, "expectancy": .1, "profit_factor": 2, "net_pnl": 2, "pnl_without_best": 1}
    uncertainty = {"mean_ci95": [-.1, .3]}
    assert classification(metric, uncertainty) == "DIRECTIONALLY SUPPORTIVE BUT UNDERPOWERED"
    metric["closed_trades"] = 30
    assert classification(metric, uncertainty) == "INDEPENDENTLY POSITIVE"


def test_strategy_filter_and_frozen_hash_contract_are_explicit():
    assert EXPECTED_PROXY_HASH == "722bb6962e575931e5d4b2ee58ce175413729c587f9eed5a796b69930a349cbc"
    independent = {"classification": "FAILED INDEPENDENT VALIDATION", "performance": {"closed_trades": 30}}
    assert final_decision(independent, {"performance": {}}, []) == "FAILED INDEPENDENT VALIDATION — REJECT CURRENT STRATEGY"
    assert SWEEP == "liquidity_sweep_reversal"


def test_execution_and_proxy_contract_must_match_prior_period_exactly():
    contract = {"historical_proxy": {"configuration_hash": EXPECTED_PROXY_HASH}, "execution_assumptions": {"spread_bps": 4.0, "same_candle_policy": "CONSERVATIVE"}}
    validate_frozen_contract(contract, contract.copy())
    changed = {**contract, "execution_assumptions": {**contract["execution_assumptions"], "spread_bps": 5.0}}
    try:
        validate_frozen_contract(changed, contract)
    except ValueError:
        pass
    else:
        raise AssertionError("execution assumption mutation accepted")


def test_period_breakdowns_are_combined_without_mixing_records():
    rows = combine_breakdowns(
        [{"dimension": "symbol", "value": "BTCUSDT", "trades": 2, "net_pnl": 1}],
        [{"dimension": "symbol", "value": "BTCUSDT", "trades": 3, "net_pnl": -2}],
    )
    assert rows == [{"dimension": "symbol", "value": "BTCUSDT", "trades": 5, "net_pnl": -1.0, "expectancy": -.2, "tiny_sample": True}]


def test_strategy_filter_is_explicit_and_default_behavior_is_unchanged():
    default = BacktestEngine(Settings(_env_file=None))
    filtered = BacktestEngine(Settings(_env_file=None), strategy_filter=frozenset({SWEEP}))
    assert default.strategy_filter is None
    assert filtered.strategy_filter == frozenset({SWEEP})
