"""app/config.py's production-LIVE model_validator: a THIRD guard layer
(alongside ExecutionService.execute()'s hybrid gate and
scripts/lib/env_guard.sh) that also hard-coded "allowlist must be exactly
microflow_scalper_v1" -- discovered only when the owner actually tried to
launch with ENABLED_STRATEGIES=adaptive_trend_tsmom_v1: Settings() itself
raised at process startup, crash-looping launchd (KeepAlive) every ~60s
until the process was stopped. No exposure occurred (Settings() fails
before any exchange client is constructed), but the loop needed fixing
here, not just in the shell guard.

Binds against the real Settings model_validator, same base-config pattern
as tests/test_production_symbol_allowlist.py.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.symbol_allowlist import OWNER_APPROVED_PRODUCTION_SYMBOLS


def _live(**overrides) -> Settings:
    values = {
        "EXECUTION_ENABLED": True,
        "EXECUTION_MODE": "LIVE",
        "EXECUTION_REQUIRE_CONFIRMATION": True,
        "PRODUCTION_SYMBOL_ALLOWLIST": "BTCUSDT,SOLUSDT,SUIUSDT",
        "MAX_SYMBOLS": 3,
        "MAX_OPEN_POSITIONS": 2,
        "EXECUTION_MAX_PER_CYCLE": 2,
        "ALLOW_AUTO_WATCHLIST_REFRESH": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


_PRODUCTION_COMMON_SAFETY_CONFIG = {
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
    "OLD_STRATEGIES_NEW_ENTRIES_ENABLED": False,
    "DYNAMIC_GRID_ENABLED": False,
    "DYNAMIC_GRID_MODE": "OFF",
    "MAKER_ENTRY_FALLBACK_MARKET": False,
}

_MICROFLOW_CONFIG = {
    "ENABLED_STRATEGIES": "microflow_scalper_v1",
    "MICROFLOW_SCALPER_ENABLED": True,
    "MICROFLOW_SYMBOLS": "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT,LINKUSDT,AVAXUSDT,SUIUSDT,HYPEUSDT,ZECUSDT,NEARUSDT",
    "MICROFLOW_LEVERAGE": 3,
    "MICROFLOW_MAX_SLIPPAGE_BPS": 1,
}

_ADAPTIVE_CONFIG = {
    "ENABLED_STRATEGIES": "adaptive_trend_tsmom_v1",
    "ADAPTIVE_TREND_LIVE_ENTRY_ENABLED": True,
}

_APPROVED = ",".join(OWNER_APPROVED_PRODUCTION_SYMBOLS)
_PRODUCTION_KWARGS = {
    "APP_ENV": "production",
    "PRODUCTION_SYMBOL_ALLOWLIST": _APPROVED,
    "MAX_SYMBOLS": len(OWNER_APPROVED_PRODUCTION_SYMBOLS),
}


def _production_microflow(**overrides) -> Settings:
    kwargs = {**_PRODUCTION_KWARGS, **_PRODUCTION_COMMON_SAFETY_CONFIG, **_MICROFLOW_CONFIG}
    kwargs.update(overrides)
    return _live(**kwargs)


def _production_adaptive(**overrides) -> Settings:
    kwargs = {**_PRODUCTION_KWARGS, **_PRODUCTION_COMMON_SAFETY_CONFIG, **_ADAPTIVE_CONFIG}
    kwargs.update(overrides)
    return _live(**kwargs)


# --- the actual crash reproduced, now fixed --------------------------------


def test_the_exact_owner_launch_config_no_longer_raises():
    """This is the precise configuration that crashed Settings() at startup
    (ENABLED_STRATEGIES=adaptive_trend_tsmom_v1, ADAPTIVE_TREND_LIVE_ENTRY_ENABLED=true,
    APP_ENV=production, EXECUTION_MODE=LIVE) -- reproduced verbatim as a
    regression test."""
    settings = _production_adaptive()
    assert settings.enabled_strategy_set == {"adaptive_trend_tsmom_v1"}
    assert settings.adaptive_trend_live_entry_enabled is True


# --- AdaptiveTrend accepted only with its flag -----------------------------


def test_adaptive_trend_rejected_when_flag_false():
    with pytest.raises(ValidationError, match="ADAPTIVE_TREND_LIVE_ENTRY_ENABLED=true"):
        _production_adaptive(ADAPTIVE_TREND_LIVE_ENTRY_ENABLED=False)


def test_adaptive_trend_rejected_when_flag_missing():
    kwargs = {**_PRODUCTION_KWARGS, **_PRODUCTION_COMMON_SAFETY_CONFIG,
              "ENABLED_STRATEGIES": "adaptive_trend_tsmom_v1"}
    with pytest.raises(ValidationError, match="ADAPTIVE_TREND_LIVE_ENTRY_ENABLED=true"):
        _live(**kwargs)


# --- MicroFlow's own path is completely unaffected -------------------------


def test_microflow_still_passes_with_its_own_full_invariant_set():
    settings = _production_microflow()
    assert settings.enabled_strategy_set == {"microflow_scalper_v1"}


def test_microflow_still_rejected_when_its_own_invariants_fail():
    with pytest.raises(ValidationError, match="MICROFLOW_SCALPER_ENABLED=true"):
        _production_microflow(MICROFLOW_SCALPER_ENABLED=False)
    with pytest.raises(ValidationError, match="MICROFLOW_SYMBOLS"):
        _production_microflow(MICROFLOW_SYMBOLS="BTCUSDT")


def test_microflow_does_not_require_the_adaptive_trend_flag():
    settings = _production_microflow(ADAPTIVE_TREND_LIVE_ENTRY_ENABLED=False)
    assert settings.enabled_strategy_set == {"microflow_scalper_v1"}


# --- unknown / multiple strategies remain blocked ---------------------------


@pytest.mark.parametrize("value", [
    "low_vol_reclaim", "momentum_breakout", "some_new_strategy",
    "adaptive_trend_tsmom_v1,microflow_scalper_v1", "",
])
def test_unapproved_strategy_sets_still_rejected(value):
    with pytest.raises(ValidationError, match="must be exactly microflow_scalper_v1"):
        _production_adaptive(ENABLED_STRATEGIES=value)


# --- neighbouring invariants remain enforced for both branches -------------


def test_max_leverage_ceiling_still_applies_to_adaptive_trend():
    with pytest.raises(ValidationError, match="MAX_LEVERAGE may not exceed"):
        _production_adaptive(MAX_LEVERAGE=11)


def test_strategy_isolation_still_enforced_for_adaptive_trend():
    with pytest.raises(ValidationError, match="STRATEGY_ISOLATION_ENABLED=true"):
        _production_adaptive(STRATEGY_ISOLATION_ENABLED=False)


def test_legacy_new_entry_paths_still_blocked_for_adaptive_trend():
    with pytest.raises(ValidationError, match="OLD_STRATEGIES_NEW_ENTRIES_ENABLED=false"):
        _production_adaptive(OLD_STRATEGIES_NEW_ENTRIES_ENABLED=True)


def test_adaptive_trend_does_not_require_microflow_specific_settings():
    """AdaptiveTrend's branch must never require MICROFLOW_SCALPER_ENABLED,
    MICROFLOW_SYMBOLS, or MICROFLOW_LEVERAGE -- those stay MicroFlow-only."""
    settings = _production_adaptive()
    assert settings.microflow_scalper_enabled is False
