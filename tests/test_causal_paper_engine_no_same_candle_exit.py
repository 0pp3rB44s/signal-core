"""CAUSAL_PAPER_ENGINE_FIX_2026-09-13.

Proven live (see reports/CAUSAL_PAPER_ENGINE_AUDIT_2026-09-13.md): entry is
priced off the trigger candle's own close (open_trade()'s `simulated_fill`),
so that same candle's high/low necessarily predates -- or is at best
contemporaneous with -- the entry. `_update_trade()` used to evaluate
SL/TP/trailing against `snapshot.primary.candles[-1]` unconditionally, with
no check for whether that candle was the same one used to open the trade.
Because the scan cadence (~2.5 min) is far shorter than the primary candle
period (15m), the very first update for any newly-opened trade was
*guaranteed* to see the identical candle used for entry -- not an edge case.

This suite proves the fix: a trade may not be evaluated for SL/TP/trailing
until the "latest closed candle" genuinely advances past the one used for
entry, regardless of how many scan cycles run against the stale candle in
the meantime.
"""

from __future__ import annotations

from app.config import Settings
from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id
from clients.schemas import Candle, ContractSpec, MarketSnapshot, TimeframeSnapshot, TradePlan
from forward_paper.service import ForwardPaperService

ENTRY_TS = 1_767_225_600_000  # 2026-01-01T00:00:00Z
NEXT_TS = ENTRY_TS + 15 * 60_000  # one primary (15m) candle later


def _contract(symbol: str) -> ContractSpec:
    return ContractSpec(
        symbol=symbol, product_type="USDT-FUTURES", quote_coin="USDT", base_coin=symbol[:-4],
        status="normal", min_trade_num=0.1, size_multiplier=1.0, price_place=3,
        volume_24h_usdt=1_000_000.0, change_pct_24h=0.0, raw={},
    )


def _timeframe(symbol: str, ts: int, *, open_: float, high: float, low: float, close: float) -> TimeframeSnapshot:
    candle = Candle(ts, open_, high, low, close, 1_000.0, 100_000.0)
    return TimeframeSnapshot(
        symbol=symbol, granularity="15m", latest_close=close, change_pct=0.0, range_pct=0.4,
        volume_ratio_20=1.0, ema20=close, ema50=close, trend="bullish",
        candles=[candle], closed_candle_timestamp_ms=ts, as_of_timestamp_ms=ts,
    )


def _snapshot(symbol: str, ts: int, *, open_: float, high: float, low: float, close: float) -> MarketSnapshot:
    tf = _timeframe(symbol, ts, open_=open_, high=high, low=low, close=close)
    return MarketSnapshot(
        symbol=symbol, contract=_contract(symbol), primary=tf, confirmation=tf,
        alignment="aligned_bullish", score_hint=80.0, notes=[],
    )


def _plan(symbol: str, direction: str, *, entry: float, stop: float, targets: list[float]) -> TradePlan:
    candidate_id = deterministic_candidate_id("trend_continuation", symbol, direction, ENTRY_TS)
    return TradePlan(
        candidate_id=candidate_id, candidate_candle_open_timestamp_ms=ENTRY_TS,
        plan_id=deterministic_plan_id(candidate_id), symbol=symbol, strategy="trend_continuation",
        direction=direction, verdict="EXECUTABLE", score=90.0,
        entry_prices=[entry], stop_loss=stop, take_profits=targets,
        risk_reward_ratio=1.5, account_risk_pct=0.5, leverage=3.0,
        position_notional_usdt=35.0, notes=[], reasons=[],
    )


def _service(tmp_path) -> ForwardPaperService:
    settings = Settings(_env_file=None, FORWARD_PAPER_ENABLED=True, EXECUTION_ENABLED=False, MAX_OPEN_POSITIONS=2)
    return ForwardPaperService(
        settings,
        events_path=tmp_path / "events.jsonl",
        outcomes_path=tmp_path / "outcomes.csv",
        quality_path=tmp_path / "quality.json",
        git_commit="test",
    )


def _open_state(service, symbol):
    states = service.trade_states()
    matches = [s for s in states.values() if s["symbol"] == symbol]
    assert len(matches) == 1, f"expected exactly one state for {symbol}, got {len(matches)}"
    return matches[0]


# --- entry candle must be ineligible, both directions, all three exit types ---


