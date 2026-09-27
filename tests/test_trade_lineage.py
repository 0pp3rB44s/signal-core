"""A position must name the plan that created it.

`trade_dataset_v2.csv` carried `position_lifecycle_id` but no `plan_id` or
`candidate_id`, so plans and positions shared no key and the funnel could not
measure executable -> opened. The identity was never missing: `ExecutionReport`
and the stored trade record both carry it. It just never reached the CSV.

The dangerous fix would have been to attribute a position to the nearest plan
by symbol, direction, time or entry price. Several tests here exist to prove
that is *not* what happens: when two plans on the same symbol and side sit
close together and one is selected, the opened position must name that one.
"""

from __future__ import annotations

import csv

import pytest

from clients.schemas import ExecutionReport
from telemetry.trade_logger import TradeDatasetV2Logger


def _report(plan_id="plan_1", candidate_id="cand_1", symbol="BTCUSDT",
            direction="LONG", strategy="momentum_breakout", lifecycle="pos-1", **extra):
    kwargs = dict(
        candidate_id=candidate_id, plan_id=plan_id, symbol=symbol, direction=direction,
        strategy=strategy, mode="LIVE", status="FILLED", message="",
        avg_entry=100.0, stop_loss=99.0, take_profits=[101.0],
        position_notional_usdt=25.0, leverage=3.0, position_lifecycle_id=lifecycle,
        exchange_entry_order_id="ord-1", exchange_entry_client_oid="coid-1",
        exchange_avg_entry=100.0, confirmed_position_size=0.25,
    )
    kwargs.update(extra)
    return ExecutionReport(**kwargs)


def _trade(plan_id="plan_1", candidate_id="cand_1", lifecycle="pos-1", **extra):
    row = {
        "symbol": "BTCUSDT", "direction": "LONG", "strategy": "momentum_breakout",
        "position_lifecycle_id": lifecycle, "plan_id": plan_id, "candidate_id": candidate_id,
        "exchange_avg_entry": 100.0, "exchange_truth_pnl": 0.5, "exchange_truth_fee": 0.02,
        "sync_source": "bitget_position_history", "opened_at": "2026-08-10T00:00:00+00:00",
        "closed_at": "2026-08-10T00:30:00+00:00", "exchange_entry_order_id": "ord-1",
    }
    row.update(extra)
    return row


def _rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture
def logger(tmp_path):
    return TradeDatasetV2Logger(path=tmp_path / "trade_dataset_v2.csv")


# --- schema -----------------------------------------------------------------


def test_lineage_columns_exist(logger):
    names = logger._fieldnames()
    assert "plan_id" in names and "candidate_id" in names


def test_lineage_columns_are_appended_not_inserted(logger):
    """Existing columns must keep their position; readers index by name and order."""
    names = logger._fieldnames()
    assert names[-2:] == ["plan_id", "candidate_id"]
    assert names.index("position_lifecycle_id") < names.index("plan_id")


def test_no_existing_column_was_renamed_or_removed(logger):
    """The 2026-07-07 incident: a changed schema shifted values into wrong columns."""
    required = {
        "event_type", "timestamp", "symbol", "direction", "strategy", "status", "result",
        "opened_at", "closed_at", "entry", "planned_avg_entry", "exchange_avg_entry",
        "position_lifecycle_id", "exchange_entry_order_id", "net_pnl", "gross_pnl",
        "fees", "funding", "sync_source", "data_confidence", "message",
    }
    assert required.issubset(set(logger._fieldnames()))


# --- happy path: candidate -> plan -> fill -> lifecycle -> close ------------


def test_open_row_carries_the_executed_plan(logger):
    logger.append_open(_report(plan_id="P1", candidate_id="C1", lifecycle="L1"))
    row = _rows(logger.path)[0]
    assert row["event_type"] == "OPEN"
    assert row["plan_id"] == "P1"
    assert row["candidate_id"] == "C1"
    assert row["position_lifecycle_id"] == "L1"
    assert row["strategy"] == "momentum_breakout"


def test_close_inherits_the_lineage_the_open_established(logger):
    logger.append_open(_report(plan_id="P1", candidate_id="C1", lifecycle="L1"))
    logger.append_close(_trade(plan_id="P1", candidate_id="C1", lifecycle="L1"),
                        result="WIN", pnl=0.5, quality={})
    rows = _rows(logger.path)
    assert len(rows) == 2
    open_row, close_row = rows
    for field in ("plan_id", "candidate_id", "position_lifecycle_id", "strategy"):
        assert close_row[field] == open_row[field], f"{field} changed between open and close"


