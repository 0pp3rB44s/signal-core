from app.config import Settings
from backtesting.backtest_engine import BacktestEngine
from clients.schemas import Candle
from unittest.mock import patch


def test_backtest_waits_for_required_hourly_warmup():
    candles = [
        Candle(
            timestamp_ms=1_700_000_000_000 + index * 900_000,
            open=100 + index * 0.01,
            high=100.1 + index * 0.01,
            low=99.9 + index * 0.01,
            close=100 + index * 0.01,
            volume_base=100 + index,
        )
        for index in range(82)
    ]
    result = BacktestEngine(Settings(_env_file=None)).run({"BTCUSDT": candles})
    assert result["trades"] == 0
    assert result["debug"]["snapshot_contract_rejected"] > 0


def test_backtest_feature_input_matches_production_candle_limit():
    candles = [
        Candle(1_700_000_000_000 + index * 900_000, 100, 101, 99, 100, 100)
        for index in range(260)
    ]
    engine = BacktestEngine(Settings(_env_file=None, BITGET_CANDLE_LIMIT=200))
    observed = []
    original = engine._build_snapshot
    def capture(symbol, history, **kwargs):
        observed.append(len(history))
        return original(symbol, history, **kwargs)
    with patch.object(engine, "_build_snapshot", side_effect=capture):
        engine.run({"BTCUSDT": candles})
    assert max(observed) == 200
