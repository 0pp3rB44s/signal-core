"""Deterministic proof that one logical entry cannot become two live orders.

Every test mocks Bitget. No real order is placed. The central assertion in the
ambiguity tests is a strict call count on the order-creating POST: after a
timeout, a 5xx or a crash, the exchange must never see a second submission for
the same logical entry.
"""

from __future__ import annotations

import logging

import pytest
import requests

from app.config import Settings
from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id
from clients.bitget_base_client import (
    BitgetBaseClient,
    BitgetOrderNotSent,
    BitgetOrderRejected,
    BitgetOrderSubmissionAmbiguous,
)
from clients.schemas import TradePlan
from execution.entry_submitter import (
    EntryOrderSubmitter,
    MAX_ENTRY_SUBMISSIONS,
    RESULT_ABANDONED,
    RESULT_ACCEPTED,
    RESULT_ADOPTED,
    RESULT_BLOCKED_UNKNOWN,
    RESULT_REJECTED,
)
from execution.order_identity import (
    ENTRY_LEG_MAKER,
    ENTRY_LEG_MARKET,
    OrderIdentityError,
    derive_entry_client_oid,
)
from execution.order_intent_store import (
    OrderIntentStore,
    STATE_ABANDONED,
    STATE_PREPARED,
    STATE_PROTECTED,
    STATE_UNKNOWN,
)


LOG = logging.getLogger("test_entry_order_idempotency")


# --- fixtures ------------------------------------------------------------


def make_plan(
    *,
    symbol: str = "BTCUSDT",
    direction: str = "LONG",
    strategy: str = "low_vol_reclaim",
    candle_ms: int = 1_700_000_000_000,
) -> TradePlan:
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
        stop_loss=95.0,
        take_profits=[110.0],
        risk_reward_ratio=2.0,
        account_risk_pct=1.0,
        leverage=5.0,
        position_notional_usdt=50.0,
        notes=[],
        reasons=[],
        geometry_entry=100.0,
    )


class FakeExchange:
    """Scriptable Bitget stand-in that counts every order-creating POST."""

    def __init__(
        self,
        *,
        submit_effects=None,
        lookup_results=None,
        positions=None,
    ) -> None:
        self.submit_effects = list(submit_effects or [])
        self.lookup_results = list(lookup_results or [])
        self.positions = list(positions or [])
        self.post_calls: list[dict] = []
        self.lookup_calls: list[str] = []
        self.position_calls = 0

    # -- order creation --
    def place_entry(self, client_oid: str, **kwargs):
        self.post_calls.append({"client_oid": client_oid, **kwargs})
        effect = (
            self.submit_effects.pop(0)
            if self.submit_effects
            else {"data": {"orderId": f"srv-{len(self.post_calls)}", "clientOid": client_oid}}
        )
        if isinstance(effect, Exception):
            raise effect
        return effect

    # -- lookups --
    def find_order_by_client_oid(self, symbol: str, client_oid: str, product_type=None):
        self.lookup_calls.append(client_oid)
        if not self.lookup_results:
            return {"status": "ABSENT", "order": None, "source": "test", "errors": []}
        result = self.lookup_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def get_all_positions(self):
        self.position_calls += 1
        return {"data": list(self.positions)}

    # -- helpers the submitter uses --
    @staticmethod
    def extract_order_id(payload):
        data = (payload or {}).get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        return str(data.get("orderId") or "")

    @staticmethod
    def extract_fill_metrics(payload):
        data = (payload or {}).get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        return {
            "order_id": str(data.get("orderId") or ""),
            "avg_price": float(data.get("priceAvg") or 0.0),
            "filled_qty": float(data.get("baseVolume") or 0.0),
            "state": str(data.get("state") or ""),
        }


def make_submitter(exchange: FakeExchange, *, session: str = "sess-1") -> EntryOrderSubmitter:
    return EntryOrderSubmitter(
        client=exchange,
        intent_store=OrderIntentStore("state/order_intents.json"),
        session_id=session,
        execution_mode="LIVE",
        log=LOG,
    )


