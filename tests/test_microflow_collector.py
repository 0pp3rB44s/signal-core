from __future__ import annotations

import ast
import gzip
import json
from pathlib import Path

import pytest

from microflow.candidates import CandidateEpisodeSampler, FrozenResearchSpec
from microflow.collector import MicroflowCollector, subscription_payload, websocket_ssl_options
from microflow.segments import ImmutableSegmentWriter
from microflow.state import BookMetrics, MicroflowSymbolState, normalized_imbalance


def book(bid_size: float = 8, ask_size: float = 2, seq: int = 1, ts: int = 100_000) -> dict:
    return {"arg": {"instType": "USDT-FUTURES", "channel": "books5", "instId": "BTCUSDT"},
            "action": "snapshot", "data": [{"bids": [["100.00", str(bid_size)], ["99.99", "5"]],
            "asks": [["100.02", str(ask_size)], ["100.03", "2"]], "ts": str(ts),
            "seq": seq, "pseq": "0"}], "ts": ts}


def trade(side: str, trade_id: str, ts: int, size: float = 1.0, price: float = 100.01) -> dict:
    return {"arg": {"instType": "USDT-FUTURES", "channel": "trade", "instId": "BTCUSDT"},
            "action": "update", "data": [{"ts": str(ts), "price": str(price), "size": str(size),
            "side": side, "tradeId": trade_id}], "ts": ts}


def snapshot(now_ms: int, *, ofi: float, book_imbalance: float,
             microprice_edge: float, spread: float = 1.0,
             movement: float = 20.0, sequence_valid: bool = True) -> dict:
    flows = {name: {"ofi": ofi, "realized_range_bps": movement}
             for name in ("1s", "5s", "15s", "30s", "60s")}
    return {"timestamp_local": now_ms, "symbol": "BTCUSDT", "trade_flow": flows,
            "book": {"book_imbalance_top5": book_imbalance, "spread_bps": spread},
            "microprice": {"mid_price": 100.0, "microprice_vs_mid_bps": microprice_edge},
            "freshness": {"trade_stream_age_ms": 10, "book_stream_age_ms": 5,
                          "sequence_valid": sequence_valid}}


def test_normalized_imbalance_and_zero_denominator_unknown():
    assert normalized_imbalance(75, 25) == pytest.approx(0.5)
    assert normalized_imbalance(25, 75) == pytest.approx(-0.5)
    assert normalized_imbalance(0, 0) is None


def test_book_imbalance_microprice_and_long_short_signs():
    bullish = BookMetrics.from_levels([[100, 8]], [[102, 2]])
    bearish = BookMetrics.from_levels([[100, 2]], [[102, 8]])
    assert bullish.book_imbalance_top1 == pytest.approx(0.6)
    assert bullish.microprice == pytest.approx(101.6)
    assert bullish.microprice_edge_bps > 0
    assert bearish.book_imbalance_top1 == pytest.approx(-0.6)
    assert bearish.microprice_edge_bps < 0


def test_crossed_book_and_zero_touch_fail_closed():
    with pytest.raises(ValueError, match="crossed"):
        BookMetrics.from_levels([[102, 1]], [[101, 1]])
    with pytest.raises(ValueError, match="zero denominator"):
        BookMetrics.from_levels([[100, 0]], [[101, 0]])


def test_rolling_windows_and_duplicate_trade_deduplication():
    state = MicroflowSymbolState("BTCUSDT")
    assert state.add_trade(timestamp_ms=100_000, price=100, size=3, side="buy", trade_id="a")
    assert not state.add_trade(timestamp_ms=100_000, price=100, size=3, side="buy", trade_id="a")
    state.add_trade(timestamp_ms=96_000, price=99.8, size=1, side="sell", trade_id="b")
    assert state.flow(100_000, 1_000)["ofi"] == 1.0
    assert state.flow(100_000, 5_000)["ofi"] == pytest.approx(0.5)
    assert state.flow(100_000, 5_000)["realized_range_bps"] > 0
    assert state.duplicate_trades == 1


