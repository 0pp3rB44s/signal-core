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


def _make_weekly_freeze_rm(enabled: bool, weekly_pnl: float, equity: float = 58.8) -> RiskManager:
    settings = MagicMock()
    settings.account_equity_usdt = equity
    settings.hard_daily_stop_pct = 2.0
    settings.weekly_freeze_loss_pct = 7.0
    settings.weekly_freeze_enabled = enabled
    rm = RiskManager(settings=settings)
    rm._latest_backtest_summary = lambda: {"by_strategy": {}, "by_symbol": {}}
    rm._latest_strategy_expectancy = lambda: {}
    rm._weekly_realized_pnl = lambda: weekly_pnl
    rm._daily_defensive_status = lambda: {"daily_total_net_pnl": 0.0, "consecutive_losses": 0, "report_readable": True}
    return rm


def test_weekly_freeze_fires_when_enabled():
    # -4.28 on 58.8 equity = 7.27% > 7% -> freeze active.
    rm = _make_weekly_freeze_rm(enabled=True, weekly_pnl=-4.28)
    allowed, reasons = rm._kill_switch_gate(_candidate())
    assert not allowed
    assert any("weekly freeze active" in r for r in reasons)


def test_weekly_freeze_on_hold_when_disabled():
    # Same loss, switch off -> weekly freeze must NOT fire.
    rm = _make_weekly_freeze_rm(enabled=False, weekly_pnl=-4.28)
    allowed, reasons = rm._kill_switch_gate(_candidate())
    assert allowed
    assert not any("weekly freeze active" in r for r in reasons)


def test_daily_stop_still_fires_with_weekly_freeze_disabled():
    # Disabling the weekly freeze must NOT disable the daily-stop brake.
    rm = _make_weekly_freeze_rm(enabled=False, weekly_pnl=0.0)
    rm._daily_defensive_status = lambda: {"daily_total_net_pnl": -5.0, "consecutive_losses": 0, "report_readable": True}
    allowed, reasons = rm._kill_switch_gate(_candidate())
    assert not allowed
    assert any("daily" in r for r in reasons)


def test_strategy_weighting_pause_status_hard_blocks():
    # A PAUSE verdict from the learning report with a solid sample must stop
    # the strategy completely; probe-mode data collection at live fee cost
    # is what bled low_vol_reclaim in July 2026.
    rm = _make_weighting_risk_manager({
        "low_vol_reclaim": {"trades": 234, "expectancy": -0.0265, "tp1_hit_rate": 0.10, "status": "PAUSE"},
    })
    allowed, reasons, probe = rm._strategy_weighting_gate(_candidate(strategy="low_vol_reclaim"))
    assert not allowed
    assert not probe
    assert any("HARD-PAUSE" in r for r in reasons)


def test_strategy_weighting_pause_status_small_sample_still_probes():
    # With a thin sample a PAUSE verdict is not statistically trustworthy;
    # fall through to the regular probe path instead of hard-blocking.
    rm = _make_weighting_risk_manager({
        "momentum_breakdown": {"trades": 14, "expectancy": -0.05, "tp1_hit_rate": None, "status": "PAUSE"},
    })
    allowed, reasons, probe = rm._strategy_weighting_gate(_candidate(strategy="momentum_breakdown"))
    assert allowed
    assert probe


def test_strategy_weighting_requalify_probe_after_geometry_fix():
    # Herkwalificatie (eigenaar-besluit 2026-07-12): een PAUSE uit het oude
    # 30d-venster + gezonde winrate (geometrie-slachtoffer) + klein post-fix
    # cohort -> probe i.p.v. hard-pauze, zodat een gerepareerde strategie
    # zich met vers bewijs kan herkwalificeren (max 15 cohort-trades).
    rm = _make_weighting_risk_manager({
        "momentum_breakout": {
            "trades": 32, "expectancy": -0.075, "winrate": 0.486,
            "tp1_hit_rate": 0.0, "status": "PAUSE",
            "fresh_since_geometry_fix": {"trades": 3, "expectancy": -0.04},
        },
    })
    allowed, reasons, probe = rm._strategy_weighting_gate(_candidate(strategy="momentum_breakout"))
    assert allowed
    assert probe
    assert any("REQUALIFY-PROBE" in r for r in reasons)


