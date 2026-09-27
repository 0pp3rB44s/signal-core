"""Every finalized LSRL_V1 trade enters the CANONICAL central close pipeline. No parallel ledger.

The central safety systems read one thing: the v2 close dataset (`logs/trade_dataset_v2.csv`).
  * weekly freeze      -> `RiskManager._weekly_realized_pnl` reads its economic CLOSE rows directly;
  * daily loss + consecutive losses -> `telemetry.dataset_builder` derives `daily_learning_report.json`
    from the same rows, and `RiskManager._daily_defensive_status` reads that report;
  * symbol expectancy  -> `risk.symbol_expectancy` reads the same economic rows.

A close counts only when it is ECONOMIC: an exchange-confirmed `sync_source` and exchange-truth money. So
LSRL does exactly what every bot-initiated close already does (`execution/closed_lifecycle_recorder.py`):

    exchange flat (verified)  ->  provisional row  ->  reconcile against Bitget position history  ->  economic CLOSE

  * the provisional row exists so a close is never invisible, even if the process dies first; the shared
    recovery sweep (`recover_provisional_close_rows`) will also finish it;
  * economics are COPIED from the exchange (`/api/v2/mix/position/history-position`), never estimated;
  * if the exchange has not published the lifecycle yet the row stays provisional and this recorder retries
    (bounded by the caller); the executor blocks NEW entries until the close is economically recorded, so a
    loss can never be invisible to the kill-switches while LSRL keeps trading.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import partial
from typing import Any, Callable

from execution.close_reconciler import CloseReconciliationUnavailable, reconcile_close
from execution.closed_lifecycle_recorder import record_closed_lifecycle
from lsrl.core.config import STRATEGY_ID

DATASET_PATH = "logs/trade_dataset_v2.csv"
HISTORY_PAGE_LIMIT = 100
HISTORY_MAX_PAGES = 5

RECORDED = {"RECONCILED", "ALREADY"}


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds")


class CentralCloseRecorder:
    def __init__(self, *, client: Any, product_type: str = "USDT-FUTURES", dataset_path: str = DATASET_PATH,
                 log: logging.Logger | None = None) -> None:
        self.client, self.product_type, self.dataset_path = client, product_type, str(dataset_path)
        self.log = log or logging.getLogger("lsrl_central_ledger")

    # ------------------------------------------------------------------ the canonical `position` record
    @staticmethod
    def position_record(life: dict[str, Any]) -> dict[str, Any]:
        """A lifecycle summary (built by the executor from EXCHANGE order fills) -> the position dict the
        canonical recorder consumes. Only facts; every money field is left to the exchange."""
        qty = float(life["entered_qty"])
        avg_entry = life["entry_notional"] / qty if qty > 0 else 0.0
        exit_qty = float(life["exit_qty"])
        avg_exit = life["exit_notional"] / exit_qty if exit_qty > 0 else ""
        return {
            "symbol": life["symbol"], "direction": life["direction"], "strategy": STRATEGY_ID,
            "status": "CLOSED", "position_lifecycle_id": f"lsrl-{life['setup_id']}",
            "opened_at_ms": int(life["opened_at_ms"]), "opened_at": _iso(int(life["opened_at_ms"])),
            "closed_at_ms": int(life["closed_at_ms"]), "closed_at": _iso(int(life["closed_at_ms"])),
            "confirmed_position_size": qty, "position_size": qty, "size": qty,
            "entry": avg_entry, "exchange_avg_entry": avg_entry, "exit": avg_exit,
            "stop_loss": life.get("stop_price") or "", "take_profits": [t for t in life.get("take_profits", [])],
            "leverage": life.get("leverage") or "", "notional": qty * avg_entry,
            "plan_id": life.get("plan_id", ""), "candidate_id": life.get("candidate_id", ""),
            "exchange_entry_order_id": life.get("first_entry_order_id", ""),
            "exchange_entry_client_oid": life.get("first_entry_client_oid", ""),
            "close_reason": life.get("close_reason", ""), "closed_reason": life.get("close_reason", ""),
            "message": f"lsrl_v1 setup {life['setup_id']}",
        }

    # ------------------------------------------------------------------ writes
    def write_provisional(self, position: dict[str, Any], close_reason: str) -> None:
        """CLOSE_PROVISIONAL: lifecycle visible, every money field empty (exactly as PositionManager)."""
        from telemetry.trade_logger import TradeDatasetV2Logger

        payload = dict(position)
        payload.update({"closed_reason": close_reason, "close_reason": close_reason,
                        "sync_source": "position_manager"})
        TradeDatasetV2Logger(self.dataset_path).append_close(
            trade=payload, result=close_reason, pnl=None, quality={},
            margin_roi_pct=None)

    def fetch_history(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str | None = None
        for _ in range(HISTORY_MAX_PAGES):
            payload = self.client.get_position_history(
                product_type=self.product_type, symbol=None, limit=HISTORY_PAGE_LIMIT, id_less_than=cursor)
            page = ((payload or {}).get("data") or {}).get("list") or []
            fresh = [r for r in page if str(r.get("positionId") or "") not in seen]
            if not fresh:
                break
            for row in fresh:
                seen.add(str(row.get("positionId") or ""))
                rows.append(row)
            cursor = str(((payload or {}).get("data") or {}).get("endId") or "") or None
            if len(page) < HISTORY_PAGE_LIMIT or not cursor:
                break
        return rows

    def reconcile_once(self, position: dict[str, Any], close_reason: str,
                       fetch_history: Callable[[], list[dict]] | None = None) -> str:
        """ONE non-blocking attempt. RECONCILED / ALREADY = the loss is now visible to the central meters;
        PROVISIONAL = the exchange has not published it yet (retry later)."""
        from telemetry.trade_logger import append_exchange_truth_close

        def _write(pos: dict, econ: Any) -> None:
            # `telemetry.dataset_builder._daily_learning_report` (the source of the daily-loss and
            # consecutive-loss kill-switches) sums `exchange_truth_pnl`, and treats an EMPTY value as 0.0.
            # The reconciled-close writer leaves that column empty, so a reconciled loss would be invisible
            # to the daily meter. Carry the exchange's own net figures in the columns that meter reads.
            row = dict(pos)
            row.update({"exchange_truth_pnl": econ["net_pnl"], "exchange_truth_fee": econ["fees"],
                        "exchange_truth_exit_price": econ["close_price"], "exchange_truth_size": econ["size"]})
            append_exchange_truth_close(position=row, economics=econ, close_reason=close_reason,
                                        dataset_path=self.dataset_path)

        try:
            return record_closed_lifecycle(
                position=position,
                close_result={"status": "CLOSED", "flatness": "FLAT", "remaining_size": 0.0},
                dataset_path=self.dataset_path,
                fetch_history=fetch_history or self.fetch_history,
                write_economic_close=_write,
                reconcile=partial(reconcile_close, delays=(0.0,), sleep=lambda _s: None))
        except CloseReconciliationUnavailable:
            return "PROVISIONAL"
        except Exception as exc:                                   # noqa: BLE001 - bookkeeping never raises
            self.log.critical("LSRL_CENTRAL_RECORD_FAILED | %s | %s: %s", position.get("position_lifecycle_id"),
                              type(exc).__name__, exc)
            return "ERROR"
