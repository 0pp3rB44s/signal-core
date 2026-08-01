"""A percentage must never be stored, summed, or trusted as USDT.

`ClosedTradeWriter._sync_journal_close` passed `margin_roi_pct` into
`TradeJournal.log_close(pnl=...)`. Every LIVE close then produced a second
dataset row whose money columns held a return percentage, and
`RiskManager._weekly_realized_pnl` summed those into the WEEKLY_FREEZE_LOSS_PCT
kill-switch: on 2026-08-01 the meter read +4.64 USDT against an exchange truth of
+0.4066, and the same defect had previously reported -3.94 against -0.28 and
blocked 24 of 31 candidates.

These tests pin all three layers independently, because any one of them alone
would have prevented the incident: the writer must not emit money it does not
have, the reader must not count money from a non-exchange source, and the
deduplicator must recognise the provisional and exchange rows as one trade.
"""

from __future__ import annotations

import csv
import importlib
import inspect
from decimal import Decimal
from pathlib import Path

import pytest

from telemetry.close_record_sources import (
    ECONOMIC_CLOSE_EVENT_TYPES,
    EXCHANGE_CONFIRMED_CLOSE_SOURCES,
    PROVISIONAL_CLOSE_EVENT_TYPE,
    QUARANTINED_CLOSE_EVENT_TYPE,
    is_economic_close,
)
from telemetry.trade_logger import TradeDatasetV2Logger

EXCHANGE_SOURCE = "bitget_position_history"


def _rows(path: Path) -> list[dict]:
    with path.open("r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _closes(path: Path) -> list[dict]:
    return [r for r in _rows(path) if str(r.get("event_type") or "").upper() in ECONOMIC_CLOSE_EVENT_TYPES]


def _trade(**overrides) -> dict:
    trade = {
        "symbol": "XLMUSDT",
        "direction": "SHORT",
        "strategy": "trend_continuation",
        "opened_at": "2026-08-01T18:51:51+00:00",
        "closed_at": "2026-08-01T19:17:50+00:00",
        "entry": 0.16863,
        "exit": 0.16989,
        "take_profits": [],
    }
    trade.update(overrides)
    return trade


# --------------------------------------------------------------------------
# 1-3. A percentage can never occupy a money column.
# --------------------------------------------------------------------------

def test_margin_roi_pct_is_never_written_into_pnl(tmp_path):
    """The exact defect: the only figure available is a ROI percentage."""
    logger = TradeDatasetV2Logger(tmp_path / "d.csv")
    logger.append_close(trade=_trade(), result="stop_loss", pnl=None, quality={}, margin_roi_pct=-2.6029)

    row = _rows(tmp_path / "d.csv")[0]
    assert row["event_type"] == PROVISIONAL_CLOSE_EVENT_TYPE
    assert row["pnl"] == ""
    assert row["net_pnl"] == ""
    assert row["fees"] == ""
    assert Decimal(row["margin_roi_pct"]) == Decimal("-2.6029")


def test_price_return_pct_is_never_written_into_pnl(tmp_path):
    logger = TradeDatasetV2Logger(tmp_path / "d.csv")
    logger.append_close(
        trade=_trade(price_return_pct=0.211),
        result="tp1",
        pnl=None,
        quality={},
        margin_roi_pct=1.0476,
    )

    row = _rows(tmp_path / "d.csv")[0]
    assert row["pnl"] == ""
    assert row["net_pnl"] == ""
    assert Decimal(row["price_return_pct"]) == Decimal("0.211")


def test_exchange_truth_close_is_monetarily_coherent(tmp_path):
    """gross - fees == net, and every figure is USDT."""
    logger = TradeDatasetV2Logger(tmp_path / "d.csv")
    logger.append_close(
        trade=_trade(
            exchange_truth_pnl=-0.14923742,
            exchange_truth_fee=0.02071741,
            sync_source=EXCHANGE_SOURCE,
            position_lifecycle_id="pos-d319b066",
            margin_roi_pct=-2.602939,
        ),
        result="stop_loss",
        pnl=None,
        quality={},
    )

    row = _rows(tmp_path / "d.csv")[0]
    assert row["event_type"] == "CLOSE"
    assert Decimal(row["pnl"]) == Decimal("-0.14923742")
    assert Decimal(row["net_pnl"]) == Decimal("-0.14923742")
    assert Decimal(row["fees"]) == Decimal("0.02071741")
    # The percentage survives, under its own name, next to the money.
    assert Decimal(row["margin_roi_pct"]) == Decimal("-2.602939")


def test_sync_journal_close_does_not_pass_a_percentage_as_money():
    """Guards the call site itself, not just the writer's behaviour."""
    from execution.closed_trade_writer import ClosedTradeWriterMixin

    source = inspect.getsource(ClosedTradeWriterMixin._sync_journal_close)
    assert "pnl=round(margin_roi_pct" not in source
    assert "margin_roi_pct=" in source


# --------------------------------------------------------------------------
# 4-7. The weekly kill-switch counts exchange truth and nothing else.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "row,counts",
    [
        ({"event_type": "CLOSE", "sync_source": EXCHANGE_SOURCE}, True),
        ({"event_type": "POSITION_CLOSED", "sync_source": EXCHANGE_SOURCE}, True),
        ({"event_type": "CLOSE", "sync_source": "position_manager"}, False),
        ({"event_type": PROVISIONAL_CLOSE_EVENT_TYPE, "sync_source": EXCHANGE_SOURCE}, False),
        ({"event_type": QUARANTINED_CLOSE_EVENT_TYPE, "sync_source": EXCHANGE_SOURCE}, False),
        ({"event_type": "CLOSE", "sync_source": "simulated"}, False),
        ({"event_type": "CLOSE", "sync_source": "unknown_future_writer"}, False),
        ({"event_type": "CLOSE", "sync_source": ""}, False),
        ({"event_type": "CLOSE", "sync_source": "0.0"}, False),
        ({"event_type": "CLOSE"}, False),
    ],
)
def test_only_exchange_confirmed_closes_carry_money(row, counts):
    assert is_economic_close(row) is counts


