"""Binance SPOT research collector. Both streams verified to deliver on a live probe before
being wired in: aggTrade 142 frames/12s, bookTicker 183 frames/12s, raw single-stream sockets.
"""
from __future__ import annotations

import gzip
import json

import pytest

from research.collectors.binance_spot_collector import (
    SPOT_SYMBOLS, BinanceSpotResearchCollector,
)

SYMS = ("BTCUSDT",)

# Captured live from wss://stream.binance.com:9443/ws/btcusdt@aggTrade
AGG = {"e": "aggTrade", "E": 1787086910902, "s": "BTCUSDT", "a": 4034876058,
       "p": "64591.75000000", "q": "0.00024000", "f": 6580036635, "l": 6580036635,
       "T": 1787086910902, "m": True, "M": True}
# Captured live from wss://stream.binance.com:9443/ws/btcusdt@bookTicker -- note: NO "e" field.
BOOK = {"u": 98645697497, "s": "BTCUSDT", "b": "64603.99000000", "B": "4.91483000",
        "a": "64604.00000000", "A": "2.16980000"}


@pytest.fixture()
def collector(tmp_path):
    c = BinanceSpotResearchCollector(symbols=SYMS, data_dir=tmp_path)
    yield c
    c.close()


def _rows(collector, name):
    for w in (collector.trade_writer, collector.book_writer):
        w.finalize()
    out = []
    for path in sorted((collector.data_dir / name / "segments").glob("*.jsonl*")):
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt") as fh:
            out += [json.loads(line) for line in fh if line.strip()]
    return out


def test_hypeusdt_excluded_no_spot_listing():
    assert "HYPEUSDT" not in SPOT_SYMBOLS
    assert len(SPOT_SYMBOLS) == 11


def test_stream_path_covers_symbols_no_futures_only_streams(collector):
    p = collector.stream_path()
    parts = p.split("streams=")[1].split("/")
    assert "btcusdt@aggTrade" in parts
    assert "btcusdt@bookTicker" in parts  # exact match: a broken suffix must not slip past `in p`
    # This collector must never touch futures-only concepts.
    assert "forceOrder" not in p and "markPrice" not in p and "depth" not in p


def test_agg_trade_recorded(collector):
    assert collector.handle_message(json.dumps(AGG)) == 1
    row = _rows(collector, "trades")[0]
    assert row["price"] == 64591.75 and row["trade_id"] == 4034876058
    assert row["aggressor_side"] == "sell"  # m=True: buyer is maker, aggressor sold
    assert row["market_type"] == "spot" and row["exchange"] == "binance-spot"
    assert collector.stream_health("trade") == "HEALTHY"


def test_book_ticker_classified_despite_missing_event_field(collector):
    """Spot bookTicker carries no `e` key -- classification must not depend on one."""
    assert collector.handle_message(json.dumps(BOOK)) == 1
    row = _rows(collector, "book_ticker")[0]
    assert row["best_bid"] == 64603.99 and row["best_ask"] == 64604.00
    assert collector.stream_health("book") == "HEALTHY"


def test_book_dedupes_on_update_id(collector):
    assert collector.handle_message(json.dumps(BOOK)) == 1
    assert collector.handle_message(json.dumps(BOOK)) == 0
    assert collector.duplicates == 1


def test_trade_health_independent_of_book_health(collector):
    collector.handle_message(json.dumps(BOOK))
    assert collector.stream_health("book") == "HEALTHY"
    assert collector.stream_health("trade") == "NO_DELIVERY_PROVEN"


def test_malformed_frame_isolated(collector):
    assert collector.handle_message("not json") == 0
    assert collector.parse_failures == 1
    assert collector.handle_message(json.dumps(BOOK)) == 1  # collector still works after


def test_research_only_no_credentials_no_execution_import():
    import ast
    import research.collectors.binance_spot_collector as mod
    src = open(mod.__file__).read()
    tree = ast.parse(src)
    banned = {"execution", "app", "risk", "planning", "strategies"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned, alias.name
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned, node.module
        if isinstance(node, ast.Name) and "order" in node.id.lower():
            raise AssertionError(f"order-related name found: {node.id}")
    assert "api_key" not in src.lower() and "secret" not in src.lower()
