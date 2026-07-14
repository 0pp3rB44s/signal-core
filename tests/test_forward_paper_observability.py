from __future__ import annotations

import csv
import json
import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from unittest.mock import MagicMock

from app.config import Settings
from clients.schemas import TradePlan
from forward_paper.service import ForwardPaperService
from forward_paper.store import (
    ForwardPaperCorruptionError,
    ForwardPaperEventStore,
    ForwardPaperReconstructor,
    content_hash,
)
from execution.execution_service import ExecutionService


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        FORWARD_PAPER_ENABLED=True,
        FORWARD_PAPER_ROUNDTRIP_FEE_BPS=12.0,
        EXECUTION_ENABLED=False,
        MAX_OPEN_POSITIONS=2,
        TP1_CLOSE_PCT=40.0,
        TP2_CLOSE_PCT=30.0,
        TP3_CLOSE_PCT=30.0,
    )


def _plan() -> TradePlan:
    return TradePlan(
        symbol="BTCUSDT", strategy="momentum_breakout", direction="LONG",
        verdict="EXECUTABLE", score=82.0, entry_prices=[100.0], stop_loss=99.0,
        take_profits=[101.0, 102.0, 103.0], risk_reward_ratio=3.0,
        account_risk_pct=0.75, leverage=5.0, position_notional_usdt=100.0,
        notes=["spread_bps=2.0", "pressure_score=70"], reasons=["test"],
    )


def _snapshot(timestamp_ms: int, *, close: float, high: float, low: float):
    candle = SimpleNamespace(timestamp_ms=timestamp_ms, high=high, low=low, close=close)
    primary = SimpleNamespace(
        candles=[candle], latest_close=close, granularity="15m", trend="bullish",
        volume_ratio_20=1.8,
    )
    confirmation = SimpleNamespace(trend="bullish", granularity="1H")
    return SimpleNamespace(
        symbol="BTCUSDT", primary=primary, confirmation=confirmation,
        alignment="aligned_bullish", volatility_rank=32.0, score_hint=80.0,
        notes=["spread_bps=2.0"], context={"regime": "bullish"},
    )


def _service(tmp_path: Path) -> ForwardPaperService:
    return ForwardPaperService(
        _settings(), events_path=tmp_path / "events.jsonl",
        outcomes_path=tmp_path / "outcomes.csv", quality_path=tmp_path / "quality.json",
        git_commit="test-commit",
    )


def _append_worker(path: str, index: int) -> None:
    store = ForwardPaperEventStore(path)
    store.append({
        "event_id": f"event-{index}", "trade_id": f"trade-{index}",
        "plan_id": f"plan-{index}", "event_type": "PAPER_REJECTED",
        "timestamp": f"2026-07-14T00:00:{index:02d}+00:00", "payload": {"reason": "test"},
    })


def _crash_after_append(path: str) -> None:
    _append_worker(path, 1)
    os._exit(17)


def test_complete_trade_lifecycle_fee_r_and_mfe_mae_timing(tmp_path):
    service = _service(tmp_path)
    plan = _plan()
    service.process([plan], [_snapshot(1_000, close=100.0, high=100.0, low=100.0)])
    service.process([], [_snapshot(2_000, close=100.6, high=100.7, low=99.5)])
    service.process([], [_snapshot(3_000, close=101.0, high=101.1, low=100.5)])
    service.process([], [_snapshot(4_000, close=102.0, high=102.1, low=100.5)])
    service.process([], [_snapshot(5_000, close=103.0, high=103.1, low=100.5)])

    outcomes = list(csv.DictReader((tmp_path / "outcomes.csv").open()))
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome["dataset"] == "forward_paper"
    assert outcome["final_exit_reason"] == "TP3"
    assert float(outcome["gross_pnl"]) == pytest.approx(1.9)
    assert float(outcome["fees"]) == pytest.approx(0.12114)
    assert float(outcome["net_pnl"]) == pytest.approx(1.77886)
    assert float(outcome["result_r"]) == pytest.approx(1.77886)
    assert outcome["mfe_timestamp"] == "1970-01-01T00:00:05+00:00"
    assert outcome["mae_timestamp"] == "1970-01-01T00:00:02+00:00"
    assert outcome["break_even_activated"] == "True"
    assert outcome["profit_lock_activated"] == "True"
    assert int(outcome["partial_exit_count"]) == 3