def submit(submitter: EntryOrderSubmitter, plan: TradePlan, *, side="buy", leg=ENTRY_LEG_MARKET):
    exchange = submitter.client
    return submitter.submit_entry(
        plan=plan,
        size=1.0,
        side=side,
        order_type="market",
        leg=leg,
        place=lambda client_oid: exchange.place_entry(client_oid, symbol=plan.symbol, side=side),
        notional_usdt=50.0,
    )


def found(client_oid: str, *, state: str = "filled", order_id: str = "srv-1"):
    return {
        "status": "FOUND",
        "order": {
            "orderId": order_id,
            "clientOid": client_oid,
            "state": state,
            "priceAvg": "100.5",
            "baseVolume": "1",
        },
        "source": "order_detail",
        "errors": [],
    }


ABSENT = {"status": "ABSENT", "order": None, "source": "all_routes", "errors": []}
UNKNOWN = {"status": "UNKNOWN", "order": None, "source": "inconclusive", "errors": ["timeout"]}
OPEN_POSITION = {"symbol": "BTCUSDT", "total": "1", "holdSide": "long"}


# --- 1. normal successful order -----------------------------------------


def test_successful_entry_posts_once_with_client_oid_and_persists_intent_first():
    exchange = FakeExchange()
    submitter = make_submitter(exchange)
    plan = make_plan()
    expected_oid = submitter.client_oid_for(plan)

    result = submit(submitter, plan)

    assert result.status == RESULT_ACCEPTED
    assert len(exchange.post_calls) == 1
    assert exchange.post_calls[0]["client_oid"] == expected_oid
    assert result.order_id == "srv-1"

    record = submitter.intents.get(expected_oid)
    assert record["state"] == "SUBMITTED"
    assert record["submit_attempts"] == 1
    # The intent existed before the POST: created_at precedes the submission.
    assert record["history"][0]["state"] == STATE_PREPARED


def test_intent_is_persisted_before_the_network_call():
    """A crash *inside* the POST still leaves a recoverable intent on disk."""
    store = OrderIntentStore("state/order_intents.json")
    exchange = FakeExchange()
    submitter = EntryOrderSubmitter(
        client=exchange, intent_store=store, session_id="s", execution_mode="LIVE", log=LOG
    )
    plan = make_plan()
    oid = submitter.client_oid_for(plan)
    seen_on_disk: list[str] = []

    def _place(client_oid: str):
        # Runs while the POST is in flight: the intent must already be durable.
        on_disk = OrderIntentStore("state/order_intents.json").get(client_oid)
        seen_on_disk.append(on_disk["state"] if on_disk else "MISSING")
        return {"data": {"orderId": "srv-1", "clientOid": client_oid}}

    submitter.submit_entry(
        plan=plan, size=1.0, side="buy", order_type="market",
        leg=ENTRY_LEG_MARKET, place=_place,
    )

    assert seen_on_disk == ["SUBMITTING"]
    assert store.get(oid) is not None


# --- 2. timeout before any response --------------------------------------


def test_read_timeout_does_not_post_twice_and_starts_reconciliation():
    exchange = FakeExchange(
        submit_effects=[BitgetOrderSubmissionAmbiguous("read timeout")],
        lookup_results=[found("x")],
    )
    submitter = make_submitter(exchange)
    plan = make_plan()

    result = submit(submitter, plan)

    assert len(exchange.post_calls) == 1, "ambiguous timeout must not trigger a blind second POST"
    assert exchange.lookup_calls == [submitter.client_oid_for(plan)]
    assert result.reconciled is True


# --- 3. exchange accepted but response timed out -------------------------


def test_accepted_order_with_lost_response_is_adopted_not_resubmitted():
    plan = make_plan()
    exchange = FakeExchange(submit_effects=[BitgetOrderSubmissionAmbiguous("read timeout")])
    submitter = make_submitter(exchange)
    oid = submitter.client_oid_for(plan)
    exchange.lookup_results = [found(oid, state="filled", order_id="exchange-777")]

    result = submit(submitter, plan)

    assert result.status == RESULT_ADOPTED
    assert result.order_id == "exchange-777"
    assert len(exchange.post_calls) == 1, "exactly one order may exist on the exchange"
    assert submitter.intents.get(oid)["state"] == "ADOPTED"
    assert submitter.intents.get(oid)["exchange_order_id"] == "exchange-777"


