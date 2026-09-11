"""Proves the low_vol_reclaim_v2 production-LIVE restoration path end to end.

This is an OPERATIONAL_REFERENCE_ONLY capability, not an economic claim: the
strategy's own real trade record (49 exchange-confirmed LIVE closes at SHA
cd9d8300861af7a69d67947f69fe75802eeb4b5d) showed gross ~0 bps/trade and net
-11.84 bps/trade. These tests only verify the engineering is sound and fails
closed exactly as designed -- they do not and cannot prove profitability.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.runner import route_new_entry_plans
from app.symbol_allowlist import LOW_VOL_RECLAIM_V2_APPROVED_SYMBOLS, OWNER_APPROVED_PRODUCTION_SYMBOLS


def _live(**overrides) -> Settings:
    values = {
        "EXECUTION_ENABLED": True,
        "EXECUTION_MODE": "LIVE",
        "EXECUTION_REQUIRE_CONFIRMATION": True,
        "PRODUCTION_SYMBOL_ALLOWLIST": ",".join(LOW_VOL_RECLAIM_V2_APPROVED_SYMBOLS),
        "MAX_SYMBOLS": len(LOW_VOL_RECLAIM_V2_APPROVED_SYMBOLS),
        "MAX_OPEN_POSITIONS": 2,
        "EXECUTION_MAX_PER_CYCLE": 2,
        "ALLOW_AUTO_WATCHLIST_REFRESH": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


#: Mirrors scripts/configure_low_vol_v2_live.py, the strategy's own historical
#: live-deployment script -- OLD_STRATEGIES_NEW_ENTRIES_ENABLED stays false
#: here too: it is not this strategy's own switch, it is the kill switch for
#: every OTHER legacy strategy (route_new_entry_plans exempts exactly the
#: "low_vol_reclaim_v2" strategy id regardless of that flag).
_LOW_VOL_RECLAIM_V2_SAFETY_CONFIG = {
    "EXECUTION_MARGIN_MODE": "isolated",
    "BREAK_EVEN_OPEN_FEE_FALLBACK_RATE": 0.0006,
    "BREAK_EVEN_EXPECTED_CLOSE_FEE_RATE": 0.0006,
    "BREAK_EVEN_SPREAD_BUFFER_PCT": 0.02,
    "BREAK_EVEN_SLIPPAGE_BUFFER_PCT": 0.03,
    "BREAK_EVEN_EXTRA_BUFFER_PCT": 0.01,
    "BREAK_EVEN_FEE_BUFFER_PCT": 0.12,
    "BREAK_EVEN_MARK_SAFETY_TICKS": 2,
    "EXECUTOR_ID": "runner01",
    "HOST_ID": "runner-mba01",
    "STRATEGY_ISOLATION_ENABLED": True,
    "ENABLED_STRATEGIES": "low_vol_reclaim_v2",
    "MAX_LEVERAGE": 5.0,
    "DEFAULT_LEVERAGE": 5.0,
    "OLD_STRATEGIES_NEW_ENTRIES_ENABLED": False,
    "DYNAMIC_GRID_ENABLED": False,
    "DYNAMIC_GRID_MODE": "OFF",
    "MAKER_ENTRY_FALLBACK_MARKET": False,
}


def _low_vol_reclaim_v2_live(**overrides) -> Settings:
    values = dict(_LOW_VOL_RECLAIM_V2_SAFETY_CONFIG)
    values.update(overrides)
    return _live(APP_ENV="production", **values)


# --- config validator: the new branch --------------------------------------


def test_production_live_accepts_low_vol_reclaim_v2_on_its_own_symbols():
    settings = _low_vol_reclaim_v2_live()
    assert settings.enabled_strategy_set == {"low_vol_reclaim_v2"}
    assert tuple(settings.production_symbols) == LOW_VOL_RECLAIM_V2_APPROVED_SYMBOLS
    assert len(LOW_VOL_RECLAIM_V2_APPROVED_SYMBOLS) == 8


def test_low_vol_reclaim_v2_rejects_the_newer_twelve_symbol_universe():
    """Restoring the code must not silently restore it onto an unvalidated
    symbol universe -- only 5 of the 12 current symbols overlap with the 8
    this strategy's real track record was measured on."""
    with pytest.raises(ValidationError, match="owner-approved allowlist"):
        _low_vol_reclaim_v2_live(
            PRODUCTION_SYMBOL_ALLOWLIST=",".join(OWNER_APPROVED_PRODUCTION_SYMBOLS),
            MAX_SYMBOLS=len(OWNER_APPROVED_PRODUCTION_SYMBOLS),
        )


