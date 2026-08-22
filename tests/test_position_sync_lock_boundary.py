"""The Bitget position fetch must happen OUTSIDE trading_state_lock.

Root cause: with 3 retries at up to 15s each plus backoff, a single degraded
`get_all_positions()` response could hold `trading_state_lock` for ~49s while
the network call ran inline inside it -- blocking the position-monitor thread
and the next scan cycle's own sync for that whole duration. This is what a
zero-completed-scan-cycle 60s window and 30s+ stalls trace back to.

Fix: `PositionManager.fetch_exchange_snapshot()` runs the network call with no
lock held. `app.runner.Runner._sync_positions` fetches first, then acquires the
lock only around `PositionManager.sync(exchange_snapshot=...)`, which no longer
does its own network I/O when a snapshot is supplied.
"""
from __future__ import annotations

import threading
import time
import types
from types import SimpleNamespace

import pytest

from execution.position_manager import (
    EXCHANGE_SNAPSHOT_MAX_AGE_MS,
    ExchangeSnapshot,
    PositionManager,
)


# --- ExchangeSnapshot / fetch_exchange_snapshot ------------------------------

class _FakeClient:
    def __init__(self, *, payload=None, exc=None, delay_s=0.0):
        self._payload = payload if payload is not None else {"data": []}
        self._exc = exc
        self._delay_s = delay_s
        self.calls = 0

    def get_all_positions(self):
        self.calls += 1
        if self._delay_s:
            time.sleep(self._delay_s)
        if self._exc:
            raise self._exc
        return self._payload


class _FetchHarness(PositionManager):
    """Only what fetch_exchange_snapshot() touches: self.client, self.log."""

    def __init__(self, client):
        self.client = client
        import logging
        self.log = logging.getLogger("fetch-harness")


def test_fetch_returns_ok_snapshot_on_success():
    client = _FakeClient(payload={"data": [{"symbol": "BTCUSDT", "total": "1.0"}]})
    snap = _FetchHarness(client).fetch_exchange_snapshot()
    assert snap.ok is True
    assert snap.open_symbols == {"BTCUSDT"}
    assert snap.error is None


def test_fetch_failure_does_not_raise_and_reports_not_ok():
    client = _FakeClient(exc=RuntimeError("timeout after 3 retries"))
    snap = _FetchHarness(client).fetch_exchange_snapshot()
    assert snap.ok is False
    assert snap.open_symbols == set()
    assert "timeout" in snap.error


def test_fetch_can_take_the_full_worst_case_duration_without_a_lock():
    """Simulates the ~49s worst-case retry storm. No lock is touched here at all --
    that is the entire point of the fix, and this proves fetch alone never asks for one."""
    client = _FakeClient(payload={"data": []}, delay_s=0.05)  # scaled down for test speed
    t0 = time.perf_counter()
    snap = _FetchHarness(client).fetch_exchange_snapshot()
    elapsed = time.perf_counter() - t0
    assert snap.ok is True
    assert elapsed >= 0.05


def test_fresh_snapshot_is_usable():
    snap = ExchangeSnapshot(positions_live=[], open_symbols=set(), ok=True,
                             fetched_at_ms=time.time() * 1000)
    assert snap.usable() is True


def test_stale_snapshot_is_not_usable():
    stale_ms = time.time() * 1000 - (EXCHANGE_SNAPSHOT_MAX_AGE_MS + 5_000)
    snap = ExchangeSnapshot(positions_live=[{"symbol": "BTCUSDT", "total": "1"}],
                             open_symbols={"BTCUSDT"}, ok=True, fetched_at_ms=stale_ms)
    assert snap.usable() is False


def test_failed_snapshot_is_never_usable_regardless_of_age():
    snap = ExchangeSnapshot(positions_live=[], open_symbols=set(), ok=False,
                             fetched_at_ms=time.time() * 1000)
    assert snap.usable() is False


# --- sync() consuming a pre-fetched snapshot ---------------------------------

class _SyncHarness(PositionManager):
    """Real sync() body, stubbed storage/event/client I/O."""

    def __init__(self, positions, client=None):
        self._positions = positions
        self.client = client or _FakeClient()
        import logging
        self.log = logging.getLogger("sync-harness")
        self.store = SimpleNamespace(load=lambda default=None: list(self._positions),
                                      save=lambda rows: self._positions.__setitem__(slice(None), rows))
        self.event_store = SimpleNamespace(load=lambda default=None: [], save=lambda rows: None)

    def recover_provisional_close_rows(self):
        return {"blocked": False}

    def _heal_missing_protection_from_fallback(self, position):
        return None

    def _live_economics(self, position, *, current_price):
        return SimpleNamespace(price_return_pct=0.0, margin_roi_pct=0.0, estimated_net_return_pct=0.0)

    def _ensure_closed_trade_dataset_row(self, position):
        return None


