from unittest.mock import MagicMock

import pytest

import app.equity as equity_mod
from risk.risk_manager import RiskManager


@pytest.fixture(autouse=True)
def _isolate_equity_snapshot(tmp_path, monkeypatch):
    # Zonder isolatie leest resolve_account_equity de LIVE snapshot van de
    # draaiende bot (state/account_equity.json), waardoor deze equity-tests
    # niet-deterministisch worden. Wijs naar een niet-bestaand pad zodat de
    # resolver terugvalt op settings.account_equity_usdt.
    monkeypatch.setattr(equity_mod, "EQUITY_SNAPSHOT_PATH", tmp_path / "no_equity.json")


def _make_risk_manager(equity: float, hard_daily_stop_pct: float, daily_pnl: float) -> RiskManager:
    settings = MagicMock()
    settings.account_equity_usdt = equity
    settings.hard_daily_stop_pct = hard_daily_stop_pct
    settings.weekly_freeze_loss_pct = 0.0

    rm = RiskManager(settings=settings)
    rm._latest_backtest_summary = lambda: {"by_strategy": {}, "by_symbol": {}}
    rm._latest_strategy_expectancy = lambda: {}
    rm._weekly_realized_pnl = lambda: 0.0
    rm._daily_defensive_status = lambda: {
        "daily_total_net_pnl": daily_pnl,
        "consecutive_losses": 0,
        "report_readable": True,
    }
    return rm


def _candidate(strategy: str = "low_vol_reclaim", symbol: str = "BTCUSDT") -> MagicMock:
    candidate = MagicMock()
    candidate.strategy = strategy
    candidate.symbol = symbol
    candidate.notes = []
    candidate.market.notes = []
    candidate.direction = "LONG"
    return candidate


def test_execution_cost_gate_reads_current_spread_note_format():
    rm = RiskManager(settings=MagicMock())
    candidate = _candidate(symbol="AAVEUSDT")
    candidate.notes = ["spread_bps=5.250", "entry_quality long=90", "close_pos=0.5"]

    allowed, reasons = rm._execution_cost_gate(candidate)

    assert not allowed
    assert any("spread too wide (5.25bps >= 5.00bps)" in reason for reason in reasons)


def test_execution_cost_gate_keeps_legacy_spread_note_compatible():
    rm = RiskManager(settings=MagicMock())
    candidate = _candidate(symbol="AAVEUSDT")
    candidate.notes = ["spread 5.250bps", "entry_quality long=90", "close_pos=0.5"]

    allowed, reasons = rm._execution_cost_gate(candidate)

    assert not allowed
    assert any("spread too wide (5.25bps >= 5.00bps)" in reason for reason in reasons)


def test_kill_switch_scales_with_equity_below_threshold():
    rm = _make_risk_manager(equity=1000.0, hard_daily_stop_pct=2.0, daily_pnl=-15.0)
    allowed, reasons = rm._kill_switch_gate(_candidate())
    assert allowed
    assert not any("kill-switch: daily" in r for r in reasons)


def test_kill_switch_scales_with_equity_above_threshold():
    rm = _make_risk_manager(equity=1000.0, hard_daily_stop_pct=2.0, daily_pnl=-25.0)
    allowed, reasons = rm._kill_switch_gate(_candidate())
    assert not allowed
    assert any("kill-switch: daily" in r for r in reasons)


def test_kill_switch_does_not_use_flat_dollar_threshold():
    # Same -10 USD loss that used to always trip the old flat threshold
    # should NOT trip on a larger account where it's a negligible % loss.
    rm = _make_risk_manager(equity=10_000.0, hard_daily_stop_pct=2.0, daily_pnl=-10.0)
    allowed, reasons = rm._kill_switch_gate(_candidate())
    assert allowed
    assert not any("kill-switch: daily" in r for r in reasons)


def _make_weighting_risk_manager(strategies: dict) -> RiskManager:
    rm = RiskManager(settings=MagicMock())
    rm._latest_strategy_expectancy = lambda: {"strategies": strategies}
    return rm


