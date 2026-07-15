from __future__ import annotations

import ast
import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from analysis.strategy_funnel import StrategyFunnelAnalyzer
from app.config import Settings
from app.runner import StartupRunner
from telemetry.funnel import (
    EVENT_ORDER,
    EVENT_TYPES,
    SCHEMA_VERSION,
    FunnelEventStore,
    FunnelTelemetry,
    FunnelTelemetryCorruptionError,
    deterministic_candidate_id,
)
from candidate_lifecycle import deterministic_plan_id


def _event(candidate: str, event_type: str, passed: bool = True, **overrides):
    reasons = {
        "DETECTOR_ATTEMPT": "ATTEMPTED", "DETECTOR_DECISION": "DETECTED",
        "SELECTOR_DECISION": "SELECTED", "SCORING_DECISION": "SCORE_GO",
        "RISK_DECISION": "RISK_ALLOWED", "PLANNER_DECISION": "PLAN_EXECUTABLE",
        "EXECUTABLE_DECISION": "PLAN_EXECUTABLE", "FORWARD_PAPER_LINK": "FORWARD_LINKED",
        "OUTCOME_LINK": "FORWARD_LINKED",
    }
    value = {
        "schema_version": SCHEMA_VERSION, "event_id": f"{candidate}-{event_type}",
        "scan_id": "scan-1", "candidate_id": candidate, "event_type": event_type,
        "event_timestamp_utc": "2026-01-01T00:00:00+00:00",
        "strategy": "momentum_breakout", "symbol": "BTCUSDT", "direction": "LONG",
        "timeframe": "15m", "candle_open_timestamp": "1767225600000",
        "signal_timestamp": "2026-01-01T00:15:00+00:00", "session": "EU",
        "regime": "bullish", "pass_fail": "PASS" if passed else "FAIL",
        "primary_reason_code": reasons[event_type], "secondary_reason_codes": [],
        "config_hash": "config", "git_commit": "commit",
    }
    value.update(overrides)
    return value


def _append_worker(path: str, quality: str, index: int) -> None:
    store = FunnelEventStore(path, quality)
    store.append(_event(f"candidate-{index}", "DETECTOR_ATTEMPT"))


def test_complete_candidate_lifecycle_is_hash_chained(tmp_path: Path) -> None:
    store = FunnelEventStore(tmp_path / "events.jsonl", tmp_path / "quality.json")
    for event_type in sorted(EVENT_TYPES, key=EVENT_ORDER.get):
        assert store.append(_event("candidate", event_type))
    events = store.read_events()
    assert {event["event_type"] for event in events} == EVENT_TYPES
    assert events[0]["previous_hash"] == "GENESIS"
    assert all(events[index]["previous_hash"] == events[index - 1]["event_hash"] for index in range(1, len(events)))
    assert store.audit()["event_chain_valid"] is True


@pytest.mark.parametrize("event_type", sorted(EVENT_TYPES - {"DETECTOR_ATTEMPT"}))
def test_reject_at_each_decision_stage(event_type: str, tmp_path: Path) -> None:
    event = _event("candidate", event_type, passed=False)
    event["primary_reason_code"] = {
        "DETECTOR_DECISION": "NO_DETECTION", "SELECTOR_DECISION": "NOT_SELECTED",
        "SCORING_DECISION": "SCORE_NO_GO", "RISK_DECISION": "RISK_BLOCKED",
        "PLANNER_DECISION": "PLAN_BLOCKED", "EXECUTABLE_DECISION": "PLAN_BLOCKED",
        "FORWARD_PAPER_LINK": "FORWARD_NOT_ELIGIBLE", "OUTCOME_LINK": "UNKNOWN_DECISION",
    }[event_type]
    store = FunnelEventStore(tmp_path / "events.jsonl", tmp_path / "quality.json")
    for prefix_type in sorted(EVENT_TYPES, key=EVENT_ORDER.get):
        if EVENT_ORDER[prefix_type] >= EVENT_ORDER[event_type]:
            break
        assert store.append(_event("candidate", prefix_type))
    assert store.append(event)
    assert store.read_events()[-1]["pass_fail"] == "FAIL"


