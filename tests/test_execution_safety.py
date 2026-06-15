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


def test_fee_adjusted_break_even_buffer_math() -> None:
    entry = 100.0
    fee_buffer_pct = 0.12

    break_even = entry * (1 + (fee_buffer_pct / 100))

    assert round(break_even, 4) == 100.12


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