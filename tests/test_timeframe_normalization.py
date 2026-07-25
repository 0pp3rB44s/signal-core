"""Regression tests for the 2026-07-25 false-healthy granularity failure.

The internal timeframe vocabulary is lowercase ("1h"), but Bitget accepts minutes
lowercase and hours/days/weeks/months UPPERCASE. Sending "1h" returns HTTP 400
code 400171. `get_multi_timeframe_candles` carried a private partial map that knew
this for 1h/4h only; every other call path, including the main scan, sent the raw
lowercase value.

The invalid request was swallowed by the per-symbol error handler, so the cycle
reached its end with zero snapshots and still published `scan_cycle_complete` with
`snapshot_count=0`. The heartbeat recorded 106 completed cycles and the dedicated
health check reported HEALTHY while every single scan had failed. Two separate
defects: a wrong value, and an observability layer that could not tell.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings
from app.runner import ScanCycleProducedNoMarketData, StartupRunner
from clients.bitget_market_client import (
    SUPPORTED_GRANULARITIES,
    UnsupportedGranularityError,
    api_granularity,
)


# --- the value that reaches the exchange -------------------------------------

def test_internal_1h_becomes_api_1H():
    """The exact defect: internal "1h" must leave as "1H"."""
    assert api_granularity("1h") == "1H"


@pytest.mark.parametrize("minute", ["1m", "3m", "5m", "15m", "30m"])
def test_lowercase_minutes_are_unchanged(minute):
    """Minutes are lowercase on Bitget and must not be uppercased."""
    assert api_granularity(minute) == minute


@pytest.mark.parametrize(
    ("internal", "expected"),
    [
        ("1h", "1H"), ("4h", "4H"), ("6h", "6H"), ("12h", "12H"),
        ("1d", "1D"), ("1w", "1W"),
        ("6hutc", "6Hutc"), ("12hutc", "12Hutc"), ("1dutc", "1Dutc"),
        ("3dutc", "3Dutc"), ("1wutc", "1Wutc"), ("1mutc", "1Mutc"),
    ],
)
def test_every_non_minute_alias_is_uppercased(internal, expected):
    assert api_granularity(internal) == expected


def test_already_correct_api_values_pass_through():
    """Callers that already hardcode the API form must keep working."""
    for value in ("1H", "4H", "1D", "1W", "15m", "1M"):
        assert api_granularity(value) == value


def test_month_and_minute_are_not_confused():
    """"1M" is one month, "1m" is one minute. Case must be preserved."""
    assert api_granularity("1M") == "1M"
    assert api_granularity("1m") == "1m"
    assert api_granularity("1M") != api_granularity("1m")


def test_supported_set_matches_the_documented_exchange_values():
    """Guards against drift from the values Bitget's 400171 message enumerates."""
    assert set(SUPPORTED_GRANULARITIES) == {
        "1m", "3m", "5m", "15m", "30m", "1H", "4H", "6H", "12H",
        "1D", "1W", "1M", "6Hutc", "12Hutc", "1Dutc", "3Dutc", "1Wutc", "1Mutc",
    }


# --- invalid values must never reach the network -----------------------------

@pytest.mark.parametrize("bad", ["", "   ", "bogus", "90m", "2h", "1y", "7d", None, "1Hutc"])
def test_invalid_timeframe_raises_before_any_network_call(bad):
    with pytest.raises(UnsupportedGranularityError):
        api_granularity(bad)


def test_get_candles_rejects_bad_granularity_without_requesting():
    """The guard sits at the top of get_candles, ahead of _request."""
    from clients.bitget_market_client import BitgetMarketClientMixin

    class Client(BitgetMarketClientMixin):
        def __init__(self):
            self.settings = Settings(_env_file=None)
            self._request = MagicMock()

    client = Client()
    with pytest.raises(UnsupportedGranularityError):
        client.get_candles("LTCUSDT", "USDT-FUTURES", granularity="1y")
    client._request.assert_not_called()