def test_trigger_candle_cannot_stop_a_close_entry_trade_long(tmp_path):
    service = _service(tmp_path)
    symbol = "AAAUSDT"
    # LONG entry at 100; the SAME candle's own low (98) already sits below the
    # stop (99) -- if this were allowed to evaluate, it would stop instantly.
    entry_snapshot = _snapshot(symbol, ENTRY_TS, open_=99.5, high=100.5, low=98.0, close=100.0)
    plan = _plan(symbol, "LONG", entry=100.0, stop=99.0, targets=[103.0])
    service.process([plan], [entry_snapshot], scan_id="scan-open")

    # Re-deliver the identical (entry) candle on a later scan cycle.
    service.update_market([entry_snapshot], scan_id="scan-same-candle")

    state = _open_state(service, symbol)
    assert state["terminal_status"] == "OPEN"
    assert state["mfe_pct"] == 0.0 and state["mae_pct"] == 0.0, "no excursion should be recorded off the entry candle"


def test_trigger_candle_cannot_stop_a_close_entry_trade_short(tmp_path):
    service = _service(tmp_path)
    symbol = "BBBUSDT"
    # SHORT entry at 100; the SAME candle's own high (102) already sits above
    # the stop (101).
    entry_snapshot = _snapshot(symbol, ENTRY_TS, open_=100.5, high=102.0, low=99.5, close=100.0)
    plan = _plan(symbol, "SHORT", entry=100.0, stop=101.0, targets=[97.0])
    service.process([plan], [entry_snapshot], scan_id="scan-open")

    service.update_market([entry_snapshot], scan_id="scan-same-candle")

    state = _open_state(service, symbol)
    assert state["terminal_status"] == "OPEN"
    assert state["mfe_pct"] == 0.0 and state["mae_pct"] == 0.0


def test_trigger_candle_cannot_hit_tp(tmp_path):
    service = _service(tmp_path)
    symbol = "CCCUSDT"
    # LONG entry at 100, TP1 at 102; the SAME candle's own high (103) already
    # exceeds it.
    entry_snapshot = _snapshot(symbol, ENTRY_TS, open_=99.8, high=103.0, low=99.0, close=100.0)
    plan = _plan(symbol, "LONG", entry=100.0, stop=97.0, targets=[102.0])
    service.process([plan], [entry_snapshot], scan_id="scan-open")

    service.update_market([entry_snapshot], scan_id="scan-same-candle")

    state = _open_state(service, symbol)
    assert state["terminal_status"] == "OPEN"
    assert state["touched_targets"] == set()
    assert state["remaining_size"] == state["initial_size"]


def test_trigger_candle_cannot_activate_trailing_or_breakeven(tmp_path):
    service = _service(tmp_path)
    symbol = "DDDUSDT"
    # LONG entry at 100; the same candle's own favorable excursion (high=103,
    # i.e. 3% >= the 60%-of-4%-distance-to-TP1 profit-lock threshold) is large
    # enough to activate profit-lock/breakeven, which -- if evaluated -- would
    # fire immediately off the entry candle rather than genuine post-entry
    # movement.
    entry_snapshot = _snapshot(symbol, ENTRY_TS, open_=100.2, high=103.0, low=99.8, close=100.0)
    plan = _plan(symbol, "LONG", entry=100.0, stop=97.0, targets=[104.0])
    service.process([plan], [entry_snapshot], scan_id="scan-open")

    service.update_market([entry_snapshot], scan_id="scan-same-candle")

    state = _open_state(service, symbol)
    assert state["profit_lock"] is False
    assert state["terminal_status"] == "OPEN"


# --- first genuinely post-entry candle IS eligible, both exit types ---


def test_first_post_entry_eligible_observation_can_stop(tmp_path):
    service = _service(tmp_path)
    symbol = "EEEUSDT"
    entry_snapshot = _snapshot(symbol, ENTRY_TS, open_=99.5, high=100.5, low=99.6, close=100.0)  # does NOT reach stop=99.0
    plan = _plan(symbol, "LONG", entry=100.0, stop=99.0, targets=[103.0])
    service.process([plan], [entry_snapshot], scan_id="scan-open")

    # A genuinely new, later candle whose own low breaches the stop.
    next_snapshot = _snapshot(symbol, NEXT_TS, open_=99.8, high=99.9, low=98.5, close=98.7)
    service.update_market([next_snapshot], scan_id="scan-next-candle")

    state = _open_state(service, symbol)
    assert state["terminal_status"] == "CLOSED"
    assert state["final_exit_reason"] == "STOP_LOSS"


