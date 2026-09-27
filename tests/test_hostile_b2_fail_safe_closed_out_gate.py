"""Hostile: CLOSED_OUT may only follow a proven-flat exchange.

`_verify_no_live_position_after_fail_safe` used to return None in all four of
its branches — position still open, position gone, malformed payload, transport
exception — and expressed the difference only as a log line. The caller then ran
`mark_closed_out` under the single guard `if entry_client_oid`. So a fail-safe
close that did not actually close moved the intent to STATE_ABANDONED anyway.

That state is terminal: it leaves `RECOVERABLE_STATES`, so
`recover_pending_intents` can never revisit it after a restart, and `_prune`
may drop the record entirely. The result is a live, unprotected position with
no intent left to find it by.

These tests drive the real verification and the real close-out gate against a
real `OrderIntentStore`, and assert on the persisted intent state — not on logs.
"""

from __future__ import annotations

import logging

import pytest

from execution.entry_submitter import EntryOrderSubmitter
from execution.execution_service import ExecutionService, FailSafeFlatness
from execution.order_intent_store import (
    RECOVERABLE_STATES,
    STATE_ABANDONED,
    STATE_FILLED,
    TERMINAL_STATES,
    OrderIntentStore,
)

SYMBOL = "BTCUSDT"
CLIENT_OID = "coid-b2-1"


def live_position(size: str = "0.001") -> dict:
    return {"symbol": SYMBOL, "holdSide": "long", "total": size,
            "available": size, "openPriceAvg": "62900.0", "positionId": "P1"}


class Submitter:
    """The real mark_closed_out over a real intent store."""

    mark_closed_out = EntryOrderSubmitter.mark_closed_out

    def __init__(self, store):
        self.intents = store


class Service:
    """The real verification and the real close-out gate."""

    _verify_no_live_position_after_fail_safe = (
        ExecutionService._verify_no_live_position_after_fail_safe
    )
    _close_out_entry_intent_if_flat = ExecutionService._close_out_entry_intent_if_flat

    def __init__(self, store, responder):
        self.log = logging.getLogger("svc")
        self.entry_submitter = Submitter(store)
        self.client = type("C", (), {"get_all_positions": lambda _s, *a, **k: responder()})()


@pytest.fixture
def store(tmp_path):
    store = OrderIntentStore(path=str(tmp_path / "order_intents.json"))
    store.prepare(
        client_oid=CLIENT_OID, plan_id="pl1", candidate_id="c1", symbol=SYMBOL,
        side="buy", direction="LONG", size=0.001, order_type="market",
        leg="MARKET", strategy="test", session_id="s1", execution_mode="LIVE",
        notional_usdt=63.0,
    )
    # the position exists and is filled: this is the state a fail-safe close
    # runs from, and the state that keeps the intent recoverable
    store.mark(CLIENT_OID, STATE_FILLED, note="exchange position confirmed")
    return store


def run(store, responder) -> tuple[FailSafeFlatness, dict]:
    service = Service(store, responder)
    flatness = service._verify_no_live_position_after_fail_safe(
        symbol=SYMBOL, direction="LONG", reason="entry_protection_failed",
    )
    service._close_out_entry_intent_if_flat(
        client_oid=CLIENT_OID, flatness=flatness, symbol=SYMBOL,
        reason="entry_protection_failed",
    )
    return flatness, store.get(CLIENT_OID)


def assert_not_closed_out(store, intent):
    assert intent["state"] == STATE_FILLED, "intent was retired without proof of flatness"
    assert intent["state"] not in TERMINAL_STATES
    assert intent.get("protection_state") != "CLOSED_OUT"
    assert intent["state"] in RECOVERABLE_STATES, "intent must stay visible to startup recovery"
    assert CLIENT_OID in {row["client_oid"] for row in store.recoverable()}


# ── H11: exchange says the position REMAINS ─────────────────────────────────

def test_h11_remains_blocks_closed_out(store):
    flatness, intent = run(store, lambda: {"data": [live_position()]})
    assert flatness is FailSafeFlatness.REMAINS
    assert_not_closed_out(store, intent)


def test_h11_remains_is_not_flat_even_with_other_symbols_flat(store):
    flatness, intent = run(store, lambda: {"data": [
        {"symbol": "ETHUSDT", "total": "0"}, live_position(),
    ]})
    assert flatness is FailSafeFlatness.REMAINS
    assert_not_closed_out(store, intent)


# ── H12: exchange answer is UNKNOWN ─────────────────────────────────────────

def test_h12_missing_data_key_is_unknown(store):
    # the old code read `payload.get("data") or []` -> empty book -> "flat"
    flatness, intent = run(store, lambda: {"code": "00000", "msg": "success"})
    assert flatness is FailSafeFlatness.UNKNOWN
    assert_not_closed_out(store, intent)


def test_h12_null_data_is_unknown(store):
    flatness, intent = run(store, lambda: {"data": None})
    assert flatness is FailSafeFlatness.UNKNOWN
    assert_not_closed_out(store, intent)


def test_h12_empty_response_is_unknown(store):
    flatness, intent = run(store, lambda: {})
    assert flatness is FailSafeFlatness.UNKNOWN
    assert_not_closed_out(store, intent)


def test_h12_none_response_is_unknown(store):
    flatness, intent = run(store, lambda: None)
    assert flatness is FailSafeFlatness.UNKNOWN
    assert_not_closed_out(store, intent)


# ── H13: transport failures ─────────────────────────────────────────────────

