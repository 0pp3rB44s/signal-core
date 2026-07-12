import csv
from types import SimpleNamespace

from app.runner import StartupRunner
from telemetry.trade_logger import StrategyPerformanceLogger


class _CapturingLog:
    def __init__(self):
        self.warnings = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)


def _runner_stub(tmp_path):
    return SimpleNamespace(
        strategy_performance_logger=StrategyPerformanceLogger(tmp_path / "strategy_performance.csv"),
        log=_CapturingLog(),
    )


def _plan(verdict="EXECUTABLE"):
    return SimpleNamespace(
        symbol="FETUSDT",
        strategy="trend_continuation",
        direction="SHORT",
        verdict=verdict,
        notes=["planner_gate=trend_continuation"],
    )


def _score():
    return SimpleNamespace(total=89.0, reasons=["score reason"])


def _risk():
    return SimpleNamespace(reasons=["risk reason"])


def _candidate():
    return SimpleNamespace(notes=["fast_lane=true", "fast_lane_granularity=5m"])


def _rows(tmp_path):
    with (tmp_path / "strategy_performance.csv").open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_executable_fast_lane_plan_writes_plan_row(tmp_path):
    runner = _runner_stub(tmp_path)

    StartupRunner._log_fast_lane_funnel(runner, _plan("EXECUTABLE"), _score(), _risk(), _candidate())

    rows = _rows(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["stage"] == "PLAN"
    assert row["symbol"] == "FETUSDT"
    assert row["strategy"] == "trend_continuation"
    assert row["direction"] == "SHORT"
    assert row["verdict"] == "EXECUTABLE"
    assert "fast_lane=true" in row["notes"]
    assert not runner.log.warnings


def test_blocked_fast_lane_plan_writes_plan_and_reject_rows(tmp_path):
    runner = _runner_stub(tmp_path)

    StartupRunner._log_fast_lane_funnel(runner, _plan("BLOCKED"), _score(), _risk(), _candidate())

    rows = _rows(tmp_path)
    assert [row["stage"] for row in rows] == ["PLAN", "PLAN_REJECT"]
    assert all(row["verdict"] == "BLOCKED" for row in rows)
    assert rows[0]["reasons"] == "risk reason | score reason"
    assert rows[1]["reasons"] == "risk reason"


def test_telemetry_failure_never_raises(tmp_path):
    runner = _runner_stub(tmp_path)

    class _BrokenLogger:
        def append_setup_event(self, **kwargs):
            raise RuntimeError("disk full")

    runner.strategy_performance_logger = _BrokenLogger()

    StartupRunner._log_fast_lane_funnel(runner, _plan("EXECUTABLE"), _score(), _risk(), _candidate())

    assert any("FAST_LANE_TELEMETRY_FAILED" in warning for warning in runner.log.warnings)
