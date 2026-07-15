from __future__ import annotations

import csv
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.runner import StartupRunner
from candidate_lifecycle.reconstruct import reconstruct_candidate_lifecycles
from clients.schemas import Candle, ContractSpec, MarketSnapshot, TimeframeSnapshot
from forward_paper.service import ForwardPaperService
from telemetry.funnel import FunnelEventStore, FunnelTelemetry


BASE_TS = 1_700_000_000_000


def _detector_fixture() -> MarketSnapshot:
    candles = []
    for index in range(23):
        price = 98.0 + index * 0.08
        candles.append(Candle(
            BASE_TS + index * 900_000, price, price + 0.3, price - 0.3,
            price + 0.12, 100 + (index % 3) * 10,
        ))
    previous_high = max(candle.high for candle in candles[-20:])
    candles.append(Candle(
        BASE_TS + 23 * 900_000, previous_high - 0.05, previous_high + 1.0,
        previous_high - 0.1, previous_high + 0.3, 300,
    ))
    candles.append(Candle(
        BASE_TS + 24 * 900_000, previous_high + 0.15, previous_high + 0.55,
        previous_high + 0.05, previous_high + 0.4, 250,
    ))
    as_of = candles[-1].timestamp_ms + 900_000
    primary = TimeframeSnapshot(
        "BTCUSDT", "15m", candles[-1].close, 2.0, 3.0, 2.0, 100.0, 99.0,
        "bullish", candles, candles[-1].timestamp_ms, as_of,
    )
    confirmation = TimeframeSnapshot(
        "BTCUSDT", "1h", candles[-1].close, 2.0, 3.0, 2.0, 100.0, 99.0,
        "bullish", candles, candles[-1].timestamp_ms, as_of,
    )
    contract = ContractSpec(
        "BTCUSDT", "USDT-FUTURES", "USDT", "BTC", "normal", 0.001,
        0.001, 2, 1_000_000_000, 2.0, {},
    )
    notes = [
        "spread_bps=2", "pressure_score=80", "expansion_prob=90",
        "breakout_context ready=true direction=bullish", "range_tightening=true",
        "higher_lows_building=true", "closes_pressing_highs=true",
        "entry_quality_score=95", "orderbook_available=true",
    ]
    return MarketSnapshot(
        "BTCUSDT", contract, primary, confirmation, "aligned_bullish", 95.0,
        notes, 70.0, {
            "regime": "bullish", "orderbook_available": True,
            "spread_available": True, "spread_bps": 2.0,
            "pressure_score": 80.0, "expansion_prob": 90.0,
        },
    )


def _runner(tmp_path, monkeypatch) -> StartupRunner:
    monkeypatch.chdir(tmp_path)
    learning_report = Path("reports/backtests/strategy_expectancy.json")
    learning_report.parent.mkdir(parents=True, exist_ok=True)
    learning_report.write_text(json.dumps({"strategies": {}}), encoding="utf-8")
    settings = Settings(
        _env_file=None, FORWARD_PAPER_ONLY=True, FAST_LANE_ENABLED=False,
        MAX_OPEN_POSITIONS=1, TP1_CLOSE_PCT=100, TP2_CLOSE_PCT=0, TP3_CLOSE_PCT=0,
        BITGET_RATE_LIMIT_STATE_PATH=str(tmp_path / "rate-limit.json"),
    )
    runner = StartupRunner(settings)
    telemetry = FunnelTelemetry(settings, FunnelEventStore(
        tmp_path / "funnel.jsonl", tmp_path / "funnel-quality.json",
    ))
    runner.funnel_telemetry = telemetry
    runner.forward_paper = ForwardPaperService(
        settings, events_path=tmp_path / "paper.jsonl",
        outcomes_path=tmp_path / "outcomes.csv", quality_path=tmp_path / "paper-quality.json",
        git_commit="test", funnel_telemetry=telemetry,
    )
    snapshot = _detector_fixture()
    runner.fetcher.fetch_contracts = MagicMock(return_value=[])
    runner.fetcher.build_market_snapshot = MagicMock(return_value=snapshot)
    runner.market_data_service.refresh_many = MagicMock()
    runner.market_data_service.get_symbol_snapshot = MagicMock(return_value=None)
    return runner


