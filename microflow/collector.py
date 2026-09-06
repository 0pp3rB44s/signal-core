from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import ssl
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from microflow.candidates import CandidateEpisodeSampler, FrozenResearchSpec
from microflow.near_miss import NearMissTracker, evaluate_gates
from microflow.segments import ImmutableSegmentWriter
from microflow.state import MicroflowSymbolState


BITGET_PUBLIC_WS = "wss://ws.bitget.com/v2/ws/public"
DEFAULT_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT",
    "LINKUSDT", "AVAXUSDT", "SUIUSDT", "HYPEUSDT", "ZECUSDT", "NEARUSDT",
)
log = logging.getLogger("microflow.collector")


def websocket_ssl_options() -> dict:
    options = {"cert_reqs": ssl.CERT_REQUIRED}
    try:
        import certifi
        options["ca_certs"] = certifi.where()
    except ImportError:
        pass
    return options


def subscription_payload(symbols: tuple[str, ...]) -> dict:
    args = []
    for symbol in symbols:
        args.extend((
            {"instType": "USDT-FUTURES", "channel": "trade", "instId": symbol},
            {"instType": "USDT-FUTURES", "channel": "books5", "instId": symbol},
        ))
    return {"op": "subscribe", "args": args}


class MicroflowCollector:
    """Public-data-only Bitget collector. This class has no order API surface."""

    def __init__(self, *, symbols: tuple[str, ...], data_dir: Path,
                 ws_url: str = BITGET_PUBLIC_WS,
                 candidate_callback: Callable[[dict], None] | None = None,
                 snapshot_callback: Callable[[dict], None] | None = None,
                 ping_interval: float = 25.0) -> None:
        if not symbols or len(symbols) > 15:
            raise ValueError("MicroFlow universe must contain 1-15 symbols")
        self.symbols = tuple(symbol.upper() for symbol in symbols)
        self.data_dir = Path(data_dir)
        self.ws_url = ws_url
        self.candidate_callback = candidate_callback
        self.snapshot_callback = snapshot_callback
        self.stop_event = threading.Event()
        self.connection_id = "not-connected"
        self.states = {symbol: MicroflowSymbolState(symbol) for symbol in self.symbols}
        self.samplers = {symbol: CandidateEpisodeSampler() for symbol in self.symbols}
        self.state_writer = ImmutableSegmentWriter(
            self.data_dir / "state", schema_version="microflow_state_v2", symbols=self.symbols
        )
        self.trade_writer = ImmutableSegmentWriter(
            self.data_dir / "trades", schema_version="microflow_trade_v2", symbols=self.symbols
        )
        self.candidate_writer = ImmutableSegmentWriter(
            self.data_dir / "candidates", schema_version="microflow_candidate_v1", symbols=self.symbols
        )
        # Near-miss telemetry. Research-only: nothing downstream of the collector
        # reads it, and disabling it cannot change what the bot trades.
        self.near_miss_writer = ImmutableSegmentWriter(
            self.data_dir / "near_miss", schema_version="microflow_near_miss_v1", symbols=self.symbols
        )
        # New in the collector-hardening pass: a structured gap/reconnect marker, symmetric
        # with the Binance research collector's reconnect_writer. Bitget's WebSocketApp gives
        # real close code/reason directly (unlike Binance's sync client), so this record is
        # actually more complete than the Binance side's for those two fields.
        self.reconnect_writer = ImmutableSegmentWriter(
            self.data_dir / "reconnects", schema_version="microflow_reconnect_v1", symbols=self.symbols
        )
        self.near_miss = NearMissTracker(FrozenResearchSpec())
        self.near_miss_rows = 0
        self.spec = FrozenResearchSpec()
        self.ws = None
        self.ping_interval = ping_interval
        self.ping_thread: threading.Thread | None = None
        #: Set when the connection owning ``ping_thread`` goes away. Each
        #: connection gets its own event so a heartbeat can be stopped without
        #: touching ``stop_event``, which ends the whole process.
        self._conn_stop: threading.Event | None = None
        self.subscription_acks: set[tuple[str, str]] = set()
        self.frames = 0
        self.trade_rows = 0
        self.book_rows = 0
        self.candidate_rows = 0
        self.reconnects = 0
        self.malformed_frames = 0
        self.last_frame_local_ms: int | None = None
        self.stream_gaps = {symbol: 0 for symbol in self.symbols}
        self._last_channel_ts: dict[tuple[str, str], int] = {}
        self._latest_snapshots: dict[str, dict] = {}
        self._snapshot_lock = threading.Lock()
        # Clock-health reference for this process (time.monotonic() vs time.time()),
        # symmetric with the Binance research collector's clock_health().
        self._started_wall_ms = int(time.time() * 1000)
        self._started_monotonic_ms = time.monotonic() * 1000
        # Consumed by _on_open to build one structured reconnect record per disconnect.
        # None until the first _on_close (the very first connection has no "previous" one).
        self._pending_disconnect: dict | None = None
        self._last_error: BaseException | None = None

    def latest_snapshot(self, symbol: str) -> dict | None:
        """Return the latest complete public snapshot for a pre-submit gate."""
        with self._snapshot_lock:
            value = self._latest_snapshots.get(str(symbol).upper())
            return dict(value) if value is not None else None

    def _record_channel_time(self, symbol: str, channel: str, timestamp_ms: int) -> None:
        key = (symbol, channel)
        previous = self._last_channel_ts.get(key)
        # books5 should update continuously; public trades are event-driven and
        # a quiet tape is not itself a transport gap.
        if channel == "books5" and previous is not None and timestamp_ms - previous > 2_000:
            self.stream_gaps[symbol] += 1
        self._last_channel_ts[key] = timestamp_ms

    def handle_payload(self, payload: dict, *, local_ts_ms: int | None = None,
                       monotonic_ts_ms: float | None = None) -> int:
        now_ms = int(local_ts_ms or time.time() * 1000)
        mono_ms = float(monotonic_ts_ms) if monotonic_ts_ms is not None else time.monotonic() * 1000
        self.last_frame_local_ms = now_ms
        self.frames += 1
        event = payload.get("event")
        arg = payload.get("arg") or {}
        if event == "subscribe":
            self.subscription_acks.add((str(arg.get("instId") or ""), str(arg.get("channel") or "")))
            return 0
        if event == "error":
            raise RuntimeError(f"Bitget subscription error code={payload.get('code')} msg={payload.get('msg')}")
        symbol = str(arg.get("instId") or "").upper()
        channel = str(arg.get("channel") or "")
        state = self.states.get(symbol)
        if state is None or channel not in {"trade", "books5"}:
            return 0
        written = 0
        if channel == "trade":
            for row in payload.get("data") or []:
                trade_ts = int(row.get("ts") or 0)
                if not state.add_trade(
                    timestamp_ms=trade_ts,
                    price=float(row.get("price") or 0),
                    size=float(row.get("size") or 0),
                    side=str(row.get("side") or ""),
                    trade_id=str(row.get("tradeId") or ""),
                ):
                    continue
                self._record_channel_time(symbol, channel, trade_ts)
                self.trade_writer.append({
                    "schema_version": "microflow_trade_v2",
                    "timestamp_exchange": trade_ts,
                    "timestamp_local": now_ms,
                    # Additive time.monotonic() channel; timestamp_exchange/timestamp_local
                    # above are unchanged and remain the source of truth for those clocks.
                    "receive_monotonic_ms": mono_ms,
                    "symbol": symbol,
                    "price": float(row["price"]),
                    "size": float(row["size"]),
                    "aggressor_side": str(row["side"]).lower(),
                    "trade_id": str(row.get("tradeId") or ""),
                    "connection_id": self.connection_id,
                    "quality": {"stream_gaps": self.stream_gaps[symbol]},
                })
                self.trade_rows += 1
                written += 1
            return written
        for row in payload.get("data") or []:
            exchange_ts = int(row.get("ts") or payload.get("ts") or 0)
            state.update_book(
                exchange_ts_ms=exchange_ts,
                seq=int(row["seq"]) if row.get("seq") not in (None, "") else None,
                pseq=int(row["pseq"]) if row.get("pseq") not in (None, "") else None,
                bids=row.get("bids") or [],
                asks=row.get("asks") or [],
            )
            self._record_channel_time(symbol, channel, exchange_ts)
            snapshot = state.snapshot(local_ts_ms=now_ms, connection_id=self.connection_id)
            # Additive time.monotonic() channel, added post-hoc rather than threading a new
            # parameter through MicroflowSymbolState.snapshot()'s existing contract.
            snapshot["receive_monotonic_ms"] = mono_ms
            snapshot["quality"]["stream_gaps"] = self.stream_gaps[symbol]
            with self._snapshot_lock:
                self._latest_snapshots[symbol] = snapshot
            self.state_writer.append(snapshot)
            self.book_rows += 1
            written += 1
            candidate = self.samplers[symbol].observe(snapshot)
            self._record_near_miss(snapshot, candidate)
            if self.snapshot_callback is not None:
                try:
                    self.snapshot_callback(dict(snapshot))
                except Exception:
                    log.exception("MICROFLOW_SNAPSHOT_CALLBACK_FAILED | symbol=%s", symbol)
            if candidate:
                candidate["timestamp_local"] = now_ms
                candidate["quality"] = {"stream_gaps": self.stream_gaps[symbol],
                                        "sequence_errors": state.sequence_errors}
                self.candidate_writer.append(candidate)
                self.candidate_rows += 1
                log.info(
                    "MICROFLOW_RESEARCH_CANDIDATE | id=%s | symbol=%s | side=%s | spec=%s",
                    candidate["candidate_id"], symbol, candidate["side"], candidate["spec_hash"],
                )
                if self.candidate_callback is not None:
                    try:
                        self.candidate_callback(dict(candidate))
                    except Exception:
                        log.exception(
                            "MICROFLOW_CANDIDATE_CALLBACK_FAILED | id=%s | symbol=%s",
                            candidate["candidate_id"], symbol,
                        )
        return written

    def _record_near_miss(self, snapshot: dict, candidate: dict | None) -> None:
        """Observe the episode. Never allowed to affect trading.

        Wrapped whole: a telemetry bug must not be able to stop the collector or
        change an entry decision, so any failure is logged and swallowed.
        """
        try:
            evaluation = evaluate_gates(snapshot, self.spec)
            rows = self.near_miss.observe(
                snapshot, evaluation,
                candidate_id=(candidate or {}).get("candidate_id"),
            )
            for row in rows:
                self.near_miss_writer.append(row)
                self.near_miss_rows += 1
        except Exception:
            log.exception("MICROFLOW_NEAR_MISS_FAILED | symbol=%s", snapshot.get("symbol"))

    def handle_message(self, raw: str) -> int:
        if raw == "pong":
            self.last_frame_local_ms = int(time.time() * 1000)
            return 0
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("frame is not an object")
            return self.handle_payload(payload)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.malformed_frames += 1
            log.warning("MICROFLOW_FRAME_REJECTED | error=%s", exc)
            return 0

    def clock_health(self) -> dict:
        """Wall-clock vs monotonic-clock divergence since process start.

        Symmetric with the Binance research collector's clock_health(): time.monotonic()
        never slews or steps, so any growth in this divergence is direct, self-contained
        proof of a wall-clock adjustment on this machine, independent of trusting Bitget's
        own clock.
        """
        now_mono_ms = time.monotonic() * 1000
        wall_elapsed_s = (int(time.time() * 1000) - self._started_wall_ms) / 1000.0
        monotonic_elapsed_s = (now_mono_ms - self._started_monotonic_ms) / 1000.0
        return {
            "wall_elapsed_s": wall_elapsed_s,
            "monotonic_elapsed_s": monotonic_elapsed_s,
            "wall_monotonic_divergence_s": wall_elapsed_s - monotonic_elapsed_s,
        }

    def status(self) -> dict:
        now_ms = int(time.time() * 1000)
        return {
            "schema_version": "microflow_collector_status_v1",
            "updated_ms": now_ms,
            "pid": os.getpid(),
            "read_only": True,
            "orders_allowed": False,
            **self.clock_health(),
            "ws_url": self.ws_url,
            "book_channel": "books5",
            "trade_channel": "trade",
            "symbols": list(self.symbols),
            "connection_id": self.connection_id,
            "subscription_acks": len(self.subscription_acks),
            "expected_subscription_acks": len(self.symbols) * 2,
            "last_frame_age_ms": now_ms - self.last_frame_local_ms if self.last_frame_local_ms else None,
            "frames": self.frames,
            "trade_rows": self.trade_rows,
            "book_rows": self.book_rows,
            "candidate_rows": self.candidate_rows,
            "near_miss_rows": self.near_miss_rows,
            "reconnects": self.reconnects,
            "malformed_frames": self.malformed_frames,
            "stream_gaps": self.stream_gaps,
            "spec_id": FrozenResearchSpec().spec_id,
            "spec_hash": FrozenResearchSpec().digest(),
        }

    def _write_status(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.data_dir / "status.json.tmp"
        tmp.write_text(json.dumps(self.status(), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.data_dir / "status.json")

    def _ping_loop(self, ws, conn_stop: threading.Event) -> None:
        """Heartbeat for exactly one connection.

        The socket is a parameter, never ``self.ws``. A thread that outlives
        its connection therefore cannot reach the socket that replaced it --
        which is what previously let N reconnects put N heartbeats on one live
        connection and earn Bitget's 30007 request-over-limit.
        """
        while not conn_stop.wait(self.ping_interval):
            if self.stop_event.is_set() or ws is not self.ws:
                return  # superseded or shutting down: this connection is done
            try:
                ws.send("ping")
            except Exception as exc:
                # Fail closed: a socket that cannot be pinged is not ours to
                # retry on. run_forever will observe the close and reconnect.
                log.warning("MICROFLOW_PING_FAILED | error=%s", exc)
                conn_stop.set()
                return

    def _stop_active_heartbeat(self) -> None:
        """Retire the current heartbeat and wait for the thread to leave."""
        conn_stop, thread = self._conn_stop, self.ping_thread
        self._conn_stop, self.ping_thread = None, None
        if conn_stop is not None:
            conn_stop.set()
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def _record_reconnect_if_pending(self, next_connection_id: str) -> None:
        """Consume the disconnect info _on_close staged and write one structured record.

        Nothing is written for the very first connection (no prior disconnect exists).
        Bitget's WebSocketApp gives real close code/reason directly here, unlike the
        Binance sync client's exception model, so this record is more complete than the
        Binance side's for those two fields -- reported as-is, not padded to look symmetric.
        """
        pending = self._pending_disconnect
        if pending is None:
            return
        self._pending_disconnect = None
        record = {
            "schema_version": "microflow_reconnect_v1",
            "exchange": "bitget",
            "event_type": "reconnect",
            "prev_connection_id": pending["prev_connection_id"],
            "next_connection_id": next_connection_id,
            "disconnect_wall_ms": pending["disconnect_wall_ms"],
            "disconnect_monotonic_ms": pending["disconnect_monotonic_ms"],
            "reconnect_start_wall_ms": int(time.time() * 1000),
            "reconnect_start_monotonic_ms": time.monotonic() * 1000,
            "close_code": pending["close_code"],
            "close_reason": pending["close_reason"],
            "last_error_type": pending["last_error_type"],
            "last_error_message": pending["last_error_message"],
            "write_timestamp_ms": int(time.time() * 1000),
            "research_only": True,
        }
        try:
            self.reconnect_writer.append(record)
        except Exception:
            log.exception("MICROFLOW_RECONNECT_RECORD_WRITE_FAILED")

    def _on_open(self, ws) -> None:
        # Defensive: a previous connection must never outlive this one, even if
        # its close callback was skipped.
        self._stop_active_heartbeat()
        new_connection_id = str(uuid.uuid4())
        self._record_reconnect_if_pending(new_connection_id)
        self.connection_id = new_connection_id
        self.subscription_acks.clear()
        try:
            ws.send(json.dumps(subscription_payload(self.symbols), separators=(",", ":")))
        except Exception as exc:
            # No subscription means no data; do not leave a heartbeat behind
            # holding a socket the collector is not actually using.
            log.warning("MICROFLOW_SUBSCRIBE_FAILED | error=%s", exc)
            try:
                ws.close()
            except Exception:
                pass
            return
        log.info("MICROFLOW_WS_OPEN | connection_id=%s | channels=%d",
                 self.connection_id, len(self.symbols) * 2)
        conn_stop = threading.Event()
        thread = threading.Thread(
            target=self._ping_loop, args=(ws, conn_stop), daemon=True,
            name=f"microflow-ping-{self.connection_id[:8]}",
        )
        self._conn_stop, self.ping_thread = conn_stop, thread
        thread.start()

    def _on_message(self, _ws, message: str) -> None:
        self.handle_message(message)
        if self.frames % 250 == 0:
            for writer in (self.state_writer, self.trade_writer, self.candidate_writer):
                writer.finalize_if_due()
            self._write_status()

    def _on_error(self, _ws, error) -> None:
        log.warning("MICROFLOW_WS_ERROR | error=%s", error)
        # Consumed (and cleared) by the next _on_close. Unlike the Binance recv/ping path,
        # there is no secondary-failure step here that could overwrite this -- websocket-
        # client's run_forever() calls on_error with whatever broke the read loop directly.
        self._last_error = error

    def _on_close(self, _ws, code, reason) -> None:
        log.warning("MICROFLOW_WS_CLOSE | code=%s | reason=%s", code, reason)
        self._stop_active_heartbeat()
        self._pending_disconnect = {
            "prev_connection_id": self.connection_id,
            "disconnect_wall_ms": int(time.time() * 1000),
            "disconnect_monotonic_ms": time.monotonic() * 1000,
            "close_code": code,
            "close_reason": reason,
            "last_error_type": type(self._last_error).__name__ if self._last_error else None,
            "last_error_message": str(self._last_error) if self._last_error else None,
        }
        self._last_error = None

    def stop(self) -> None:
        self.stop_event.set()
        self._stop_active_heartbeat()
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass

    def run(self) -> None:
        import websocket
        delays = (1, 2, 4, 8, 15, 30)
        attempt = 0
        while not self.stop_event.is_set():
            self.ws = websocket.WebSocketApp(
                self.ws_url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            self.ws.run_forever(sslopt=websocket_ssl_options())
            if self.stop_event.is_set():
                break
            self.reconnects += 1
            self._write_status()
            self.stop_event.wait(delays[min(attempt, len(delays) - 1)])
            attempt += 1
        self.state_writer.close()
        self.trade_writer.close()
        self.near_miss_writer.close()
        self.candidate_writer.close()
        self.reconnect_writer.close()
        self._write_status()


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Bitget MicroFlow collector")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    args = parser.parse_args()
    symbols = tuple(value.strip().upper() for value in args.symbols.split(",") if value.strip())
    args.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(args.data_dir / "collector.log")],
    )
    collector = MicroflowCollector(symbols=symbols, data_dir=args.data_dir)
    signal.signal(signal.SIGTERM, lambda *_: collector.stop())
    signal.signal(signal.SIGINT, lambda *_: collector.stop())
    (args.data_dir / "collector.pid").write_text(str(os.getpid()), encoding="utf-8")
    try:
        collector.run()
    finally:
        (args.data_dir / "collector.pid").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