def test_strategy_weighting_requalify_denied_for_low_winrate():
    # Structurele verliezers (lage winrate) blijven hard dicht — de
    # herkwalificatie is alleen voor geometrie-slachtoffers, niet voor
    # low_vol_reclaim-achtige selectieproblemen (eigenaar-pauze 2026-07-10).
    rm = _make_weighting_risk_manager({
        "low_vol_reclaim": {
            "trades": 257, "expectancy": -0.028, "winrate": 0.358,
            "tp1_hit_rate": 0.09, "status": "PAUSE",
            "fresh_since_geometry_fix": {"trades": 0, "expectancy": 0.0},
        },
    })
    allowed, reasons, probe = rm._strategy_weighting_gate(_candidate(strategy="low_vol_reclaim"))
    assert not allowed
    assert any("HARD-PAUSE" in r for r in reasons)


def test_strategy_weighting_requalify_cohort_full_hard_pauses_again():
    # Cohort vol (>=15) en nog steeds PAUSE -> het verse bewijs is negatief;
    # de hard-pauze geldt weer onverkort.
    rm = _make_weighting_risk_manager({
        "momentum_breakout": {
            "trades": 40, "expectancy": -0.06, "winrate": 0.45,
            "tp1_hit_rate": 0.05, "status": "PAUSE",
            "fresh_since_geometry_fix": {"trades": 15, "expectancy": -0.05},
        },
    })
    allowed, reasons, probe = rm._strategy_weighting_gate(_candidate(strategy="momentum_breakout"))
    assert not allowed
    assert any("HARD-PAUSE" in r for r in reasons)


def _sweep_candidate(close_pos: float, participation: float, followthrough: float, direction: str = "LONG") -> MagicMock:
    candidate = _candidate(strategy="liquidity_sweep_reversal")
    candidate.direction = direction
    candidate.notes = [
        f"close_pos={close_pos}",
        f"participation_score={participation}",
        f"followthrough_volume_ratio={followthrough}",
    ]
    return candidate


def test_execution_cost_gate_allows_strong_sweep_reclaim_at_extreme_close():
    # A sweep reversal closes near its extreme by design; with strong
    # participation the close-position block must not fire.
    rm = _make_risk_manager(equity=1000.0, hard_daily_stop_pct=2.0, daily_pnl=0.0)
    allowed, reasons = rm._execution_cost_gate(_sweep_candidate(0.95, 1.10, 0.80))
    assert allowed, reasons


def test_execution_cost_gate_still_blocks_weak_sweep_at_extreme_close():
    rm = _make_risk_manager(equity=1000.0, hard_daily_stop_pct=2.0, daily_pnl=0.0)
    allowed, reasons = rm._execution_cost_gate(_sweep_candidate(0.95, 0.40, 0.20))
    assert not allowed
    assert any("entry too high in candle" in r for r in reasons)


def test_execution_cost_gate_allows_strong_breakout_at_extreme_close():
    # A momentum breakout closes at its high by design and is filled by a
    # limit ladder below the close (PLANNER_LADDER_STEPS=3), so a high
    # close_pos is not a bad fill. Strong participation must pass this gate
    # (e.g. ENAUSDT LONG score=99 that was blocked solely on close_pos).
    rm = _make_risk_manager(equity=1000.0, hard_daily_stop_pct=2.0, daily_pnl=0.0)
    candidate = _candidate(strategy="momentum_breakout")
    candidate.direction = "LONG"
    candidate.notes = ["close_pos=1.000", "participation_score=3.25", "followthrough_volume_ratio=4.97"]
    allowed, reasons = rm._execution_cost_gate(candidate)
    assert allowed, reasons