# --- 4. HTTP 500 after exchange acceptance -------------------------------


def test_http_500_after_acceptance_adopts_the_existing_order():
    plan = make_plan()
    exchange = FakeExchange(
        submit_effects=[BitgetOrderSubmissionAmbiguous("status=500", status_code=500)]
    )
    submitter = make_submitter(exchange)
    exchange.lookup_results = [found(submitter.client_oid_for(plan), state="live")]

    result = submit(submitter, plan)

    assert result.status == RESULT_ADOPTED
    assert len(exchange.post_calls) == 1


# --- 5. reconciliation confirms no order ---------------------------------


def test_definitive_absence_allows_exactly_one_controlled_resubmission():
    exchange = FakeExchange(
        submit_effects=[BitgetOrderSubmissionAmbiguous("timeout")],
        lookup_results=[ABSENT],
    )
    submitter = make_submitter(exchange)
    plan = make_plan()

    result = submit(submitter, plan)

    assert result.status == RESULT_ACCEPTED
    assert len(exchange.post_calls) == MAX_ENTRY_SUBMISSIONS == 2
    # The controlled retry reuses the identity; it never mints a new one.
    assert len({call["client_oid"] for call in exchange.post_calls}) == 1


def test_repeated_ambiguity_never_exceeds_the_submission_budget():
    exchange = FakeExchange(
        submit_effects=[
            BitgetOrderSubmissionAmbiguous("timeout"),
            BitgetOrderSubmissionAmbiguous("timeout"),
            BitgetOrderSubmissionAmbiguous("timeout"),
        ],
        lookup_results=[ABSENT, ABSENT, ABSENT],
    )
    submitter = make_submitter(exchange)

    result = submit(submitter, make_plan())

    assert len(exchange.post_calls) <= MAX_ENTRY_SUBMISSIONS
    assert result.status == RESULT_ABANDONED


def test_business_rejection_is_terminal_and_never_resubmitted():
    exchange = FakeExchange(submit_effects=[BitgetOrderRejected("code=40762 insufficient balance")])
    submitter = make_submitter(exchange)

    result = submit(submitter, make_plan())

    assert result.status == RESULT_REJECTED
    assert len(exchange.post_calls) == 1
    assert exchange.lookup_calls == [], "a business rejection needs no reconciliation"


def test_not_sent_is_verified_before_any_resubmission():
    exchange = FakeExchange(
        submit_effects=[BitgetOrderNotSent("connection refused")],
        lookup_results=[ABSENT],
    )
    submitter = make_submitter(exchange)

    result = submit(submitter, make_plan())

    assert exchange.lookup_calls, "even NOT_SENT is confirmed against the exchange"
    assert result.status == RESULT_ACCEPTED
    assert len(exchange.post_calls) == 2


# --- 6. reconciliation unavailable ---------------------------------------


def test_unknown_exchange_state_halts_and_blocks_new_entries():
    exchange = FakeExchange(
        submit_effects=[BitgetOrderSubmissionAmbiguous("timeout")],
        lookup_results=[UNKNOWN],
    )
    submitter = make_submitter(exchange)
    plan = make_plan()

    result = submit(submitter, plan)

    assert result.status == RESULT_BLOCKED_UNKNOWN
    assert result.blocks_new_entries is True
    assert len(exchange.post_calls) == 1, "never submit again while the state is unknown"
    assert submitter.intents.get(submitter.client_oid_for(plan))["state"] == STATE_UNKNOWN
    assert submitter.intents.blocking()


def test_lookup_exception_is_treated_as_unknown_not_as_absent():
    exchange = FakeExchange(
        submit_effects=[BitgetOrderSubmissionAmbiguous("timeout")],
        lookup_results=[RuntimeError("exchange unreachable")],
    )
    submitter = make_submitter(exchange)

    result = submit(submitter, make_plan())

    assert result.status == RESULT_BLOCKED_UNKNOWN
    assert len(exchange.post_calls) == 1


