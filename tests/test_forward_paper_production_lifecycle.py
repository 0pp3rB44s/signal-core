"""End-to-end forward-paper lifecycle over the production snapshot factory.

The hand-built snapshots used elsewhere in the suite carry ``context={}``, so they
could not catch the ContractSpec serialization failure that silently dropped every
executable plan in production. These tests drive the same factory, the same
serialization path and the same event store the running bot uses.
"""

from __future__ import annotations

import csv

import pytest

from clients.schemas import Candle, ContractSpec, TradePlan
from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id
from data.market_fetcher import MarketFetcher
from forward_paper.service import ForwardPaperService
from forward_paper.store import canonical_json
from market_features.engine import LiveMarketContext, aggregate_candles
from tests.test_candidate_lifecycle import _settings

SYMBOL = "SOLUSDT"
STEP_MS = 900_000
FIRST_TS = 1_767_225_600_000


def _candles(count: int = 320, *, start: int = FIRST_TS, price: float = 100.0) -> list[Candle]:
    result: list[Candle] = []
    for index in range(count):
        close = price + 0.08 + (index % 7) * 0.01
        result.append(
            Candle(start + index * STEP_MS, price, close + 0.2, price - 0.2, close, 100 + index % 13, 10_000 + index)
        )
        price = close
    return result


def _contract() -> ContractSpec:
    """A ContractSpec exactly as the exchange client produces it."""
    return ContractSpec(
        symbol=SYMBOL, product_type="USDT-FUTURES", quote_coin="USDT", base_coin="SOL",
        status="normal", min_trade_num=0.1, size_multiplier=0.1, price_place=3,
        volume_24h_usdt=812_345_678.0, change_pct_24h=0.0231,
        raw={"symbol": SYMBOL, "makerFeeRate": "0.0002", "takerFeeRate": "0.0006"},
    )


def _live_inputs() -> LiveMarketContext:
    """Live context including the raw orderbook ladder the bot really carries."""
    return LiveMarketContext(
        orderbook={
            "bids": [[100.0 - i * 0.01, 25.0 + i] for i in range(50)],
            "asks": [[100.01 + i * 0.01, 25.0 + i] for i in range(50)],
        },
        spread_bps=2.75,
        htf_context={"htf_regime_1d": "bullish", "htf_regime_4h": "bullish", "htf_regime": "bullish"},
        contract=_contract(),
    )


def _production_snapshot(candles: list[Candle]):
    """Build a snapshot through the exact factory the live scan loop uses."""
    as_of = candles[-1].timestamp_ms + STEP_MS
    hourly = aggregate_candles(candles, "15m", "1h", as_of)
    return MarketFetcher.build_snapshot_from_inputs(
        SYMBOL, candles, hourly, as_of_timestamp_ms=as_of, inputs=_live_inputs()
    )


def _plan(snapshot, *, direction: str = "LONG") -> TradePlan:
    """Derive an executable plan from the snapshot's real price."""
    fill = float(snapshot.primary.latest_close)
    timestamp = snapshot.primary.closed_candle_timestamp_ms
    candidate_id = deterministic_candidate_id("momentum_breakout", SYMBOL, direction, timestamp)
    sign = 1 if direction == "LONG" else -1
    return TradePlan(
        candidate_id=candidate_id,
        candidate_candle_open_timestamp_ms=timestamp,
        plan_id=deterministic_plan_id(candidate_id),
        symbol=SYMBOL, strategy="momentum_breakout", direction=direction,
        verdict="EXECUTABLE", score=82.0,
        entry_prices=[fill],
        stop_loss=round(fill * (1 - sign * 0.01), 4),
        take_profits=[round(fill * (1 + sign * 0.01), 4)],
        risk_reward_ratio=1.0, account_risk_pct=0.5, leverage=2.0,
        position_notional_usdt=100.0, notes=["spread_bps=2.750"], reasons=[],
    )


def _service(tmp_path) -> ForwardPaperService:
    return ForwardPaperService(
        _settings(), events_path=tmp_path / "paper.jsonl",
        outcomes_path=tmp_path / "outcomes.csv", quality_path=tmp_path / "quality.json",
        git_commit="test",
    )


def _extend(candles: list[Candle], *, close: float, high: float, low: float) -> list[Candle]:
    """Append one properly spaced candle so the factory's contract checks pass."""
    last = candles[-1]
    return candles + [
        Candle(last.timestamp_ms + STEP_MS, last.close, high, low, close, 120.0, 12_000.0)
    ]


