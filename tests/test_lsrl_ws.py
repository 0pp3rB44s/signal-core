"""LSRL_V1 WebSocket layer: frame parsing (pure), the single stamping bus, and the transport (fake socket)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from lsrl.core.types import FeedMarker, RawBook, RawTrade
from lsrl.obs.observer import FeedConfig
from lsrl.ws import BITGET_PUBLIC_WS, BitgetWsFeed, EventBus, parse_frame, subscription_message

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bitget_ws_solusdt_real_capture.jsonl"
FEED = FeedConfig()


def rows():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def first(kind_channel: str, action: str | None):
    for row in rows():
        if row["raw"] == "pong":
            continue
        d = json.loads(row["raw"])
        if (d.get("event") or d.get("arg", {}).get("channel")) == kind_channel and d.get("action") == action:
            return row
    raise AssertionError((kind_channel, action))


# ------------------------------------------------------------------ parsing (pure)
def test_the_subscription_asks_for_the_deep_books_channel_and_the_trade_tape_of_one_symbol():
    msg = json.loads(subscription_message(FEED))
    assert msg["op"] == "subscribe"
    assert msg["args"] == [{"instType": "USDT-FUTURES", "channel": "books", "instId": "SOLUSDT"},
                           {"instType": "USDT-FUTURES", "channel": "trade", "instId": "SOLUSDT"}]
    assert "books5" not in subscription_message(FEED) and BITGET_PUBLIC_WS == "wss://ws.bitget.com/v2/ws/public"


def test_a_real_book_snapshot_parses_with_200_levels_per_side_and_the_exchange_fields():
    row = first("books", "snapshot")
    (ev,) = parse_frame(row["raw"], row["recv_ms"], "c1", FEED)
    assert isinstance(ev, RawBook) and ev.action == "snapshot" and ev.pseq == 0 and ev.seq and ev.exchange_ts_ms
    assert len(ev.bids) == 200 and len(ev.asks) == 200 and ev.t_ms == row["recv_ms"] and ev.conn_id == "c1"
    assert all(isinstance(p, str) and isinstance(s, str) for p, s in ev.bids[:3])      # strings preserved as received


def test_a_real_book_update_carries_seq_and_pseq():
    row = first("books", "update")
    (ev,) = parse_frame(row["raw"], row["recv_ms"], "c1", FEED)
    assert ev.action == "update" and ev.seq > ev.pseq > 0 and ev.exchange_ts_ms


def test_real_trade_frames_split_into_one_event_per_trade_with_the_taker_side_and_id():
    row = first("trade", "update")
    events = parse_frame(row["raw"], row["recv_ms"], "c1", FEED)
    payload = json.loads(row["raw"])["data"]
    assert len(events) == len(payload) >= 1 and all(isinstance(e, RawTrade) for e in events)
    assert [e.trade_id for e in events] == [str(t["tradeId"]) for t in payload]
    assert {e.side for e in events} <= {"buy", "sell"} and all(e.action == "update" for e in events)
    hist = first("trade", "snapshot")
    assert all(e.action == "snapshot" for e in parse_frame(hist["raw"], hist["recv_ms"], "c1", FEED))


def test_subscribe_acks_and_errors_become_markers():
    ack = first("subscribe", None)
    assert parse_frame(ack["raw"], 5, "c1", FEED) == [FeedMarker(5, "SUBSCRIBED", "c1", "books")] or \
        parse_frame(ack["raw"], 5, "c1", FEED)[0].kind == "SUBSCRIBED"
    err = json.dumps({"event": "error", "code": 30001, "msg": "bad", "arg": {}})
    (m,) = parse_frame(err, 7, "c1", FEED)
    assert m.kind == "SUBSCRIBE_ERROR" and "30001" in m.detail


def test_pong_is_not_a_market_event_and_junk_becomes_a_marker_never_an_exception():
    assert parse_frame("pong", 1, "c", FEED) == []
    for junk in ("{not json", "[]", "42", "null", "", "\x00\x01"):
        (m,) = parse_frame(junk, 1, "c", FEED)
        assert m.kind == "BAD_FRAME"


def test_frames_for_another_symbol_or_channel_are_ignored():
    other = json.dumps({"action": "update", "arg": {"channel": "books", "instId": "BTCUSDT"},
                        "data": [{"bids": [], "asks": [], "ts": "1", "seq": 2, "pseq": 1}]})
    assert parse_frame(other, 1, "c", FEED) == []
    ticker = json.dumps({"action": "snapshot", "arg": {"channel": "ticker", "instId": "SOLUSDT"}, "data": [{}]})
    assert parse_frame(ticker, 1, "c", FEED) == []


def test_missing_or_garbled_numbers_become_none_so_the_observer_can_refuse_them():
    frame = json.dumps({"action": "update", "arg": {"channel": "books", "instId": "SOLUSDT"}, "ts": "999",
                        "data": [{"bids": [["1", "2"]], "asks": [["bad"], 5], "ts": "oops", "seq": "x"}]})
    (ev,) = parse_frame(frame, 3, "c", FEED)
    assert ev.seq is None and ev.pseq is None and ev.exchange_ts_ms == 999          # falls back to the outer ts
    assert ev.bids == (("1", "2"),) and ev.asks == ()


def test_every_real_frame_parses_without_error():
    for row in rows():
        parse_frame(row["raw"], row["recv_ms"], "c", FEED)


# ------------------------------------------------------------------ the stamping bus
class Clock:
    def __init__(self, t=0):
        self.t = t

    def __call__(self):
        return self.t


def test_stamping_is_monotonic_even_when_the_wall_clock_steps_back():
    clock = Clock(1_000)
    bus = EventBus(clock)
    a = bus.push(lambda t: t)
    clock.t = 500
    b = bus.push(lambda t: t)
    assert (a, b) == (1_000, 1_000)
    bus.floor(5_000)
    assert bus.push(lambda t: t) == 5_000 and bus.now() == 5_000


def test_drain_is_fifo_and_empties_the_queue():
    bus = EventBus(Clock(10))
    for i in range(3):
        bus.push(lambda t, i=i: (i, t))
    assert len(bus) == 3 and [x[0] for x in bus.drain()] == [0, 1, 2] and len(bus) == 0


def test_push_many_shares_one_receipt_time():
    bus = EventBus(Clock(77))
    assert bus.push_many(lambda t: [t, t, t]) == [77, 77, 77] and bus.drain() == [77, 77, 77]


def test_queue_order_is_time_order_under_real_concurrency():
    """Stamping and enqueueing are one atomic step, so many producer threads can never enqueue out of order."""
    import time as _time
    bus = EventBus(lambda: int(_time.perf_counter() * 1_000_000))
    stop = threading.Event()

    def producer():
        while not stop.is_set():
            bus.push(lambda t: t)

    threads = [threading.Thread(target=producer) for _ in range(6)]
    for th in threads:
        th.start()
    _time.sleep(0.3)
    stop.set()
    for th in threads:
        th.join()
    stamps = bus.drain()
    assert len(stamps) > 1_000 and stamps == sorted(stamps)


# ------------------------------------------------------------------ transport (fake socket)
class FakeSocket:
    def __init__(self, fail_send: bool = False) -> None:
        self.sent: list[str] = []
        self.closed = 0
        self.fail_send = fail_send

    def send(self, data):
        if self.fail_send:
            raise OSError("broken pipe")
        self.sent.append(data)

    def close(self):
        self.closed += 1


class Mono:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(**kw):
    bus, mono = EventBus(Clock(1_000)), Mono()
    feed = BitgetWsFeed(bus, FEED, clock_s=mono, **kw)
    return feed, bus, mono


def events(bus):
    return bus.drain()


def test_on_open_records_a_connect_marker_and_subscribes_exactly_once():
    feed, bus, _ = make()
    ws = FakeSocket()
    feed.on_open(ws)
    (m,) = events(bus)
    assert m.kind == "CONNECT" and m.conn_id == feed.conn_id and feed.connected
    assert ws.sent == [subscription_message(FEED)]


def test_frames_are_parsed_stamped_and_tagged_with_the_connection():
    feed, bus, _ = make()
    ws = FakeSocket()
    feed.on_open(ws)
    events(bus)
    row = first("books", "snapshot")
    feed.on_message(ws, row["raw"])
    (ev,) = events(bus)
    assert isinstance(ev, RawBook) and ev.conn_id == feed.conn_id and ev.t_ms == 1_000
    assert feed.stats["frames"] == 1


def test_a_superseded_socket_can_never_feed_the_bus():
    feed, bus, _ = make()
    old, new = FakeSocket(), FakeSocket()
    feed.on_open(old)
    feed.on_open(new)
    events(bus)
    feed.on_message(old, first("books", "update")["raw"])
    feed.on_close(old, 1000, "late")
    assert events(bus) == [] and feed.connected


def test_pong_is_swallowed_and_counted_and_keeps_the_connection_alive():
    feed, bus, mono = make()
    ws = FakeSocket()
    feed.on_open(ws)
    events(bus)
    mono.t = 50.0
    feed.on_message(ws, "pong")
    assert events(bus) == [] and feed.stats["pongs"] == 1 and feed._last_frame_s == 50.0


def test_a_bad_frame_is_recorded_as_a_marker_and_counted():
    feed, bus, _ = make()
    ws = FakeSocket()
    feed.on_open(ws)
    events(bus)
    feed.on_message(ws, "{garbage")
    (m,) = events(bus)
    assert m.kind == "BAD_FRAME" and feed.stats["bad_frames"] == 1


def test_a_subscribe_error_closes_the_socket_and_records_why():
    feed, bus, _ = make()
    ws = FakeSocket()
    feed.on_open(ws)
    events(bus)
    feed.on_message(ws, json.dumps({"event": "error", "code": 30001, "msg": "no", "arg": {}}))
    assert ws.closed == 1
    feed.on_close(ws, 1006, "x")
    kinds = [e.kind for e in events(bus)]
    assert kinds == ["SUBSCRIBE_ERROR", "DISCONNECT"]


def test_close_emits_one_disconnect_with_the_reason_and_marks_the_feed_disconnected():
    feed, bus, _ = make()
    ws = FakeSocket()
    feed.on_open(ws)
    events(bus)
    feed.on_close(ws, 1006, "abnormal")
    feed.on_close(ws, 1006, "abnormal")                                             # a second callback is ignored
    (m,) = events(bus)
    assert m.kind == "DISCONNECT" and "1006" in m.detail and not feed.connected


def test_ping_is_sent_on_schedule_and_only_then():
    feed, bus, mono = make(ping_interval_s=25.0)
    ws = FakeSocket()
    feed.on_open(ws)
    mono.t = 24.9
    feed.tick()
    assert "ping" not in ws.sent
    mono.t = 25.0
    feed.on_message(ws, "pong")                                                     # server alive
    feed.tick()
    assert ws.sent.count("ping") == 1
    mono.t = 30.0
    feed.tick()
    assert ws.sent.count("ping") == 1


def test_a_silent_connection_is_killed_by_the_watchdog_and_the_reason_is_recorded():
    feed, bus, mono = make(silence_timeout_s=60.0)
    ws = FakeSocket()
    feed.on_open(ws)
    events(bus)
    mono.t = 60.0
    feed.tick()
    assert ws.closed == 0                                                           # exactly at the limit
    mono.t = 61.0
    feed.tick()
    assert ws.closed == 1
    feed.on_close(ws, 1006, "x")
    assert events(bus)[0].detail.startswith("SILENT_")


def test_a_failed_ping_closes_the_connection():
    feed, bus, mono = make(ping_interval_s=1.0)
    ws = FakeSocket(fail_send=False)
    feed.on_open(ws)
    events(bus)
    ws.fail_send = True
    mono.t = 2.0
    feed.on_message(ws, "pong")
    feed.tick()
    assert ws.closed == 1
    feed.on_close(ws)
    assert events(bus)[0].detail.startswith("PING_FAILED")


def test_a_failed_subscribe_send_does_not_leave_a_dead_socket_open():
    feed, bus, _ = make()
    ws = FakeSocket(fail_send=True)
    feed.on_open(ws)
    assert ws.closed == 1


def test_reconnect_requests_are_rate_limited_and_recorded_with_their_reason():
    feed, bus, mono = make(min_reconnect_gap_s=2.0)
    ws = FakeSocket()
    feed.on_open(ws)
    events(bus)
    mono.t = 10.0
    assert feed.request_reconnect("SEQ_GAP") is True and ws.closed == 1
    assert feed.request_reconnect("SEQ_GAP") is False                               # too soon
    feed.on_close(ws)
    assert events(bus)[0].detail == "RECONNECT_REQUESTED:SEQ_GAP" and feed.stats["reconnects"] == 1
    assert feed.request_reconnect("again") is False                                 # nothing connected


def test_the_run_loop_reconnects_with_backoff_and_a_fresh_connection_id_each_time():
    bus = EventBus(Clock(1_000))
    sockets: list = []

    class App:
        def __init__(self, url, on_open, on_message, on_close, on_error):
            self.cb = (on_open, on_close)
            sockets.append(self)

        def run_forever(self, sslopt=None):
            ws = FakeSocket()
            self.cb[0](ws)
            self.cb[1](ws, 1006, "drop")

    feed = BitgetWsFeed(bus, FEED, ws_factory=App, backoff_s=(0.01,))
    feed.start()
    deadline = threading.Event()
    for _ in range(200):
        if len(sockets) >= 3:
            break
        deadline.wait(0.01)
    feed.stop()
    assert len(sockets) >= 3
    kinds = [e.kind for e in bus.drain()]
    assert kinds.count("CONNECT") >= 3 and kinds.count("DISCONNECT") >= 3


def test_status_reports_connection_state_and_counters():
    feed, bus, mono = make()
    assert feed.status()["connected"] is False
    feed.on_open(FakeSocket())
    st = feed.status()
    assert st["connected"] and st["connections"] == 1 and st["conn_id"] == feed.conn_id