def test_low_vol_reclaim_v2_leverage_ceiling_fails_closed():
    """5.0 is the verified historical value (app/config.py at cd9d830). Higher
    leverage was never validated for this strategy and must not be reachable
    by a mistyped or silently-defaulted setting."""
    with pytest.raises(ValidationError, match="low_vol_reclaim_v2"):
        _low_vol_reclaim_v2_live(MAX_LEVERAGE=10.0, DEFAULT_LEVERAGE=10.0)


def test_low_vol_reclaim_v2_still_requires_old_strategies_new_entries_disabled():
    """Unchanged from every other production LIVE branch: this flag is the
    kill switch for the *other* legacy strategies, not this one's own."""
    with pytest.raises(ValidationError, match="OLD_STRATEGIES_NEW_ENTRIES_ENABLED"):
        _low_vol_reclaim_v2_live(OLD_STRATEGIES_NEW_ENTRIES_ENABLED=True)


def test_production_live_error_names_all_three_strategy_sets():
    # Supply the *current* owner-approved universe so the symbol check (which
    # runs first) passes and this actually reaches the strategy-set branch.
    with pytest.raises(ValidationError, match="low_vol_reclaim_v2"):
        _low_vol_reclaim_v2_live(
            ENABLED_STRATEGIES="something_unrecognised",
            PRODUCTION_SYMBOL_ALLOWLIST=",".join(OWNER_APPROVED_PRODUCTION_SYMBOLS),
            MAX_SYMBOLS=len(OWNER_APPROVED_PRODUCTION_SYMBOLS),
        )


def test_low_vol_reclaim_v2_cannot_mix_with_microflow_or_adaptive_trend():
    with pytest.raises(ValidationError):
        _low_vol_reclaim_v2_live(ENABLED_STRATEGIES="low_vol_reclaim_v2,microflow_scalper_v1")


# --- entry routing: exactly this one strategy id, both directions ----------


def _plan(strategy: str, direction: str = "LONG", plan_id: str = "p1"):
    return SimpleNamespace(strategy=strategy, direction=direction, plan_id=plan_id)


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_low_vol_reclaim_v2_entry_routes_both_directions(direction):
    allowed, blocked = route_new_entry_plans(
        [_plan("low_vol_reclaim_v2", direction)],
        enabled_strategies={"low_vol_reclaim_v2"},
        legacy_new_entries_enabled=False,
        require_explicit_allowlist=True,
    )
    assert len(allowed) == 1 and not blocked
    assert allowed[0].direction == direction


@pytest.mark.parametrize(
    "other_strategy",
    ["momentum_breakout", "momentum_breakdown", "trend_continuation",
     "liquidity_sweep_reversal", "microflow_scalper_v1", "adaptive_trend_tsmom_v1"],
)
def test_only_low_vol_reclaim_v2_is_restored_not_the_wider_family_or_retired_strategies(other_strategy):
    """The selector still evaluates momentum_breakout/breakdown, trend
    continuation and liquidity-sweep-reversal every cycle for observability,
    exactly as it does today in shadow -- restoring low_vol_reclaim_v2 must
    not also open a live entry path for them, and must not let a retired
    strategy back in under the same allowlist."""
    allowed, blocked = route_new_entry_plans(
        [_plan(other_strategy)],
        enabled_strategies={"low_vol_reclaim_v2"},
        legacy_new_entries_enabled=False,
        require_explicit_allowlist=True,
    )
    assert not allowed
    assert len(blocked) == 1


def test_stale_or_missing_strategy_id_is_blocked():
    allowed, blocked = route_new_entry_plans(
        [_plan("")],
        enabled_strategies={"low_vol_reclaim_v2"},
        legacy_new_entries_enabled=False,
        require_explicit_allowlist=True,
    )
    assert not allowed
    assert blocked[0][1] == "missing_strategy_id"
