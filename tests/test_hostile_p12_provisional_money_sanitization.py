"""Hostile: a provisional close must stay provisional, whatever money it inherits.

`_append_provisional_close_dataset_row` copied the whole position object. By the
time a close path reaches it the position routinely carries exchange-truth money
— the disappearance route sets `exchange_truth_pnl` from the order-history
fallback *before* discovering there are no full economics. `append_close` then
saw money and wrote `event_type=CLOSE` instead of `CLOSE_PROVISIONAL`.

Such a row is the worst of both worlds. Its `sync_source` is `position_manager`,
which is not exchange truth, so `is_economic_close` is False and no risk meter
counts it. Its `event_type` is no longer `CLOSE_PROVISIONAL`, so
`load_provisional_rows` never offers it to recovery. The close is unreachable
forever. Twenty such rows exist on the live dataset.

These tests drive the real writer, the real recorder and the real recovery
loader, and assert on what actually lands in the CSV.
"""

from __future__ import annotations

import ast
import csv
import logging
from pathlib import Path

import pytest

from execution.closed_trade_writer import (
    PROVISIONAL_PROMOTING_MONEY_FIELDS,
    PROVISIONAL_STRIPPED_MONEY_FIELDS,
    ClosedTradeWriterMixin,
)
from execution.close_reconciler import economics_from_history, is_provisional
from execution.closed_lifecycle_recorder import (
    load_provisional_rows,
    recover_provisional_closes,
)
from telemetry.close_record_sources import is_economic_close

DATASET = "logs/trade_dataset_v2.csv"
OPEN_MS = 1_785_700_000_000

#: Every money key a position can plausibly reach the helper with.
ALL_EXCHANGE_TRUTH_MONEY = {
    "exchange_truth_pnl": "-0.05",
    "exchange_truth_fee": "0.03",
    "exchange_truth_net_profit": "-0.0663147",
    "exchange_truth_gross_pnl": "-0.03619",
    "exchange_truth_open_fee": "-0.01505149",
    "exchange_truth_close_fee": "-0.01507321",
    "exchange_truth_funding": "0",
}


class Writer:
    """The real helper, nothing else."""

    _append_provisional_close_dataset_row = ClosedTradeWriterMixin.__dict__[
        "_append_provisional_close_dataset_row"
    ]

    def __init__(self):
        self.log = logging.getLogger("writer")


def position(lifecycle="LC-1", **extra) -> dict:
    row = {
        "symbol": "BTCUSDT", "direction": "LONG", "strategy": "test",
        "position_lifecycle_id": lifecycle,
        "opened_at": "2026-08-01T10:00:00+00:00",
        "opened_at_ms": OPEN_MS,
        "confirmed_position_size": "0.001",
        "exchange_position_id": "P-1",
    }
    row.update(extra)
    return row


def write(pos, extra=None, close_reason="exchange_position_closed", roi=-0.5):
    Writer()._append_provisional_close_dataset_row(
        position=pos, close_reason=close_reason, exit_price=101.0,
        margin_roi_pct=roi, extra=extra or {},
    )


def rows() -> list[dict]:
    path = Path(DATASET)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        return list(csv.DictReader(fh))


def last() -> dict:
    return rows()[-1]


MONEY_COLUMNS = ("pnl", "net_pnl", "fees", "gross_pnl", "funding")


def assert_is_a_real_provisional(row: dict):
    assert row["event_type"] == "CLOSE_PROVISIONAL"
    assert is_provisional(row) is True
    assert is_economic_close(row) is False
    for column in MONEY_COLUMNS:
        assert row.get(column, "") == "", f"{column} presented money on a provisional row"


# ── H-P12-1: the proven production trigger ──────────────────────────────────

def test_hp12_1_exchange_truth_pnl_stays_provisional_and_recoverable():
    write(position(exchange_truth_pnl="-0.05"))
    row = last()
    assert_is_a_real_provisional(row)

    found = load_provisional_rows(DATASET)
    assert found.blocked is False
    assert [r["position_lifecycle_id"] for r in found.rows] == ["LC-1"], (
        "the recovery loader must be able to find this row again"
    )