def test_production_snapshot_opens_paper_trade_and_persists_event(tmp_path):
    """Regression: a ContractSpec in snapshot.context must not abort the write."""
    candles = _candles()
    snapshot = _production_snapshot(candles)
    # Guard the premise: the production factory really does embed live dataclasses.
    assert isinstance(snapshot.context["instrument"], ContractSpec)
    assert isinstance(snapshot.context["live"], LiveMarketContext)

    service = _service(tmp_path)
    plan = _plan(snapshot)
    service.process([plan], [snapshot])

    events = service.store.read_events()
    opened = [event for event in events if event["event_type"] == "TRADE_OPENED"]
    assert len(opened) == 1, f"expected one TRADE_OPENED, got {[e['event_type'] for e in events]}"

    payload = opened[0]["payload"]
    assert payload["symbol"] == SYMBOL
    assert payload["simulated_fill"] == pytest.approx(float(snapshot.primary.latest_close))
    # The whole payload must now be pure JSON, and survive a round trip.
    assert canonical_json(payload)
    context = payload["strategy_features"]["market_context"]
    assert context["instrument"]["symbol"] == SYMBOL
    assert "live" not in context, "redundant live blob must not be persisted"
    assert "bids" not in canonical_json(context), "raw orderbook ladder must not be persisted"


def test_full_lifecycle_survives_restart_and_records_accounted_outcome(tmp_path):
    """open -> persist -> restart -> restore -> TP -> close -> outcome."""
    candles = _candles()
    open_snapshot = _production_snapshot(candles)
    plan = _plan(open_snapshot)
    fill = float(open_snapshot.primary.latest_close)
    target = float(plan.take_profits[0])

    service = _service(tmp_path)
    service.process([plan], [open_snapshot])

    open_states = service.open_states()
    assert len(open_states) == 1
    trade_id, state = next(iter(open_states.items()))
    assert state["stop"] == pytest.approx(float(plan.stop_loss))
    assert state["targets"] == [pytest.approx(target)]
    size = state["initial_size"]
    assert size == pytest.approx(100.0 / fill)

    # Restart: a fresh service rebuilds state purely from the persisted event log.
    restarted = _service(tmp_path)
    restored = restarted.open_states()
    assert list(restored) == [trade_id], "open position must be restored exactly once"
    assert restored[trade_id]["remaining_size"] == pytest.approx(size)

    # Feed a candle that reaches the take-profit.
    tp_candles = _extend(candles, close=target, high=target + 0.5, low=fill - 0.1)
    restarted.process([], [_production_snapshot(tp_candles)])

    assert restarted.open_states() == {}, "position must be closed after TP"
    events = restarted.store.read_events()
    types = [event["event_type"] for event in events]
    assert types.count("TRADE_OPENED") == 1
    assert types.count("TP_TOUCH") == 1
    assert types.count("TRADE_CLOSED") == 1

    # TP1 closes the full size, so the realised gross lands on the partial exit
    # and the terminal close carries the zero remainder.
    closed = next(event for event in events if event["event_type"] == "TRADE_CLOSED")["payload"]
    partial = next(event for event in events if event["event_type"] == "PARTIAL_EXIT")["payload"]
    expected_gross = (target - fill) * size
    assert closed["exit_price"] == pytest.approx(target)
    assert partial["exit_size"] == pytest.approx(size)
    assert partial["gross_pnl"] + closed["gross_pnl"] == pytest.approx(expected_gross)

    outcomes = list(csv.DictReader((tmp_path / "outcomes.csv").open(encoding="utf-8")))
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome["trade_id"] == trade_id
    assert outcome["symbol"] == SYMBOL
    assert outcome["final_exit_reason"] == "TP1"
    assert float(outcome["exit_price"]) == pytest.approx(target)
    assert float(outcome["gross_pnl"]) == pytest.approx(expected_gross)
    # Fees must cover both legs of the round trip, and reduce the gross result.
    assert float(outcome["fees"]) > 0
    assert float(outcome["net_pnl"]) == pytest.approx(
        float(outcome["gross_pnl"]) - float(outcome["fees"]) + float(outcome["funding"]), abs=1e-6
    )
    assert float(outcome["net_pnl"]) < float(outcome["gross_pnl"])

    _, quality = restarted.reconstructor.reconstruct()
    assert quality["event_chain_valid"] is True
    assert quality["unresolved_open_trade_count"] == 0
    assert quality["duplicate_semantic_transition_count"] == 0


