from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.equity as equity_mod
from app.equity import portfolio_equity_drawdown_gate
from microflow.live import MicroflowLiveRuntime
from clients.schemas import StrategyScore
from risk.risk_manager import RiskManager


def _settings(**overrides):
    values = {
        "microflow_max_slippage_bps": 1.0,
        "microflow_leverage": 10.0,
        "default_leverage": 10.0,
        "max_leverage": 10.0,
        "max_open_positions": 2,
        "microflow_margin_reserve_pct": 10.0,
        "microflow_max_notional_pct_equity": 500.0,
        "microflow_max_loss_pct_equity": 2.0,
        "account_risk_per_trade_pct": 0.75,
        "hard_daily_stop_pct": 2.0,
        "max_total_exposure_pct": 1000.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _candidate():
    return {
        "candidate_id": "feed-candidate",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "signal_ts": 1_000_000,
        "entry_reference": 100.0,
        "stop_loss": 99.8,
        "take_profit": 100.4,
        "persistence_ms": 2_000,
        "features": {
            "trade_flow": {},
            "book": {"spread_bps": 1.0},
            "microprice": {},
            "freshness": {},
        },
    }


def _runtime(risk):
    runtime = MicroflowLiveRuntime.__new__(MicroflowLiveRuntime)
    runtime.settings = _settings()
    runtime.risk_manager = risk
    runtime.client = MagicMock()
    runtime.client.get_accounts.return_value = {
        "data": [{"marginCoin": "USDT", "accountEquity": "100", "available": "100"}]
    }
    runtime.client.get_trade_fee_rate.return_value = {
        "data": {"makerFeeRate": "0.0002", "takerFeeRate": "0.0006"}
    }
    return runtime


def test_microflow_build_fails_closed_when_risk_manager_blocks():
    risk = MagicMock()
    risk.SAFE_ALPHA_MAX_RISK_PCT = 0.75
    risk.evaluate.return_value = SimpleNamespace(
        allowed=False, account_risk_pct=0.75, reasons=["kill-switch: daily defensive mode active"]
    )
    with pytest.raises(RuntimeError, match="RiskManager blocked MicroFlow"):
        _runtime(risk)._build_plan(_candidate())
    risk.evaluate.assert_called_once()


def test_microflow_build_fails_closed_when_risk_evaluation_raises():
    risk = MagicMock()
    risk.SAFE_ALPHA_MAX_RISK_PCT = 0.75
    risk.evaluate.side_effect = RuntimeError("risk state unavailable")
    with pytest.raises(RuntimeError, match="risk state unavailable"):
        _runtime(risk)._build_plan(_candidate())


def test_microflow_passes_authenticated_equity_and_proposed_notional_to_risk():
    risk = MagicMock()
    risk.SAFE_ALPHA_MAX_RISK_PCT = 0.75
    risk.evaluate.return_value = SimpleNamespace(
        allowed=True, account_risk_pct=0.75, reasons=["risk gate passed"]
    )
    plan = _runtime(risk)._build_plan(_candidate())
    call = risk.evaluate.call_args
    assert call.kwargs["observed_equity"] == pytest.approx(100.0)
    assert call.kwargs["proposed_notional_usdt"] == pytest.approx(450.0)
    assert plan.position_notional_usdt == pytest.approx(450.0)


def test_microflow_session_or_probe_multiplier_reduces_real_notional():
    risk = MagicMock()
    risk.SAFE_ALPHA_MAX_RISK_PCT = 0.75
    risk.evaluate.return_value = SimpleNamespace(
        allowed=True, account_risk_pct=0.375, reasons=["session risk window active"]
    )
    plan = _runtime(risk)._build_plan(_candidate())
    # Full sizing is 100 equity * 90% / 2 slots * 10x = 450 USDT.
    assert plan.position_notional_usdt == pytest.approx(225.0)
    assert plan.account_risk_pct == pytest.approx(0.375)


def test_daily_kill_switch_runs_even_without_backtest_summary(monkeypatch):
    rm = RiskManager(settings=_settings(account_equity_usdt=100.0, weekly_freeze_loss_pct=0.0))
    monkeypatch.setattr(rm, "_latest_backtest_summary", lambda: {})
    monkeypatch.setattr(rm, "_latest_strategy_expectancy", lambda: {})
    monkeypatch.setattr(rm, "_weekly_realized_pnl", lambda: 0.0)
    monkeypatch.setattr(rm, "_daily_defensive_status", lambda: {
        "daily_total_net_pnl": -2.5,
        "consecutive_losses": 3,
    })
    allowed, reasons = rm._kill_switch_gate(SimpleNamespace(
        strategy="microflow_scalper_v1", symbol="BTCUSDT", direction="LONG", notes=[]
    ))
    assert not allowed
    assert any("daily defensive mode" in reason for reason in reasons)
    assert any("consecutive loss limit" in reason for reason in reasons)


def test_portfolio_equity_breaker_persists_high_water_and_blocks_two_percent(tmp_path, monkeypatch):
    path = tmp_path / "portfolio_equity_guard.json"
    monkeypatch.setattr(equity_mod, "PORTFOLIO_EQUITY_GUARD_PATH", path)
    now = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)
    assert portfolio_equity_drawdown_gate(_settings(), observed_equity=100.0, now=now)[0]
    allowed, reason = portfolio_equity_drawdown_gate(
        _settings(), observed_equity=98.0, now=now
    )
    assert not allowed
    assert "portfolio equity breaker active" in reason


