"""Whole-path guarantees for the live entry order.

Complements tests/test_entry_order_idempotency.py (which proves the submitter's
behaviour) by proving that the *execution service* actually routes through it,
that no other code path can create an entry without an identity, and that the
reduce-only close behaviour is unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id
from clients.bitget_base_client import BitgetOrderSubmissionAmbiguous
from clients.schemas import RiskVerdict, TradePlan
from execution.execution_service import ExecutionService
from risk.mandatory_authorization import (
    RiskAuthorizationMissing, record_authorization, require_authorized,
)


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


def _authorize(service: ExecutionService, *plans: TradePlan) -> None:
    """Test helper standing in for what a real caller (app/runner.py) does before
    ever calling execute(): RiskManager.evaluate() then record_authorization().
    Deployment Gap #1 fix, integrated 2026-09-18 -- execute() now refuses any
    plan that was never authorized this way. `plan` doubles as the 'candidate'
    argument here -- TradePlan already carries candidate_id/symbol/direction,
    the only attributes record_authorization() reads from that parameter."""
    for plan in plans:
        verdict = RiskVerdict(allowed=True, status="ALLOWED", reasons=[],
                              account_risk_pct=plan.account_risk_pct, leverage=plan.leverage,
                              max_open_positions=2)
        record_authorization(verdict, plan, plan_id=plan.plan_id,
                             position_notional_usdt=plan.position_notional_usdt,
                             store=service._risk_authorization_store)


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

    _authorize(service, plan)
    reports = service.execute([plan])

    assert service.client.place_futures_market_order.call_count == 1
    kwargs = service.client.place_futures_market_order.call_args.kwargs
    assert kwargs["side"] == expected_side
    assert kwargs["client_oid"] == service.entry_submitter.client_oid_for(plan)
    assert reports[0].status == "EXECUTED"

    intent = service.intent_store.get(kwargs["client_oid"])
    assert intent["state"] == "PROTECTED"
    assert intent["protection_state"] == "CONFIRMED"


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

    _authorize(service, plan)
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

    _authorize(service, plan)
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

    _authorize(service, first, second)
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

    _authorize(service, plan)
    service.execute([plan])
    service.client.place_futures_market_order.reset_mock()
    service.client.place_futures_market_order.side_effect = None

    second_plan = _plan("SOLUSDT")
    _authorize(service, second_plan)
    reports = service.execute([second_plan])

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

    pending_plan = _plan("SOLUSDT")
    _authorize(service, pending_plan)
    reports = service.execute([pending_plan])

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

    _authorize(service, plan)
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


# --- mandatory risk authorization integration (Deployment Gap #1, 2026-09-18) ---


def test_execute_refuses_a_plan_with_no_prior_authorization(monkeypatch):
    """The core integration guarantee: execute() must never act on a plan that
    nothing ever authorized -- no RiskManager call, no receipt, no order."""
    service = _service(monkeypatch)
    plan = _plan()
    # Deliberately NOT calling _authorize() -- this is the whole point.

    reports = service.execute([plan])

    assert reports == []
    service.client.place_futures_market_order.assert_not_called()
    service.client.set_futures_leverage.assert_not_called()
    assert service.intent_store.all() == []  # never even reached order-intent persistence


def test_execute_refuses_an_explicitly_denied_plan(monkeypatch):
    """A plan whose RiskManager verdict was recorded as denied (e.g. a cluster-
    exposure breach) must be refused even though a record exists -- presence of
    a record is not sufficient, it must also say allowed=True."""
    service = _service(monkeypatch)
    plan = _plan()
    verdict = RiskVerdict(allowed=False, status="DENIED",
                          reasons=["cluster-risk: correlated exposure limit reached"],
                          account_risk_pct=plan.account_risk_pct, leverage=plan.leverage,
                          max_open_positions=2)
    record_authorization(verdict, plan, plan_id=plan.plan_id,
                         position_notional_usdt=plan.position_notional_usdt,
                         store=service._risk_authorization_store)

    reports = service.execute([plan])

    assert reports == []
    service.client.place_futures_market_order.assert_not_called()


def test_authorized_plan_executes_normally_no_regression(monkeypatch):
    """A plan authorized the way app/runner.py's real normal path now does it
    (RiskManager.evaluate() -> record_authorization(), both wired 2026-09-18)
    must still execute exactly as before the gate existed -- proves the fix
    adds a precondition, not a behavior change, for the legacy working path."""
    service = _service(monkeypatch)
    plan = _plan()
    service.client.get_all_positions.side_effect = [
        {"data": []}, {"data": []},
        {"data": [{"symbol": plan.symbol, "holdSide": "long", "total": "0.5",
                   "openPriceAvg": "100.0", "markPrice": "100.0"}]},
    ]

    _authorize(service, plan)
    reports = service.execute([plan])

    assert service.client.place_futures_market_order.call_count == 1
    assert reports[0].status == "EXECUTED"


def test_one_denied_plan_does_not_block_an_unrelated_authorized_plan(monkeypatch):
    """Per-plan filtering, not all-or-nothing: proves the exact semantic Agent
    C's integration spec required."""
    service = _service(monkeypatch)
    good_plan = _plan("BTCUSDT")
    bad_plan = _plan("ETHUSDT")
    _authorize(service, good_plan)
    # bad_plan gets no authorization at all.
    service.client.get_all_positions.side_effect = [
        {"data": []}, {"data": []},
        {"data": [{"symbol": "BTCUSDT", "holdSide": "long", "total": "0.5",
                   "openPriceAvg": "100.0", "markPrice": "100.0"}]},
    ]

    reports = service.execute([good_plan, bad_plan])

    assert service.client.place_futures_market_order.call_count == 1
    kwargs = service.client.place_futures_market_order.call_args.kwargs
    assert kwargs["client_oid"] == service.entry_submitter.client_oid_for(good_plan)


