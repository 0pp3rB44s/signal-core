"""Delivery and data-integrity contract for the Binance research collector.

Every payload below is a real one captured from Binance on 2026-08-18, not invented.
"""
from __future__ import annotations

import json

import pytest

from research.collectors.binance_collector import BinanceResearchCollector

SYMS = ("BTCUSDT",)

# Captured live from wss://fstream.binance.com/ws/btcusdt@bookTicker
BOOK = {"e": "bookTicker", "u": 11314999834173, "s": "BTCUSDT", "ps": "BTCUSDT",
        "b": "64643.20", "B": "0.576", "a": "64643.30", "A": "40.345",
        "T": 1787075969893, "E": 1787075969894, "st": 1}
# Captured live from GET /fapi/v1/aggTrades?symbol=BTCUSDT
AGG = [{"a": 3410357518, "p": "64631.00", "q": "0.001", "nq": "0.001",
        "f": 7977560142, "l": 7977560142, "T": 1787076014625, "m": True},
       {"a": 3410357519, "p": "64631.10", "q": "0.001", "nq": "0.001",
        "f": 7977560143, "l": 7977560143, "T": 1787076014711, "m": False}]
# Captured live from GET /fapi/v1/premiumIndex?symbol=BTCUSDT
PREMIUM = {"symbol": "BTCUSDT", "markPrice": "64631.71166667", "indexPrice": "64675.07782609",
           "estimatedSettlePrice": "64736.28080229", "lastFundingRate": "0.00002355",
           "interestRate": "0.00010000", "nextFundingTime": 1787097600000, "time": 1787076015001}


@pytest.fixture()
def collector(tmp_path):
    c = BinanceResearchCollector(symbols=SYMS, data_dir=tmp_path)
    yield c
    c.close()


def _rows(collector, name):
    """Read persisted rows. Segments buffer, so finalize before reading them back."""
    for writer in (collector.trade_writer, collector.book_writer, collector.mark_writer):
        try:
            writer.finalize()
        except Exception:
            pass
    out = []
    for path in sorted((collector.data_dir / name / "segments").glob("*.jsonl*")):
        import gzip
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt") as fh:
            out += [json.loads(line) for line in fh if line.strip()]
    return out


# --- delivery ---------------------------------------------------------------

def test_websocket_subscribes_only_to_streams_that_deliver(collector):
    path = collector.stream_path()
    assert "btcusdt@bookTicker" in path
    # These two are acked by the server and never sent; they must not be subscribed.
    assert "aggTrade" not in path
    assert "markPrice" not in path


def test_agg_trades_collected_over_rest(collector):
    written = collector.poll_trades_once(fetch=lambda p, q: AGG)
    assert written == 2
    rows = _rows(collector, "trades")
    assert [r["trade_id"] for r in rows] == [3410357518, 3410357519]
    first = rows[0]
    assert first["price"] == 64631.00 and first["quantity"] == 0.001
    assert first["first_trade_id"] == 7977560142 and first["last_trade_id"] == 7977560142
    assert first["event_timestamp_ms"] == 1787076014625
    # Binance flags the BUYER as maker, so m=True means the aggressor sold.
    assert first["aggressor_side"] == "sell"
    assert rows[1]["aggressor_side"] == "buy"
    assert collector.stream_health("trade") == "HEALTHY"


def test_trade_polling_resumes_from_last_id_so_it_cannot_skip(collector):
    seen = {}

    def fetch(path, params):
        seen.update(params)
        return AGG

    collector.poll_trades_once(fetch=fetch)
    assert "fromId" not in seen           # first sweep has no cursor
    collector.poll_trades_once(fetch=fetch)
    assert seen["fromId"] == 3410357519 + 1