def test_portfolio_equity_breaker_fails_closed_on_corrupt_state(tmp_path, monkeypatch):
    path = tmp_path / "portfolio_equity_guard.json"
    path.write_text("not-json")
    monkeypatch.setattr(equity_mod, "PORTFOLIO_EQUITY_GUARD_PATH", path)
    allowed, reason = portfolio_equity_drawdown_gate(
        _settings(), observed_equity=100.0,
        now=datetime(2026, 8, 20, 8, tzinfo=timezone.utc),
    )
    assert not allowed
    assert "state unreadable" in reason


def test_projected_portfolio_notional_limit_blocks_entry(monkeypatch):
    rm = RiskManager(settings=_settings(max_total_exposure_pct=200.0))
    monkeypatch.setattr(rm, "_load_open_positions", lambda: [])
    allowed, reasons = rm._directional_exposure_gate(
        SimpleNamespace(symbol="BTCUSDT", direction="LONG"),
        proposed_notional_usdt=250.0,
        observed_equity=100.0,
    )
    assert not allowed
    assert any("MAX_TOTAL_EXPOSURE_PCT" in reason for reason in reasons)


def test_unreadable_position_state_blocks_cluster_and_portfolio(monkeypatch):
    rm = RiskManager(settings=_settings(max_correlated_positions=2, max_cluster_exposure_pct=120.0))
    monkeypatch.setattr(rm, "_load_open_positions", lambda: None)
    candidate = SimpleNamespace(symbol="BTCUSDT", direction="LONG")
    assert rm._cluster_risk_gate(candidate)[0] is False
    assert rm._directional_exposure_gate(candidate)[0] is False


def _wired_risk_manager(monkeypatch, tmp_path, **setting_overrides):
    defaults = dict(
        account_equity_usdt=100.0,
        weekly_freeze_loss_pct=7.0,
        max_correlated_positions=2,
        max_cluster_exposure_pct=1000.0,
        max_same_direction_positions=2,
        enabled_strategy_set={"microflow_scalper_v1"},
        enable_shorts=True,
        execution_mode="LIVE",
        is_live_execution=True,
        session_risk_reduction_windows_utc="",
        session_risk_multiplier=0.5,
    )
    defaults.update(setting_overrides)
    settings = _settings(**defaults)
    rm = RiskManager(settings=settings)
    monkeypatch.setattr(equity_mod, "PORTFOLIO_EQUITY_GUARD_PATH", tmp_path / "guard.json")
    monkeypatch.setattr(rm, "_latest_backtest_summary", lambda: {})
    monkeypatch.setattr(rm, "_latest_strategy_expectancy", lambda: {})
    monkeypatch.setattr(rm, "_weekly_realized_pnl", lambda: 0.0)
    monkeypatch.setattr(rm, "_daily_defensive_status", lambda: {
        "daily_total_net_pnl": 0.0, "consecutive_losses": 0,
    })
    monkeypatch.setattr(rm, "_strategy_weighting_gate", lambda candidate: (True, [], False))
    monkeypatch.setattr(rm, "_ai_agent_gate", lambda candidate: (True, [], False))
    monkeypatch.setattr(rm, "_execution_cost_gate", lambda candidate: (True, []))
    monkeypatch.setattr(rm, "_load_open_positions", lambda: [])
    return rm


