from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.runner import StartupRunner
from execution.runtime_lock import trading_state_lock


def _runner() -> StartupRunner:
    runner = StartupRunner.__new__(StartupRunner)
    runner.log = logging.getLogger("test.position-monitor")
    runner.settings = SimpleNamespace(position_manager_enabled=True)
    runner.position_manager = MagicMock()
    runner.position_manager.sync.return_value = []
    runner.position_logger = MagicMock()
    runner._position_sync_lock = threading.Lock()
    runner._position_snapshots_lock = threading.Lock()
    runner._position_snapshots = [MagicMock()]
    return runner


def test_monitor_recovers_on_next_iteration_after_exception():
    runner = _runner()
    runner.position_manager.sync.side_effect = [RuntimeError("temporary"), []]

    assert runner._position_monitor_iteration() is False
    assert runner._position_monitor_iteration() is True
    assert runner.position_manager.sync.call_count == 2


def test_at_most_one_position_sync_runs_at_once():
    runner = _runner()
    active = 0
    maximum = 0
    guard = threading.Lock()

    def slow_sync(*_args, **_kwargs):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.03)
        with guard:
            active -= 1
        return []

    runner.position_manager.sync.side_effect = slow_sync
    threads = [threading.Thread(target=runner._position_monitor_cycle) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    assert maximum == 1


def test_position_sync_waits_for_execution_state_update(tmp_path, monkeypatch):
    runner = _runner()
    lock_path = tmp_path / "trading-state.lock"
    monkeypatch.setattr("app.runner.trading_state_lock", lambda: trading_state_lock(str(lock_path)))
    execution_started = threading.Event()
    execution_release = threading.Event()
    sync_finished = threading.Event()

    def execution_update():
        with trading_state_lock(str(lock_path)):
            execution_started.set()
            execution_release.wait(timeout=1)

    sync_thread = threading.Thread(
        target=lambda: (runner._position_monitor_cycle(), sync_finished.set())
    )
    execution_thread = threading.Thread(target=execution_update)
    execution_thread.start()
    assert execution_started.wait(timeout=1)
    sync_thread.start()
    time.sleep(0.03)
    assert not sync_finished.is_set()

    execution_release.set()
    execution_thread.join(timeout=1)
    sync_thread.join(timeout=1)
    assert sync_finished.is_set()