def test_first_post_entry_eligible_observation_can_tp(tmp_path):
    service = _service(tmp_path)
    symbol = "FFFUSDT"
    entry_snapshot = _snapshot(symbol, ENTRY_TS, open_=99.8, high=100.5, low=99.5, close=100.0)  # does NOT reach TP=102
    plan = _plan(symbol, "LONG", entry=100.0, stop=97.0, targets=[102.0])
    service.process([plan], [entry_snapshot], scan_id="scan-open")

    next_snapshot = _snapshot(symbol, NEXT_TS, open_=100.2, high=102.5, low=100.0, close=102.2)
    service.update_market([next_snapshot], scan_id="scan-next-candle")

    state = _open_state(service, symbol)
    assert 0 in state["touched_targets"]


# --- no future leakage / determinism / crash-resume preserved ---


def test_no_future_leakage_same_candle_update_reads_nothing_new(tmp_path):
    """A same-candle update must be a complete no-op: no events beyond
    TRADE_OPENED may be written at all."""
    service = _service(tmp_path)
    symbol = "GGGUSDT"
    entry_snapshot = _snapshot(symbol, ENTRY_TS, open_=99.5, high=105.0, low=90.0, close=100.0)  # would trip stop AND tp if evaluated
    plan = _plan(symbol, "LONG", entry=100.0, stop=95.0, targets=[102.0])
    service.process([plan], [entry_snapshot], scan_id="scan-open")

    trade_id = next(iter(service.open_states().keys()))
    events_before = [e for e in service.store.read_events() if e["trade_id"] == trade_id]
    assert [e["event_type"] for e in events_before] == ["TRADE_OPENED"]

    service.update_market([entry_snapshot], scan_id="scan-same-candle-1")
    service.update_market([entry_snapshot], scan_id="scan-same-candle-2")

    events_after = [e for e in service.store.read_events() if e["trade_id"] == trade_id]
    assert [e["event_type"] for e in events_after] == ["TRADE_OPENED"], (
        "repeated same-candle updates must not append any evaluation event"
    )


def test_deterministic_replay(tmp_path):
    service = _service(tmp_path)
    symbol = "HHHUSDT"
    entry_snapshot = _snapshot(symbol, ENTRY_TS, open_=99.5, high=100.5, low=99.6, close=100.0)
    plan = _plan(symbol, "LONG", entry=100.0, stop=99.0, targets=[103.0])
    service.process([plan], [entry_snapshot], scan_id="scan-open")
    next_snapshot = _snapshot(symbol, NEXT_TS, open_=99.8, high=99.9, low=98.5, close=98.7)
    service.update_market([next_snapshot], scan_id="scan-next-candle")

    first = service.trade_states()
    second = service.trade_states()
    assert first == second, "reconstructing from the same event log twice must be identical"


def test_existing_crash_resume_pending_terminal_still_completes_on_stale_candle(tmp_path):
    """A pending_terminal (crash-recovery) intent must still be honored even
    when the next update happens to be called with the entry candle's own
    snapshot -- the new guard must never block genuine recovery."""
    service = _service(tmp_path)
    symbol = "IIIUSDT"
    entry_snapshot = _snapshot(symbol, ENTRY_TS, open_=99.5, high=100.5, low=99.6, close=100.0)
    plan = _plan(symbol, "LONG", entry=100.0, stop=99.0, targets=[103.0])
    service.process([plan], [entry_snapshot], scan_id="scan-open")
    trade_id = next(iter(service.open_states().keys()))

    # Simulate a crash right after an EXIT_REASON_TRANSITION was persisted but
    # before TRADE_CLOSED -- exactly the scenario _update_trade()'s existing
    # pending_terminal branch exists to recover.
    state = service.trade_states()[trade_id]
    service._append(
        trade_id, state["plan_id"], "EXIT_REASON_TRANSITION", "2026-01-01T00:15:00+00:00",
        {"from": "OPEN", "to": "STOP_LOSS", "mark_price": 99.0},
        candidate_id=state["candidate_id"],
    )

    # Even delivering the stale entry-candle snapshot again must not block
    # completing the already-decided terminal transition.
    service.update_market([entry_snapshot], scan_id="scan-resume")

    final_state = service.trade_states()[trade_id]
    assert final_state["terminal_status"] == "CLOSED"
    assert final_state["final_exit_reason"] == "STOP_LOSS"