@pytest.mark.parametrize("exc", [
    ConnectionError("transport down"),
    TimeoutError("read timeout"),
    RuntimeError("HTTP 502 Bad Gateway"),
    ValueError("Expecting value: line 1 column 1 (char 0)"),
])
def test_h13_transport_error_blocks_closed_out(store, exc):
    def boom():
        raise exc

    flatness, intent = run(store, boom)
    assert flatness is FailSafeFlatness.UNKNOWN
    assert_not_closed_out(store, intent)


# ── H14: malformed responses ────────────────────────────────────────────────

@pytest.mark.parametrize("payload", [
    "not a dict",
    ["a", "list"],
    {"data": "not a list"},
    {"data": {"symbol": SYMBOL}},
    {"data": ["not a dict"]},
])
def test_h14_malformed_payload_is_unknown(store, payload):
    flatness, intent = run(store, lambda: payload)
    assert flatness is FailSafeFlatness.UNKNOWN
    assert_not_closed_out(store, intent)


def test_h14_unparseable_size_is_unknown(store):
    # `_safe_float(..., 0.0)` used to round this straight down to "flat"
    flatness, intent = run(store, lambda: {"data": [live_position(size="n/a")]})
    assert flatness is FailSafeFlatness.UNKNOWN
    assert_not_closed_out(store, intent)


def test_h14_size_absent_on_matching_row_is_unknown(store):
    flatness, intent = run(store, lambda: {"data": [{"symbol": SYMBOL, "holdSide": "long"}]})
    assert flatness is FailSafeFlatness.UNKNOWN
    assert_not_closed_out(store, intent)


# ── H15: proven flat ────────────────────────────────────────────────────────

def test_h15_flat_allows_closed_out(store):
    flatness, intent = run(store, lambda: {"data": []})
    assert flatness is FailSafeFlatness.FLAT
    assert intent["state"] == STATE_ABANDONED
    assert intent["protection_state"] == "CLOSED_OUT"
    assert CLIENT_OID not in {row["client_oid"] for row in store.recoverable()}


def test_h15_zero_size_row_is_flat(store):
    flatness, intent = run(store, lambda: {"data": [live_position(size="0")]})
    assert flatness is FailSafeFlatness.FLAT
    assert intent["protection_state"] == "CLOSED_OUT"


def test_h15_other_symbol_still_open_is_flat(store):
    flatness, intent = run(store, lambda: {"data": [{"symbol": "ETHUSDT", "total": "5"}]})
    assert flatness is FailSafeFlatness.FLAT
    assert intent["protection_state"] == "CLOSED_OUT"


# ── H16: REMAINS then FLAT ──────────────────────────────────────────────────

def test_h16_remains_then_flat_closes_out_exactly_once(store):
    calls: list[str] = []
    responses = [{"data": [live_position()]}, {"data": []}, {"data": []}]

    def responder():
        calls.append("read")
        return responses[min(len(calls) - 1, len(responses) - 1)]

    # attempt 1: position still there -> intent survives, recovery can retry
    service = Service(store, responder)
    first = service._verify_no_live_position_after_fail_safe(
        symbol=SYMBOL, direction="LONG", reason="entry_protection_failed")
    closed_first = service._close_out_entry_intent_if_flat(
        client_oid=CLIENT_OID, flatness=first, symbol=SYMBOL, reason="entry_protection_failed")
    assert first is FailSafeFlatness.REMAINS
    assert closed_first is False
    assert_not_closed_out(store, store.get(CLIENT_OID))

    # attempt 2: exchange now flat -> exactly one CLOSED_OUT
    second = service._verify_no_live_position_after_fail_safe(
        symbol=SYMBOL, direction="LONG", reason="entry_protection_failed")
    assert service._close_out_entry_intent_if_flat(
        client_oid=CLIENT_OID, flatness=second, symbol=SYMBOL,
        reason="entry_protection_failed") is True
    assert second is FailSafeFlatness.FLAT

    # attempt 3: idempotent -- still exactly one CLOSED_OUT transition on record
    third = service._verify_no_live_position_after_fail_safe(
        symbol=SYMBOL, direction="LONG", reason="entry_protection_failed")
    service._close_out_entry_intent_if_flat(
        client_oid=CLIENT_OID, flatness=third, symbol=SYMBOL, reason="entry_protection_failed")

    intent = store.get(CLIENT_OID)
    assert intent["state"] == STATE_ABANDONED
    transitions = [h for h in intent["history"] if h["state"] == STATE_ABANDONED]
    assert len(transitions) == 1, f"expected one CLOSED_OUT transition, got {len(transitions)}"


# ── the gate itself ─────────────────────────────────────────────────────────

def test_absent_client_oid_never_marks(store):
    service = Service(store, lambda: {"data": []})
    assert service._close_out_entry_intent_if_flat(
        client_oid="", flatness=FailSafeFlatness.FLAT, symbol=SYMBOL, reason="r") is False
    assert store.get(CLIENT_OID)["state"] == STATE_FILLED


@pytest.mark.parametrize("flatness", [FailSafeFlatness.REMAINS, FailSafeFlatness.UNKNOWN])
def test_gate_refuses_every_non_flat_verdict(store, flatness):
    service = Service(store, lambda: {"data": []})
    assert service._close_out_entry_intent_if_flat(
        client_oid=CLIENT_OID, flatness=flatness, symbol=SYMBOL, reason="r") is False
    assert_not_closed_out(store, store.get(CLIENT_OID))
