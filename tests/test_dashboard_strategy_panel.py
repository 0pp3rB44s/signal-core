"""The strategy panel must never invent a number it cannot source.

Two failure modes are guarded here, and they are not the same:

* **Fabricated lineage.** Plans and positions share no key, and "selected" is
  never written down. A conversion rate across that boundary would be made up.
* **Mixed cohorts.** Provisional closes, recovery rows and low-confidence rows
  are not LIVE money. Summing them into strategy economics would overstate or
  double-count the result.
"""

from __future__ import annotations

import time

import pytest

from dashboard_v3.core import sources as src
from dashboard_v3.core.status import Status
from dashboard_v3.panels import strategy as sp


class _Loaded:
    """Stands in for sources.Loaded, carrying a real Provenance.

    The real dataclass is used rather than a hand-rolled stub so a field added
    upstream surfaces here instead of silently diverging.
    """

    def __init__(self, value, source="fixture"):
        self.value = value
        self.provenance = src.Provenance(source=source, mtime_epoch=time.time(),
                                         rows=len(value) if hasattr(value, "__len__") else None)


@pytest.fixture
def feed(monkeypatch):
    """Serve fixture rows in place of the on-disk CSVs."""
    tables: dict[str, list[dict]] = {}

    def fake_csv(rel, limit=None, tail=True):
        return _Loaded(tables.get(rel, []))

    def fake_jsonl(rel, **kwargs):
        return _Loaded(tables.get(rel, []))

    monkeypatch.setattr(src, "load_csv", fake_csv)
    monkeypatch.setattr(src, "load_jsonl_tail", fake_jsonl)
    return tables


def _cand(strategy, cid):
    return {"timestamp": "2026-08-10T00:00:00+00:00", "candidate_id": cid, "strategy": strategy}


def _plan(strategy, cid, verdict="EXECUTABLE", reasons=""):
    return {"timestamp": "2026-08-10T00:00:00+00:00", "candidate_id": cid,
            "plan_id": "p" + cid, "strategy": strategy, "verdict": verdict, "reasons": reasons}


def _close(strategy, lifecycle, gross, fees, funding, net, **extra):
    row = {
        "event_type": "CLOSE", "status": "CLOSED", "strategy": strategy,
        "position_lifecycle_id": lifecycle, "closed_at": "2026-08-10T00:00:00+00:00",
        "gross_pnl": gross, "fees": fees, "funding": funding, "net_pnl": net,
        "sync_source": "bitget_position_history", "data_confidence": "EXCHANGE_TRUTH",
    }
    row.update(extra)
    return row


def _row(rows, name):
    return next(r for r in rows if r["strategy"] == name)


# --- funnel: counts and separation ------------------------------------------


def test_each_strategy_is_counted_separately(feed):
    feed["logs/strategy_candidates.csv"] = [
        _cand("low_vol_reclaim", "a"), _cand("low_vol_reclaim", "b"),
        _cand("momentum_breakout", "c"), _cand("trend_continuation", "d"),
    ]
    feed["logs/trade_plans.csv"] = [
        _plan("low_vol_reclaim", "a"), _plan("low_vol_reclaim", "b", verdict="BLOCKED"),
        _plan("momentum_breakout", "c", verdict="BLOCKED"), _plan("trend_continuation", "d"),
    ]
    rows = sp.build_funnel()["rows"]
    assert _row(rows, "low_vol_reclaim")["candidates"] == 2
    assert _row(rows, "low_vol_reclaim")["executable"] == 1
    assert _row(rows, "momentum_breakout")["executable"] == 0
    assert _row(rows, "trend_continuation")["executable"] == 1


def test_known_strategies_appear_even_with_no_rows(feed):
    rows = sp.build_funnel()["rows"]
    names = [r["strategy"] for r in rows]
    for known in sp.KNOWN_STRATEGIES:
        assert known in names, f"{known} vanished from the funnel"


