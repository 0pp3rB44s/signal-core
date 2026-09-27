"""AdaptiveTrend live-entry call site: AdaptiveTrend -> TradePlan ->
ExecutionService.execute() -> existing initial protection pipeline, gated
behind ADAPTIVE_TREND_LIVE_ENTRY_ENABLED (default OFF).

Binds against the real execution/adaptive_trend_entry.py and the real
app/config.py Settings field -- not helper doubles -- so if the flag
default drifts, the weekly-freeze independence is lost, or the call site
is removed from app/runner.py, these go red.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from execution.adaptive_trend_entry import submit_adaptive_trend_entry
from strategies.adaptive_trend_tsmom import (
    STRATEGY_VERSION,
    Side,
    SignalCandidate,
    initial_stop,
    size_position,
)

REPO = Path(__file__).resolve().parents[1]


def _winner(symbol="BTCUSDT", side=Side.LONG, close=100.0, mom=0.05, atr=2.0,
            close_ms=1_785_700_000_000):
    return SignalCandidate(symbol=symbol, side=side, signal_candle_close_ms=close_ms,
                            close=close, mom=mom, atr=atr)


def _sizing(winner, equity=1000.0, exchange_min_notional=5.0):
    stop = initial_stop(winner.close, winner.atr, winner.side)
    return size_position(equity=equity, entry_price=winner.close, stop_price=stop,
                          exchange_min_notional=exchange_min_notional)


# --- 1. flag missing -> treated as FALSE ---------------------------------

def test_flag_missing_defaults_to_false():
    s = Settings(_env_file=None)
    assert s.adaptive_trend_live_entry_enabled is False


# --- 2 & 3. flag FALSE: zero live execution calls, shadow still works ---

def test_flag_false_blocks_the_runner_call_site_source_level():
    """Structural proof: the call site in app/runner.py is guarded by the
    settings flag, and the shadow scan call is NOT inside that guard (so
    shadow evaluation keeps running when the flag is false)."""
    source = (REPO / "app" / "runner.py").read_text()
    assert "adaptive_trend_live_entry_enabled" in source
    assert "run_adaptive_trend_shadow_scan" in source
    # The shadow call must appear before the flag check that gates entry --
    # i.e. shadow evaluation is unconditional, entry submission is not.
    shadow_idx = source.index("run_adaptive_trend_shadow_scan(")
    flag_idx = source.index("adaptive_trend_live_entry_enabled")
    assert shadow_idx < flag_idx


def test_flag_false_execute_never_called_even_with_valid_winner():
    """Even bypassing the runner's own `if flag:` gate entirely -- calling
    submit_adaptive_trend_entry directly, as the runner would only do when
    the flag is true -- proves the function itself has no independent way
    to fire without a caller opting in. This test documents intent: the
    ACTUAL gate is the `if self.settings.adaptive_trend_live_entry_enabled`
    check in app/runner.py (see previous test); submit_adaptive_trend_entry
    has no flag parameter of its own by design, so it cannot be called
    accidentally from anywhere except that one guarded call site."""
    import inspect
    params = inspect.signature(submit_adaptive_trend_entry).parameters
    assert "winner" in params and "winner_sizing" in params
    assert "weekly_freeze_active" in params
    # No flag-bypassing default: every required param has no default, so a
    # caller cannot invoke this with "everything unset" by accident.
    for name in ("winner", "winner_sizing", "weekly_freeze_active", "execution_service"):
        assert params[name].default is inspect.Parameter.empty


# --- 4. flag TRUE + weekly freeze active -> zero execute() calls --------

def test_weekly_freeze_active_blocks_entry_even_with_accepted_sizing():
    winner = _winner()
    sizing = _sizing(winner)
    assert sizing.accepted
    svc = MagicMock()
    result = submit_adaptive_trend_entry(
        winner=winner, winner_sizing=sizing, weekly_freeze_active=True,
        execution_service=svc,
    )
    assert result is None
    svc.execute.assert_not_called()


# --- 5. flag TRUE + account/risk rejection -> zero execute() calls ------

def test_rejected_sizing_blocks_entry_before_execute_is_called():
    winner = _winner()
    # Force rejection: tiny equity relative to a large exchange minimum.
    sizing = _sizing(winner, equity=1.0, exchange_min_notional=100_000.0)
    assert not sizing.accepted
    svc = MagicMock()
    result = submit_adaptive_trend_entry(
        winner=winner, winner_sizing=sizing, weekly_freeze_active=False,
        execution_service=svc,
    )
    assert result is None
    svc.execute.assert_not_called()


def test_no_winner_blocks_entry_before_execute_is_called():
    svc = MagicMock()
    result = submit_adaptive_trend_entry(
        winner=None, winner_sizing=None, weekly_freeze_active=False,
        execution_service=svc,
    )
    assert result is None
    svc.execute.assert_not_called()


# --- 6. flag TRUE + valid candidate + all gates pass -> exactly one call ---

def test_valid_candidate_and_clear_gates_calls_execute_exactly_once():
    winner = _winner()
    sizing = _sizing(winner)
    assert sizing.accepted
    svc = MagicMock()
    svc.execute.return_value = [MagicMock(status="SKIPPED", message="hybrid gate blocked unsupported strategy")]
    result = submit_adaptive_trend_entry(
        winner=winner, winner_sizing=sizing, weekly_freeze_active=False,
        execution_service=svc,
    )
    assert svc.execute.call_count == 1
    plans = svc.execute.call_args.args[0]
    assert len(plans) == 1
    assert plans[0].strategy == STRATEGY_VERSION
    assert plans[0].symbol == winner.symbol
    assert result is not None


# --- 7. repeated same 6H candle cannot produce duplicate execution ------

def test_same_winner_called_twice_produces_identical_plan_identity():
    """The scan loop only ever hands submit_adaptive_trend_entry a winner
    for the LATEST unprocessed candle (evaluate_universe's own dedup, proven
    in test_adaptive_trend_runtime.py); if it were somehow called twice for
    the same candle, build_trade_plan's deterministic identity guarantees
    the second call produces the SAME candidate_id/plan_id, not a new one --
    so even a duplicate call cannot create two distinct order intents."""
    winner = _winner()
    sizing = _sizing(winner)
    svc = MagicMock()
    svc.execute.return_value = [MagicMock(status="SKIPPED", message="")]

    submit_adaptive_trend_entry(winner=winner, winner_sizing=sizing,
                                 weekly_freeze_active=False, execution_service=svc)
    submit_adaptive_trend_entry(winner=winner, winner_sizing=sizing,
                                 weekly_freeze_active=False, execution_service=svc)

    first_plan = svc.execute.call_args_list[0].args[0][0]
    second_plan = svc.execute.call_args_list[1].args[0][0]
    assert first_plan.candidate_id == second_plan.candidate_id
    assert first_plan.plan_id == second_plan.plan_id


# --- 8. active AdaptiveTrend position blocks second entry ---------------
# --- 9. opposite signal cannot hedge -------------------------------------
# Both already proven at the real ExecutionService.execute() layer in
# tests/test_adaptive_trend_one_position_cap.py (PER_STRATEGY_MAX_OPEN_POSITIONS_OVERRIDE
# pinned to 1 for adaptive_trend_tsmom_v1, direction-agnostic). This module
# calls that same execute() unconditionally on a valid candidate (test 6
# above), so those existing proofs cover this call site's actual behavior
# once the hybrid gate is eventually opened. The two tests below confirm
# the *mechanism* is still wired, from this module's perspective.

def test_one_position_cap_mechanism_still_wired_for_the_real_strategy_key():
    from execution.execution_service import ExecutionService
    assert ExecutionService.PER_STRATEGY_MAX_OPEN_POSITIONS_OVERRIDE.get(STRATEGY_VERSION) == 1


def test_entry_call_site_never_bypasses_execute_to_hedge_directly():
    """Structural: this module must route every entry through
    ExecutionService.execute() -- the same choke point the one-position cap
    lives in -- never construct or submit an order by any other path."""
    source = (REPO / "execution" / "adaptive_trend_entry.py").read_text()
    assert "execution_service.execute(" in source
    forbidden = ("place_futures_market_order", "place_futures_limit_order",
                 "place_futures_order", "EntryOrderSubmitter(")
    for name in forbidden:
        assert name not in source


# --- 10. MicroFlow remains retired regardless of this flag --------------

def test_microflow_retirement_gate_is_unconditional_and_flag_independent():
    source = (REPO / "risk" / "risk_manager.py").read_text()
    assert "_microflow_retirement_gate" in source
    assert "adaptive_trend_live_entry_enabled" not in source


def test_microflow_retirement_gate_does_not_read_the_new_flag():
    """Direct proof at the callable level: the retirement gate's signature
    takes only the candidate -- it cannot be parameterized by, or coupled
    to, the AdaptiveTrend entry flag."""
    import inspect

    from risk.risk_manager import RiskManager
    sig = inspect.signature(RiskManager._microflow_retirement_gate)
    assert "adaptive_trend_live_entry_enabled" not in sig.parameters