def test_get_candles_sends_the_normalized_value():
    from clients.bitget_market_client import BitgetMarketClientMixin

    class Client(BitgetMarketClientMixin):
        def __init__(self):
            self.settings = Settings(_env_file=None)
            self._request = MagicMock(return_value={"data": []})

    client = Client()
    client.get_candles("LTCUSDT", "USDT-FUTURES", granularity="1h")
    params = client._request.call_args.kwargs["params"]
    assert params["granularity"] == "1H", "the exchange must never receive '1h'"


def test_no_call_path_keeps_a_private_partial_timeframe_map():
    """The duplicate map in get_multi_timeframe_candles caused this defect."""
    module = Path(__file__).parents[1] / "clients" / "bitget_market_client.py"
    source = module.read_text(encoding="utf-8")
    assert "timeframe_mapping" not in source, (
        "normalisation must live only in api_granularity, not in a per-method map"
    )


# --- a failed scan may not report success -----------------------------------

@pytest.fixture
def runner(tmp_path, monkeypatch) -> StartupRunner:
    monkeypatch.chdir(tmp_path)
    report = Path("reports/backtests/strategy_expectancy.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"strategies": {}}), encoding="utf-8")
    settings = Settings(
        _env_file=None, FORWARD_PAPER_ONLY=True, FAST_LANE_ENABLED=False,
        MAX_OPEN_POSITIONS=1,
        BITGET_RATE_LIMIT_STATE_PATH=str(tmp_path / "rate-limit.json"),
    )
    return StartupRunner(settings)


def _all_symbols_fail(runner, exc):
    """Every symbol raises, exactly as an HTTP 400 on every candle request did."""
    runner.fetcher.fetch_contracts = MagicMock(return_value=[])
    runner.fetcher.build_market_snapshot = MagicMock(side_effect=exc)
    runner.market_data_service.refresh_many = MagicMock()
    runner.market_data_service.get_symbol_snapshot = MagicMock(return_value=None)


def test_scan_with_zero_snapshots_raises_instead_of_completing(runner):
    _all_symbols_fail(runner, RuntimeError("HTTP 400 code=400171"))
    with patch("app.runner.get_watchlist", return_value=["LTCUSDT"]):
        with pytest.raises(ScanCycleProducedNoMarketData):
            runner.scan_once()


def test_failed_scan_never_publishes_a_healthy_scan_cycle_complete(runner, tmp_path):
    """The heartbeat must not claim a completed cycle when nothing was fetched."""
    _all_symbols_fail(runner, RuntimeError("HTTP 400 code=400171"))
    stages: list[tuple[str, dict]] = []
    with patch("app.runner.runtime_heartbeat", side_effect=lambda s, **k: stages.append((s, k))):
        with patch("app.runner.get_watchlist", return_value=["LTCUSDT"]):
            assert runner._scan_cycle_iteration() is False

    published = [stage for stage, _ in stages]
    assert "scan_cycle_complete" not in published, (
        f"a scan that built no snapshot reported success: {published}"
    )
    assert "scan_cycle_failed" in published
    assert runner._consecutive_scan_failures == 1


def test_consecutive_failures_accumulate_for_the_health_check(runner):
    _all_symbols_fail(runner, RuntimeError("HTTP 400 code=400171"))
    with patch("app.runner.get_watchlist", return_value=["LTCUSDT"]):
        for expected in (1, 2, 3):
            assert runner._scan_cycle_iteration() is False
            assert runner._consecutive_scan_failures == expected


def test_a_real_snapshot_still_completes_normally(runner):
    """The guard must not fire when market data is actually returned."""
    from tests.test_public_candidate_pipeline import _detector_fixture

    runner.fetcher.fetch_contracts = MagicMock(return_value=[])
    runner.fetcher.build_market_snapshot = MagicMock(return_value=_detector_fixture())
    runner.market_data_service.refresh_many = MagicMock()
    runner.market_data_service.get_symbol_snapshot = MagicMock(return_value=None)
    stages: list[str] = []
    with patch("app.runner.runtime_heartbeat", side_effect=lambda s, **k: stages.append(s)):
        with patch("app.runner.get_watchlist", return_value=["BTCUSDT"]):
            assert runner._scan_cycle_iteration() is True
    assert "scan_cycle_complete" in stages
    assert runner._consecutive_scan_failures == 0