def test_dormant_strategy_is_labelled_not_omitted(feed):
    rows = sp.build_funnel()["rows"]
    dormant = _row(rows, "adaptive_momentum_continuation")
    assert dormant["dormant"] is True
    assert dormant["candidates"] == 0


def test_unknown_strategy_label_is_kept_not_dropped(feed):
    feed["logs/trade_plans.csv"] = [_plan("some_new_strategy", "x")]
    rows = sp.build_funnel()["rows"]
    assert "some_new_strategy" in [r["strategy"] for r in rows]


def test_missing_strategy_field_becomes_unknown_not_silent(feed):
    feed["logs/trade_plans.csv"] = [{"candidate_id": "x", "plan_id": "px", "verdict": "EXECUTABLE"}]
    rows = sp.build_funnel()["rows"]
    assert _row(rows, "unknown")["executable"] == 1


def test_malformed_rows_do_not_crash_the_panel(feed):
    feed["logs/trade_plans.csv"] = [{}, {"strategy": None, "verdict": None}, _plan("low_vol_reclaim", "a")]
    feed["logs/trade_dataset_v2.csv"] = [{}, {"event_type": "CLOSE"}]
    assert sp.build_funnel()["rows"]


def test_duplicate_open_events_count_one_position(feed):
    feed["logs/trade_dataset_v2.csv"] = [
        {"event_type": "OPEN", "strategy": "low_vol_reclaim", "position_lifecycle_id": "L1"},
        {"event_type": "OPEN", "strategy": "low_vol_reclaim", "position_lifecycle_id": "L1"},
    ]
    assert _row(sp.build_funnel()["rows"], "low_vol_reclaim")["opened"] == 1


# --- funnel: conversion-rate semantics --------------------------------------


def test_rate_is_shown_only_where_the_cohort_is_shared(feed):
    feed["logs/trade_plans.csv"] = [
        _plan("low_vol_reclaim", "a"), _plan("low_vol_reclaim", "b", verdict="BLOCKED"),
    ]
    feed["logs/trade_dataset_v2.csv"] = [
        {"event_type": "OPEN", "strategy": "low_vol_reclaim", "position_lifecycle_id": "L1"},
        {"event_type": "CLOSE", "strategy": "low_vol_reclaim", "position_lifecycle_id": "L1"},
    ]
    row = _row(sp.build_funnel()["rows"], "low_vol_reclaim")
    assert row["plan_to_executable_pct"] == 50.0     # candidate_id joins these
    assert row["opened_to_closed_pct"] == 100.0      # lifecycle id joins these
    # No shared key, and "selected" is never recorded: must stay absent.
    assert row["executable_to_opened_pct"] is None


def test_lineage_is_declared_incomplete(feed):
    built = sp.build_funnel()
    assert all(r["lineage"] == "INCOMPLETE_LINEAGE" for r in built["rows"])
    assert "plan_id" in built["lineage_note"]


def test_rate_is_none_rather_than_zero_when_denominator_is_empty(feed):
    row = _row(sp.build_funnel()["rows"], "low_vol_reclaim")
    assert row["plan_to_executable_pct"] is None
    assert row["opened_to_closed_pct"] is None


# --- rejection reasons ------------------------------------------------------


def test_blocking_reasons_are_grouped_with_digits_masked(feed):
    feed["logs/trade_plans.csv"] = [
        _plan("momentum_breakout", "a", "BLOCKED", "score below Safe Mode minimum: 91 < 95"),
        _plan("momentum_breakout", "b", "BLOCKED", "score below Safe Mode minimum: 88 < 95"),
        _plan("momentum_breakout", "c", "BLOCKED", "blocked: long without bullish primary trend"),
    ]
    reasons = _row(sp.build_funnel()["rows"], "momentum_breakout")["top_reasons"]
    top = reasons[0]
    assert top["count"] == 2 and "<N>" in top["reason"]
    assert top["pct"] == pytest.approx(66.7, abs=0.1)


