from types import SimpleNamespace

import pytest

from clients.schemas import Candle
from execution.execution_service import (
    ExecutionService,
    fee_feasibility,
    normal_entry_policy,
)
from market_data.volatility_engine import VolatilityEngine
from strategies.strategies.low_vol_reclaim import LowVolReclaimStrategy


def _candles(count: int = 60, *, final_range: float | None = None) -> list[Candle]:
    rows: list[Candle] = []
    price = 100.0
    for index in range(count):
        candle_range = 1.0 + ((index % 5) * 0.1)
        if final_range is not None and index == count - 1:
            candle_range = final_range
        half = candle_range / 2.0
        rows.append(
            Candle(
                timestamp_ms=index * 900_000,
                open=price,
                high=price + half,
                low=price - half,
                close=price,
                volume_base=100.0,
            )
        )
    return rows


def test_atr_gate_uses_same_atr_definition_as_strategy_market_context():
    candles = _candles(final_range=0.1)
    atr_bps, _, _ = LowVolReclaimStrategy.low_atr_gate(candles)
    engine_atr_bps = VolatilityEngine().analyze(candles[-20:])["atr_percent"] * 100.0
    assert atr_bps == pytest.approx(engine_atr_bps, abs=0.01)


def test_atr_gate_median_excludes_current_observation():
    low_current = _candles(final_range=0.1)
    high_current = _candles(final_range=20.0)

    low_atr, low_median, low_pass = LowVolReclaimStrategy.low_atr_gate(low_current)
    high_atr, high_median, high_pass = LowVolReclaimStrategy.low_atr_gate(high_current)

    assert low_median == pytest.approx(high_median)
    assert low_atr < high_atr
    assert low_pass is True
    assert high_pass is False


def test_atr_gate_fails_closed_without_frozen_history():
    with pytest.raises(ValueError, match="40 closed candles"):
        LowVolReclaimStrategy.low_atr_gate(_candles(39))


def test_v2_normal_entry_is_always_maker_only():
    settings = SimpleNamespace(
        maker_entry_enabled=False,
        maker_entry_fallback_market=True,
    )
    assert normal_entry_policy(settings, "low_vol_reclaim_v2") == (True, False)
    assert normal_entry_policy(settings, "low_vol_reclaim") == (False, True)


def test_fee_gate_uses_maker_entry_and_conservative_normal_exit_cost():
    passing = fee_feasibility(
        planned_gross_move_bps=14.0,
        maker_fee_rate=0.0002,
        normal_exit_fee_rate=0.0006,
        net_edge_buffer_bps=4.0,
    )
    blocked = fee_feasibility(
        planned_gross_move_bps=11.99,
        maker_fee_rate=0.0002,
        normal_exit_fee_rate=0.0006,
        net_edge_buffer_bps=4.0,
    )
    assert passing == {
        "planned_gross_move_bps": 14.0,
        "expected_fee_bps": 8.0,
        "minimum_required_price_movement_bps": 12.0,
        "fee_gate_pass": True,
    }
    assert blocked["fee_gate_pass"] is False


def test_v2_fee_gate_loads_actual_account_rates_and_caches_by_symbol():
    calls: list[tuple[str, str]] = []

    class Client:
        def get_trade_fee_rate(self, symbol: str, business_type: str):
            calls.append((symbol, business_type))
            return {"data": {"makerFeeRate": "0.0002", "takerFeeRate": "0.0006"}}

    service = ExecutionService.__new__(ExecutionService)
    service.client = Client()
    service.settings = SimpleNamespace(planner_minimum_net_edge_buffer_bps=4.0)
    service._account_fee_rates = {}
    plan = SimpleNamespace(
        symbol="BTCUSDT",
        geometry_entry=100.0,
        take_profits=[100.14],
    )

    first = service._v2_fee_gate(plan, 100.0)
    second = service._v2_fee_gate(plan, 100.0)

    assert calls == [("BTCUSDT", "mix")]
    assert first == second
    assert first["expected_fee_bps"] == 8.0
    assert first["fee_gate_pass"] is True
