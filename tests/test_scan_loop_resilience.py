"""Regression tests for the 2026-07-24 scan-loop process death.

The scan loop ran `while True: sleep(); self._scan_cycle()` with no exception
handling, while the position-monitor loop 20 lines below already wrapped every
iteration. A transient DNS failure raised BitgetRetryableError out of
`fetch_contracts` — the one unguarded network call in the cycle — and killed
the process after 431 cycles. Nothing restarted it for 20 hours.

A long-running collector must outlive the network.
"""
from __future__ import annotations

import json
import fcntl
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings
from app.runner import StartupRunner
from clients.bitget_base_client import BitgetRetryableError


@pytest.fixture
def runner(tmp_path, monkeypatch) -> StartupRunner:
    monkeypatch.chdir(tmp_path)
    learning_report = Path("reports/backtests/strategy_expectancy.json")
    learning_report.parent.mkdir(parents=True, exist_ok=True)
    learning_report.write_text(json.dumps({"strategies": {}}), encoding="utf-8")
    settings = Settings(
        _env_file=None, FORWARD_PAPER_ONLY=True, FAST_LANE_ENABLED=False,
        MAX_OPEN_POSITIONS=1,
        BITGET_RATE_LIMIT_STATE_PATH=str(tmp_path / "rate-limit.json"),
    )
    return StartupRunner(settings)


def _dns_error() -> BitgetRetryableError:
    return BitgetRetryableError(
        "Bitget request failed after retries: HTTPSConnectionPool(host='api.bitget.com', "
        "port=443): Max retries exceeded with url: /api/v2/mix/market/contracts "
        "(Caused by NameResolutionError(\"Failed to resolve 'api.bitget.com' "
        "([Errno 8] nodename nor servname provided, or not known)\"))"
    )


def test_retryable_network_error_does_not_terminate_the_loop(runner):
    """The exact 2026-07-24 exception must not escape the iteration wrapper."""
    runner._scan_cycle = MagicMock(side_effect=_dns_error())

    assert runner._scan_cycle_iteration() is False
    assert runner._consecutive_scan_failures == 1


@pytest.mark.parametrize("error", [
    BitgetRetryableError("temporary exchange outage"),
    TimeoutError("read timed out"),
    ConnectionError("connection reset by peer"),
    ValueError("unexpected payload shape"),
])
def test_no_exception_class_escapes_the_iteration(runner, error):
    runner._scan_cycle = MagicMock(side_effect=error)

    assert runner._scan_cycle_iteration() is False


def test_loop_survives_sustained_outage_then_recovers(runner):
    """Ten consecutive DNS failures, then success — the process stays alive."""
    runner._scan_cycle = MagicMock(side_effect=_dns_error())
    for expected in range(1, 11):
        assert runner._scan_cycle_iteration() is False
        assert runner._consecutive_scan_failures == expected

    runner._scan_cycle = MagicMock(return_value=None)
    assert runner._scan_cycle_iteration() is True
    assert runner._consecutive_scan_failures == 0


def test_failure_count_is_published_to_heartbeat(runner):
    """Failures must never be silent: health checks read this counter."""
    runner._scan_cycle = MagicMock(side_effect=_dns_error())

    with patch("app.runner.runtime_heartbeat") as heartbeat:
        runner._scan_cycle_iteration()

    stages = [call.args[0] for call in heartbeat.call_args_list]
    assert "scan_cycle_failed" in stages
    payload = next(
        call.kwargs for call in heartbeat.call_args_list
        if call.args and call.args[0] == "scan_cycle_failed"
    )
    assert payload["consecutive_scan_failures"] == 1
    assert payload["error_type"] == "BitgetRetryableError"


def test_fetch_contracts_dns_failure_skips_cycle_without_raising(runner):
    """fetch_contracts was the only unguarded network call in _scan_cycle."""
    runner.fetcher.fetch_contracts = MagicMock(side_effect=_dns_error())

    # Must not raise, and must not reach market data refresh.
    runner.market_data_service.refresh_many = MagicMock()
    runner._scan_cycle()

    runner.market_data_service.refresh_many.assert_not_called()


def test_fetch_contracts_failure_releases_the_scan_lock(runner):
    """Early return must unwind through finally, or every later scan is skipped."""
    runner.fetcher.fetch_contracts = MagicMock(side_effect=_dns_error())

    runner._scan_cycle()

    assert runner._scan_in_progress is False
    # A second cycle is still able to start.
    runner._scan_cycle()
    assert runner.fetcher.fetch_contracts.call_count == 2


def test_second_runner_process_cannot_enter_an_active_scan_cycle(tmp_path, monkeypatch):
    """The interprocess scan lock rejects overlap before market or execution work."""
    monkeypatch.chdir(tmp_path)
    lock_path = tmp_path / "state" / "scan_cycle.lock"
    lock_path.parent.mkdir()
    runner = StartupRunner.__new__(StartupRunner)
    runner._scan_in_progress = False
    runner._scan_lock_path = str(lock_path)
    runner.log = MagicMock()

    with lock_path.open("w") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with patch("app.runner.runtime_heartbeat") as heartbeat:
            runner._scan_cycle()

    runner.log.warning.assert_called_once_with(
        "SCAN_SKIPPED | another runner process is already scanning"
    )
    assert runner._scan_in_progress is False
    heartbeat.assert_called_once_with("scan_cycle_incomplete")