def test_public_runner_builds_complete_real_pipeline_lineage(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    with patch("app.runner.get_watchlist", return_value=["BTCUSDT"]):
        runner.scan_once()

    events = runner.forward_paper.store.read_events()
    assert [event["event_type"] for event in events].count("TRADE_OPENED") == 1
    opened = next(event for event in events if event["event_type"] == "TRADE_OPENED")
    close_snapshot = _detector_fixture()
    last = close_snapshot.primary.candles[-1]
    last.high = float(opened["payload"]["initial_targets"][0]) + 0.1
    last.close = float(opened["payload"]["initial_targets"][0])
    runner.forward_paper.process([], [close_snapshot], scan_id="close-scan")

    funnel = runner.funnel_telemetry.store.read_events()
    paper = runner.forward_paper.store.read_events()
    outcomes = list(csv.DictReader((tmp_path / "outcomes.csv").open(encoding="utf-8")))
    report = reconstruct_candidate_lifecycles(
        tmp_path / "funnel.jsonl", tmp_path / "paper.jsonl", tmp_path / "outcomes.csv",
    )
    linked_funnel = [event for event in funnel if event["candidate_id"] == opened["candidate_id"]]
    assert {event["candidate_id"] for event in linked_funnel} == {opened["candidate_id"]}
    assert len({event["plan_id"] for event in paper}) == 1
    assert len({event["trade_id"] for event in paper}) == 1
    assert [event["event_type"] for event in paper].count("TRADE_CLOSED") == 1
    assert len(outcomes) == 1
    linked_record = next(record for record in report["records"] if record["candidate_id"] == opened["candidate_id"])
    assert linked_record["complete"] is True
    assert linked_record["funnel_stages"] == [
        "DETECTOR_ATTEMPT", "DETECTOR_DECISION", "SELECTOR_DECISION", "SCORING_DECISION",
        "RISK_DECISION", "PLANNER_DECISION", "EXECUTABLE_DECISION",
        "FORWARD_PAPER_LINK", "OUTCOME_LINK",
    ]


def test_public_runner_restart_resumes_transition_without_duplicate_lineage(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    with patch("app.runner.get_watchlist", return_value=["BTCUSDT"]):
        runner.scan_once()
    opened = next(
        event for event in runner.forward_paper.store.read_events()
        if event["event_type"] == "TRADE_OPENED"
    )
    close_snapshot = _detector_fixture()
    close_snapshot.primary.candles[-1].high = float(opened["payload"]["initial_targets"][0]) + 0.1
    close_snapshot.primary.candles[-1].close = float(opened["payload"]["initial_targets"][0])
    original_append = runner.forward_paper.store.append

    def crash_after_partial(event):
        result = original_append(event)
        if event["event_type"] == "PARTIAL_EXIT":
            raise RuntimeError("injected process crash")
        return result

    monkeypatch.setattr(runner.forward_paper.store, "append", crash_after_partial)
    try:
        runner.forward_paper.process([], [close_snapshot], scan_id="crash-scan")
    except RuntimeError as exc:
        assert str(exc) == "injected process crash"
    else:
        raise AssertionError("fault injection did not fire")

    restarted = _runner(tmp_path, monkeypatch)
    with patch("app.runner.get_watchlist", return_value=["BTCUSDT"]):
        restarted.scan_once()

    paper = restarted.forward_paper.store.read_events()
    funnel = restarted.funnel_telemetry.store.read_events()
    outcomes = list(csv.DictReader((tmp_path / "outcomes.csv").open(encoding="utf-8")))
    assert [event["event_type"] for event in paper].count("TRADE_OPENED") == 1
    assert [event["event_type"] for event in paper].count("PARTIAL_EXIT") == 1
    assert [event["event_type"] for event in paper].count("TRADE_CLOSED") == 1
    assert len(outcomes) == 1
    assert len(restarted.forward_paper.open_states()) == 0
    lifecycle_keys = [event["lifecycle_key"] for event in funnel]
    assert len(lifecycle_keys) == len(set(lifecycle_keys))
    first = reconstruct_candidate_lifecycles(
        tmp_path / "funnel.jsonl", tmp_path / "paper.jsonl", tmp_path / "outcomes.csv",
    )
    second = reconstruct_candidate_lifecycles(
        tmp_path / "funnel.jsonl", tmp_path / "paper.jsonl", tmp_path / "outcomes.csv",
    )
    linked = next(record for record in first["records"] if record["candidate_id"] == opened["candidate_id"])
    assert first == second and linked["complete"] is True
