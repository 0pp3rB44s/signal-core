import pytest

from clients.schemas import Candle
from scripts.strategy_diagnosis import counterfactual, excursion, uncertainty, wilson


def _record(**changes):
    row = {
        "strategy": "trend_continuation", "symbol": "BTCUSDT", "direction": "LONG",
        "signal_timestamp": "1", "fill_timestamp": "2", "executed_entry": "100",
        "requested_entry": "100", "initial_stop": "99", "tp1_price": "100.8",
        "initial_quantity": "10", "entry_fee": ".6", "stop_after_tp1": "100.12",
    }
    row.update(changes)
    return row


def _candles():
    return [
        Candle(1, 100, 100.2, 99.8, 100, 100), Candle(2, 100, 100.1, 99.9, 100, 100),
        Candle(3, 100, 100.5, 99.7, 100.4, 100), Candle(4, 100.4, 101.0, 100.2, 100.9, 100),
        Candle(5, 100.9, 101.6, 100.8, 101.5, 100), Candle(6, 101.5, 500, 0.1, 400, 100),
    ]


def test_mfe_mae_and_r_calculation():
    row = excursion(_record(), _candles(), horizon=3)
    assert row["mfe"] == pytest.approx(1.6)
    assert row["mae"] == pytest.approx(.3)
    assert row["mfe_r"] == pytest.approx(1.6)
    assert row["mae_r"] == pytest.approx(.3)


def test_excursion_timestamp_ordering_and_target_reach():
    row = excursion(_record(), _candles(), horizon=3)
    assert row["time_to_mae"] == 1
    assert row["time_to_mfe"] == 3
    assert row["time_to_tp1"] == 2
    assert row["reached_final"]


def test_excursion_does_not_use_candle_beyond_horizon():
    row = excursion(_record(), _candles(), horizon=2)
    assert row["mfe"] == pytest.approx(1.0)
    assert row["mae"] == pytest.approx(.3)


def test_stop_distance_and_later_target_attribution():
    row = excursion(_record(), _candles(), horizon=3)
    assert row["stop_distance_pct"] == pytest.approx(1.0)
    assert row["initial_rr_tp1"] == pytest.approx(.8)
    assert row["initial_rr_final"] == pytest.approx(1.5)


def test_full_tp1_counterfactual_accounts_for_entry_and_exit_fee():
    row = counterfactual(_record(), _candles(), "full_tp1_sl")
    assert row["reason"] == "TP1"
    assert row["fees"] > .6
    assert row["net_pnl"] < row["gross_pnl"]


def test_partial_counterfactual_accounts_for_partial_fee():
    row = counterfactual(_record(), _candles(), "partial_original_sl")
    assert row["fees"] > .6
    assert row["reason"] in {"FINAL_TARGET", "HORIZON"}


@pytest.mark.parametrize("mode", ["time_4", "time_8", "time_16", "max_horizon"])
def test_time_exit_counterfactuals_are_bounded(mode):
    first = counterfactual(_record(), _candles(), mode)
    second = counterfactual(_record(), _candles(), mode)
    assert first == second


def test_bootstrap_and_monte_carlo_are_seed_deterministic():
    assert uncertainty([1, -1, 2, -2], seed=7) == uncertainty([1, -1, 2, -2], seed=7)


def test_wilson_interval_contains_observed_win_rate():
    low, high = wilson(6, 10)
    assert low < .6 < high


def test_short_excursion_is_directionally_symmetric():
    long = excursion(_record(), _candles(), horizon=3)
    short_candles = [Candle(c.timestamp_ms, 200-c.open, 200-c.low, 200-c.high, 200-c.close, c.volume_base) for c in _candles()]
    short = excursion(_record(direction="SHORT", executed_entry="100", requested_entry="100", initial_stop="101", tp1_price="99.2"), short_candles, horizon=3)
    assert short["mfe_r"] == pytest.approx(long["mfe_r"])
    assert short["mae_r"] == pytest.approx(long["mae_r"])
