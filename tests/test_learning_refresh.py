"""Tests for the in-bot learning-artifact refresh chain (StartupRunner)."""

import logging
import os
import time
from pathlib import Path

import app.runner as runner_mod
from app.runner import StartupRunner


class FakeProc:
    def __init__(self, argv):
        self.argv = argv
        self.returncode = None

    def poll(self):
        return self.returncode


def _make_runner() -> StartupRunner:
    runner = object.__new__(StartupRunner)
    runner.log = logging.getLogger("test_learning_refresh")
    runner._learning_refresh_proc = None
    runner._learning_refresh_step = None
    runner._learning_refresh_queue = []
    runner._learning_refresh_failed_steps = []
    runner._learning_refresh_active = False
    runner._learning_refresh_last_start = 0.0
    return runner


def _setup_workdir(tmp_path, monkeypatch, spawned):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()

    def fake_popen(argv, stdout=None, stderr=None):
        proc = FakeProc(argv)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(runner_mod.subprocess, "Popen", fake_popen)


def _write_artifact(tmp_path, rel_path: str, age_hours: float) -> Path:
    path = tmp_path / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    stamp = time.time() - age_hours * 3600
    os.utime(path, (stamp, stamp))
    return path


def _write_all_artifacts(tmp_path, age_hours: float) -> None:
    for _name, rel_path in StartupRunner.LEARNING_ARTIFACTS:
        _write_artifact(tmp_path, rel_path, age_hours)


def test_no_refresh_when_all_artifacts_fresh(tmp_path, monkeypatch):
    spawned = []
    _setup_workdir(tmp_path, monkeypatch, spawned)
    _write_all_artifacts(tmp_path, age_hours=1.0)

    runner = _make_runner()
    runner._maybe_refresh_learning_reports()

    assert spawned == []
    assert runner._learning_refresh_queue == []


def test_stale_daily_report_triggers_chain_even_if_expectancy_fresh(tmp_path, monkeypatch):
    # The original regression: the chain only keyed on strategy_expectancy.json,
    # so a silently failing dataset_builder left daily_learning_report.json
    # (kill-switch input) stale forever.
    spawned = []
    _setup_workdir(tmp_path, monkeypatch, spawned)
    _write_all_artifacts(tmp_path, age_hours=1.0)
    _write_artifact(tmp_path, "data_store/trades/daily_learning_report.json", age_hours=26.0)

    runner = _make_runner()
    runner._maybe_refresh_learning_reports()

    assert len(spawned) == 1
    assert spawned[0].argv[-1] == "telemetry.dataset_builder"
    remaining = [step for step, _argv in runner._learning_refresh_queue]
    assert remaining == ["validation_backtest", "morning_audit", "knowledge_builder"]


def test_missing_learning_json_triggers_chain(tmp_path, monkeypatch):
    spawned = []
    _setup_workdir(tmp_path, monkeypatch, spawned)
    _write_all_artifacts(tmp_path, age_hours=1.0)
    (tmp_path / "agents_v2" / "reports" / "learning.json").unlink()

    runner = _make_runner()
    runner._maybe_refresh_learning_reports()

    assert len(spawned) == 1
    assert runner._learning_refresh_active


def test_step_failure_logs_failed_tag_and_chain_continues(tmp_path, monkeypatch, caplog):
    spawned = []
    _setup_workdir(tmp_path, monkeypatch, spawned)
    _write_all_artifacts(tmp_path, age_hours=30.0)

    runner = _make_runner()
    with caplog.at_level(logging.INFO, logger="test_learning_refresh"):
        runner._maybe_refresh_learning_reports()
        assert len(spawned) == 1

        # dataset_builder fails -> WARNING with the FAILED tag, chain continues.
        spawned[0].returncode = 1
        runner._maybe_refresh_learning_reports()
        assert len(spawned) == 2

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("LEARNING_REFRESH_FAILED" in r.getMessage() for r in warnings)

        # Remaining steps succeed; drained chain logs an ERROR summary because
        # one step failed.
        while runner._learning_refresh_proc is not None or runner._learning_refresh_queue:
            if runner._learning_refresh_proc is not None:
                runner._learning_refresh_proc.returncode = 0
            runner._maybe_refresh_learning_reports()
        assert len(spawned) == 4

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any(
        "LEARNING_REFRESH_FAILED" in r.getMessage() and "dataset_builder:exit=1" in r.getMessage()
        for r in errors
    )
    assert not runner._learning_refresh_active


def test_successful_chain_logs_ok_summary(tmp_path, monkeypatch, caplog):
    spawned = []
    _setup_workdir(tmp_path, monkeypatch, spawned)
    _write_all_artifacts(tmp_path, age_hours=30.0)

    runner = _make_runner()
    with caplog.at_level(logging.INFO, logger="test_learning_refresh"):
        runner._maybe_refresh_learning_reports()
        while runner._learning_refresh_proc is not None or runner._learning_refresh_queue:
            if runner._learning_refresh_proc is not None:
                runner._learning_refresh_proc.returncode = 0
            runner._maybe_refresh_learning_reports()

    steps = [proc.argv for proc in spawned]
    assert len(steps) == 4
    assert steps[-1][-1] == "agents_v2.learning.knowledge_builder"
    assert any("LEARNING_REFRESH_OK" in r.getMessage() for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_retry_backoff_prevents_restart_within_an_hour(tmp_path, monkeypatch):
    spawned = []
    _setup_workdir(tmp_path, monkeypatch, spawned)
    _write_all_artifacts(tmp_path, age_hours=30.0)

    runner = _make_runner()
    runner._maybe_refresh_learning_reports()
    while runner._learning_refresh_proc is not None or runner._learning_refresh_queue:
        if runner._learning_refresh_proc is not None:
            runner._learning_refresh_proc.returncode = 1
        runner._maybe_refresh_learning_reports()

    # Artifacts are still stale, but the chain must not restart immediately.
    count_after_first_chain = len(spawned)
    runner._maybe_refresh_learning_reports()
    assert len(spawned) == count_after_first_chain

    # After the backoff window it retries.
    runner._learning_refresh_last_start = time.time() - StartupRunner.LEARNING_REFRESH_RETRY_MIN_SEC - 1
    runner._maybe_refresh_learning_reports()
    assert len(spawned) == count_after_first_chain + 1


def test_spawn_failure_is_logged_and_queue_continues(tmp_path, monkeypatch, caplog):
    spawned = []
    _setup_workdir(tmp_path, monkeypatch, spawned)
    _write_all_artifacts(tmp_path, age_hours=30.0)

    calls = {"count": 0}
    real_popen = runner_mod.subprocess.Popen

    def flaky_popen(argv, stdout=None, stderr=None):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("spawn blocked")
        return real_popen(argv, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(runner_mod.subprocess, "Popen", flaky_popen)

    runner = _make_runner()
    with caplog.at_level(logging.INFO, logger="test_learning_refresh"):
        runner._maybe_refresh_learning_reports()

    assert runner._learning_refresh_proc is None
    assert runner._learning_refresh_failed_steps == ["dataset_builder:spawn"]
    assert any("LEARNING_REFRESH_FAILED" in r.getMessage() for r in caplog.records)

    # Next cycle moves on to the next queued step.
    runner._maybe_refresh_learning_reports()
    assert len(spawned) == 1
    assert runner._learning_refresh_step == "validation_backtest"
