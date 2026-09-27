"""Hostile: no new entry until startup recovery is fully accounted for.

`_ensure_startup_close_recovery` asked one question — `stats["blocked"]` — and a
sweep sets that flag only when it aborts outright. A lifecycle the exchange could
not resolve, or one that several history rows fit, raises inside the per-row
loop, lands in `still_pending`, and the sweep returns normally. The gate then
reported "complete", latched permanently, and let the bot open new positions
while an earlier close still counted in no risk decision.

Rows past the sweep's `limit` were worse: never attempted, so not even pending.

These tests drive the real gate and the real `recover_provisional_closes`, and
assert on whether `ExecutionService.execute` is reached at all.
"""

from __future__ import annotations

import logging

import pytest

from app.runner import StartupRunner
from execution.closed_lifecycle_recorder import recover_provisional_closes

OPEN_MS = 1_785_700_000_000


def stats(**overrides) -> dict:
    base = {
        "seen": 0, "skipped": 0, "recovered": 0, "still_pending": 0,
        "ambiguous": 0, "blocked": False, "unresolved_total": 0,
    }
    base.update(overrides)
    return base


class Runner:
    """The real startup gate and the real execution entry point."""

    _ensure_startup_close_recovery = StartupRunner._ensure_startup_close_recovery
    _startup_recovery_verdict = StartupRunner._startup_recovery_verdict
    _execute_selected_plans = StartupRunner._execute_selected_plans

    def __init__(self, sweeps):
        self.log = logging.getLogger("runner")
        self._startup_close_recovery_complete = False
        self.executed: list = []
        self.sweeps = list(sweeps)
        self.sweep_calls = 0

        outer = self

        def recover(_self, *a, **k):
            outer.sweep_calls += 1
            result = outer.sweeps[min(outer.sweep_calls - 1, len(outer.sweeps) - 1)]
            if isinstance(result, Exception):
                raise result
            return result

        self.position_manager = type("PM", (), {"recover_provisional_close_rows": recover})()
        self.execution_service = type("ES", (), {
            "execute": lambda _s, plans: (outer.executed.append(plans) or ["REPORT"]),
        })()


def run(sweeps, plans=("PLAN-A",)) -> Runner:
    runner = Runner(sweeps)
    runner._execute_selected_plans(list(plans))
    return runner


# ── H21: a lifecycle is still pending ───────────────────────────────────────

def test_h21_still_pending_blocks_execution():
    runner = run([stats(still_pending=1, unresolved_total=1, seen=1)])
    assert runner.executed == [], "a new entry opened while an old close was unresolved"
    assert runner._startup_close_recovery_complete is False, "the gate must not latch"


def test_h21_rows_past_the_sweep_limit_block_execution():
    """Not pending, because they were never even attempted."""
    runner = run([stats(recovered=20, unresolved_total=50, seen=20)])
    assert runner.executed == []


# ── H22: ambiguous recovery ─────────────────────────────────────────────────

def test_h22_ambiguous_blocks_execution():
    runner = run([stats(still_pending=1, ambiguous=1, unresolved_total=1, seen=1)])
    assert runner.executed == []


def test_h22_real_sweep_reports_ambiguity(tmp_path):
    """The `ambiguous` counter is produced by production code, not by the test."""
    dataset = tmp_path / "trade_dataset_v2.csv"
    dataset.write_text("event_type,symbol,direction\n", encoding="utf-8")
    provisional = {
        "event_type": "CLOSE_PROVISIONAL", "symbol": "BTCUSDT", "direction": "LONG",
        "opened_at_ms": OPEN_MS + 1_000, "confirmed_position_size": "0.001",
        "position_lifecycle_id": "LC-1",
    }

    def row(ctime):
        return {"symbol": "BTCUSDT", "holdSide": "long", "ctime": ctime,
                "openTotalPos": "0.001", "closeTotalPos": "0.001",
                "openAvgPrice": "100", "closeAvgPrice": "101", "pnl": "1",
                "netProfit": "0.88", "openFee": "-0.06", "closeFee": "-0.06",
                "totalFunding": "0", "positionId": f"P{ctime}"}

    written: list = []
    result = recover_provisional_closes(
        provisional_rows=[provisional],
        dataset_path=str(dataset),
        # two rows both inside the open-time tolerance: indistinguishable
        fetch_history=lambda: [row(OPEN_MS), row(OPEN_MS + 2_000)],
        write_economic_close=lambda r, e: written.append(e),
    )

    assert result["ambiguous"] == 1
    assert result["still_pending"] == 1
    assert result["blocked"] is False, "ambiguity returns normally -- that is the trap"
    assert written == []

    # and that normal return must still block the gate
    assert run([result]).executed == []