def test_break_even_stop_never_fills_beyond_the_traded_range(tmp_path):
    """Regression from the 2026-07-25 smoke run.

    With a target narrower than BREAK_EVEN_FEE_BUFFER_PCT, profit-lock moved the
    stop to 74.0049 while the candle high was 73.947. The stop was immediately
    "touched" and the trade closed as STOP_LOSS at a price the market never traded,
    booking a fabricated +0.025 profit.
    """
    candles = _candles()
    open_snapshot = _production_snapshot(candles)
    fill = float(open_snapshot.primary.latest_close)
    # Target far narrower than the break-even fee buffer, as in the live run.
    target = round(fill * 1.0002, 6)
    candidate_id = deterministic_candidate_id("smoke", SYMBOL, "LONG", open_snapshot.primary.closed_candle_timestamp_ms)
    plan = TradePlan(
        candidate_id=candidate_id,
        candidate_candle_open_timestamp_ms=open_snapshot.primary.closed_candle_timestamp_ms,
        plan_id=deterministic_plan_id(candidate_id),
        symbol=SYMBOL, strategy="smoke", direction="LONG", verdict="EXECUTABLE",
        score=100.0, entry_prices=[fill], stop_loss=round(fill * 0.95, 6),
        take_profits=[target], risk_reward_ratio=1.0, account_risk_pct=0.0,
        leverage=1.0, position_notional_usdt=25.0, notes=[], reasons=[],
    )
    settings = _settings()
    assert settings.break_even_fee_buffer_pct / 100 > 0.0002, "premise: buffer wider than target"

    service = _service(tmp_path)
    service.process([plan], [open_snapshot])

    # A candle that nudges up enough to arm profit-lock but stays below the buffer.
    high = fill * 1.00012
    nudge = _extend(candles, close=high, high=high, low=fill - 0.01)
    service.process([], [_production_snapshot(nudge)])

    events = service.store.read_events()
    traded_high = max(
        float(event["payload"]["candle_high"])
        for event in events if event["event_type"] == "MARK_DECISION"
    )
    for event in events:
        if event["event_type"] == "STOP_UPDATED":
            assert float(event["payload"]["new_stop"]) < traded_high, (
                "a protective stop must not be placed beyond the traded range"
            )
    closed = [event for event in events if event["event_type"] == "TRADE_CLOSED"]
    for event in closed:
        assert float(event["payload"]["exit_price"]) <= traded_high, (
            f"exit {event['payload']['exit_price']} exceeds traded high {traded_high}"
        )
        if event["payload"]["exit_reason"] == "STOP_LOSS":
            assert float(event["payload"]["gross_pnl"]) <= 0, (
                "a stop-loss must not book a profit"
            )


def test_stop_loss_closes_position_with_negative_net_pnl(tmp_path):
    candles = _candles()
    open_snapshot = _production_snapshot(candles)
    plan = _plan(open_snapshot)
    fill = float(open_snapshot.primary.latest_close)
    stop = float(plan.stop_loss)

    service = _service(tmp_path)
    service.process([plan], [open_snapshot])
    trade_id = next(iter(service.open_states()))

    sl_candles = _extend(candles, close=stop, high=fill + 0.1, low=stop - 0.5)
    service.process([], [_production_snapshot(sl_candles)])

    assert service.open_states() == {}
    events = service.store.read_events()
    types = [event["event_type"] for event in events]
    assert types.count("SL_TOUCH") == 1
    assert types.count("TRADE_CLOSED") == 1

    outcomes = list(csv.DictReader((tmp_path / "outcomes.csv").open(encoding="utf-8")))
    assert len(outcomes) == 1
    assert outcomes[0]["trade_id"] == trade_id
    assert outcomes[0]["final_exit_reason"] == "STOP_LOSS"
    assert float(outcomes[0]["gross_pnl"]) < 0
    assert float(outcomes[0]["fees"]) > 0
    assert float(outcomes[0]["net_pnl"]) < float(outcomes[0]["gross_pnl"]), "fees must worsen a loss"
    assert float(outcomes[0]["exit_price"]) == pytest.approx(stop)