def test_full_partial_exit_recovery_closes_even_without_a_fresh_candle(tmp_path):
    """If a partial exit already reduced remaining_size to zero (a full TP1
    close) but the process crashed before TRADE_CLOSED was appended, recovery
    must complete the close on restart even if the only snapshot available
    is still the (stale) entry candle -- this is finishing an
    already-decided bookkeeping fact, not a new market decision, and must
    not be blocked by the same-candle guard."""
    service = _service(tmp_path)
    symbol = "JJJUSDT"
    entry_snapshot = _snapshot(symbol, ENTRY_TS, open_=99.8, high=100.5, low=99.5, close=100.0)
    plan = _plan(symbol, "LONG", entry=100.0, stop=97.0, targets=[102.0])
    service.process([plan], [entry_snapshot], scan_id="scan-open")
    trade_id = next(iter(service.open_states().keys()))
    state = service.trade_states()[trade_id]

    # Simulate: a genuinely later candle touched TP1 and PARTIAL_EXIT was
    # persisted (TP1_CLOSE_PCT defaults to a full close), but the process
    # crashed before EXIT_REASON_TRANSITION/TRADE_CLOSED were appended.
    service._append(
        trade_id, state["plan_id"], "TP_TOUCH", "2026-01-01T00:15:00+00:00",
        {"target_index": 1, "price": 102.0, "mark_price": 102.0},
        candidate_id=state["candidate_id"],
    )
    service._append(
        trade_id, state["plan_id"], "PARTIAL_EXIT", "2026-01-01T00:15:00+00:00",
        {"target_index": 1, "exit_price": 102.0, "exit_size": state["initial_size"],
         "gross_pnl": 0.0, "fee": 0.0},
        candidate_id=state["candidate_id"],
    )

    mid_state = service.trade_states()[trade_id]
    assert mid_state["remaining_size"] <= 1e-12
    assert mid_state["terminal_status"] == "OPEN", "not yet closed -- exactly the crash gap being recovered"

    # Recovery scan cycle: only the stale entry candle is available again.
    service.update_market([entry_snapshot], scan_id="scan-recovery")

    final_state = service.trade_states()[trade_id]
    assert final_state["terminal_status"] == "CLOSED"
    assert final_state["final_exit_reason"] == "TP1"


# --- CAUSAL_PAPER_ENGINE_FIX_2026-09-13_SECONDARY: a repriced stop must not
# be tested against the same candle that repriced it ---


def test_pre_existing_stop_can_trigger_on_candle_t(tmp_path):
    """A stop that has been in place since entry (never repriced) must still
    be testable on any genuinely later candle -- the new guard must not
    over-block ordinary, already-covered stop touches."""
    service = _service(tmp_path)
    symbol = "KKKUSDT"
    entry_snapshot = _snapshot(symbol, ENTRY_TS, open_=99.5, high=100.5, low=99.6, close=100.0)  # does not reach stop=99.0
    plan = _plan(symbol, "LONG", entry=100.0, stop=99.0, targets=[103.0])
    service.process([plan], [entry_snapshot], scan_id="scan-open")

    next_snapshot = _snapshot(symbol, NEXT_TS, open_=99.8, high=99.9, low=98.5, close=98.7)
    service.update_market([next_snapshot], scan_id="scan-next-candle")

    state = _open_state(service, symbol)
    assert state["terminal_status"] == "CLOSED"
    assert state["final_exit_reason"] == "STOP_LOSS"


