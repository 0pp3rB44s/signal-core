"""Wiring tests: a confirmed close yields exactly one economic CLOSE, or none."""

from __future__ import annotations

import csv

import pytest

from execution.close_reconciler import CloseReconciliationUnavailable, RECONCILED_SOURCE
from execution.closed_lifecycle_recorder import (
    exchange_confirmed_flat,
    recover_provisional_closes,
    record_closed_lifecycle,
)
from telemetry.close_record_sources import is_economic_close

OPEN_MS = 1_785_700_000_000
FIELDS = ["event_type", "symbol", "direction", "opened_at", "net_pnl", "fees",
          "sync_source", "position_lifecycle_id", "confirmed_position_size"]


def hist(symbol="TRXUSDT", side="short", ctime=OPEN_MS, size="77", pid="P1"):
    return {"symbol": symbol, "holdSide": side, "ctime": ctime, "utime": ctime + 5_400_000,
            "openTotalPos": size, "closeTotalPos": size, "openAvgPrice": "0.32579",
            "closeAvgPrice": "0.32626", "pnl": "-0.03619", "netProfit": "-0.06631471",
            "openFee": "-0.01505149", "closeFee": "-0.01507321", "totalFunding": "0",
            "positionId": pid}


def pos(symbol="TRXUSDT", direction="short", lifecycle="pos-abc", size="77", ctime=OPEN_MS):
    return {"symbol": symbol, "direction": direction, "position_lifecycle_id": lifecycle,
            "confirmed_position_size": size, "opened_at_ms": ctime,
            "opened_at": "2026-08-03T06:01:36"}


def dataset(tmp_path, rows=()):
    p = tmp_path / "trade_dataset_v2.csv"
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    return p


class Sink:
    def __init__(self):
        self.written, self.retired = [], []

    def write(self, position, econ):
        self.written.append((position, econ))

    def retire(self, position):
        self.retired.append(position)


def _run(tmp_path, close_result, fetch, position=None, rows=()):
    s = Sink()
    out = record_closed_lifecycle(
        position=position or pos(), close_result=close_result,
        dataset_path=str(dataset(tmp_path, rows)), fetch_history=fetch,
        write_economic_close=s.write, retire_provisional=s.retire,
        reconcile=lambda **kw: __import__("execution.close_reconciler", fromlist=["x"])
        .reconcile_close(sleep=lambda _: None, **kw),
    )
    return out, s


# ── exchange flatness gates everything ──────────────────────────────────────

def test_confirmed_flat_only_on_closed_status():
    assert exchange_confirmed_flat({"status": "CLOSED"}) is True
    assert exchange_confirmed_flat({"status": "CLOSE_FULL_POSITION_REMAINS"}) is False
    assert exchange_confirmed_flat({"status": "ACCEPTED"}) is False
    assert exchange_confirmed_flat({"code": "00000"}) is False
    assert exchange_confirmed_flat(None) is False
    assert exchange_confirmed_flat("CLOSED") is False


def test_no_economic_close_when_remaining_size_not_zero(tmp_path):
    out, s = _run(tmp_path, {"status": "CLOSE_FULL_POSITION_REMAINS", "remaining": 77},
                  lambda: [hist()])
    assert out == "NOT_FLAT"
    assert s.written == []


def test_api_acknowledgement_alone_is_not_enough(tmp_path):
    out, s = _run(tmp_path, {"code": "00000", "msg": "success"}, lambda: [hist()])
    assert out == "NOT_FLAT" and s.written == []


# ── happy path ──────────────────────────────────────────────────────────────

def test_confirmed_close_yields_one_economic_close(tmp_path):
    out, s = _run(tmp_path, {"status": "CLOSED"}, lambda: [hist()])
    assert out == "RECONCILED"
    assert len(s.written) == 1
    econ = s.written[0][1]
    assert econ["net_pnl"] == pytest.approx(-0.06631471)
    assert econ["sync_source"] == RECONCILED_SOURCE
    assert len(s.retired) == 1


def test_long_and_short_both_wire_through(tmp_path):
    for side in ("long", "short"):
        out, s = _run(tmp_path, {"status": "CLOSED"},
                      lambda: [hist(side=side)], position=pos(direction=side))
        assert out == "RECONCILED", side


def test_delayed_history_still_reconciles(tmp_path):
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return [] if calls["n"] < 3 else [hist()]

    out, s = _run(tmp_path, {"status": "CLOSED"}, fetch)
    assert out == "RECONCILED" and calls["n"] == 3


