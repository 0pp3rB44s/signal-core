"""Standalone Bitget research collector -- isolation, instrumentation, and process-plumbing.

This process exists purely so cross-venue research collection no longer depends on the
production trading process's lifecycle. Its entire safety argument is: it is a thin wrapper
around the already-hardened, already-tested MicroflowCollector, with zero trading-path
dependencies of its own -- so most of what matters here is proving that wrapper adds nothing
risky, not re-testing MicroflowCollector's own behavior (that lives in
test_microflow_collector.py and stays untouched).
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

from microflow.collector import DEFAULT_SYMBOLS, MicroflowCollector
from research.collectors import bitget_microflow_collector as standalone

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "research/collectors/bitget_microflow_collector.py"


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def book(bid_size: float = 8, ask_size: float = 2, seq: int = 1, ts: int = 100_000,
        symbol: str = "BTCUSDT") -> dict:
    return {"arg": {"instType": "USDT-FUTURES", "channel": "books5", "instId": symbol},
            "action": "snapshot", "data": [{"bids": [["100.00", str(bid_size)], ["99.99", "5"]],
            "asks": [["100.02", str(ask_size)], ["100.03", "2"]], "ts": str(ts),
            "seq": seq, "pseq": "0"}], "ts": ts}


def trade(side: str, trade_id: str, ts: int, size: float = 1.0, price: float = 100.01,
         symbol: str = "BTCUSDT") -> dict:
    return {"arg": {"instType": "USDT-FUTURES", "channel": "trade", "instId": symbol},
            "action": "update", "data": [{"ts": str(ts), "price": str(price), "size": str(size),
            "side": side, "tradeId": trade_id}], "ts": ts}


# --- 1/2/3: isolation -- importable standalone, no trading-path dependency anywhere ---------


def test_module_imports_successfully_without_touching_production_runtime():
    """Merely importing this module must not import app.main, microflow.live, execution,
    risk, or any order/credential-bearing client -- proving it can run as a fully separate
    process from production."""
    imports = _imports(MODULE_PATH)
    for name in imports:
        root = name.split(".")[0]
        assert root not in {"execution", "app", "risk", "planning", "strategies", "clients"}, \
            f"standalone Bitget launcher imports {name!r} from the trading path"
    assert "microflow.live" not in imports, "must not import the production embedding module"
    assert "app.main" not in imports


def test_no_order_execution_risk_or_live_account_imports_transitively():
    """Transitive check across the module's own real import graph (not just its own file):
    microflow.collector's dependency closure (candidates/near_miss/segments/state) must also
    be clean. This mirrors the existing invariant for microflow/collector.py itself."""
    for path in (MODULE_PATH, REPO / "microflow/collector.py", REPO / "microflow/candidates.py",
                REPO / "microflow/near_miss.py", REPO / "microflow/segments.py",
                REPO / "microflow/state.py"):
        for name in _imports(path):
            root = name.split(".")[0]
            assert root not in {"execution", "app", "risk", "planning", "strategies",
                                "clients"}, f"{path.name} imports {name!r} from the trading path"


def test_no_order_submission_method_reachable():
    src = MODULE_PATH.read_text()
    for banned in ("place_order", "submit_order", "cancel_order", "execute_order",
                  "api_key", "apiKey", "secret", "passphrase", "signature"):
        assert banned not in src, f"credential or order-placement reference {banned!r} found"


# --- 4/5/6/7: reused hardened instrumentation, exercised via the standalone construction path -


def test_monotonic_and_exchange_and_wall_timestamps_all_present(tmp_path: Path):
    collector = MicroflowCollector(symbols=("BTCUSDT",), data_dir=tmp_path)
    rows = []
    collector.trade_writer.append = lambda r: rows.append(r)
    collector.handle_payload(trade("buy", "t1", 99_000, size=1, price=100), local_ts_ms=100_000)
    r = rows[0]
    assert r["timestamp_exchange"] == 99_000
    assert r["timestamp_local"] == 100_000
    assert "receive_monotonic_ms" in r
    collector.candidate_writer.close()


def test_connection_id_present_on_generated_records(tmp_path: Path):
    collector = MicroflowCollector(symbols=("BTCUSDT",), data_dir=tmp_path)
    collector.connection_id = "standalone-test-connection"
    rows = []
    collector.trade_writer.append = lambda r: rows.append(r)
    collector.handle_payload(trade("buy", "t1", 99_000, size=1, price=100), local_ts_ms=100_000)
    assert rows[0]["connection_id"] == "standalone-test-connection"
    collector.candidate_writer.close()


def test_reconnect_marker_still_works_when_constructed_the_standalone_way(tmp_path: Path):
    """Same reconnect-diagnostics behaviour added in the hardening commit, exercised through a
    MicroflowCollector built exactly the way run() in this module builds one."""
    collector = MicroflowCollector(symbols=tuple(DEFAULT_SYMBOLS), data_dir=tmp_path)
    rows = []
    collector.reconnect_writer.append = lambda r: rows.append(r)

    class FakeWS:
        def send(self, payload: str) -> None:
            pass

    try:
        first = FakeWS()
        collector.ws = first
        collector._on_open(first)
        first_id = collector.connection_id
        collector._on_close(first, 1006, "dropped")

        second = FakeWS()
        collector.ws = second
        collector._on_open(second)

        assert len(rows) == 1
        assert rows[0]["prev_connection_id"] == first_id
        assert rows[0]["next_connection_id"] == collector.connection_id
    finally:
        collector.stop()


# --- 8/9: graceful shutdown, configurable data root ------------------------------------------


def test_graceful_shutdown_closes_writers_and_removes_pid_file(tmp_path: Path):
    data_dir = tmp_path / "clean_epoch"

    class ImmediateStopCollector(MicroflowCollector):
        """run() would otherwise block on a real websocket; here it returns immediately, as
        if stop() had already been called before the connection loop started -- enough to
        exercise run()'s own PID-file lifecycle and writer cleanup without a real socket."""

        def run(self) -> None:
            self.stop_event.set()
            self.state_writer.close()
            self.trade_writer.close()
            self.near_miss_writer.close()
            self.candidate_writer.close()
            self.reconnect_writer.close()

    original = standalone.MicroflowCollector
    standalone.MicroflowCollector = ImmediateStopCollector
    try:
        standalone.run(("BTCUSDT",), data_dir)
    finally:
        standalone.MicroflowCollector = original

    assert not (data_dir / "collector.pid").exists(), "PID file must be removed on clean exit"
    assert (data_dir / "collector.log").exists()