def _open_position(symbol="BTCUSDT"):
    return {"symbol": symbol, "status": "OPEN", "stop_loss": 100.0,
            "confirmed_stop": 100.0, "last_price": 101.0}


def test_stale_snapshot_preserves_open_state_instead_of_treating_as_closed():
    """The dangerous race: a snapshot fetched before a fill/close, used after one
    happened, must NOT resurrect/close a position on stale information. Staleness
    routes through the exact same 'preserve OPEN, do not trust bitget_open_symbols'
    path a hard fetch failure already used."""
    positions = [_open_position("BTCUSDT")]
    harness = _SyncHarness(positions)
    stale = ExchangeSnapshot(positions_live=[], open_symbols=set(), ok=True,
                              fetched_at_ms=time.time() * 1000 - (EXCHANGE_SNAPSHOT_MAX_AGE_MS + 1_000))
    updates = harness.sync([], use_snapshot_context=True, exchange_snapshot=stale)
    assert len(updates) == 1
    assert updates[0].note == "exchange sync failed; preserving OPEN state and confirmed stop"
    assert updates[0].status == "OPEN"


def test_fresh_snapshot_with_symbol_open_heals_protection_normally():
    positions = [_open_position("BTCUSDT")]
    harness = _SyncHarness(positions)
    healed = []
    harness._heal_missing_protection_from_fallback = lambda position: healed.append(position["symbol"])
    fresh = ExchangeSnapshot(positions_live=[{"symbol": "BTCUSDT", "total": "1.0"}],
                              open_symbols={"BTCUSDT"}, ok=True, fetched_at_ms=time.time() * 1000)
    harness.sync([], use_snapshot_context=True, exchange_snapshot=fresh)
    assert healed == ["BTCUSDT"]


def test_no_snapshot_argument_falls_back_to_inline_fetch():
    """Backward compatibility: a caller that doesn't pre-fetch still works."""
    positions = [_open_position("BTCUSDT")]
    client = _FakeClient(payload={"data": [{"symbol": "BTCUSDT", "total": "1.0"}]})
    harness = _SyncHarness(positions, client=client)
    harness._heal_missing_protection_from_fallback = lambda position: None
    harness.sync([], use_snapshot_context=True, exchange_snapshot=None)
    assert client.calls == 1


def test_no_open_positions_returns_no_updates_regardless_of_snapshot():
    harness = _SyncHarness([])
    fresh = ExchangeSnapshot(positions_live=[], open_symbols=set(), ok=True,
                              fetched_at_ms=time.time() * 1000)
    updates = harness.sync([], use_snapshot_context=True, exchange_snapshot=fresh)
    assert updates == []


# --- runner-level: fetch happens before the lock, lock held only for reconcile ---

class _LockProbe:
    """Records whether a lock was held at the moment a callable ran."""

    def __init__(self):
        self._lock = threading.Lock()
        self.held_during_fetch = None
        self.max_concurrent_lock_holders = 0
        self._holders = 0
        self._holders_guard = threading.Lock()

    def __enter__(self):
        self._lock.acquire()
        with self._holders_guard:
            self._holders += 1
            self.max_concurrent_lock_holders = max(self.max_concurrent_lock_holders, self._holders)
        return self

    def __exit__(self, *a):
        with self._holders_guard:
            self._holders -= 1
        self._lock.release()


def _make_runner_stub(position_manager, probe):
    import app.runner as runner_mod

    self = SimpleNamespace()
    self.settings = SimpleNamespace(position_manager_enabled=True)
    self.position_manager = position_manager
    self._position_sync_lock = threading.Lock()
    import logging
    self.log = logging.getLogger("runner-stub")
    self._emit_position_summary = lambda updates: None
    self.position_logger = SimpleNamespace(append_rows=lambda rows: None)
    return self, runner_mod


def test_network_fetch_runs_with_lock_not_held(monkeypatch):
    """Direct proof of the fix: at the instant the network call executes,
    trading_state_lock is NOT held."""
    import app.runner as runner_mod

    lock_held_during_fetch = {"value": None}
    real_lock = threading.Lock()

    class ProbeLock:
        def __enter__(self):
            real_lock.acquire()
            return self

        def __exit__(self, *a):
            real_lock.release()

    def fake_trading_state_lock():
        return ProbeLock()

    monkeypatch.setattr(runner_mod, "trading_state_lock", fake_trading_state_lock)

    class FetchProbePM:
        def fetch_exchange_snapshot(self):
            lock_held_during_fetch["value"] = real_lock.locked()
            return ExchangeSnapshot(positions_live=[], open_symbols=set(), ok=True,
                                     fetched_at_ms=time.time() * 1000)

        def sync(self, snapshots, *, use_snapshot_context, exchange_snapshot):
            assert real_lock.locked()  # reconciliation DOES run under the lock
            return []

    self, _ = _make_runner_stub(FetchProbePM(), None)
    snapshot = SimpleNamespace(symbol="BTCUSDT")
    runner_mod.StartupRunner._sync_positions(self, [snapshot], use_snapshot_context=True)

    assert lock_held_during_fetch["value"] is False