# ── H-P12-2: nothing leaks, from anywhere ───────────────────────────────────

def test_hp12_2_no_exchange_truth_money_field_leaks():
    write(position("LC-2", **ALL_EXCHANGE_TRUTH_MONEY))
    row = last()
    assert_is_a_real_provisional(row)
    for name in ALL_EXCHANGE_TRUTH_MONEY:
        assert row.get(name, "") == "", f"{name} survived into the provisional row"


def test_hp12_2_money_supplied_through_extra_is_also_stripped():
    write(position("LC-3"), extra=dict(ALL_EXCHANGE_TRUTH_MONEY))
    assert_is_a_real_provisional(last())


@pytest.mark.parametrize("field", sorted(PROVISIONAL_PROMOTING_MONEY_FIELDS))
def test_hp12_2_each_promoting_field_alone_cannot_promote(field):
    payload = {field: "-0.05"}
    if field == "exchange_truth_net_profit":
        # this branch demands the full contract; supply it so the only reason
        # the row stays provisional is the stripping itself
        payload.update({
            "exchange_truth_gross_pnl": "-0.036", "exchange_truth_open_fee": "-0.015",
            "exchange_truth_close_fee": "-0.015", "exchange_truth_funding": "0",
        })
    write(position(f"LC-{field}", **payload))
    assert_is_a_real_provisional(last())


# ── H-P12-3: every production call site ─────────────────────────────────────

def callsites() -> list[int]:
    """Line numbers of every real call to the helper, read from the source."""
    path = Path(__file__).resolve().parents[1] / "execution" / "position_manager.py"
    tree = ast.parse(path.read_text())
    return sorted(
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", "") == "_append_provisional_close_dataset_row"
    )


def test_hp12_3_every_callsite_produces_a_recoverable_row():
    """Drive the helper once per real call site, each carrying money."""
    sites = callsites()
    assert sites, "no production call sites found"
    for line in sites:
        write(position(f"LC-site-{line}", **ALL_EXCHANGE_TRUTH_MONEY))
        assert_is_a_real_provisional(last())
    assert len(load_provisional_rows(DATASET).rows) == len(sites)


# ── H-P12-4: the real order-history fallback shape ──────────────────────────

def test_hp12_4_order_history_fallback_leaves_a_recoverable_row():
    """`bitget_order_history` returns pnl but no economics -- the proven path."""
    pos = position("LC-fallback", exchange_truth_pnl="-0.05", exchange_truth_fee="0.03")
    pos["close_source"] = "bitget_order_history"
    write(pos, extra={"close_source": "bitget_order_history"})

    row = last()
    assert_is_a_real_provisional(row)
    assert row["sync_source"] == "position_manager"
    dead = [r for r in rows() if r["event_type"] == "CLOSE" and not is_economic_close(r)]
    assert dead == [], "a dead non-economic CLOSE was written"


# ── H-P12-5 / 6: recovery replaces it with exactly one economic close ───────

def history_row() -> dict:
    return {
        "symbol": "BTCUSDT", "holdSide": "long", "ctime": OPEN_MS,
        "utime": OPEN_MS + 600_000, "openTotalPos": "0.001", "closeTotalPos": "0.001",
        "openAvgPrice": "100.0", "closeAvgPrice": "101.0", "pnl": "-0.03619",
        "netProfit": "-0.06631471", "openFee": "-0.01505149",
        "closeFee": "-0.01507321", "totalFunding": "0", "positionId": "P-1",
    }


def run_recovery() -> dict:
    from telemetry.trade_logger import append_exchange_truth_close

    return recover_provisional_closes(
        provisional_rows=load_provisional_rows(DATASET).rows,
        dataset_path=DATASET,
        fetch_history=lambda: [history_row()],
        write_economic_close=lambda row, econ: append_exchange_truth_close(
            position=row, economics=econ, close_reason="recovered_close",
            dataset_path=DATASET,
        ),
    )


