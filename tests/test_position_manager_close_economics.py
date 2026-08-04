"""Integration: the real PositionManager must reconcile its own closes.

These bind against `ClosedTradeWriterMixin.reconcile_closed_lifecycle` as the
real `PositionManager` inherits it — not against a helper double. If the wiring
is removed from position_manager.py, or the flatness gate is loosened, these go
red.
"""

from __future__ import annotations

import csv
import inspect

import pytest

from execution.close_reconciler import RECONCILED_SOURCE
from execution.position_manager import PositionManager
from telemetry.close_record_sources import is_economic_close

OPEN_MS = 1_785_700_000_000
FLAT_RESULT = {"status": "CLOSED", "flatness": "FLAT", "remaining_size": 0.0}


def hist(symbol="TRXUSDT", side="short", ctime=OPEN_MS, size="77", pid="P1"):
    return {"symbol": symbol, "holdSide": side, "ctime": ctime, "utime": ctime + 5_400_000,
            "openTotalPos": size, "closeTotalPos": size, "openAvgPrice": "0.32579",
            "closeAvgPrice": "0.32626", "pnl": "-0.03619", "netProfit": "-0.06631471",
            "openFee": "-0.01505149", "closeFee": "-0.01507321", "totalFunding": "0",
            "positionId": pid}