def test_sequence_and_out_of_order_errors_are_recorded():
    state = MicroflowSymbolState("BTCUSDT")
    state.update_book(exchange_ts_ms=100, seq=10, bids=[[100, 1]], asks=[[101, 1]])
    state.update_book(exchange_ts_ms=101, seq=9, bids=[[100, 1]], asks=[[101, 1]])
    assert state.sequence_valid is False and state.sequence_errors == 1
    state.add_trade(timestamp_ms=200, price=100, size=1, side="buy", trade_id="a")
    state.add_trade(timestamp_ms=199, price=100, size=1, side="sell", trade_id="b")
    assert state.out_of_order_trades == 1


def test_raw_top5_levels_are_retained_and_pseq_gap_is_rejected():
    state = MicroflowSymbolState("BTCUSDT")
    bids = [["100", "2"], ["99", "3"]]
    asks = [["101", "4"], ["102", "5"]]
    state.update_book(exchange_ts_ms=1_000, seq=10, pseq=9, bids=bids, asks=asks)
    snapshot_row = state.snapshot(local_ts_ms=1_000, connection_id="c1")
    assert snapshot_row["book"]["bid_levels"] == [[100.0, 2.0], [99.0, 3.0]]
    assert snapshot_row["book"]["ask_levels"] == [[101.0, 4.0], [102.0, 5.0]]

    state.update_book(exchange_ts_ms=1_100, seq=12, pseq=8, bids=bids, asks=asks)
    assert state.sequence_valid is False
    assert state.sequence_errors == 1

    state = MicroflowSymbolState("BTCUSDT")
    state.update_book(exchange_ts_ms=1_000, seq=10, pseq=0, bids=bids, asks=asks)
    state.update_book(exchange_ts_ms=1_100, seq=11, pseq=0, bids=bids, asks=asks)
    assert state.sequence_valid is True
    assert state.sequence_errors == 0


def test_candidate_persistence_symmetry_stale_feed_and_deduplication():
    sampler = CandidateEpisodeSampler()
    assert sampler.observe(snapshot(1_000, ofi=.3, book_imbalance=.3, microprice_edge=.1)) is None
    assert sampler.observe(snapshot(2_999, ofi=.3, book_imbalance=.3, microprice_edge=.1)) is None
    candidate = sampler.observe(snapshot(3_000, ofi=.3, book_imbalance=.3, microprice_edge=.1))
    assert candidate["side"] == "LONG"
    assert sampler.observe(snapshot(4_000, ofi=.4, book_imbalance=.4, microprice_edge=.2)) is None
    short = CandidateEpisodeSampler()
    short.observe(snapshot(1_000, ofi=-.3, book_imbalance=-.3, microprice_edge=-.1))
    assert short.observe(snapshot(3_000, ofi=-.3, book_imbalance=-.3, microprice_edge=-.1))["side"] == "SHORT"
    stale = snapshot(6_000, ofi=.9, book_imbalance=.9, microprice_edge=1)
    stale["freshness"]["trade_stream_age_ms"] = 1_001
    assert CandidateEpisodeSampler().observe(stale) is None


def test_neutralization_and_cooldown_prevent_duplicate_entry():
    sampler = CandidateEpisodeSampler(FrozenResearchSpec(persistence_ms=1_000, neutralize_ms=1_000,
                                                          cooldown_ms=5_000))
    good = lambda ts: snapshot(ts, ofi=.4, book_imbalance=.4, microprice_edge=.1)
    neutral = lambda ts: snapshot(ts, ofi=0, book_imbalance=0, microprice_edge=0)
    sampler.observe(good(1_000)); assert sampler.observe(good(2_000)) is not None
    sampler.observe(neutral(3_000)); sampler.observe(neutral(4_000))
    assert sampler.observe(good(5_000)) is None
    assert sampler.observe(good(9_000)) is None
    assert sampler.observe(good(10_000)) is not None


