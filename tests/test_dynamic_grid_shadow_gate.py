from datetime import datetime, timedelta, timezone
import json

from scripts.evaluate_dynamic_grid_shadow import evaluate


def _write_gate_log(path, *, cycles=48, include_error=False):
    start = datetime(2026, 8, 10, tzinfo=timezone.utc)
    rows = []
    for symbol in ("BTCUSDT", "SOLUSDT"):
        rows.append({
            "timestamp": start.isoformat(), "event_type": "FEE_RATE_AUTHENTICATED",
            "strategy": "dynamic_grid_v1", "mode": "SHADOW", "symbol": symbol,
        })
    for index in range(cycles):
        timestamp = start + timedelta(minutes=5 * index + 1)
        rows.append({
            "timestamp": timestamp.isoformat(), "event_type": "GRID_SELECTION",
            "strategy": "dynamic_grid_v1", "mode": "SHADOW",
        })
        for symbol in ("BTCUSDT", "SOLUSDT"):
            rows.append({
                "timestamp": timestamp.isoformat(), "event_type": "GRID_DECISION",
                "strategy": "dynamic_grid_v1", "mode": "SHADOW", "symbol": symbol,
                "regime": "GRID_ALLOWED", "levels": [{}, {}, {}],
                "economics": {"expected_net_capture_bps": 1.0},
            })
    if include_error:
        rows.append({
            "timestamp": start.isoformat(), "event_type": "GRID_STOP",
            "strategy": "dynamic_grid_v1", "mode": "SHADOW",
        })
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def test_shadow_gate_passes_only_complete_multi_hour_evidence(tmp_path):
    path = tmp_path / "events.jsonl"
    _write_gate_log(path, cycles=50)
    assert evaluate(path)["verdict"] == "PASS"


def test_shadow_gate_fails_on_runtime_stop(tmp_path):
    path = tmp_path / "events.jsonl"
    _write_gate_log(path, cycles=50, include_error=True)
    result = evaluate(path)
    assert result["verdict"] == "FAIL"
    assert not result["checks"]["no_runtime_errors"]


def test_shadow_gate_rejects_malformed_hypothetical_fill_mapping(tmp_path):
    path = tmp_path / "events.jsonl"
    _write_gate_log(path, cycles=50)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "timestamp": "2026-08-10T05:00:00+00:00",
            "event_type": "SHADOW_LEVEL_FILLED",
            "strategy": "dynamic_grid_v1", "mode": "SHADOW",
            "symbol": "BTCUSDT", "level": 1,
            "entry_price": 100.0, "target_price": 99.0,
            "expected_net_capture_bps": -1.0,
        }) + "\n")
    result = evaluate(path)
    assert result["verdict"] == "FAIL"
    assert not result["checks"]["hypothetical_fill_mapping_well_formed"]
