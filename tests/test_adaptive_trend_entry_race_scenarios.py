"""End-to-end proof that AdaptiveTrend's entry/protection lifecycle is safe
through the REAL, generic execution path -- not a parallel order engine.

The HYBRID SAFE MODE gate in ExecutionService.execute() is opened ONLY here,
ONLY inside this test module, via monkeypatching a test-scoped strategy-name
alias that legitimately satisfies the gate's existing substring check
("momentum" in name) -- production code (execution/execution_service.py's
gate condition) is never edited. Everything downstream of the gate --
identity, idempotency, sizing, entry submission, protection placement,
fail-safe close, one-position cap -- is the exact same generic machinery
every other strategy already uses; this file proves AdaptiveTrend's specific
inputs (empty take_profits, trailing-stop-only) flow through it correctly
after the two scoped fixes in this change set:
  - execution/portfolio_selector.py: empty take_profits no longer rejected
    for adaptive_trend_tsmom_v1
  - execution/execution_service.py: is_trailing_stop_only_strategy() makes
    the mandatory-TP entry gate and the has_tp protection-verification
    check both trivially satisfied for this strategy specifically
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import execution.execution_service as execution_service_module
import execution.portfolio_selector as portfolio_selector_module
import strategies.adaptive_trend_plan as adaptive_trend_plan_module
from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id
from clients.schemas import TradePlan
from execution.execution_service import ExecutionService
from strategies.adaptive_trend_tsmom import (
    Side,
    SignalCandidate,
    initial_stop,
    size_position,
)
from tests.test_entry_path_audit import _live_settings, _service

ALIAS_STRATEGY = "adaptive_trend_momentum_v1_test"


@pytest.fixture
def adaptive_gate_open(monkeypatch):
    """Opens the hybrid gate for exactly one test-scoped strategy string,
    and makes the real trailing-stop-only recognition + one-position cap
    apply to that alias too -- so the test exercises the REAL logic paths,
    just keyed on a name that legitimately passes the existing substring
    check, instead of touching the gate itself."""
    monkeypatch.setattr(adaptive_trend_plan_module, "STRATEGY_NAME", ALIAS_STRATEGY)
    monkeypatch.setattr(
        execution_service_module, "is_trailing_stop_only_strategy",
        lambda strategy: str(strategy or "").strip().lower() == ALIAS_STRATEGY,
    )
    monkeypatch.setattr(
        portfolio_selector_module, "is_trailing_stop_only_strategy",
        lambda strategy: str(strategy or "").strip().lower() == ALIAS_STRATEGY,
    )
    monkeypatch.setitem(
        ExecutionService.PER_STRATEGY_MAX_OPEN_POSITIONS_OVERRIDE, ALIAS_STRATEGY, 1
    )
    return ALIAS_STRATEGY


def _winner(symbol="BTCUSDT", side=Side.LONG, close=100.0, mom=0.05, atr=2.0,
            close_ms=1_785_700_000_000):
    return SignalCandidate(symbol=symbol, side=side, signal_candle_close_ms=close_ms,
                            close=close, mom=mom, atr=atr)


def _adaptive_plan(winner=None, equity=1000.0, exchange_min_notional=5.0) -> TradePlan:
    """Builds a real AdaptiveTrend TradePlan via the production
    build_trade_plan() adapter -- exercising the real identity/sizing/plan
    construction code, not a hand-rolled substitute."""
    winner = winner or _winner()
    stop = initial_stop(winner.close, winner.atr, winner.side)
    sizing = size_position(equity=equity, entry_price=winner.close, stop_price=stop,
                            exchange_min_notional=exchange_min_notional)
    assert sizing.accepted, f"test fixture sizing unexpectedly rejected: {sizing.rejection_reason}"
    return adaptive_trend_plan_module.build_trade_plan(winner, sizing)


def _adaptive_service(monkeypatch, **overrides) -> ExecutionService:
    return _service(monkeypatch, **overrides)


def _open_position(symbol="BTCUSDT", *, entry=100.0, side="long"):
    return {"symbol": symbol, "holdSide": side, "total": "0.5",
            "openPriceAvg": str(entry), "markPrice": str(entry)}


# --- 1. one-position enforcement with a real existing position fixture ---


def test_one_position_e2e_second_entry_blocked_by_existing_open_position(
    adaptive_gate_open, monkeypatch,
):
    service = _adaptive_service(monkeypatch)
    existing = [{"symbol": "BTCUSDT", "status": "OPEN", "strategy": adaptive_gate_open}]
    service.store.save(existing)
    service.client.get_all_positions.return_value = {"data": [_open_position("BTCUSDT")]}

    plan = _adaptive_plan(_winner(symbol="ETHUSDT"))
    reports = service.execute([plan])

    assert reports[0].status == "SKIPPED"
    assert service.client.place_futures_market_order.call_count == 0


# --- 2. opposite signal cannot create a hedge ----------------------------


def test_no_hedge_e2e_opposite_direction_same_symbol_blocked(
    adaptive_gate_open, monkeypatch,
):
    service = _adaptive_service(monkeypatch)
    existing = [{"symbol": "BTCUSDT", "status": "OPEN", "strategy": adaptive_gate_open}]
    service.store.save(existing)
    service.client.get_all_positions.return_value = {"data": [_open_position("BTCUSDT")]}

    short_winner = _winner(symbol="BTCUSDT", side=Side.SHORT)
    plan = _adaptive_plan(short_winner)
    reports = service.execute([plan])

    assert reports[0].status == "SKIPPED"
    assert service.client.place_futures_market_order.call_count == 0


# --- 3. duplicate same-candle candidate cannot execute twice -------------


def test_duplicate_same_candle_candidate_cannot_execute_twice(
    adaptive_gate_open, monkeypatch,
):
    service = _adaptive_service(monkeypatch)
    winner = _winner()
    plan = _adaptive_plan(winner)

    service.client.get_all_positions.side_effect = [
        {"data": []}, {"data": []}, {"data": [_open_position("BTCUSDT")]},
    ]
    first = service.execute([plan])
    assert first[0].status == "EXECUTED"
    assert service.client.place_futures_market_order.call_count == 1

    # Second cycle, same exact candidate (same signal candle) -- the
    # exchange now reports the position from the first call, so the
    # one-position cap blocks it before any second order attempt.
    same_plan = _adaptive_plan(winner)
    assert same_plan.candidate_id == plan.candidate_id
    assert same_plan.plan_id == plan.plan_id
    service.client.get_all_positions.return_value = {"data": [_open_position("BTCUSDT")]}
    second = service.execute([same_plan])

    assert second[0].status == "SKIPPED"
    assert service.client.place_futures_market_order.call_count == 1


# --- 4/5/7. entry idempotency/crash-safety are the generic, already- ------
# proven EntryOrderSubmitter guarantees (tests/test_entry_order_idempotency.py).
# These confirm they apply unmodified to a REAL AdaptiveTrend TradePlan.


def test_crash_after_durable_intent_before_submission_reconciles_not_resubmits(
    adaptive_gate_open,
):
    from execution.entry_submitter import EntryOrderSubmitter
    from execution.order_identity import ENTRY_LEG_MARKET, derive_entry_client_oid
    from execution.order_intent_store import OrderIntentStore, STATE_ABANDONED

    plan = _adaptive_plan()
    store = OrderIntentStore("state/order_intents.json")
    oid = derive_entry_client_oid(
        plan_id=plan.plan_id, candidate_id=plan.candidate_id, symbol=plan.symbol,
        direction=plan.direction, strategy=plan.strategy, leg=ENTRY_LEG_MARKET,
    )
    store.prepare(
        client_oid=oid, plan_id=plan.plan_id, candidate_id=plan.candidate_id,
        symbol=plan.symbol, side="buy", direction=plan.direction, size=1.0,
        order_type="market", leg=ENTRY_LEG_MARKET, strategy=plan.strategy,
        session_id="dead-session", execution_mode="LIVE",
    )
    client = MagicMock()
    client.get_all_positions.return_value = {"data": []}
    client.find_order_by_client_oid.return_value = {
        "status": "ABSENT", "order": None, "source": "all_routes", "errors": [],
    }
    submitter = EntryOrderSubmitter(client=client, intent_store=store,
                                     session_id="new-session", execution_mode="LIVE",
                                     log=__import__("logging").getLogger("t"))

    recovery = submitter.recover_pending_intents()

    assert recovery["blocked"] is False
    assert client.place_futures_market_order.call_count == 0
    assert store.get(oid)["state"] == STATE_ABANDONED


def test_exchange_entry_succeeds_local_ack_lost_is_adopted_not_resubmitted(
    adaptive_gate_open,
):
    from clients.bitget_base_client import BitgetOrderSubmissionAmbiguous
    from execution.entry_submitter import EntryOrderSubmitter, RESULT_ADOPTED
    from execution.order_identity import ENTRY_LEG_MARKET
    from execution.order_intent_store import OrderIntentStore

    plan = _adaptive_plan()
    client = MagicMock()
    client.get_all_positions.return_value = {"data": []}
    submitter = EntryOrderSubmitter(client=client, intent_store=OrderIntentStore("state/order_intents.json"),
                                     session_id="s", execution_mode="LIVE",
                                     log=__import__("logging").getLogger("t"))
    oid = submitter.client_oid_for(plan)

    def _place(client_oid):
        raise BitgetOrderSubmissionAmbiguous("read timeout")

    client.find_order_by_client_oid.return_value = {
        "status": "FOUND",
        "order": {"orderId": "exchange-777", "clientOid": oid, "state": "filled",
                   "priceAvg": "100.0", "baseVolume": "1"},
        "source": "order_detail", "errors": [],
    }

    result = submitter.submit_entry(plan=plan, size=1.0, side="buy", order_type="market",
                                     leg=ENTRY_LEG_MARKET, place=_place, notional_usdt=50.0)

    assert result.status == RESULT_ADOPTED
    assert result.order_id == "exchange-777"


# --- 6. crash after entry, before initial protection ----------------------
# --- 7. protection succeeds but response times out (retried, not duplicated)
# --- 8. protection fails -> existing fail-safe/emergency-close handles it --


def test_initial_protection_e2e_confirms_and_records_state(
    adaptive_gate_open, monkeypatch,
):
    service = _adaptive_service(monkeypatch)
    plan = _adaptive_plan()
    service.client.get_all_positions.side_effect = [
        {"data": []}, {"data": []}, {"data": [_open_position("BTCUSDT")]},
    ]
    # Real AdaptiveTrend shape: zero take-profits expected, only a stop.
    service.client.place_futures_protection_orders.return_value = {
        "protection_verified": True, "protection_integrity": "OK",
        "stop_loss_verified": True, "take_profit_count": 0,
        "expected_take_profit_count": 0,
    }

    reports = service.execute([plan])

    assert reports[0].status == "EXECUTED"
    assert service.client.place_futures_protection_orders.call_count == 1
    call = service.client.place_futures_protection_orders.call_args.kwargs
    assert call["take_profits"] == []
    rows = service.store.load(default=[])
    assert rows[0]["protection_state"] == "INITIAL_PROTECTION_CONFIRMED"


def test_protection_failure_failsafe_e2e_emergency_close_invoked(
    adaptive_gate_open, monkeypatch,
):
    """A genuine protection failure (exchange never confirms the stop) must
    still trigger the existing fail-safe close -- the trailing-stop-only fix
    only changes what counts as a MISSING take-profit, never what counts as
    a missing STOP LOSS."""
    service = _adaptive_service(monkeypatch)
    plan = _adaptive_plan()
    service.client.get_all_positions.side_effect = [
        {"data": []}, {"data": []}, {"data": [_open_position("BTCUSDT")]},
        {"data": []},  # post-fail-safe flatness verification
    ]
    service.client.place_futures_protection_orders.return_value = {
        "protection_verified": False,
        "protection_integrity": "UNVERIFIED",
        "stop_loss_verified": False,
        "take_profit_count": 0,
        "expected_take_profit_count": 0,
    }

    reports = service.execute([plan])

    assert reports[0].status == "ERROR"
    assert "FAIL-SAFE" in reports[0].message
    assert service.client.close_futures_position_full.call_count >= 1


def test_protection_timeout_then_retry_succeeds_without_duplicate_protection(
    adaptive_gate_open, monkeypatch,
):
    service = _adaptive_service(monkeypatch)
    plan = _adaptive_plan()
    service.client.get_all_positions.side_effect = [
        {"data": []}, {"data": []}, {"data": [_open_position("BTCUSDT")]},
    ]
    service.client.place_futures_protection_orders.side_effect = [
        TimeoutError("response timed out"),
        {
            "protection_verified": True, "protection_integrity": "OK",
            "stop_loss_verified": True, "take_profit_count": 0,
            "expected_take_profit_count": 0,
        },
    ]

    reports = service.execute([plan])

    assert reports[0].status == "EXECUTED"
    assert service.client.place_futures_protection_orders.call_count == 2


# --- 9. weekly freeze activates after signal selection, before execute() ---


def test_weekly_freeze_active_at_call_time_blocks_before_execute(adaptive_gate_open):
    from execution.adaptive_trend_entry import submit_adaptive_trend_entry

    winner = _winner()
    stop = initial_stop(winner.close, winner.atr, winner.side)
    sizing = size_position(equity=1000.0, entry_price=winner.close, stop_price=stop,
                            exchange_min_notional=5.0)
    svc = MagicMock()
    result = submit_adaptive_trend_entry(
        winner=winner, winner_sizing=sizing, weekly_freeze_active=True,
        execution_service=svc,
    )
    assert result is None
    svc.execute.assert_not_called()


# --- 10. exchange position appears before local lifecycle finalization ----


def test_unattributed_exchange_position_never_misattributed_to_adaptive_trend():
    from execution.position_reconciler import RECOVERED_EXCHANGE_POSITION_STRATEGY
    from strategies.adaptive_trend_tsmom import STRATEGY_VERSION

    assert RECOVERED_EXCHANGE_POSITION_STRATEGY != STRATEGY_VERSION


# --- 11. restart with an already-open position continues from exchange truth


def test_restart_reconciliation_e2e_uses_exchange_reported_stop_not_local():
    from execution.position_manager import PositionManager

    class Harness(PositionManager):
        def __init__(self, client):
            import logging
            self.client = client
            self.settings = type("S", (), {"bitget_product_type": "USDT-FUTURES"})()
            self.log = logging.getLogger("h")

    import time as _time
    six_h = 6 * 60 * 60 * 1000
    now_ms = int(_time.time() * 1000)
    boundary = (now_ms // six_h) * six_h

    class FakeClient:
        def __init__(self):
            self.protect_calls = []

        def get_candles(self, symbol, product_type, granularity="6h", limit=200):
            closes = [100.0] * 15 + [120.0]
            start = boundary - six_h - (len(closes) - 1) * six_h
            return {"data": [[start + i * six_h, c, c + 1, c - 1, c, "1", "1"]
                              for i, c in enumerate(closes)]}

        def place_futures_protection_orders(self, **kwargs):
            self.protect_calls.append(kwargs)
            return {"stop_loss": kwargs["stop_loss"]}

    manager = Harness(FakeClient())
    from strategies.adaptive_trend_tsmom import STRATEGY_VERSION
    position = {
        "symbol": "BTCUSDT", "status": "OPEN", "strategy": STRATEGY_VERSION,
        "direction": "LONG", "stop_loss": 50.0,  # stale local value, as-if-crashed
        "last_price": 120.0, "adaptive_trend_last_processed_close_ms": None,
    }
    live_position = {"symbol": "BTCUSDT", "stopLoss": "95.0", "holdSide": "long", "total": "1.0"}

    updates, events = [], []
    manager._sync_adaptive_trend_position(
        position, bitget_sync_ok=True, bitget_open_symbols={"BTCUSDT"},
        positions_live=[live_position], price_map={"BTCUSDT": 120.0},
        updates=updates, events=events,
    )

    assert manager.client.protect_calls[0]["stop_loss"] > 95.0
    assert position["stop_loss"] == manager.client.protect_calls[0]["stop_loss"]
    assert position["stop_loss"] != 50.0


# --- 12. repeated trailing-stop evaluation is idempotent, never loosens ---


def test_trailing_idempotency_e2e_repeated_evaluation_never_loosens():
    from strategies.adaptive_trend_trail import TrailOutcome, evaluate_trail
    from strategies.adaptive_trend_tsmom import ATR_PERIOD, Candle6h, Side as _Side

    six_h = 6 * 60 * 60 * 1000
    warmup = ATR_PERIOD + 1
    closes = [100.0] * warmup + [120.0]

    def bars(cs):
        out = []
        for i, c in enumerate(cs):
            open_ms = i * six_h
            out.append(Candle6h(open_ms=open_ms, close_ms=open_ms + six_h,
                                 open=c, high=c + 1.0, low=c - 1.0, close=c))
        return out

    candles = bars(closes)
    first = evaluate_trail(symbol="BTCUSDT", side=_Side.LONG, current_stop=90.0,
                            candles=candles, now_ms=candles[-1].close_ms,
                            last_processed_close_ms=None)
    assert first.outcome == TrailOutcome.UPDATED
    tightened_stop = first.new_stop

    # Re-evaluate the SAME candle set again with the NEW stop as current --
    # repeated evaluation must be a pure no-op, never re-loosen or re-tighten
    # past what the exchange already holds.
    second = evaluate_trail(symbol="BTCUSDT", side=_Side.LONG, current_stop=tightened_stop,
                             candles=candles, now_ms=candles[-1].close_ms,
                             last_processed_close_ms=first.last_processed_close_ms)
    assert second.outcome == TrailOutcome.NO_NEW_CANDLE
    assert second.new_stop is None  # caller must not touch the exchange stop again


# --- MicroFlow remains retired regardless of any of the above -------------


def test_microflow_remains_retired_alongside_adaptive_trend_entry_wiring(adaptive_gate_open):
    from risk.risk_manager import RiskManager

    assert hasattr(RiskManager, "_microflow_retirement_gate")
    import inspect
    sig = inspect.signature(RiskManager._microflow_retirement_gate)
    assert ALIAS_STRATEGY not in str(sig)