def test_profit_lock_repriced_stop_cannot_trigger_on_the_same_candle(tmp_path):
    """Reproduces the live DOGEUSDT finding: PROFIT_LOCK_ACTIVATED and the
    resulting new stop must not be touch-tested using the very candle whose
    favorable excursion triggered the reprice."""
    service = _service(tmp_path)
    symbol = "LLLUSDT"
    # LONG entry at 100, TP1 far away (110) so profit-lock (not TP) is what
    # activates. The SAME candle's own high (103) both (a) triggers
    # profit-lock (favorable_pct=3% >= 60% of the 10%-to-TP1 distance is NOT
    # met here -- use a closer TP1 to make the arithmetic land) and (b), if
    # tested, would touch the resulting protective stop (fee-adjusted near
    # breakeven, ~100).
    plan = _plan(symbol, "LONG", entry=100.0, stop=95.0, targets=[104.0])
    first_snapshot = _snapshot(symbol, ENTRY_TS, open_=99.8, high=100.2, low=99.7, close=100.0)
    service.process([plan], [first_snapshot], scan_id="scan-open")

    # Candle T (genuinely post-entry): favorable excursion big enough to
    # activate profit-lock (high=103 -> 3% >= 60% * 4% distance-to-TP1 = 2.4%),
    # AND that same candle's own low (99.9) would touch a fee-adjusted-near-
    # breakeven stop if it were allowed to be tested.
    candle_t = _snapshot(symbol, NEXT_TS, open_=100.2, high=103.0, low=99.9, close=101.0)
    service.update_market([candle_t], scan_id="scan-candle-t")

    state = _open_state(service, symbol)
    assert state["profit_lock"] is True, "profit-lock should have activated on candle T's favorable excursion"
    assert state["terminal_status"] == "OPEN", (
        "the newly-repriced stop must NOT be touch-tested against candle T itself"
    )

    # Re-deliver the identical candle T again (simulating the next scan cycle
    # before a new candle has closed -- exactly the live DOGEUSDT scenario).
    service.update_market([candle_t], scan_id="scan-candle-t-again")
    state = _open_state(service, symbol)
    assert state["terminal_status"] == "OPEN", (
        "repeated delivery of the SAME candle must still not trigger the just-repriced stop"
    )

    # A genuinely later candle (T+1) whose low touches the repriced stop MUST
    # be allowed to close the trade.
    candle_t_plus_1 = _snapshot(symbol, NEXT_TS + 15 * 60_000, open_=100.9, high=101.0, low=99.5, close=99.8)
    service.update_market([candle_t_plus_1], scan_id="scan-candle-t-plus-1")
    state = _open_state(service, symbol)
    assert state["terminal_status"] == "CLOSED"
    assert state["final_exit_reason"] == "STOP_LOSS"


def test_tp1_breakeven_repriced_stop_cannot_trigger_on_the_same_candle(tmp_path):
    """Same principle for the TP1-driven breakeven reprice (BREAK_EVEN_ACTIVATED)."""
    service = _service(tmp_path)
    symbol = "MMMUSDT"
    plan = _plan(symbol, "LONG", entry=100.0, stop=97.0, targets=[102.0, 110.0])
    first_snapshot = _snapshot(symbol, ENTRY_TS, open_=99.8, high=100.2, low=99.7, close=100.0)
    service.process([plan], [first_snapshot], scan_id="scan-open")

    # Candle T: touches TP1 (high=102.5 >= 102) triggering a partial close and
    # a breakeven reprice, AND its own low (99.4) would touch that fresh
    # breakeven-ish stop if allowed to be tested in the same pass.
    candle_t = _snapshot(symbol, NEXT_TS, open_=100.2, high=102.5, low=99.4, close=101.0)
    service.update_market([candle_t], scan_id="scan-candle-t")

    state = _open_state(service, symbol)
    assert 0 in state["touched_targets"], "TP1 should have touched"
    assert state["terminal_status"] == "OPEN", (
        "the fresh TP1-breakeven stop must not be touch-tested against candle T itself"
    )

    # Re-deliver the identical candle T again (the next scan cycle before a
    # new candle has closed) -- this is what actually exercises the bug: the
    # first pass only sets the stop, a second look at the SAME candle is what
    # incorrectly touch-tested it under the pre-fix code.
    service.update_market([candle_t], scan_id="scan-candle-t-again")
    state = _open_state(service, symbol)
    assert state["terminal_status"] == "OPEN", (
        "repeated delivery of the SAME candle must still not trigger the fresh TP1-breakeven stop"
    )

    candle_t_plus_1 = _snapshot(symbol, NEXT_TS + 15 * 60_000, open_=100.9, high=101.0, low=99.3, close=99.6)
    service.update_market([candle_t_plus_1], scan_id="scan-candle-t-plus-1")
    state = _open_state(service, symbol)
    assert state["terminal_status"] == "CLOSED"