def test_history_never_available_stays_provisional(tmp_path):
    out, s = _run(tmp_path, {"status": "CLOSED"}, lambda: [])
    assert out == "PROVISIONAL"
    assert s.written == []          # no phantom PnL
    assert s.retired == []          # provisional row survives for later recovery


# ── dedup ───────────────────────────────────────────────────────────────────

def test_existing_economic_close_blocks_second_write(tmp_path):
    existing = {"event_type": "CLOSE", "symbol": "TRXUSDT", "direction": "SHORT",
                "sync_source": RECONCILED_SOURCE, "position_lifecycle_id": "pos-abc",
                "net_pnl": "-0.06631471"}
    out, s = _run(tmp_path, {"status": "CLOSED"}, lambda: [hist()], rows=[existing])
    assert out == "ALREADY" and s.written == []


def test_provisional_row_does_not_block(tmp_path):
    prov = {"event_type": "CLOSE_PROVISIONAL", "symbol": "TRXUSDT", "direction": "SHORT",
            "sync_source": "position_manager", "position_lifecycle_id": "pos-abc"}
    out, s = _run(tmp_path, {"status": "CLOSED"}, lambda: [hist()], rows=[prov])
    assert out == "RECONCILED" and len(s.written) == 1


def test_two_lifecycles_same_second_are_not_merged(tmp_path):
    """Different lifecycle ids: the second must still be recorded."""
    first = {"event_type": "CLOSE", "symbol": "TRXUSDT", "direction": "SHORT",
             "sync_source": RECONCILED_SOURCE, "position_lifecycle_id": "pos-abc",
             "opened_at": "2026-08-03T06:01:36", "confirmed_position_size": "77"}
    out, s = _run(tmp_path, {"status": "CLOSED"},
                  lambda: [hist(ctime=OPEN_MS + 600_000, size="80", pid="P2")],
                  position=pos(lifecycle="pos-def", size="80", ctime=OPEN_MS + 600_000),
                  rows=[first])
    assert out == "RECONCILED" and len(s.written) == 1


# ── recovery ────────────────────────────────────────────────────────────────

def test_recovery_fills_in_a_late_lifecycle(tmp_path):
    s = Sink()
    row = pos()
    stats = recover_provisional_closes(
        provisional_rows=[row], dataset_path=str(dataset(tmp_path)),
        fetch_history=lambda: [hist()], write_economic_close=s.write,
        retire_provisional=s.retire,
        reconcile=lambda **kw: __import__("execution.close_reconciler", fromlist=["x"])
        .reconcile_close(sleep=lambda _: None, **kw),
    )
    assert stats["recovered"] == 1 and len(s.written) == 1


def test_second_recovery_run_is_idempotent(tmp_path):
    """Restart between close and reconciliation, then two sweeps."""
    existing = {"event_type": "CLOSE", "symbol": "TRXUSDT", "direction": "SHORT",
                "sync_source": RECONCILED_SOURCE, "position_lifecycle_id": "pos-abc",
                "net_pnl": "-0.06631471"}
    s = Sink()
    kw = dict(provisional_rows=[pos()], dataset_path=str(dataset(tmp_path, [existing])),
              fetch_history=lambda: [hist()], write_economic_close=s.write,
              retire_provisional=s.retire)
    a = recover_provisional_closes(**kw)
    b = recover_provisional_closes(**kw)
    assert a["skipped"] == 1 and b["skipped"] == 1
    assert s.written == []


def test_recovery_is_bounded(tmp_path):
    s = Sink()
    stats = recover_provisional_closes(
        provisional_rows=[pos(lifecycle=f"pos-{i}") for i in range(50)],
        dataset_path=str(dataset(tmp_path)), fetch_history=lambda: [],
        write_economic_close=s.write, limit=5,
        reconcile=lambda **kw: (_ for _ in ()).throw(CloseReconciliationUnavailable("x")),
    )
    assert stats["seen"] == 5 and stats["still_pending"] == 5


# ── the reconciled row actually counts ──────────────────────────────────────

def test_reconciled_row_is_economic_and_provisional_is_not():
    assert is_economic_close({"event_type": "CLOSE", "sync_source": RECONCILED_SOURCE}) is True
    assert is_economic_close({"event_type": "CLOSE_PROVISIONAL",
                              "sync_source": RECONCILED_SOURCE}) is False
    assert is_economic_close({"event_type": "CLOSE",
                              "sync_source": "dead_trade_timeout"}) is False
    assert is_economic_close({"event_type": "CLOSE_QUARANTINED",
                              "sync_source": RECONCILED_SOURCE}) is False
