"""Observability may add output. It may not change a single decision.

This is the gate for the ranked-plan patch. It pins the property that matters:
for identical inputs, the selector's ranking, winner and every ranking key are
byte-identical whether or not telemetry is attached, and the telemetry call
does not mutate the selection it was handed.
"""

from __future__ import annotations

import copy

from execution.portfolio_selector import select_execution_winner
from telemetry.ranked_plans import RankedPlanLogger


class _Plan:
    def __init__(self, plan_id, symbol, direction, strategy, score, notes=None):
        self.plan_id = plan_id
        self.candidate_id = "cand_" + plan_id
        self.symbol = symbol
        self.direction = direction
        self.strategy = strategy
        self.score = score
        self.verdict = "EXECUTABLE"
        self.notes = notes or []
        self.reasons = []
        self.entry_prices = [100.0]
        self.stop_loss = 99.0
        self.take_profits = [101.0]


def _fixture():
    """A cycle with every ranking key exercised, including a real tie."""
    eq = ["planner_entry_quality=85", "spread_bps=1.40"]
    return [
        _Plan("p1", "BTCUSDT", "LONG", "low_vol_reclaim", 92.0, eq),
        _Plan("p2", "BTCUSDT", "LONG", "trend_continuation", 108.0, eq),
        _Plan("p3", "SOLUSDT", "SHORT", "momentum_breakout", 99.0,
              ["planner_entry_quality=95", "spread_bps=0.13",
               "entry_quality_long=100", "entry_quality_short=70"]),
        _Plan("p4", "DOGEUSDT", "SHORT", "momentum_breakdown", 98.0,
              ["planner_entry_quality=60", "spread_bps=4.0"]),
    ]


SCORES = {"BTCUSDT": 90.0, "SOLUSDT": 93.5, "DOGEUSDT": 81.25}


def _fingerprint(selection):
    """Everything the rest of the system can observe about a selection."""
    return {
        "winner": getattr(selection.winner, "plan_id", None),
        "order": [r.plan.plan_id for r in selection.ranked],
        "keys": [
            (r.plan.plan_id, r.execution_score, r.expectancy,
             r.setup_quality, r.liquidity_spread_quality)
            for r in selection.ranked
        ],
        "rejected": [(r.plan_id, r.reason) for r in selection.rejected],
    }


def test_selection_is_identical_with_and_without_telemetry(tmp_path):
    without = _fingerprint(select_execution_winner(_fixture(), execution_scores=SCORES))

    selection = select_execution_winner(_fixture(), execution_scores=SCORES)
    RankedPlanLogger(path=tmp_path / "r.jsonl").append("s", "t", selection)
    with_telemetry = _fingerprint(selection)

    assert with_telemetry == without


def test_telemetry_does_not_mutate_the_selection(tmp_path):
    selection = select_execution_winner(_fixture(), execution_scores=SCORES)
    before = copy.deepcopy(_fingerprint(selection))
    RankedPlanLogger(path=tmp_path / "r.jsonl").append("s", "t", selection)
    assert _fingerprint(selection) == before


def test_telemetry_does_not_mutate_the_plans(tmp_path):
    plans = _fixture()
    before = [(p.plan_id, p.symbol, p.direction, p.strategy, p.score,
               list(p.notes), p.stop_loss, list(p.take_profits), list(p.entry_prices))
              for p in plans]
    selection = select_execution_winner(plans, execution_scores=SCORES)
    RankedPlanLogger(path=tmp_path / "r.jsonl").append("s", "t", selection)
    after = [(p.plan_id, p.symbol, p.direction, p.strategy, p.score,
              list(p.notes), p.stop_loss, list(p.take_profits), list(p.entry_prices))
             for p in plans]
    assert after == before


def test_repeated_selection_is_deterministic():
    a = _fingerprint(select_execution_winner(_fixture(), execution_scores=SCORES))
    b = _fingerprint(select_execution_winner(_fixture(), execution_scores=SCORES))
    assert a == b


def test_input_order_does_not_change_the_outcome():
    forward = _fingerprint(select_execution_winner(_fixture(), execution_scores=SCORES))
    reverse = _fingerprint(
        select_execution_winner(list(reversed(_fixture())), execution_scores=SCORES)
    )
    assert reverse == forward


def test_logger_makes_no_network_or_exchange_calls(monkeypatch, tmp_path):
    """The hot path must not gain an API dependency."""
    import socket

    def _blocked(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("observability attempted a network call")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    selection = select_execution_winner(_fixture(), execution_scores=SCORES)
    assert RankedPlanLogger(path=tmp_path / "r.jsonl").append("s", "t", selection) is True
