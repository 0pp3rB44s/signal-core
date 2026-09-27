"""AdaptiveTrend panel — the strategy the dashboard previously could not see."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from dashboard_v3.core import sources
from dashboard_v3.core.status import Status
from dashboard_v3.panels import adaptive_trend as at

NOW = datetime(2026, 8, 26, 14, 30, 0, tzinfo=timezone.utc)


@pytest.fixture
def base(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "BASE_PATH", tmp_path)
    return tmp_path


def _shadow(base, rows):
    p = base / at.SHADOW_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _row(**kw):
    base = {"timestamp": (NOW - timedelta(hours=1)).isoformat(), "symbol": "BTCUSDT",
            "side": "LONG", "six_h_close": 79046.4, "mom": 0.052, "atr": 1684.6,
            "mom_strength": 1.73, "entry_candidate": 79046.4, "initial_stop": 74834.9,
            "risk_pct": 0.0097, "notional": 5.0, "decision": "ACCOUNT_FREEZE_BLOCKED",
            "rejection_reason": "weekly_freeze_active",
            "strategy_version": "adaptive_trend_tsmom_v1", "code_sha": "63a0d57"}
    base.update(kw)
    return base


def test_six_hour_boundary_is_the_next_utc_quarter_day():
    assert at.next_boundary(NOW) == datetime(2026, 8, 26, 18, 0, tzinfo=timezone.utc)
    on_boundary = datetime(2026, 8, 26, 18, 0, tzinfo=timezone.utc)
    assert at.next_boundary(on_boundary) == datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
    assert at.next_boundary(datetime(2026, 8, 26, 23, 59, tzinfo=timezone.utc)) == \
        datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)


def test_missing_shadow_log_is_unknown_not_empty(base):
    out = at.build(now=NOW)
    assert out["signal_status"] is Status.UNKNOWN
    assert out["shadow_rows"] == 0
    # Every universe symbol still appears, as UNKNOWN.
    assert [v.symbol for v in out["per_symbol"]] == list(at.SYMBOL_UNIVERSE)
    assert all(v.mom is None for v in out["per_symbol"])


def test_latest_row_per_symbol_wins(base):
    _shadow(base, [
        _row(timestamp=(NOW - timedelta(hours=8)).isoformat(), mom=0.01),
        _row(timestamp=(NOW - timedelta(hours=1)).isoformat(), mom=0.052),
    ])
    out = at.build(now=NOW)
    btc = next(v for v in out["per_symbol"] if v.symbol == "BTCUSDT")
    assert btc.mom == pytest.approx(0.052)
    assert out["shadow_rows"] == 2


def test_actionable_is_measured_against_the_frozen_threshold(base):
    _shadow(base, [_row(mom=0.052)])
    btc = next(v for v in at.build(now=NOW)["per_symbol"] if v.symbol == "BTCUSDT")
    assert btc.actionable is True
    assert btc.distance_from_threshold == pytest.approx(0.052 - at.MOM_THRESHOLD)


def test_below_threshold_is_not_actionable(base):
    _shadow(base, [_row(mom=0.01)])
    btc = next(v for v in at.build(now=NOW)["per_symbol"] if v.symbol == "BTCUSDT")
    assert btc.actionable is False
    assert btc.distance_from_threshold == pytest.approx(0.01 - at.MOM_THRESHOLD)


def test_short_side_uses_absolute_momentum(base):
    _shadow(base, [_row(side="SHORT", mom=-0.052)])
    btc = next(v for v in at.build(now=NOW)["per_symbol"] if v.symbol == "BTCUSDT")
    assert btc.actionable is True


def test_absent_momentum_is_unknown_not_false(base):
    _shadow(base, [_row(mom=None)])
    btc = next(v for v in at.build(now=NOW)["per_symbol"] if v.symbol == "BTCUSDT")
    assert btc.actionable is None
    assert btc.distance_from_threshold is None


def test_stale_shadow_log_is_flagged(base):
    _shadow(base, [_row(timestamp=(NOW - timedelta(hours=20)).isoformat())])
    out = at.build(now=NOW)
    assert out["signal_status"] is Status.STALE


def test_recent_shadow_log_is_healthy(base):
    _shadow(base, [_row(timestamp=(NOW - timedelta(hours=2)).isoformat())])
    assert at.build(now=NOW)["signal_status"] is Status.HEALTHY


def test_freeze_blocked_decisions_are_counted(base):
    _shadow(base, [_row(), _row(timestamp=(NOW - timedelta(hours=7)).isoformat())])
    out = at.build(now=NOW)
    assert out["decision_counts"]["ACCOUNT_FREEZE_BLOCKED"] == 2
    assert out["blocked_count"] == 2


def test_malformed_rows_do_not_break_the_panel(base):
    p = base / at.SHADOW_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(_row()) + "\n{ not json }\n", encoding="utf-8")
    out = at.build(now=NOW)
    assert out["signal_status"] in (Status.HEALTHY, Status.STALE, Status.UNKNOWN)


def test_spec_values_match_the_frozen_strategy(base):
    """If the strategy is retuned, this test fails and the panel stops lying."""
    from strategies import adaptive_trend_tsmom as spec
    out = at.build(now=NOW)
    assert out["strategy_id"] == spec.STRATEGY_VERSION
    assert out["spec"]["mom_lookback"] == spec.MOM_LOOKBACK
    assert out["spec"]["mom_threshold"] == spec.MOM_THRESHOLD
    assert out["spec"]["atr_period"] == spec.ATR_PERIOD
    assert out["spec"]["atr_mult"] == spec.ATR_MULT
    assert out["spec"]["max_open_positions"] == spec.MAX_OPEN_POSITIONS
    assert tuple(out["universe"]) == spec.SYMBOL_UNIVERSE
