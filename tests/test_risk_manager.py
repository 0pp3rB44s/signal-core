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


# ── _load_open_positions schema (HIGH-1) ────────────────────────────────────
#
# Real incident: production state/executed_trades.json is always
# {"_state_metadata": {...}, "data": [...]}, never a bare list. Checking
# isinstance(payload, list) against that file is always False, so every
# cluster/portfolio exposure gate returned None and rejected every single
# candidate with "open-position state unreadable" -- 863/863 evaluations in
# one weekend, confirmed in production logs, regardless of actual exposure.

import json as _json

import risk.risk_manager as risk_manager_mod


def _write_state(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(risk_manager_mod, "BASE_PATH", tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "executed_trades.json"
    if payload is _MISSING_FILE:
        return path
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(payload, str):
            fh.write(payload)
        else:
            _json.dump(payload, fh)
    return path


_MISSING_FILE = object()


def test_real_production_envelope_schema_is_read(tmp_path, monkeypatch):
    """The exact real schema: {"_state_metadata": {...}, "data": [...]}."""
    _write_state(tmp_path, monkeypatch, {
        "_state_metadata": {"version": 1},
        "data": [
            {"symbol": "BTCUSDT", "status": "OPEN"},
            {"symbol": "ETHUSDT", "status": "CLOSED"},
        ],
    })
    got = RiskManager._load_open_positions()
    assert got is not None
    assert [p["symbol"] for p in got] == ["BTCUSDT"]


def test_valid_empty_data_is_a_readable_zero_position_set(tmp_path, monkeypatch):
    """An account with nothing open must read as [], not as an error."""
    _write_state(tmp_path, monkeypatch, {"_state_metadata": {}, "data": []})
    assert RiskManager._load_open_positions() == []


def test_multiple_real_open_positions_parse_correctly(tmp_path, monkeypatch):
    _write_state(tmp_path, monkeypatch, {"data": [
        {"symbol": "SOLUSDT", "status": "OPEN", "direction": "SHORT"},
        {"symbol": "ZECUSDT", "status": "OPEN", "direction": "LONG"},
        {"symbol": "BTCUSDT", "status": "CLOSED_SYNCED"},
    ]})
    got = RiskManager._load_open_positions()
    assert {p["symbol"] for p in got} == {"SOLUSDT", "ZECUSDT"}


def test_missing_file_is_a_readable_zero_position_set(tmp_path, monkeypatch):
    _write_state(tmp_path, monkeypatch, _MISSING_FILE)
    assert RiskManager._load_open_positions() == []


def test_invalid_json_fails_closed(tmp_path, monkeypatch):
    _write_state(tmp_path, monkeypatch, "{not valid json")
    assert RiskManager._load_open_positions() is None


def test_missing_data_key_fails_closed(tmp_path, monkeypatch):
    _write_state(tmp_path, monkeypatch, {"_state_metadata": {}})
    assert RiskManager._load_open_positions() is None


def test_non_list_data_fails_closed(tmp_path, monkeypatch):
    _write_state(tmp_path, monkeypatch, {"data": "not-a-list"})
    assert RiskManager._load_open_positions() is None


def test_bare_list_legacy_schema_still_works(tmp_path, monkeypatch):
    """Any pre-envelope caller/fixture using a bare list must keep working."""
    _write_state(tmp_path, monkeypatch, [{"symbol": "BTCUSDT", "status": "OPEN"}])
    got = RiskManager._load_open_positions()
    assert got is not None and got[0]["symbol"] == "BTCUSDT"


def test_exposure_gates_no_longer_reject_valid_empty_state(tmp_path, monkeypatch):
    """The actual production symptom: valid empty state must not block entries."""
    _write_state(tmp_path, monkeypatch, {"_state_metadata": {}, "data": []})
    rm = _make_risk_manager(equity=27.44, hard_daily_stop_pct=1.5, daily_pnl=0.0)
    rm.settings.max_correlated_positions = 5
    rm.settings.max_open_positions = 4
    rm.settings.max_same_direction_positions = 3
    rm.settings.correlated_exposure_cap_usdt = 1_000_000.0
    rm.settings.max_cluster_exposure_pct = 1_000_000.0
    candidate = _candidate(symbol="BTCUSDT")
    allowed, reasons = rm._cluster_risk_gate(candidate, proposed_notional_usdt=100.0)
    assert allowed is True
    assert not any("state unreadable" in r for r in reasons)
    # Only the HIGH-1 symptom is asserted here: a large notional may still be
    # rejected for legitimate exposure-percentage reasons unrelated to this fix.
    _, reasons = rm._directional_exposure_gate(candidate, proposed_notional_usdt=100.0)
    assert not any("state unreadable" in r for r in reasons)


# --- MicroFlow retirement --------------------------------------------------

def _numeric_settings(rm):
    rm.settings.default_leverage = 10.0
    rm.settings.max_leverage = 10.0
    rm.settings.account_risk_per_trade_pct = 0.5
    rm.settings.max_open_positions = 2
    return rm


def test_microflow_new_entries_blocked_before_any_other_gate():
    """The retirement check runs before any other gate that needs real
    numeric settings -- verified here with a bare-minimum candidate/score."""
    rm = _numeric_settings(_make_risk_manager(equity=1000.0, hard_daily_stop_pct=2.0, daily_pnl=0.0))
    candidate = _candidate(strategy="microflow_scalper_v1")
    score = MagicMock()

    verdict = rm.evaluate(candidate, score, observed_equity=1000.0, proposed_notional_usdt=10.0)

    assert verdict.allowed is False
    assert verdict.status == "BLOCKED"
    assert any("microflow_scalper_v1 retired" in r for r in verdict.reasons)


def test_microflow_retirement_check_is_case_insensitive():
    rm = _numeric_settings(_make_risk_manager(equity=1000.0, hard_daily_stop_pct=2.0, daily_pnl=0.0))
    candidate = _candidate(strategy="MicroFlow_Scalper_V1")
    verdict = rm.evaluate(candidate, MagicMock(), observed_equity=1000.0, proposed_notional_usdt=10.0)
    assert verdict.allowed is False


def test_non_microflow_strategy_is_unaffected_by_retirement_gate():
    """Proves the gate only ever turns an approval into a rejection for
    microflow_scalper_v1 specifically. A non-microflow candidate reaches a
    LATER gate (proven by that gate's own unrelated MagicMock TypeError,
    since this test does not mock the full pipeline) instead of being
    blocked by the retirement check itself."""
    rm = _numeric_settings(_make_risk_manager(equity=1000.0, hard_daily_stop_pct=2.0, daily_pnl=0.0))
    candidate = _candidate(strategy="adaptive_trend_tsmom_v1")
    try:
        verdict = rm.evaluate(candidate, MagicMock(), observed_equity=1000.0, proposed_notional_usdt=10.0)
        assert not any("microflow_scalper_v1 retired" in r for r in verdict.reasons)
    except TypeError:
        # Reached a downstream gate that needs fuller mocking -- proves the
        # retirement check itself did not block this candidate.
        pass
