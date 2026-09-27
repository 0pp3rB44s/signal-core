from __future__ import annotations

import ast
import gzip
import json
import threading
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from archiving import common
from archiving.common import (ArchiveWriter, ArchiverConfig, DiskGuardTripped,
                              SourceHealth, backoff_delays, utc_now)
from archiving.funding_archiver import FundingArchiver
from archiving.liquidation_archiver import (LiquidationArchiver,
                                            parse_bybit_liquidations,
                                            parse_force_order,
                                            provider_settings)
from archiving.orderbook_archiver import OrderbookArchiver, build_record


def make_config(tmp_path: Path, **overrides) -> ArchiverConfig:
    env = {"ARCHIVE_DIR": str(tmp_path / "archive"),
           "ARCHIVE_ORDERBOOK_INTERVAL_S": "10",
           "ARCHIVE_MIN_FREE_GB": "0.1"}
    env.update(overrides)
    return ArchiverConfig.from_env(env)


def fixture_orderbook(symbol="BTCUSDT", ts=1789000000000):
    bids = [{"price": 100.0 - i * 0.1, "size": 40.0 if i == 3 else 5.0} for i in range(20)]
    asks = [{"price": 100.1 + i * 0.1, "size": 25.0 if i == 2 else 4.0} for i in range(20)]
    bid_depth = sum(r["price"] * r["size"] for r in bids)
    ask_depth = sum(r["price"] * r["size"] for r in asks)
    return {"symbol": symbol, "bids": bids, "asks": asks,
            "best_bid": 100.0, "best_ask": 100.1, "spread": 0.1,
            "spread_bps": 9.995, "mid_price": 100.05,
            "bid_depth_notional": bid_depth, "ask_depth_notional": ask_depth,
            "total_depth_notional": bid_depth + ask_depth,
            "depth_imbalance": (bid_depth - ask_depth) / (bid_depth + ask_depth),
            "raw_payload": {"ts": str(ts)}}


# --- ArchiveWriter -----------------------------------------------------------

def test_writer_dedupes_and_recovers_after_restart(tmp_path: Path) -> None:
    writer = ArchiveWriter(tmp_path, "orderbook", min_free_gb=0.001)
    assert writer.append({"a": 1}, dedupe_key="k1") is True
    assert writer.append({"a": 2}, dedupe_key="k1") is False
    writer.close()
    # herstart: dedupe-sleutels worden uit het bestaande dagbestand herladen
    resumed = ArchiveWriter(tmp_path, "orderbook", min_free_gb=0.001)
    assert resumed.append({"a": 3}, dedupe_key="k1") is False
    assert resumed.append({"a": 4}, dedupe_key="k2") is True
    resumed.close()
    lines = list((tmp_path / "orderbook").glob("*.jsonl"))[0].read_text().splitlines()
    assert len(lines) == 2


def test_writer_disk_guard_blocks_writes(tmp_path: Path, monkeypatch) -> None:
    writer = ArchiveWriter(tmp_path, "orderbook", min_free_gb=1.0)
    monkeypatch.setattr(common, "disk_free_gb", lambda _p: 0.5)
    monkeypatch.setattr("archiving.common.disk_free_gb", lambda _p: 0.5)
    with pytest.raises(DiskGuardTripped):
        writer.append({"a": 1})


