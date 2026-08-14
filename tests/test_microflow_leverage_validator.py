"""The MicroFlow leverage ceiling, raised 5x -> 10x on 2026-08-14 by owner request.

The ceiling exists so a mistyped or silently-defaulted leverage can never widen
exposure. Raising it is a deliberate act, so the new bound is pinned from both
sides: 10 must load, anything above it must fail closed, and `MAX_LEVERAGE`
itself may not be pushed past the same constant.

Legacy strategies do not gain anything from the higher ceiling. They cannot open
a LIVE entry at all — production LIVE requires the enabled-strategy allowlist to
be exactly `microflow_scalper_v1` — and that is asserted here rather than assumed.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import MICROFLOW_MAX_ALLOWED_LEVERAGE, LIVE_MAX_OPEN_POSITIONS, Settings

_SAFETY = {
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
    "ENABLED_STRATEGIES": "microflow_scalper_v1",
    "MICROFLOW_SCALPER_ENABLED": True,
    "MICROFLOW_SYMBOLS": ("BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT,"
                          "LINKUSDT,AVAXUSDT,SUIUSDT,HYPEUSDT,ZECUSDT,NEARUSDT"),
    "MICROFLOW_MAX_SLIPPAGE_BPS": 1,
    "OLD_STRATEGIES_NEW_ENTRIES_ENABLED": False,
    "DYNAMIC_GRID_ENABLED": False,
    "DYNAMIC_GRID_MODE": "OFF",
    "MAKER_ENTRY_FALLBACK_MARKET": False,
}

_UNIVERSE = _SAFETY["MICROFLOW_SYMBOLS"]


def _live(**overrides) -> Settings:
    values = {
        "APP_ENV": "production",
        "APP_MODE": "live",
        "EXECUTION_ENABLED": True,
        "EXECUTION_MODE": "LIVE",
        "EXECUTION_REQUIRE_CONFIRMATION": True,
        "PRODUCTION_SYMBOL_ALLOWLIST": _UNIVERSE,
        "MAX_SYMBOLS": 12,
        "MAX_OPEN_POSITIONS": LIVE_MAX_OPEN_POSITIONS,
        "EXECUTION_MAX_PER_CYCLE": 2,
        "ALLOW_AUTO_WATCHLIST_REFRESH": False,
        "MICROFLOW_LEVERAGE": 10,
        "MAX_LEVERAGE": 10,
        "DEFAULT_LEVERAGE": 10,
        **_SAFETY,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_the_constant_is_ten():
    assert MICROFLOW_MAX_ALLOWED_LEVERAGE == 10.0


@pytest.mark.parametrize("leverage", [1, 3, 5, 10])
def test_leverage_up_to_the_ceiling_loads(leverage):
    s = _live(MICROFLOW_LEVERAGE=leverage)
    assert s.microflow_leverage == leverage


@pytest.mark.parametrize("leverage", [10.01, 11, 20, 125])
def test_leverage_above_the_ceiling_fails_closed(leverage):
    with pytest.raises(ValidationError, match="MICROFLOW_LEVERAGE"):
        _live(MICROFLOW_LEVERAGE=leverage, MAX_LEVERAGE=leverage, DEFAULT_LEVERAGE=leverage)


@pytest.mark.parametrize("leverage", [0, -1, -10])
def test_invalid_leverage_fails_closed(leverage):
    with pytest.raises(ValidationError):
        _live(MICROFLOW_LEVERAGE=leverage)


def test_microflow_may_not_exceed_max_leverage():
    with pytest.raises(ValidationError, match="may not exceed MAX_LEVERAGE"):
        _live(MICROFLOW_LEVERAGE=10, MAX_LEVERAGE=3)


def test_max_leverage_itself_is_bounded_by_the_same_constant():
    """Raising MAX_LEVERAGE must not become a back door to unlimited leverage."""
    with pytest.raises(ValidationError, match="MAX_LEVERAGE may not exceed"):
        _live(MICROFLOW_LEVERAGE=10, MAX_LEVERAGE=50, DEFAULT_LEVERAGE=10)


# --- legacy strategies gain nothing from the higher ceiling ------------------


@pytest.mark.parametrize("strategies", [
    "microflow_scalper_v1,momentum_breakout",
    "low_vol_reclaim_v2",
    "momentum_breakout",
    "",
])
def test_only_microflow_may_be_entry_enabled_in_live(strategies):
    with pytest.raises(ValidationError):
        _live(ENABLED_STRATEGIES=strategies)


def test_old_strategy_entries_stay_disabled():
    with pytest.raises(ValidationError):
        _live(OLD_STRATEGIES_NEW_ENTRIES_ENABLED=True)


# --- the sizing bounds are validated too ------------------------------------


@pytest.mark.parametrize("field,bad", [
    ("MICROFLOW_MARGIN_RESERVE_PCT", 100),
    ("MICROFLOW_MARGIN_RESERVE_PCT", -1),
    ("MICROFLOW_MAX_NOTIONAL_PCT_EQUITY", 0),
    ("MICROFLOW_MAX_NOTIONAL_PCT_EQUITY", 1001),
    ("MICROFLOW_MAX_LOSS_PCT_EQUITY", 0),
    ("MICROFLOW_MAX_LOSS_PCT_EQUITY", 5.01),
])
def test_sizing_bounds_fail_closed(field, bad):
    with pytest.raises(ValidationError):
        _live(**{field: bad})


def test_position_count_stays_pinned_at_two():
    with pytest.raises(ValidationError):
        _live(MAX_OPEN_POSITIONS=3)
    with pytest.raises(ValidationError):
        _live(MAX_OPEN_POSITIONS=1)