@pytest.mark.parametrize(
    "row,shown",
    [
        # Legacy rows predate both columns and must keep rendering.
        ({"net_pnl": "-0.5"}, True),
        ({"event_type": "CLOSE", "sync_source": EXCHANGE_SOURCE}, True),
        # The rows that would display a percentage as money.
        ({"event_type": "CLOSE", "sync_source": "position_manager"}, False),
        ({"event_type": PROVISIONAL_CLOSE_EVENT_TYPE}, False),
        ({"event_type": QUARANTINED_CLOSE_EVENT_TYPE}, False),
        ({"event_type": "CLOSE", "sync_source": "simulated"}, False),
    ],
)
def test_display_rule_drops_percentage_rows_but_keeps_legacy(row, shown):
    """The dashboard must not show provisional money, but must not lose history."""
    from telemetry.close_record_sources import is_displayable_close

    assert is_displayable_close(row) is shown


def test_display_rule_is_never_used_for_risk():
    """A displayable row is not automatically an economic one."""
    from telemetry.close_record_sources import is_displayable_close

    legacy = {"net_pnl": "-0.5"}
    assert is_displayable_close(legacy) is True
    assert is_economic_close(legacy) is False


def test_expectancy_and_weekly_pnl_share_one_allowlist():
    """The two consumers drifted apart once; they must not again."""
    from risk import symbol_expectancy

    assert symbol_expectancy.EXCHANGE_CONFIRMED_SOURCES is EXCHANGE_CONFIRMED_CLOSE_SOURCES
    assert symbol_expectancy.CLOSE_EVENT_TYPES is ECONOMIC_CLOSE_EVENT_TYPES


# --------------------------------------------------------------------------
# 8-14. Deduplication links the provisional row to its exchange twin.
# --------------------------------------------------------------------------

def _write_pair(logger: TradeDatasetV2Logger, *, symbol, direction, provisional_at, exchange_at,
                roi, net, fees, lifecycle):
    logger.append_close(
        trade=_trade(symbol=symbol, direction=direction, closed_at=provisional_at),
        result="tp1",
        pnl=None,
        quality={},
        margin_roi_pct=roi,
    )
    logger.append_close(
        trade=_trade(
            symbol=symbol,
            direction=direction,
            closed_at=exchange_at,
            exchange_truth_pnl=net,
            exchange_truth_fee=fees,
            sync_source=EXCHANGE_SOURCE,
            position_lifecycle_id=lifecycle,
        ),
        result="tp1",
        pnl=None,
        quality={},
    )