def test_writer_rotation_compression_and_retention(tmp_path: Path) -> None:
    writer = ArchiveWriter(tmp_path, "funding", min_free_gb=0.001)
    old_day = (utc_now() - timedelta(days=200)).strftime("%Y-%m-%d")
    yesterday = (utc_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    (tmp_path / "funding" / f"funding-{old_day}.jsonl").write_text('{"old":1}\n')
    (tmp_path / "funding" / f"funding-{yesterday}.jsonl").write_text('{"y":1}\n')
    writer.append({"today": 1}, dedupe_key="t1")
    stats = writer.compress_and_prune(retention_days=90)
    assert stats == {"compressed": 1, "pruned": 1}
    gz = tmp_path / "funding" / f"funding-{yesterday}.jsonl.gz"
    assert gz.exists()
    assert json.loads(gzip.open(gz).read().decode().strip()) == {"y": 1}
    today = utc_now().strftime("%Y-%m-%d")
    assert (tmp_path / "funding" / f"funding-{today}.jsonl").exists()


# --- config ------------------------------------------------------------------

def test_config_validation_rejects_bad_values(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="INTERVAL"):
        make_config(tmp_path, ARCHIVE_ORDERBOOK_INTERVAL_S="0.1")
    with pytest.raises(ValueError, match="SYMBOLS"):
        make_config(tmp_path, ARCHIVE_SYMBOLS=" , ")
    with pytest.raises(ValueError, match="wss"):
        make_config(tmp_path, ARCHIVE_WS_LIQUIDATION_URL="http://x")
    cfg = make_config(tmp_path)
    assert cfg.symbols[0] == "BTCUSDT" and len(cfg.symbols) == 12


# --- orderbook records -------------------------------------------------------

def test_orderbook_record_fields_and_quality(tmp_path: Path) -> None:
    record = build_record(fixture_orderbook(), "USDT-FUTURES",
                          depth_levels=15, recv_ts_ms=1789000000500)
    assert record["exchange"] == "BITGET" and record["symbol"] == "BTCUSDT"
    assert record["exchange_ts_ms"] == 1789000000000
    assert record["quality"]["status"] == "OK"
    assert record["quality"]["exchange_lag_ms"] == 500
    assert len(record["bids"]) == 15 and len(record["asks"]) == 15
    assert record["best_bid_size"] == 5.0
    assert record["spread_bps"] == pytest.approx(9.995)
    assert record["imbalance"] != 0.0
    assert record["largest_bid_wall"]["ratio"] > 1.0
    assert record["largest_ask_wall"]["ratio"] > 1.0
    assert record["band_bid_notional_bps"]["10"] > 0
    assert record["seq_available"] is False
    # ISO-8601 UTC-timestamp
    assert record["ts_utc"].endswith("+00:00")


def test_orderbook_record_flags_crossed_and_empty(tmp_path: Path) -> None:
    ob = fixture_orderbook()
    ob["best_bid"], ob["best_ask"] = 100.2, 100.1  # crossed
    assert build_record(ob, "USDT-FUTURES", 15, 1789000000500)["quality"]["crossed_book"]
    ob2 = fixture_orderbook()
    ob2["asks"] = []
    rec = build_record(ob2, "USDT-FUTURES", 15, 1789000000500)
    assert rec["quality"]["status"] == "EMPTY" and rec["quality"]["empty_side"]


def test_orderbook_poll_cycle_writes_and_dedupes(tmp_path: Path) -> None:
    config = make_config(tmp_path, ARCHIVE_SYMBOLS="BTCUSDT,ETHUSDT",
                         ARCHIVE_ORDERBOOK_INTERVAL_S="1")
    client = MagicMock()
    client.settings = SimpleNamespace(bitget_product_type="USDT-FUTURES")
    client.get_orderbook = MagicMock(
        side_effect=lambda sym, limit=50: fixture_orderbook(sym))
    archiver = OrderbookArchiver(client, config, threading.Event(), SourceHealth())
    assert archiver.poll_once() == 2
    # zelfde exchange-ts => volledige dedupe in ronde 2 (herstelscenario)
    assert archiver.poll_once() == 0
    archiver.writer.close()
    rows = [json.loads(l) for l in
            list((Path(config.archive_dir) / "orderbook").glob("*.jsonl"))[0]
            .read_text().splitlines()]
    assert {r["symbol"] for r in rows} == {"BTCUSDT", "ETHUSDT"}
    assert all(r["quality"]["status"] == "OK" for r in rows)


def test_orderbook_poll_survives_client_errors(tmp_path: Path) -> None:
    config = make_config(tmp_path, ARCHIVE_SYMBOLS="BTCUSDT,ETHUSDT",
                         ARCHIVE_ORDERBOOK_INTERVAL_S="1")
    client = MagicMock()
    client.settings = SimpleNamespace(bitget_product_type="USDT-FUTURES")
    client.get_orderbook = MagicMock(side_effect=RuntimeError("timeout"))
    health = SourceHealth()
    archiver = OrderbookArchiver(client, config, threading.Event(), health)
    assert archiver.poll_once() == 0
    assert health.consecutive_errors == 2
    assert "timeout" in health.last_error


# --- funding -----------------------------------------------------------------

def test_funding_poll_and_settlement_dedupe(tmp_path: Path) -> None:
    config = make_config(tmp_path, ARCHIVE_SYMBOLS="BTCUSDT",
                         ARCHIVE_FUNDING_INTERVAL_S="60")
    client = MagicMock()
    client.settings = SimpleNamespace(bitget_product_type="USDT-FUTURES")
    client._request = MagicMock(side_effect=[
        {"data": [{"symbol": "BTCUSDT", "fundingRate": "0.0001"}]},
        {"data": [{"fundingTime": "1789000000000", "fundingRate": "0.0002"},
                  {"fundingTime": "1788971200000", "fundingRate": "0.0001"}]},
        {"data": [{"fundingTime": "1789000000000", "fundingRate": "0.0002"},
                  {"fundingTime": "1788971200000", "fundingRate": "0.0001"}]},
    ])
    archiver = FundingArchiver(client, config, threading.Event(), SourceHealth())
    assert archiver.poll_current() == 1
    assert archiver.poll_history() == 2
    assert archiver.poll_history() == 0  # dezelfde settlements -> volledig gededuped
    rows = [json.loads(l) for l in
            list((Path(config.archive_dir) / "funding").glob("*.jsonl"))[0]
            .read_text().splitlines()]
    assert rows[0]["funding_rate"] == 0.0001 and rows[0]["exchange"] == "BITGET"


# --- liquidations ------------------------------------------------------------

FORCE_ORDER = json.dumps({
    "e": "forceOrder", "E": 1789000001000,
    "o": {"s": "BTCUSDT", "S": "SELL", "o": "LIMIT", "q": "0.014",
          "p": "9910", "ap": "9910", "X": "FILLED", "T": 1789000000900}})


def test_parse_force_order_and_malformed_frames() -> None:
    record = parse_force_order(FORCE_ORDER)
    assert record["exchange"] == "BINANCE" and record["symbol"] == "BTCUSDT"
    assert record["side"] == "SELL" and record["qty"] == 0.014
    assert record["notional_usdt"] == pytest.approx(138.74)
    assert record["trade_ts_ms"] == 1789000000900
    assert parse_force_order("{niet json") is None
    assert parse_force_order('{"e":"trade"}') is None
    assert parse_force_order('{"e":"forceOrder","o":{"s":"","T":0}}') is None


BYBIT_LIQ = json.dumps({
    "topic": "allLiquidation.BTCUSDT", "type": "snapshot", "ts": 1789000001000,
    "data": [{"T": 1789000000900, "s": "BTCUSDT", "S": "Sell", "v": "0.5", "p": "64000"},
             {"T": 1789000000950, "s": "BTCUSDT", "S": "Buy", "v": "1.2", "p": "64010"}]})


def test_parse_bybit_liquidations() -> None:
    records = parse_bybit_liquidations(BYBIT_LIQ)
    assert len(records) == 2
    assert records[0]["exchange"] == "BYBIT" and records[0]["symbol"] == "BTCUSDT"
    assert records[0]["side"] == "Sell" and records[0]["notional_usdt"] == 32000.0
    assert records[1]["trade_ts_ms"] == 1789000000950
    assert parse_bybit_liquidations('{"op":"pong"}') is None
    assert parse_bybit_liquidations("{kapot") is None
    # malformed item wordt overgeslagen, valide item blijft
    frame = json.dumps({"topic": "allLiquidation.ETHUSDT", "ts": 1,
                        "data": [{"T": 0, "s": "", "v": "x", "p": "1"},
                                 {"T": 5, "s": "ETHUSDT", "S": "Buy", "v": "2", "p": "3000"}]})
    assert len(parse_bybit_liquidations(frame)) == 1


def test_provider_settings_and_validation(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    bybit = provider_settings("bybit", config)
    assert len(bybit["subscribe"]["args"]) == 12
    assert bybit["subscribe"]["args"][0] == "allLiquidation.BTCUSDT"
    assert bybit["client_ping"] == '{"op": "ping"}'
    binance = provider_settings("binance", config)
    assert binance["subscribe"] is None
    with pytest.raises(ValueError, match="provider"):
        LiquidationArchiver(config, threading.Event(), SourceHealth(),
                            provider="kraken")


def test_liquidation_handle_frame_dedupes_binance(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    archiver = LiquidationArchiver(config, threading.Event(), SourceHealth(),
                                   provider="binance")
    assert archiver.handle_frame(FORCE_ORDER) == 1
    assert archiver.handle_frame(FORCE_ORDER) == 0  # duplicate delivery
    assert archiver.handle_frame("garbage") == 0
    assert archiver.frames_malformed == 1
    archiver.writer.close()


def test_liquidation_bybit_frames_dedupe_and_heartbeat(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    health = SourceHealth()
    archiver = LiquidationArchiver(config, threading.Event(), health,
                                   provider="bybit")
    assert archiver.handle_frame(BYBIT_LIQ) == 2
    assert archiver.handle_frame(BYBIT_LIQ) == 0  # volledige dedupe
    # ack/pong-frames archiveren niets maar bewijzen verbindingsleven
    assert archiver.handle_frame('{"op":"pong"}') == 0
    assert health.last_success_utc is not None
    assert health.extra["provider"] == "bybit"
    archiver.writer.close()
    rows = [json.loads(l) for l in
            list((Path(config.archive_dir) / "liquidations").glob("*.jsonl"))[0]
            .read_text().splitlines()]
    assert len(rows) == 2 and all(r["exchange"] == "BYBIT" for r in rows)


def test_sslopt_requires_verification_with_ca_bundle() -> None:
    import ssl
    from archiving.liquidation_archiver import build_sslopt
    opts = build_sslopt()
    assert opts["cert_reqs"] == ssl.CERT_REQUIRED
    if "ca_certs" in opts:  # certifi aanwezig
        assert Path(opts["ca_certs"]).exists()


def test_backoff_doubles_and_caps() -> None:
    gen = backoff_delays(base=1.0, cap=8.0)
    assert [next(gen) for _ in range(6)] == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


# --- veiligheid: archivering kan geen orders plaatsen ------------------------

def test_archiving_imports_no_execution_code_and_no_order_calls() -> None:
    archiving_dir = Path(__file__).parents[1] / "archiving"
    forbidden_modules = {"execution", "planning", "risk", "strategies",
                         "candidate_lifecycle", "forward_paper", "agents"}
    forbidden_calls = ("place_", "submit_order", "create_order", "cancel_order",
                      "set_leverage", "modify_")
    for path in archiving_dir.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden_modules, \
                    f"{path.name} importeert {node.module}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_modules, \
                        f"{path.name} importeert {alias.name}"
        lowered = source.lower()
        for token in forbidden_calls:
            assert token not in lowered, f"{path.name} bevat verboden call {token}"


def test_archiver_scripts_contain_no_secret_or_order_words() -> None:
    scripts_dir = Path(__file__).parents[1] / "scripts"
    for name in ("start_archiver.sh", "stop_archiver.sh"):
        text = (scripts_dir / name).read_text(encoding="utf-8").lower()
        for token in ("api_key", "secret", "passphrase", "place_order"):
            assert token not in text, f"{name} bevat {token}"


# --- heartbeat/status --------------------------------------------------------

def test_write_status_is_atomic_and_health_lag(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    common.write_status(path, {"x": 1})
    assert json.loads(path.read_text()) == {"x": 1}
    assert not path.with_suffix(".tmp").exists()
    health = SourceHealth()
    assert health.lag_seconds() is None
    health.ok()
    lag = health.lag_seconds()
    assert lag is not None and lag < 5.0
    health.fail(RuntimeError("boom"))
    assert health.consecutive_errors == 1 and "boom" in health.last_error
    health.ok()
    assert health.consecutive_errors == 0