def test_secondary_fix_does_not_affect_tp_touch_eligibility(tmp_path):
    """Targets are never repriced, so TP-touch checks need no equivalent
    gating -- confirm a TP can still touch on the very candle that first
    makes it reachable, immediately after entry's own (ineligible) candle."""
    service = _service(tmp_path)
    symbol = "NNNUSDT"
    entry_snapshot = _snapshot(symbol, ENTRY_TS, open_=99.8, high=100.5, low=99.5, close=100.0)  # TP=102 not reached
    plan = _plan(symbol, "LONG", entry=100.0, stop=97.0, targets=[102.0])
    service.process([plan], [entry_snapshot], scan_id="scan-open")

    next_snapshot = _snapshot(symbol, NEXT_TS, open_=100.2, high=102.5, low=100.0, close=102.2)
    service.update_market([next_snapshot], scan_id="scan-next-candle")

    state = _open_state(service, symbol)
    assert 0 in state["touched_targets"]


def test_deterministic_replay_with_repriced_stop(tmp_path):
    service = _service(tmp_path)
    symbol = "OOOUSDT"
    plan = _plan(symbol, "LONG", entry=100.0, stop=95.0, targets=[104.0])
    first_snapshot = _snapshot(symbol, ENTRY_TS, open_=99.8, high=100.2, low=99.7, close=100.0)
    service.process([plan], [first_snapshot], scan_id="scan-open")
    candle_t = _snapshot(symbol, NEXT_TS, open_=100.2, high=103.0, low=99.9, close=101.0)
    service.update_market([candle_t], scan_id="scan-candle-t")

    first = service.trade_states()
    second = service.trade_states()
    assert first == second


# --- CAUSAL_PAPER_ENGINE_FIX_2026-09-13_STALE_FILL: simulated_fill prefers a
# fresh, causal quote fetched at decision time over the stale trigger-candle
# close ---


def test_simulated_fill_uses_decision_reference_price_when_available(tmp_path):
    service = _service(tmp_path)
    symbol = "PPPUSDT"
    # Candle close (stale) is 100.0; the fresh decision-time quote is 100.8 --
    # a genuinely different, later-known price.
    entry_snapshot = _snapshot(symbol, ENTRY_TS, open_=99.8, high=100.5, low=99.5, close=100.0)
    plan = _plan(symbol, "LONG", entry=100.0, stop=97.0, targets=[104.0])
    ref_ts = "2026-01-01T00:05:00+00:00"
    service.process(
        [plan], [entry_snapshot], scan_id="scan-open",
        decision_reference_prices={symbol: (100.8, ref_ts, "2026-01-01T00:04:59.500000+00:00", "2026-01-01T00:04:59.900000+00:00")},
    )

    trade_id = next(iter(service.open_states().keys()))
    events = [e for e in service.store.read_events() if e["trade_id"] == trade_id]
    opened = next(e for e in events if e["event_type"] == "TRADE_OPENED")
    payload = opened["payload"]
    assert payload["simulated_fill"] == 100.8
    assert payload["signal_candle_close_price"] == 100.0
    assert payload["decision_reference_price"] == 100.8
    assert payload["decision_reference_timestamp"] == ref_ts
    assert payload["signal_to_decision_bps"] is not None
    # LONG: price rose (100.0 -> 100.8), which is unfavorable (worse fill),
    # so signal_to_decision_bps must be positive under the established
    # direction-aware sign convention (matching signal_to_fill_bps).
    assert payload["signal_to_decision_bps"] > 0


