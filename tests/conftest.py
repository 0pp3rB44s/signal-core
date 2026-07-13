import pytest


@pytest.fixture(autouse=True)
def _isolate_relative_writes(tmp_path, monkeypatch):
    """Run every test from a temp cwd.

    Several loggers (TradeDatasetV2Logger, StrategyPerformanceLogger, ...) default
    to relative paths like logs/trade_dataset_v2.csv. Tests that construct them
    without an explicit path used to append fake trades into the production
    learning datasets whenever pytest ran from the repo root.
    """
    monkeypatch.chdir(tmp_path)