def _risk_input():
    return MicroflowLiveRuntime._risk_candidate(_candidate())


def _evaluate(rm, *, equity=100.0, notional=100.0):
    return rm.evaluate(
        _risk_input(),
        StrategyScore(total=100.0, breakdown={}, verdict="GO", reasons=[]),
        observed_equity=equity,
        proposed_notional_usdt=notional,
    )


def test_full_microflow_risk_approval_path(monkeypatch, tmp_path):
    verdict = _evaluate(_wired_risk_manager(monkeypatch, tmp_path))
    assert verdict.allowed
    assert verdict.status == "EXECUTABLE"


@pytest.mark.parametrize("daily_pnl,consecutive,weekly_pnl,expected", [
    (-2.1, 0, 0.0, "daily defensive mode"),
    (0.0, 3, 0.0, "consecutive loss limit"),
    (0.0, 0, -7.1, "weekly freeze active"),
])
def test_full_microflow_risk_path_blocks_loss_breakers(
    monkeypatch, tmp_path, daily_pnl, consecutive, weekly_pnl, expected
):
    rm = _wired_risk_manager(monkeypatch, tmp_path)
    monkeypatch.setattr(rm, "_daily_defensive_status", lambda: {
        "daily_total_net_pnl": daily_pnl, "consecutive_losses": consecutive,
    })
    monkeypatch.setattr(rm, "_weekly_realized_pnl", lambda: weekly_pnl)
    verdict = _evaluate(rm)
    assert not verdict.allowed
    assert any(expected in reason for reason in verdict.reasons)


def test_full_microflow_risk_path_blocks_correlated_exposure(monkeypatch, tmp_path):
    rm = _wired_risk_manager(monkeypatch, tmp_path, max_correlated_positions=1)
    monkeypatch.setattr(rm, "_load_open_positions", lambda: [{
        "symbol": "ETHUSDT", "direction": "LONG", "status": "OPEN",
        "position_notional_usdt": 50.0,
    }])
    verdict = _evaluate(rm)
    assert not verdict.allowed
    assert any("cluster limit reached" in reason for reason in verdict.reasons)


def test_full_microflow_risk_path_applies_session_reduction(monkeypatch, tmp_path):
    rm = _wired_risk_manager(monkeypatch, tmp_path)
    monkeypatch.setattr(rm, "_session_risk_multiplier", lambda: (0.5, "session risk active"))
    verdict = _evaluate(rm)
    assert verdict.allowed
    assert verdict.account_risk_pct == pytest.approx(0.375)
    assert "session risk active" in verdict.reasons


def test_full_microflow_risk_path_fails_closed_on_daily_state_ambiguity(monkeypatch, tmp_path):
    rm = _wired_risk_manager(monkeypatch, tmp_path)
    monkeypatch.setattr(rm, "_daily_defensive_status", lambda: {"daily_status_unreadable": True})
    verdict = _evaluate(rm)
    assert not verdict.allowed
    assert any("failing closed" in reason for reason in verdict.reasons)


def test_equity_breaker_survives_risk_manager_restart(monkeypatch, tmp_path):
    path = tmp_path / "guard.json"
    monkeypatch.setattr(equity_mod, "PORTFOLIO_EQUITY_GUARD_PATH", path)
    first = _wired_risk_manager(monkeypatch, tmp_path)
    assert _evaluate(first, equity=100.0).allowed
    restarted = _wired_risk_manager(monkeypatch, tmp_path)
    verdict = _evaluate(restarted, equity=97.9)
    assert not verdict.allowed
    assert any("portfolio equity breaker active" in reason for reason in verdict.reasons)