def test_collector_parses_trade_and_book_without_order_surface(tmp_path: Path):
    collector = MicroflowCollector(symbols=("BTCUSDT",), data_dir=tmp_path)
    collector.connection_id = "test"
    collector.handle_payload(trade("buy", "t1", 99_000, size=4, price=99.8), local_ts_ms=100_000)
    collector.handle_payload(trade("sell", "t2", 99_500, size=1, price=100), local_ts_ms=100_000)
    assert collector.handle_payload(book(), local_ts_ms=100_000) == 1
    status = collector.status()
    assert status["read_only"] is True and status["orders_allowed"] is False
    assert status["trade_rows"] == 2 and status["book_rows"] == 1
    collector.state_writer.close(); collector.trade_writer.close(); collector.candidate_writer.close()


def test_subscription_is_bitget_native_trade_and_books5_only():
    payload = subscription_payload(("BTCUSDT", "SOLUSDT"))
    assert payload["op"] == "subscribe" and len(payload["args"]) == 4
    assert {row["channel"] for row in payload["args"]} == {"trade", "books5"}
    assert all(row["instType"] == "USDT-FUTURES" for row in payload["args"])


def test_websocket_tls_verification_cannot_be_disabled():
    import ssl
    options = websocket_ssl_options()
    assert options["cert_reqs"] == ssl.CERT_REQUIRED
    if "ca_certs" in options:
        assert Path(options["ca_certs"]).is_file()


def test_immutable_segment_manifest_hash_and_gzip(tmp_path: Path):
    writer = ImmutableSegmentWriter(tmp_path, schema_version="test_v1", symbols=("BTCUSDT",),
                                    max_seconds=1)
    writer.append({"timestamp_local": 1_000, "symbol": "BTCUSDT", "quality": {}})
    writer.append({"timestamp_local": 2_001, "symbol": "BTCUSDT", "quality": {}})
    writer.close()
    manifests = [json.loads(row) for row in (tmp_path / "manifest.jsonl").read_text().splitlines()]
    assert len(manifests) == 2 and all(len(row["sha256"]) == 64 for row in manifests)
    segments = sorted((tmp_path / "segments").glob("*.jsonl.gz"))
    assert len(segments) == 2
    assert json.loads(gzip.open(segments[0], "rt").readline())["symbol"] == "BTCUSDT"
    assert not list((tmp_path / "segments").glob("*.tmp"))


def test_sparse_segment_is_sealed_when_wall_clock_rotation_is_due(tmp_path: Path):
    writer = ImmutableSegmentWriter(tmp_path, schema_version="test_v1", symbols=("BTCUSDT",),
                                    max_seconds=300)
    writer.append({"timestamp_local": 1_000, "symbol": "BTCUSDT", "quality": {}})
    manifest = writer.finalize_if_due(301_000)
    assert manifest is not None and manifest["rows"] == 1
    assert len(list((tmp_path / "segments").glob("*.jsonl.gz"))) == 1


def test_event_driven_trade_silence_is_not_counted_as_stream_gap(tmp_path: Path):
    collector = MicroflowCollector(symbols=("BTCUSDT",), data_dir=tmp_path)
    collector._record_channel_time("BTCUSDT", "trade", 1_000)
    collector._record_channel_time("BTCUSDT", "trade", 20_000)
    assert collector.stream_gaps["BTCUSDT"] == 0
    collector._record_channel_time("BTCUSDT", "books5", 1_000)
    collector._record_channel_time("BTCUSDT", "books5", 3_001)
    assert collector.stream_gaps["BTCUSDT"] == 1


def test_collector_module_cannot_import_execution_or_credentials():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "microflow/collector.py").read_text())
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
               for alias in node.names}
    imports |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any(name.startswith(("execution", "planning", "risk", "clients")) for name in imports)
