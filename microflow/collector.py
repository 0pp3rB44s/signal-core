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

from microflow.candidates import CandidateEpisodeSampler, FrozenResearchSpec
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
                 ws_url: str = BITGET_PUBLIC_WS) -> None:
        if not symbols or len(symbols) > 15:
            raise ValueError("MicroFlow universe must contain 1-15 symbols")
        self.symbols = tuple(symbol.upper() for symbol in symbols)
        self.data_dir = Path(data_dir)
        self.ws_url = ws_url
        self.stop_event = threading.Event()
        self.connection_id = "not-connected"
        self.states = {symbol: MicroflowSymbolState(symbol) for symbol in self.symbols}
        self.samplers = {symbol: CandidateEpisodeSampler() for symbol in self.symbols}
        self.state_writer = ImmutableSegmentWriter(
            self.data_dir / "state", schema_version="microflow_state_v1", symbols=self.symbols
        )
        self.trade_writer = ImmutableSegmentWriter(
            self.data_dir / "trades", schema_version="microflow_trade_v1", symbols=self.symbols
        )
        self.candidate_writer = ImmutableSegmentWriter(
            self.data_dir / "candidates", schema_version="microflow_candidate_v1", symbols=self.symbols
        )
        self.ws = None
        self.ping_thread: threading.Thread | None = None
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

    def _record_channel_time(self, symbol: str, channel: str, timestamp_ms: int) -> None:
        key = (symbol, channel)
        previous = self._last_channel_ts.get(key)
        # books5 should update continuously; public trades are event-driven and
        # a quiet tape is not itself a transport gap.
        if channel == "books5" and previous is not None and timestamp_ms - previous > 2_000:
            self.stream_gaps[symbol] += 1
        self._last_channel_ts[key] = timestamp_ms

    def handle_payload(self, payload: dict, *, local_ts_ms: int | None = None) -> int:
        now_ms = int(local_ts_ms or time.time() * 1000)
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
                    "schema_version": "microflow_trade_v1",
                    "timestamp_exchange": trade_ts,
                    "timestamp_local": now_ms,
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
            snapshot["quality"]["stream_gaps"] = self.stream_gaps[symbol]
            self.state_writer.append(snapshot)
            self.book_rows += 1
            written += 1
            candidate = self.samplers[symbol].observe(snapshot)
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
        return written

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

    def status(self) -> dict:
        now_ms = int(time.time() * 1000)
        return {
            "schema_version": "microflow_collector_status_v1",
            "updated_ms": now_ms,
            "pid": os.getpid(),
            "read_only": True,
            "orders_allowed": False,
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

    def _ping_loop(self) -> None:
        while not self.stop_event.wait(25):
            try:
                if self.ws is not None:
                    self.ws.send("ping")
            except Exception as exc:
                log.warning("MICROFLOW_PING_FAILED | error=%s", exc)

    def _on_open(self, ws) -> None:
        self.connection_id = str(uuid.uuid4())
        self.subscription_acks.clear()
        ws.send(json.dumps(subscription_payload(self.symbols), separators=(",", ":")))
        log.info("MICROFLOW_WS_OPEN | connection_id=%s | channels=%d",
                 self.connection_id, len(self.symbols) * 2)
        self.ping_thread = threading.Thread(target=self._ping_loop, daemon=True)
        self.ping_thread.start()

    def _on_message(self, _ws, message: str) -> None:
        self.handle_message(message)
        if self.frames % 250 == 0:
            for writer in (self.state_writer, self.trade_writer, self.candidate_writer):
                writer.finalize_if_due()
            self._write_status()

    def _on_error(self, _ws, error) -> None:
        log.warning("MICROFLOW_WS_ERROR | error=%s", error)

    def _on_close(self, _ws, code, reason) -> None:
        log.warning("MICROFLOW_WS_CLOSE | code=%s | reason=%s", code, reason)

    def stop(self) -> None:
        self.stop_event.set()
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
        self.candidate_writer.close()
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
