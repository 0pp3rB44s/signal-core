"""LSRL_V1: candle feed, and the runtime loop over the RAW WebSocket pipeline (SHADOW / PAPER).

Covers: REST candles (bars only), exchange-metadata -> rules (real SOLUSDT tick 0.001), the runtime turning raw
frames into decisions, the restart boundary (SESSION_START marker, persisted engine + simulated-exchange state,
replay across a restart), feed supervision (reconnect requests), the single stamping bus, build_runtime wiring,
and structural guarantees that no REST order-book path exists in the strategy runtime.
"""
from __future__ import annotations

import ast
import json
import logging
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from execution.state_store import JsonStateStore
from lsrl.adapters.sim import SimExchange
from lsrl.contract import rules_from_contract_spec
from lsrl.core.config import DEFAULT_CONFIG as CFG
from lsrl.core.engine import LSRLEngine
from lsrl.core.gates import NullRiskGate
from lsrl.core.types import BarClosed, FeedMarker, OrderEvent, RawBook, RawTrade
from lsrl.driver import SimDriver
from lsrl.feed import CandleFeed
from lsrl.io.gate_log import RecordingRiskGate
from lsrl.io.recorder import LSRLRecorder, decision_files, input_files, read_events
from lsrl.io.replay import decisions_match, replay
from lsrl.modes import Mode
from lsrl.obs.observer import NO_BOOK, OK, UNSAFE, BookObserver, FeedConfig
from lsrl.runtime import LSRLRuntime, build_runtime, load_rules, resolve_git_sha
from lsrl.ws import EventBus
from tests.lsrl_fixtures import RULES, S, warmup
from tests.lsrl_raw_feed import Truth, to_raw
from tests.test_lsrl_causal_timelines import timeline_a

REPO = Path(__file__).resolve().parents[1]
MIN = 60_000
T0 = 1_800_000_000_000


# ------------------------------------------------------------------ fakes
class FakeCandleClient:
    def __init__(self) -> None:
        self.rows: dict[str, list] = {"1m": [], "15m": []}
        self.calls: list[tuple] = []

    def get_candles(self, symbol, product_type, granularity="15m", limit=200):
        self.calls.append(("candles", symbol, granularity, limit))
        return {"data": list(self.rows[granularity])}


def row(open_ts: int, o=150.0, h=150.2, l=149.8, c=150.0, v=10.0):
    return [str(open_ts), str(o), str(h), str(l), str(c), str(v), "1500"]      # Bitget returns strings


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self):
        return self.t


def candles_at(client, now: int, **kw):
    return CandleFeed(client, "solusdt", "USDT-FUTURES", now_ms=Clock(now), **kw)


# ------------------------------------------------------------------ candle feed (REST: bars only)
def test_a_still_forming_candle_is_never_emitted():
    client = FakeCandleClient()
    client.rows["1m"] = [row(T0), row(T0 + MIN)]
    feed = candles_at(client, T0 + MIN + 30_000)                  # T0 closed; T0+MIN is 30 s old
    assert [b.open_ts_ms for b in feed.bars(60, 5)] == [T0]


def test_a_candle_is_only_emitted_after_the_publication_lag():
    client = FakeCandleClient()
    client.rows["1m"] = [row(T0)]
    clock = Clock(T0 + MIN + 1_999)                                # closed 1.999 s ago: the exchange may still amend it
    feed = CandleFeed(client, "SOLUSDT", "USDT-FUTURES", now_ms=clock)
    assert feed.bars(60, 5) == []
    clock.t = T0 + MIN + 2_000
    assert [b.open_ts_ms for b in feed.bars(60, 5)] == [T0]


def test_bars_are_delivered_once_in_ascending_order_whatever_the_api_order():
    client = FakeCandleClient()
    client.rows["1m"] = [row(T0 + 2 * MIN), row(T0), row(T0 + MIN)]
    feed = candles_at(client, T0 + 10 * MIN)
    assert [b.open_ts_ms for b in feed.bars(60, 5)] == [T0, T0 + MIN, T0 + 2 * MIN]
    assert feed.bars(60, 5) == []
    client.rows["1m"].append(row(T0 + 3 * MIN))
    assert [b.open_ts_ms for b in feed.bars(60, 5)] == [T0 + 3 * MIN]


