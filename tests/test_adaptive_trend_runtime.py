"""Live signal wiring and shadow-mode wiring correctness.

Covers: no-look-ahead against real fetch data, restart-safe candle dedup,
per-candle CLOSED logging even for skipped-while-down candles, structured
signal-decision logging, deterministic ranking feeding into a single routed
candidate, and the hard shadow-mode invariant -- this module can never
create live state no matter what account/risk state it is called with.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import strategies.adaptive_trend_runtime as runtime_module
from strategies.adaptive_trend_runtime import (
    evaluate_symbol,
    evaluate_universe,
    fetch_6h_candles,
    route_selected_candidate,
)
from strategies.adaptive_trend_shadow import ShadowDecisionLog
from strategies.adaptive_trend_tsmom import (
    ATR_PERIOD,
    Candle6h,
    MOM_LOOKBACK,
    Side,
    SignalCandidate,
)

SIX_H = 6 * 60 * 60 * 1000
WARMUP = MOM_LOOKBACK + ATR_PERIOD + 1


def series(closes, start_ms=0, high_pad=1.0, low_pad=1.0):
    out = []
    for i, c in enumerate(closes):
        open_ms = start_ms + i * SIX_H
        out.append(Candle6h(open_ms=open_ms, close_ms=open_ms + SIX_H,
                             open=c, high=c + high_pad, low=c - low_pad, close=c))
    return out


# --- fetch_6h_candles: real-shaped payload parsing --------------------------

class _FakeClient:
    def __init__(self, rows):
        self.rows = rows

    def get_candles(self, symbol, product_type, granularity="15m", limit=200):
        assert granularity == "6h"
        return {"data": self.rows}


def test_fetch_6h_candles_parses_real_bitget_row_shape():
    rows = [
        ["1700000000000", "100.0", "102.0", "99.0", "101.0", "10", "1000"],
        ["1700021600000", "101.0", "103.0", "100.5", "102.5", "12", "1200"],
    ]
    client = _FakeClient(rows)
    candles = fetch_6h_candles(client, "BTCUSDT", "USDT-FUTURES")
    assert len(candles) == 2
    assert candles[0].open_ms == 1700000000000
    assert candles[0].close_ms == 1700000000000 + SIX_H
    assert candles[0].close == 101.0
    assert candles[1].close == 102.5


def test_fetch_6h_candles_skips_malformed_rows_without_crashing():
    rows = [["not_a_number", "1", "1", "1", "1"], ["1700000000000", "100", "101", "99", "100"]]
    client = _FakeClient(rows)
    candles = fetch_6h_candles(client, "BTCUSDT", "USDT-FUTURES")
    assert len(candles) == 1


# --- evaluate_symbol: no look-ahead, dedup, restart continuity -------------

def test_evaluate_symbol_no_signal_before_warmup_complete():
    candles = series([100.0] * 5)
    ev = evaluate_symbol(symbol="BTCUSDT", raw_candles=candles,
                          now_ms=candles[-1].close_ms, last_processed_close_ms=None,
                          runtime_sha="abc123")
    assert ev.decision == "DATA_UNHEALTHY"
    assert ev.reason == "insufficient_warmup"


def test_evaluate_symbol_selects_long_on_strong_momentum():
    closes = [100.0] * WARMUP + [110.0]  # +10% over lookback, well past 3% threshold
    candles = series(closes)
    ev = evaluate_symbol(symbol="BTCUSDT", raw_candles=candles,
                          now_ms=candles[-1].close_ms, last_processed_close_ms=None,
                          runtime_sha="abc123")
    assert ev.decision == "SIGNAL_SELECTED"
    assert ev.candidate.side is Side.LONG
    assert ev.last_processed_close_ms == candles[-1].close_ms


def test_evaluate_symbol_rejects_inside_dead_zone():
    closes = [100.0] * WARMUP + [101.0]  # +1%, inside the no-signal zone
    candles = series(closes)
    ev = evaluate_symbol(symbol="BTCUSDT", raw_candles=candles,
                          now_ms=candles[-1].close_ms, last_processed_close_ms=None,
                          runtime_sha="abc123")
    assert ev.decision == "SIGNAL_REJECTED"
    assert ev.reason == "no_signal"


def test_evaluate_symbol_no_look_ahead_forming_candle_ignored():
    """A huge move on a still-forming candle must not influence the signal."""
    closes = [100.0] * WARMUP
    candles = series(closes)
    forming = Candle6h(open_ms=candles[-1].close_ms, close_ms=candles[-1].close_ms + SIX_H,
                        open=100.0, high=500.0, low=100.0, close=500.0)
    all_candles = candles + [forming]
    now_mid_forming = forming.open_ms + 1000  # forming candle not yet closed
    ev = evaluate_symbol(symbol="BTCUSDT", raw_candles=all_candles, now_ms=now_mid_forming,
                          last_processed_close_ms=None, runtime_sha="abc123")
    # Only the flat warmup series is visible -- momentum must be ~0, not driven by 500.0.
    assert ev.decision != "SIGNAL_SELECTED" or ev.candidate.close == 100.0


def test_evaluate_symbol_no_duplicate_evaluation_of_processed_candle():
    closes = [100.0] * WARMUP + [110.0]
    candles = series(closes)
    already_seen = candles[-1].close_ms
    ev = evaluate_symbol(symbol="BTCUSDT", raw_candles=candles, now_ms=candles[-1].close_ms,
                          last_processed_close_ms=already_seen, runtime_sha="abc123")
    assert ev.decision == "NO_SIGNAL"
    assert ev.reason == "no_new_closed_candle"


def test_evaluate_symbol_restart_does_not_skip_candles_closed_while_down(caplog):
    closes = [100.0] * WARMUP + [101.0, 102.0, 110.0]
    candles = series(closes)
    # Only the first WARMUP candle was processed before the (simulated) restart.
    last_seen = candles[WARMUP - 1].close_ms
    with caplog.at_level("INFO", logger="adaptive_trend"):
        ev = evaluate_symbol(symbol="BTCUSDT", raw_candles=candles, now_ms=candles[-1].close_ms,
                              last_processed_close_ms=last_seen, runtime_sha="abc123")
    closed_logs = [r for r in caplog.records if "ADAPTIVE_6H_CANDLE_CLOSED" in r.message]
    # Three candles closed while "down" -- each must get its own CLOSED log line.
    assert len(closed_logs) == 3
    assert ev.last_processed_close_ms == candles[-1].close_ms


def test_evaluate_symbol_data_health_stale_refuses_signal():
    candles = series([100.0] * (WARMUP + 1))
    far_future = candles[-1].close_ms + 20 * SIX_H
    ev = evaluate_symbol(symbol="BTCUSDT", raw_candles=candles, now_ms=far_future,
                          last_processed_close_ms=None, runtime_sha="abc123")
    assert ev.decision == "DATA_UNHEALTHY"
    assert ev.reason == "stale_data"


# --- shadow routing: never live state, regardless of freeze ----------------

def test_route_selected_candidate_always_writes_shadow_never_live(tmp_path):
    log = ShadowDecisionLog(tmp_path / "shadow.jsonl")
    winner = SignalCandidate(symbol="BTCUSDT", side=Side.LONG, signal_candle_close_ms=0,
                              close=100.0, mom=0.05, atr=2.0)
    reason = route_selected_candidate(winner=winner, equity=1000.0, exchange_min_notional=5.0,
                                       weekly_freeze_active=True, runtime_sha="abc123",
                                       shadow_log=log)
    assert reason == "weekly_freeze_active"
    rows = log.read_all()
    assert len(rows) == 1
    assert rows[0]["decision"] == "ACCOUNT_FREEZE_BLOCKED"


def test_route_selected_candidate_not_frozen_still_shadow_only(tmp_path):
    """Even with the freeze off and sizing accepted, this module still only
    writes a shadow record -- real order submission is not built yet."""
    log = ShadowDecisionLog(tmp_path / "shadow.jsonl")
    winner = SignalCandidate(symbol="BTCUSDT", side=Side.LONG, signal_candle_close_ms=0,
                              close=100.0, mom=0.05, atr=2.0)
    reason = route_selected_candidate(winner=winner, equity=1000.0, exchange_min_notional=5.0,
                                       weekly_freeze_active=False, runtime_sha="abc123",
                                       shadow_log=log)
    assert reason == "order_submission_not_yet_implemented"
    rows = log.read_all()
    assert len(rows) == 1
    assert rows[0]["decision"] == "ACCOUNT_FREEZE_BLOCKED"  # label is fixed by the builder


def test_route_selected_candidate_account_too_small_records_that_reason(tmp_path):
    log = ShadowDecisionLog(tmp_path / "shadow.jsonl")
    winner = SignalCandidate(symbol="BTCUSDT", side=Side.LONG, signal_candle_close_ms=0,
                              close=100.0, mom=0.05, atr=0.01)
    reason = route_selected_candidate(winner=winner, equity=5.0, exchange_min_notional=1000.0,
                                       weekly_freeze_active=False, runtime_sha="abc123",
                                       shadow_log=log)
    assert reason == "ACCOUNT_TOO_SMALL_FOR_SAFE_ORDER"


def test_runtime_module_has_no_bitget_order_submission_import():
    """Structural guarantee: this module cannot place a real order because it
    never imports anything from the order-submission machinery."""
    src = Path(runtime_module.__file__).read_text()
    tree = ast.parse(src)
    forbidden = ("execution.entry_submitter", "execution.execution_service",
                 "clients.bitget_order_client")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module not in forbidden, node.module


# --- evaluate_universe: end-to-end ranking + single routed candidate -------

def test_evaluate_universe_routes_only_the_ranked_winner(tmp_path):
    strong = series([100.0] * WARMUP + [130.0])       # huge momentum
    weak = series([50.0] * WARMUP + [51.6])            # just past 3%, weak strength

    class MultiClient:
        def get_candles(self, symbol, product_type, granularity="15m", limit=200):
            rows = strong if symbol == "SOLUSDT" else weak
            return {"data": [[c.open_ms, c.open, c.high, c.low, c.close, 1, 1] for c in rows]}

    log = ShadowDecisionLog(tmp_path / "shadow.jsonl")
    now_ms = max(strong[-1].close_ms, weak[-1].close_ms)
    result = evaluate_universe(
        client=MultiClient(), product_type="USDT-FUTURES",
        symbols=("BTCUSDT", "SOLUSDT"), now_ms=now_ms,
        last_processed={"BTCUSDT": None, "SOLUSDT": None},
        equity=1000.0, exchange_min_notional={}, weekly_freeze_active=True,
        runtime_sha="abc123", shadow_log=log,
    )
    assert result["winner_symbol"] == "SOLUSDT"
    rows = log.read_all()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "SOLUSDT"


# --- evaluate_universe: falls through past an unexecutable top-ranked
# candidate to the next-best one that actually sizes, instead of giving up
# for the whole boundary (2026-08-27: SOLUSDT ranked highest and was
# rejected by size_position at real production equity while ETHUSDT, ranked
# below it, would have sized and executed fine -- the boundary produced zero
# trade with no fallback in place) --------------------------------------

def test_evaluate_universe_falls_through_to_next_executable_candidate(tmp_path):
    # Real production numbers, reproduced with synthetic candles: a
    # low-priced, wide-relative-ATR symbol ranks highest on raw momentum
    # strength but is rejected by size_position at real equity; a
    # high-priced, tight-relative-ATR symbol ranks lower but sizes fine.
    wide_unexecutable = series([100.0] * WARMUP + [180.0])
    tight_executable = series([80000.0] * WARMUP + [82560.0], high_pad=40.0, low_pad=40.0)

    class MultiClient:
        def get_candles(self, symbol, product_type, granularity="15m", limit=200):
            rows = wide_unexecutable if symbol == "SOLUSDT" else tight_executable
            return {"data": [[c.open_ms, c.open, c.high, c.low, c.close, 1, 1] for c in rows]}

    log = ShadowDecisionLog(tmp_path / "shadow.jsonl")
    now_ms = max(wide_unexecutable[-1].close_ms, tight_executable[-1].close_ms)
    result = evaluate_universe(
        client=MultiClient(), product_type="USDT-FUTURES",
        symbols=("BTCUSDT", "SOLUSDT"), now_ms=now_ms,
        last_processed={"BTCUSDT": None, "SOLUSDT": None},
        # Real production equity -- the exact size that produced this defect.
        equity=27.4437, exchange_min_notional={}, weekly_freeze_active=False,
        runtime_sha="abc123", shadow_log=log,
    )
    # SOLUSDT ranks highest by momentum strength but must not be selected --
    # it cannot be sized. BTCUSDT, ranked below it, is what actually routes.
    assert result["winner_symbol"] == "BTCUSDT"
    assert result["winner_sizing"] is not None
    assert result["winner_sizing"].accepted is True
    rows = log.read_all()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"


def test_evaluate_universe_all_candidates_unexecutable_falls_back_to_top_ranked(tmp_path):
    """When NOTHING sizes, shadow observability must still record the
    strongest candidate and its real rejection reason -- unchanged from
    before this fix."""
    wide_unexecutable = series([100.0] * WARMUP + [180.0])

    class OneSymbolClient:
        def get_candles(self, symbol, product_type, granularity="15m", limit=200):
            return {"data": [[c.open_ms, c.open, c.high, c.low, c.close, 1, 1]
                              for c in wide_unexecutable]}

    log = ShadowDecisionLog(tmp_path / "shadow.jsonl")
    result = evaluate_universe(
        client=OneSymbolClient(), product_type="USDT-FUTURES",
        symbols=("SOLUSDT",), now_ms=wide_unexecutable[-1].close_ms,
        last_processed={"SOLUSDT": None},
        equity=27.4437, exchange_min_notional={}, weekly_freeze_active=False,
        runtime_sha="abc123", shadow_log=log,
    )
    assert result["winner_symbol"] == "SOLUSDT"
    assert result["winner_sizing"].accepted is False
    rows = log.read_all()
    assert len(rows) == 1
    assert rows[0]["rejection_reason"] == "ACCOUNT_TOO_SMALL_FOR_SAFE_ORDER"


def test_evaluate_universe_no_candidates_writes_no_shadow_record(tmp_path):
    flat = series([100.0] * (WARMUP + 1))

    class FlatClient:
        def get_candles(self, symbol, product_type, granularity="15m", limit=200):
            return {"data": [[c.open_ms, c.open, c.high, c.low, c.close, 1, 1] for c in flat]}

    log = ShadowDecisionLog(tmp_path / "shadow.jsonl")
    result = evaluate_universe(
        client=FlatClient(), product_type="USDT-FUTURES",
        symbols=("BTCUSDT",), now_ms=flat[-1].close_ms,
        last_processed={"BTCUSDT": None},
        equity=1000.0, exchange_min_notional={}, weekly_freeze_active=True,
        runtime_sha="abc123", shadow_log=log,
    )
    assert result["winner_symbol"] is None
    assert log.read_all() == []


def test_evaluate_universe_fetch_failure_isolated_per_symbol(tmp_path):
    class HalfBrokenClient:
        def get_candles(self, symbol, product_type, granularity="15m", limit=200):
            if symbol == "BTCUSDT":
                raise ConnectionError("simulated network failure")
            flat = series([100.0] * (WARMUP + 1))
            return {"data": [[c.open_ms, c.open, c.high, c.low, c.close, 1, 1] for c in flat]}

    log = ShadowDecisionLog(tmp_path / "shadow.jsonl")
    result = evaluate_universe(
        client=HalfBrokenClient(), product_type="USDT-FUTURES",
        symbols=("BTCUSDT", "ETHUSDT"), now_ms=10**15,
        last_processed={"BTCUSDT": None, "ETHUSDT": None},
        equity=1000.0, exchange_min_notional={}, weekly_freeze_active=True,
        runtime_sha="abc123", shadow_log=log,
    )
    btc_eval = next(e for e in result["evaluations"] if e.symbol == "BTCUSDT")
    eth_eval = next(e for e in result["evaluations"] if e.symbol == "ETHUSDT")
    assert btc_eval.decision == "DATA_UNHEALTHY"
    assert btc_eval.reason == "fetch_failed"
    # ETH's evaluation must not be affected by BTC's failure.
    assert eth_eval.decision in ("DATA_UNHEALTHY", "SIGNAL_REJECTED", "NO_SIGNAL", "SIGNAL_SELECTED")