def test_winning_trade_is_not_double_counted(tmp_path):
    path = tmp_path / "d.csv"
    _write_pair(
        TradeDatasetV2Logger(path), symbol="AVAXUSDT", direction="SHORT",
        provisional_at="2026-08-01T18:30:59+00:00", exchange_at="2026-08-01T18:30:59+00:00",
        roi=2.652319, net=0.15541176, fees=0.02098824, lifecycle="pos-688ab26",
    )
    economic = _closes(path)
    assert len(economic) == 1
    assert Decimal(economic[0]["net_pnl"]) == Decimal("0.15541176")


def test_losing_trade_is_not_double_counted(tmp_path):
    path = tmp_path / "d.csv"
    _write_pair(
        TradeDatasetV2Logger(path), symbol="XLMUSDT", direction="SHORT",
        provisional_at="2026-08-01T19:17:50+00:00", exchange_at="2026-08-01T19:17:50+00:00",
        roi=-2.602939, net=-0.14923742, fees=0.02071741, lifecycle="pos-d319b06",
    )
    economic = _closes(path)
    assert len(economic) == 1
    assert Decimal(economic[0]["net_pnl"]) == Decimal("-0.14923742")


def test_lifecycle_id_dedups_two_economic_rows(tmp_path):
    path = tmp_path / "d.csv"
    logger = TradeDatasetV2Logger(path)
    for _ in range(2):
        logger.append_close(
            trade=_trade(
                exchange_truth_pnl=0.0796674,
                exchange_truth_fee=0.0301326,
                sync_source=EXCHANGE_SOURCE,
                position_lifecycle_id="pos-69c52ac",
            ),
            result="tp1", pnl=None, quality={},
        )
    assert len(_closes(path)) == 1


def test_dedup_falls_back_when_one_row_has_no_lifecycle_id(tmp_path):
    """The provisional row never carries a lifecycle id — the exact live case."""
    path = tmp_path / "d.csv"
    logger = TradeDatasetV2Logger(path)
    logger.append_close(
        trade=_trade(exchange_truth_pnl=0.0923428, exchange_truth_fee=0.0316572,
                     sync_source=EXCHANGE_SOURCE),
        result="tp1", pnl=None, quality={},
    )
    logger.append_close(
        trade=_trade(exchange_truth_pnl=0.0923428, exchange_truth_fee=0.0316572,
                     sync_source=EXCHANGE_SOURCE, position_lifecycle_id="pos-1019b0f"),
        result="tp1", pnl=None, quality={},
    )
    assert len(_closes(path)) == 1


def test_one_second_timestamp_tolerance(tmp_path):
    """SOLUSDT closed at 18:49:58 locally and 18:49:57 on the exchange."""
    path = tmp_path / "d.csv"
    logger = TradeDatasetV2Logger(path)
    logger.append_close(
        trade=_trade(symbol="SOLUSDT", closed_at="2026-08-01T18:49:58+00:00",
                     exchange_truth_pnl=0.08897954, exchange_truth_fee=0.02552046,
                     sync_source=EXCHANGE_SOURCE),
        result="tp1", pnl=None, quality={},
    )
    logger.append_close(
        trade=_trade(symbol="SOLUSDT", closed_at="2026-08-01T18:49:57+00:00",
                     exchange_truth_pnl=0.08897954, exchange_truth_fee=0.02552046,
                     sync_source=EXCHANGE_SOURCE, position_lifecycle_id="pos-cf10843"),
        result="tp1", pnl=None, quality={},
    )
    assert len(_closes(path)) == 1


def test_two_seconds_apart_is_not_merged(tmp_path):
    """Tolerance must be tight enough not to swallow a genuinely distinct close."""
    path = tmp_path / "d.csv"
    logger = TradeDatasetV2Logger(path)
    for stamp, lifecycle in (
        ("2026-08-01T18:49:55+00:00", "pos-a"),
        ("2026-08-01T18:49:58+00:00", "pos-b"),
    ):
        logger.append_close(
            trade=_trade(symbol="SOLUSDT", closed_at=stamp, exchange_truth_pnl=0.05,
                         exchange_truth_fee=0.01, sync_source=EXCHANGE_SOURCE,
                         position_lifecycle_id=lifecycle),
            result="tp1", pnl=None, quality={},
        )
    assert len(_closes(path)) == 2


