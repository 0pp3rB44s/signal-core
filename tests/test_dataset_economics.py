from __future__ import annotations

import csv

import pytest

from telemetry.trade_logger import TradeDatasetV2Logger, append_closed_trade_row


def _close(*, lifecycle_id: str, pnl: float = 1.25) -> dict:
    return {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "strategy": "low_vol_reclaim",
        "closed_at": "2026-07-31T12:00:00+00:00",
        "planned_avg_entry": 99.5,
        "exchange_avg_entry": 100.0,
        "exchange_avg_entry_source": "BITGET_OPEN_POSITION",
        "position_lifecycle_id": lifecycle_id,
        "exchange_entry_order_id": f"order-{lifecycle_id}",
        "exchange_entry_client_oid": f"client-{lifecycle_id}",
        "exchange_truth_exit_price": 101.0,
        "exit": 101.0,
        "exchange_truth_size": 2.0,
        "confirmed_position_size": 2.0,
        "confirmed_opening_fee_usdt": 0.04,
        "exchange_truth_pnl": pnl,
        "exchange_truth_fee": 0.05,
        "margin_roi_pct": 1.875,
        "price_return_pct": 1.0,
        "protection_state": "PROFIT_LOCK_CONFIRMED",
        "confirmed_stop": 100.2,
    }


def _rows(path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_close_dataset_keeps_money_and_percentages_in_separate_fields(tmp_path):
    path = tmp_path / "trade_dataset_v2.csv"
    logger = TradeDatasetV2Logger(path)

    logger.append_close(_close(lifecycle_id="life-a"), "tp_close", 999.0, {})

    row = _rows(path)[0]
    assert float(row["entry"]) == 100.0
    assert float(row["exchange_avg_entry"]) == 100.0
    assert float(row["planned_avg_entry"]) == 99.5
    assert float(row["net_pnl"]) == 1.25
    assert float(row["fees"]) == 0.05
    assert float(row["price_return_pct"]) == 1.0
    assert float(row["margin_roi_pct"]) == 1.875
    assert row["position_lifecycle_id"] == "life-a"
    assert row["confirmed_position_size"] == "2.0"


def test_close_dedupe_is_lifecycle_scoped_and_survives_restart(tmp_path):
    path = tmp_path / "trade_dataset_v2.csv"
    logger = TradeDatasetV2Logger(path)
    logger.append_close(_close(lifecycle_id="life-a"), "tp_close", 1.25, {})
    logger.append_close(_close(lifecycle_id="life-a"), "retry", 1.25, {})
    logger.append_close(_close(lifecycle_id="life-b"), "new_trade", 1.25, {})

    restarted = TradeDatasetV2Logger(path)
    restarted.append_close(_close(lifecycle_id="life-b"), "restart_retry", 1.25, {})

    assert [row["position_lifecycle_id"] for row in _rows(path)] == ["life-a", "life-b"]


def test_percentage_only_close_is_rejected_instead_of_becoming_usdt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="monetary PnL is required"):
        append_closed_trade_row(
            trade={
                "symbol": "SOLUSDT",
                "direction": "LONG",
                "exchange_avg_entry": 100.0,
            },
            margin_roi_pct=2.5,
            pnl_pct=0.5,
            exit_price=100.5,
        )

    assert not (tmp_path / "logs" / "trade_dataset_v2.csv").exists()