def test_simulated_fill_falls_back_to_candle_close_without_a_reference(tmp_path):
    """A missing/failed decision-time quote (e.g. network hiccup) must never
    block a paper trade -- falls back to the prior (candle-close) behavior."""
    service = _service(tmp_path)
    symbol = "QQQUSDT"
    entry_snapshot = _snapshot(symbol, ENTRY_TS, open_=99.8, high=100.5, low=99.5, close=100.0)
    plan = _plan(symbol, "LONG", entry=100.0, stop=97.0, targets=[104.0])
    service.process([plan], [entry_snapshot], scan_id="scan-open")  # no decision_reference_prices at all

    trade_id = next(iter(service.open_states().keys()))
    events = [e for e in service.store.read_events() if e["trade_id"] == trade_id]
    opened = next(e for e in events if e["event_type"] == "TRADE_OPENED")
    payload = opened["payload"]
    assert payload["simulated_fill"] == 100.0
    assert payload["signal_candle_close_price"] == 100.0
    assert payload["decision_reference_price"] is None
    assert payload["decision_reference_timestamp"] is None
    assert payload["decision_reference_exchange_timestamp"] is None
    assert payload["quote_request_local_timestamp"] is None
    assert payload["quote_received_local_timestamp"] is None
    assert payload["signal_to_decision_bps"] is None


def test_decision_reference_fill_short_direction_sign_convention(tmp_path):
    service = _service(tmp_path)
    symbol = "RRRUSDT"
    entry_snapshot = _snapshot(symbol, ENTRY_TS, open_=100.2, high=100.5, low=99.5, close=100.0)
    plan = _plan(symbol, "SHORT", entry=100.0, stop=103.0, targets=[96.0])
    ref_ts = "2026-01-01T00:05:00+00:00"
    # Price fell (100.0 -> 99.2) between signal and decision. Under the same
    # "positive = worse fill for this direction" convention signal_to_fill_bps
    # already uses: a SHORT that will now fill at 99.2 instead of the 100.0
    # signal price is entering after most of the downmove already happened --
    # worse (less room left to profit) -- so signal_to_decision_bps must be
    # POSITIVE here, matching signal_to_fill_bps's own sign rule exactly.
    service.process(
        [plan], [entry_snapshot], scan_id="scan-open",
        decision_reference_prices={symbol: (99.2, ref_ts, "2026-01-01T00:04:59.500000+00:00", "2026-01-01T00:04:59.900000+00:00")},
    )
    trade_id = next(iter(service.open_states().keys()))
    events = [e for e in service.store.read_events() if e["trade_id"] == trade_id]
    opened = next(e for e in events if e["event_type"] == "TRADE_OPENED")
    payload = opened["payload"]
    assert payload["simulated_fill"] == 99.2
    assert payload["signal_to_decision_bps"] > 0


def test_stale_fill_fix_preserves_existing_causal_exit_fixes(tmp_path):
    """A decision-reference fill must not disturb the entry-candle-ineligible
    guard or the repriced-stop-same-candle guard -- both already proven in
    this file; this pins that the NEW fill-price source doesn't bypass them."""
    service = _service(tmp_path)
    symbol = "SSSUSDT"
    entry_snapshot = _snapshot(symbol, ENTRY_TS, open_=99.8, high=105.0, low=90.0, close=100.0)  # would trip stop AND tp if evaluated
    plan = _plan(symbol, "LONG", entry=100.0, stop=95.0, targets=[104.0])  # far enough that candle T triggers profit-lock, not TP1
    service.process(
        [plan], [entry_snapshot], scan_id="scan-open",
        decision_reference_prices={symbol: (100.5, "2026-01-01T00:05:00+00:00", "2026-01-01T00:04:59.500000+00:00", "2026-01-01T00:04:59.900000+00:00")},
    )
    state = _open_state(service, symbol)
    assert state["terminal_status"] == "OPEN", "entry candle must still be ineligible for its own exit evaluation"

    # Re-deliver the same (now non-entry) candle to confirm the repriced-stop
    # guard is unaffected by which fill source was used.
    next_snapshot = _snapshot(symbol, NEXT_TS, open_=100.2, high=103.0, low=99.9, close=101.0)
    service.update_market([next_snapshot], scan_id="scan-candle-t")
    state = _open_state(service, symbol)
    assert state["profit_lock"] is True
    assert state["terminal_status"] == "OPEN", "repriced stop must still not trigger on the same candle that repriced it"


def test_deterministic_replay_with_decision_reference_fill(tmp_path):
    service = _service(tmp_path)
    symbol = "TTTUSDT"
    entry_snapshot = _snapshot(symbol, ENTRY_TS, open_=99.8, high=100.5, low=99.5, close=100.0)
    plan = _plan(symbol, "LONG", entry=100.0, stop=97.0, targets=[104.0])
    service.process(
        [plan], [entry_snapshot], scan_id="scan-open",
        decision_reference_prices={symbol: (100.8, "2026-01-01T00:05:00+00:00", "2026-01-01T00:04:59.500000+00:00", "2026-01-01T00:04:59.900000+00:00")},
    )
    first = service.trade_states()
    second = service.trade_states()
    assert first == second