def test_mark_price_collected_over_rest(collector):
    assert collector.poll_mark_once(fetch=lambda p, q: PREMIUM) == 1
    row = _rows(collector, "mark_price")[0]
    assert row["mark_price"] == pytest.approx(64631.71166667)
    assert row["index_price"] == pytest.approx(64675.07782609)
    assert row["funding_rate"] == pytest.approx(0.00002355)
    assert row["next_funding_ms"] == 1787097600000
    assert row["event_timestamp_ms"] == 1787076015001
    assert collector.stream_health("mark") == "HEALTHY"


# --- dedupe -----------------------------------------------------------------

def test_same_millisecond_distinct_update_ids_are_both_kept(collector):
    a = dict(BOOK, u=11314999834173, E=1787075969894)
    b = dict(BOOK, u=11314999834174, E=1787075969894, b="64643.40")
    assert collector.handle_message(json.dumps(a)) == 1
    assert collector.handle_message(json.dumps(b)) == 1
    assert collector.counts["book"] == 2
    assert collector.duplicates == 0
    assert collector.same_ms_distinct_retained == 1


def test_exact_duplicate_update_id_is_suppressed(collector):
    assert collector.handle_message(json.dumps(BOOK)) == 1
    assert collector.handle_message(json.dumps(BOOK)) == 0
    assert collector.counts["book"] == 1
    assert collector.duplicates == 1


def test_dedupe_uses_update_id_not_timestamp(collector):
    """Same id at a different millisecond is still the same event."""
    collector.handle_message(json.dumps(BOOK))
    assert collector.handle_message(json.dumps(dict(BOOK, E=BOOK["E"] + 500))) == 0
    assert collector.duplicates == 1


def test_duplicate_trade_ids_suppressed_across_sweeps(collector):
    collector.poll_trades_once(fetch=lambda p, q: AGG)
    collector.poll_trades_once(fetch=lambda p, q: AGG)   # same rows replayed
    assert collector.counts["trade"] == 2
    assert collector.duplicates == 2


# --- per-stream health isolation --------------------------------------------

def test_book_health_does_not_leak_into_trade_health(collector):
    for i in range(5):
        collector.handle_message(json.dumps(dict(BOOK, u=BOOK["u"] + i)))
    assert collector.stream_health("book") == "HEALTHY"
    # The whole point: a busy book stream must not vouch for a silent trade stream.
    assert collector.stream_health("trade") == "NO_DELIVERY_PROVEN"
    assert collector.stream_health("mark") == "NO_DELIVERY_PROVEN"
    h = collector.health()
    assert h["book_stream_health"] == "HEALTHY"
    assert h["trade_stream_health"] == "NO_DELIVERY_PROVEN"
    assert h["streams"]["trade"]["rows"] == 0


def test_stale_stream_is_not_reported_healthy(collector, monkeypatch):
    collector.handle_message(json.dumps(BOOK))
    assert collector.stream_health("book") == "HEALTHY"
    collector._last_row_ms["book"] -= 120_000
    assert collector.stream_health("book") == "STALE"


def test_liquidation_never_inherits_from_trade_rows(collector):
    collector.poll_trades_once(fetch=lambda p, q: AGG)
    assert collector.counts["trade"] == 2
    # Trades now arrive over REST and say nothing about the websocket carrying forceOrder.
    assert collector.liquidation_status() == "NO_DELIVERY_PROVEN"


def test_liquidation_requires_a_real_frame(collector):
    collector.handle_message(json.dumps(BOOK))
    assert collector.liquidation_status() == "HEALTHY_NO_EVENTS_OBSERVED"
    assert collector.liq_events_total == 0


# --- timestamps -------------------------------------------------------------

def test_raw_exchange_timestamps_are_preserved_not_corrected(collector):
    collector.poll_trades_once(fetch=lambda p, q: AGG)
    row = _rows(collector, "trades")[0]
    assert row["event_timestamp_ms"] == AGG[0]["T"]          # untouched
    assert row["receive_timestamp_ms"] != row["event_timestamp_ms"]
    assert row["fetch_started_ms"] <= row["fetch_completed_ms"]
    assert row["raw"]["a"] == AGG[0]["a"]


