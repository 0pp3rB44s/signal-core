"""The entry flow's fail-safe close must reconcile, or record nothing at all."""

from __future__ import annotations

import ast
import csv
import logging

import pytest

from execution.close_reconciler import RECONCILED_SOURCE
from execution.closed_lifecycle_recorder import reconcile_fail_safe_close

OPEN_MS = 1_785_700_000_000
FIELDS = ["event_type", "symbol", "direction", "opened_at", "net_pnl", "fees",
          "sync_source", "position_lifecycle_id", "confirmed_position_size"]


def hist(symbol="BTCUSDT", side="short", ctime=OPEN_MS, size="0.0003", pid="P1",
         pnl="-0.03619", net="-0.06631471"):
    return {"symbol": symbol, "holdSide": side, "ctime": ctime, "utime": ctime + 600_000,
            "openTotalPos": size, "closeTotalPos": size, "openAvgPrice": "62900.0",
            "closeAvgPrice": "62950.0", "pnl": pnl, "netProfit": net,
            "openFee": "-0.01505149", "closeFee": "-0.01507321", "totalFunding": "0",
            "positionId": pid}


def identity(symbol="BTCUSDT", direction="SHORT", side="short", ctime=OPEN_MS,
             size=0.0003):
    return {"symbol": symbol, "direction": direction, "hold_side": side,
            "opened_at_ms": ctime, "confirmed_position_size": size,
            "exchange_avg_entry": 62900.0, "exchange_position_id": "P1",
            "entry_order_id": "O1"}


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
        self.written = []

    def write(self, ident, econ):
        self.written.append(econ)


_SENTINEL = object()


def run(tmp_path, close_result, history, ident=_SENTINEL, rows=()):
    s = Sink()
    out = reconcile_fail_safe_close(
        lifecycle_identity=identity() if ident is _SENTINEL else ident,
        close_result=close_result, dataset_path=str(dataset(tmp_path, rows)),
        fetch_history=history, write_economic_close=s.write,
        log_=logging.getLogger("t"),
        reconcile=lambda **kw: __import__("execution.close_reconciler", fromlist=["x"])
        .reconcile_close(sleep=lambda _: None, **kw),
    )
    return out, s


# ── the production call must exist ──────────────────────────────────────────

def _service_src():
    import execution.execution_service as m
    return open(m.__file__).read()


def test_fail_safe_close_calls_the_shared_recorder():
    src = _service_src()
    assert "self._record_fail_safe_close_economics(response, lifecycle_identity)" in src
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "_record_fail_safe_close_economics" in names


def test_both_call_sites_still_pass_identity():
    tree = ast.parse(_service_src())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "_fail_safe_close"]
    assert len(calls) == 2
    for c in calls:
        assert "lifecycle_identity" in {k.arg for k in c.keywords if k.arg}


# ── A / B: LONG and SHORT reconcile ─────────────────────────────────────────

@pytest.mark.parametrize("direction,side", [("LONG", "long"), ("SHORT", "short")])
def test_confirmed_close_writes_one_economic_row(tmp_path, direction, side):
    out, s = run(tmp_path, {"status": "CLOSED"}, lambda: [hist(side=side)],
                 ident=identity(direction=direction, side=side))
    assert out == "RECONCILED"
    assert len(s.written) == 1
    assert s.written[0]["net_pnl"] == pytest.approx(-0.06631471)
    assert s.written[0]["fees"] == pytest.approx(0.01505149 + 0.01507321)
    assert s.written[0]["sync_source"] == RECONCILED_SOURCE


# ── C / D / E: anything short of proven flat records nothing ────────────────

@pytest.mark.parametrize("result", [
    {"status": "ACCEPTED"},
    {"code": "00000", "msg": "success"},
    {"status": "CLOSE_FULL_POSITION_REMAINS", "remaining_size": 0.0003},
    {"status": "CLOSE_FAILED", "error": "boom"},
    None,
])
def test_not_flat_records_nothing(tmp_path, result):
    out, s = run(tmp_path, result, lambda: [hist()])
    assert out == "NOT_FLAT"
    assert s.written == []


# ── F: missing identity fails closed ────────────────────────────────────────

@pytest.mark.parametrize("ident", [
    None,
    {},
    {"symbol": "BTCUSDT", "hold_side": "short"},                  # no open time
    {"symbol": "BTCUSDT", "hold_side": "short", "opened_at_ms": None},
])
def test_missing_identity_records_no_economics(tmp_path, ident):
    out, s = run(tmp_path, {"status": "CLOSED"}, lambda: [hist()], ident=ident)
    assert out == "NO_IDENTITY"
    assert s.written == []


# ── G / H: history absent or ambiguous ──────────────────────────────────────

def test_history_absent_stays_provisional(tmp_path):
    out, s = run(tmp_path, {"status": "CLOSED"}, lambda: [])
    assert out == "PROVISIONAL"
    assert s.written == []


def test_ambiguous_history_is_not_guessed(tmp_path):
    """Two lifecycles, neither within the open-time tolerance: refuse."""
    far = OPEN_MS + 3_600_000
    out, s = run(tmp_path, {"status": "CLOSED"},
                 lambda: [hist(ctime=far, pid="A"), hist(ctime=far + 1000, pid="B")])
    assert out == "PROVISIONAL"
    assert s.written == []


def test_missing_money_field_records_nothing(tmp_path):
    out, s = run(tmp_path, {"status": "CLOSED"}, lambda: [hist(net="")])
    assert out == "PROVISIONAL"
    assert s.written == []


# ── I: dedup ────────────────────────────────────────────────────────────────

def test_second_processing_writes_no_duplicate(tmp_path):
    existing = {"event_type": "CLOSE", "symbol": "BTCUSDT", "direction": "SHORT",
                "sync_source": RECONCILED_SOURCE, "opened_at": "",
                "confirmed_position_size": "0.0003",
                "position_lifecycle_id": "", "net_pnl": "-0.06631471"}
    ident = identity()
    ident["position_lifecycle_id"] = "pos-fs"
    existing["position_lifecycle_id"] = "pos-fs"
    out, s = run(tmp_path, {"status": "CLOSED"}, lambda: [hist()],
                 ident=ident, rows=[existing])
    assert out == "ALREADY"
    assert s.written == []
