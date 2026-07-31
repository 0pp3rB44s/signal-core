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
        "MAX_OPEN_POSITIONS": 1,
        "EXECUTION_MAX_PER_CYCLE": 1,
        "ALLOW_AUTO_WATCHLIST_REFRESH": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


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
        ({"MAX_OPEN_POSITIONS": 2}, "MAX_OPEN_POSITIONS=1"),
        ({"EXECUTION_MAX_PER_CYCLE": 2}, "EXECUTION_MAX_PER_CYCLE=1"),
        ({"MAX_SYMBOLS": 2}, "MAX_SYMBOLS"),
        ({"ALLOW_AUTO_WATCHLIST_REFRESH": True}, "ALLOW_AUTO_WATCHLIST_REFRESH=false"),
        ({"EXECUTION_REQUIRE_CONFIRMATION": False}, "EXECUTION_REQUIRE_CONFIRMATION=true"),
    ],
)
def test_live_portfolio_and_scope_invariants_fail_closed(override, message):
    with pytest.raises(ValidationError, match=message):
        _live(**override)


def test_production_live_requires_exact_owner_approved_nine_symbols():
    with pytest.raises(ValidationError, match="owner-approved nine-symbol"):
        _live(APP_ENV="production")


def _run_guard(body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'. "{GUARD}"; {body}'],
        capture_output=True,
        text=True,
    )


def test_shell_guard_derives_all_legacy_views_from_one_source():
    result = _run_guard(
        "PRODUCTION_SYMBOL_ALLOWLIST='btcusdt,SOLUSDT,SUIUSDT'; "
        "MAX_OPEN_POSITIONS=1; EXECUTION_MAX_PER_CYCLE=1; "
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
