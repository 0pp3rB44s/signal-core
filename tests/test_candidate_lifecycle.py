from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from candidate_lifecycle import canonical_timestamp_utc, deterministic_candidate_id, deterministic_plan_id, lifecycle_key
from candidate_lifecycle.reconstruct import reconstruct_candidate_lifecycles
from clients.schemas import TradePlan
from forward_paper.service import ForwardPaperService
from forward_paper.store import ForwardPaperCorruptionError, ForwardPaperEventStore, canonical_json, content_hash
from telemetry.funnel import FunnelEventStore, FunnelTelemetry, FunnelTelemetryCorruptionError, canonical_json as funnel_json, content_hash as funnel_hash


TS = 1_767_225_600_000


def _candidate(timestamp: int = TS, direction: str = "LONG", strategy: str = "momentum_breakout"):
    return SimpleNamespace(
        candidate_id=deterministic_candidate_id(strategy, "BTCUSDT", direction, timestamp),
        candidate_candle_open_timestamp_ms=timestamp,
        strategy=strategy, symbol="BTCUSDT", direction=direction,
    )


def _plan(candidate=None):
    candidate = candidate or _candidate()
    return TradePlan(
        candidate_id=candidate.candidate_id,
        candidate_candle_open_timestamp_ms=candidate.candidate_candle_open_timestamp_ms,
        plan_id=deterministic_plan_id(candidate.candidate_id),
        symbol=candidate.symbol, strategy=candidate.strategy, direction=candidate.direction,
        verdict="EXECUTABLE", score=82.0, entry_prices=[100.0], stop_loss=99.0,
        take_profits=[101.0], risk_reward_ratio=1.0, account_risk_pct=.5,
        leverage=2.0, position_notional_usdt=100.0, notes=["spread_bps=2"], reasons=[],
    )


def _snapshot(timestamp: int, close: float, high: float, low: float):
    candle = SimpleNamespace(timestamp_ms=timestamp, close=close, high=high, low=low)
    primary = SimpleNamespace(candles=[candle], latest_close=close, granularity="15m", trend="bullish", volume_ratio_20=1.5)
    confirmation = SimpleNamespace(granularity="1h", trend="bullish")
    return SimpleNamespace(symbol="BTCUSDT", primary=primary, confirmation=confirmation, alignment="aligned_bullish", volatility_rank=30, score_hint=80, notes=["spread_bps=2"], context={})


def _settings():
    return Settings(_env_file=None, FORWARD_PAPER_ENABLED=True, EXECUTION_ENABLED=False, MAX_OPEN_POSITIONS=2, TP1_CLOSE_PCT=100, TP2_CLOSE_PCT=0, TP3_CLOSE_PCT=0)