def test_absent_order_but_live_position_blocks_instead_of_resubmitting():
    exchange = FakeExchange(
        submit_effects=[BitgetOrderSubmissionAmbiguous("timeout")],
        lookup_results=[ABSENT],
        positions=[OPEN_POSITION],
    )
    submitter = make_submitter(exchange)

    result = submit(submitter, make_plan())

    assert result.status == RESULT_BLOCKED_UNKNOWN
    assert len(exchange.post_calls) == 1


# --- 7. crash after intent persistence, before POST ----------------------


def test_restart_after_persist_without_post_reconciles_and_reuses_identity():
    plan = make_plan()
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

    exchange = FakeExchange(lookup_results=[ABSENT])
    submitter = make_submitter(exchange, session="new-session")

    recovery = submitter.recover_pending_intents()

    assert recovery["blocked"] is False
    assert exchange.post_calls == [], "recovery must never submit a fresh order"
    assert exchange.lookup_calls == [oid], "recovery reuses the persisted identity"
    assert store.get(oid)["state"] == STATE_ABANDONED
    # And the identity is unchanged after the restart.
    assert submitter.client_oid_for(plan) == oid


# --- 8. crash after acceptance, before local save ------------------------


def test_restart_discovers_accepted_order_by_client_oid():
    plan = make_plan()
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
    store.record_attempt(oid)  # died with the POST in flight

    protected_position = dict(OPEN_POSITION, stopLoss="95", takeProfit="110")
    exchange = FakeExchange(
        lookup_results=[found(oid, state="filled", order_id="exchange-999")],
        positions=[protected_position],
    )
    submitter = make_submitter(exchange, session="new-session")

    recovery = submitter.recover_pending_intents()

    assert exchange.post_calls == []
    assert recovery["recovered"][0]["resolution"] == RESULT_ADOPTED
    record = store.get(oid)
    assert record["exchange_order_id"] == "exchange-999"
    assert record["state"] == STATE_PROTECTED
    assert recovery["blocked"] is False


def test_restart_with_unprotected_recovered_position_blocks_new_entries():
    plan = make_plan()
    store = OrderIntentStore("state/order_intents.json")
    oid = derive_entry_client_oid(
        plan_id=plan.plan_id, candidate_id=plan.candidate_id, symbol=plan.symbol,
        direction=plan.direction, strategy=plan.strategy, leg=ENTRY_LEG_MARKET,
    )
    store.prepare(
        client_oid=oid, plan_id=plan.plan_id, candidate_id=plan.candidate_id,
        symbol=plan.symbol, side="buy", direction=plan.direction, size=1.0,
        order_type="market", leg=ENTRY_LEG_MARKET, strategy=plan.strategy,
        session_id="dead", execution_mode="LIVE",
    )
    store.record_attempt(oid)

    exchange = FakeExchange(
        lookup_results=[found(oid, state="filled")],
        positions=[OPEN_POSITION],  # no stopLoss / takeProfit fields
    )
    submitter = make_submitter(exchange)

    recovery = submitter.recover_pending_intents()

    assert recovery["blocked"] is True
    assert store.get(oid)["state"] == STATE_UNKNOWN
    assert exchange.post_calls == []


# --- 9/10. identity stability -------------------------------------------


def test_separate_logical_trades_get_different_client_oids():
    oids = {
        make_plan(symbol="BTCUSDT", direction="LONG").symbol: None,
    }
    variants = [
        make_plan(symbol="BTCUSDT", direction="LONG"),
        make_plan(symbol="BTCUSDT", direction="SHORT"),
        make_plan(symbol="ETHUSDT", direction="LONG"),
        make_plan(symbol="BTCUSDT", direction="LONG", candle_ms=1_700_000_900_000),
        make_plan(symbol="BTCUSDT", direction="LONG", strategy="momentum_breakout"),
    ]
    oids = {
        derive_entry_client_oid(
            plan_id=plan.plan_id, candidate_id=plan.candidate_id, symbol=plan.symbol,
            direction=plan.direction, strategy=plan.strategy,
        )
        for plan in variants
    }
    assert len(oids) == len(variants)