def test_bar_fields_and_timeframe_are_correct():
    client = FakeCandleClient()
    client.rows["15m"] = [row(T0, o=1, h=4, l=0.5, c=3, v=99)]
    feed = candles_at(client, T0 + 30 * MIN)
    (b,) = feed.bars(900, 3)
    assert (b.tf_s, b.open_ts_ms, b.close_ts_ms) == (900, T0, T0 + 900_000)
    assert (b.open, b.high, b.low, b.close, b.volume) == (1, 4, 0.5, 3, 99)
    assert ("candles", "SOLUSDT", "15m", 3) in client.calls


def test_malformed_candle_rows_are_dropped_and_timeframes_are_independent():
    client = FakeCandleClient()
    client.rows["1m"] = [["short"], "junk", None, row(T0)]
    client.rows["15m"] = [row(T0)]
    feed = candles_at(client, T0 + 60 * MIN)
    assert [b.open_ts_ms for b in feed.bars(60, 5)] == [T0] and len(feed.bars(900, 5)) == 1
    assert feed.bars(60, 5) == [] and feed.bars(900, 5) == []


def test_after_a_restart_bars_the_engine_already_consumed_are_not_delivered_again():
    client = FakeCandleClient()
    client.rows["1m"] = [row(T0), row(T0 + MIN), row(T0 + 2 * MIN)]
    feed = candles_at(client, T0 + 10 * MIN)
    feed.seed(last_open_1m=T0 + MIN, last_open_15m=None)
    assert [b.open_ts_ms for b in feed.bars(60, 5)] == [T0 + 2 * MIN]


def test_there_is_no_rest_order_book_or_trade_path_anywhere_in_the_strategy_runtime():
    """Sweep classification evidence is WebSocket-only: REST depth/trades must not be reachable from lsrl/.
    (The words may appear in prose that explains the rule; a call or attribute access may not.)"""
    calls = []
    for path in (REPO / "lsrl").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in ("get_orderbook", "get_recent_fills", "get_fills"):
                calls.append((path.name, node.attr))
    assert calls == []
    assert not hasattr(CandleFeed, "depth")