def test_candidate_identity_is_canonical_utc_sha256_and_scan_independent():
    expected = deterministic_candidate_id("momentum_breakout", "BTCUSDT", "LONG", TS)
    assert len(expected) == 64
    assert expected == deterministic_candidate_id(" Momentum_Breakout ", "btcusdt", "long", str(TS))
    assert expected == deterministic_candidate_id("momentum_breakout", "BTCUSDT", "LONG", "2026-01-01T00:00:00+00:00")
    assert expected == deterministic_candidate_id("momentum_breakout", "BTCUSDT", "LONG", datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert canonical_timestamp_utc(TS) == "2026-01-01T00:00:00.000Z"
    with pytest.raises(ValueError):
        canonical_timestamp_utc(datetime(2026, 1, 1))


def test_identity_changes_for_every_identity_dimension_and_new_candle():
    base = deterministic_candidate_id("momentum_breakout", "BTCUSDT", "LONG", TS)
    variants = {
        deterministic_candidate_id("momentum_breakdown", "BTCUSDT", "LONG", TS),
        deterministic_candidate_id("momentum_breakout", "ETHUSDT", "LONG", TS),
        deterministic_candidate_id("momentum_breakout", "BTCUSDT", "SHORT", TS),
        deterministic_candidate_id("momentum_breakout", "BTCUSDT", "LONG", TS + 900_000),
    }
    assert base not in variants and len(variants) == 4


def test_funnel_lifecycle_key_is_persistent_and_scan_independent(tmp_path):
    candidate = _candidate()
    path = tmp_path / "funnel.jsonl"
    first = FunnelTelemetry(FunnelEventStore(path))
    assert first.record(candidate, "DETECTOR_DECISION", scan_id="scan-a", passed=True, reason="DETECTED")
    restarted = FunnelTelemetry(FunnelEventStore(path))
    assert restarted.record(candidate, "DETECTOR_DECISION", scan_id="scan-b", passed=True, reason="DETECTED") is False
    assert restarted.record(candidate, "SELECTOR_DECISION", scan_id="scan-b", passed=True, reason="SELECTED")
    events = restarted.store.read_events()
    assert [event["lifecycle_key"] for event in events] == [
        lifecycle_key(candidate.candidate_id, "DETECTOR_DECISION"),
        lifecycle_key(candidate.candidate_id, "SELECTOR_DECISION"),
    ]


def test_funnel_missing_identity_and_corruption_fail_closed(tmp_path):
    path = tmp_path / "funnel.jsonl"
    telemetry = FunnelTelemetry(FunnelEventStore(path))
    with pytest.raises(ValueError, match="candidate_id"):
        telemetry.record(SimpleNamespace(candidate_id=""), "DETECTOR_DECISION", scan_id="x", passed=True, reason="x")
    telemetry.record(_candidate(), "DETECTOR_DECISION", scan_id="x", passed=True, reason="x")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{broken\n")
    assert telemetry.record(_candidate(TS + 900_000), "DETECTOR_DECISION", scan_id="y", passed=True, reason="x") is False
    with pytest.raises(FunnelTelemetryCorruptionError):
        telemetry.store.read_events()


def test_forward_paper_restart_never_opens_same_candidate_twice(tmp_path):
    candidate = _candidate(); plan = _plan(candidate); snapshot = _snapshot(TS + 900_000, 100, 100, 100)
    kwargs = dict(events_path=tmp_path / "paper.jsonl", outcomes_path=tmp_path / "outcomes.csv", quality_path=tmp_path / "quality.json", git_commit="test")
    first = ForwardPaperService(_settings(), **kwargs)
    first.process([plan], [snapshot])
    restarted = ForwardPaperService(_settings(), **kwargs)
    restarted.process([plan], [snapshot])
    opened = [event for event in restarted.store.read_events() if event["event_type"] == "TRADE_OPENED"]
    assert len(opened) == 1 and opened[0]["candidate_id"] == candidate.candidate_id


def test_detector_to_funnel_plan_paper_outcome_reconstruction_is_deterministic(tmp_path):
    candidate = _candidate(); plan = _plan(candidate)
    funnel_path = tmp_path / "funnel.jsonl"; paper_path = tmp_path / "paper.jsonl"; outcomes_path = tmp_path / "outcomes.csv"
    telemetry = FunnelTelemetry(FunnelEventStore(funnel_path))
    for event_type in ("DETECTOR_DECISION", "SELECTOR_DECISION", "SCORING_DECISION", "RISK_DECISION"):
        telemetry.record(candidate, event_type, scan_id="scan", passed=True, reason="PASS")
    for event_type in ("PLANNER_DECISION", "EXECUTABLE_DECISION"):
        telemetry.record(candidate, event_type, scan_id="scan", passed=True, reason="PASS", plan_id=plan.plan_id)
    service = ForwardPaperService(_settings(), events_path=paper_path, outcomes_path=outcomes_path, quality_path=tmp_path / "quality.json", git_commit="test", funnel_telemetry=telemetry)
    service.process([plan], [_snapshot(TS + 900_000, 100, 100, 100)], scan_id="scan")
    service.process([], [_snapshot(TS + 1_800_000, 101, 101.1, 99.5)], scan_id="scan-2")
    first = reconstruct_candidate_lifecycles(funnel_path, paper_path, outcomes_path)
    second = reconstruct_candidate_lifecycles(funnel_path, paper_path, outcomes_path)
    assert first == second
    record = first["records"][0]
    assert record["candidate_id"] == candidate.candidate_id
    assert record["plan_ids"] == [plan.plan_id]
    assert len(record["trade_ids"]) == 1 and record["complete"] is True


def test_legacy_forward_record_is_never_given_a_candidate_id(tmp_path):
    path = tmp_path / "legacy.jsonl"
    stored = {
        "schema_version": 1, "dataset": "forward_paper", "sequence": 1,
        "event_id": "legacy", "trade_id": "trade", "plan_id": "plan",
        "event_type": "PAPER_REJECTED", "timestamp": "2025-01-01T00:00:00+00:00",
        "payload": {"reason": "legacy"}, "previous_hash": "GENESIS",
    }
    stored["event_hash"] = content_hash(stored)
    path.write_text(canonical_json(stored) + "\n", encoding="utf-8")
    event = ForwardPaperEventStore(path).read_events()[0]
    assert event["candidate_id"] == "" and event["identity_status"] == "LEGACY_UNLINKED"


def test_legacy_funnel_record_is_read_only_unlinked(tmp_path):
    path = tmp_path / "legacy-funnel.jsonl"
    event = {
        "schema_version": 1, "event_id": "legacy", "scan_id": "old-scan",
        "candidate_id": "old-untrusted-id", "event_type": "DETECTOR_DECISION",
        "event_timestamp_utc": "2025-01-01T00:00:00+00:00", "strategy": "momentum_breakout",
        "symbol": "BTCUSDT", "direction": "LONG", "timeframe": "15m",
        "candle_open_timestamp": "1735689600000", "signal_timestamp": "2025-01-01T00:15:00+00:00",
        "session": "EU", "regime": "bullish", "pass_fail": "PASS",
        "primary_reason_code": "DETECTED", "secondary_reason_codes": [],
        "config_hash": "config", "git_commit": "commit", "sequence": 1,
        "previous_hash": "GENESIS",
    }
    event["event_hash"] = funnel_hash(event)
    path.write_text(funnel_json(event) + "\n", encoding="utf-8")
    visible = FunnelEventStore(path).read_events()[0]
    assert visible["candidate_id"] == "" and visible["identity_status"] == "LEGACY_UNLINKED"


def test_sweep_identity_uses_reclaim_candle_contract_and_no_private_calls():
    root = Path(__file__).parents[1]
    sweep_source = (root / "strategies/liquidity_sweep.py").read_text(encoding="utf-8")
    assert sweep_source.count("closed_candle_at_offset(market.primary, bull.bars_since_sweep).timestamp_ms") == 2
    assert sweep_source.count("closed_candle_at_offset(market.primary, bear.bars_since_sweep).timestamp_ms") == 2
    lifecycle_sources = "\n".join((root / path).read_text(encoding="utf-8") for path in ("candidate_lifecycle/identity.py", "candidate_lifecycle/reconstruct.py", "telemetry/funnel.py"))
    assert "Bitget" not in lifecycle_sources and ".execute(" not in lifecycle_sources