def test_same_logical_trade_always_derives_the_same_client_oid():
    first = make_plan()
    second = make_plan()  # rebuilt from scratch, e.g. after a restart

    def oid(plan, leg=ENTRY_LEG_MARKET):
        return derive_entry_client_oid(
            plan_id=plan.plan_id, candidate_id=plan.candidate_id, symbol=plan.symbol,
            direction=plan.direction, strategy=plan.strategy, leg=leg,
        )

    assert oid(first) == oid(second)
    # Maker and market legs are distinct exchange orders, so distinct ids.
    assert oid(first, ENTRY_LEG_MAKER) != oid(first, ENTRY_LEG_MARKET)


def test_client_oid_is_exchange_safe_and_leaks_nothing():
    plan = make_plan()
    oid = derive_entry_client_oid(
        plan_id=plan.plan_id, candidate_id=plan.candidate_id, symbol=plan.symbol,
        direction=plan.direction, strategy=plan.strategy,
    )
    assert len(oid) <= 64
    assert all(char.isalnum() or char in "-_" for char in oid)
    assert plan.plan_id not in oid and plan.candidate_id not in oid


def test_untrustworthy_plan_identity_is_refused():
    with pytest.raises(OrderIdentityError):
        derive_entry_client_oid(
            plan_id="plan_handmade", candidate_id="candidate-x", symbol="BTCUSDT",
            direction="LONG", strategy="s",
        )
    with pytest.raises(OrderIdentityError):
        derive_entry_client_oid(
            plan_id="", candidate_id="", symbol="BTCUSDT", direction="LONG", strategy="s",
        )


def test_unusable_identity_blocks_submission_entirely():
    exchange = FakeExchange()
    submitter = make_submitter(exchange)

    class BrokenPlan:
        plan_id = "not-derived"
        candidate_id = "nope"
        symbol = "BTCUSDT"
        direction = "LONG"
        strategy = "s"

    result = submitter.submit_entry(
        plan=BrokenPlan(), size=1.0, side="buy", order_type="market",
        leg=ENTRY_LEG_MARKET, place=lambda oid: exchange.place_entry(oid),
    )

    assert result.status == RESULT_REJECTED
    assert exchange.post_calls == []


# --- 11. long and short entry paths --------------------------------------


@pytest.mark.parametrize(
    ("direction", "side"), [("LONG", "buy"), ("SHORT", "sell")]
)
def test_long_and_short_entries_both_carry_a_client_oid(direction, side):
    exchange = FakeExchange()
    submitter = make_submitter(exchange)
    plan = make_plan(direction=direction)

    result = submit(submitter, plan, side=side)

    assert result.status == RESULT_ACCEPTED
    assert len(exchange.post_calls) == 1
    assert exchange.post_calls[0]["client_oid"] == submitter.client_oid_for(plan)
    assert exchange.post_calls[0]["side"] == side


def test_maker_entry_leg_also_carries_a_client_oid_and_reconciles(monkeypatch):
    """The post-only maker entry runs through the same protected path."""
    from execution import maker_entry

    plan = make_plan()
    exchange = FakeExchange(submit_effects=[BitgetOrderSubmissionAmbiguous("timeout")])
    submitter = make_submitter(exchange)
    maker_oid = submitter.client_oid_for(plan, leg=ENTRY_LEG_MAKER)
    exchange.lookup_results = [found(maker_oid, state="filled", order_id="maker-1")]

    client = type(
        "Client",
        (),
        {
            "place_futures_limit_order": staticmethod(
                lambda **kwargs: exchange.place_entry(kwargs.get("client_oid"), **{
                    k: v for k, v in kwargs.items() if k != "client_oid"
                })
            ),
            "get_order_detail": staticmethod(lambda **_: {"data": {"state": "filled"}}),
            "extract_fill_metrics": staticmethod(
                lambda payload: {"filled_qty": 1.0, "state": "filled", "avg_price": 100.0}
            ),
        },
    )()

    settings = type("S", (), {"maker_entry_offset_bps": 1.0, "maker_entry_wait_seconds": 0.0})()

    result = maker_entry.attempt_maker_entry(
        client=client, settings=settings, symbol=plan.symbol, direction=plan.direction,
        size=1.0, anchor_price=100.0, hold_side="long", log=LOG,
        submit=lambda place: submitter.submit_entry(
            plan=plan, size=1.0, side="buy", order_type="limit",
            leg=ENTRY_LEG_MAKER, place=place,
        ),
    )

    assert result["client_oid"] == maker_oid
    assert exchange.post_calls[0]["client_oid"] == maker_oid
    assert len(exchange.post_calls) == 1, "the maker leg must not be resubmitted blindly"
    assert result["order_id"] == "maker-1"


