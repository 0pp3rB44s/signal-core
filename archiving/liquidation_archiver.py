from __future__ import annotations

import json
import logging
import threading
import time

from archiving.common import (ArchiveWriter, ArchiverConfig, DiskGuardTripped,
                              SourceHealth, backoff_delays, utc_iso)

log = logging.getLogger("archiving.liquidation")

STABLE_RESET_S = 300.0  # verbinding >5 min stabiel -> backoff resetten


def build_sslopt() -> dict:
    """TLS-opties voor websocket-client: verificatie AAN, certifi-CA-bundel.

    De kale OpenSSL-store is op macOS-Python vaak leeg, waardoor elke keten
    als 'self-signed' faalt; certifi (al aanwezig via requests) levert de
    publieke CA-bundel. Verificatie wordt nooit uitgeschakeld.
    """
    import ssl
    opts: dict = {"cert_reqs": ssl.CERT_REQUIRED}
    try:
        import certifi
        opts["ca_certs"] = certifi.where()
    except ImportError:
        pass  # systeem-store als fallback; verificatie blijft vereist
    return opts


def parse_force_order(frame: str) -> dict | None:
    """Parseert één Binance USDT-M forceOrder-frame naar een archiveringsrecord.

    Bitget biedt geen publiek liquidatiekanaal (v2 public channels: tickers,
    candlesticks, order book, trades — gecontroleerd 2026-07-18). Deze bron is
    daarom Binance USDT-M, expliciet gelabeld; geen Bitget-native claim.
    Retourneert None voor niet-forceOrder- of malformed frames.
    """
    try:
        msg = json.loads(frame)
    except json.JSONDecodeError:
        return None
    if msg.get("e") != "forceOrder":
        return None
    o = msg.get("o") or {}
    try:
        price = float(o.get("p") or 0.0)
        avg_price = float(o.get("ap") or 0.0)
        qty = float(o.get("q") or 0.0)
    except (TypeError, ValueError):
        return None
    symbol = str(o.get("s") or "").upper()
    trade_ts = int(o.get("T") or 0)
    if not symbol or not trade_ts:
        return None
    fill_price = avg_price or price
    return {
        "ts_utc": utc_iso(),
        "exchange": "BINANCE",
        "market": "USDT-M-FUTURES",
        "channel": "forceOrder",
        "symbol": symbol,
        "side": str(o.get("S") or ""),           # SELL = long geliquideerd
        "order_type": str(o.get("o") or ""),
        "status": str(o.get("X") or ""),
        "price": price,
        "avg_price": avg_price,
        "qty": qty,
        "notional_usdt": round(fill_price * qty, 2),
        "event_ts_ms": int(msg.get("E") or 0),
        "trade_ts_ms": trade_ts,
    }


class LiquidationArchiver(threading.Thread):
    """WebSocket-archiver met reconnect + exponentiële backoff.

    De stream-URL bevat het kanaal (!forceOrder@arr): geen subscribe-bericht
    nodig. websocket-client beantwoordt server-pings automatisch met pongs.
    """

    def __init__(self, config: ArchiverConfig, stop_event: threading.Event,
                 health: SourceHealth) -> None:
        super().__init__(name="liquidation-archiver", daemon=True)
        self.config = config
        self.stop_event = stop_event
        self.health = health
        self.writer = ArchiveWriter(config.archive_dir, "liquidations",
                                    config.min_free_gb)
        self.frames_malformed = 0
        self.reconnects = 0
        self._app = None

    def handle_frame(self, frame: str) -> bool:
        record = parse_force_order(frame)
        if record is None:
            self.frames_malformed += 1
            return False
        key = (f"{record['symbol']}:{record['trade_ts_ms']}:"
               f"{record['qty']}:{record['price']}")
        try:
            written = self.writer.append(record, dedupe_key=key)
        except DiskGuardTripped as exc:
            self.health.fail(exc)
            log.critical("LIQUIDATION_DISK_GUARD | %s", exc)
            return False
        if written:
            self.health.ok(self.writer, malformed=self.frames_malformed,
                           reconnects=self.reconnects)
        return written

    def run(self) -> None:
        import websocket  # websocket-client

        log.info("LIQUIDATION_ARCHIVER_START | url=%s", self.config.ws_url)
        delays = backoff_delays(base=1.0, cap=60.0)
        while not self.stop_event.is_set():
            connected_at = time.monotonic()

            def on_message(_ws, message: str) -> None:
                self.handle_frame(message)

            def on_error(_ws, error) -> None:
                self.health.fail(error if isinstance(error, BaseException)
                                 else RuntimeError(str(error)))
                log.warning("LIQUIDATION_WS_ERROR | %s", error)

            app = websocket.WebSocketApp(self.config.ws_url,
                                         on_message=on_message, on_error=on_error)
            self._app = app
            try:
                # server pingt; client pongt automatisch
                app.run_forever(ping_interval=0, sslopt=build_sslopt())
            except Exception as exc:
                self.health.fail(exc)
                log.warning("LIQUIDATION_WS_CRASH | %s", exc)
            if self.stop_event.is_set():
                break
            self.reconnects += 1
            if time.monotonic() - connected_at >= STABLE_RESET_S:
                delays = backoff_delays(base=1.0, cap=60.0)
            delay = next(delays)
            log.info("LIQUIDATION_WS_RECONNECT | attempt=%d | sleep_s=%.0f",
                     self.reconnects, delay)
            self.stop_event.wait(delay)
        self.writer.close()
        log.info("LIQUIDATION_ARCHIVER_STOP | rows=%d | malformed=%d | reconnects=%d",
                 self.writer.rows_written, self.frames_malformed, self.reconnects)

    def shutdown(self) -> None:
        if self._app is not None:
            try:
                self._app.close()
            except Exception:
                pass
