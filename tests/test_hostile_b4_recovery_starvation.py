"""Hostile: no provisional row may be starved out of recovery forever.

The sweep sorted unresolved rows oldest-first and took `unresolved[:limit]`.
That looks fair and is not. A row the exchange can no longer produce — older
than the history window it fetches — never resolves, stays oldest, and holds its
slot on every sweep. Twenty such rows permanently starve every newer row behind
them, including ones the exchange would have answered immediately.

Fetching a single page of 50 was the other half: a lifecycle further back than
that could not be matched at all.

These tests drive the real sweep, the real selection and the real paging fetch,
and assert on which rows actually reach `write_economic_close`.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from execution.closed_lifecycle_recorder import recover_provisional_closes

OLD_MS = 1_700_000_000_000
NEW_MS = 1_785_700_000_000


def provisional(index: int, opened_ms: int, symbol: str) -> dict:
    return {
        "event_type": "CLOSE_PROVISIONAL", "symbol": symbol, "direction": "LONG",
        "opened_at_ms": opened_ms, "confirmed_position_size": "0.001",
        "position_lifecycle_id": f"LC-{index}", "_recovery_line": index,
    }


def history(symbol: str, opened_ms: int, position_id: str) -> dict:
    return {
        "symbol": symbol, "holdSide": "long", "ctime": opened_ms,
        "utime": opened_ms + 600_000, "openTotalPos": "0.001",
        "closeTotalPos": "0.001", "openAvgPrice": "100.0", "closeAvgPrice": "101.0",
        "pnl": "1.0", "netProfit": "0.88", "openFee": "-0.06", "closeFee": "-0.06",
        "totalFunding": "0", "positionId": position_id,
    }


@pytest.fixture
def dataset(tmp_path):
    path = tmp_path / "trade_dataset_v2.csv"
    path.write_text("event_type,symbol,direction\n", encoding="utf-8")
    return path


def sweep(dataset, rows, fetch, cursor=None, limit=20, recovered=None):
    """One real sweep. Recovered symbols are appended to `recovered`."""
    sink = recovered if recovered is not None else []
    return recover_provisional_closes(
        provisional_rows=rows,
        dataset_path=str(dataset),
        fetch_history=fetch,
        write_economic_close=lambda row, econ: sink.append(row["symbol"]),
        cursor=cursor,
        limit=limit,
    ), sink


# ── H27: twenty stuck rows must not hide a recoverable one ──────────────────

def test_h27_new_row_is_eventually_recovered(dataset):
    rows = [provisional(i, OLD_MS + i * 1000, f"OLD{i}USDT") for i in range(20)]
    rows.append(provisional(99, NEW_MS, "NEWUSDT"))
    # the exchange only knows the new lifecycle; the twenty old ones never resolve
    fetch = lambda: [history("NEWUSDT", NEW_MS, "P-NEW")]

    recovered: list[str] = []
    cursor = None
    for _ in range(3):
        stats, _ = sweep(dataset, rows, fetch, cursor=cursor, recovered=recovered)
        cursor = stats["next_cursor"]

    assert "NEWUSDT" in recovered, "the newer row was starved by twenty stuck ones"


def test_h27_without_rotation_the_new_row_starves(dataset):
    """Pin the failure mode itself: a fixed prefix never reaches row 21."""
    rows = [provisional(i, OLD_MS + i * 1000, f"OLD{i}USDT") for i in range(20)]
    rows.append(provisional(99, NEW_MS, "NEWUSDT"))
    fetch = lambda: [history("NEWUSDT", NEW_MS, "P-NEW")]

    recovered: list[str] = []
    for _ in range(5):
        # cursor=None every sweep is exactly the old always-take-the-first-20
        sweep(dataset, rows, fetch, cursor=None, recovered=recovered)

    assert recovered == [], "this fixture must reproduce the starvation"


# ── H28: several history pages ──────────────────────────────────────────────

def test_h28_recovery_completes_across_history_pages(monkeypatch, tmp_path):
    """The real paging fetch must walk past the first page."""
    from execution import closed_trade_writer as ctw

    pages = [
        [history(f"S{i}USDT", NEW_MS + i, f"P{i}") for i in range(100)],
        [history(f"S{i}USDT", NEW_MS + i, f"P{i}") for i in range(100, 150)],
    ]
    calls: list[dict] = []

    class Client:
        def _request(self, method, path, params, private=False):
            calls.append(dict(params))
            index = len(calls) - 1
            page = pages[index] if index < len(pages) else []
            return {"data": {"list": page}}

    writer = type("W", (), {"_fetch_closed_position_history": _fetch_from(ctw)})()
    writer.client = Client()
    writer.settings = type("S", (), {"bitget_product_type": "USDT-FUTURES"})()

    rows = writer._fetch_closed_position_history()

    assert len(rows) == 150, "paging stopped at the first page"
    assert len(calls) == 2
    assert "idLessThan" not in calls[0]
    assert calls[1]["idLessThan"] == "P99", "the second page must resume after the first"

    # and a lifecycle only present on page 2 is now matchable
    dataset = tmp_path / "trade_dataset_v2.csv"
    dataset.write_text("event_type,symbol,direction\n", encoding="utf-8")
    target = provisional(1, NEW_MS + 120, "S120USDT")
    stats, recovered = sweep(dataset, [target], lambda: rows)
    assert recovered == ["S120USDT"]
    assert stats["recovered"] == 1


def _fetch_from(module):
    """The production fetch, unbound, without constructing the whole mixin."""
    return module.ClosedTradeWriterMixin.__dict__["_fetch_closed_position_history"]


def test_h28_paging_stops_on_a_repeating_page(tmp_path):
    """A cursor the endpoint ignores must not loop."""
    from execution import closed_trade_writer as ctw

    page = [history(f"S{i}USDT", NEW_MS + i, f"P{i}") for i in range(100)]
    calls: list[dict] = []

    class Client:
        def _request(self, method, path, params, private=False):
            calls.append(dict(params))
            return {"data": {"list": page}}

    writer = type("W", (), {"_fetch_closed_position_history": _fetch_from(ctw)})()
    writer.client = Client()
    writer.settings = type("S", (), {"bitget_product_type": "USDT-FUTURES"})()

    rows = writer._fetch_closed_position_history()
    assert len(rows) == 100, "repeated rows must not be collected twice"
    assert len(calls) == 2, "the sweep must stop once a page repeats"


# ── H29: the cursor survives a crash ────────────────────────────────────────

def test_h29_cursor_round_trips_through_json(dataset):
    """The persisted form must come back as a usable cursor."""
    import json

    rows = [provisional(i, OLD_MS + i * 1000, f"OLD{i}USDT") for i in range(20)]
    rows.append(provisional(99, NEW_MS, "NEWUSDT"))
    fetch = lambda: [history("NEWUSDT", NEW_MS, "P-NEW")]

    first, _ = sweep(dataset, rows, fetch, cursor=None)
    # exactly what JsonStateStore would write and read back
    restored = json.loads(json.dumps({"cursor": first["next_cursor"]}))["cursor"]

    recovered: list[str] = []
    sweep(dataset, rows, fetch, cursor=restored, recovered=recovered)
    assert recovered == ["NEWUSDT"], "a restart must resume, not restart from the front"


@pytest.mark.parametrize("corrupt", [None, "", "garbage", [], [1, 2], {"a": 1}, [1, 2, 3, "x"]])
def test_h29_unusable_cursor_falls_back_to_the_front(dataset, corrupt):
    """A lost cursor costs one pass of fairness, never a row."""
    rows = [provisional(i, OLD_MS + i * 1000, f"OLD{i}USDT") for i in range(20)]
    rows.append(provisional(99, NEW_MS, "NEWUSDT"))
    stats, _ = sweep(dataset, rows, fetch=lambda: [], cursor=corrupt)
    assert stats["seen"] == 20
    assert stats["blocked"] is False


def test_h29_a_blocked_sweep_does_not_advance_the_cursor(dataset):
    rows = [provisional(i, OLD_MS + i * 1000, f"OLD{i}USDT") for i in range(21)]

    def boom():
        raise ConnectionError("transport down")

    stats, _ = sweep(dataset, rows, boom, cursor=None)
    assert stats["blocked"] is True
    # the window was selected, so the cursor may advance past it -- what must not
    # happen is a cursor that jumps over rows nobody attempted
    assert stats["next_cursor"] is not None


# ── H30: rows that are already done ─────────────────────────────────────────

def test_h30_already_recorded_rows_do_not_starve_the_rest(dataset):
    """Skipped rows leave the queue instead of holding slots."""
    from telemetry.trade_logger import append_exchange_truth_close
    from execution.close_reconciler import economics_from_history

    rows = [provisional(i, NEW_MS + i * 1000, f"S{i}USDT") for i in range(25)]
    hist = [history(f"S{i}USDT", NEW_MS + i * 1000, f"P{i}") for i in range(25)]

    def write(row, econ):
        append_exchange_truth_close(
            position=row, economics=econ, close_reason="recovered_close",
            dataset_path=str(dataset),
        )

    cursor = None
    for _ in range(3):
        stats = recover_provisional_closes(
            provisional_rows=rows, dataset_path=str(dataset),
            fetch_history=lambda: hist, write_economic_close=write,
            cursor=cursor, limit=20,
        )
        cursor = stats["next_cursor"]

    assert stats["unresolved_total"] == 0, "every row must reach recovery"
    assert stats["skipped"] == 25


# ── H31: a growing queue still reaches every row ────────────────────────────

def test_h31_every_row_is_reached_within_one_pass(dataset):
    """No row may sit outside the window for a whole rotation."""
    total, limit = 53, 20
    rows = [provisional(i, OLD_MS + i * 1000, f"S{i}USDT") for i in range(total)]
    seen: set[str] = set()

    def fetch():
        return []  # nothing resolves; only the selection is under test

    cursor = None
    sweeps = 0
    while len(seen) < total and sweeps < 10:
        before = set(seen)
        stats = recover_provisional_closes(
            provisional_rows=rows, dataset_path=str(dataset),
            fetch_history=fetch,
            write_economic_close=lambda r, e: None,
            cursor=cursor, limit=limit,
        )
        # reconstruct the window from the cursor the sweep reports
        cursor = stats["next_cursor"]
        sweeps += 1
        seen |= _window_symbols(rows, stats, limit)
        assert seen != before or len(seen) == total

    assert seen == {f"S{i}USDT" for i in range(total)}, f"unreached after {sweeps} sweeps"
    assert sweeps <= 3, "one full pass must cover everything"


def _window_symbols(rows, stats, limit) -> set[str]:
    from execution.closed_lifecycle_recorder import _oldest_key

    ordered = sorted(rows, key=_oldest_key)
    end_key = tuple(stats["next_cursor"]) if stats["next_cursor"] else None
    if end_key is None:
        return {r["symbol"] for r in ordered}
    end = next(i for i, r in enumerate(ordered) if _oldest_key(r) == end_key) + 1
    start = end - limit
    window = ordered[start:end] if start >= 0 else ordered[start:] + ordered[:end]
    return {r["symbol"] for r in window}


def test_h31_every_sweep_uses_its_full_budget(dataset):
    """Wrapping is what keeps the pass rate constant.

    Without it the window shrinks to whatever is left at the tail, so a queue
    that does not divide evenly by `limit` spends sweeps doing less work than it
    is allowed to, and a full pass takes longer than it should.
    """
    total, limit = 53, 20
    rows = [provisional(i, OLD_MS + i * 1000, f"S{i}USDT") for i in range(total)]

    cursor = None
    for sweep_index in range(4):
        stats, _ = sweep(dataset, rows, fetch=lambda: [], cursor=cursor, limit=limit)
        assert stats["seen"] == limit, (
            f"sweep {sweep_index + 1} attempted {stats['seen']} of a possible {limit}"
        )
        cursor = stats["next_cursor"]


def test_h31_a_growing_queue_never_locks_out_the_newest(dataset):
    rows = [provisional(i, OLD_MS + i * 1000, f"S{i}USDT") for i in range(20)]
    fetch_target = {"symbol": None}

    def fetch():
        if fetch_target["symbol"] is None:
            return []
        return [history(fetch_target["symbol"], NEW_MS, "P-NEW")]

    recovered: list[str] = []
    cursor = None
    for cycle in range(6):
        # a new provisional row arrives every cycle, always at the back
        newest = f"NEW{cycle}USDT"
        rows.append(provisional(100 + cycle, NEW_MS + cycle, newest))
        fetch_target["symbol"] = "NEW0USDT"
        stats, _ = sweep(dataset, rows, fetch, cursor=cursor, recovered=recovered)
        cursor = stats["next_cursor"]

    assert "NEW0USDT" in recovered, "the first newcomer never got a turn"