def test_maker_leg_unknown_state_reports_blocked_so_market_fallback_is_suppressed():
    from execution import maker_entry

    plan = make_plan()
    exchange = FakeExchange(
        submit_effects=[BitgetOrderSubmissionAmbiguous("timeout")], lookup_results=[UNKNOWN]
    )
    submitter = make_submitter(exchange)
    settings = type("S", (), {"maker_entry_offset_bps": 1.0, "maker_entry_wait_seconds": 0.0})()

    result = maker_entry.attempt_maker_entry(
        client=None, settings=settings, symbol=plan.symbol, direction=plan.direction,
        size=1.0, anchor_price=100.0, hold_side="long", log=LOG,
        submit=lambda place: submitter.submit_entry(
            plan=plan, size=1.0, side="buy", order_type="limit",
            leg=ENTRY_LEG_MAKER,
            place=lambda oid: exchange.place_entry(oid),
        ),
    )

    assert result["status"] == "BLOCKED_UNKNOWN"
    assert len(exchange.post_calls) == 1


# --- 12. partial fill ----------------------------------------------------


def test_partially_filled_adopted_order_reconciles_quantity_and_price():
    plan = make_plan()
    exchange = FakeExchange(submit_effects=[BitgetOrderSubmissionAmbiguous("timeout")])
    submitter = make_submitter(exchange)
    oid = submitter.client_oid_for(plan)
    exchange.lookup_results = [
        {
            "status": "FOUND",
            "order": {
                "orderId": "srv-partial", "clientOid": oid, "state": "partially_filled",
                "priceAvg": "100.25", "baseVolume": "0.4",
            },
            "source": "orders_pending",
            "errors": [],
        }
    ]

    result = submit(submitter, plan)

    assert result.status == RESULT_ADOPTED
    assert len(exchange.post_calls) == 1
    record = submitter.intents.get(oid)
    assert record["filled_qty"] == pytest.approx(0.4)
    assert record["avg_price"] == pytest.approx(100.25)

    # Protection is recorded exactly once for the adopted position.
    submitter.mark_protected(oid, integrity="OK")
    assert submitter.intents.get(oid)["protection_state"] == "CONFIRMED"
    assert submitter.intents.get(oid)["state"] == STATE_PROTECTED


def test_cancelled_order_without_position_is_not_treated_as_a_fill():
    plan = make_plan()
    exchange = FakeExchange(submit_effects=[BitgetOrderSubmissionAmbiguous("timeout")])
    submitter = make_submitter(exchange)
    oid = submitter.client_oid_for(plan)
    exchange.lookup_results = [found(oid, state="cancelled")]

    result = submit(submitter, plan)

    assert result.status == RESULT_ABANDONED
    assert result.has_live_order is False


# --- 13. duplicate signal / duplicate worker -----------------------------


def test_duplicate_invocation_of_the_same_plan_reaches_the_exchange_once():
    plan = make_plan()
    exchange = FakeExchange()
    submitter = make_submitter(exchange)
    oid = submitter.client_oid_for(plan)

    first = submit(submitter, plan)
    exchange.lookup_results = [found(oid, state="filled", order_id="srv-1")]
    second = submit(submitter, plan)

    assert first.status == RESULT_ACCEPTED
    assert second.status == RESULT_ADOPTED
    assert len(exchange.post_calls) == 1, "a duplicate signal must not create a second order"