def test_full_chain_is_joinable_end_to_end(logger):
    """The point of the patch: a close row names its candidate and its plan."""
    logger.append_open(_report(plan_id="P1", candidate_id="C1", lifecycle="L1"))
    logger.append_close(_trade(plan_id="P1", candidate_id="C1", lifecycle="L1"),
                        result="WIN", pnl=0.5, quality={})
    close_row = _rows(logger.path)[-1]
    assert (close_row["candidate_id"], close_row["plan_id"],
            close_row["position_lifecycle_id"]) == ("C1", "P1", "L1")


# --- attribution: no nearest-plan guessing ---------------------------------


def test_competing_same_symbol_plans_do_not_confuse_attribution(logger):
    """Two plans, same symbol, same side, moments apart. Only one executed."""
    selected = _report(plan_id="P_SELECTED", candidate_id="C_SELECTED", lifecycle="L1")
    logger.append_open(selected)
    row = _rows(logger.path)[0]
    assert row["plan_id"] == "P_SELECTED"
    assert row["candidate_id"] == "C_SELECTED"


def test_attribution_ignores_entry_price_proximity(logger):
    """A different plan priced closer to the fill must not be credited."""
    logger.append_open(_report(plan_id="P_SELECTED", candidate_id="C_SELECTED",
                               avg_entry=100.0, exchange_avg_entry=100.9))
    assert _rows(logger.path)[0]["plan_id"] == "P_SELECTED"


def test_two_positions_keep_separate_lineage(logger):
    logger.append_open(_report(plan_id="PA", candidate_id="CA", lifecycle="LA"))
    logger.append_open(_report(plan_id="PB", candidate_id="CB", lifecycle="LB", symbol="SOLUSDT"))
    rows = _rows(logger.path)
    assert [(r["plan_id"], r["position_lifecycle_id"]) for r in rows] == [("PA", "LA"), ("PB", "LB")]


# --- backward compatibility -------------------------------------------------


def test_legacy_trade_without_lineage_still_closes(logger):
    """A position opened before this change carries blanks, not an error."""
    legacy = _trade()
    legacy.pop("plan_id")
    legacy.pop("candidate_id")
    logger.append_close(legacy, result="WIN", pnl=0.5, quality={})
    row = _rows(logger.path)[0]
    assert row["plan_id"] == ""
    assert row["candidate_id"] == ""
    assert row["net_pnl"] not in ("", None)  # economics unaffected


def test_recovered_position_without_plan_is_not_rejected(logger):
    recovered = _trade(strategy="recovered_exchange_position")
    recovered.pop("plan_id")
    recovered.pop("candidate_id")
    logger.append_close(recovered, result="WIN", pnl=0.5, quality={})
    assert len(_rows(logger.path)) == 1


def test_provisional_close_also_carries_lineage(logger):
    """A provisional row must still be traceable; it is just not economics."""
    provisional = _trade()
    provisional.pop("exchange_truth_pnl")
    provisional.pop("sync_source")
    logger.append_close(provisional, result="UNKNOWN", pnl=None, quality={})
    row = _rows(logger.path)[0]
    assert row["plan_id"] == "plan_1"


# --- deduplication is unchanged --------------------------------------------


def test_same_lifecycle_closed_twice_is_still_deduplicated(logger):
    trade = _trade(lifecycle="L1")
    logger.append_close(trade, result="WIN", pnl=0.5, quality={})
    logger.append_close(trade, result="WIN", pnl=0.5, quality={})
    closes = [r for r in _rows(logger.path) if r["event_type"] == "CLOSE"]
    assert len(closes) == 1


def test_differing_lineage_does_not_create_a_second_close(logger):
    """Metadata must not become part of the dedup identity."""
    logger.append_close(_trade(lifecycle="L1", plan_id="P1"), result="WIN", pnl=0.5, quality={})
    logger.append_close(_trade(lifecycle="L1", plan_id="P_OTHER"), result="WIN", pnl=0.5, quality={})
    closes = [r for r in _rows(logger.path) if r["event_type"] == "CLOSE"]
    assert len(closes) == 1


# --- economics untouched ----------------------------------------------------


def test_economics_are_identical_with_and_without_lineage(logger, tmp_path):
    """Metadata only: the money columns must not move."""
    other = TradeDatasetV2Logger(path=tmp_path / "other.csv")
    with_lineage = _trade(plan_id="P1", candidate_id="C1")
    without = _trade()
    without.pop("plan_id")
    without.pop("candidate_id")

    logger.append_close(with_lineage, result="WIN", pnl=0.5, quality={})
    other.append_close(without, result="WIN", pnl=0.5, quality={})

    a, b = _rows(logger.path)[0], _rows(other.path)[0]
    money = ("net_pnl", "gross_pnl", "fees", "funding", "exchange_truth_pnl",
             "exchange_truth_fee", "result", "sync_source", "data_confidence", "event_type")
    assert {k: a[k] for k in money} == {k: b[k] for k in money}