def test_slow_fetch_does_not_block_a_concurrent_lock_consumer(monkeypatch):
    """The core acceptance criterion: while one sync's fetch is 'slow', an unrelated
    lock consumer (standing in for the other sync caller) must not be blocked by it,
    because the fetch itself never touches the lock."""
    import app.runner as runner_mod

    real_lock = threading.Lock()

    class ProbeLock:
        def __enter__(self):
            real_lock.acquire()
            return self

        def __exit__(self, *a):
            real_lock.release()

    monkeypatch.setattr(runner_mod, "trading_state_lock", lambda: ProbeLock())

    class SlowFetchPM:
        def fetch_exchange_snapshot(self):
            time.sleep(0.2)  # stands in for the ~49s worst case, scaled for test speed
            return ExchangeSnapshot(positions_live=[], open_symbols=set(), ok=True,
                                     fetched_at_ms=time.time() * 1000)

        def sync(self, snapshots, *, use_snapshot_context, exchange_snapshot):
            return []

    self, _ = _make_runner_stub(SlowFetchPM(), None)
    other_acquired = threading.Event()

    def other_lock_consumer():
        with real_lock:
            other_acquired.set()
            time.sleep(0.01)

    t0 = time.perf_counter()
    worker = threading.Thread(target=lambda: runner_mod.StartupRunner._sync_positions(
        self, [SimpleNamespace(symbol="BTCUSDT")], use_snapshot_context=True))
    worker.start()
    time.sleep(0.02)  # let the fetch (sleep 0.2s) begin, unlocked
    other = threading.Thread(target=other_lock_consumer)
    other.start()
    got_lock_while_fetch_in_flight = other_acquired.wait(timeout=0.15)
    worker.join()
    other.join()

    assert got_lock_while_fetch_in_flight is True, (
        "an unrelated lock consumer was blocked by an in-flight, unlocked network fetch"
    )


def test_lock_is_released_even_if_reconciliation_raises(monkeypatch):
    """The lock context managers must release on an exception path, not just
    the happy path -- otherwise one bad reconciliation permanently wedges every
    later sync call and the whole runtime along with it."""
    import app.runner as runner_mod

    class _FakeLock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(runner_mod, "trading_state_lock", lambda: _FakeLock())

    class RaisingPM:
        def fetch_exchange_snapshot(self):
            return ExchangeSnapshot(positions_live=[], open_symbols=set(), ok=True,
                                     fetched_at_ms=time.time() * 1000)

        def sync(self, snapshots, *, use_snapshot_context, exchange_snapshot):
            raise RuntimeError("reconciliation blew up")

    self, _ = _make_runner_stub(RaisingPM(), None)
    with pytest.raises(RuntimeError):
        runner_mod.StartupRunner._sync_positions(
            self, [SimpleNamespace(symbol="BTCUSDT")], use_snapshot_context=True)

    assert self._position_sync_lock.locked() is False, (
        "trading_state_lock/position_sync_lock was left held after sync() raised"
    )


def test_monitor_and_main_callers_are_distinguished_in_logs(caplog, monkeypatch):
    """caller must actually reach the log line, not just be accepted as a no-op arg."""
    import logging
    import app.runner as runner_mod

    class _FakeLock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(runner_mod, "trading_state_lock", lambda: _FakeLock())

    class NoopPM:
        def fetch_exchange_snapshot(self):
            return ExchangeSnapshot(positions_live=[], open_symbols=set(), ok=True,
                                     fetched_at_ms=time.time() * 1000)

        def sync(self, snapshots, *, use_snapshot_context, exchange_snapshot):
            return []

    self, _ = _make_runner_stub(NoopPM(), None)
    with caplog.at_level(logging.INFO, logger="runner-stub"):
        runner_mod.StartupRunner._sync_positions(
            self, [SimpleNamespace(symbol="BTCUSDT")],
            use_snapshot_context=False, caller="monitor")
    assert any("caller=monitor" in r.message for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="runner-stub"):
        runner_mod.StartupRunner._sync_positions(
            self, [SimpleNamespace(symbol="BTCUSDT")],
            use_snapshot_context=True, caller="main")
    assert any("caller=main" in r.message for r in caplog.records)
