"""LSRL_V1 BACKTEST mode: the identical engine over RECORDED market inputs (no invented liquidity)."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from lsrl import backtest as bt
from lsrl.backtest import MARKET_EVENTS, backtest
from lsrl.core.types import FillEvent, NavEvent, OrderEvent
from lsrl.io.recorder import (LSRLRecorder, input_files, read_events, read_gate_decisions, read_header,
                              verify_chain)
from lsrl.io.replay import replay
from lsrl.modes import Mode
from tests.lsrl_fixtures import RULES, long_sweep
from tests.test_lsrl_modes_and_replay import CFG, FILL_ALL, Q, TP1, TP2, TP3, driver

REPO = Path(__file__).resolve().parents[1]


def record_paper_session(tmp_path, bars=(Q, FILL_ALL, TP1, TP2, TP3), nav=1_000.0):
    rec = LSRLRecorder(tmp_path / "rec", CFG, RULES, mode="PAPER")
    d = driver(Mode.PAPER, recorder=rec, nav=nav)
    d.run(long_sweep(extra_bars=tuple(bars), tail_snapshots=2))
    return d, input_files(tmp_path / "rec")


def kinds(records):
    return [(r.kind, r.data) for r in records]


def test_the_module_states_that_no_historical_liquidity_replay_exists():
    assert bt.HISTORICAL_LIQUIDITY_REPLAY_AVAILABLE is False
    assert "HISTORICAL_LIQUIDITY_REPLAY_AVAILABLE=NO" in (bt.__doc__ or "")


def test_backtest_over_a_recorded_session_reaches_the_same_decisions_as_the_paper_run(tmp_path):
    paper, files = record_paper_session(tmp_path)
    assert any(r.kind == "setup_closed" for r in paper.records)          # the session actually traded to a close
    result = backtest(files)
    assert result.mode is Mode.BACKTEST
    assert kinds(result.records) == kinds(paper.records)                 # one alpha implementation, two adapters
    assert result.engine.snapshot() == paper.engine.snapshot()


def test_adapter_produced_events_in_the_log_are_not_replayed_the_sim_regenerates_them(tmp_path):
    _paper, files = record_paper_session(tmp_path)
    types = {type(e).__name__ for e in read_events(files)}
    assert {"NavEvent", "OrderEvent", "FillEvent"} <= types              # they ARE in the recorded log ...
    assert not any(issubclass(t, (NavEvent, OrderEvent, FillEvent)) for t in MARKET_EVENTS)   # ... never fed back


def test_nav_comes_from_the_backtest_account_not_from_the_recorded_run(tmp_path):
    paper, files = record_paper_session(tmp_path, bars=(Q,))
    small = backtest(files, start_nav_usdt=250.0)
    big = backtest(files, start_nav_usdt=1_000.0)
    qty = lambda d: sum(r.qty for r in d.engine.setup.rungs.values())     # noqa: E731
    assert qty(small) < qty(big)
    assert qty(big) == pytest.approx(qty(paper))                          # 1000 == the recorded run's NAV


def test_a_recorded_fill_is_never_replayed_into_a_backtest(tmp_path):
    """Forge a log in which the recorded run's adapter claims a rung filled that the market never reached."""
    paper, files = record_paper_session(tmp_path / "a", bars=(Q,))
    setup = paper.engine.setup
    assert setup is not None and setup.ledger.entry_qty == 0.0
    forged = LSRLRecorder(tmp_path / "forged", CFG, RULES, mode="MICRO_LIVE")
    events = list(read_events(files))
    for setup_id, decision in read_gate_decisions(files).items():
        forged.record_gate(events[0].t_ms, setup_id, decision)
    for ev in events:
        forged.record_event(ev)
    rung = setup.rungs[1]
    forged.record_event(FillEvent(t_ms=events[-1].t_ms + 1_000, setup_id=setup.setup_id, tag=rung.tag, kind="ENTRY",
                                  price=rung.price, qty=rung.qty, fee_usdt=0.0, rung=1))
    forged_files = input_files(tmp_path / "forged")
    assert any(isinstance(e, FillEvent) for e in read_events(forged_files))            # the forged fill is in the log
    assert replay(forged_files).engine.setup.ledger.entry_qty == pytest.approx(rung.qty)   # REPLAY honours it ...
    result = backtest(forged_files)                                                    # ... BACKTEST regenerates
    assert result.engine.setup.ledger.entry_qty == 0.0 and result.sim.position_qty == 0.0


def test_backtest_can_record_its_own_verifiable_chain_labelled_backtest(tmp_path):
    _paper, files = record_paper_session(tmp_path)
    own = LSRLRecorder(tmp_path / "bt", CFG, RULES, mode="BACKTEST")
    backtest(files, recorder=own)
    out_files = input_files(tmp_path / "bt")
    ok, why = verify_chain(out_files)
    assert ok, why
    assert read_header(out_files[0])["mode"] == "BACKTEST"


