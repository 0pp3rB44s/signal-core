"""Live equity resolution + weekly freeze kill-switch (fail-closed paden)."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import app.equity as equity_mod
from risk.risk_manager import RiskManager


def _settings(configured: float = 100.0) -> MagicMock:
    s = MagicMock()
    s.account_equity_usdt = configured
    return s


def _write_snapshot(tmp_path, monkeypatch, equity: float, age_seconds: float = 0.0) -> None:
    path = tmp_path / "account_equity.json"
    updated = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    path.write_text(json.dumps({
        "equity": equity,
        "updated_at": updated.isoformat(timespec="seconds"),
    }))
    monkeypatch.setattr(equity_mod, "EQUITY_SNAPSHOT_PATH", path)


def test_fresh_snapshot_wins_over_configured(tmp_path, monkeypatch):
    _write_snapshot(tmp_path, monkeypatch, 85.0)
    value, source = equity_mod.resolve_account_equity(_settings(100.0))
    assert value == 85.0
    assert source == "live"


def test_missing_snapshot_falls_back_to_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(equity_mod, "EQUITY_SNAPSHOT_PATH", tmp_path / "nope.json")
    value, source = equity_mod.resolve_account_equity(_settings(100.0))
    assert value == 100.0
    assert source == "configured"


def test_stale_snapshot_errs_small(tmp_path, monkeypatch):
    # Stale snapshot van 120 bij configured 100 -> kies de kleinste (100).
    _write_snapshot(tmp_path, monkeypatch, 120.0, age_seconds=3600)
    value, source = equity_mod.resolve_account_equity(_settings(100.0))
    assert value == 100.0
    assert source == "stale_min"
    # Stale snapshot van 80 bij configured 100 -> kies 80.
    _write_snapshot(tmp_path, monkeypatch, 80.0, age_seconds=3600)
    value, source = equity_mod.resolve_account_equity(_settings(100.0))
    assert value == 80.0


def test_implausible_snapshot_ignored(tmp_path, monkeypatch):
    _write_snapshot(tmp_path, monkeypatch, 999999.0)
    value, source = equity_mod.resolve_account_equity(_settings(100.0))
    assert value == 100.0
    assert source == "configured"


def _freeze_rm(weekly_pnl: float, equity: float = 100.0, freeze_pct: float = 7.0) -> RiskManager:
    s = MagicMock()
    s.account_equity_usdt = equity
    s.hard_daily_stop_pct = 1.5
    s.weekly_freeze_loss_pct = freeze_pct
    rm = RiskManager(settings=s)
    rm._latest_backtest_summary = lambda: {"by_strategy": {}, "by_symbol": {}}
    rm._latest_strategy_expectancy = lambda: {}
    rm._daily_defensive_status = lambda: {"daily_total_net_pnl": 0.0, "consecutive_losses": 0, "report_readable": True}
    rm._weekly_realized_pnl = lambda: weekly_pnl
    return rm


def _candidate() -> MagicMock:
    c = MagicMock()
    c.strategy = "low_vol_reclaim"
    c.symbol = "BTCUSDT"
    c.notes = []
    return c


def test_weekly_freeze_blocks_when_loss_exceeds_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(equity_mod, "EQUITY_SNAPSHOT_PATH", tmp_path / "nope.json")
    rm = _freeze_rm(weekly_pnl=-8.0)  # -8% op 100 equity > 7% freeze
    allowed, reasons = rm._kill_switch_gate(_candidate())
    assert not allowed
    assert any("weekly freeze" in r for r in reasons)


def test_weekly_freeze_inactive_below_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(equity_mod, "EQUITY_SNAPSHOT_PATH", tmp_path / "nope.json")
    rm = _freeze_rm(weekly_pnl=-3.0)
    allowed, reasons = rm._kill_switch_gate(_candidate())
    assert allowed
    assert not any("weekly freeze" in r for r in reasons)


def test_weekly_freeze_ignores_positive_week(tmp_path, monkeypatch):
    monkeypatch.setattr(equity_mod, "EQUITY_SNAPSHOT_PATH", tmp_path / "nope.json")
    rm = _freeze_rm(weekly_pnl=+9.0)
    allowed, reasons = rm._kill_switch_gate(_candidate())
    assert allowed