def test_duplicate_event_is_idempotent(tmp_path):
    store = ForwardPaperEventStore(tmp_path / "events.jsonl")
    event = {
        "event_id": "same", "trade_id": "trade", "plan_id": "plan",
        "event_type": "PAPER_REJECTED", "timestamp": "2026-07-14T00:00:00+00:00",
        "payload": {"reason": "missing"},
    }
    assert store.append(event) is True
    assert store.append(event) is False
    assert len(store.read_events()) == 1


def test_process_exit_and_resume_preserves_chain(tmp_path):
    path = str(tmp_path / "events.jsonl")
    process = multiprocessing.Process(target=_crash_after_append, args=(path,))
    process.start()
    process.join(timeout=5)
    assert process.exitcode == 17
    _append_worker(path, 2)
    events = ForwardPaperEventStore(path).read_events()
    assert [event["sequence"] for event in events] == [1, 2]


def test_missing_market_data_fails_closed_without_outcome(tmp_path):
    service = _service(tmp_path)
    service.process([_plan()], [])
    events = service.store.read_events()
    assert [event["event_type"] for event in events] == ["PAPER_REJECTED"]
    assert list(csv.DictReader((tmp_path / "outcomes.csv").open())) == []
    quality = json.loads((tmp_path / "quality.json").read_text())
    assert quality["complete_outcomes"] == 0
    assert len(quality["incomplete_trades"]) == 1


def test_concurrent_writers_keep_contiguous_valid_chain(tmp_path):
    path = str(tmp_path / "events.jsonl")
    processes = [multiprocessing.Process(target=_append_worker, args=(path, index)) for index in range(12)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    events = ForwardPaperEventStore(path).read_events()
    assert len(events) == 12
    assert [event["sequence"] for event in events] == list(range(1, 13))


def test_corruption_is_detected_and_future_append_fails_closed(tmp_path):
    path = tmp_path / "events.jsonl"
    store = ForwardPaperEventStore(path)
    _append_worker(str(path), 1)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("partial")
    with pytest.raises(ForwardPaperCorruptionError):
        store.read_events()
    with pytest.raises(ForwardPaperCorruptionError):
        _append_worker(str(path), 2)


def test_reconstruction_is_reproducible_and_never_mixes_datasets(tmp_path):
    service = _service(tmp_path)
    service.process([_plan()], [_snapshot(1_000, close=100.0, high=100.0, low=100.0)])
    service.process([], [_snapshot(2_000, close=98.8, high=100.1, low=98.8)])
    reconstructor = ForwardPaperReconstructor(
        service.store, tmp_path / "outcomes.csv", tmp_path / "quality.json"
    )
    first_outcomes, first_quality = reconstructor.reconstruct()
    second_outcomes, second_quality = reconstructor.reconstruct()
    assert first_outcomes == second_outcomes
    assert first_quality["outcome_dataset_hash"] == second_quality["outcome_dataset_hash"]
    assert all(row["dataset"] == "forward_paper" for row in first_outcomes)
    with pytest.raises(ValueError, match="non-paper"):
        service.store.append({
            "dataset": "live", "event_id": "x", "trade_id": "x", "plan_id": "x",
            "event_type": "TRADE_OPENED", "timestamp": "2026-01-01T00:00:00+00:00", "payload": {},
        })


def test_forward_paper_has_no_exchange_client_and_execution_stays_disabled(tmp_path):
    settings = _settings()
    service = ForwardPaperService(
        settings, events_path=tmp_path / "events.jsonl",
        outcomes_path=tmp_path / "outcomes.csv", quality_path=tmp_path / "quality.json",
        git_commit="test",
    )
    assert settings.execution_enabled is False
    assert settings.forward_paper_enabled is True
    assert not hasattr(service, "client")

    execution = ExecutionService(settings)
    execution.client = MagicMock()
    assert execution.execute([_plan()]) == []
    execution.client.get_all_positions.assert_not_called()
    execution.client.place_futures_order.assert_not_called()


def test_config_identity_never_serializes_secrets(tmp_path):
    secrets = {
        "BITGET_API_KEY": "private-api-key-value",
        "BITGET_API_SECRET": "private-api-secret-value",
        "BITGET_API_PASSPHRASE": "private-passphrase-value",
    }
    settings = Settings(
        _env_file=None,
        FORWARD_PAPER_ENABLED=True,
        EXECUTION_ENABLED=False,
        **secrets,
    )
    service = ForwardPaperService(
        settings,
        events_path=tmp_path / "events.jsonl",
        outcomes_path=tmp_path / "outcomes.csv",
        quality_path=tmp_path / "quality.json",
        git_commit="test",
    )
    service.process([_plan()], [_snapshot(1_000, close=100.0, high=100.0, low=100.0)])

    serialized = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert all(value not in serialized for value in secrets.values())