def test_strategy_weighting_missing_tp1_hit_rate_does_not_probe():
    # reports/backtests/strategy_expectancy.json can explicitly mark
    # tp1_hit_rate as null ("missing_not_zero") when the backfill hasn't run
    # yet. That must not be treated as a genuine 0% hit-rate.
    rm = _make_weighting_risk_manager({
        "low_vol_reclaim": {"trades": 28, "expectancy": 0.0663, "tp1_hit_rate": None},
    })
    allowed, reasons, probe = rm._strategy_weighting_gate(_candidate(strategy="low_vol_reclaim"))
    assert allowed
    assert not probe
    assert any("tp1_hit_rate data missing, not treated as zero" in r for r in reasons)


def test_strategy_weighting_genuine_low_tp1_hit_rate_probes_at_reduced_size():
    rm = _make_weighting_risk_manager({
        "momentum_breakout": {"trades": 10, "expectancy": 0.1, "tp1_hit_rate": 0.1},
    })
    allowed, reasons, probe = rm._strategy_weighting_gate(_candidate(strategy="momentum_breakout"))
    assert allowed
    assert probe
    assert any("PROBE: weak TP1 hit-rate" in r for r in reasons)


def test_strategy_weighting_negative_expectancy_probes_at_reduced_size():
    # Hedge-fund style allocation: negative recent expectancy shrinks the
    # allocation instead of freezing the strategy out (a frozen strategy can
    # never generate the fresh data needed to re-qualify).
    rm = _make_weighting_risk_manager({
        "momentum_breakout": {"trades": 10, "expectancy": -0.5, "tp1_hit_rate": None},
    })
    allowed, reasons, probe = rm._strategy_weighting_gate(_candidate(strategy="momentum_breakout"))
    assert allowed
    assert probe
    assert any("PROBE: negative expectancy" in r for r in reasons)


def test_strategy_weighting_insufficient_sample_does_not_probe():
    rm = _make_weighting_risk_manager({
        "momentum_breakout": {"trades": 3, "expectancy": -0.8, "tp1_hit_rate": None},
    })
    allowed, reasons, probe = rm._strategy_weighting_gate(_candidate(strategy="momentum_breakout"))
    assert allowed
    assert not probe
    assert any("insufficient data" in r for r in reasons)


def _make_session_risk_manager(windows: str, multiplier: float = 0.5) -> RiskManager:
    settings = MagicMock()
    settings.session_risk_reduction_windows_utc = windows
    settings.session_risk_multiplier = multiplier
    return RiskManager(settings=settings)


def test_session_multiplier_inside_simple_window():
    rm = _make_session_risk_manager("08-12,23-01")
    multiplier, reason = rm._session_risk_multiplier(now_hour_utc=9)
    assert multiplier == 0.5
    assert "08-12" in reason


def test_session_multiplier_outside_windows_is_full_size():
    rm = _make_session_risk_manager("08-12,23-01")
    multiplier, reason = rm._session_risk_multiplier(now_hour_utc=17)
    assert multiplier == 1.0
    assert reason == ""


def test_session_multiplier_midnight_wrap_window():
    rm = _make_session_risk_manager("08-12,23-01")
    assert rm._session_risk_multiplier(now_hour_utc=23)[0] == 0.5
    assert rm._session_risk_multiplier(now_hour_utc=0)[0] == 0.5
    assert rm._session_risk_multiplier(now_hour_utc=1)[0] == 1.0


def test_session_multiplier_disabled_when_no_windows():
    rm = _make_session_risk_manager("")
    assert rm._session_risk_multiplier(now_hour_utc=9)[0] == 1.0


def test_kill_switch_fails_closed_on_unreadable_daily_report():
    rm = _make_risk_manager(equity=1000.0, hard_daily_stop_pct=2.0, daily_pnl=0.0)
    rm._daily_defensive_status = lambda: {"daily_status_unreadable": True}
    allowed, reasons = rm._kill_switch_gate(_candidate())
    assert not allowed
    assert any("daily learning report unreadable" in r for r in reasons)


def test_kill_switch_missing_daily_report_is_not_a_block():
    rm = _make_risk_manager(equity=1000.0, hard_daily_stop_pct=2.0, daily_pnl=0.0)
    rm._daily_defensive_status = lambda: {}
    allowed, _ = rm._kill_switch_gate(_candidate())
    assert allowed