def test_rest_rows_are_stamped_per_symbol_not_per_sweep(tmp_path):
    c = BinanceResearchCollector(symbols=("BTCUSDT", "ETHUSDT"), data_dir=tmp_path)
    try:
        import time as _t
        def fetch(path, params):
            _t.sleep(0.02)
            return dict(PREMIUM, symbol=params["symbol"])
        c.poll_mark_once(fetch=fetch)
        stamps = {r["symbol"]: r["fetch_started_ms"] for r in _rows(c, "mark_price")}
        assert len(stamps) == 2
        assert stamps["BTCUSDT"] != stamps["ETHUSDT"]
    finally:
        c.close()


# --- robustness --------------------------------------------------------------

def test_malformed_rows_do_not_stop_the_sweep(collector):
    bad = [{"a": 1}, "not-a-dict", dict(AGG[0])]
    assert collector.poll_trades_once(fetch=lambda p, q: bad) == 1
    assert collector.parse_failures == 2


def test_rest_failure_is_counted_not_raised(collector):
    def boom(path, params):
        raise RuntimeError("network")
    assert collector.poll_trades_once(fetch=boom) == 0
    assert collector.trade_errors == 1
    assert collector.poll_mark_once(fetch=boom) == 0
    assert collector.mark_errors == 1


def test_research_only_flags_present_on_every_class(collector):
    collector.handle_message(json.dumps(BOOK))
    collector.poll_trades_once(fetch=lambda p, q: AGG)
    collector.poll_mark_once(fetch=lambda p, q: PREMIUM)
    for name in ("book_ticker", "trades", "mark_price"):
        rows = _rows(collector, name)
        assert rows and all(r["research_only"] is True for r in rows)
    assert collector.health()["orders_allowed"] is False


# --- endpoint contract ------------------------------------------------------
#
# Every other test injects `fetch`, so a broken endpoint path would sail through them.
# These assert the exact paths and fields we ask Binance for.

def test_trade_sweep_requests_the_agg_trades_endpoint(collector):
    calls = []
    collector.poll_trades_once(fetch=lambda path, params: calls.append((path, params)) or AGG)
    assert calls[0][0] == "/fapi/v1/aggTrades"
    assert calls[0][1]["symbol"] == "BTCUSDT"


def test_mark_sweep_requests_the_premium_index_endpoint(collector):
    calls = []
    collector.poll_mark_once(fetch=lambda path, params: calls.append((path, params)) or PREMIUM)
    assert calls[0][0] == "/fapi/v1/premiumIndex"
    assert calls[0][1]["symbol"] == "BTCUSDT"


def test_mark_row_reads_the_documented_premium_index_fields(collector):
    """Field names are Binance's, not ours; a renamed source field must fail loudly."""
    collector.poll_mark_once(fetch=lambda p, q: PREMIUM)
    row = _rows(collector, "mark_price")[0]
    assert row["mark_price"] == pytest.approx(float(PREMIUM["markPrice"]))
    assert row["estimated_settle"] == pytest.approx(float(PREMIUM["estimatedSettlePrice"]))
    assert row["interest_rate"] == pytest.approx(float(PREMIUM["interestRate"]))
    assert row["raw"]["markPrice"] == PREMIUM["markPrice"]


def test_open_interest_stream_reports_its_own_freshness(collector):
    collector.poll_open_interest_once(
        fetch=lambda p, q: ({"openInterest": "12.5", "time": 1787076015001}
                            if p.endswith("openInterest") else PREMIUM))
    assert collector.counts["oi"] == 1
    assert collector.stream_age_s("oi") is not None
    assert collector.stream_health("oi") == "HEALTHY"


def test_rows_without_a_freshness_clock_are_never_called_healthy(collector):
    """A stream that counts rows but never stamps itself must not coast as HEALTHY."""
    collector.counts["oi"] = 5
    collector._last_row_ms.pop("oi", None)
    assert collector.stream_health("oi") == "UNKNOWN_AGE"
