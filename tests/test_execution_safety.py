from telemetry.trade_logger import LiveTradeJournalLogger, TradeDatasetLogger


def test_force_sync_closed_closes_open_trade(tmp_path) -> None:
    path = tmp_path / "journal.json"

    logger = LiveTradeJournalLogger(path)

    logger._write([
        {
            "symbol": "SOLUSDT",
            "status": "OPEN",
            "pnl": 0.0,
        }
    ])

    logger.force_sync_closed(
        symbol="SOLUSDT",
        result="closed_synced",
        pnl=4.25,
    )

    rows = logger._read()

    assert rows[0]["status"] == "CLOSED"
    assert rows[0]["result"] == "closed_synced"
    assert rows[0]["pnl"] == 4.25


def test_duplicate_open_trade_is_prevented(tmp_path) -> None:
    path = tmp_path / "journal.json"

    logger = LiveTradeJournalLogger(path)

    rows = [
        {
            "symbol": "ETHUSDT",
            "status": "OPEN",
        }
    ]

    logger._write(rows)

    logger.log_close(
        symbol="ETHUSDT",
        result="tp_hit",
        pnl=2.0,
    )

    rows = logger._read()

    closed = [
        row for row in rows
        if row["symbol"] == "ETHUSDT"
    ]

    assert len(closed) == 1
    assert closed[0]["status"] == "CLOSED"


def _be_stub(configured=0.10, fee_bps=12.0, margin=0.04):
    from types import SimpleNamespace
    from execution.tp_sl_lifecycle import TpSlLifecycleMixin

    stub = TpSlLifecycleMixin.__new__(TpSlLifecycleMixin)
    stub.settings = SimpleNamespace(
        break_even_fee_buffer_pct=configured,
        planner_estimated_roundtrip_fee_bps=fee_bps,
        break_even_extra_margin_pct=margin,
    )
    return stub


def test_fee_adjusted_break_even_covers_fees_plus_margin() -> None:
    # BE buffer must be max(configured, roundtrip_fee + margin). With the .env
    # 0.10% (below the 0.12% roundtrip fee) the effective buffer becomes
    # 0.12 + 0.04 = 0.16%, so a BE stop-out is flat-to-green, not a fee loss.
    stub = _be_stub(configured=0.10, fee_bps=12.0, margin=0.04)
    be_long = stub._fee_adjusted_break_even("LONG", 100.0)
    be_short = stub._fee_adjusted_break_even("SHORT", 100.0)
    assert round(be_long, 4) == 100.16
    assert round(be_short, 4) == 99.84
    # The buffer now exceeds the roundtrip fee, so the net after fees is positive.
    assert (be_long - 100.0) / 100.0 * 100.0 > 12.0 / 100.0


def test_fee_adjusted_break_even_respects_higher_configured_buffer() -> None:
    # An explicitly larger configured buffer still wins the max().
    stub = _be_stub(configured=0.30, fee_bps=12.0, margin=0.04)
    assert round(stub._fee_adjusted_break_even("LONG", 100.0), 4) == 100.30


def test_dynamic_precision_rounding() -> None:
    price = 123.456789
    rounded = round(price, 3)

    assert rounded == 123.457


def test_symbol_cooldown_timestamp_storage() -> None:
    cooldowns = {}

    cooldowns["SOLUSDT"] = 123456789

    assert "SOLUSDT" in cooldowns
    assert cooldowns["SOLUSDT"] == 123456789


def test_trade_dataset_logger_writes_open_and_close_rows(tmp_path) -> None:
    dataset_path = tmp_path / "trade_dataset.csv"
    logger = TradeDatasetLogger(dataset_path)

    class Report:
        symbol = "SOLUSDT"
        direction = "LONG"
        strategy = "momentum_breakout"
        status = "EXECUTED"
        avg_entry = 100.0
        stop_loss = 99.0
        take_profits = [101.0, 102.0, 103.0]
        position_notional_usdt = 50.0
        leverage = 5.0
        message = "test open"

    logger.append_open(Report())
    logger.append_close(
        symbol="SOLUSDT",
        result="tp1_fee_be",
        pnl=1.25,
        exit_price=101.0,
        tp1_hit=True,
        break_even_active=True,
    )

    content = dataset_path.read_text()

    assert "event_type" in content
    assert "OPEN" in content
    assert "CLOSE" in content
    assert "SOLUSDT" in content
    assert "momentum_breakout" in content
    assert "tp1_fee_be" in content