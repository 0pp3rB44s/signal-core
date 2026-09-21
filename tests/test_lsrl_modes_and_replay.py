"""LSRL_V1: simulated exchange, mode parity, structural purity, record / replay."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from lsrl.adapters.sim import SimExchange
from lsrl.core.config import DEFAULT_CONFIG as CFG
from lsrl.core.engine import LSRLEngine
from lsrl.core.gates import NullRiskGate
from lsrl.core.types import DepthSnapshot, PlaceEntry, event_to_dict, intent_to_dict
from lsrl.driver import SimDriver
from lsrl.io.gate_log import RecordingRiskGate
from lsrl.io.recorder import (LSRLRecorder, RecorderError, decision_files, input_files, read_header,
                              verify_chain)
from lsrl.io.replay import replay
from lsrl.modes import Mode
from tests.lsrl_fixtures import RULES, S, long_sweep, make_book

REPO = Path(__file__).resolve().parents[1]

Q = (149.7, 149.9, 149.65, 149.8)                       # quiet bar: touches nothing
FILL_ALL = (149.6, 149.65, 149.15, 149.5)               # through E5 (149.20) but above the stop (149.13)
TP1 = (149.8, 150.35, 149.8, 150.0)                     # high >= 150.30 + 1 tick
TP2 = (150.0, 150.75, 149.95, 150.5)
TP3 = (150.5, 151.15, 150.4, 151.0)                    # stays above the stop that TP2 moved to TP1 (150.30)


def driver(mode: Mode = Mode.PAPER, *, recorder=None, nav: float = 1_000.0, gate=None) -> SimDriver:
    holder: dict = {}
    g = gate or NullRiskGate()
    if recorder is not None:
        g = RecordingRiskGate(g, lambda *a: holder["d"].gate_sink(*a))
    engine = LSRLEngine(CFG, RULES, risk_gate=g)
    holder["d"] = SimDriver(mode, engine, SimExchange(RULES, CFG), start_nav_usdt=nav, recorder=recorder)
    return holder["d"]


def run(mode=Mode.PAPER, bars=(), **kw) -> SimDriver:
    d = driver(mode, **kw)
    return d.run(long_sweep(extra_bars=tuple(bars), tail_snapshots=2))


# ------------------------------------------------------------------ conservative fill model
def test_a_bar_that_only_touches_a_limit_price_does_not_fill_it():
    touch = (149.7, 149.8, 149.20, 149.5)                # low == E5 exactly: touched, not traded through
    d = run(bars=[Q, touch, Q])
    assert d.engine.setup is not None and d.engine.setup.ledger.entry_qty == pytest.approx(
        sum(r.qty for r in d.engine.setup.rungs.values() if r.price > 149.20 + 0.0095))


def test_no_fill_in_the_bar_where_the_decision_was_taken_or_the_partial_bar_after_it():
    d = run(bars=[(149.7, 149.9, 149.0, 149.7)])          # bar starts BEFORE the order existed
    assert d.engine.setup.ledger.entry_qty == 0.0 and d.sim.position_qty == 0.0


def test_ladder_fills_only_in_a_later_bar_and_each_rung_needs_its_own_price():
    d = run(bars=[Q, (149.6, 149.65, 149.385, 149.5)])   # through E1, E2, E3 (needs <= 149.39) but not E4
    filled = {i for i, r in d.engine.setup.rungs.items() if r.status == "FILLED"}
    assert filled == {1, 2, 3} and d.engine.setup.state == "IN_POSITION"
    assert d.sim.position_qty == pytest.approx(3 * d.engine.setup.rungs[1].qty)


def test_stop_beats_targets_when_one_bar_reaches_both():
    wild = (149.6, 150.5, 149.10, 150.0)                 # fills the ladder AND trades through stop and TP1
    d = run(bars=[Q, wild, Q])
    assert d.engine.setup is None
    assert d.records[-1].data["close_reason"] == "STOP_LOSS"
    assert not any("TP1" in str(r.data.get("event")) for r in d.records)


def test_stop_fills_with_adverse_slippage_and_taker_fee_while_entries_pay_maker():
    d = run(bars=[Q, (149.6, 149.65, 149.10, 149.5), Q])
    closed = d.records[-1].data
    assert closed["actual"]["stop_slippage_usdt"] > 0            # filled worse than the stop price
    entry_fee = closed["actual"]["entry_fees_usdt"]
    assert entry_fee == pytest.approx(sum(r["qty"] * r["price"] for r in closed["rungs"].values()) * 0.0002)


def test_full_paper_round_trip_through_tp1_tp2_tp3():
    d = run(bars=[Q, FILL_ALL, Q, TP1, TP2, TP3])
    assert d.engine.setup is None
    closed = d.records[-1].data
    assert closed["close_reason"] == "TP3_FLAT" and closed["realized_net_usdt"] > 0 and closed["realized_r"] > 0
    kinds = [e["kind"] for e in closed["lifecycle_events"]]
    assert kinds.index("STOP_MOVED_TO_COST_ADJUSTED_BE") < kinds.index("STOP_MOVED_TO_TP1") < kinds.index("SETUP_CLOSED")
    assert d.nav > 1_000.0                                         # PAPER compounds realized PnL


def test_take_profit_is_not_eligible_in_the_bar_after_the_position_was_sized():
    # TP orders are placed when the entry fills; the very next bar cannot fill them.
    d = run(bars=[Q, FILL_ALL, TP1])
    assert d.engine.setup is not None and d.engine.setup.state == "IN_POSITION"
    assert not any("TP1_FILL" == r.data.get("event") for r in d.records)


# ------------------------------------------------------------------ mode parity
def test_backtest_shadow_and_paper_take_identical_decisions_on_identical_inputs():
    bars = [Q, FILL_ALL, Q, TP1, TP2, TP3]
    outs = {}
    for mode in (Mode.BACKTEST, Mode.SHADOW, Mode.PAPER):
        d = run(mode, bars=bars)
        outs[mode] = ([r.to_dict() for r in d.records], [intent_to_dict(i) for i in d.intents])
    assert outs[Mode.BACKTEST] == outs[Mode.SHADOW] == outs[Mode.PAPER]
    assert len(outs[Mode.PAPER][1]) > 10                            # a real lifecycle, not an empty stream


def test_shadow_uses_a_fixed_reference_nav_and_never_compounds():
    bars = [Q, FILL_ALL, Q, TP1, TP2, TP3]
    assert run(Mode.SHADOW, bars=bars).nav == 1_000.0
    assert run(Mode.PAPER, bars=bars).nav > 1_000.0
    assert run(Mode.BACKTEST, bars=bars).nav > 1_000.0


def test_micro_live_cannot_be_driven_by_the_simulator():
    with pytest.raises(ValueError):
        SimDriver(Mode.MICRO_LIVE, LSRLEngine(CFG, RULES, risk_gate=NullRiskGate()), SimExchange(RULES, CFG),
                  start_nav_usdt=100.0)


# ------------------------------------------------------------------ structural guarantees
_PURE_DIRS = [REPO / "lsrl" / "core", REPO / "lsrl" / "io"]
_PURE_FILES = [REPO / "lsrl" / "adapters" / "sim.py", REPO / "lsrl" / "driver.py", REPO / "lsrl" / "modes.py"]
_FORBIDDEN_ROOTS = {"clients", "execution", "risk", "app", "planning", "telemetry", "market_data",
                    "strategies", "microflow", "forward_paper", "requests", "websocket", "urllib", "socket",
                    "http", "subprocess"}


def _py(paths):
    for p in paths:
        yield from (sorted(p.glob("*.py")) if p.is_dir() else [p])


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_strategy_core_and_sim_adapters_import_nothing_that_can_reach_an_exchange():
    offenders = {str(p.relative_to(REPO)): sorted(_imports(p) & _FORBIDDEN_ROOTS)
                 for p in _py(_PURE_DIRS + _PURE_FILES) if _imports(p) & _FORBIDDEN_ROOTS}
    assert offenders == {}


def test_the_engine_has_no_notion_of_a_run_mode():
    for path in _py([REPO / "lsrl" / "core"]):
        text = path.read_text()
        assert "lsrl.modes" not in text and "import modes" not in text, path
        for word in ("MICRO_LIVE", "SHADOW", "PAPER", "BACKTEST"):
            assert word not in text, f"{path.name} mentions run mode {word}"


def test_the_core_uses_no_wall_clock_or_randomness():
    for path in _py([REPO / "lsrl" / "core"]):
        assert not ({"time", "random", "datetime", "secrets"} & _imports(path)), path


# ------------------------------------------------------------------ record / replay
def _recorded_run(tmp_path, bars):
    rec = LSRLRecorder(tmp_path / "rec", CFG, RULES, mode="PAPER", min_free_gb=0.001)
    d = run(bars=bars, recorder=rec)
    rec.close()
    return d, tmp_path / "rec"


def test_replaying_the_recorded_inputs_reproduces_every_decision_exactly(tmp_path):
    d, root = _recorded_run(tmp_path, [Q, FILL_ALL, Q, TP1, TP2, TP3])
    result = replay(input_files(root))
    assert [r.to_dict() for r in result.records] == [r.to_dict() for r in d.records]
    assert [intent_to_dict(i) for i in result.intents] == [intent_to_dict(i) for i in d.intents]
    assert result.engine.snapshot() == d.engine.snapshot()
    assert d.records[-1].data["close_reason"] == "TP3_FLAT"


def test_every_setup_is_reconstructable_from_the_recorded_inputs_alone(tmp_path):
    d, root = _recorded_run(tmp_path, [Q])
    replayed = replay(input_files(root))
    original = next(r for r in d.records if r.kind == "setup_accepted").data
    again = next(r for r in replayed.records if r.kind == "setup_accepted").data
    assert original == again and original["pool_frozen"]["levels"] and original["geometry"]["rungs"]


def test_the_recorded_log_is_hash_chained_and_tamper_evident(tmp_path):
    _d, root = _recorded_run(tmp_path, [Q])
    files = input_files(root)
    assert verify_chain(files) == (True, "OK")
    lines = files[0].read_text().splitlines()
    lines[5] = lines[5].replace("149", "148", 1)                    # edit one recorded number
    files[0].write_text("\n".join(lines) + "\n")
    ok, why = verify_chain(files)
    assert ok is False and "HASH_MISMATCH" in why
    with pytest.raises(ValueError):
        replay(files)


def test_truncated_or_reordered_logs_are_detected(tmp_path):
    _d, root = _recorded_run(tmp_path, [Q])
    files = input_files(root)
    lines = files[0].read_text().splitlines()
    del lines[10]
    files[0].write_text("\n".join(lines) + "\n")
    assert verify_chain(files)[0] is False


def test_header_records_config_symbol_rules_and_data_quality(tmp_path):
    _d, root = _recorded_run(tmp_path, [Q])
    h = read_header(input_files(root)[0])
    assert h["config_digest"] == CFG.digest() and h["spec"] == "LSRL_V1" and h["mode"] == "PAPER"
    assert h["symbol_rules"]["tick_size"] == RULES.tick_size
    assert (h["schema_version"], h["strategy"], h["symbol"], h["exchange"]) == (2, "LSRL_V1", "SOLUSDT", "bitget")
    assert h["session_id"] and h["start_ts_ms"] > 0 and h["git_sha"] and "feed_digest" in h


def test_the_chain_continues_across_a_process_restart(tmp_path):
    root = tmp_path / "rec"
    for _ in range(2):
        rec = LSRLRecorder(root, CFG, RULES, mode="PAPER", min_free_gb=0.001)
        for i in range(3):
            rec.record_event(make_book(S + i * 10_000, 149.95, 150.05))
        rec.close()
    assert verify_chain(input_files(root)) == (True, "OK")
    sessions = [json.loads(l)["body"] for l in input_files(root)[0].read_text().splitlines()
                if json.loads(l)["body"]["type"] == "session_start"]
    assert len(sessions) == 2


def test_the_log_rotates_per_utc_day_without_breaking_the_chain(tmp_path):
    rec = LSRLRecorder(tmp_path / "rec", CFG, RULES, mode="PAPER", min_free_gb=0.001)
    day = 86_400_000
    edge = (S // day + 1) * day
    for t in (edge - 20_000, edge - 10_000, edge + 10_000, edge + 20_000):
        rec.record_event(make_book(t, 149.95, 150.05))
    rec.close()
    files = input_files(tmp_path / "rec")
    assert len(files) == 2 and verify_chain(files) == (True, "OK")


def test_low_disk_fails_closed_and_halts_new_setups_without_losing_position_management(tmp_path):
    rec = LSRLRecorder(tmp_path / "rec", CFG, RULES, mode="PAPER", min_free_gb=1e9)
    d = run(recorder=rec)
    assert d.recorder_error and "RECORDER_DISK_GUARD" in d.recorder_error
    assert d.engine.setup is None and any(r.startswith("ENGINE_HALTED") for r in
                                          [x.data.get("reason", "") for x in d.records if x.kind == "setup_rejected"])


def test_recorder_raises_rather_than_dropping_a_write(tmp_path):
    rec = LSRLRecorder(tmp_path / "rec", CFG, RULES, mode="PAPER", min_free_gb=1e9)
    with pytest.raises(RecorderError):
        rec.record_event(make_book(S, 149.95, 150.05))


def test_risk_gate_decisions_are_recorded_so_replay_needs_no_live_gate(tmp_path):
    from lsrl.core.gates import RiskDecision

    class Reduce:
        def check(self, p):
            return RiskDecision(True, ("reduced",), 0.0025, "RiskManager")
    rec = LSRLRecorder(tmp_path / "rec", CFG, RULES, mode="PAPER", min_free_gb=0.001)
    d = run(bars=[Q], recorder=rec, gate=Reduce())
    rec.close()
    replayed = replay(input_files(tmp_path / "rec"))
    assert replayed.engine.snapshot() == d.engine.snapshot()
    sizing = next(r for r in replayed.records if r.kind == "setup_accepted").data["sizing"]
    assert sizing["risk_fraction"] == 0.0025


def test_input_stream_serializes_every_event_type_losslessly():
    ev = make_book(S, 149.95, 150.05)
    assert isinstance(ev, DepthSnapshot)
    again = json.loads(json.dumps(event_to_dict(ev)))
    from lsrl.core.types import event_from_dict
    assert event_from_dict(again) == ev