def test_long_and_short_on_one_symbol_stay_separate(tmp_path):
    path = tmp_path / "d.csv"
    logger = TradeDatasetV2Logger(path)
    for direction, net, lifecycle in (("LONG", 0.11, "pos-long"), ("SHORT", -0.07, "pos-short")):
        logger.append_close(
            trade=_trade(symbol="BTCUSDT", direction=direction, exchange_truth_pnl=net,
                         exchange_truth_fee=0.01, sync_source=EXCHANGE_SOURCE,
                         position_lifecycle_id=lifecycle),
            result="tp1", pnl=None, quality={},
        )
    economic = _closes(path)
    assert len(economic) == 2
    assert {r["direction"] for r in economic} == {"LONG", "SHORT"}


def test_multiple_symbols_do_not_collide(tmp_path):
    path = tmp_path / "d.csv"
    logger = TradeDatasetV2Logger(path)
    for symbol in ("BTCUSDT", "SOLUSDT", "XLMUSDT", "AVAXUSDT", "DOGEUSDT"):
        logger.append_close(
            trade=_trade(symbol=symbol, exchange_truth_pnl=0.1, exchange_truth_fee=0.01,
                         sync_source=EXCHANGE_SOURCE, position_lifecycle_id=f"pos-{symbol}"),
            result="tp1", pnl=None, quality={},
        )
    assert len({r["symbol"] for r in _closes(path)}) == 5


# --------------------------------------------------------------------------
# 15-18. Migration behaviour against the real live rows.
# --------------------------------------------------------------------------

LIVE_PAIRS = [
    # symbol, direction, roi_written_as_money, exchange_net, exchange_fees
    ("BTCUSDT", "SHORT", "0.9497", "0.0796674", "0.0301326"),
    ("XLMUSDT", "SHORT", "1.0476", "0.0923428", "0.0316572"),
    ("AVAXUSDT", "SHORT", "2.6523", "0.15541176", "0.02098824"),
    ("DOGEUSDT", "SHORT", "1.9328", "0.1394796", "0.02588039"),
    ("SOLUSDT", "SHORT", "1.2518", "0.08897954", "0.02552046"),
    ("XLMUSDT", "SHORT", "-2.6029", "-0.14923742", "0.02071741"),
]


