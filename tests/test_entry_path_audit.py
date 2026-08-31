"""Whole-path guarantees for the live entry order.

Complements tests/test_entry_order_idempotency.py (which proves the submitter's
behaviour) by proving that the *execution service* actually routes through it,
that no other code path can create an entry without an identity, and that the
reduce-only close behaviour is unchanged.
"""

from __future__ import annotations

import re
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id
from clients.bitget_base_client import BitgetOrderSubmissionAmbiguous
from clients.schemas import TradePlan
from execution.execution_service import ExecutionService
from funding_pilot.canonical import CanonicalFundingPilot
from funding_pilot.core import ExchangeTruth, PilotConfig, PilotLedger, PilotRuntime, PilotSignal
from execution.position_manager import PositionManager


REPO = Path(__file__).resolve().parents[1]


# --- repository-wide audit ----------------------------------------------


def _production_python_files() -> list[Path]:
    skip_parts = {
        "tests", ".claude", "__pycache__", ".venv", ".venv-archiver",
        "archiving", "backtesting", "research", "agents", "agents_v2", "agents_v3",
    }
    return [
        path
        for path in REPO.rglob("*.py")
        if not skip_parts & set(path.relative_to(REPO).parts)
    ]


def test_no_production_code_places_an_entry_order_outside_the_protected_path():
    """Every live entry call site must go through EntryOrderSubmitter.

    The only permitted direct callers of the entry endpoints are the order
    client itself (which defines them) and maker_entry's _place helper, which
    receives its clientOid from the submitter.
    """
    allowed = {
        REPO / "clients" / "bitget_order_client.py",   # definition site
        REPO / "clients" / "bitget_rest.py",           # thin alias
        REPO / "execution" / "maker_entry.py",         # _place(), fed by the submitter
        REPO / "execution" / "execution_service.py",   # wrapped in submit_entry()
    }
    pattern = re.compile(r"\.place_futures_(market|limit)_order\s*\(")

    offenders: list[str] = []
    for path in _production_python_files():
        if path in allowed:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if pattern.search(line):
                offenders.append(f"{path.relative_to(REPO)}:{lineno}")

    assert offenders == [], (
        "live entry orders may only be created through EntryOrderSubmitter; "
        f"unprotected call sites: {offenders}"
    )


def test_execution_service_entry_call_supplies_the_client_oid():
    """The execution service must hand the identity to the order client."""
    source = (REPO / "execution" / "execution_service.py").read_text()
    market_call = source.split("self.client.place_futures_market_order(", 1)
    assert len(market_call) == 2, "entry call site not found"
    call_body = market_call[1].split(")", 1)[0]
    assert "client_oid=client_oid" in call_body

    # ...and that call must be wrapped by submit_entry().
    assert "self.entry_submitter.submit_entry(" in source
    assert "place=_place_market_entry" in source


def test_entry_endpoints_are_marked_non_idempotent():
    source = (REPO / "clients" / "bitget_order_client.py").read_text()
    for func in ("place_futures_market_order", "place_futures_limit_order"):
        body = source.split(f"def {func}(", 1)[1].split("\n    def ", 1)[0]
        assert "allow_blind_retry=False" in body, f"{func} may still be retried blindly"


def test_reduce_only_close_keeps_its_retry_behaviour_and_flags():
    """Closing is reduce-only and stays on the normal retry policy (unchanged)."""
    source = (REPO / "clients" / "bitget_order_client.py").read_text()
    body = source.split("def close_futures_position(", 1)[1].split("\n    def ", 1)[0]

    assert '"reduceOnly": "YES"' in body
    assert '"tradeSide": "close"' in body
    assert "allow_blind_retry" not in body, "close path behaviour must stay unchanged"


# --- execution service integration --------------------------------------


