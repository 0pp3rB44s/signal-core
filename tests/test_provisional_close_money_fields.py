"""A provisional close must not crash, and must never invent a money figure.

`PositionManager` closes a position the moment it detects one is gone — before
Bitget reports realized PnL. That call carries only a return percentage, so the
USDT columns have no value yet. The v1 close writer used to receive `""` for
`pnl` and immediately compute `pnl - fee_value`, raising

    TypeError: unsupported operand type(s) for -: 'str' and 'float'

The exception was swallowed by `_sync_journal_close`, so every close logged a
warning and the dead-trade route — which has no second, exchange-confirmed
writer — silently lost its economics: three of the first ten trades under
817bc72 landed in the dataset with `fees=0.0` and no exchange source, counting
nowhere in the weekly freeze meter.
"""

from __future__ import annotations

import csv

import pytest

from telemetry.trade_logger import MoneyFieldError, TradeDatasetLogger, _money_or_none


def _rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh) if r.get("event_type") == "CLOSE"]


def test_provisional_close_writes_empty_money_columns(tmp_path):
    """No monetary figure yet: both money columns stay empty, no exception."""
    path = tmp_path / "trade_dataset.csv"
    TradeDatasetLogger(str(path)).append_close(
        symbol="SUIUSDT", result="dead_trade_timeout", pnl=None, fees=0.0278
    )
    row = _rows(path)[-1]
    assert row["pnl"] == ""
    assert row["net_pnl"] == ""
    assert row["fees"] == "0.0278"


def test_empty_string_pnl_is_provisional_not_a_crash(tmp_path):
    """The exact live regression: `pnl=""` must behave as 'not known yet'."""
    path = tmp_path / "trade_dataset.csv"
    TradeDatasetLogger(str(path)).append_close(
        symbol="TRXUSDT", result="dead_trade_timeout", pnl="", fees=""
    )
    row = _rows(path)[-1]
    assert row["pnl"] == ""
    assert row["net_pnl"] == ""
    assert row["fees"] == ""


def test_known_money_still_nets_out_fees(tmp_path):
    """A real monetary close is unchanged: net = pnl - |fees|."""
    path = tmp_path / "trade_dataset.csv"
    TradeDatasetLogger(str(path)).append_close(
        symbol="XLMUSDT", result="tp1", pnl=0.08712, fees=0.01495
    )
    row = _rows(path)[-1]
    assert float(row["pnl"]) == pytest.approx(0.08712)
    assert float(row["net_pnl"]) == pytest.approx(0.08712 - 0.01495)


def test_negative_fees_do_not_increase_net(tmp_path):
    """Bitget reports fees as negatives; magnitude is what gets subtracted."""
    path = tmp_path / "trade_dataset.csv"
    TradeDatasetLogger(str(path)).append_close(
        symbol="BTCUSDT", result="tp1", pnl=0.05, fees=-0.02
    )
    assert float(_rows(path)[-1]["net_pnl"]) == pytest.approx(0.03)


@pytest.mark.parametrize("bad", ["-0.8023%", "n/a", "abc", object(), True])
def test_non_money_value_fails_closed(bad):
    """A percentage or any non-numeric must raise, never coerce to 0.0.

    A silent 0.0 is indistinguishable from a real break-even trade and would
    corrupt the weekly meter that gates live trading.
    """
    with pytest.raises(MoneyFieldError):
        _money_or_none(bad, field="pnl", symbol="SOLUSDT")


@pytest.mark.parametrize("empty", [None, ""])
def test_absent_value_is_none_not_zero(empty):
    assert _money_or_none(empty, field="pnl") is None


def test_numeric_strings_are_accepted():
    """CSV round-trips produce numeric strings; those are still money."""
    assert _money_or_none("0.0279", field="pnl") == pytest.approx(0.0279)
    assert _money_or_none(-0.5, field="fees") == pytest.approx(-0.5)


def test_error_names_the_offending_field_and_symbol():
    with pytest.raises(MoneyFieldError) as exc:
        _money_or_none("1.73%", field="pnl", symbol="XLMUSDT")
    assert "pnl" in str(exc.value) and "XLMUSDT" in str(exc.value)