def _live_like_dataset(path: Path) -> None:
    fields = [
        "event_type", "timestamp", "symbol", "direction", "strategy", "status", "result",
        "opened_at", "closed_at", "fees", "pnl", "net_pnl", "margin_roi_pct",
        "position_lifecycle_id", "sync_source",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for i, (symbol, direction, roi, net, fees) in enumerate(LIVE_PAIRS):
            closed = f"2026-08-01T19:{10 + i:02d}:00+00:00"
            w.writerow({
                "event_type": "CLOSE", "symbol": symbol, "direction": direction,
                "opened_at": f"2026-08-01T18:{10 + i:02d}:00+00:00", "closed_at": closed,
                "fees": "", "pnl": roi, "net_pnl": roi, "margin_roi_pct": roi,
                "position_lifecycle_id": "", "sync_source": "position_manager",
            })
            w.writerow({
                "event_type": "CLOSE", "symbol": symbol, "direction": direction,
                "opened_at": f"2026-08-01T18:{10 + i:02d}:00+00:00", "closed_at": closed,
                "fees": fees, "pnl": net, "net_pnl": net, "margin_roi_pct": roi,
                "position_lifecycle_id": f"pos-{symbol}-{i}", "sync_source": EXCHANGE_SOURCE,
            })


def _migration():
    return importlib.import_module("deployment.quarantine_percentage_closes")


def test_migration_dry_run_changes_nothing(tmp_path):
    path = tmp_path / "trade_dataset_v2.csv"
    _live_like_dataset(path)
    before = path.read_bytes()

    mig = _migration()
    assert mig.process(path, apply=False, backup_dir=tmp_path / "bk") == 0
    assert path.read_bytes() == before


def test_migration_apply_retires_exactly_the_percentage_rows(tmp_path):
    path = tmp_path / "trade_dataset_v2.csv"
    _live_like_dataset(path)

    mig = _migration()
    assert mig.process(path, apply=True, backup_dir=tmp_path / "bk") == 0

    rows = _rows(path)
    quarantined = [r for r in rows if r["event_type"] == QUARANTINED_CLOSE_EVENT_TYPE]
    survivors = _closes(path)
    assert len(quarantined) == len(LIVE_PAIRS)
    assert len(survivors) == len(LIVE_PAIRS)
    # Not one exchange-truth row was touched.
    assert all(r["sync_source"] == EXCHANGE_SOURCE for r in survivors)
    assert all(r["sync_source"] == "position_manager" for r in quarantined)


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "trade_dataset_v2.csv"
    _live_like_dataset(path)
    mig = _migration()

    mig.process(path, apply=True, backup_dir=tmp_path / "bk")
    after_first = path.read_bytes()
    mig.process(path, apply=True, backup_dir=tmp_path / "bk")
    assert path.read_bytes() == after_first


def test_migration_takes_a_mode_600_backup(tmp_path):
    path = tmp_path / "trade_dataset_v2.csv"
    _live_like_dataset(path)
    backup_dir = tmp_path / "bk"

    _migration().process(path, apply=True, backup_dir=backup_dir)

    backups = list(backup_dir.glob("*.bak"))
    assert len(backups) == 1
    assert oct(backups[0].stat().st_mode)[-3:] == "600"


def test_six_live_trades_reconstruct_to_the_exchange_total(tmp_path):
    """The headline number: +0.406644 USDT, not +4.64."""
    path = tmp_path / "trade_dataset_v2.csv"
    _live_like_dataset(path)
    mig = _migration()

    polluted = mig.weekly_total(_rows(path))
    mig.process(path, apply=True, backup_dir=tmp_path / "bk")
    clean = mig.weekly_total(_rows(path))

    expected = sum(Decimal(net) for _, _, _, net, _ in LIVE_PAIRS)
    assert clean == expected
    assert clean.quantize(Decimal("0.000001")) == Decimal("0.406644")
    assert polluted > clean


# --------------------------------------------------------------------------
# 19-20. The kill-switch and expectancy read only exchange truth.
# --------------------------------------------------------------------------

def test_weekly_freeze_meter_ignores_percentage_rows(tmp_path, monkeypatch):
    """End-to-end through RiskManager, on the unmigrated dataset."""
    import risk.risk_manager as rm

    logs = tmp_path / "logs"
    logs.mkdir()
    _live_like_dataset(logs / "trade_dataset_v2.csv")
    monkeypatch.setattr(rm, "BASE_PATH", tmp_path)

    manager = rm.RiskManager.__new__(rm.RiskManager)
    manager._weekly_pnl_cache = None
    manager.WEEKLY_PNL_CACHE_SECONDS = rm.RiskManager.WEEKLY_PNL_CACHE_SECONDS

    total = rm.RiskManager._weekly_realized_pnl(manager)
    expected = float(sum(Decimal(net) for _, _, _, net, _ in LIVE_PAIRS))
    assert total == pytest.approx(expected, abs=1e-9)
    assert total == pytest.approx(0.406644, abs=1e-6)


def test_expectancy_stays_directional_and_exchange_only(tmp_path, monkeypatch):
    from risk import symbol_expectancy as se

    logs = tmp_path / "logs"
    logs.mkdir()
    dataset = logs / "trade_dataset_v2.csv"
    _live_like_dataset(dataset)
    monkeypatch.setattr(se, "DATASET_PATH", dataset)

    rows = [r for r in _rows(dataset) if r["sync_source"] in EXCHANGE_CONFIRMED_CLOSE_SOURCES]
    assert rows, "fixture must contain exchange rows"
    # Every counted row is exchange-confirmed and keyed by (symbol, direction).
    assert all(is_economic_close(r) for r in rows)
    assert {(r["symbol"], r["direction"]) for r in rows} == {
        (symbol, direction) for symbol, direction, _, _, _ in LIVE_PAIRS
    }