def test_key_value_telemetry_is_not_reported_as_a_reason(feed):
    feed["logs/trade_plans.csv"] = [
        _plan("momentum_breakout", "a", "BLOCKED", "volume_ratio=0.68 | breakout_pct=0.12"),
    ]
    assert _row(sp.build_funnel()["rows"], "momentum_breakout")["top_reasons"] == []


# --- economics: cohort protection -------------------------------------------


def test_provisional_close_is_excluded(feed):
    feed["logs/trade_dataset_v2.csv"] = [
        dict(_close("low_vol_reclaim", "L1", 1.0, 0.1, 0.0, 0.9), event_type="CLOSE_PROVISIONAL"),
    ]
    built = sp.build_economics()
    assert _row(built["rows"], "low_vol_reclaim")["n"] == 0
    assert built["excluded"]["provisional_or_non_economic"] == 1


def test_provisional_and_authoritative_same_lifecycle_counts_once(feed):
    feed["logs/trade_dataset_v2.csv"] = [
        dict(_close("low_vol_reclaim", "L1", 1.0, 0.1, 0.0, 0.9), event_type="CLOSE_PROVISIONAL"),
        _close("low_vol_reclaim", "L1", 1.0, 0.1, 0.0, 0.9),
    ]
    row = _row(sp.build_economics()["rows"], "low_vol_reclaim")
    assert row["n"] == 1
    assert row["net"] == pytest.approx(0.9)


def test_duplicate_authoritative_lifecycle_counts_once(feed):
    feed["logs/trade_dataset_v2.csv"] = [
        _close("low_vol_reclaim", "L1", 1.0, 0.1, 0.0, 0.9),
        _close("low_vol_reclaim", "L1", 1.0, 0.1, 0.0, 0.9),
    ]
    built = sp.build_economics()
    assert _row(built["rows"], "low_vol_reclaim")["n"] == 1
    assert built["excluded"]["duplicate_lifecycle"] == 1


def test_recovery_rows_are_excluded_from_live_economics(feed):
    feed["logs/trade_dataset_v2.csv"] = [
        _close("recovered_exchange_position", "L1", 5.0, 0.1, 0.0, 4.9),
        _close("low_vol_reclaim", "L2", 1.0, 0.1, 0.0, 0.9, data_confidence="LOW_CONFIDENCE"),
        _close("low_vol_reclaim", "L3", 1.0, 0.1, 0.0, 0.9),
    ]
    built = sp.build_economics()
    assert built["totals"]["n"] == 1
    assert built["totals"]["net"] == pytest.approx(0.9)
    assert built["excluded"]["recovery_or_low_confidence"] == 2


def test_backtest_rows_never_reach_live_economics(feed):
    """Backtest output lives in reports/, which this panel never reads."""
    feed["reports/backtests/strategy_expectancy.json"] = [{"net": 999.0}]
    feed["logs/trade_dataset_v2.csv"] = [_close("low_vol_reclaim", "L1", 1.0, 0.1, 0.0, 0.9)]
    assert sp.build_economics()["totals"]["net"] == pytest.approx(0.9)


# --- economics: the numbers -------------------------------------------------


def test_profitable_and_losing_strategies_are_reported_separately(feed):
    feed["logs/trade_dataset_v2.csv"] = [
        _close("momentum_breakout", "L1", 1.0, 0.1, 0.0, 0.9),
        _close("low_vol_reclaim", "L2", -1.0, 0.1, 0.0, -1.1),
    ]
    rows = sp.build_economics()["rows"]
    assert _row(rows, "momentum_breakout")["net"] == pytest.approx(0.9)
    assert _row(rows, "low_vol_reclaim")["net"] == pytest.approx(-1.1)