def test_stable_scan_and_candidate_ids(tmp_path: Path) -> None:
    settings = SimpleNamespace()
    telemetry = FunnelTelemetry(settings, FunnelEventStore(tmp_path / "events", tmp_path / "quality"))
    candidate = deterministic_candidate_id("momentum_breakout", "btcusdt", "long", 123)
    assert candidate == deterministic_candidate_id("MOMENTUM_BREAKOUT", "BTCUSDT", "LONG", "123")
    kwargs = dict(
        scan_id="scan", candidate_id=candidate, event_type="DETECTOR_ATTEMPT",
        strategy="momentum_breakout", symbol="BTCUSDT", direction="LONG", timeframe="15m",
        candle_open_timestamp="123", signal_timestamp="now", session="EU", regime="bullish",
        passed=True, primary_reason_code="ATTEMPTED",
    )
    assert telemetry.event(**kwargs)
    assert telemetry.event(**kwargs) is False
    assert len(telemetry.store.read_events()) == 1


def test_overlap_uses_candle_open_timestamp_and_structured_data_wins(tmp_path: Path) -> None:
    store = FunnelEventStore(tmp_path / "data_store/funnel_events.jsonl", tmp_path / "quality.json")
    store.append(_event("a", "DETECTOR_ATTEMPT"))
    store.append(_event("a", "DETECTOR_DECISION"))
    store.append(_event(
        "b", "DETECTOR_ATTEMPT", strategy="trend_continuation",
        event_id="b-attempt",
    ))
    store.append(_event(
        "b", "DETECTOR_DECISION", strategy="trend_continuation",
        event_id="b-detected",
    ))
    report = StrategyFunnelAnalyzer(tmp_path).analyze()
    assert report["dataset_views"][0]["dataset_scope"] == "structured_funnel_current"
    assert report["overlap_analysis"]["pairs"] == [{
        "strategy_a": "momentum_breakout", "strategy_b": "trend_continuation",
        "same_candle_count": 1,
    }]


