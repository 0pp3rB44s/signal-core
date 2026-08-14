"""One heartbeat per connection, and never one that outlives its socket.

The collector reconnects on every dropped WebSocket. Before this was fixed each
reconnect started a heartbeat thread that read the *shared* ``self.ws`` and only
exited on the process-wide stop event, so N reconnects left N threads pinging
whichever socket happened to be current. In production that reached 26,622
``MICROFLOW_PING_FAILED`` lines and 22 Bitget ``30007`` request-over-limit
rejections across 11 reconnects.

These tests reconnect many times and assert the thread population directly,
rather than asserting on log text, so they fail if the leak returns in any form.
"""

from __future__ import annotations

import threading

from microflow.collector import MicroflowCollector


class FakeWS:
    """A socket that records its own pings and can refuse to send."""

    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.pings = 0
        self.subscribed = False
        self.closed = False
        self.lock = threading.Lock()

    def send(self, payload: str) -> None:
        if self.fail:
            raise ConnectionError(f"{self.name}: connection is already closed")
        with self.lock:
            if payload == "ping":
                self.pings += 1
            else:
                self.subscribed = True

    def close(self) -> None:
        self.closed = True


def make_collector(tmp_path, **kwargs) -> MicroflowCollector:
    # A tiny interval keeps the test fast; the loop still wakes on the event.
    return MicroflowCollector(symbols=("BTCUSDT",), data_dir=tmp_path,
                              ping_interval=0.01, **kwargs)


def live_ping_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate()
            if t.name.startswith("microflow-ping-") and t.is_alive()]


def test_many_reconnects_leave_exactly_one_heartbeat(tmp_path):
    """50 reconnects must never accumulate heartbeat threads."""
    collector = make_collector(tmp_path)
    try:
        for i in range(50):
            ws = FakeWS(f"ws-{i}")
            collector.ws = ws
            collector._on_open(ws)
            assert len(live_ping_threads()) == 1, f"leak after {i + 1} connections"
            collector._on_close(ws, 1006, "dropped")
            assert live_ping_threads() == [], f"heartbeat survived close {i + 1}"
    finally:
        collector.stop()


def test_retired_heartbeat_cannot_ping_the_replacement_socket(tmp_path):
    """The core defect: an old thread reaching the socket that replaced it."""
    collector = make_collector(tmp_path)
    try:
        old = FakeWS("old")
        collector.ws = old
        collector._on_open(old)

        # The connection is replaced without a close callback -- the worst case.
        new = FakeWS("new")
        collector.ws = new
        collector._on_open(new)

        old_pings_at_swap = old.pings
        threading.Event().wait(0.15)  # several ping intervals

        assert len(live_ping_threads()) == 1
        assert old.pings == old_pings_at_swap, "retired heartbeat still pinging"
        assert new.pings > 0, "current connection is not being pinged"
    finally:
        collector.stop()


def test_ping_loop_refuses_a_socket_it_no_longer_owns(tmp_path):
    """The guard itself, with the stop event deliberately left unset.

    ``_on_open`` normally retires the previous heartbeat through its event, so
    that path alone cannot prove the loop checks ownership. Here the event is
    never set -- exactly what a leaked thread looks like -- and the loop must
    still refuse to touch the current socket and exit on its own.
    """
    collector = make_collector(tmp_path)
    stale, current = FakeWS("stale"), FakeWS("current")
    collector.ws = current
    conn_stop = threading.Event()  # never set: this heartbeat was leaked

    thread = threading.Thread(target=collector._ping_loop, args=(stale, conn_stop), daemon=True)
    thread.start()
    thread.join(timeout=2.0)

    assert not thread.is_alive(), "leaked heartbeat never exited"
    assert current.pings == 0, "leaked heartbeat pinged the socket that replaced it"
    assert stale.pings == 0


def test_ping_failure_retires_that_heartbeat(tmp_path):
    """Fail closed: a socket that cannot be pinged is not retried forever."""
    collector = make_collector(tmp_path)
    try:
        ws = FakeWS("broken", fail=False)
        collector.ws = ws
        collector._on_open(ws)
        ws.fail = True
        threading.Event().wait(0.15)
        assert live_ping_threads() == [], "heartbeat kept retrying a dead socket"
    finally:
        collector.stop()


def test_subscription_failure_starts_no_heartbeat(tmp_path):
    """No subscription means no data; a heartbeat would hold a socket we do not use."""
    collector = make_collector(tmp_path)
    try:
        ws = FakeWS("no-sub", fail=True)
        collector.ws = ws
        collector._on_open(ws)
        assert live_ping_threads() == []
        assert ws.closed, "failed subscription must close the socket"
    finally:
        collector.stop()


def test_subscription_is_still_sent_on_open(tmp_path):
    """The fix must not disturb 24/24 subscription readiness."""
    collector = make_collector(tmp_path)
    try:
        ws = FakeWS("ok")
        collector.ws = ws
        collector._on_open(ws)
        assert ws.subscribed
        assert collector.subscription_acks == set()
    finally:
        collector.stop()


def test_stop_shuts_every_heartbeat_down(tmp_path):
    collector = make_collector(tmp_path)
    ws = FakeWS("last")
    collector.ws = ws
    collector._on_open(ws)
    assert len(live_ping_threads()) == 1
    collector.stop()
    assert live_ping_threads() == []
    assert collector.ping_thread is None
