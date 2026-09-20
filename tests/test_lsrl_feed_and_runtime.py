"""LSRL_V1: live market feed, contract-metadata conversion, and the runtime loop (SHADOW / PAPER)."""
from __future__ import annotations

import logging
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from execution.state_store import JsonStateStore
from lsrl.adapters.sim import SimExchange
from lsrl.core.config import DEFAULT_CONFIG as CFG
from lsrl.core.engine import LSRLEngine
from lsrl.core.gates import NullRiskGate
from lsrl.core.types import BarClosed, DepthSnapshot
from lsrl.driver import SimDriver
from lsrl.feed import LiveFeed
from lsrl.io.gate_log import RecordingRiskGate
from lsrl.io.recorder import LSRLRecorder, input_files, verify_chain
from lsrl.io.replay import replay
from lsrl.modes import Mode
from lsrl.runtime import LSRLRuntime, build_runtime, rules_from_contract_spec
from tests.lsrl_fixtures import RULES, S, bar, long_sweep, shift, warmup

MIN = 60_000
T0 = 1_800_000_000_000


# ------------------------------------------------------------------ fake REST client
class FakeMarketClient:
    def __init__(self) -> None:
        self.book = {"bids": [{"price": 149.95, "size": 10.0}, {"price": 149.90, "size": 11.0}],
                     "asks": [{"price": 150.05, "size": 9.0}, {"price": 150.10, "size": 8.0}],
                     "raw_payload": {"ts": "1800000000123"}}
        self.rows: dict[str, list] = {"1m": [], "15m": []}
        self.calls: list[tuple] = []

    def get_orderbook(self, symbol, product_type=None, limit=50):
        self.calls.append(("book", symbol, limit))
        return self.book

    def get_candles(self, symbol, product_type, granularity="15m", limit=200):
        self.calls.append(("candles", symbol, granularity, limit))
        return {"data": list(self.rows[granularity])}


def row(open_ts: int, o=150.0, h=150.2, l=149.8, c=150.0, v=10.0):
    return [str(open_ts), str(o), str(h), str(l), str(c), str(v), "1500"]      # Bitget returns strings


class Clock:
    def __init__(self, t: int) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


def feed_with(client, now: int, **kw):
    clock = Clock(now)
    return LiveFeed(client, "solusdt", "USDT-FUTURES", now_ms=clock, **kw), clock


# ------------------------------------------------------------------ depth
def test_depth_is_normalised_with_exchange_and_local_receipt_time():
    client = FakeMarketClient()
    feed, _ = feed_with(client, T0 + 500)
    snap = feed.depth()
    assert snap.bids == ((149.95, 10.0), (149.90, 11.0)) and snap.asks == ((150.05, 9.0), (150.10, 8.0))
    assert snap.t_ms == T0 + 500 and snap.exchange_ts_ms == 1_800_000_000_123
    assert client.calls[0] == ("book", "SOLUSDT", 50)


@pytest.mark.parametrize("ts", [None, "", "not-a-number", []])
def test_missing_or_garbled_exchange_timestamp_is_recorded_as_unknown_not_invented(ts):
    client = FakeMarketClient()
    client.book["raw_payload"] = {"ts": ts} if ts is not None else {}
    feed, _ = feed_with(client, T0)
    assert feed.depth().exchange_ts_ms is None


def test_the_receipt_clock_never_steps_backwards():
    feed, clock = feed_with(FakeMarketClient(), T0 + 10_000)
    first = feed.depth().t_ms
    clock.t = T0                                                   # NTP slew: the wall clock jumped back
    assert feed.depth().t_ms == first and feed.now() == first


# ------------------------------------------------------------------ candles
def test_a_still_forming_candle_is_never_emitted():
    client = FakeMarketClient()
    client.rows["1m"] = [row(T0), row(T0 + MIN)]
    feed, clock = feed_with(client, T0 + MIN + 30_000)            # T0 closed; T0+MIN is 30s old
    got = feed.bars(60, 5)
    assert [b.open_ts_ms for b in got] == [T0]


def test_a_candle_is_only_emitted_after_the_publication_lag():
    client = FakeMarketClient()
    client.rows["1m"] = [row(T0)]
    feed, clock = feed_with(client, T0 + MIN + 1_999)             # closed 1.999s ago: exchange may still amend it
    assert feed.bars(60, 5) == []
    clock.t = T0 + MIN + 2_000
    assert [b.open_ts_ms for b in feed.bars(60, 5)] == [T0]


