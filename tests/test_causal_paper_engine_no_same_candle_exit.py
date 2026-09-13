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