def _live_settings(**overrides) -> Settings:
    values = {
        "EXECUTION_ENABLED": True,
        "EXECUTION_MODE": "LIVE",
        "PRODUCTION_SYMBOL_ALLOWLIST": "BTCUSDT,ETHUSDT,SOLUSDT",
        "MAX_SYMBOLS": 3,
        "MAX_OPEN_POSITIONS": 2,
        "EXECUTION_MAX_PER_CYCLE": 2,
        "ALLOW_AUTO_WATCHLIST_REFRESH": False,
        "EXECUTION_MAX_LIVE_NOTIONAL_PER_TRADE_USDT": 50.0,
        "MAKER_ENTRY_ENABLED": False,
        "SYMBOL_COOLDOWN_MINUTES": 0,
        "EXECUTION_REQUIRE_CONFIRMATION": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _plan(symbol: str = "BTCUSDT", direction: str = "LONG") -> TradePlan:
    strategy = "low_vol_reclaim"
    candle_ms = 1_700_000_000_000
    candidate_id = deterministic_candidate_id(strategy, symbol, direction, candle_ms)
    return TradePlan(
        candidate_id=candidate_id,
        candidate_candle_open_timestamp_ms=candle_ms,
        plan_id=deterministic_plan_id(candidate_id),
        symbol=symbol,
        strategy=strategy,
        direction=direction,
        verdict="EXECUTABLE",
        score=80.0,
        entry_prices=[100.0],
        stop_loss=95.0 if direction == "LONG" else 105.0,
        take_profits=[110.0 if direction == "LONG" else 90.0],
        risk_reward_ratio=2.0,
        account_risk_pct=1.0,
        leverage=5.0,
        position_notional_usdt=50.0,
        notes=[],
        reasons=[],
        geometry_entry=100.0,
    )


def _funding_pilot_plan() -> TradePlan:
    plan = _plan(symbol="DOGEUSDT")
    plan.strategy = "funding_crowding_continuation_24h"
    plan.take_profits = []
    plan.leverage = 1.0
    plan.position_notional_usdt = 2.744
    plan.protection_mode = "STOP_ONLY_TIME_EXIT"
    plan.scheduled_exit_at_ms = 4_000_000_000_000
    plan.frozen_spec_sha256 = "cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13"
    plan.pilot_authorized = True
    return plan


def _service(monkeypatch, **setting_overrides) -> ExecutionService:
    monkeypatch.setattr(
        "execution.execution_service.resolve_account_equity", lambda _s: (1000.0, "test")
    )
    service = ExecutionService(settings=_live_settings(**setting_overrides))
    client = MagicMock()
    client.get_all_positions.return_value = {"data": []}
    client.get_pending_orders.return_value = {"data": {"entrustedList": []}}
    client._format_size.return_value = 0.5
    client._contract_price_scale.return_value = 2
    client.extract_order_id.side_effect = lambda payload: str(
        ((payload or {}).get("data") or {}).get("orderId") or ""
    )
    client.extract_fill_metrics.return_value = {
        "order_id": "srv-1", "avg_price": 100.0, "filled_qty": 0.5, "fee": 0.01,
        "pnl": 0.0, "state": "filled",
    }
    client.place_futures_market_order.return_value = {"data": {"orderId": "srv-1"}}
    client.set_futures_leverage.return_value = {"code": "00000", "data": {}}
    client.place_futures_protection_orders.return_value = {
        "protection_verified": True,
        "protection_integrity": "OK",
        "stop_loss_verified": True,
        "take_profit_count": 1,
        "expected_take_profit_count": 1,
        "stop_loss": 95.0,
    }
    client.get_order_detail.return_value = {"data": {"orderId": "srv-1"}}
    service.client = client
    service.entry_submitter.client = client
    return service


@pytest.mark.parametrize(
    ("direction", "expected_side"), [("LONG", "buy"), ("SHORT", "sell")]
)
def test_live_entry_passes_a_deterministic_client_oid(monkeypatch, direction, expected_side):
    service = _service(monkeypatch)
    plan = _plan(direction=direction)

    # Position confirmation after the entry.
    service.client.get_all_positions.side_effect = [
        {"data": []},  # pre-flight open-symbol sync
        {"data": []},  # per-plan exchange-truth max-positions check
        {
            "data": [
                {
                    "symbol": plan.symbol,
                    "holdSide": "long" if direction == "LONG" else "short",
                    "total": "0.5",
                    "openPriceAvg": "100.0",
                    "markPrice": "100.0",
                }
            ]
        },
    ]

    reports = service.execute([plan])

    assert service.client.place_futures_market_order.call_count == 1
    kwargs = service.client.place_futures_market_order.call_args.kwargs
    assert kwargs["side"] == expected_side
    assert kwargs["client_oid"] == service.entry_submitter.client_oid_for(plan)
    assert reports[0].status == "EXECUTED"

    intent = service.intent_store.get(kwargs["client_oid"])
    assert intent["state"] == "PROTECTED"
    assert intent["protection_state"] == "CONFIRMED"


def test_funding_pilot_uses_canonical_entry_and_native_stop_without_tp(monkeypatch):
    service = _service(
        monkeypatch,
        PRODUCTION_SYMBOL_ALLOWLIST="DOGEUSDT",
        MAX_SYMBOLS=1,
        EXECUTION_MIN_LIVE_NOTIONAL_USDT=1.0,
    )
    monkeypatch.setattr(
        "execution.execution_service.resolve_account_equity", lambda _s: (27.44, "pilot-nav")
    )
    plan = _funding_pilot_plan()
    service.funding_pilot_state_provider = lambda: {
        "pilot_nav": 27.44, "available_margin": 20.0, "gross_notional": 0.0,
        "position_count": 0, "kill_switch_latched": False,
        "native_stop_available": True,
    }
    service.client.get_all_positions.side_effect = [
        {"data": []},
        {"data": []},
        {"data": [{"symbol": "DOGEUSDT", "holdSide": "long", "total": "0.02744", "openPriceAvg": "100", "markPrice": "100"}]},
    ]
    service.client.move_futures_stop_loss.return_value = {
        "verified": True,
        "placed": {"data": {"orderId": "stop-1"}},
        "confirmed_stop": {"plan_order_id": "stop-1", "trigger_price": 95.0},
        "verify": {"matched_order": {"plan_order_id": "stop-1"}},
    }

    reports = service.execute([plan])

    assert reports[0].status == "EXECUTED"
    service.client.move_futures_stop_loss.assert_called_once()
    service.client.place_futures_protection_orders.assert_not_called()
    stored = service.store.load(default=[])[-1]
    assert stored["protection_mode"] == "STOP_ONLY_TIME_EXIT"
    assert stored["take_profits"] == []
    assert stored["entry_protection_verified"] is True


def test_integrated_authoritative_canonical_pilot_entry_and_stop_reconciliation(monkeypatch, tmp_path):
    service = _service(monkeypatch, PRODUCTION_SYMBOL_ALLOWLIST="DOGEUSDT", MAX_SYMBOLS=1,
                       EXECUTION_MIN_LIVE_NOTIONAL_USDT=1.0)
    monkeypatch.setattr("execution.execution_service.resolve_account_equity", lambda _s: (27.44, "pilot"))

    class Harness:
        def __init__(self):
            self.state = ExchangeTruth(True, 100.0, 50.0, (), (), (), 20.0)
        def truth(self): return self.state
        def min_notional(self, _symbol): return 1.0
        def decision_book(self, _symbol): return {"bid": 99.9, "ask": 100.1, "timestamp_ms": int(time.time() * 1000)}
        def place_native_stop(self, **_kwargs): raise AssertionError("ExecutionService owns stop placement")
        def verify_native_stop(self, **_kwargs): return {"verified": bool(self.state.pilot_stops)}
        def cancel_working_order(self, _order): self.state = replace(self.state, pilot_working_orders=())
        def close_reduce_only(self, _position, _reason): self.state = replace(self.state, pilot_positions=()); return {"status": "CLOSED"}
        def cancel_stop(self, _stop): self.state = replace(self.state, pilot_stops=())

    harness = Harness()
    def submit(**_kwargs):
        harness.state = replace(harness.state, pilot_positions=({
            "symbol": "DOGEUSDT", "side": "LONG", "client_oid": "cgc-fcp-integrated",
            "notional": 2.744, "unrealized_pnl": 0.0,
        },))
        return {"data": {"orderId": "entry-1"}}
    def positions():
        return {"data": [{"symbol": row["symbol"], "holdSide": "long", "total": "0.02744",
                           "openPriceAvg": "100", "markPrice": "100"}
                          for row in harness.state.pilot_positions]}
    def stop(**_kwargs):
        harness.state = replace(harness.state, pilot_stops=({
            "symbol": "DOGEUSDT", "client_oid": "cgc-fcp-stop", "order_id": "stop-1", "stop_price": 90.0,
        },))
        return {"verified": True, "placed": {"data": {"orderId": "stop-1"}},
                "confirmed_stop": {"plan_order_id": "stop-1", "trigger_price": 90.0},
                "verify": {"matched_order": {"plan_order_id": "stop-1"}}}

    service.client.get_all_positions.side_effect = positions
    service.client.place_futures_market_order.side_effect = submit
    service.client.move_futures_stop_loss.side_effect = stop
    spec = Path(__file__).resolve().parents[1] / "research/validation/FROZEN_SPECS.json"
    ledger = PilotLedger(tmp_path / "pilot.sqlite")
    runtime = PilotRuntime(replace(PilotConfig(spec, tmp_path / "pilot.sqlite"), orders_enabled=True), ledger, harness)
    manager = object.__new__(PositionManager)
    canonical = CanonicalFundingPilot(runtime, service, manager)
    now = int(time.time() * 1000)
    report = canonical.process_signal(PilotSignal("sig-integrated", now, "DOGEUSDT", "LONG", 100.0, {"f": 1}))
    assert report.status == "EXECUTED"
    assert ledger.events("CANONICAL_OPEN")[0]["payload"]["stop_order_id"] == "stop-1"
    assert canonical.recover()["scheduled_exits"]["DOGEUSDT"] == now + 86_400_000
    manager.store = service.store
    manager.client = MagicMock()
    def close_position(**_kwargs):
        harness.state = replace(harness.state, pilot_positions=())
        return {"status": "CLOSED", "orderId": "exit-1"}
    def cancel_protection(**_kwargs):
        harness.state = replace(harness.state, pilot_stops=())
        return {"cancelled": ["stop-1"]}
    manager.client.close_futures_position_full.side_effect = close_position
    manager.client.get_all_positions.side_effect = positions
    manager.client.cancel_all_futures_tpsl_orders.side_effect = cancel_protection
    manager.client.get_futures_protection_orders.return_value = {"stop_orders": [], "take_profit_orders": []}
    manager.client.get_pending_orders.return_value = {"data": {"entrustedList": []}}
    assert canonical.process_time_exits(now_ms=now + 86_400_001) == [
        {"symbol": "DOGEUSDT", "status": "POSITION_CLOSED_STOP_CANCELLED"}
    ]
    assert ledger.events("CANONICAL_TIME_EXIT")
    assert not harness.state.pilot_positions and not harness.state.pilot_stops


def test_ambiguous_live_entry_never_posts_twice(monkeypatch):
    service = _service(monkeypatch)
    plan = _plan()
    oid = service.entry_submitter.client_oid_for(plan)

    service.client.place_futures_market_order.side_effect = BitgetOrderSubmissionAmbiguous(
        "status=504 gateway timeout", status_code=504
    )
    service.client.find_order_by_client_oid.return_value = {
        "status": "FOUND",
        "order": {"orderId": "srv-adopted", "clientOid": oid, "state": "filled",
                  "priceAvg": "100.4", "baseVolume": "0.5"},
        "source": "order_detail",
        "errors": [],
    }
    service.client.get_all_positions.side_effect = [
        {"data": []},
        {"data": []},
        {
            "data": [
                {
                    "symbol": plan.symbol,
                    "holdSide": "long",
                    "total": "0.5",
                    "openPriceAvg": "100.4",
                    "markPrice": "100.4",
                }
            ]
        },
    ]

    reports = service.execute([plan])

    assert service.client.place_futures_market_order.call_count == 1
    assert reports[0].status == "EXECUTED"
    assert reports[0].exchange_order_id == "srv-adopted"
    assert service.intent_store.get(oid)["exchange_order_id"] == "srv-adopted"


def test_v2_unfilled_cancelled_maker_is_retired_in_the_execution_path(monkeypatch):
    service = _service(
        monkeypatch,
        MAKER_ENTRY_WAIT_SECONDS=0.0,
        OLD_STRATEGIES_NEW_ENTRIES_ENABLED=False,
    )
    plan = _plan()
    plan.strategy = "low_vol_reclaim_v2"
    oid = service.entry_submitter.client_oid_for(plan, leg="maker")
    service.client.get_trade_fee_rate.return_value = {
        "data": {"makerFeeRate": "0.0002", "takerFeeRate": "0.0006"}
    }
    service.client.place_futures_limit_order.return_value = {
        "data": {"orderId": "maker-unfilled"}
    }
    service.client.extract_fill_metrics.return_value = {
        "order_id": "maker-unfilled",
        "avg_price": 0.0,
        "filled_qty": 0.0,
        "fee": 0.0,
        "pnl": 0.0,
        "state": "live",
    }
    service.client.get_all_positions.return_value = {"data": []}

    reports = service.execute([plan])

    assert reports[0].status == "SKIPPED"
    assert "unfilled_cancelled" in reports[0].message
    intent = service.intent_store.get(oid)
    assert intent["state"] == "ABANDONED"
    assert intent["classification"] == "ORDER_DEAD"
    assert service.intent_store.recoverable() == []


def test_unknown_exchange_state_blocks_every_further_entry_in_the_cycle(monkeypatch):
    service = _service(monkeypatch)
    first, second = _plan("BTCUSDT"), _plan("ETHUSDT")

    service.client.place_futures_market_order.side_effect = BitgetOrderSubmissionAmbiguous(
        "read timeout"
    )
    service.client.find_order_by_client_oid.return_value = {
        "status": "UNKNOWN", "order": None, "source": "inconclusive", "errors": ["boom"],
    }

    reports = service.execute([first, second])

    assert service.client.place_futures_market_order.call_count == 1
    assert [report.status for report in reports] == ["SKIPPED"]
    assert reports[0].symbol == "BTCUSDT"
    assert "could not be established" in reports[0].message.lower()
    assert service.intent_store.blocking()[0]["state"] == "UNKNOWN"


def test_next_cycle_stays_blocked_until_the_intent_is_reconciled(monkeypatch):
    service = _service(monkeypatch)
    plan = _plan()
    service.client.place_futures_market_order.side_effect = BitgetOrderSubmissionAmbiguous("t/o")
    service.client.find_order_by_client_oid.return_value = {
        "status": "UNKNOWN", "order": None, "source": "inconclusive", "errors": ["boom"],
    }

    service.execute([plan])
    service.client.place_futures_market_order.reset_mock()
    service.client.place_futures_market_order.side_effect = None

    reports = service.execute([_plan("SOLUSDT")])

    assert service.client.place_futures_market_order.call_count == 0
    assert reports[0].status == "SKIPPED"
    assert "UNKNOWN" in reports[0].message


def test_pending_exchange_entry_blocks_every_new_symbol(monkeypatch):
    service = _service(monkeypatch)
    service.client.get_pending_orders.return_value = {
        "data": {
            "entrustedList": [{
                "symbol": "BTCUSDT",
                "orderId": "pending-entry-1",
                "tradeSide": "open",
                "reduceOnly": "NO",
            }]
        }
    }

    reports = service.execute([_plan("SOLUSDT")])

    assert reports[0].status == "SKIPPED"
    assert "pending exchange entry order" in reports[0].message
    service.client.place_futures_market_order.assert_not_called()
    assert service.intent_store.all() == []


def test_pending_reduce_only_close_does_not_look_like_a_new_entry(monkeypatch):
    service = _service(monkeypatch)
    service.client.get_pending_orders.return_value = {
        "data": {
            "entrustedList": [{
                "symbol": "BTCUSDT",
                "orderId": "pending-close-1",
                "tradeSide": "close",
                "reduceOnly": "YES",
            }]
        }
    }
    plan = _plan("SOLUSDT")
    service.client.get_all_positions.side_effect = [
        {"data": []},
        {"data": []},
        {"data": [{
            "symbol": "SOLUSDT", "holdSide": "long", "total": "0.5",
            "openPriceAvg": "100", "markPrice": "100",
        }]},
    ]

    reports = service.execute([plan])

    assert reports[0].status == "EXECUTED"
    assert service.client.place_futures_market_order.call_count == 1


def test_fail_safe_close_uses_the_reduce_only_path_not_a_new_opening_order(monkeypatch):
    """place_futures_market_order() always sends tradeSide=open, so the
    fail-safe must never route through it."""
    service = _service(monkeypatch)

    service._fail_safe_close(
        symbol="BTCUSDT", size=0.5, close_side="sell",
        direction="LONG", reason="entry_protection_failed",
    )

    service.client.place_futures_market_order.assert_not_called()
    service.client.close_futures_position.assert_not_called()
    service.client.close_futures_position_full.assert_called_once()
    kwargs = service.client.close_futures_position_full.call_args.kwargs
    assert kwargs["direction"] == "LONG"
    assert kwargs["size"] == 0.5


def test_empty_pending_orders_envelope_is_not_read_as_an_order():
    """Bitget sends {"entrustedList": null} when flat; that is zero orders, not one."""
    from clients.bitget_order_client import BitgetOrderClientMixin as M

    assert M._order_rows({"data": {"entrustedList": None, "endId": None}}) == []
    assert M._order_rows({"data": {"entrustedList": [], "endId": None}}) == []
    assert M._order_rows({"data": {"entrustedList": [{"orderId": "1"}]}}) == [{"orderId": "1"}]
    # A bare order object (order/detail) is still one row.
    assert M._order_rows({"data": {"orderId": "9", "clientOid": "x"}}) == [
        {"orderId": "9", "clientOid": "x"}
    ]
    assert M._order_rows({"data": None}) == []