def test_bars_are_delivered_once_in_ascending_order_whatever_the_api_order():
    client = FakeMarketClient()
    client.rows["1m"] = [row(T0 + 2 * MIN), row(T0), row(T0 + MIN)]        # newest-first / shuffled
    feed, clock = feed_with(client, T0 + 10 * MIN)
    first = feed.bars(60, 5)
    assert [b.open_ts_ms for b in first] == [T0, T0 + MIN, T0 + 2 * MIN]
    assert feed.bars(60, 5) == []                                          # dedupe
    client.rows["1m"].append(row(T0 + 3 * MIN))
    assert [b.open_ts_ms for b in feed.bars(60, 5)] == [T0 + 3 * MIN]


def test_bar_fields_receipt_time_and_timeframe_are_correct():
    client = FakeMarketClient()
    client.rows["15m"] = [row(T0, o=1, h=4, l=0.5, c=3, v=99)]
    feed, _ = feed_with(client, T0 + 30 * MIN)
    (b,) = feed.bars(900, 3)
    assert (b.tf_s, b.open_ts_ms, b.close_ts_ms) == (900, T0, T0 + 900_000)
    assert (b.open, b.high, b.low, b.close, b.volume) == (1, 4, 0.5, 3, 99)
    assert b.t_ms == T0 + 30 * MIN                                         # local receipt, not the bar's own time
    assert ("candles", "SOLUSDT", "15m", 3) in client.calls


def test_malformed_candle_rows_are_dropped():
    client = FakeMarketClient()
    client.rows["1m"] = [["short"], "junk", None, row(T0)]
    feed, _ = feed_with(client, T0 + 10 * MIN)
    assert [b.open_ts_ms for b in feed.bars(60, 5)] == [T0]


def test_timeframes_are_tracked_independently():
    client = FakeMarketClient()
    client.rows["1m"], client.rows["15m"] = [row(T0)], [row(T0)]
    feed, _ = feed_with(client, T0 + 60 * MIN)
    assert len(feed.bars(60, 5)) == 1 and len(feed.bars(900, 5)) == 1
    assert feed.bars(60, 5) == [] and feed.bars(900, 5) == []


