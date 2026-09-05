"""ADAPTIVE_TREND_RETIRED: the dedicated, strategy-specific retirement gate.

Binds against the real execution/execution_service.py, execution/adaptive_trend_entry.py,
and app/config.py Settings field -- not helper doubles -- so if the flag default drifts, the
gate is removed from either choke point, or another strategy_id is accidentally affected, these
go red.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id
from clients.schemas import TradePlan
from execution.execution_service import ExecutionService
from execution.adaptive_trend_entry import submit_adaptive_trend_entry
from strategies.adaptive_trend_tsmom import STRATEGY_VERSION, Side, SignalCandidate, initial_stop, size_position

CANDLE_MS = 1_785_000_000_000
REPO = Path(__file__).resolve().parents[1]


def _settings(**over) -> Settings:
    base = {
        "EXECUTION_ENABLED": True,
        "EXECUTION_MODE": "LIVE",
        "PRODUCTION_SYMBOL_ALLOWLIST": "BTCUSDT,ETHUSDT,SOLUSDT",
        "MAX_SYMBOLS": 3,
        "MAX_OPEN_POSITIONS": 2,
        "EXECUTION_MAX_PER_CYCLE": 2,
        "ALLOW_AUTO_WATCHLIST_REFRESH": False,
        "EXECUTION_REQUIRE_CONFIRMATION": True,
        "MAKER_ENTRY_ENABLED": False,
        "SYMBOL_COOLDOWN_MINUTES": 0,
        "ENABLE_SHORTS": True,
        "ADAPTIVE_TREND_LIVE_ENTRY_ENABLED": True,
    }
    base.update(over)
    return Settings(_env_file=None, **base)


def _plan(symbol: str, strategy: str, direction: str = "LONG") -> TradePlan:
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
        take_profits=[120.0 if long_side else 80.0],
        risk_reward_ratio=1.3, account_risk_pct=0.5, leverage=2.0,
        position_notional_usdt=26.79, notes=[], reasons=[], geometry_entry=100.0,
    )


def _service(monkeypatch, settings: Settings, existing_open: list[dict] | None = None) -> ExecutionService:
    monkeypatch.setattr("execution.execution_service.resolve_account_equity",
                        lambda _s: (1000.0, "test"))
    svc = ExecutionService(settings=settings)
    client = MagicMock()
    open_symbols = {row["symbol"] for row in (existing_open or [])}
    client.get_all_positions.return_value = {
        "data": [{"symbol": s, "total": "1.0"} for s in open_symbols]
    }
    client._format_size.return_value = 0.5
    svc.client = client
    svc.entry_submitter.client = client
    svc.store.load = lambda default=None: list(existing_open or [])
    return svc


# --- 1. flag missing -> defaults False -----------------------------------

def test_flag_missing_defaults_to_false():
    s = Settings(_env_file=None)
    assert s.adaptive_trend_retired is False


# --- 2. retired=False -> existing behaviour unchanged --------------------

def test_retired_false_does_not_produce_the_retirement_skip(monkeypatch):
    settings = _settings(ADAPTIVE_TREND_RETIRED=False)
    svc = _service(monkeypatch, settings)
    plan = _plan("BTCUSDT", STRATEGY_VERSION)
    reports = svc.execute([plan])
    assert len(reports) == 1
    assert reports[0].message != "ADAPTIVE_TREND_RETIRED"


# --- 3. retired=True -> new entry blocked, explicit reason ----------------

def test_retired_true_blocks_new_entry_with_explicit_reason(monkeypatch):
    settings = _settings(ADAPTIVE_TREND_RETIRED=True)
    svc = _service(monkeypatch, settings)
    plan = _plan("BTCUSDT", STRATEGY_VERSION)
    reports = svc.execute([plan])
    assert len(reports) == 1
    assert reports[0].status == "SKIPPED"
    assert reports[0].message == "ADAPTIVE_TREND_RETIRED"


def test_retired_true_blocks_even_when_live_entry_flag_is_true(monkeypatch):
    """The retirement gate must win regardless of ADAPTIVE_TREND_LIVE_ENTRY_ENABLED --
    it is a later, independent off switch, not something the live-entry flag can override."""
    settings = _settings(ADAPTIVE_TREND_LIVE_ENTRY_ENABLED=True, ADAPTIVE_TREND_RETIRED=True)
    svc = _service(monkeypatch, settings)
    plan = _plan("ETHUSDT", STRATEGY_VERSION)
    reports = svc.execute([plan])
    assert reports[0].status == "SKIPPED"
    assert reports[0].message == "ADAPTIVE_TREND_RETIRED"


# --- 4. no other strategy_id is affected ---------------------------------

def test_other_strategy_ids_unaffected_by_retirement_flag(monkeypatch):
    settings = _settings(ADAPTIVE_TREND_RETIRED=True, ENABLED_STRATEGIES="")
    svc = _service(monkeypatch, settings)
    plan = _plan("BTCUSDT", "momentum_breakout")
    reports = svc.execute([plan])
    assert len(reports) == 1
    assert reports[0].message != "ADAPTIVE_TREND_RETIRED"


def test_microflow_unaffected_by_adaptive_trend_retirement_flag(monkeypatch):
    settings = _settings(ADAPTIVE_TREND_RETIRED=True, ENABLED_STRATEGIES="")
    svc = _service(monkeypatch, settings)
    plan = _plan("SOLUSDT", "microflow_scalper_v1")
    reports = svc.execute([plan])
    assert reports[0].message != "ADAPTIVE_TREND_RETIRED"


# --- 5. sync/reconciliation/trailing untouched, structurally -------------

def test_sync_and_trailing_modules_do_not_reference_the_retirement_flag():
    """AdaptiveTrend's position sync/reconciliation (execution/adaptive_trend_sync.py) and
    trailing-stop management (strategies/adaptive_trend_trail.py) must continue running for any
    already-open position regardless of retirement -- proven here by the flag not appearing in
    either module at all (it only gates new entries, in execution_service.py /
    adaptive_trend_entry.py)."""
    for rel in ("execution/adaptive_trend_sync.py", "strategies/adaptive_trend_trail.py"):
        source = (REPO / rel).read_text()
        assert "adaptive_trend_retired" not in source
        assert "ADAPTIVE_TREND_RETIRED" not in source


# --- 6. startup validation remains valid with retirement enabled ---------

def test_production_startup_validation_passes_with_retirement_enabled():
    """Restart with ADAPTIVE_TREND_RETIRED=true must boot normally: the existing production LIVE
    validation (requiring ADAPTIVE_TREND_LIVE_ENTRY_ENABLED=true for the adaptive_trend_tsmom_v1
    allowlist) is untouched by this flag and must not raise."""
    from tests.test_config_production_adaptive_trend_eligibility import _production_adaptive
    s = _production_adaptive(ADAPTIVE_TREND_RETIRED=True)
    assert s.adaptive_trend_retired is True
    assert s.adaptive_trend_live_entry_enabled is True


def test_entry_call_site_parameter_defaults_false_and_is_wired(monkeypatch):
    """submit_adaptive_trend_entry's defensive retired= parameter defaults False (so an existing
    caller that forgot to pass it keeps current behaviour) and, when explicitly True, blocks
    before ExecutionService.execute() is ever called."""
    winner = SignalCandidate(symbol="BTCUSDT", side=Side.LONG, signal_candle_close_ms=CANDLE_MS,
                             close=100.0, mom=0.05, atr=2.0)
    stop = initial_stop(winner.close, winner.atr, winner.side)
    sizing = size_position(equity=1000.0, entry_price=winner.close, stop_price=stop,
                           exchange_min_notional=5.0)
    assert sizing.accepted
    svc = MagicMock()
    result = submit_adaptive_trend_entry(
        winner=winner, winner_sizing=sizing, weekly_freeze_active=False,
        execution_service=svc, retired=True,
    )
    assert result is None
    svc.execute.assert_not_called()


def test_runner_call_site_passes_the_settings_flag_through():
    """Structural: app/runner.py's call site must pass retired=self.settings.adaptive_trend_retired
    so the defensive entry-call-site check in adaptive_trend_entry.py is actually reachable from
    production, not just from a test that calls submit_adaptive_trend_entry directly."""
    source = (REPO / "app" / "runner.py").read_text()
    idx = source.index("submit_adaptive_trend_entry(")
    call_block = source[idx: idx + 400]
    assert "retired=self.settings.adaptive_trend_retired" in call_block
