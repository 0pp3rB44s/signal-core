from __future__ import annotations

import copy
import threading
import time

from execution.runtime_lock import trading_state_lock
from tests.test_entry_path_audit import _service
from tests.test_portfolio_selection import _plan
from tests.test_position_model_migration import _manager


def _exchange_position(symbol: str, *, entry: float = 100.0) -> dict:
    return {
        "symbol": symbol,
        "holdSide": "long",
        "total": "0.5",
        "openPriceAvg": str(entry),
        "markPrice": str(entry),
    }


def _prepare_success(service, *, symbol: str, order_id: str, fee: float) -> None:
    service.client.get_all_positions.side_effect = [
        {"data": []},
        {"data": []},
        {"data": [_exchange_position(symbol)]},
    ]
    service.client.place_futures_market_order.return_value = {
        "data": {"orderId": order_id}
    }
    service.client.extract_fill_metrics.return_value = {
        "order_id": order_id,
        "avg_price": 100.0,
        "filled_qty": 0.5,
        "fee": fee,
        "pnl": 0.0,
        "state": "filled",
    }


def test_three_symbols_remain_isolated_across_three_sequential_lifecycles(monkeypatch):
    service = _service(monkeypatch)
    sol = _plan("SOLUSDT", 95, candle_ms=1_700_000_000_000)
    btc = _plan("BTCUSDT", 90, candle_ms=1_700_000_000_000)
    eth = _plan("ETHUSDT", 85, candle_ms=1_700_000_000_000)
    sol.reasons = ["symbol expectancy source=live (SOLUSDT LONG, n=8, exp=0.30)"]
    btc.reasons = ["symbol expectancy source=live (BTCUSDT LONG, n=8, exp=0.20)"]
    eth.reasons = ["symbol expectancy source=live (ETHUSDT LONG, n=8, exp=0.10)"]

    _prepare_success(service, symbol="SOLUSDT", order_id="sol-order", fee=0.01)
    first_reports = service.execute([eth, btc, sol])

    assert [report.symbol for report in first_reports] == ["SOLUSDT"]
    first_rows = service.store.load(default=[])
    assert [row["symbol"] for row in first_rows] == ["SOLUSDT"]
    sol_state = copy.deepcopy(first_rows[0])
    assert sol_state["position_lifecycle_id"]
    assert sol_state["confirmed_opening_fee_usdt"] == 0.01
    assert service.intent_store.get(service.entry_submitter.client_oid_for(btc)) is None
    assert service.intent_store.get(service.entry_submitter.client_oid_for(eth)) is None
    assert service.cooldowns.get("SOLUSDT").active is True
    assert service.cooldowns.get("BTCUSDT").active is False

    # Persist a retry marker on A.  B must not inherit any of it after A closes.
    first_rows[0]["protection_update_pending"] = True
    first_rows[0]["pending_protection_requested_stop"] = 101.25
    service.store.save(first_rows)
    _prepare_success(service, symbol="BTCUSDT", order_id="btc-order", fee=0.02)
    second_reports = service.execute([eth, btc])

    assert [report.symbol for report in second_reports] == ["BTCUSDT"]
    second_rows = service.store.load(default=[])
    old_sol = next(row for row in second_rows if row["symbol"] == "SOLUSDT")
    btc_state = next(row for row in second_rows if row["symbol"] == "BTCUSDT")
    assert old_sol["status"] == "CLOSED_SYNCED"
    assert old_sol["position_lifecycle_id"] == sol_state["position_lifecycle_id"]
    assert btc_state["status"] == "OPEN"
    assert btc_state["position_lifecycle_id"] != sol_state["position_lifecycle_id"]
    assert btc_state["confirmed_opening_fee_usdt"] == 0.02
    assert "protection_update_pending" not in btc_state
    assert "pending_protection_requested_stop" not in btc_state
    assert btc_state["reasons"] == btc.reasons

    # A later, owner-eligible A trade gets a new identity and clean protection state.
    service.cooldowns.clear("SOLUSDT")
    sol_new = _plan("SOLUSDT", 96, candle_ms=1_700_000_900_000)
    sol_new.reasons = ["symbol expectancy source=live (SOLUSDT LONG, n=9, exp=0.40)"]
    _prepare_success(service, symbol="SOLUSDT", order_id="sol-order-new", fee=0.03)
    third_reports = service.execute([sol_new])

    assert [report.symbol for report in third_reports] == ["SOLUSDT"]
    final_rows = service.store.load(default=[])
    sol_rows = [row for row in final_rows if row["symbol"] == "SOLUSDT"]
    assert len(sol_rows) == 2
    new_sol = next(row for row in sol_rows if row["status"] == "OPEN")
    assert new_sol["position_lifecycle_id"] != sol_state["position_lifecycle_id"]
    assert new_sol["exchange_entry_order_id"] == "sol-order-new"
    assert new_sol["confirmed_opening_fee_usdt"] == 0.03
    assert new_sol["confirmed_position_size"] == 0.5
    assert new_sol["protection_state"] == "INITIAL_PROTECTION_CONFIRMED"
    assert "protection_update_pending" not in new_sol
    assert new_sol["reasons"] == sol_new.reasons