def test_second_worker_on_a_fresh_intent_reconciles_instead_of_posting():
    """Two workers, one intent: the second sees the claim and reconciles."""
    plan = make_plan()
    store = OrderIntentStore("state/order_intents.json")
    exchange = FakeExchange()
    worker_a = make_submitter(exchange, session="worker-a")
    worker_b = EntryOrderSubmitter(
        client=exchange, intent_store=store, session_id="worker-b",
        execution_mode="LIVE", log=LOG,
    )
    oid = worker_a.client_oid_for(plan)

    submit(worker_a, plan)
    exchange.lookup_results = [found(oid, state="filled")]
    result_b = submit(worker_b, plan)

    assert len(exchange.post_calls) == 1
    assert result_b.status == RESULT_ADOPTED


# --- 14/15. surrounding behaviour is untouched ---------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}", response=self)


def _client(tmp_path) -> BitgetBaseClient:
    settings = Settings(
        _env_file=None,
        BITGET_RATE_LIMIT_STATE_PATH=str(tmp_path / "rate.json"),
        BITGET_RETRY_BACKOFF_SECONDS=0.0,
        BITGET_MAX_REQUEST_RETRIES=3,
    )
    return BitgetBaseClient(settings=settings)


def test_generic_get_retry_behaviour_remains_operational(tmp_path, monkeypatch):
    client = _client(tmp_path)
    calls: list[str] = []

    def _request(**kwargs):
        calls.append(kwargs["method"])
        if len(calls) < 3:
            return _FakeResponse(503, {"code": "50000", "msg": "unavailable"})
        return _FakeResponse(200, {"code": "00000", "data": {"ok": True}})

    monkeypatch.setattr(requests, "request", _request)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    payload = client._request("GET", "/api/v2/mix/market/tickers")

    assert payload["data"]["ok"] is True
    assert len(calls) == 3, "safe reads must still retry"


def test_order_post_marked_non_idempotent_is_attempted_once(tmp_path, monkeypatch):
    client = _client(tmp_path)
    calls: list[str] = []

    def _request(**kwargs):
        calls.append(kwargs["method"])
        return _FakeResponse(500, {"code": "50000", "msg": "server error"})

    monkeypatch.setattr(requests, "request", _request)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    with pytest.raises(BitgetOrderSubmissionAmbiguous):
        client._request(
            "POST", "/api/v2/mix/order/place-order", body={"size": "1"},
            allow_blind_retry=False, client_oid="bgai-m-test",
        )

    assert len(calls) == 1, "an order POST must never be retried blindly"


@pytest.mark.parametrize("status_code", [408, 429, 500, 502, 503, 504])
def test_every_ambiguous_status_is_classified_not_retried(tmp_path, monkeypatch, status_code):
    client = _client(tmp_path)
    calls: list[str] = []

    def _request(**kwargs):
        calls.append(kwargs["method"])
        return _FakeResponse(status_code, {"code": "x", "msg": "boom"})

    monkeypatch.setattr(requests, "request", _request)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    with pytest.raises(BitgetOrderSubmissionAmbiguous):
        client._request(
            "POST", "/api/v2/mix/order/place-order", body={}, allow_blind_retry=False
        )
    assert len(calls) == 1


def test_read_timeout_on_order_post_is_ambiguous_and_connect_error_is_not_sent(
    tmp_path, monkeypatch
):
    client = _client(tmp_path)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    monkeypatch.setattr(
        requests, "request", lambda **_: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("read timed out"))
    )
    with pytest.raises(BitgetOrderSubmissionAmbiguous):
        client._request("POST", "/api/v2/mix/order/place-order", body={}, allow_blind_retry=False)

    monkeypatch.setattr(
        requests, "request",
        lambda **_: (_ for _ in ()).throw(requests.exceptions.ConnectTimeout("connect timed out")),
    )
    with pytest.raises(BitgetOrderNotSent):
        client._request("POST", "/api/v2/mix/order/place-order", body={}, allow_blind_retry=False)


def test_business_error_on_order_post_is_a_definite_rejection(tmp_path, monkeypatch):
    client = _client(tmp_path)
    calls: list[str] = []

    def _request(**kwargs):
        calls.append(kwargs["method"])
        return _FakeResponse(200, {"code": "40762", "msg": "insufficient balance"})

    monkeypatch.setattr(requests, "request", _request)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    with pytest.raises(BitgetOrderRejected):
        client._request("POST", "/api/v2/mix/order/place-order", body={}, allow_blind_retry=False)
    assert len(calls) == 1