def test_adaptive_trend_plan_end_to_end_now_blocked(monkeypatch):
    """Direct reproduction, end-to-end (not just at the mandatory_authorization
    unit level), of the exact defect Agent C found: a TradePlan built by
    strategies/adaptive_trend_plan.py.build_trade_plan() -- which hardcodes
    verdict="EXECUTABLE" with zero RiskManager involvement -- must now be
    refused by execute(), because nothing ever authorized it. This is the
    'safe direction to fail in' Agent C's review predicted: the gap remains
    unfixed in build_trade_plan() itself (not urgent -- that path is still
    unreachable via adaptive_trend_runtime.route_selected_candidate's
    shadow-only routing), but execute() no longer trusts it blindly either
    way."""
    from strategies.adaptive_trend_plan import build_trade_plan
    from strategies.adaptive_trend_tsmom import Side, SignalCandidate, SizingResult

    candidate = SignalCandidate(
        symbol="BTCUSDT", side=Side.LONG, signal_candle_close_ms=1_800_021_600_000,
        close=65000.0, mom=0.02, atr=800.0,
    )
    sizing = SizingResult(
        equity=100.0, atr=800.0, stop_distance=800.0, stop_distance_pct=0.0123,
        risk_usdt=0.5, raw_notional=20.0, exchange_min_notional=5.0, rounded_notional=20.0,
        effective_risk_usdt=0.5, effective_risk_pct=0.005, required_margin=6.67,
        projected_total_exposure_pct=0.2, leverage=3.0, accepted=True,
    )
    plan = build_trade_plan(candidate, sizing)
    assert plan.verdict == "EXECUTABLE"  # confirms the defect is still present in this function

    service = _service(monkeypatch)
    # Deliberately no _authorize() call -- exactly what happens today, since
    # nothing on this path ever calls RiskManager.

    reports = service.execute([plan])

    assert reports == []
    service.client.place_futures_market_order.assert_not_called()


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