def test_gross_positive_but_net_negative_from_fees_is_visible(feed):
    """The defining case: the strategy was right and still lost money."""
    feed["logs/trade_dataset_v2.csv"] = [_close("momentum_breakout", "L1", 0.10, 0.12, 0.0, -0.02)]
    row = _row(sp.build_economics()["rows"], "momentum_breakout")
    assert row["gross"] > 0 and row["net"] < 0
    # Fees exceed gross, so it is stated as a multiple rather than a percentage.
    assert row["fees_pct_of_abs_gross"] is None
    assert row["fees_multiple_of_abs_gross"] == pytest.approx(1.2, abs=0.05)


def test_fee_share_is_a_percentage_when_fees_are_below_gross(feed):
    feed["logs/trade_dataset_v2.csv"] = [_close("low_vol_reclaim", "L1", 1.0, 0.25, 0.0, 0.75)]
    row = _row(sp.build_economics()["rows"], "low_vol_reclaim")
    assert row["fees_pct_of_abs_gross"] == pytest.approx(25.0)
    assert row["fees_multiple_of_abs_gross"] is None


def test_fee_share_withheld_when_gross_is_effectively_zero(feed):
    feed["logs/trade_dataset_v2.csv"] = [_close("low_vol_reclaim", "L1", 0.0001, 0.12, 0.0, -0.12)]
    row = _row(sp.build_economics()["rows"], "low_vol_reclaim")
    assert row["fees_pct_of_abs_gross"] is None
    assert row["fees_multiple_of_abs_gross"] is None


@pytest.mark.parametrize("funding", [0.05, -0.05])
def test_funding_is_reported_in_both_directions(feed, funding):
    feed["logs/trade_dataset_v2.csv"] = [_close("low_vol_reclaim", "L1", 1.0, 0.1, funding, 0.9 + funding)]
    assert _row(sp.build_economics()["rows"], "low_vol_reclaim")["funding"] == pytest.approx(funding)


def test_no_trades_yields_unknown_not_zero(feed):
    row = _row(sp.build_economics()["rows"], "low_vol_reclaim")
    assert row["n"] == 0
    assert row["evidence"] == "NO_DATA"
    assert row["expectancy"] is None and row["winrate"] is None and row["profit_factor"] is None


def test_decomposition_adds_up(feed):
    feed["logs/trade_dataset_v2.csv"] = [
        _close("low_vol_reclaim", "L1", 1.0, 0.10, 0.02, 0.92),
        _close("low_vol_reclaim", "L2", -0.5, 0.10, -0.01, -0.61),
    ]
    row = _row(sp.build_economics()["rows"], "low_vol_reclaim")
    assert row["gross"] == pytest.approx(0.5)
    assert row["fees"] == pytest.approx(0.2)
    assert row["funding"] == pytest.approx(0.01)
    assert row["net"] == pytest.approx(0.31)


# --- evidence labels --------------------------------------------------------


@pytest.mark.parametrize("n,label", [(0, "NO_DATA"), (1, "TINY_SAMPLE"), (9, "TINY_SAMPLE"),
                                     (10, "DESCRIPTIVE"), (29, "DESCRIPTIVE"),
                                     (30, "REASONABLE_SAMPLE"), (500, "REASONABLE_SAMPLE")])
def test_evidence_bands_are_fixed(n, label):
    assert sp.evidence_label(n) == label


def test_tiny_sample_is_labelled_even_when_profitable(feed):
    feed["logs/trade_dataset_v2.csv"] = [_close("momentum_breakout", "L1", 5.0, 0.1, 0.0, 4.9)]
    row = _row(sp.build_economics()["rows"], "momentum_breakout")
    assert row["evidence"] == "TINY_SAMPLE"
    assert "profitable" not in str(row).lower()


# --- ranking boundary -------------------------------------------------------


def test_ranking_reports_absence_and_reconstructs_nothing(feed):
    built = sp.build_ranking()
    assert built["available"] is False
    assert built["state"] == "RANKED_PLAN_TELEMETRY_NOT_AVAILABLE"
    assert built["cycles"] == []
    assert built["status"] is Status.UNKNOWN