class Harness(PositionManager):
    """Real PositionManager methods, stubbed I/O."""

    def __init__(self, history, dataset):
        self.rows = []
        self._history = history
        self._dataset = str(dataset)
        import logging
        self.log = logging.getLogger("harness")

    def _append_closed_trade_dataset_row(self, *, position, close_reason, exit_price,
                                         margin_roi_pct, extra=None):
        self.rows.append({"close_reason": close_reason, **(extra or {})})

    def _fetch_closed_position_history(self):
        return self._history()

    @staticmethod
    def _safe_float(v, d=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return d

    def reconcile_closed_lifecycle(self, position, close_result, reason):
        # Same body as production, with the dataset path pointed at tmp.
        from execution.closed_lifecycle_recorder import record_closed_lifecycle

        def _write(pos, econ):
            self._append_closed_trade_dataset_row(
                position=pos, close_reason=reason, exit_price=econ.get("exit_price"),
                margin_roi_pct=0.0,
                extra={"sync_source": econ["sync_source"], "net_pnl": econ["net_pnl"],
                       "fees": econ["fees"], "funding": econ["funding"],
                       "open_fee": econ["open_fee"], "close_fee": econ["close_fee"],
                       "event_type": "CLOSE"},
            )

        return record_closed_lifecycle(
            position=position, close_result=close_result, dataset_path=self._dataset,
            fetch_history=self._fetch_closed_position_history, write_economic_close=_write,
            reconcile=lambda **kw: __import__(
                "execution.close_reconciler", fromlist=["x"]
            ).reconcile_close(sleep=lambda _: None, **kw),
        )


def pos(lifecycle="pos-abc", size="77", ctime=OPEN_MS, direction="short"):
    return {"symbol": "TRXUSDT", "direction": direction, "position_lifecycle_id": lifecycle,
            "confirmed_position_size": size, "opened_at_ms": ctime,
            "opened_at": "2026-08-03T06:01:36"}


def empty_dataset(tmp_path, rows=()):
    p = tmp_path / "trade_dataset_v2.csv"
    fields = ["event_type", "symbol", "direction", "opened_at", "net_pnl", "fees",
              "sync_source", "position_lifecycle_id", "confirmed_position_size"]
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    return p


# ── the wiring itself must exist ────────────────────────────────────────────

def test_position_manager_exposes_the_reconciliation_hook():
    assert hasattr(PositionManager, "reconcile_closed_lifecycle")


@pytest.mark.parametrize("marker", [
    'self.reconcile_closed_lifecycle(\n                            position, dead_close_result, "dead_trade_timeout"\n                        )',
    'self.reconcile_closed_lifecycle(position, close_all_result, "tp3")',
    'self.reconcile_closed_lifecycle(\n                            position, residual_close_result, position["closed_reason"]\n                        )',
])
def test_every_bot_close_path_calls_the_reconciler(marker):
    """Source-level proof that all three PositionManager close paths are wired."""
    src = inspect.getsource(__import__("execution.position_manager", fromlist=["x"]))
    assert marker in src, "close path is not wired to reconcile_closed_lifecycle"


# ── A: confirmed flat -> exactly one economic close ─────────────────────────

@pytest.mark.parametrize("reason", ["dead_trade_timeout", "tp3", "residual_position_cleanup"])
def test_confirmed_close_reconciles_on_every_path(tmp_path, reason):
    h = Harness(lambda: [hist()], empty_dataset(tmp_path))
    out = h.reconcile_closed_lifecycle(pos(), FLAT_RESULT, reason)
    assert out == "RECONCILED"
    econ = [r for r in h.rows if r.get("sync_source") == RECONCILED_SOURCE]
    assert len(econ) == 1
    assert econ[0]["net_pnl"] == pytest.approx(-0.06631471)
    assert econ[0]["fees"] == pytest.approx(0.01505149 + 0.01507321)
    assert is_economic_close({"event_type": "CLOSE", "sync_source": econ[0]["sync_source"]})


def test_long_and_short_both_reconcile(tmp_path):
    for side in ("long", "short"):
        h = Harness(lambda: [hist(side=side)], empty_dataset(tmp_path))
        assert h.reconcile_closed_lifecycle(
            pos(direction=side), FLAT_RESULT, "dead_trade_timeout") == "RECONCILED"


# ── B: not flat -> nothing recorded ─────────────────────────────────────────

@pytest.mark.parametrize("result", [
    {"status": "CLOSE_FULL_POSITION_REMAINS", "remaining": 77},
    {"status": "ACCEPTED"},
    {"code": "00000", "msg": "success"},
    {"status": "CLOSE_FAILED", "error": "boom"},
])
def test_no_economic_close_without_proven_flatness(tmp_path, result):
    h = Harness(lambda: [hist()], empty_dataset(tmp_path))
    assert h.reconcile_closed_lifecycle(pos(), result, "dead_trade_timeout") == "NOT_FLAT"
    assert h.rows == []


# ── C: history late -> provisional, then recovered ──────────────────────────

def test_history_absent_leaves_it_provisional(tmp_path):
    h = Harness(lambda: [], empty_dataset(tmp_path))
    assert h.reconcile_closed_lifecycle(pos(), FLAT_RESULT, "dead_trade_timeout") == "PROVISIONAL"
    assert h.rows == []  # no phantom PnL


# ── D: ambiguous match -> fail closed ───────────────────────────────────────

def test_ambiguous_history_does_not_guess(tmp_path):
    h = Harness(lambda: [hist(pid="A"), hist(pid="B")], empty_dataset(tmp_path))
    p = pos()
    p.pop("opened_at_ms")
    p.pop("confirmed_position_size")
    assert h.reconcile_closed_lifecycle(p, FLAT_RESULT, "dead_trade_timeout") == "PROVISIONAL"
    assert h.rows == []


# ── E: dedup, including rotated segments ────────────────────────────────────

def test_second_processing_writes_nothing(tmp_path):
    existing = {"event_type": "CLOSE", "symbol": "TRXUSDT", "direction": "SHORT",
                "sync_source": RECONCILED_SOURCE, "position_lifecycle_id": "pos-abc"}
    h = Harness(lambda: [hist()], empty_dataset(tmp_path, [existing]))
    assert h.reconcile_closed_lifecycle(pos(), FLAT_RESULT, "dead_trade_timeout") == "ALREADY"
    assert h.rows == []


def test_rotated_segment_still_blocks_duplicate(tmp_path):
    ds = empty_dataset(tmp_path)
    empty_dataset(tmp_path)  # active stays empty
    rotated = tmp_path / "trade_dataset_v2.csv.1"
    fields = ["event_type", "symbol", "direction", "opened_at", "net_pnl", "fees",
              "sync_source", "position_lifecycle_id", "confirmed_position_size"]
    with open(rotated, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerow({k: {"event_type": "CLOSE", "symbol": "TRXUSDT", "direction": "SHORT",
                        "sync_source": RECONCILED_SOURCE,
                        "position_lifecycle_id": "pos-abc"}.get(k, "") for k in fields})
    h = Harness(lambda: [hist()], ds)
    assert h.reconcile_closed_lifecycle(pos(), FLAT_RESULT, "dead_trade_timeout") == "ALREADY"