def test_a_tampered_log_is_refused(tmp_path):
    _paper, files = record_paper_session(tmp_path)
    lines = files[0].read_text().splitlines()
    doc = json.loads(lines[5])
    doc["body"]["event"]["t_ms"] += 1
    lines[5] = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    files[0].write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="integrity"):
        backtest(files)


def test_a_log_recorded_under_a_different_frozen_config_is_refused(tmp_path):
    from dataclasses import replace
    _paper, files = record_paper_session(tmp_path)
    with pytest.raises(ValueError, match="config digest"):
        backtest(files, cfg=replace(CFG, risk_per_full_trade_frac=0.0025))


def test_backtest_never_builds_liquidity_from_candles_or_swings():
    """Structural: the module reads recorded depth only; no candle/swing/indicator construction of pools."""
    tree = ast.parse((REPO / "lsrl" / "backtest.py").read_text())
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not any(m.startswith(("lsrl.core.pools", "lsrl.core.sweep", "pandas", "numpy", "ta")) for m in imported)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree)
                                                                       if isinstance(n, ast.Attribute)}
    assert not names & {"swing_high", "swing_low", "find_peaks", "rolling"}


# ------------------------------------------------------------------ BACKTEST over RAW recordings (the production format)
def raw_session(tmp_path, direction="LONG", name="A_true_consumption"):
    from lsrl.io.gate_log import RecordingRiskGate
    from lsrl.core.gates import NullRiskGate
    from lsrl.core.engine import LSRLEngine
    from lsrl.driver import SimDriver
    from lsrl.adapters.sim import SimExchange
    from tests.test_lsrl_causal_timelines import observed
    holder = {}
    rec = LSRLRecorder(tmp_path / "rec", CFG, RULES, mode="PAPER", min_free_gb=0.001)
    engine = LSRLEngine(CFG, RULES, risk_gate=RecordingRiskGate(NullRiskGate(), lambda *a: holder["d"].gate_sink(*a)))
    holder["d"] = SimDriver(Mode.PAPER, engine, SimExchange(RULES, CFG), start_nav_usdt=1_000.0, recorder=rec)
    holder["d"].run(observed(name, direction)[0])
    rec.close()
    return holder["d"], input_files(tmp_path / "rec")


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_backtest_over_a_raw_recording_takes_the_same_decisions_as_the_run_that_recorded_it(tmp_path, direction):
    paper, files = raw_session(tmp_path, direction)
    assert paper.engine.setup is not None
    result = backtest(files)
    assert kinds(result.records) == kinds(paper.records)
    assert result.engine.snapshot() == paper.engine.snapshot()
    assert result.observer.state == paper.observer.state and result.observer.epoch == 1


def test_backtest_replays_only_market_frames_and_regenerates_the_account(tmp_path):
    _paper, files = raw_session(tmp_path)
    logged = {type(e).__name__ for e in read_events(files)}
    assert {"OrderEvent", "NavEvent"} <= logged                            # the recording HAS adapter output ...
    small = backtest(files, start_nav_usdt=250.0)
    big = backtest(files, start_nav_usdt=1_000.0)
    assert sum(r.qty for r in small.engine.setup.rungs.values()) < sum(r.qty for r in big.engine.setup.rungs.values())


def test_backtest_keeps_its_own_engine_state_across_a_recorded_restart_marker(tmp_path):
    """A SESSION_START in the log resets the market view (uncertain interval) but is NOT allowed to overwrite the
    backtest's account with the recorded run's restored state."""
    from tests.lsrl_fixtures import S
    from tests.lsrl_raw_feed import Truth, to_raw
    from tests.test_lsrl_feed_and_runtime import Rig, timeline_events
    first = Rig(tmp_path)
    first.start()
    first.run(timeline_events())
    first.close()
    quiet = Truth()
    quiet.book(S + 90_000, 149.95, 150.05)
    second = Rig(tmp_path, session=2, start_ms=S + 70_000)
    second.start()
    second.run(to_raw(quiet, keepalive_until_ms=S + 100_000, bars=[]))
    second.close()
    files = input_files(tmp_path / "rec")
    result = backtest(files, start_nav_usdt=500.0)
    boots = [r.data for r in result.records if r.kind == "feed_health" and r.data["reason"] == "BOOTSTRAP"]
    assert len(boots) == 2 and result.observer.state == "OK"              # the market view WAS rebuilt at the boundary
    assert result.engine.setup is None and result.nav != 1_000.0            # own account (500 start), own decisions
    closed = [r.data for r in result.records if r.kind == "setup_closed"]
    assert closed and closed[0]["close_reason"] == "LIQUIDITY_DATA_STALE"