def test_duplicate_crash_resume_and_corruption(tmp_path: Path) -> None:
    path, quality = tmp_path / "events", tmp_path / "quality"
    first = FunnelEventStore(path, quality)
    event = _event("candidate", "DETECTOR_ATTEMPT")
    assert first.append(event)
    resumed = FunnelEventStore(path, quality)
    assert resumed.append(event) is False
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{broken\n")
    with pytest.raises(FunnelTelemetryCorruptionError):
        resumed.read_events()
    assert resumed.audit()["event_chain_valid"] is False


def test_concurrent_append_is_lossless(tmp_path: Path) -> None:
    path, quality = str(tmp_path / "events"), str(tmp_path / "quality")
    processes = [multiprocessing.Process(target=_append_worker, args=(path, quality, index)) for index in range(8)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    events = FunnelEventStore(path, quality).read_events()
    assert len(events) == 8
    assert [event["sequence"] for event in events] == list(range(1, 9))


def test_missing_fields_and_unknown_reason_are_rejected(tmp_path: Path) -> None:
    store = FunnelEventStore(tmp_path / "events", tmp_path / "quality")
    event = _event("candidate", "DETECTOR_ATTEMPT")
    del event["scan_id"]
    with pytest.raises(ValueError, match="scan_id"):
        store.append(event)
    event = _event("candidate", "DETECTOR_ATTEMPT", primary_reason_code="free text")
    with pytest.raises(ValueError, match="reason"):
        store.append(event)


def test_analyzer_output_is_deterministic_for_structured_events(tmp_path: Path) -> None:
    store = FunnelEventStore(tmp_path / "data_store/funnel_events.jsonl", tmp_path / "quality")
    store.append(_event("candidate", "DETECTOR_ATTEMPT"))
    store.append(_event("candidate", "DETECTOR_DECISION"))
    first = StrategyFunnelAnalyzer(tmp_path).analyze()
    second = StrategyFunnelAnalyzer(tmp_path).analyze()
    assert first["analysis_hash"] == second["analysis_hash"]


def test_instrumentation_has_no_private_calls_or_decision_mutation() -> None:
    source = (Path(__file__).parents[1] / "telemetry/funnel.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        node.module.split(".")[0] for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imports.isdisjoint({"clients", "execution", "planning", "risk", "strategies"})
    runner = (Path(__file__).parents[1] / "app/runner.py").read_text(encoding="utf-8")
    assert "get_accounts" not in source and "execute(" not in source
    assert "self.funnel_telemetry.event" in runner
    # Telemetry observes returned objects; it does not assign verdict/allowed/score.
    assert ".verdict =" not in source and ".allowed =" not in source and ".total =" not in source


def test_lifecycle_key_deduplicates_across_scans(tmp_path: Path) -> None:
    store = FunnelEventStore(tmp_path / "events", tmp_path / "quality")
    assert store.append(_event("candidate", "SELECTOR_DECISION"))
    duplicate = _event("candidate", "SELECTOR_DECISION", scan_id="scan-2", event_id="scan-2-selector")
    assert store.append(duplicate) is False


def test_append_uses_incremental_index_after_initial_sync(tmp_path: Path, monkeypatch) -> None:
    path, quality = tmp_path / "events", tmp_path / "quality"
    first = FunnelEventStore(path, quality)
    assert first.append(_event("candidate-1", "DETECTOR_ATTEMPT"))
    resumed = FunnelEventStore(path, quality)
    assert resumed.append(_event("candidate-2", "DETECTOR_ATTEMPT"))

    def forbidden_full_read():
        raise AssertionError("append must not reread the complete JSONL file")

    monkeypatch.setattr(resumed, "_read_unlocked", forbidden_full_read)
    assert resumed.append(_event("candidate-3", "DETECTOR_ATTEMPT"))


def test_analyzer_rejects_broken_structured_chain(tmp_path: Path) -> None:
    path = tmp_path / "data_store/funnel_events.jsonl"
    store = FunnelEventStore(path, tmp_path / "quality")
    store.append(_event("candidate", "DETECTOR_ATTEMPT"))
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["event_hash"] = "tampered"
    path.write_text(json.dumps(stored) + "\n", encoding="utf-8")
    report = StrategyFunnelAnalyzer(tmp_path).analyze()
    assert any(
        issue["code"] == "STRUCTURED_FUNNEL_INVALID"
        for issue in report["data_quality"]["issues"]
    )
    assert all(row["dataset_scope"] != "structured_funnel_current" for row in report["dataset_views"])


def test_structured_rejects_use_fixed_codes_and_context(tmp_path: Path) -> None:
    store = FunnelEventStore(
        tmp_path / "data_store/funnel_events.jsonl", tmp_path / "quality"
    )
    store.append(_event("candidate", "DETECTOR_ATTEMPT"))
    store.append(_event("candidate", "DETECTOR_DECISION"))
    store.append(_event(
        "candidate", "SELECTOR_DECISION", passed=False,
        primary_reason_code="SIGNAL_COOLDOWN", secondary_reason_codes=[],
    ))
    report = StrategyFunnelAnalyzer(tmp_path).analyze()
    reject = next(row for row in report["reject_analysis"] if row["reason"] == "SIGNAL_COOLDOWN")
    assert reject["symbols"] == {"BTCUSDT": 1}
    assert reject["sessions"] == {"EU": 1}
    assert reject["timeframes"] == {"15m": 1}


def test_scan_planner_outcome_is_identical_when_telemetry_fails(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        _env_file=None, FORWARD_PAPER_ONLY=True, FAST_LANE_ENABLED=False,
        BITGET_RATE_LIMIT_STATE_PATH=str(tmp_path / "rate-limit.json"),
    )
    primary = SimpleNamespace(
        candles=[SimpleNamespace(timestamp_ms=1767225600000)], granularity="15m",
        trend="UP", change_pct=1.0, volume_ratio_20=1.2, latest_close=101.0,
    )
    snapshot = SimpleNamespace(
        symbol="BTCUSDT", primary=primary,
        confirmation=SimpleNamespace(granularity="1H", trend="UP"),
        alignment="aligned_bullish", score_hint=80.0, volatility_rank=70.0,
        notes=[], context={},
    )
    candidate = SimpleNamespace(
        strategy="momentum_breakout", symbol="BTCUSDT", direction="LONG",
        notes=[], market=snapshot,
        detection=SimpleNamespace(entry_hint=101.0, invalidation=99.0),
    )
    candidate.candidate_candle_open_timestamp_ms = 1767225600000
    candidate.candidate_id = deterministic_candidate_id(candidate.strategy, candidate.symbol, candidate.direction, candidate.candidate_candle_open_timestamp_ms)
    score = SimpleNamespace(verdict="GO", total=82.0, reasons=[])
    risk = SimpleNamespace(allowed=True, status="ALLOWED", reasons=[])
    plan = SimpleNamespace(
        candidate_id=candidate.candidate_id,
        candidate_candle_open_timestamp_ms=candidate.candidate_candle_open_timestamp_ms,
        plan_id=deterministic_plan_id(candidate.candidate_id),
        verdict="EXECUTABLE", symbol="BTCUSDT", strategy="momentum_breakout",
        direction="LONG", risk_reward_ratio=2.0, notes=[], score=82.0,
        entry_prices=[101.0], stop_loss=99.0, take_profits=[105.0],
        account_risk_pct=0.5, leverage=1.0, position_notional_usdt=100.0,
    )

    def run_with(telemetry):
        runner = StartupRunner(settings)
        runner.funnel_telemetry = telemetry
        runner._maybe_refresh_learning_reports = MagicMock()
        runner.fetcher.fetch_contracts = MagicMock(return_value=[])
        runner.fetcher.build_market_snapshot = MagicMock(return_value=snapshot)
        runner.market_data_service.refresh_many = MagicMock()
        runner.market_data_service.get_symbol_snapshot = MagicMock(return_value=None)
        runner.market_context_logger = MagicMock()
        runner.scan_logger = MagicMock()
        runner.candidate_logger = MagicMock()
        runner.trade_plan_logger = MagicMock()
        runner.strategy_performance_logger = MagicMock()
        runner.forward_paper = MagicMock()
        runner.strategy.detect = MagicMock(return_value=None)
        runner.momentum_strategy.detect = MagicMock(return_value=candidate)
        runner.momentum_breakdown_strategy.detect = MagicMock(return_value=None)
        runner.scorer.score = MagicMock(return_value=score)
        runner.risk_manager.evaluate = MagicMock(return_value=risk)
        runner.risk_manager.day_mode = MagicMock(return_value={
            "mode": "NORMAL", "daily_realized_pnl": 0.0, "daily_loss_pct": 0.0,
            "consecutive_losses": 0, "weekly_realized_pnl": 0.0,
            "weekly_loss_pct": 0.0, "account_equity": 1000.0,
            "equity_source": "test",
        })
        runner.trade_planner.build = MagicMock(return_value=plan)
        runner._cooldown_is_on = MagicMock(return_value=False)
        runner.cooldown_manager.as_log_payload = MagicMock(return_value=None)
        runner._duplicate_continuation_block = MagicMock(return_value=None)
        runner._emit_summary = MagicMock()
        runner._emit_candidate_summary = MagicMock()
        runner._emit_plan_summary = MagicMock()
        with (
            patch("app.runner.get_watchlist", return_value=["BTCUSDT"]),
            patch("app.runner.run_coach_rules", return_value={"decision_count": 0}),
            patch("app.runner.detect_continuation", return_value=None),
            patch("app.runner.detect_low_vol_reclaim", return_value=None),
            patch("app.runner.select_best_candidate", return_value=(candidate, "selected")),
            patch("app.runner.runtime_heartbeat"),
        ):
            runner._scan_cycle()
        submitted = runner.forward_paper.process.call_args.args[0]
        return [(item.strategy, item.direction, item.verdict) for item in submitted]

    healthy = MagicMock()
    healthy.event.return_value = True
    failing_store = MagicMock()
    failing_store.append.side_effect = RuntimeError("telemetry unavailable")
    failing = FunnelTelemetry(settings, store=failing_store)
    assert run_with(healthy) == run_with(failing) == [
        ("momentum_breakout", "LONG", "EXECUTABLE")
    ]
