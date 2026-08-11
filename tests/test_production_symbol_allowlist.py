from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.symbol_allowlist import SymbolAllowlistError, parse_symbol_allowlist
from data.watchlist import get_watchlist


REPO = Path(__file__).resolve().parents[1]
GUARD = REPO / "scripts" / "lib" / "env_guard.sh"


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


#: Production LIVE additionally demands every safety value be set explicitly
#: rather than defaulted. Mirrors what .env.live carries.
_PRODUCTION_SAFETY_CONFIG = {
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
    "OLD_STRATEGIES_NEW_ENTRIES_ENABLED": False,
    "DYNAMIC_GRID_ENABLED": False,
    "DYNAMIC_GRID_MODE": "OFF",
    "MAKER_ENTRY_FALLBACK_MARKET": False,
}


def test_allowlist_normalises_but_never_reorders_owner_selection():
    assert parse_symbol_allowlist(" btcusdt, SOLUSDT ,suiusdt ") == (
        "BTCUSDT",
        "SOLUSDT",
        "SUIUSDT",
    )


@pytest.mark.parametrize("value", ["", "BTCUSDT,BTCUSDT", "BTCUSD", "BTC-USDT"])
def test_invalid_or_ambiguous_allowlist_fails_closed(value):
    with pytest.raises(SymbolAllowlistError):
        parse_symbol_allowlist(value, required=True)


def test_live_requires_explicit_owner_allowlist():
    with pytest.raises(ValidationError, match="PRODUCTION_SYMBOL_ALLOWLIST"):
        _live(PRODUCTION_SYMBOL_ALLOWLIST="", MAX_SYMBOLS=0)


def test_live_scanner_and_execution_derive_the_same_canonical_set():
    settings = _live(
        WATCHLIST="DOGEUSDT",
        EXECUTION_CONFIRM_SYMBOLS="AVAXUSDT",
    )
    assert settings.watchlist_symbols == ["BTCUSDT", "SOLUSDT", "SUIUSDT"]
    assert settings.execution_confirm_symbol_set == {
        "BTCUSDT",
        "SOLUSDT",
        "SUIUSDT",
    }
    assert get_watchlist(settings, contracts=[]) == settings.watchlist_symbols


@pytest.mark.parametrize(
    ("override", "message"),
    [
        # Pinned to exactly 2: both a lower and a higher value must fail closed,
        # so a mistyped or silently-defaulted setting can never widen exposure.
        ({"MAX_OPEN_POSITIONS": 1}, "MAX_OPEN_POSITIONS=2"),
        ({"MAX_OPEN_POSITIONS": 3}, "MAX_OPEN_POSITIONS=2"),
        ({"EXECUTION_MAX_PER_CYCLE": 1}, "EXECUTION_MAX_PER_CYCLE=2"),
        ({"EXECUTION_MAX_PER_CYCLE": 3}, "EXECUTION_MAX_PER_CYCLE=2"),
        ({"MAX_SYMBOLS": 2}, "MAX_SYMBOLS"),
        ({"ALLOW_AUTO_WATCHLIST_REFRESH": True}, "ALLOW_AUTO_WATCHLIST_REFRESH=false"),
        ({"EXECUTION_REQUIRE_CONFIRMATION": False}, "EXECUTION_REQUIRE_CONFIRMATION=true"),
    ],
)
def test_live_portfolio_and_scope_invariants_fail_closed(override, message):
    with pytest.raises(ValidationError, match=message):
        _live(**override)


def test_production_live_requires_exact_owner_approved_allowlist():
    """A three-symbol subset is not the owner-approved set, even though every
    symbol in it is individually approved."""
    with pytest.raises(ValidationError, match="owner-approved allowlist"):
        _live(APP_ENV="production")


def test_production_live_accepts_exactly_the_owner_approved_eight():
    from app.symbol_allowlist import OWNER_APPROVED_PRODUCTION_SYMBOLS
    approved = ",".join(OWNER_APPROVED_PRODUCTION_SYMBOLS)
    settings = _live(APP_ENV="production", PRODUCTION_SYMBOL_ALLOWLIST=approved,
                     MAX_SYMBOLS=len(OWNER_APPROVED_PRODUCTION_SYMBOLS),
                     **_PRODUCTION_SAFETY_CONFIG)
    assert tuple(settings.watchlist_symbols) == OWNER_APPROVED_PRODUCTION_SYMBOLS
    assert len(OWNER_APPROVED_PRODUCTION_SYMBOLS) == 8


def test_wifusdt_is_conditional_and_not_executable():
    """WIFUSDT stays a known symbol with a recorded reason; it is simply not in
    the active set. Adding it back needs a separate owner-approved change."""
    from app.symbol_allowlist import (CONDITIONAL_PRODUCTION_SYMBOLS,
                                      OWNER_APPROVED_PRODUCTION_SYMBOLS)
    assert "WIFUSDT" not in OWNER_APPROVED_PRODUCTION_SYMBOLS
    assert CONDITIONAL_PRODUCTION_SYMBOLS["WIFUSDT"] == "CONDITIONAL_SPREAD"


def test_adding_wif_back_silently_fails_closed():
    from app.symbol_allowlist import OWNER_APPROVED_PRODUCTION_SYMBOLS
    nine = ",".join(OWNER_APPROVED_PRODUCTION_SYMBOLS) + ",WIFUSDT"
    with pytest.raises(ValidationError, match="owner-approved allowlist"):
        _live(APP_ENV="production", PRODUCTION_SYMBOL_ALLOWLIST=nine, MAX_SYMBOLS=9)


def _run_guard(body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'. "{GUARD}"; {body}'],
        capture_output=True,
        text=True,
    )


def test_shell_guard_derives_all_legacy_views_from_one_source():
    result = _run_guard(
        "PRODUCTION_SYMBOL_ALLOWLIST='btcusdt,SOLUSDT,SUIUSDT'; "
        "MAX_OPEN_POSITIONS=2; EXECUTION_MAX_PER_CYCLE=2; "
        "FORWARD_PAPER_SMOKE_STRATEGY_ENABLED=false; "
        "guard_apply_canonical_symbol_allowlist; guard_assert_pilot_limits; "
        "printf '%s|%s|%s|%s' \"$WATCHLIST\" \"$EXECUTION_CONFIRM_SYMBOLS\" "
        "\"$MAX_SYMBOLS\" \"$ALLOW_AUTO_WATCHLIST_REFRESH\""
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip().endswith(
        "BTCUSDT,SOLUSDT,SUIUSDT|BTCUSDT,SOLUSDT,SUIUSDT|3|false"
    )


def test_shell_guard_has_no_btc_only_or_dynamic_expansion_path():
    source = GUARD.read_text(encoding="utf-8")
    assert "MAX_SYMBOLS must be <=1" not in source
    assert "PRODUCTION_SYMBOL_ALLOWLIST" in source
    assert "ALLOW_AUTO_WATCHLIST_REFRESH=false" in source