def test_ranking_consumes_the_feed_when_present(feed):
    feed[sp.RANKED_PLANS_PATH] = [{"scan_id": "s1", "plans": []}]
    built = sp.build_ranking()
    assert built["available"] is True and len(built["cycles"]) == 1


def test_dynamic_grid_panel_preserves_decision_economics_and_order_lineage(feed, monkeypatch):
    feed[sp.DYNAMIC_GRID_EVENTS_PATH] = [{
        "strategy": "dynamic_grid_v1", "mode": "SHADOW",
        "event_type": "GRID_DECISION", "symbol": "BTCUSDT",
        "regime": "GRID_ALLOWED", "score": 81.0, "center": 100.0,
        "atr": 0.4, "economics": {"expected_net_capture_bps": 8.0},
    }]
    monkeypatch.setattr(src, "load_json", lambda *args, **kwargs: _Loaded({
        "_state_metadata": {}, "data": {"active_grid": {
            "symbol": "BTCUSDT", "levels": [{
                "index": 1, "entry_client_oid": "dgv1-entry",
                "tp_client_oid": "dgv1-tp", "notional_usdt": 10.0,
            }],
        }}
    }))
    built = sp.build_dynamic_grid()
    assert built["available"] is True
    assert built["decisions"][0]["economics"]["expected_net_capture_bps"] == 8.0
    assert built["levels"][0]["entry_client_oid"] == "dgv1-entry"
    assert built["levels"][0]["tp_client_oid"] == "dgv1-tp"


def test_dynamic_grid_panel_calculates_rolling_live_cycle_metrics(feed, monkeypatch):
    feed[sp.DYNAMIC_GRID_EVENTS_PATH] = [
        {
            "strategy": "dynamic_grid_v1", "mode": "LIVE",
            "event_type": "GRID_CYCLE_CLOSED", "net_capture_usdt": 1.0,
            "gross_capture_usdt": 1.2, "fees_usdt": 0.2,
            "gross_capture_bps": 12.0, "net_capture_bps": 10.0,
            "duration_minutes": 30.0,
        },
        {
            "strategy": "dynamic_grid_v1", "mode": "LIVE",
            "event_type": "GRID_CYCLE_CLOSED", "net_capture_usdt": -0.5,
            "gross_capture_usdt": -0.3, "fees_usdt": 0.2,
            "gross_capture_bps": -3.0, "net_capture_bps": -5.0,
            "duration_minutes": 60.0,
        },
    ]
    monkeypatch.setattr(src, "load_json", lambda *args, **kwargs: _Loaded({}))
    rolling = sp.build_dynamic_grid()["rolling"]
    assert rolling["cycles"] == 2
    assert rolling["win_rate_pct"] == 50.0
    assert rolling["net_expectancy_bps"] == 2.5
    assert rolling["profit_factor"] == 2.0
    assert rolling["max_drawdown_usdt"] == 0.5
    assert rolling["average_inventory_duration_minutes"] == 45.0


# --- agreement with the performance page ------------------------------------


def test_live_totals_match_the_performance_page(feed):
    """Two screens, one truth. Both use is_displayable_close; pin them equal."""
    from dashboard_v3.panels import history

    feed["logs/trade_dataset_v2.csv"] = [
        _close("low_vol_reclaim", "L1", 1.0, 0.10, 0.0, 0.90),
        _close("momentum_breakout", "L2", -0.5, 0.10, 0.0, -0.60),
        dict(_close("low_vol_reclaim", "L3", 9.9, 0.1, 0.0, 9.8), event_type="CLOSE_PROVISIONAL"),
    ]
    mine = sp.build_economics()["totals"]
    theirs = history.build()["live_stats"]
    assert mine["n"] == theirs["trades"]
    # history calls it "total"; the same money must come out of both panels.
    assert mine["net"] == pytest.approx(theirs["total"])
