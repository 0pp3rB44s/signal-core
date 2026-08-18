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
    but that is only claimable once another stream proves the socket works."""
    c.handle_message(_agg())
    assert c.liquidation_status() == "HEALTHY_NO_EVENT_OBSERVED"


def test_delivery_proven_only_after_a_real_liquidation_frame(c):
    c.handle_message(_agg())
    c.handle_message(_force())
    assert c.liquidation_status() == "DELIVERY_PROVEN"
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
        for suffix in ("@aggTrade", "@bookTicker", "@markPrice@1s"):
            assert f"{s.lower()}{suffix}" in p
    assert "!forceOrder@arr" in p, "all-market liquidation stream missing"


def test_a_dead_endpoint_is_counted_not_raised(c):
    def boom(path, params):
        raise RuntimeError("down")
    assert c.poll_open_interest_once(fetch=boom) == 0
    assert c.oi_errors == len(SYMBOLS)