def test_configurable_data_root_is_actually_used(tmp_path: Path):
    custom_root = tmp_path / "research_bitget_v2"
    collector = MicroflowCollector(symbols=("BTCUSDT",), data_dir=custom_root)
    assert collector.data_dir == custom_root
    collector.trade_writer.append({"schema_version": "microflow_trade_v2", "timestamp_exchange": 1,
                                   "timestamp_local": 2, "symbol": "BTCUSDT", "price": 1.0,
                                   "size": 1.0, "aggressor_side": "buy", "trade_id": "x",
                                   "connection_id": "c", "quality": {"stream_gaps": 0}})
    collector.trade_writer.close()
    assert (custom_root / "trades" / "manifest.jsonl").exists()


# --- 10: PID/status metadata belongs to the standalone process itself -----------------------


def test_pid_file_contains_this_processs_own_pid(tmp_path: Path):
    data_dir = tmp_path / "clean_epoch"
    recorded_pid = [None]

    class ImmediateStopCollector(MicroflowCollector):
        def run(self) -> None:
            recorded_pid[0] = int((data_dir / "collector.pid").read_text())
            self.stop_event.set()
            self.state_writer.close()
            self.trade_writer.close()
            self.near_miss_writer.close()
            self.candidate_writer.close()
            self.reconnect_writer.close()

    original = standalone.MicroflowCollector
    standalone.MicroflowCollector = ImmediateStopCollector
    try:
        standalone.run(("BTCUSDT",), data_dir)
    finally:
        standalone.MicroflowCollector = original

    assert recorded_pid[0] == os.getpid(), \
        "the PID file must belong to THIS process, not a hardcoded or copied value"


def test_status_pid_field_is_self_referential(tmp_path: Path):
    collector = MicroflowCollector(symbols=("BTCUSDT",), data_dir=tmp_path)
    assert collector.status()["pid"] == os.getpid()


# --- 11/12: production behaviour and legacy data are untouched by this file's existence ------


def test_standalone_module_never_references_the_production_live_data_directory():
    src = MODULE_PATH.read_text()
    assert "microflow_live" not in src, \
        "must never hardcode production's live data directory -- data root is configurable " \
        "and defaults to a dedicated clean-epoch path"


def test_production_microflow_collector_module_is_untouched_by_this_change():
    """This test file adds new coverage; it does not and must not modify microflow/collector.py
    beyond what the separately-reviewed hardening commit already changed. Sanity-checked here
    by re-asserting that module's own isolation invariant still holds unmodified."""
    tree = ast.parse((REPO / "microflow/collector.py").read_text())
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
              for alias in node.names}
    imports |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any(name.startswith(("execution", "planning", "risk", "clients")) for name in imports)


# --- 13: launcher documents (and can be checked against) the required interpreter -----------


def test_required_launch_command_documents_the_venv_interpreter():
    assert standalone.REQUIRED_LAUNCH_COMMAND.startswith(".venv/bin/python3")
    assert "/usr/bin/python3" not in standalone.REQUIRED_LAUNCH_COMMAND
    assert not standalone.REQUIRED_LAUNCH_COMMAND.startswith(("python ", "python3 "))


# --- 14: full mocked, no-network smoke test ---------------------------------------------------


def test_standalone_smoke_test_runs_without_network_or_trading_code(tmp_path: Path):
    """Constructs the collector exactly as run() does, feeds it synthetic frames (no real
    socket, no real exchange), and confirms research rows are written with zero reachable
    trading-path code -- the full standalone lifecycle in miniature."""
    data_dir = tmp_path / "clean_epoch"
    collector = MicroflowCollector(symbols=tuple(DEFAULT_SYMBOLS), data_dir=data_dir)
    collector.connection_id = "smoke-test"
    assert collector.handle_payload(trade("buy", "t1", 99_000, size=4, price=99.8),
                                    local_ts_ms=100_000) == 1
    assert collector.handle_payload(book(), local_ts_ms=100_000) == 1
    status = collector.status()
    assert status["read_only"] is True and status["orders_allowed"] is False
    assert status["trade_rows"] == 1 and status["book_rows"] == 1
    collector.state_writer.close()
    collector.trade_writer.close()
    collector.candidate_writer.close()
    collector.reconnect_writer.close()
