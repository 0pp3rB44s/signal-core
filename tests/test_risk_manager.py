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
    candidate.notes = []
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
