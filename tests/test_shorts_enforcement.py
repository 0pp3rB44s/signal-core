"""ENABLE_SHORTS must actually block SHORT candidates.

Root cause this pins: ENABLE_SHORTS existed in app/config.py but was referenced
nowhere as a gate, so a deployment with ENABLE_SHORTS=false still produced,
scored and risk-evaluated SHORT candidates. In the 2026-07-29 live pilot 39 of
39 candidates reaching the risk layer were SHORT; only an unrelated expectancy
pause prevented them from executing.

Two independent layers are tested separately:
  1. RiskManager.evaluate  - rejects before the planner is ever called;
  2. ExecutionService      - defensive invariant, unreachable in normal flow.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.config import Settings
from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id
from clients.schemas import (MarketSnapshot, StrategyCandidate, StrategyScore,
                             SweepDetection, TradePlan)
from execution.execution_service import ExecutionService
from risk.risk_manager import RiskManager

CANDLE_MS = 1_785_000_000_000


def _settings(**over) -> Settings:
    base = {
        "EXECUTION_ENABLED": True,
        "EXECUTION_MODE": "LIVE",
        "EXECUTION_REQUIRE_CONFIRMATION": False,
        "MAKER_ENTRY_ENABLED": False,
        "SYMBOL_COOLDOWN_MINUTES": 0,
    }
    base.update(over)
    return Settings(_env_file=None, **base)


def _candidate(direction: str, symbol: str = "BTCUSDT",
               strategy: str = "low_vol_reclaim") -> StrategyCandidate:
    """Minimal candidate; market/detection are stubs the short gate never reads."""
    cid = deterministic_candidate_id(strategy, symbol, direction, CANDLE_MS)
    return StrategyCandidate(
        candidate_id=cid,
        candidate_candle_open_timestamp_ms=CANDLE_MS,
        symbol=symbol,
        strategy=strategy,
        direction=direction,
        primary_granularity="15m",
        confirmation_granularity="1H",
        market=MagicMock(spec=MarketSnapshot),
        detection=MagicMock(spec=SweepDetection),
        notes=[],
    )


def _score(total: float = 92.0) -> StrategyScore:
    return StrategyScore(total=total, breakdown={}, verdict="GO", reasons=[])


def _plan(direction: str, symbol: str = "BTCUSDT",
          strategy: str = "low_vol_reclaim") -> TradePlan:
    cid = deterministic_candidate_id(strategy, symbol, direction, CANDLE_MS)
    long_side = direction.upper() == "LONG"
    return TradePlan(
        candidate_id=cid,
        candidate_candle_open_timestamp_ms=CANDLE_MS,
        plan_id=deterministic_plan_id(cid),
        symbol=symbol, strategy=strategy, direction=direction,
        verdict="EXECUTABLE", score=92.0,
        entry_prices=[100.0],
        stop_loss=95.0 if long_side else 105.0,
        take_profits=[110.0 if long_side else 90.0],
        risk_reward_ratio=1.3, account_risk_pct=0.5, leverage=3.0,
        position_notional_usdt=26.79, notes=[], reasons=[], geometry_entry=100.0,
    )


# --- layer 1: risk manager ----------------------------------------------

def test_short_is_rejected_when_shorts_disabled():
    rm = RiskManager(settings=_settings(ENABLE_SHORTS=False))
    verdict = rm.evaluate(_candidate("SHORT"), _score())
    assert verdict.allowed is False
    assert verdict.status == "BLOCKED"
    assert any("shorts disabled by configuration" in r for r in verdict.reasons)


def test_short_rejection_happens_before_any_other_gate():
    """The verdict must carry ONLY the shorts reason - proving it short-circuits
    before expectancy, alignment and every other evaluation."""
    rm = RiskManager(settings=_settings(ENABLE_SHORTS=False))
    verdict = rm.evaluate(_candidate("SHORT"), _score())
    assert len(verdict.reasons) == 1
    for other in ("expectancy", "alignment", "momentum", "execution-cost"):
        assert not any(other in r.lower() for r in verdict.reasons)


def test_short_continues_normally_when_shorts_enabled():
    """With shorts enabled the short gate must not fire; the candidate proceeds
    into the ordinary gates (and may be blocked there for unrelated reasons)."""
    rm = RiskManager(settings=_settings(ENABLE_SHORTS=True))
    verdict = rm.evaluate(_candidate("SHORT"), _score())
    assert not any("shorts disabled" in r for r in verdict.reasons)


def test_long_is_unaffected_when_shorts_disabled():
    rm = RiskManager(settings=_settings(ENABLE_SHORTS=False))
    verdict = rm.evaluate(_candidate("LONG"), _score())
    assert not any("shorts disabled" in r for r in verdict.reasons)


@pytest.mark.parametrize("bad", [None, "maybe", 2, object()])
def test_invalid_value_fails_closed_in_live(bad):
    rm = RiskManager(settings=_settings(ENABLE_SHORTS=True, EXECUTION_MODE="LIVE"))
    rm.settings = MagicMock(wraps=rm.settings)
    rm.settings.enable_shorts = bad
    rm.settings.execution_mode = "LIVE"
    assert rm.shorts_permitted() is False


@pytest.mark.parametrize("bad", [None, "maybe"])
def test_invalid_value_does_not_fail_closed_outside_live(bad):
    rm = RiskManager(settings=_settings(EXECUTION_MODE="DRY_RUN"))
    rm.settings = MagicMock(wraps=rm.settings)
    rm.settings.enable_shorts = bad
    rm.settings.execution_mode = "DRY_RUN"
    assert rm.shorts_permitted() is True


def test_shorts_permitted_reads_parsed_settings_not_env_text():
    assert RiskManager(settings=_settings(ENABLE_SHORTS=False)).shorts_permitted() is False
    assert RiskManager(settings=_settings(ENABLE_SHORTS=True)).shorts_permitted() is True


# --- layer 2: execution invariant ---------------------------------------

def _service(monkeypatch, **over) -> ExecutionService:
    monkeypatch.setattr("execution.execution_service.resolve_account_equity",
                        lambda _s: (1000.0, "test"))
    svc = ExecutionService(settings=_settings(**over))
    client = MagicMock()
    client.get_all_positions.return_value = {"data": []}
    client._format_size.return_value = 0.5
    svc.client = client
    svc.entry_submitter.client = client
    return svc


MUTATING_CALLS = (
    "place_futures_market_order", "place_futures_limit_order", "place_futures_order",
    "close_futures_position", "close_futures_position_full", "cancel_futures_order",
    "place_position_tpsl", "place_futures_protection_orders", "set_futures_leverage",
)


def test_execution_invariant_blocks_injected_short(monkeypatch):
    """A SHORT plan injected straight into execution - bypassing the risk layer
    entirely - must still be refused."""
    svc = _service(monkeypatch, ENABLE_SHORTS=False)
    reports = svc.execute([_plan("SHORT")])
    assert reports[0].status == "SKIPPED"
    assert "shorts disabled by configuration" in reports[0].message


def test_blocked_short_makes_zero_exchange_mutation_calls(monkeypatch):
    svc = _service(monkeypatch, ENABLE_SHORTS=False)
    svc.execute([_plan("SHORT")])
    for name in MUTATING_CALLS:
        assert getattr(svc.client, name).call_count == 0, f"{name} was called"


def test_execution_invariant_allows_long_through(monkeypatch):
    """The invariant must not touch the LONG path: a LONG plan proceeds past it
    and is only stopped by ordinary downstream gates."""
    svc = _service(monkeypatch, ENABLE_SHORTS=False)
    reports = svc.execute([_plan("LONG")])
    assert "shorts disabled" not in reports[0].message


def test_execution_shorts_permitted_mirrors_risk_manager(monkeypatch):
    svc = _service(monkeypatch, ENABLE_SHORTS=False)
    assert svc._shorts_permitted() is False
    svc2 = _service(monkeypatch, ENABLE_SHORTS=True)
    assert svc2._shorts_permitted() is True


# --- telemetry ----------------------------------------------------------

def test_reason_maps_to_shorts_disabled_code():
    from telemetry.funnel import REASON_CODES, classify_reason_codes
    codes = classify_reason_codes(
        ["blocked: shorts disabled by configuration (ENABLE_SHORTS=false)"])
    assert "SHORTS_DISABLED" in codes
    assert "SHORTS_DISABLED" in REASON_CODES


def test_shorts_code_does_not_collide_with_probe_text():
    """Must not be inferred from non-blocking annotations, the way
    EXPECTANCY_BLOCK matches the bare word 'expectancy' in PROBE text."""
    from telemetry.funnel import classify_reason_codes
    for benign in (
        "strategy weighting PROBE: negative expectancy, trading at reduced size",
        "reclaim PROBE: geen volledige HTF-consensus, halve size",
        "expectancy-watch: strategy weak but not hard-paused",
        "ai-agent: passed (8 decisions checked)",
    ):
        assert "SHORTS_DISABLED" not in classify_reason_codes([benign])


# --- production regression fixture --------------------------------------

def test_production_regression_btcusdt_short_2026_07_29():
    """Exact shape observed in the live pilot: BTCUSDT SHORT, low_vol_reclaim,
    score 92 (well above the 68/72 Safe-Mode minimum), risk otherwise passable,
    ENABLE_SHORTS=false. Expected: rejected as SHORTS_DISABLED, zero execution."""
    from telemetry.funnel import classify_reason_codes

    rm = RiskManager(settings=_settings(ENABLE_SHORTS=False))
    verdict = rm.evaluate(_candidate("SHORT", "BTCUSDT", "low_vol_reclaim"), _score(92.0))

    assert verdict.allowed is False
    assert verdict.status == "BLOCKED"
    codes = classify_reason_codes(verdict.reasons)
    assert "SHORTS_DISABLED" in codes
    # And it never reached the gates that were previously the only thing
    # standing between this candidate and the exchange.
    assert not any("expectancy" in r.lower() for r in verdict.reasons)


# --- untouched-behaviour guards -----------------------------------------

def test_expectancy_logic_and_thresholds_are_untouched():
    """This patch must not have altered expectancy or any risk threshold."""
    from risk.risk_manager import RiskManager as RM
    assert RM.SAFE_ALPHA_MAX_LEVERAGE == 8
    assert RM.SAFE_ALPHA_MAX_RISK_PCT == 0.75
    src = (RM._stats_should_pause.__doc__ or "")
    import inspect
    body = inspect.getsource(RM._stats_should_pause)
    assert "trades < min_trades" in body
    assert "expectancy < 0" in body
    assert "lossrate >= 0.75" in body


def test_expectancy_data_file_is_not_written_by_this_patch():
    import subprocess
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    changed = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"], cwd=repo,
        capture_output=True, text=True).stdout.split()
    for path in changed:
        assert "latest_summary" not in path
        assert "strategy_expectancy" not in path


def test_sizing_leverage_and_symbols_unchanged():
    """Guards the code defaults. The live values come from .env.live, which this
    patch does not touch - asserted separately below."""
    s = _settings()
    assert s.max_leverage == 5.0
    assert s.default_leverage == 5.0
    assert s.account_risk_per_trade_pct == 0.75
    assert s.max_open_positions == 2


def test_live_env_trading_parameters_untouched():
    """The deployed .env.live must still carry the pilot's exact parameters."""
    from pathlib import Path
    live = Path(__file__).resolve().parents[1] / ".env.live"
    if not live.exists():
        pytest.skip(".env.live not present in this checkout")
    text = live.read_text()
    # Risk-bearing ceilings. These must not drift; loosening any of them is the
    # failure mode this guard exists for.
    for expected in ("MAX_SYMBOLS=1", "MAX_OPEN_POSITIONS=1", "DEFAULT_LEVERAGE=3",
                     "MAX_LEVERAGE=3", "ACCOUNT_RISK_PER_TRADE_PCT=0.50",
                     "EXECUTION_CONFIRM_SYMBOLS=BTCUSDT",
                     "EXECUTION_REQUIRE_CONFIRMATION=true",
                     "EXECUTION_MAX_LIVE_NOTIONAL_PER_TRADE_USDT=35"):
        assert expected in text, f"missing/changed in .env.live: {expected}"

    # ENABLE_SHORTS is owner-controlled and was deliberately switched to true on
    # 2026-07-30 (short trading re-enabled after the symbol-expectancy repair).
    # It is asserted as present-and-explicit rather than pinned to false: the
    # enforcement path is covered by the RiskManager and ExecutionService tests
    # above, which is what actually matters regardless of the configured value.
    assert ("ENABLE_SHORTS=true" in text or "ENABLE_SHORTS=false" in text), \
        "ENABLE_SHORTS must be explicitly set in .env.live, never left to a default"
