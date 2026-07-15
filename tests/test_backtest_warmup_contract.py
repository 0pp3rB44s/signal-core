from app.config import Settings
from backtesting.backtest_engine import BacktestEngine
from clients.schemas import Candle


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
