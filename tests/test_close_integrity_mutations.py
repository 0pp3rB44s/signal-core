"""Mutation tests: prove the guards fail when the guarded behaviour is removed.

A passing test suite only means "the code does what it does". These three
reintroduce the exact defects seen in production and assert that something
still catches them — so a future refactor that quietly restores the old
behaviour cannot go green.
"""

from __future__ import annotations

import csv

import pytest

from execution.close_dedup import economic_close_exists
from execution.close_reconciler import (
    CloseReconciliationUnavailable,
    RECONCILED_SOURCE,
    reconcile_close,
)
from telemetry.close_record_sources import is_economic_close
from telemetry.trade_logger import MoneyFieldError, TradeDatasetLogger, _money_or_none


# ── (a) the original str/float bug is caught ────────────────────────────────

def test_mutation_a_percentage_into_money_column_is_rejected():
    """Restore the old behaviour — a ROI percentage handed to a money column.

    Previously `pnl=""` reached `pnl - fee_value` and raised TypeError deep in
    the writer, where `_sync_journal_close` swallowed it. Now the boundary
    either accepts it as 'unknown' or names it as a bug; it never computes.
    """
    assert _money_or_none("", field="pnl") is None          # unknown, not a crash
    with pytest.raises(MoneyFieldError):
        _money_or_none("-0.8023%", field="pnl", symbol="TRXUSDT")


def test_mutation_a_writer_never_raises_typeerror_on_provisional(tmp_path):
    path = tmp_path / "trade_dataset.csv"
    # The exact live call shape that used to crash.
    TradeDatasetLogger(str(path)).append_close(
        symbol="TRXUSDT", result="dead_trade_timeout", pnl="", fees=""
    )
    with open(path, newline="", encoding="utf-8") as fh:
        row = [r for r in csv.DictReader(fh) if r["event_type"] == "CLOSE"][-1]
    assert row["pnl"] == "" and row["net_pnl"] == ""


# ── (b) a dead-trade close cannot silently vanish ───────────────────────────

def test_mutation_b_unreconciled_dead_trade_is_visibly_non_economic():
    """If reconciliation never runs, the row must not masquerade as economic.

    This is the failure that lost five of thirteen trades: a row with
    sync_source='dead_trade_timeout' and fees=0.0 looked like a close but
    counted nowhere. It must be *detectably* non-economic, not quietly so.
    """
    unreconciled = {
        "event_type": "CLOSE", "symbol": "TRXUSDT", "direction": "SHORT",
        "sync_source": "dead_trade_timeout", "net_pnl": "0.0", "fees": "0.0",
    }
    assert is_economic_close(unreconciled) is False

    reconciled = dict(unreconciled, sync_source=RECONCILED_SOURCE,
                      net_pnl="-0.06631471", fees="0.0301247")
    assert is_economic_close(reconciled) is True


def test_mutation_b_reconciler_refuses_to_invent_zero():
    """A silent 0.0 would be indistinguishable from a real break-even trade."""
    with pytest.raises(CloseReconciliationUnavailable):
        reconcile_close(symbol="TRXUSDT", direction="short", opened_at_ms=1,
                        size=77.0, fetch_history=lambda: [], sleep=lambda s: None)


# ── (c) double reconciliation produces no duplicate ─────────────────────────

FIELDS = ["event_type", "symbol", "direction", "opened_at", "net_pnl", "fees",
          "sync_source", "position_lifecycle_id", "confirmed_position_size"]


def test_mutation_c_second_pass_is_blocked(tmp_path):
    path = tmp_path / "trade_dataset_v2.csv"
    row = {"event_type": "CLOSE", "symbol": "TRXUSDT", "direction": "SHORT",
           "opened_at": "2026-08-03T06:01:36", "net_pnl": "-0.06631471",
           "fees": "0.0301247", "sync_source": RECONCILED_SOURCE,
           "position_lifecycle_id": "pos-abc", "confirmed_position_size": "77"}
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerow(row)

    # First pass already wrote it; a second must be refused on every identity.
    assert economic_close_exists(path, {"position_lifecycle_id": "pos-abc"}) is True
    assert economic_close_exists(path, row) is True

    # And a genuinely different lifecycle is still allowed through.
    other = dict(row, position_lifecycle_id="pos-def",
                 opened_at="2026-08-03T06:11:36", confirmed_position_size="80")
    assert economic_close_exists(path, other) is False


def test_mutation_c_duplicate_would_double_the_weekly_meter(tmp_path):
    """Show the consequence the dedup prevents, in money."""
    from decimal import Decimal
    path = tmp_path / "trade_dataset_v2.csv"
    row = {"event_type": "CLOSE", "symbol": "TRXUSDT", "direction": "SHORT",
           "opened_at": "2026-08-03T06:01:36", "net_pnl": "-0.06631471",
           "fees": "0.0301247", "sync_source": RECONCILED_SOURCE,
           "position_lifecycle_id": "pos-abc", "confirmed_position_size": "77"}
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerow(row)
        w.writerow(row)  # the duplicate dedup exists to prevent
    with open(path, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if is_economic_close(r)]
    doubled = sum(Decimal(r["net_pnl"]) for r in rows)
    assert doubled == Decimal("-0.13262942")  # exactly twice the real loss
    assert economic_close_exists(path, {"position_lifecycle_id": "pos-abc"}) is True