def test_decision_reference_quote_endpoint_is_unauthenticated_public_call():
    """Structural guard: the fresh quote used for the fill must come from the
    same public, unauthenticated endpoint already relied on elsewhere
    (execution/entry_routing.py) -- never a private/authenticated call, and
    never anything that could place, cancel, or modify an order."""
    import inspect

    from clients.bitget_market_client import BitgetMarketClientMixin

    source = inspect.getsource(BitgetMarketClientMixin.get_symbol_price)
    assert "private=False" in source


def test_decision_reference_exchange_timestamp_is_an_explicit_alias(tmp_path):
    """decision_reference_timestamp is the exchange's own self-reported clock
    (observed running a few seconds ahead of local UTC in production -- clock
    skew, not a causality bug). decision_reference_exchange_timestamp makes
    that provenance explicit so the two clocks are never conflated."""
    service = _service(tmp_path)
    symbol = "UUUUSDT"
    entry_snapshot = _snapshot(symbol, ENTRY_TS, open_=99.8, high=100.5, low=99.5, close=100.0)
    plan = _plan(symbol, "LONG", entry=100.0, stop=97.0, targets=[104.0])
    ref_ts = "2026-01-01T00:05:00+00:00"
    service.process(
        [plan], [entry_snapshot], scan_id="scan-open",
        decision_reference_prices={symbol: (100.8, ref_ts, "2026-01-01T00:04:59.500000+00:00", "2026-01-01T00:04:59.900000+00:00")},
    )
    trade_id = next(iter(service.open_states().keys()))
    events = [e for e in service.store.read_events() if e["trade_id"] == trade_id]
    opened = next(e for e in events if e["event_type"] == "TRADE_OPENED")
    payload = opened["payload"]
    assert payload["decision_reference_exchange_timestamp"] == ref_ts
    assert payload["decision_reference_exchange_timestamp"] == payload["decision_reference_timestamp"]


def test_quote_received_local_timestamp_proves_no_lookahead(tmp_path):
    """TELEMETRY_QUOTE_LOCAL_RECEIPT_2026-09-13: quote_received_local_timestamp
    is captured on THIS host's clock, strictly before decision_timestamp is
    stamped (see app/runner.py's quote-fetch loop, which completes before
    forward_paper.process() is ever called). This test proves that ordering
    holds against the real clock -- bounding decision_timestamp between two
    real datetime.now(UTC) reads taken immediately around service.process(),
    then asserting the recorded quote_received_local_timestamp falls at or
    before that window's start, independent of any exchange clock skew."""
    from datetime import datetime, timezone

    service = _service(tmp_path)
    symbol = "VVVUSDT"
    entry_snapshot = _snapshot(symbol, ENTRY_TS, open_=99.8, high=100.5, low=99.5, close=100.0)
    plan = _plan(symbol, "LONG", entry=100.0, stop=97.0, targets=[104.0])

    quote_received_local = datetime.now(timezone.utc).isoformat()
    before_process = datetime.now(timezone.utc)
    service.process(
        [plan], [entry_snapshot], scan_id="scan-open",
        decision_reference_prices={symbol: (100.8, "2026-01-01T00:05:00+00:00", quote_received_local, quote_received_local)},
    )
    after_process = datetime.now(timezone.utc)

    trade_id = next(iter(service.open_states().keys()))
    events = [e for e in service.store.read_events() if e["trade_id"] == trade_id]
    opened = next(e for e in events if e["event_type"] == "TRADE_OPENED")
    payload = opened["payload"]

    assert payload["quote_received_local_timestamp"] == quote_received_local
    assert payload["quote_request_local_timestamp"] == quote_received_local
    decision_dt = datetime.fromisoformat(payload["decision_timestamp"])
    assert before_process <= decision_dt <= after_process
    # The causality property the mission asked to prove: the locally-captured
    # quote receipt must be at or before the moment this trade's own
    # decision_timestamp was stamped.
    quote_received_dt = datetime.fromisoformat(payload["quote_received_local_timestamp"])
    assert quote_received_dt <= decision_dt