def test_execution_cost_gate_still_blocks_weak_breakout_at_extreme_close():
    # A breakout with no real participation stays blocked at the extreme close.
    rm = _make_risk_manager(equity=1000.0, hard_daily_stop_pct=2.0, daily_pnl=0.0)
    candidate = _candidate(strategy="momentum_breakout")
    candidate.direction = "LONG"
    candidate.notes = ["close_pos=0.95", "participation_score=0.40", "followthrough_volume_ratio=0.20"]
    allowed, reasons = rm._execution_cost_gate(candidate)
    assert not allowed
    assert any("entry too high in candle" in r for r in reasons)


def test_execution_cost_gate_still_blocks_unrelated_strategy_at_extreme_close():
    # Strategies that do NOT close at their extreme by design (e.g. a pullback
    # continuation) keep the close_pos protection regardless of participation.
    rm = _make_risk_manager(equity=1000.0, hard_daily_stop_pct=2.0, daily_pnl=0.0)
    candidate = _candidate(strategy="trend_continuation")
    candidate.direction = "LONG"
    candidate.notes = ["close_pos=0.95", "participation_score=1.50", "followthrough_volume_ratio=1.00"]
    allowed, reasons = rm._execution_cost_gate(candidate)
    assert not allowed
    assert any("entry too high in candle" in r for r in reasons)


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


# --- Richting-leerlaag (2026-07-13): grijpt alleen in bij echte asymmetrie ---

def _make_direction_risk_manager(directions: dict) -> RiskManager:
    rm = RiskManager(settings=MagicMock())
    rm._latest_strategy_expectancy = lambda: {
        "strategies": {
            "low_vol_reclaim": {"trades": 10, "expectancy": 0.2, "tp1_hit_rate": 0.5},
        },
        "directions": directions,
    }
    return rm


def _direction_candidate(direction: str) -> MagicMock:
    candidate = _candidate()
    candidate.direction = direction
    return candidate


def test_direction_asymmetry_probes_weaker_direction():
    rm = _make_direction_risk_manager({
        "SHORT": {"trades": 40, "expectancy": -0.05, "status": "PAUSE",
                  "fresh_since_geometry_fix": {"trades": 5}},
        "LONG": {"trades": 40, "expectancy": 0.01, "status": "WATCH"},
    })
    allowed, reasons, probe = rm._strategy_weighting_gate(_direction_candidate("SHORT"))
    assert allowed
    assert probe
    assert any("direction weighting PROBE" in r for r in reasons)


def test_direction_symmetric_losses_do_not_trigger():
    # Beide richtingen ~even slecht = strategie-probleem, geen richting-
    # probleem (bot-only analyse 2026-07-13: LONG -0.034 vs SHORT -0.031).
    directions = {
        "SHORT": {"trades": 113, "expectancy": -0.031, "status": "PAUSE"},
        "LONG": {"trades": 88, "expectancy": -0.034, "status": "PAUSE"},
    }
    for side in ("SHORT", "LONG"):
        rm = _make_direction_risk_manager(directions)
        allowed, reasons, probe = rm._strategy_weighting_gate(_direction_candidate(side))
        assert allowed
        assert not probe
        assert not any("direction weighting" in r for r in reasons)


def test_direction_hard_pauses_after_requalify_budget():
    rm = _make_direction_risk_manager({
        "SHORT": {"trades": 60, "expectancy": -0.08, "status": "PAUSE",
                  "fresh_since_geometry_fix": {"trades": 15}},
        "LONG": {"trades": 40, "expectancy": 0.0, "status": "PAUSE"},
    })
    allowed, reasons, probe = rm._strategy_weighting_gate(_direction_candidate("SHORT"))
    assert not allowed
    assert any("direction weighting HARD-PAUSE" in r for r in reasons)


def test_direction_layer_noop_without_directions_section():
    rm = _make_weighting_risk_manager({
        "low_vol_reclaim": {"trades": 10, "expectancy": 0.2, "tp1_hit_rate": 0.5},
    })
    allowed, reasons, probe = rm._strategy_weighting_gate(_candidate())
    assert allowed
    assert not any("direction weighting" in r for r in reasons)
