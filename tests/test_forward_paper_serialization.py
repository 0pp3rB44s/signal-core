"""Regression tests for the forward-paper serialization outage (2026-07-19 → 2026-07-25).

Root cause: market_features.engine placed a ContractSpec dataclass into
MarketSnapshot.context; forward_paper.service copies that context verbatim into
the event payload; forward_paper.store.canonical_json then raised
"Object of type ContractSpec is not JSON serializable". app.runner caught the
error, logged FORWARD_PAPER_FAILED_CLOSED and continued, so every executable
plan was silently dropped for six days.

These tests lock both halves of the fix:
  1. the producer emits only JSON-safe values, and
  2. the writer cannot be killed by any object a future producer introduces.
"""
from __future__ import annotations

import json

import pytest

from clients.schemas import Candle, ContractSpec
from forward_paper.store import (
    UNSERIALIZABLE_PREFIX,
    canonical_json,
    content_hash,
)
from market_features.engine import (
    LiveMarketContext,
    build_market_snapshot,
    instrument_context,
)

BASE_TS = 1784419200000


def _contract() -> ContractSpec:
    return ContractSpec(
        "BTCUSDT", "USDT-FUTURES", "USDT", "BTC", "normal",
        0.001, 0.001, 2, 1_000_000_000.0, 2.0, {"unbounded": "exchange blob"},
    )


def _production_snapshot():
    """Build a snapshot through the real production factory, not a fixture."""
    primary = [
        Candle(BASE_TS + i * 900_000, 100 + i * 0.01, 100.5 + i * 0.01,
               99.5 + i * 0.01, 100.2 + i * 0.01, 100.0)
        for i in range(240)
    ]
    confirmation = [
        Candle(BASE_TS + i * 3_600_000, 100 + i * 0.04, 100.5 + i * 0.04,
               99.5 + i * 0.04, 100.2 + i * 0.04, 100.0)
        for i in range(60)
    ]
    return build_market_snapshot(
        "BTCUSDT", primary, confirmation,
        as_of_timestamp_ms=BASE_TS + 60 * 3_600_000,
        primary_granularity="15m", confirmation_granularity="1h",
        inputs=LiveMarketContext(orderbook=None, htf_context=None, contract=_contract()),
    )


def test_production_snapshot_context_is_json_serializable():
    """The exact failure: a real snapshot's context must survive the paper writer."""
    snapshot = _production_snapshot()
    payload = {"strategy_features": {"market_context": dict(snapshot.context)}}

    encoded = canonical_json(payload)

    assert UNSERIALIZABLE_PREFIX not in encoded, (
        "snapshot context still contains a value that needed repr() coercion"
    )
    json.loads(encoded)


def test_instrument_context_is_plain_json_types():
    """context['instrument'] carries metadata, never the ContractSpec object."""
    snapshot = _production_snapshot()
    instrument = snapshot.context["instrument"]

    assert isinstance(instrument, dict)
    assert instrument["symbol"] == "BTCUSDT"
    assert instrument["volume_24h_usdt"] == 1_000_000_000.0
    assert "raw" not in instrument, "unbounded exchange blob must not be persisted"
    # The typed object stays reachable where it belongs.
    assert isinstance(snapshot.contract, ContractSpec)


def test_instrument_context_handles_missing_contract():
    assert instrument_context(None) is None


def test_canonical_json_never_raises_on_unknown_object():
    """Defence in depth: no future producer may kill a paper write."""
    class Exotic:
        pass

    encoded = canonical_json({"context": {"weird": Exotic()}})

    assert f"{UNSERIALIZABLE_PREFIX}Exotic>" in encoded
    json.loads(encoded)


@pytest.mark.parametrize("payload", [
    {"b": 1, "a": 2},
    {"nested": {"z": [1, 2, 3], "y": "text"}},
    {"unicode": "ünïcødé", "float": 1.5, "null": None, "bool": True},
])
def test_canonical_json_output_unchanged_for_serializable_payloads(payload):
    """The default= hook must not alter existing hashes or dedupe behaviour."""
    expected = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    assert canonical_json(payload) == expected
    assert content_hash(payload) == content_hash(payload)


def test_canonical_json_coerces_dataclass_to_dict():
    """A dataclass that slips through is preserved as data, not lost."""
    encoded = canonical_json({"contract": _contract()})
    decoded = json.loads(encoded)

    assert decoded["contract"]["symbol"] == "BTCUSDT"
    assert UNSERIALIZABLE_PREFIX not in encoded


def test_open_trade_writes_event_with_production_snapshot(tmp_path):
    """Integration: the exact path that wrote nothing for six days.

    Drives ForwardPaperService.open_trade with a snapshot from the production
    factory (so context carries real instrument metadata) and asserts a
    TRADE_OPENED event actually lands on disk.
    """
    from app.config import get_settings
    from clients.schemas import TradePlan
    from candidate_lifecycle.identity import deterministic_candidate_id, deterministic_plan_id
    from forward_paper.service import ForwardPaperService

    snapshot = _production_snapshot()
    settings = get_settings()

    candidate_id = deterministic_candidate_id(
        "momentum_breakout", "BTCUSDT", "LONG", BASE_TS,
    )
    fill = snapshot.primary.latest_close
    plan = TradePlan(
        candidate_id=candidate_id,
        candidate_candle_open_timestamp_ms=BASE_TS,
        plan_id=deterministic_plan_id(candidate_id),
        symbol="BTCUSDT",
        strategy="momentum_breakout",
        direction="LONG",
        verdict="EXECUTABLE",
        score=100.0,
        entry_prices=[fill],
        stop_loss=fill * 0.99,
        take_profits=[fill * 1.02, fill * 1.03],
        risk_reward_ratio=2.0,
        account_risk_pct=0.75,
        leverage=5.0,
        position_notional_usdt=50.0,
        notes=["spread_bps=2"],
        reasons=[],
    )

    service = ForwardPaperService(
        settings,
        events_path=tmp_path / "paper.jsonl",
        outcomes_path=tmp_path / "outcomes.csv",
        quality_path=tmp_path / "quality.json",
        git_commit="test",
    )

    trade_id = service.open_trade(plan, snapshot)

    assert trade_id, "open_trade returned no trade id — the write failed closed"
    events = service.store.read_events()
    assert [event["event_type"] for event in events].count("TRADE_OPENED") == 1

    opened = next(event for event in events if event["event_type"] == "TRADE_OPENED")
    instrument = opened["payload"]["strategy_features"]["market_context"]["instrument"]
    assert instrument["symbol"] == "BTCUSDT"
    assert UNSERIALIZABLE_PREFIX not in json.dumps(opened)