def test_hp12_5_recovery_replaces_it_with_exchange_truth():
    write(position("LC-rec", exchange_truth_pnl="-0.05"))
    assert_is_a_real_provisional(last())

    stats = run_recovery()
    assert stats["recovered"] == 1

    economic = [r for r in rows() if is_economic_close(r)]
    assert len(economic) == 1
    got = economic[0]
    expected = economics_from_history(history_row())
    assert float(got["net_pnl"]) == pytest.approx(expected["net_pnl"])
    assert float(got["gross_pnl"]) == pytest.approx(expected["gross_pnl"])
    assert float(got["fees"]) == pytest.approx(expected["fees"])
    assert float(got["funding"]) == pytest.approx(expected["funding"])


def test_hp12_6_second_recovery_run_writes_no_duplicate():
    write(position("LC-dup", exchange_truth_pnl="-0.05"))
    run_recovery()
    first = len([r for r in rows() if is_economic_close(r)])
    stats = run_recovery()
    assert first == 1
    assert len([r for r in rows() if is_economic_close(r)]) == 1
    assert stats["recovered"] == 0


# ── H-P12-7 / 8: percentages and unknowns ───────────────────────────────────

def test_hp12_7_margin_roi_stays_in_its_own_column():
    write(position("LC-roi", exchange_truth_pnl="-0.05"), roi=-7.375)
    row = last()
    assert row["margin_roi_pct"] == "-7.375"
    for column in MONEY_COLUMNS:
        assert row.get(column, "") == "", f"a percentage reached the {column} column"


def test_hp12_8_unknown_money_stays_empty_not_zero():
    write(position("LC-empty", exchange_truth_pnl="-0.05"))
    row = last()
    for column in MONEY_COLUMNS:
        assert row.get(column, "") == "", f"{column} is {row.get(column)!r}"
        assert row.get(column) not in ("0", "0.0", "0.00"), f"{column} was zeroed"


# ── H-P12-9: the promotion itself is impossible from this helper ────────────

def test_hp12_9_helper_can_never_emit_a_plain_close():
    for index, (name, value) in enumerate(ALL_EXCHANGE_TRUTH_MONEY.items()):
        write(position(f"LC-solo-{index}", **{name: value}))
        assert last()["event_type"] == "CLOSE_PROVISIONAL", f"{name} promoted the row"
    assert all(r["event_type"] == "CLOSE_PROVISIONAL" for r in rows())


# ── H-P12-10: a new call site must not slip in untested ─────────────────────

def test_hp12_10_all_callsites_are_known_and_covered():
    """Adding a call site fails this test until it is added to the audit above."""
    sites = callsites()
    assert len(sites) == 9, (
        f"production call sites changed: {sites}. Extend "
        "test_hp12_3_every_callsite_produces_a_recoverable_row and update this count."
    )
    other = _helper_callers_outside_position_manager()
    assert other == [], f"the provisional helper is called from outside PositionManager: {other}"


def _helper_callers_outside_position_manager() -> list[str]:
    root = Path(__file__).resolve().parents[1]
    hits: list[str] = []
    for path in sorted((root / "execution").glob("*.py")):
        if path.name in {"position_manager.py", "closed_trade_writer.py"}:
            continue
        if "_append_provisional_close_dataset_row" in path.read_text():
            hits.append(path.name)
    return hits


def test_hp12_10_stripped_set_covers_every_promoting_field():
    assert PROVISIONAL_PROMOTING_MONEY_FIELDS <= PROVISIONAL_STRIPPED_MONEY_FIELDS
    source = (
        Path(__file__).resolve().parents[1] / "telemetry" / "trade_logger.py"
    ).read_text()
    # the two branches that promote a row read exactly these trade keys
    for name in PROVISIONAL_PROMOTING_MONEY_FIELDS:
        assert f'trade.get("{name}")' in source, (
            f"{name} is no longer read by append_close; the strip set is stale"
        )
