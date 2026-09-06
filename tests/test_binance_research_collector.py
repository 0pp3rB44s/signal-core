"""Binance research collector — isolation, timestamps, and honest health reporting.

Two properties carry the weight. The collector must be structurally incapable of trading, and it
must not repeat the two data-quality failures this project has already paid for: a sweep-level
timestamp (which made 100% of Bitget OI rows show negative latency) and treating a subscription
acknowledgement as proof of delivery (which is why Bitget's liquidation feed looked healthy for
23 hours while emitting nothing).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from research.collectors.binance_collector import BinanceResearchCollector, _f

REPO = Path(__file__).resolve().parents[1]
SYMBOLS = ("BTCUSDT", "ETHUSDT")


@pytest.fixture()
def c(tmp_path):
    col = BinanceResearchCollector(symbols=SYMBOLS, data_dir=tmp_path)
    yield col
    col.close()


def _agg(symbol="BTCUSDT", E=1_000, a=1, m=False, p="63000.5", q="0.4"):
    return json.dumps({"stream": f"{symbol.lower()}@aggTrade",
                       "data": {"e": "aggTrade", "E": E, "s": symbol, "a": a,
                                "p": p, "q": q, "m": m, "T": E - 5}})


def _book(symbol="BTCUSDT", E=1_500, u=99):
    return json.dumps({"stream": f"{symbol.lower()}@bookTicker",
                       "data": {"e": "bookTicker", "E": E, "u": u, "s": symbol,
                                "b": "63000.0", "B": "1.0", "a": "63000.1", "A": "2.0",
                                "T": E - 1}})


def _force(symbol="BTCUSDT", E=2_000, side="SELL", ap="62000", z="1.5"):
    return json.dumps({"stream": "!forceOrder@arr",
                       "data": {"e": "forceOrder", "E": E,
                                "o": {"s": symbol, "S": side, "o": "LIMIT", "f": "IOC",
                                      "q": "1.5", "p": "62000", "ap": ap, "X": "FILLED",
                                      "l": "1.5", "z": z, "T": E - 3}}})


# --- isolation: the boundary that matters -----------------------------------


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_collector_cannot_reach_the_trading_path():
    for name in _imports(REPO / "research/collectors/binance_collector.py"):
        root = name.split(".")[0]
        assert root not in {"execution", "app", "risk", "planning", "strategies"}, \
            f"binance collector imports {name!r} from the trading path"
        assert "order" not in name.lower(), f"order-placement import: {name}"


def test_live_decision_path_does_not_import_the_collector():
    for module in ("execution/execution_service.py", "execution/position_manager.py",
                   "app/runner.py", "microflow/live.py"):
        assert "binance_collector" not in (REPO / module).read_text()


def test_no_credentials_are_referenced():
    src = (REPO / "research/collectors/binance_collector.py").read_text()
    for banned in ("api_key", "apiKey", "secret", "passphrase", "signature", "X-MBX-APIKEY"):
        assert banned not in src, f"credential reference {banned!r} in a public-data collector"


def test_health_declares_no_order_authority(c):
    h = c.health()
    assert h["research_only"] is True and h["orders_allowed"] is False


# --- payload parsing (shapes observed on the live probe) ---------------------


def test_agg_trade_is_recorded_with_the_aggressor_resolved(c):
    assert c.handle_message(_agg(m=False)) == 1
    assert c.counts["trade"] == 1
    assert c.handle_message(_agg(a=2, E=1_001, m=True)) == 1


def test_buyer_is_maker_means_the_aggressor_was_the_seller(c):
    """Binance flags the BUYER as maker; getting this backwards inverts every flow feature."""
    rows = []
    c.trade_writer.append = lambda r: rows.append(r)
    c.handle_message(_agg(m=True))
    assert rows[0]["aggressor_side"] == "sell"
    rows.clear()
    c.handle_message(_agg(a=9, E=1_009, m=False))
    assert rows[0]["aggressor_side"] == "buy"


def test_book_ticker_and_mark_price_are_recorded(c):
    bt = json.dumps({"data": {"e": "bookTicker", "E": 5, "s": "BTCUSDT", "b": "1", "B": "2",
                              "a": "3", "A": "4", "T": 4}})
    mp = json.dumps({"data": {"e": "markPriceUpdate", "E": 6, "s": "BTCUSDT", "p": "10",
                              "i": "9.9", "r": "0.0001", "T": 99}})
    assert c.handle_message(bt) == 1 and c.handle_message(mp) == 1
    assert c.counts["book"] == 1 and c.counts["mark"] == 1


def test_force_order_is_recorded_raw_with_notional(c):
    rows = []
    c.liq_writer.append = lambda r: rows.append(r)
    assert c.handle_message(_force()) == 1
    assert c.liq_events_total == 1
    assert rows[0]["side"] == "SELL" and rows[0]["order_status"] == "FILLED"
    assert rows[0]["notional"] == pytest.approx(62000 * 1.5)
    assert rows[0]["raw"]["o"]["s"] == "BTCUSDT", "the raw event must be preserved"


@pytest.mark.parametrize("raw", ["", "{", "null", "[]", '{"data":123}', '{"data":{"e":"unknown"}}'])
def test_malformed_payloads_never_raise(c, raw):
    assert c.handle_message(raw) == 0


def test_malformed_payloads_are_counted_not_swallowed_silently(c):
    c.handle_message("{")
    c.handle_message("null")
    assert c.parse_failures >= 2


# --- timestamps: the regression that matters --------------------------------


def test_every_row_carries_all_three_clocks(c):
    rows = []
    c.trade_writer.append = lambda r: rows.append(r)
    c.handle_message(_agg(E=1_234), now_ms=1_500)
    r = rows[0]
    assert r["event_timestamp_ms"] == 1_234
    assert r["receive_timestamp_ms"] == 1_500
    assert r["write_timestamp_ms"] >= 0


def test_rest_rows_are_stamped_per_symbol_not_per_sweep(c):
    """The Bitget defect, prevented here: one sweep timestamp across N symbols produced 100%
    negative receive latency and would invert sub-second lead/lag."""
    import time as _t
    rows = []
    c.oi_writer.append = lambda r: rows.append(r)

    def slow(path, params):
        _t.sleep(0.02)
        return ({"openInterest": "123", "time": int(_t.time() * 1000)}
                if "openInterest" in path
                else {"markPrice": "10", "indexPrice": "9.9", "lastFundingRate": "0.0001",
                      "nextFundingTime": 1, "time": int(_t.time() * 1000)})

    c.poll_open_interest_once(fetch=slow)
    assert len(rows) == len(SYMBOLS)
    assert len({r["fetch_started_ms"] for r in rows}) == len(rows), "sweep-level stamp is back"
    for r in rows:
        assert r["fetch_completed_ms"] >= r["fetch_started_ms"]


def test_negative_latency_is_counted(c):
    c.handle_message(_agg(E=9_999), now_ms=1_000)      # event stamped after receive
    assert c.negative_latency == 1


def test_out_of_order_events_are_detected(c):
    c.handle_message(_agg(a=1, E=5_000))
    c.handle_message(_agg(a=2, E=4_000))
    assert c.out_of_order == 1


def test_duplicate_events_are_suppressed(c):
    assert c.handle_message(_agg(a=7, E=3_000)) == 1
    assert c.handle_message(_agg(a=7, E=3_000)) == 0
    assert c.duplicates == 1


def test_the_dedupe_set_is_bounded(c):
    for i in range(20_050):
        c.handle_message(_agg(a=i, E=10_000 + i))
    assert len(c._seen) <= 20_000


# --- honest health reporting ------------------------------------------------


def test_no_delivery_proven_before_anything_arrives(c):
    assert c.liquidation_status() == "NO_DELIVERY_PROVEN"


def test_healthy_no_event_once_ordinary_streams_flow(c):
    """The Bitget lesson: silence on a live socket is the market, not a broken feed —
    but that is only claimable once another stream on THAT SAME SOCKET proves it works.

    Trades now arrive over REST, so they can no longer vouch for the websocket. Only
    bookTicker, which shares the socket with !forceOrder@arr, can.
    """
    c.handle_message(_agg())
    assert c.liquidation_status() == "NO_DELIVERY_PROVEN", "REST trades must not vouch for the socket"
    c.handle_message(_book())
    assert c.liquidation_status() == "HEALTHY_NO_EVENTS_OBSERVED"


def test_delivery_proven_only_after_a_real_liquidation_frame(c):
    c.handle_message(_book())
    c.handle_message(_force())
    assert c.liquidation_status() == "DELIVERING"
    assert c.health()["liq_events_total"] == 1
    assert c.health()["last_liq_event_age_s"] is not None


def test_health_exposes_every_required_counter(c):
    h = c.health()
    for k in ("rows", "parse_failures", "dropped", "duplicates", "out_of_order",
              "negative_latency_rows", "reconnects", "liq_events_total",
              "liquidation_status", "symbols_expected"):
        assert k in h


# --- stream construction ----------------------------------------------------


def test_stream_path_covers_every_symbol_and_the_all_market_liquidation_feed(c):
    p = c.stream_path()
    for s in SYMBOLS:
        assert f"{s.lower()}@bookTicker" in p
    assert "!forceOrder@arr" in p, "all-market liquidation stream missing"
    # aggTrade and markPrice@1s are acked by the server and never delivered, so they are
    # collected over REST instead. Subscribing to them would only restore a silent stream.
    assert "@aggTrade" not in p
    assert "@markPrice" not in p


def test_a_dead_endpoint_is_counted_not_raised(c):
    def boom(path, params):
        raise RuntimeError("down")
    assert c.poll_open_interest_once(fetch=boom) == 0
    assert c.oi_errors == len(SYMBOLS)


# --- collector hardening: monotonic timestamps, connection id, exception preservation ----
#
# Added for the research-collector-hardening pass. The two MUST_FIX items being tested here
# are: (1) every row also carries a time.monotonic()-based receive timestamp, additive to the
# existing wall/exchange clocks, so a future pipeline can detect wall-clock slew directly
# instead of inferring it; (2) a reconnect's ORIGINAL recv() exception must survive even when
# the ping fallback probe also fails, which previously replaced it with "socket is already
# closed" in 94.9% of the 1,791 historical reconnects analyzed in the data-integrity audit.


def test_monotonic_receive_timestamp_is_written_on_new_ws_records(c):
    rows = []
    c.book_writer.append = lambda r: rows.append(r)
    c.handle_message(_book())
    assert "receive_monotonic_ms" in rows[0]
    assert isinstance(rows[0]["receive_monotonic_ms"], float)


def test_monotonic_timestamp_is_additive_not_a_replacement(c):
    rows = []
    c.book_writer.append = lambda r: rows.append(r)
    c.handle_message(_book(E=1_500), now_ms=1_600)
    r = rows[0]
    # every field the schema had before this patch is still there, unchanged
    assert r["event_timestamp_ms"] == 1_500
    assert r["receive_timestamp_ms"] == 1_600
    assert r["write_timestamp_ms"] >= 0
    # and the new channel is genuinely additional, not a rename
    assert "receive_monotonic_ms" in r and "write_monotonic_ms" in r


def test_monotonic_receive_values_do_not_decrease_within_one_session(c):
    rows = []
    c.book_writer.append = lambda r: rows.append(r)
    for i in range(20):
        c.handle_message(_book(u=100 + i))
    values = [r["receive_monotonic_ms"] for r in rows]
    assert values == sorted(values), "monotonic receive timestamps went backward in one process"


def test_rest_rows_also_carry_monotonic_fetch_timestamps(c):
    import time as _t
    rows = []
    c.mark_writer.append = lambda r: rows.append(r)

    def fetch(path, params):
        return {"markPrice": "10", "indexPrice": "9.9", "lastFundingRate": "0.0001",
                "nextFundingTime": 1, "time": int(_t.time() * 1000)}

    c.poll_mark_once(fetch=fetch)
    assert len(rows) == len(SYMBOLS)
    for r in rows:
        assert "fetch_started_monotonic_ms" in r and "fetch_completed_monotonic_ms" in r
        assert r["fetch_completed_monotonic_ms"] >= r["fetch_started_monotonic_ms"]
        # existing wall-clock fields are untouched
        assert "fetch_started_ms" in r and "fetch_completed_ms" in r


def test_connection_id_is_assigned_and_persisted_on_every_row(c):
    first_id = c.connection_id
    assert first_id
    rows = []
    c.book_writer.append = lambda r: rows.append(r)
    c.handle_message(_book())
    assert rows[0]["connection_id"] == first_id


def test_connection_id_changes_after_a_recorded_reconnect(c):
    first_id = c.connection_id
    record = c.record_reconnect(primary_exception=RuntimeError("boom"))
    assert c.connection_id != first_id
    assert record["prev_connection_id"] == first_id
    assert record["next_connection_id"] == c.connection_id

    second_id = c.connection_id
    record2 = c.record_reconnect(primary_exception=RuntimeError("boom again"))
    assert record2["prev_connection_id"] == second_id
    assert record2["next_connection_id"] == c.connection_id
    assert record2["next_connection_id"] != record["next_connection_id"]


def test_original_recv_exception_survives_even_when_ping_fallback_also_fails(c):
    class DeadSocket:
        def recv(self):
            raise ValueError("original recv failure: connection reset")

        def ping(self):
            raise OSError("socket is already closed.")

    with pytest.raises(ValueError) as excinfo:
        c.recv_once(DeadSocket())
    assert "original recv failure" in str(excinfo.value)
    # the ping fallback's failure is retained too, but as the cause -- never as a replacement
    assert isinstance(excinfo.value.__cause__, OSError)
    assert "socket is already closed" in str(excinfo.value.__cause__)


def test_a_successful_ping_after_a_transient_recv_error_does_not_raise(c):
    class FlakySocket:
        def recv(self):
            raise TimeoutError("temporary read timeout")

        def ping(self):
            return None  # socket is still alive

    c.recv_once(FlakySocket())  # must not raise


def test_record_reconnect_preserves_both_exceptions_separately(c):
    primary = ConnectionResetError("connection reset by peer")
    secondary = OSError("socket is already closed.")
    record = c.record_reconnect(primary_exception=primary, ping_exception=secondary)
    assert record["original_exception_type"] == "ConnectionResetError"
    assert "connection reset by peer" in record["original_exception_message"]
    assert record["ping_fallback_exception_type"] == "OSError"
    assert "socket is already closed" in record["ping_fallback_exception_message"]


def test_reconnect_record_has_disconnect_and_reconnect_timing(c):
    record = c.record_reconnect(primary_exception=RuntimeError("x"),
                                disconnect_wall_ms=1_000, disconnect_monotonic_ms=2_000.0)
    assert record["disconnect_wall_ms"] == 1_000
    assert record["disconnect_monotonic_ms"] == 2_000.0
    assert record["reconnect_start_wall_ms"] >= 0
    assert record["reconnect_start_monotonic_ms"] >= 0


def test_clock_health_exposes_wall_and_monotonic_elapsed(c):
    h = c.health()
    for key in ("wall_elapsed_s", "monotonic_elapsed_s", "wall_monotonic_divergence_s"):
        assert key in h
    assert h["connection_id"] == c.connection_id


def test_legacy_shaped_rows_remain_writable_by_the_segment_writer(c):
    """No reader in this repo re-parses these collectors' own historical segments, so there
    is no reader contract to break -- but the writer itself must still happily accept a
    pre-hardening (v1-shaped) row that lacks every new field, proving the change is additive
    at the storage layer as well as the schema layer."""
    legacy_row = {"schema_version": "research_binance_v1_book", "exchange": "binance-usdm",
                 "symbol": "BTCUSDT", "event_timestamp_ms": 1, "receive_timestamp_ms": 2,
                 "write_timestamp_ms": 3, "raw": {}, "research_only": True}
    c.book_writer.append(legacy_row)  # must not raise


def test_collectors_remain_research_only_after_hardening():
    """Re-assert the isolation invariant after this patch: no execution/order import crept
    in alongside uuid (the only new stdlib import this patch adds)."""
    for name in _imports(REPO / "research/collectors/binance_collector.py"):
        root = name.split(".")[0]
        assert root not in {"execution", "app", "risk", "planning", "strategies"}
        assert "order" not in name.lower()
