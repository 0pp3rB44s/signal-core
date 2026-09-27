"""The dashboard's weekly risk figure must equal the kill-switch's, not resemble it."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

import pytest

from dashboard_v3.core.risk_truth import compute_weekly_risk

NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
FIELDS = ["event_type", "timestamp", "symbol", "direction", "closed_at",
          "net_pnl", "pnl", "position_lifecycle_id", "sync_source"]


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def _row(**kw):
    base = {"event_type": "CLOSE", "symbol": "BTCUSDT", "direction": "LONG",
            "closed_at": (NOW - timedelta(days=1)).isoformat(),
            "net_pnl": "-1.0", "position_lifecycle_id": "pos-1",
            "sync_source": "bitget_position_history"}
    base.update(kw)
    return base


def test_counts_only_exchange_confirmed_closes(tmp_path):
    """A row with no sync_source is displayable but NOT economic. The old panel
    counted it; the kill-switch never did."""
    _write(tmp_path / "logs" / "trade_dataset_v2.csv", [
        _row(position_lifecycle_id="pos-1", net_pnl="-1.0"),
        _row(position_lifecycle_id="pos-2", net_pnl="-99.0", sync_source=""),
    ])
    r = compute_weekly_risk(tmp_path, equity=100.0, freeze_threshold_pct=7.0, now=NOW)
    assert r.realized_pnl == pytest.approx(-1.0)
    assert r.counted_trades == 1
    assert r.skipped_non_economic == 1


def test_reads_the_rotated_segment_too(tmp_path):
    """Closes that rotated into .csv.1 still gate the account."""
    _write(tmp_path / "logs" / "trade_dataset_v2.csv.1", [_row(position_lifecycle_id="old", net_pnl="-4.0")])
    _write(tmp_path / "logs" / "trade_dataset_v2.csv", [_row(position_lifecycle_id="new", net_pnl="-1.0")])
    r = compute_weekly_risk(tmp_path, equity=100.0, freeze_threshold_pct=7.0, now=NOW)
    assert r.realized_pnl == pytest.approx(-5.0)
    assert r.counted_trades == 2
    assert set(r.files_read) == {"logs/trade_dataset_v2.csv.1", "logs/trade_dataset_v2.csv"}


def test_deduplicates_rows_without_a_lifecycle_id(tmp_path):
    """The same close present in both files, with no lifecycle id, is one trade."""
    dup = _row(position_lifecycle_id="", net_pnl="-3.0")
    _write(tmp_path / "logs" / "trade_dataset_v2.csv.1", [dup])
    _write(tmp_path / "logs" / "trade_dataset_v2.csv", [dup])
    r = compute_weekly_risk(tmp_path, equity=100.0, freeze_threshold_pct=7.0, now=NOW)
    assert r.counted_trades == 1
    assert r.realized_pnl == pytest.approx(-3.0)


def test_excludes_closes_outside_the_rolling_window(tmp_path):
    _write(tmp_path / "logs" / "trade_dataset_v2.csv", [
        _row(position_lifecycle_id="in", closed_at=(NOW - timedelta(days=6)).isoformat(), net_pnl="-2.0"),
        _row(position_lifecycle_id="out", closed_at=(NOW - timedelta(days=8)).isoformat(), net_pnl="-50.0"),
    ])
    r = compute_weekly_risk(tmp_path, equity=100.0, freeze_threshold_pct=7.0, now=NOW)
    assert r.realized_pnl == pytest.approx(-2.0)


def test_freeze_activates_at_the_threshold(tmp_path):
    _write(tmp_path / "logs" / "trade_dataset_v2.csv", [_row(net_pnl="-7.0")])
    r = compute_weekly_risk(tmp_path, equity=100.0, freeze_threshold_pct=7.0, now=NOW)
    assert r.loss_pct == pytest.approx(7.0)
    assert r.freeze_active is True
    assert r.headroom_pct == pytest.approx(0.0)


def test_freeze_clear_below_threshold(tmp_path):
    _write(tmp_path / "logs" / "trade_dataset_v2.csv", [_row(net_pnl="-1.0")])
    r = compute_weekly_risk(tmp_path, equity=100.0, freeze_threshold_pct=7.0, now=NOW)
    assert r.freeze_active is False
    assert r.headroom_pct == pytest.approx(6.0)


def test_profit_is_not_a_loss(tmp_path):
    _write(tmp_path / "logs" / "trade_dataset_v2.csv", [_row(net_pnl="5.0")])
    r = compute_weekly_risk(tmp_path, equity=100.0, freeze_threshold_pct=7.0, now=NOW)
    assert r.loss_pct == pytest.approx(0.0)
    assert r.freeze_active is False


def test_missing_dataset_is_unknown_not_zero(tmp_path):
    """No file must never render as 'no loss' — that reads as safe."""
    r = compute_weekly_risk(tmp_path, equity=100.0, freeze_threshold_pct=7.0, now=NOW)
    assert r.realized_pnl is None
    assert r.loss_pct is None
    assert r.freeze_active is None
    assert r.usable is False


def test_no_equity_yields_unknown_percentage(tmp_path):
    _write(tmp_path / "logs" / "trade_dataset_v2.csv", [_row(net_pnl="-5.0")])
    r = compute_weekly_risk(tmp_path, equity=None, freeze_threshold_pct=7.0, now=NOW)
    assert r.realized_pnl == pytest.approx(-5.0)
    assert r.loss_pct is None and r.freeze_active is None


def test_malformed_net_pnl_is_skipped_not_fatal(tmp_path):
    _write(tmp_path / "logs" / "trade_dataset_v2.csv", [
        _row(position_lifecycle_id="ok", net_pnl="-2.0"),
        _row(position_lifecycle_id="bad", net_pnl="not-a-number"),
    ])
    r = compute_weekly_risk(tmp_path, equity=100.0, freeze_threshold_pct=7.0, now=NOW)
    assert r.realized_pnl == pytest.approx(-2.0)
    assert r.counted_trades == 1
