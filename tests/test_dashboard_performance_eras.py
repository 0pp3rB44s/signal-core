"""MicroFlow losses and AdaptiveTrend results must never be silently summed."""

from __future__ import annotations

import pytest

from dashboard_v3.panels import performance_eras as eras

EX = "bitget_position_history"


def _close(strategy, net, **kw):
    row = {"event_type": "CLOSE", "sync_source": EX, "strategy": strategy,
           "symbol": "BTCUSDT", "direction": "LONG", "net_pnl": str(net),
           "closed_at": "2026-08-20T10:00:00+00:00",
           "position_lifecycle_id": kw.pop("lid", f"pos-{strategy}-{net}")}
    row.update({k: str(v) for k, v in kw.items()})
    return row


def test_microflow_only_history_does_not_appear_as_adaptive_trend():
    rows = [_close(eras.MICROFLOW, -5.0, lid="a"), _close(eras.MICROFLOW, -3.0, lid="b")]
    live = eras.compute(rows, "ADAPTIVETREND_LIVE")
    legacy = eras.compute(rows, "MICROFLOW_LEGACY")
    assert live.trades == 0 and live.evidence == "NO_DATA"
    assert live.net_pnl is None
    assert legacy.trades == 2
    assert legacy.net_pnl == pytest.approx(-8.0)


def test_adaptive_trend_live_empty_is_no_data_not_zero():
    """Zero trades must read NO_DATA, never '0.00 net' which looks like a result."""
    m = eras.compute([], "ADAPTIVETREND_LIVE")
    assert m.trades == 0
    assert m.net_pnl is None and m.profit_factor is None and m.win_rate is None
    assert m.evidence == "NO_DATA"


def test_all_historical_is_the_only_scope_that_combines():
    rows = [_close(eras.MICROFLOW, -5.0, lid="a"), _close(eras.ADAPTIVE_TREND, 2.0, lid="b")]
    assert eras.compute(rows, "ALL_HISTORICAL").net_pnl == pytest.approx(-3.0)
    assert eras.compute(rows, "MICROFLOW_LEGACY").net_pnl == pytest.approx(-5.0)
    assert eras.compute(rows, "ADAPTIVETREND_LIVE").net_pnl == pytest.approx(2.0)


def test_non_economic_rows_are_excluded_from_every_scope():
    rows = [_close(eras.MICROFLOW, -5.0, lid="a"),
            {**_close(eras.MICROFLOW, -99.0, lid="b"), "sync_source": ""}]
    assert eras.compute(rows, "MICROFLOW_LEGACY").trades == 1


def test_duplicate_lifecycle_counted_once():
    dup = _close(eras.MICROFLOW, -5.0, lid="same")
    assert eras.compute([dup, dup], "MICROFLOW_LEGACY").trades == 1


def test_rows_without_lifecycle_id_dedupe_on_symbol_direction_time():
    row = _close(eras.MICROFLOW, -5.0, lid="")
    assert eras.compute([row, dict(row)], "MICROFLOW_LEGACY").trades == 1


def test_shadow_scope_is_never_priced():
    """Shadow decisions have no fills, so they must carry no PnL at all."""
    m = eras.compute([_close(eras.ADAPTIVE_TREND, 5.0)], "ADAPTIVETREND_SHADOW")
    assert m.net_pnl is None and m.profit_factor is None and m.trades == 0


def test_evidence_states_track_sample_size():
    assert eras.evidence_state(0) == "NO_DATA"
    assert eras.evidence_state(1) == "TINY_SAMPLE"
    assert eras.evidence_state(9) == "TINY_SAMPLE"
    assert eras.evidence_state(10) == "DESCRIPTIVE"
    assert eras.evidence_state(30) == "REASONABLE_SAMPLE"


def test_small_sample_is_not_statistically_meaningful():
    rows = [_close(eras.MICROFLOW, -1.0, lid=f"p{i}") for i in range(4)]
    m = eras.compute(rows, "MICROFLOW_LEGACY")
    assert m.evidence == "TINY_SAMPLE"
    assert m.statistically_meaningful is False


def test_profit_factor_and_drawdown(pytestconfig):
    rows = [_close(eras.MICROFLOW, 4.0, lid="w1"), _close(eras.MICROFLOW, -2.0, lid="l1"),
            _close(eras.MICROFLOW, -2.0, lid="l2")]
    m = eras.compute(rows, "MICROFLOW_LEGACY")
    assert m.profit_factor == pytest.approx(1.0)
    assert m.win_rate == pytest.approx(1 / 3)
    assert m.max_drawdown == pytest.approx(4.0)


def test_shadow_metrics_counts_decisions_only():
    class V:
        def __init__(self, d): self.decision = d
    out = eras.shadow_metrics([V("ACCOUNT_FREEZE_BLOCKED"), V("ACCOUNT_FREEZE_BLOCKED"), V(None)])
    assert out["decisions"] == 3
    assert out["decision_counts"]["ACCOUNT_FREEZE_BLOCKED"] == 2
    assert out["decision_counts"]["UNKNOWN"] == 1
    assert "never summed" in out["note"]


def test_compute_all_returns_every_scope_separately():
    out = eras.compute_all([_close(eras.MICROFLOW, -5.0)], [])
    scopes = {m.scope for m in out["scopes"]}
    assert scopes == {"ADAPTIVETREND_LIVE", "ADAPTIVETREND_SHADOW",
                      "MICROFLOW_LEGACY", "ALL_HISTORICAL"}
