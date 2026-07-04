from unittest.mock import MagicMock

from risk.risk_manager import RiskManager


def _make_risk_manager(equity: float, hard_daily_stop_pct: float, daily_pnl: float) -> RiskManager:
    settings = MagicMock()
    settings.account_equity_usdt = equity
    settings.hard_daily_stop_pct = hard_daily_stop_pct

    rm = RiskManager(settings=settings)
    rm._latest_backtest_summary = lambda: {"by_strategy": {}, "by_symbol": {}}
    rm._latest_strategy_expectancy = lambda: {}
    rm._daily_defensive_status = lambda: {
        "daily_total_net_pnl": daily_pnl,
        "consecutive_losses": 0,
    }
    return rm


def _candidate(strategy: str = "low_vol_reclaim", symbol: str = "BTCUSDT") -> MagicMock:
    candidate = MagicMock()
    candidate.strategy = strategy
    candidate.symbol = symbol
    return candidate


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