# ------------------------------------------------------------------ exchange metadata -> rules
def spec(**kw):
    base = dict(symbol="SOLUSDT", product_type="USDT-FUTURES", min_trade_num=Decimal("0.1"),
                size_multiplier=Decimal("0.1"), volume_place=1, price_place=3, price_end_step=Decimal("1"),
                min_trade_usdt=Decimal("5"), source="live", retrieved_at=0.0, fallback_used=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_the_real_solusdt_metadata_gives_tick_0_001_not_the_old_test_value():
    r = rules_from_contract_spec(spec(), "solusdt")
    assert (r.symbol, r.tick_size, r.qty_step, r.min_qty, r.min_notional_usdt) == ("SOLUSDT", 0.001, 0.1, 0.1, 5.0)
    assert rules_from_contract_spec(spec(price_end_step=Decimal("5")), "SOLUSDT").tick_size == 0.005


def test_the_test_fixture_rules_are_the_real_exchange_metadata():
    assert RULES.tick_size == 0.001 and RULES.qty_step == 0.1 and RULES.min_qty == 0.1 and RULES.min_notional_usdt == 5.0


def test_unknown_min_notional_falls_back_to_the_exchange_wide_floor_not_zero():
    assert rules_from_contract_spec(spec(min_trade_usdt=None), "SOLUSDT").min_notional_usdt == 5.0


def test_unknown_contract_metadata_refuses_to_run():
    with pytest.raises(RuntimeError, match="CONTRACT_METADATA_UNAVAILABLE"):
        rules_from_contract_spec(None, "SOLUSDT")


# ------------------------------------------------------------------ runtime harness (raw pipeline, manual clocks)
class FakeWs:
    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.reconnects: list[str] = []
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def request_reconnect(self, reason: str) -> bool:
        self.reconnects.append(reason)
        return True

    def status(self):
        return {"connected": self.connected}


class FakeCandles:
    def __init__(self) -> None:
        self.seeded = None

    def seed(self, **kw):
        self.seeded = kw

    def bars(self, tf_s, limit):
        return []


class Rig:
    """A runtime wired exactly like build_runtime for SHADOW/PAPER, but with manual clocks and no network."""

    def __init__(self, tmp_path: Path, mode: Mode = Mode.SHADOW, session: int = 1, start_ms: int = 0) -> None:
        self.tmp = tmp_path
        self.feed_cfg = FeedConfig()
        self.recorder = LSRLRecorder(tmp_path / "rec", CFG, RULES, mode=mode.value, feed=asdict(self.feed_cfg),
                                     git_sha="abc123", session_id=f"s{session}", min_free_gb=0.001)
        holder: dict = {}
        self.engine = LSRLEngine(CFG, RULES, risk_gate=RecordingRiskGate(NullRiskGate(), lambda *a: holder["d"].gate_sink(*a)),
                                 tape_available=True)
        self.observer = BookObserver(self.feed_cfg)
        self.driver = SimDriver(mode, self.engine, SimExchange(RULES, CFG), start_nav_usdt=1_000.0,
                                recorder=self.recorder, observer=self.observer)
        holder["d"] = self.driver
        self.bus_clock, self.mono = Clock(start_ms), Clock(0.0)
        self.ws, self.candles = FakeWs(), FakeCandles()
        self.rt = LSRLRuntime(mode=mode, engine=self.engine, driver=self.driver, bus=EventBus(self.bus_clock),
                              ws=self.ws, candles=self.candles,
                              state_store=JsonStateStore(str(tmp_path / "state.json")), git_sha="abc123",
                              session_id=f"s{session}", clock=self.mono)

    def start(self):
        self.rt.restore()
        self.rt.begin_session()

    def push(self, ev) -> None:
        self.bus_clock.t = max(self.bus_clock.t, ev.t_ms)
        self.rt.bus.push(lambda t: replace(ev, t_ms=t))

    def run(self, events, *, chunk: int = 40) -> None:
        for i, ev in enumerate(events, 1):
            self.push(ev)
            if i % chunk == 0:
                self.step()
        self.step()

    def step(self) -> None:
        self.mono.t += 100.0                                    # every cadence interval is due
        self.rt.step()

    def close(self) -> None:
        self.recorder.close()


def timeline_events():
    events, _ = timeline_a()
    return events


def test_the_runtime_turns_raw_frames_into_a_ladder_and_reports_a_healthy_feed(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    rig.run(timeline_events())
    s = rig.engine.setup
    assert s is not None and s.state == "LADDER_PENDING" and s.consumption["method"] == "TAPE"
    st = rig.rt.status()
    assert st["feed"]["state"] == OK and st["signal_allowed"] is True and rig.rt.last_error is None
    assert st["git_sha"] == "abc123" and st["feed"]["window"] is not None


class HistoryCandles(FakeCandles):
    def bars(self, tf_s, limit):
        return [e for e in warmup(nav=None) if isinstance(e, BarClosed) and e.tf_s == tf_s][-limit:]


def test_warmup_feeds_history_without_asking_for_nav_and_without_decisions(tmp_path):
    rig = Rig(tmp_path, start_ms=S)
    rig.rt.candles = HistoryCandles()
    rig.start()
    rig.rt._warmup()
    e = rig.engine
    assert e.atr15.value is not None and e.atr1m.value is not None and e.now_ms > 0
    assert e.nav_usdt is None and e.setup is None                   # history never asks for NAV, never opens a setup
    assert [r.kind for r in rig.driver.records if r.kind != "feed_health"] == []


def test_state_written_by_another_mode_is_refused(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    rig.run(timeline_events()[:80])
    rig.close()
    other = tmp_path / "other"
    other.mkdir()
    (other / "state.json").write_bytes((tmp_path / "state.json").read_bytes())
    paper = Rig(other, mode=Mode.PAPER)
    with pytest.raises(RuntimeError, match="mode SHADOW, not PAPER"):
        paper.rt.restore()


def test_the_first_recorded_event_of_a_session_is_the_session_start_marker_with_the_restored_state(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    rig.run(timeline_events()[:60])
    rig.close()
    first = next(iter(read_events(input_files(tmp_path / "rec"))))
    assert isinstance(first, FeedMarker) and first.kind == "SESSION_START"
    assert '"engine_state"' in first.detail and '"session_id":"s1"' in first.detail


def test_engine_and_simulated_exchange_state_survive_a_restart_and_the_session_boundary_then_cancels_the_ladder(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    rig.run(timeline_events())
    setup_id, orders = rig.engine.setup.setup_id, dict(rig.driver.sim.orders)
    assert orders
    rig.close()
    again = Rig(tmp_path, session=2, start_ms=S + 70_000)
    again.rt.restore()                                                      # what a restart restores ...
    assert again.engine.setup is not None and again.engine.setup.setup_id == setup_id
    assert set(again.driver.sim.orders) == set(orders) and again.driver.nav == rig.driver.nav
    assert again.candles.seeded == {"last_open_1m": rig.engine.last_bar1m_open_ts,
                                    "last_open_15m": rig.engine.last_bar15_open_ts}
    again.rt.begin_session()                                                # ... and what the boundary does with it
    assert again.rt.status()["feed"]["state"] == NO_BOOK                    # a new process has NO book until a snapshot
    assert again.engine.setup is None                                       # no market view => stale rule cancels the ladder
    closed = [r.data for r in again.driver.records if r.kind == "setup_closed"]
    assert closed and closed[0]["close_reason"] == "LIQUIDITY_DATA_STALE" and closed[0]["setup_id"] == setup_id


def test_replay_across_a_restart_reproduces_the_decisions_of_both_sessions(tmp_path):
    """Session 1 ends with a pending ladder; session 2 restarts, has no market view, and (stale-data rule)
    cancels it. A fresh engine replaying the raw log must take the same decisions across the boundary."""
    first = Rig(tmp_path)
    first.start()
    first.run(timeline_events())
    assert first.engine.setup is not None
    first.close()
    quiet = Truth()
    quiet.book(S + 90_000, 149.95, 150.05)                        # a NEW connection and a NEW snapshot
    second = Rig(tmp_path, session=2, start_ms=S + 70_000)
    second.start()
    second.run(to_raw(quiet, keepalive_until_ms=S + 100_000, bars=[]))
    second.close()
    files = input_files(tmp_path / "rec")
    kinds = [b.kind for b in read_events(files) if isinstance(b, FeedMarker)]
    assert kinds.count("SESSION_START") == 2
    replayed = replay(files, anchor=tmp_path / "rec" / "anchor-inputs.jsonl")
    assert replayed.sessions == 2 and replayed.restart_state_mismatches == []
    assert decisions_match(files, decision_files(tmp_path / "rec"))[0]
    assert replayed.engine.snapshot() == second.engine.snapshot()
    assert second.engine.setup is None                            # the restored ladder was cancelled and closed


# ------------------------------------------------------------------ feed supervision
def gapped_update(previous: list, t_ms: int) -> RawBook:
    """The next update after a LOST packet: its pseq names a message we never received."""
    last = [e for e in previous if isinstance(e, RawBook)][-1]
    return RawBook(t_ms=t_ms, exchange_ts_ms=t_ms - 50, action="update", seq=last.seq + 900, pseq=last.seq + 450,
                   bids=(("149.9", "1"),), asks=(), conn_id=last.conn_id)


def test_an_unsafe_view_makes_the_runtime_ask_the_transport_for_a_fresh_snapshot(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    events = timeline_events()
    rig.run(events[:80])
    assert rig.observer.state == OK and rig.ws.reconnects == []
    rig.push(gapped_update(events[:80], events[80].t_ms))         # pseq does not chain: a packet was lost
    rig.step()
    assert rig.observer.state == UNSAFE and rig.ws.reconnects and rig.ws.reconnects[0].startswith("SEQ_GAP")
    assert rig.engine.signal_allowed is False


def test_no_snapshot_after_a_connect_triggers_a_reconnect_after_the_grace_period(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    rig.push(FeedMarker(1_000, "CONNECT", "c1"))
    rig.step()
    assert rig.observer.state == NO_BOOK and rig.ws.reconnects == []          # within the grace period
    for _ in range(300):
        rig.step()
    assert rig.ws.reconnects[-1] == "NO_SNAPSHOT_AFTER_CONNECT"


def test_a_disconnected_transport_is_left_to_its_own_backoff(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    rig.ws.connected = False
    for _ in range(300):
        rig.step()
    assert rig.ws.reconnects == []


# ------------------------------------------------------------------ the one stamping bus
class StubDriver:
    def __init__(self) -> None:
        self.observer = BookObserver()
        self.fed: list = []

    def feed(self, ev, *, nav=True):
        self.fed.append(ev)

    def poll_events(self, t_ms):
        return [OrderEvent(t_ms=1, setup_id="x", tag="t", status="CANCELLED")]

    def state_snapshot(self):
        return {}

    def state_restore(self, s):
        return None


def test_executor_events_are_queued_behind_earlier_market_frames_on_the_same_bus(tmp_path):
    """The engine must see ONE non-decreasing timeline: an executor event stamped 'now' may never overtake a
    market frame that was received earlier but not yet processed."""
    clock, mono = Clock(0), Clock(0.0)
    bus = EventBus(clock)
    driver = StubDriver()
    engine = SimpleNamespace(halt=lambda r: None, last_bar1m_open_ts=None, last_bar15_open_ts=None,
                             snapshot=lambda: {}, signal_allowed=True, halted_reason=None)
    rt = LSRLRuntime(mode=Mode.MICRO_LIVE, engine=engine, driver=driver, bus=bus, ws=FakeWs(), candles=FakeCandles(),
                     state_store=JsonStateStore(str(tmp_path / "s.json")), clock=mono)
    clock.t = 1_000
    bus.push(lambda t: RawTrade(t, t - 50, "update", "a", "1", "1", "buy"))        # received BEFORE the poll
    clock.t = 2_000
    mono.t = 100.0
    rt.step()
    times = [e.t_ms for e in driver.fed]
    assert times == sorted(times) and any(isinstance(e, OrderEvent) for e in driver.fed)
    assert driver.fed[0].t_ms == 1_000 and isinstance(driver.fed[0], RawTrade)


def test_a_step_that_raises_halts_new_setups_instead_of_continuing_blind(tmp_path):
    rig = Rig(tmp_path)
    rig.start()

    def boom():
        raise ValueError("driver exploded")

    class RunOnce:                                                 # stand-in for the stop Event: one loop pass
        def __init__(self):
            self.n = 0

        def is_set(self):
            self.n += 1
            return self.n > 1

        def wait(self, _timeout):
            return None
    rig.rt.step, rig.rt._stop = boom, RunOnce()
    rig.rt._loop()
    assert rig.engine.halted_reason.startswith("RUNTIME_ERROR:ValueError:driver exploded")
    assert rig.rt.last_error == "ValueError:driver exploded"


def test_a_failing_candle_source_never_kills_the_loop_and_is_reported(tmp_path, caplog):
    rig = Rig(tmp_path)
    rig.start()

    def broken(tf_s, limit):
        raise ConnectionError("exchange unreachable")
    rig.candles.bars = broken
    with caplog.at_level(logging.WARNING):
        rig.step()
    assert "LSRL_FEED_ERROR" in caplog.text and rig.rt.last_error.startswith("bars_")
    assert rig.engine.halted_reason is None                        # a data gap is a staleness matter, not a halt


# ------------------------------------------------------------------ git sha
def test_git_sha_is_recorded_with_a_dirty_marker_or_unknown_never_blank(tmp_path):
    def runner(rc_head, out_head, rc_dirty, out_dirty):
        def run(cmd, **kw):
            head = cmd[1] == "rev-parse"
            return SimpleNamespace(returncode=rc_head if head else rc_dirty, stdout=out_head if head else out_dirty)
        return run
    sha = "fa3f9b4c1d2e3f405162738495a6b7c8d9e0f1a2"
    assert resolve_git_sha(tmp_path, runner(0, sha + "\n", 0, "")) == sha
    assert resolve_git_sha(tmp_path, runner(0, sha + "\n", 0, " M app/config.py\n")) == sha + "+dirty"
    assert resolve_git_sha(tmp_path, runner(128, "", 0, "")) == "UNKNOWN"

    def explode(cmd, **kw):
        raise FileNotFoundError("git")
    assert resolve_git_sha(tmp_path, explode) == "UNKNOWN"


# ------------------------------------------------------------------ build_runtime wiring / refusals
def settings(**kw):
    base = dict(lsrl_mode="SHADOW", lsrl_symbol="SOLUSDT", lsrl_data_dir="", lsrl_shadow_nav_usdt=1000.0,
                lsrl_min_free_gb=0.0, bitget_product_type="USDT-FUTURES")
    base.update(kw)
    return SimpleNamespace(**base)


class MetaClient(FakeCandleClient):
    def __init__(self, sp):
        super().__init__()
        self.sp = sp

    def get_contract_spec(self, symbol):
        return self.sp


def test_build_runtime_consumes_exchange_metadata_and_wires_the_websocket_pipeline(tmp_path):
    for mode in ("SHADOW", "PAPER"):
        rt = build_runtime(settings(lsrl_mode=mode, lsrl_data_dir=str(tmp_path)), client=MetaClient(spec()),
                           execution_service=None, risk_manager=None, project_dir=tmp_path)
        assert rt.mode is Mode[mode] and isinstance(rt.driver, SimDriver)
        assert rt.engine.rules.tick_size == 0.001                 # from exchange metadata, not a hard-coded value
        assert rt.driver.recorder.root == tmp_path / mode.lower() / "recorded"
        assert rt.driver.recorder.header["git_sha"] == "UNKNOWN"   # tmp_path is not a git checkout: recorded as such
        tp = rt.driver.recorder.header["transport"]                # transport parameters are part of the recorded header
        assert tp["url"].startswith("wss://ws.bitget.com") and tp["ping_interval_s"] == 25.0
        assert [a["channel"] for a in tp["subscription"]] == ["books", "trade"]
        assert rt.driver.compounding is (mode == "PAPER")
        assert rt.ws.feed.symbol == "SOLUSDT" and rt.ws.feed.book_channel == "books"
        assert rt.driver.observer is not None and rt.engine.detector.tape_available is True


def test_build_runtime_refuses_without_contract_metadata(tmp_path):
    with pytest.raises(RuntimeError, match="CONTRACT_METADATA_UNAVAILABLE"):
        build_runtime(settings(lsrl_data_dir=str(tmp_path)), client=MetaClient(None), execution_service=None,
                      risk_manager=None)


def test_build_runtime_refuses_backtest_off_and_micro_live_without_execution_service(tmp_path):
    for mode, exc, match in (("BACKTEST", ValueError, "recorded inputs"), ("OFF", ValueError, "OFF"),
                             ("MICRO_LIVE", RuntimeError, "requires the execution service")):
        with pytest.raises(exc, match=match):
            build_runtime(settings(lsrl_mode=mode, lsrl_data_dir=str(tmp_path)), client=MetaClient(spec()),
                          execution_service=None, risk_manager=None)


def test_build_runtime_micro_live_is_unarmed_by_default_so_it_cannot_place_anything(tmp_path):
    from lsrl.adapters.live import LiveNotArmed
    st = settings(lsrl_mode="MICRO_LIVE", lsrl_data_dir=str(tmp_path), forward_paper_only=True,
                  execution_enabled=False, execution_mode="DRY_RUN", lsrl_microlive_armed=False)
    with pytest.raises(LiveNotArmed):
        build_runtime(st, client=MetaClient(spec()), execution_service=object(), risk_manager=None)


def test_a_wall_clock_that_stepped_backwards_across_a_restart_cannot_make_new_events_look_old(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    rig.run(timeline_events())
    known = rig.engine.now_ms
    rig.close()
    again = Rig(tmp_path, session=2, start_ms=known - 3_600_000)            # the clock is an hour BEHIND
    again.start()
    stamped = again.rt.bus.push(lambda t: t)
    assert stamped >= known


# ------------------------------------------------------------------ metadata straight from the exchange payload
REAL_CONTRACTS = json.loads((REPO / "tests" / "fixtures" / "bitget_contracts_solusdt_2026-09-20.json").read_text())


class RawRowClient(MetaClient):
    def __init__(self, sp, payload):
        super().__init__(sp)
        self.payload = payload

    def get_contracts(self, product_type, symbol=None):
        return self.payload


def test_the_real_exchange_payload_is_the_source_of_truth_for_tick_and_lot_rules():
    row = REAL_CONTRACTS["data"][0]
    assert (row["symbol"], row["pricePlace"], row["priceEndStep"], row["sizeMultiplier"], row["minTradeNum"],
            row["minTradeUSDT"]) == ("SOLUSDT", "3", "1", "0.1", "0.1", "5")
    rules = load_rules(RawRowClient(spec(), REAL_CONTRACTS), "SOLUSDT", "USDT-FUTURES")
    assert (rules.tick_size, rules.qty_step, rules.min_qty, rules.min_notional_usdt) == (0.001, 0.1, 0.1, 5.0)
    assert rules == RULES                                                     # the fixture rules ARE the exchange rules


def test_a_spec_that_disagrees_with_the_raw_contract_row_refuses_to_run():
    """ContractSpec has no priceEndStep: a symbol quoted in steps of 5 would silently get tick 0.001."""
    payload = json.loads(json.dumps(REAL_CONTRACTS))
    payload["data"][0]["priceEndStep"] = "5"
    with pytest.raises(RuntimeError, match="CONTRACT_METADATA_MISMATCH"):
        load_rules(RawRowClient(spec(), payload), "SOLUSDT", "USDT-FUTURES")


def test_a_missing_raw_row_refuses_to_run():
    with pytest.raises(RuntimeError, match="CONTRACT_METADATA_UNAVAILABLE"):
        load_rules(RawRowClient(spec(), {"data": []}), "SOLUSDT", "USDT-FUTURES")


def test_build_runtime_uses_the_cross_checked_exchange_rules(tmp_path):
    rt = build_runtime(settings(lsrl_data_dir=str(tmp_path)), client=RawRowClient(spec(), REAL_CONTRACTS),
                       execution_service=None, risk_manager=None, project_dir=tmp_path)
    assert rt.engine.rules == RULES


# ------------------------------------------------------------------ END TO END: raw WS frames -> ... -> (fake) exchange orders
# Built from the executor's own test rig (real EntryOrderSubmitter, real authorization store, real CentralRiskGate over
# the unmodified RiskManager, real LSRLLiveExecutor) talking to an IN-MEMORY exchange. Nothing here can reach Bitget.
from tests.test_lsrl_live_executor import Rig as LiveRig            # noqa: E402
from tests.test_lsrl_risk_integration import _isolate               # noqa: E402,F401  (autouse isolation fixture)


class LiveWire:
    def __init__(self, tmp_path: Path) -> None:
        from lsrl.adapters.live_driver import LiveDriver
        self.live = LiveRig(tmp_path)
        self.observer = BookObserver(FeedConfig())
        self.driver = LiveDriver(self.live.engine, self.live.exec, nav_fn=lambda: 1_000.0, observer=self.observer)
        self.bus_clock, self.mono = Clock(0), Clock(0.0)
        self.rt = LSRLRuntime(mode=Mode.MICRO_LIVE, engine=self.live.engine, driver=self.driver,
                              bus=EventBus(self.bus_clock), ws=FakeWs(), candles=FakeCandles(),
                              state_store=JsonStateStore(str(tmp_path / "rt_state.json")), clock=self.mono)

    def run(self, events, chunk: int = 40) -> None:
        for i, ev in enumerate(events, 1):
            self.bus_clock.t = max(self.bus_clock.t, ev.t_ms)
            self.live.ex.now_ms = self.bus_clock.t                           # the exchange clock follows the market clock
            self.rt.bus.push(lambda t, ev=ev: replace(ev, t_ms=t))
            if i % chunk == 0 or i == len(events):
                self.step()

    def step(self) -> None:
        self.mono.t += 100.0
        self.rt.step()


def test_raw_websocket_frames_end_up_as_five_authorised_post_only_limits_on_the_exchange(tmp_path):
    wire = LiveWire(tmp_path)
    wire.run(timeline_events())
    engine, ex = wire.live.engine, wire.live.ex
    assert engine.setup is not None and engine.setup.state == "LADDER_PENDING"
    entries = [c for c in ex.calls if c[0] == "place_entry"]
    assert len(entries) == 5 and all(c[2] == "LONG" and c[5] is True for c in entries)          # limit, post-only, LONG
    assert sorted(c[4] for c in entries) == sorted(engine.setup.geometry["rungs"])
    assert not [c for c in ex.calls if "market" in str(c[0]).lower()]                          # never a market order
    assert wire.observer.state == OK and wire.rt.last_error is None
    assert wire.live.auth is not None and engine.setup.risk_gate["source"] == "RiskManager"     # the central verdict was used


def test_a_fill_seen_by_the_executor_poll_is_queued_on_the_same_bus_and_protects_the_position(tmp_path):
    wire = LiveWire(tmp_path)
    wire.run(timeline_events())
    ex, engine = wire.live.ex, wire.live.engine
    e1 = max((o for o in ex.orders.values() if o["tradeSide"] == "open"), key=lambda o: o["price"])
    ex.fill(e1["clientOid"])                                                 # the exchange fills E1 (the highest bid)
    wire.bus_clock.t += 5_000
    ex.now_ms = wire.bus_clock.t
    wire.step()                                                              # poll_events -> bus -> engine -> executor
    wire.step()
    assert engine.setup.ledger.entry_qty > 0 and engine.setup.state == "IN_POSITION"
    assert ex.stop_plan is not None and ex.stop_plan["price"] == engine.setup.stop_price   # exchange-side protection set
    assert ex.stop_plan["size"] == pytest.approx(engine.setup.ledger.entry_qty)
