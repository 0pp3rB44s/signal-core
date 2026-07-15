from __future__ import annotations

import csv
import json

import pytest

from forward_paper.service import ForwardPaperService
from forward_paper.store import (
    ForwardPaperEventStore,
    ForwardPaperReconstructor,
    ForwardPaperSemanticConflictError,
    canonical_json,
    content_hash,
    semantic_transition_key,
)
from tests.test_candidate_lifecycle import TS, _candidate, _plan, _settings, _snapshot


class InjectedCrash(RuntimeError):
    pass


def _service(tmp_path) -> ForwardPaperService:
    return ForwardPaperService(
        _settings(), events_path=tmp_path / "paper.jsonl",
        outcomes_path=tmp_path / "outcomes.csv", quality_path=tmp_path / "quality.json",
        git_commit="test",
    )


@pytest.mark.parametrize(
    ("event_type", "crash_before"),
    [
        ("TP_TOUCH", False),
        ("PARTIAL_EXIT", False),
        ("EXIT_REASON_TRANSITION", False),
        ("TRADE_CLOSED", True),
        ("TRADE_CLOSED", False),
    ],
    ids=("after-touch", "after-partial", "after-exit-transition", "before-close", "after-close"),
)
def test_public_process_resumes_every_terminal_crash_window(
    tmp_path, monkeypatch, event_type, crash_before,
):
    candidate = _candidate()
    plan = _plan(candidate)
    service = _service(tmp_path)
    service.process([plan], [_snapshot(TS + 900_000, 100.0, 100.0, 100.0)])

    original_append = service.store.append
    crashed = False

    def faulting_append(event):
        nonlocal crashed
        if not crashed and event["event_type"] == event_type and crash_before:
            crashed = True
            raise InjectedCrash(event_type)
        result = original_append(event)
        if not crashed and event["event_type"] == event_type:
            crashed = True
            raise InjectedCrash(event_type)
        return result

    monkeypatch.setattr(service.store, "append", faulting_append)
    with pytest.raises(InjectedCrash):
        service.process([], [_snapshot(TS + 1_800_000, 101.0, 101.1, 99.5)])

    restarted = _service(tmp_path)
    restarted.process([], [_snapshot(TS + 2_700_000, 100.5, 100.8, 99.5)])
    restarted.process([], [_snapshot(TS + 3_600_000, 100.5, 100.8, 99.5)])

    events = restarted.store.read_events()
    event_types = [event["event_type"] for event in events]
    outcomes = list(csv.DictReader((tmp_path / "outcomes.csv").open(encoding="utf-8")))
    _, quality = restarted.reconstructor.reconstruct()
    assert event_types.count("TRADE_OPENED") == 1
    assert event_types.count("TP_TOUCH") == 1
    assert event_types.count("PARTIAL_EXIT") == 1
    assert event_types.count("EXIT_REASON_TRANSITION") == 1
    assert event_types.count("TRADE_CLOSED") == 1
    assert len(outcomes) == 1
    assert len(restarted.open_states()) == 0
    assert quality["fragmented_transition_count"] == 0
    assert quality["unresolved_open_trade_count"] == 0
    assert quality["duplicate_semantic_transition_count"] == 0
    assert quality["terminal_close_conflict_count"] == 0


def test_semantic_retry_is_idempotent_and_conflict_fails_closed(tmp_path):
    store = ForwardPaperEventStore(tmp_path / "events.jsonl")
    payload = {"exit_price": 101.0, "exit_size": 1.0, "exit_reason": "TP1"}
    base = {
        "event_id": "close-a", "semantic_key": semantic_transition_key("trade", "TRADE_CLOSED", payload),
        "candidate_id": "candidate", "trade_id": "trade", "plan_id": "plan",
        "event_type": "TRADE_CLOSED", "timestamp": "2026-01-01T00:00:00Z", "payload": payload,
    }
    assert store.append(base)
    retry = {**base, "event_id": "close-b", "timestamp": "2026-01-01T00:01:00Z"}
    assert store.append(retry) is False
    conflict = {**retry, "event_id": "close-c", "payload": {**payload, "exit_price": 102.0}}
    with pytest.raises(ForwardPaperSemanticConflictError):
        store.append(conflict)
    assert len(store.read_events()) == 1


def test_terminal_conflict_fails_closed_and_is_reported(tmp_path):
    path = tmp_path / "events.jsonl"
    store = ForwardPaperEventStore(path)
    payload = {"exit_price": 101.0, "exit_size": 1.0, "exit_reason": "TP1"}
    assert store.append({
        "event_id": "close-a", "semantic_key": "trade:terminal_close",
        "candidate_id": "candidate", "trade_id": "trade", "plan_id": "plan",
        "event_type": "TRADE_CLOSED", "timestamp": "2026-01-01T00:00:00Z", "payload": payload,
    })
    first = json.loads(path.read_text(encoding="utf-8"))
    conflict = {
        **{key: value for key, value in first.items() if key != "event_hash"},
        "sequence": 2, "event_id": "close-b", "semantic_key": "trade:terminal_close:conflict",
        "timestamp": "2026-01-01T00:01:00Z", "previous_hash": first["event_hash"],
        "payload": {**payload, "exit_price": 102.0},
    }
    conflict["event_hash"] = content_hash(conflict)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(canonical_json(conflict) + "\n")
    quality_path = tmp_path / "quality.json"
    reconstructor = ForwardPaperReconstructor(store, tmp_path / "outcomes.csv", quality_path)
    with pytest.raises(ForwardPaperSemanticConflictError):
        reconstructor.reconstruct()
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    assert quality["terminal_close_conflict_count"] == 1
    assert quality["duplicate_semantic_transition_count"] == 1
    assert quality["event_chain_valid"] is False
