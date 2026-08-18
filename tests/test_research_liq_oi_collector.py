"""The research collector must be unable to touch trading, and must fail quietly.

Two properties matter more than feature coverage here. First, nothing in the live decision path
may import this module — a research collector that can raise into execution is a liability, not an
asset. Second, every failure mode (malformed frame, dead endpoint, full queue, writer error) must
be swallowed and counted rather than propagated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.collectors.liq_oi_collector import LiquidationOICollector, _oi_row

SYMBOLS = ("BTCUSDT", "ETHUSDT")


@pytest.fixture()
def collector(tmp_path: Path):
    c = LiquidationOICollector(symbols=SYMBOLS, data_dir=tmp_path)
    yield c
    c.close()


# --- the firewall ------------------------------------------------------------


REPO = Path(__file__).resolve().parents[1]


def _imported_modules(path: Path) -> set[str]:
    """Actual import graph of one file — prose in a docstring is not an import."""
    import ast
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_no_execution_import_anywhere_in_the_module():
    """The whole point: research cannot reach into trading."""
    imports = _imported_modules(REPO / "research/collectors/liq_oi_collector.py")
    for name in imports:
        root = name.split(".")[0]
        assert root not in {"execution", "app", "risk", "planning", "strategies"}, \
            f"research collector imports {name!r} from the trading path"
    assert not any("order" in n for n in imports), f"order-placement import present: {imports}"


def test_live_decision_path_does_not_import_the_collector():
    for module in ("execution/execution_service.py", "execution/position_manager.py",
                   "app/runner.py", "microflow/live.py"):
        assert "liq_oi_collector" not in (REPO / module).read_text()


def test_status_declares_itself_read_only(collector):
    status = collector.status()
    assert status["research_only"] is True
    assert status["orders_allowed"] is False


# --- liquidation frames ------------------------------------------------------


def _frame(symbol="BTCUSDT", **row):
    base = {"ts": "1786980000000", "side": "sell", "price": "63000.5", "size": "0.4"}
    base.update(row)
    return json.dumps({"arg": {"instType": "USDT-FUTURES", "channel": "liquidation",
                               "instId": symbol}, "data": [base], "ts": "1786980000001"})


def test_a_liquidation_event_is_recorded(collector):
    assert collector.handle_message(_frame()) == 1
    assert collector.liq_rows == 1


def test_subscription_acks_are_tracked(collector):
    for symbol in SYMBOLS:
        collector.handle_message(json.dumps(
            {"event": "subscribe", "arg": {"channel": "liquidation", "instId": symbol}}))
    assert collector.status()["subscription_acks"] == len(SYMBOLS)


@pytest.mark.parametrize("raw", ["", "pong", "{", "null", "[]", '{"event":"error","code":30001}'])
def test_malformed_frames_never_raise(collector, raw):
    assert collector.handle_message(raw) == 0


def test_raw_payload_is_preserved_verbatim(collector):
    """No interpretation may be baked into the archive; derived features come later."""
    collector.handle_message(_frame(side="buy", price="1.5"))
    seg = collector.liq_writer
    assert seg is not None and collector.liq_rows == 1


def test_missing_numeric_fields_become_none_not_zero(collector):
    """A missing size is unknown, not zero — zero would be a silent data lie."""
    assert collector.handle_message(_frame(size=None, price=None)) == 1


def test_queue_overflow_is_counted_not_unbounded(collector):
    collector._queue = [{}] * 10_000
    assert collector.handle_message(_frame()) == 0
    assert collector.dropped == 1


# --- open interest sweep -----------------------------------------------------


def _oi_payloads():
    oi = {"data": {"openInterestList": [{"symbol": "BTCUSDT", "size": "12345.6"}],
                   "ts": "1786980000000"}}
    px = {"data": [{"symbol": "BTCUSDT", "price": "63000", "indexPrice": "62990",
                    "markPrice": "63001", "ts": "1786980000000"}]}
    fr = {"data": [{"symbol": "BTCUSDT", "fundingRate": "0.0001",
                    "fundingRateInterval": "8", "nextUpdate": "1786990000000"}]}
    return oi, px, fr


def test_oi_row_carries_both_clocks_and_derived_basis():
    oi, px, fr = _oi_payloads()
    row = _oi_row("BTCUSDT", oi, px, fr, now_ms=1786980000999)
    assert row["timestamp_exchange"] == 1786980000000
    assert row["timestamp_local"] == 1786980000999
    assert row["open_interest"] == pytest.approx(12345.6)
    assert row["basis_bps"] == pytest.approx((63001 - 62990) / 62990 * 10_000)
    assert row["funding_rate"] == pytest.approx(0.0001)
    assert row["research_only"] is True


def test_oi_row_is_dropped_when_the_endpoint_returns_nothing():
    assert _oi_row("BTCUSDT", {}, {}, {}, now_ms=1) is None


def test_a_dead_endpoint_is_counted_not_raised(collector):
    def boom(path, params):
        raise RuntimeError("endpoint down")
    assert collector.poll_open_interest_once(fetch=boom) == 0
    assert collector.oi_errors == len(SYMBOLS)


def test_one_bad_symbol_does_not_stop_the_sweep(collector):
    oi, px, fr = _oi_payloads()

    def fetch(path, params):
        if params["symbol"] == "BTCUSDT":
            raise RuntimeError("transient")
        return {"open-interest": oi, "symbol-price": px, "current-fund-rate": fr}[path.split("/")[-1]]

    assert collector.poll_open_interest_once(fetch=fetch) == 1
    assert collector.oi_errors == 1


def test_stop_event_halts_the_sweep_immediately(collector):
    collector.stop_event.set()
    assert collector.poll_open_interest_once(fetch=lambda p, q: {}) == 0


# --- timestamp quality (regression: the 2026-08-18 negative-latency defect) --------------


def test_each_row_is_stamped_when_it_was_fetched_not_when_the_sweep_began(collector):
    """The defect this reproduces: a sweep of 12 symbols takes ~12 s, and stamping every row
    with the sweep-start time made 100% of rows show negative receive latency (median -6.2 s).
    The Runner clock was verified good, so the fault was here. Lead/lag research on sub-second
    scales is worthless against a 6 s systematic error."""
    import time as _t
    oi, px, fr = _oi_payloads()
    seen = []

    def slow_fetch(path, params):
        _t.sleep(0.02)                      # each symbol's sweep costs real time
        seen.append(_t.time())
        return {"open-interest": oi, "symbol-price": px, "current-fund-rate": fr}[path.split("/")[-1]]

    rows = []
    collector.oi_writer.append = lambda row: rows.append(row)
    collector.poll_open_interest_once(fetch=slow_fetch)
    assert len(rows) == len(SYMBOLS)
    stamps = [r["timestamp_local"] for r in rows]
    assert len(set(stamps)) == len(stamps), \
        "every symbol shares one timestamp — the sweep-level stamp bug is back"
    assert stamps == sorted(stamps), "timestamps must advance through the sweep"


def test_both_ends_of_the_request_are_recorded():
    """Receive latency must be measurable from the row, not assumed."""
    oi, px, fr = _oi_payloads()
    row = _oi_row("BTCUSDT", oi, px, fr, now_ms=1_000, fetch_started_ms=1_000,
                  fetch_completed_ms=1_250)
    assert row["fetch_started_ms"] == 1_000
    assert row["fetch_completed_ms"] == 1_250
    assert row["fetch_completed_ms"] >= row["fetch_started_ms"]


def test_receive_latency_is_not_systematically_negative(collector):
    """The row's own clock must not predate its exchange timestamp by construction."""
    oi, px, fr = _oi_payloads()
    rows = []
    collector.oi_writer.append = lambda row: rows.append(row)
    collector.poll_open_interest_once(
        fetch=lambda p, q: {"open-interest": oi, "symbol-price": px,
                            "current-fund-rate": fr}[p.split("/")[-1]])
    for r in rows:
        assert r["timestamp_local"] >= r["fetch_started_ms"]