# ── H23: the recovery state itself is unknown ───────────────────────────────

@pytest.mark.parametrize("result", [
    None,
    "recovery unavailable",
    [],
    {"blocked": False},                                  # missing every counter
    {"blocked": False, "still_pending": 0},              # missing the rest
    {"blocked": False, "still_pending": "?", "ambiguous": 0,
     "unresolved_total": 0, "recovered": 0},             # unreadable counter
])
def test_h23_unknown_recovery_state_blocks_execution(result):
    runner = run([result])
    assert runner.executed == []
    assert runner._startup_close_recovery_complete is False


# ── H24: transport failure ──────────────────────────────────────────────────

@pytest.mark.parametrize("exc", [
    ConnectionError("transport down"),
    TimeoutError("read timeout"),
    RuntimeError("HTTP 502"),
])
def test_h24_transport_failure_blocks_execution(exc):
    runner = run([exc])
    assert runner.executed == []
    assert runner._startup_close_recovery_complete is False


def test_h24_blocked_sweep_blocks_execution():
    runner = run([stats(blocked=True, still_pending=1, unresolved_total=1)])
    assert runner.executed == []


# ── H25: a fully accounted sweep releases execution ─────────────────────────

def test_h25_clean_recovery_allows_execution():
    runner = run([stats()])
    assert runner.executed == [["PLAN-A"]]
    assert runner._startup_close_recovery_complete is True


def test_h25_everything_recovered_allows_execution():
    runner = run([stats(seen=3, recovered=3, unresolved_total=3)])
    assert runner.executed == [["PLAN-A"]]


def test_h25_skipped_rows_do_not_block():
    """Rows that already had economics are accounted for, not outstanding."""
    runner = run([stats(skipped=4)])
    assert runner.executed == [["PLAN-A"]]


# ── H26: blocked, then successful ───────────────────────────────────────────

def test_h26_blocked_then_success_executes_exactly_once():
    runner = Runner([
        stats(still_pending=1, unresolved_total=1, seen=1),   # cycle 1: blocked
        stats(seen=1, recovered=1, unresolved_total=1),       # cycle 2: resolved
        stats(),                                              # cycle 3: nothing left
    ])

    runner._execute_selected_plans(["PLAN-A"])
    assert runner.executed == [], "cycle 1 must not trade"
    assert runner.sweep_calls == 1

    runner._execute_selected_plans(["PLAN-A"])
    assert runner.executed == [["PLAN-A"]], "cycle 2 must trade once"
    assert runner.sweep_calls == 2

    runner._execute_selected_plans(["PLAN-B"])
    assert runner.executed == [["PLAN-A"], ["PLAN-B"]], "each cycle executes its own plans"
    assert runner.sweep_calls == 2, "the completed phase must not re-sweep"


# ── each refusal must stand on its own ──────────────────────────────────────
#
# A sweep that fails usually violates several conditions at once, which would let
# any single check be deleted without a test noticing. These fixtures are clean
# in every respect but one.

def test_only_blocked_is_enough_to_refuse():
    runner = run([stats(blocked=True)])
    assert runner.executed == []


def test_only_ambiguous_is_enough_to_refuse():
    runner = run([stats(ambiguous=1)])
    assert runner.executed == []


def test_only_still_pending_is_enough_to_refuse():
    runner = run([stats(still_pending=1)])
    assert runner.executed == []


def test_only_an_unattempted_row_is_enough_to_refuse():
    runner = run([stats(unresolved_total=1)])
    assert runner.executed == []


def test_h26_a_blocked_cycle_never_latches_the_phase():
    runner = Runner([stats(still_pending=1, unresolved_total=1)] * 5)
    for _ in range(5):
        runner._execute_selected_plans(["PLAN-A"])
    assert runner.executed == []
    assert runner.sweep_calls == 5, "a blocked phase must keep retrying"