# ------------------------------------------------------------------ contract metadata -> SymbolRules
def spec(**kw):
    base = dict(symbol="SOLUSDT", product_type="USDT-FUTURES", min_trade_num=Decimal("0.1"),
                size_multiplier=Decimal("0.1"), volume_place=1, price_place=2, min_trade_usdt=Decimal("5"),
                source="live", retrieved_at=0.0, fallback_used=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_contract_spec_becomes_symbol_rules():
    r = rules_from_contract_spec(spec(), "solusdt")
    assert (r.symbol, r.tick_size, r.qty_step, r.min_qty, r.min_notional_usdt) == ("SOLUSDT", 0.01, 0.1, 0.1, 5.0)


def test_unknown_min_notional_falls_back_to_the_exchange_wide_floor_not_zero():
    assert rules_from_contract_spec(spec(min_trade_usdt=None), "SOLUSDT").min_notional_usdt == 5.0


def test_unknown_contract_metadata_refuses_to_run():
    with pytest.raises(RuntimeError, match="CONTRACT_METADATA_UNAVAILABLE"):
        rules_from_contract_spec(None, "SOLUSDT")


# ------------------------------------------------------------------ scripted feed: one event per step
def session(**kw):
    """(history, live events): history ended 15 min before the sweep bar; the bridge bars and the sweep
    itself arrive while the runtime is running, exactly like real polling."""
    history = shift(warmup(nav=None), -15 * MIN)
    bridge = [bar(t, 150.0, 150.2, 149.8, 150.0) for t in range(S - 15 * MIN, S, MIN)]
    live = long_sweep(warm=False, **kw) + bridge
    live.sort(key=lambda e: e.t_ms)
    return history, live


class ScriptedFeed:
    """Serves a prepared scenario one event per runtime step (depth via depth(), bars via bars())."""

    def __init__(self, history, live) -> None:
        self.script = [e for e in live if isinstance(e, (DepthSnapshot, BarClosed))]
        self.first_t = min((e.t_ms for e in self.script), default=0)
        # history is stamped with the moment the runtime started, like LiveFeed stamps its warm-up fetch
        self.history = [replace(b, t_ms=self.first_t - 1) for b in history if isinstance(b, BarClosed)]
        self.pending = None
        self.t = 0

    def now(self) -> int:
        return self.t

    def depth(self):
        out, self.pending = (self.pending, None) if isinstance(self.pending, DepthSnapshot) else (None, self.pending)
        return out

    def bars(self, tf_s, limit):
        if limit >= 60:                                            # the warm-up request
            return [b for b in self.history if b.tf_s == tf_s]
        if isinstance(self.pending, BarClosed) and self.pending.tf_s == tf_s:
            out, self.pending = [self.pending], None
            return out
        return []


def make_runtime(tmp_path, feed, mode=Mode.SHADOW, *, recorded=True, nav=1_000.0):
    holder: dict = {}
    recorder = LSRLRecorder(tmp_path / "rec", CFG, RULES, mode=mode.value, tape_available=False) if recorded else None
    engine = LSRLEngine(CFG, RULES, risk_gate=RecordingRiskGate(NullRiskGate(), lambda *a: holder["d"].gate_sink(*a)))
    holder["d"] = SimDriver(mode, engine, SimExchange(RULES, CFG), start_nav_usdt=nav, recorder=recorder)
    clock = Clock(0)
    runtime = LSRLRuntime(mode=mode, engine=engine, driver=holder["d"], feed=feed,
                          state_store=JsonStateStore(str(tmp_path / "state.json")), clock=clock)
    return runtime, holder["d"], clock


def drive(runtime, feed, clock):
    for ev in list(feed.script):
        feed.pending, feed.t = ev, ev.t_ms
        clock.t += 100.0                                           # every cadence interval is due
        runtime.step()


def scripted(**kw):
    return ScriptedFeed(*session(**kw))


def test_the_runtime_produces_the_ladder_from_a_scripted_session(tmp_path):
    feed = scripted(tail_snapshots=2)
    runtime, d, clock = make_runtime(tmp_path, feed)
    drive(runtime, feed, clock)
    assert d.engine.setup is not None and d.engine.setup.state == "LADDER_PENDING"
    assert sorted(r.status for r in d.engine.setup.rungs.values()) == ["OPEN"] * 5
    assert runtime.last_error is None


def test_warmup_feeds_history_without_asking_for_nav_and_without_decisions(tmp_path):
    feed = scripted(tail_snapshots=2)
    runtime, d, clock = make_runtime(tmp_path, feed)
    runtime.step()                                                 # first step = warm-up (+ nothing pending)
    assert runtime._warmed and d.engine.now_ms > 0
    assert d.records == [] and d.engine.setup is None
    assert d.engine.atr15.value is not None and d.engine.atr1m.value is not None
    assert d.engine.nav_usdt is None                               # history never triggered an equity/NAV fetch


def test_a_failing_data_source_never_kills_the_loop_and_is_reported(tmp_path, caplog):
    feed = scripted(tail_snapshots=2)
    runtime, d, clock = make_runtime(tmp_path, feed)
    clock.t += 100

    def broken():
        raise ConnectionError("exchange unreachable")
    feed.depth = broken
    with caplog.at_level(logging.WARNING):
        runtime.step()
    assert "LSRL_FEED_ERROR" in caplog.text and runtime.last_error == "depth:ConnectionError:exchange unreachable"
    assert d.engine.halted_reason is None                          # a data gap is a staleness matter, not a halt


def test_state_is_persisted_each_step_and_restored_by_a_fresh_runtime(tmp_path):
    feed = scripted(tail_snapshots=2)
    runtime, d, clock = make_runtime(tmp_path, feed, recorded=False)
    drive(runtime, feed, clock)
    before = d.engine.snapshot()
    feed2 = scripted(tail_snapshots=2)
    runtime2, d2, _ = make_runtime(tmp_path, feed2, recorded=False)
    runtime2.restore()
    assert d2.engine.snapshot() == before
    assert d2.engine.setup is not None and d2.engine.setup.setup_id == d.engine.setup.setup_id


def test_state_written_by_another_mode_is_refused(tmp_path):
    feed = scripted(tail_snapshots=2)
    runtime, d, clock = make_runtime(tmp_path, feed, Mode.SHADOW, recorded=False)
    drive(runtime, feed, clock)
    other, *_ = make_runtime(tmp_path, scripted(tail_snapshots=2), Mode.PAPER, recorded=False)
    with pytest.raises(RuntimeError, match="mode SHADOW, not PAPER"):
        other.restore()


def test_a_step_that_raises_halts_new_setups_instead_of_continuing_blind(tmp_path):
    runtime, d, _clock = make_runtime(tmp_path, scripted(tail_snapshots=2), recorded=False)

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
    runtime.step, runtime._stop = boom, RunOnce()
    runtime._loop()
    assert d.engine.halted_reason.startswith("RUNTIME_ERROR:ValueError:driver exploded")
    assert runtime.last_error == "ValueError:driver exploded"


# ------------------------------------------------------------------ what the runtime records replays exactly
def test_a_runtime_session_is_recorded_and_replays_to_the_same_decisions(tmp_path):
    feed = scripted(tail_snapshots=2)
    runtime, d, clock = make_runtime(tmp_path, feed)
    drive(runtime, feed, clock)
    files = input_files(tmp_path / "rec")
    ok, why = verify_chain(files)
    assert ok, why
    result = replay(files)
    live = [(r.kind, r.data) for r in d.records]
    assert [(r.kind, r.data) for r in result.records] == live
    assert result.engine.snapshot() == d.engine.snapshot()


def test_shadow_never_compounds_but_paper_does(tmp_path):
    for mode, compounds in ((Mode.SHADOW, False), (Mode.PAPER, True)):
        feed = scripted(tail_snapshots=2)
        runtime, d, clock = make_runtime(tmp_path / mode.value, feed, mode, recorded=False)
        assert d.compounding is compounds and d.nav == 1_000.0


# ------------------------------------------------------------------ build_runtime wiring / refusals
def settings(**kw):
    base = dict(lsrl_mode="SHADOW", lsrl_symbol="SOLUSDT", lsrl_data_dir="", lsrl_shadow_nav_usdt=1000.0,
                lsrl_min_free_gb=0.0, lsrl_depth_interval_s=10.0, bitget_product_type="USDT-FUTURES")
    base.update(kw)
    return SimpleNamespace(**base)


class MetaClient(FakeMarketClient):
    def __init__(self, sp):
        super().__init__()
        self.sp = sp

    def get_contract_spec(self, symbol):
        return self.sp


def test_build_runtime_shadow_and_paper_construct_a_sim_runtime(tmp_path):
    for mode in ("SHADOW", "PAPER"):
        rt = build_runtime(settings(lsrl_mode=mode, lsrl_data_dir=str(tmp_path)), client=MetaClient(spec()),
                           execution_service=None, risk_manager=None)
        assert rt.mode is Mode[mode] and isinstance(rt.driver, SimDriver)
        assert rt.driver.recorder.root == tmp_path / mode.lower() / "recorded"     # created on the first write
        assert rt.driver.compounding is (mode == "PAPER")


def test_build_runtime_refuses_without_contract_metadata(tmp_path):
    with pytest.raises(RuntimeError, match="CONTRACT_METADATA_UNAVAILABLE"):
        build_runtime(settings(lsrl_data_dir=str(tmp_path)), client=MetaClient(None), execution_service=None,
                      risk_manager=None)


def test_build_runtime_refuses_backtest_and_off_and_micro_live_without_execution_service(tmp_path):
    with pytest.raises(ValueError, match="recorded inputs"):
        build_runtime(settings(lsrl_mode="BACKTEST", lsrl_data_dir=str(tmp_path)), client=MetaClient(spec()),
                      execution_service=None, risk_manager=None)
    with pytest.raises(ValueError):
        build_runtime(settings(lsrl_mode="OFF", lsrl_data_dir=str(tmp_path)), client=MetaClient(spec()),
                      execution_service=None, risk_manager=None)
    with pytest.raises(RuntimeError, match="requires the execution service"):
        build_runtime(settings(lsrl_mode="MICRO_LIVE", lsrl_data_dir=str(tmp_path)), client=MetaClient(spec()),
                      execution_service=None, risk_manager=None)


def test_build_runtime_micro_live_is_unarmed_by_default_so_it_cannot_place_anything(tmp_path):
    from lsrl.adapters.live import LiveNotArmed
    st = settings(lsrl_mode="MICRO_LIVE", lsrl_data_dir=str(tmp_path), forward_paper_only=True,
                  execution_enabled=False, execution_mode="DRY_RUN", lsrl_microlive_armed=False)
    with pytest.raises(LiveNotArmed):
        build_runtime(st, client=MetaClient(spec()), execution_service=object(), risk_manager=None)