def test_two_overlapping_execution_cycles_create_only_one_position(monkeypatch, tmp_path):
    first = _service(monkeypatch)
    second = _service(monkeypatch)
    first_plan = _plan("BTCUSDT", 90)
    second_plan = _plan("SOLUSDT", 95)
    exchange_positions: list[dict] = []
    exchange_guard = threading.Lock()
    barrier = threading.Barrier(2)
    reports: list = []

    def get_positions():
        with exchange_guard:
            return {"data": copy.deepcopy(exchange_positions)}

    def place_order(*, symbol, **_kwargs):
        # Simulate exchange latency while the production trading-state lock is held.
        time.sleep(0.04)
        with exchange_guard:
            exchange_positions[:] = [_exchange_position(symbol)]
        return {"data": {"orderId": f"order-{symbol}"}}

    for service in (first, second):
        service.client.get_all_positions.side_effect = get_positions
        service.client.place_futures_market_order.side_effect = place_order
        service.client.extract_fill_metrics.return_value = {
            "order_id": "shared-order", "avg_price": 100.0,
            "filled_qty": 0.5, "fee": 0.01, "pnl": 0.0, "state": "filled",
        }

    lock_path = tmp_path / "trading-state.lock"

    def run(service, plan):
        barrier.wait(timeout=1)
        with trading_state_lock(str(lock_path)):
            reports.extend(service.execute([plan]))

    threads = [
        threading.Thread(target=run, args=(first, first_plan)),
        threading.Thread(target=run, args=(second, second_plan)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert (
        first.client.place_futures_market_order.call_count
        + second.client.place_futures_market_order.call_count
    ) == 1
    assert len(exchange_positions) == 1
    assert len([report for report in reports if report.status == "EXECUTED"]) == 1
    assert len([report for report in reports if report.status == "SKIPPED"]) == 1
    stored = first.store.load(default=[])
    assert len([row for row in stored if row["status"] == "OPEN"]) == 1


def test_late_legality_failure_never_falls_through_to_runner_up(monkeypatch):
    service = _service(monkeypatch)
    sol = _plan("SOLUSDT", 95)
    btc = _plan("BTCUSDT", 90)
    # The exchange confirms a mark below the re-anchored LONG stop.
    service.client.get_all_positions.side_effect = [
        {"data": []},
        {"data": []},
        {"data": [{
            **_exchange_position("SOLUSDT"),
            "markPrice": "94",
        }]},
        {"data": []},
    ]

    reports = service.execute([btc, sol])

    assert [report.symbol for report in reports] == ["SOLUSDT"]
    assert reports[0].status == "ERROR"
    assert service.client.place_futures_market_order.call_count == 1
    service.client.close_futures_position.assert_called_once()
    assert service.intent_store.get(service.entry_submitter.client_oid_for(btc)) is None


def test_recovery_builds_independent_symbol_side_and_lifecycle_state():
    manager = _manager()
    live_positions = [
        {
            "symbol": "BTCUSDT", "holdSide": "long", "total": "0.2",
            "openPriceAvg": "63000", "markPrice": "63100", "leverage": "3",
            "stopLoss": "62000", "takeProfit": "65000",
            "entryOrderId": "btc-entry", "entryClientOid": "btc-client",
        },
        {
            "symbol": "SOLUSDT", "holdSide": "short", "total": "4",
            "openPriceAvg": "75", "markPrice": "74", "leverage": "3",
            "stopLoss": "77", "takeProfit": "70",
            "entryOrderId": "sol-entry", "entryClientOid": "sol-client",
        },
    ]

    recovered = manager._recover_missing_local_positions(
        ["BTCUSDT", "SOLUSDT"],
        live_positions,
        {"BTCUSDT": 63100.0, "SOLUSDT": 74.0},
    )

    assert len(recovered) == 2
    by_symbol = {row["symbol"]: row for row in recovered}
    btc = by_symbol["BTCUSDT"]
    sol = by_symbol["SOLUSDT"]
    assert btc["direction"] == "LONG"
    assert btc["exchange_avg_entry"] == 63000.0
    assert btc["confirmed_position_size"] == 0.2
    assert btc["confirmed_stop"] == 62000.0
    assert btc["exchange_entry_order_id"] == "btc-entry"
    assert sol["direction"] == "SHORT"
    assert sol["exchange_avg_entry"] == 75.0
    assert sol["confirmed_position_size"] == 4.0
    assert sol["confirmed_stop"] == 77.0
    assert sol["exchange_entry_order_id"] == "sol-entry"
    assert btc["position_lifecycle_id"] != sol["position_lifecycle_id"]
    assert btc["planned_avg_entry"] is None
    assert sol["planned_avg_entry"] is None
