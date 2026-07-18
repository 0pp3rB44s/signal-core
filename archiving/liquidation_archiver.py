from __future__ import annotations

import json
import logging
import threading
import time

from archiving.common import (ArchiveWriter, ArchiverConfig, DiskGuardTripped,
                              SourceHealth, backoff_delays, utc_iso)

log = logging.getLogger("archiving.liquidation")

STABLE_RESET_S = 300.0   # verbinding >5 min stabiel -> backoff resetten
BYBIT_PING_S = 20.0      # Bybit vereist client-ping (tekstframe) < 20 s

# Providerkeuze (ARCHIVE_LIQ_PROVIDER):
# - "bybit" (default): allLiquidation per symbool, volledige publieke feed.
#   Empirisch geverifieerd 2026-07-18: pushes bereiken dit netwerk
#   (controle-stream publicTrade leverde frames; subscribe-ack success).
# - "binance": !forceOrder@arr. Bitget biedt geen publiek liquidatiekanaal.
#   Op dit netwerk bleken Binance-WS-pushes uit te blijven (REST werkt,
#   subscribe-ack ok, 0 frames op controle-stream) — daarom niet default.
PROVIDERS = ("bybit", "binance")


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
    """Binance USDT-M forceOrder-frame -> archiveringsrecord (of None)."""
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


def parse_bybit_liquidations(frame: str) -> list[dict] | None:
    """Bybit v5 allLiquidation-frame -> records; None = geen liquidatieframe.

    Frame: {"topic":"allLiquidation.BTCUSDT","type":"snapshot","ts":...,
            "data":[{"T":ms,"s":"BTCUSDT","S":"Buy|Sell","v":"qty","p":"prijs"}]}
    S = zijde van de liquidatie-order: Buy = short geliquideerd,
    Sell = long geliquideerd (Bybit v5-conventie).
    """
    try:
        msg = json.loads(frame)
    except json.JSONDecodeError:
        return None
    if not str(msg.get("topic") or "").startswith("allLiquidation."):
        return None
    records: list[dict] = []
    for item in msg.get("data") or []:
        try:
            price = float(item.get("p") or 0.0)
            qty = float(item.get("v") or 0.0)
        except (TypeError, ValueError):
            continue
        symbol = str(item.get("s") or "").upper()
        trade_ts = int(item.get("T") or 0)
        if not symbol or not trade_ts or price <= 0 or qty <= 0:
            continue
        records.append({
            "ts_utc": utc_iso(),
            "exchange": "BYBIT",
            "market": "LINEAR-PERP",
            "channel": "allLiquidation",
            "symbol": symbol,
            "side": str(item.get("S") or ""),    # Sell = long geliquideerd
            "price": price,
            "qty": qty,
            "notional_usdt": round(price * qty, 2),
            "event_ts_ms": int(msg.get("ts") or 0),
            "trade_ts_ms": trade_ts,
        })
    return records


def provider_settings(provider: str, config: ArchiverConfig) -> dict:
    if provider == "bybit":
        return {
            "url": "wss://stream.bybit.com/v5/public/linear",
            "subscribe": {"op": "subscribe",
                          "args": [f"allLiquidation.{s}" for s in config.symbols]},
            "client_ping": json.dumps({"op": "ping"}),
        }
    if provider == "binance":
        return {"url": config.ws_url, "subscribe": None, "client_ping": None}
    raise ValueError(f"onbekende ARCHIVE_LIQ_PROVIDER: {provider}")


class LiquidationArchiver(threading.Thread):
    """WebSocket-archiver met reconnect + exponentiële backoff.

    Verbindings-leven wordt gemeten op elk inkomend frame (incl. pong/ack),
    zodat de healthstatus 'OK' toont zolang de stream leeft, ook als er even
    geen liquidaties zijn (events zijn zeldzaam in rustige markten).
    """

    def __init__(self, config: ArchiverConfig, stop_event: threading.Event,
                 health: SourceHealth, provider: str = "bybit") -> None:
        super().__init__(name="liquidation-archiver", daemon=True)
        if provider not in PROVIDERS:
            raise ValueError(f"onbekende provider: {provider}")
        self.config = config
        self.stop_event = stop_event
        self.health = health
        self.provider = provider
        self.settings = provider_settings(provider, config)
        self.writer = ArchiveWriter(config.archive_dir, "liquidations",
                                    config.min_free_gb)
        self.frames_malformed = 0
        self.events_archived = 0
        self.reconnects = 0
        self._app = None

    def _records_for(self, frame: str) -> list[dict]:
        if self.provider == "bybit":
            records = parse_bybit_liquidations(frame)
            if records is None:
                return []  # ack/pong/overig — telt als verbindingsleven
            return records
        record = parse_force_order(frame)
        if record is None:
            self.frames_malformed += 1  # raw forceOrder-stream: elk frame hoort event te zijn
            return []
        return [record]

    def handle_frame(self, frame: str) -> int:
        """Verwerkt één frame; retourneert aantal gearchiveerde events."""
        # elk inkomend frame bewijst dat de verbinding leeft
        self.health.ok(self.writer, provider=self.provider,
                       events=self.events_archived,
                       malformed=self.frames_malformed,
                       reconnects=self.reconnects)
        written = 0
        for record in self._records_for(frame):
            key = (f"{record['symbol']}:{record['trade_ts_ms']}:"
                   f"{record['qty']}:{record['price']}")
            try:
                if self.writer.append(record, dedupe_key=key):
                    written += 1
            except DiskGuardTripped as exc:
                self.health.fail(exc)
                log.critical("LIQUIDATION_DISK_GUARD | %s", exc)
                return written
        if written:
            self.events_archived += written
            log.info("LIQUIDATION_EVENTS | provider=%s | nieuw=%d | totaal=%d",
                     self.provider, written, self.events_archived)
        return written

    def _ping_loop(self, app) -> None:
        payload = self.settings["client_ping"]
        while not self.stop_event.is_set() and app is self._app:
            try:
                app.send(payload)
            except Exception:
                return  # verbinding weg; run_forever handelt reconnect af
            self.stop_event.wait(BYBIT_PING_S)

    def run(self) -> None:
        import websocket  # websocket-client

        log.info("LIQUIDATION_ARCHIVER_START | provider=%s | url=%s | symbols=%d",
                 self.provider, self.settings["url"], len(self.config.symbols))
        delays = backoff_delays(base=1.0, cap=60.0)
        while not self.stop_event.is_set():
            connected_at = time.monotonic()

            def on_open(ws) -> None:
                if self.settings["subscribe"] is not None:
                    ws.send(json.dumps(self.settings["subscribe"]))
                if self.settings["client_ping"] is not None:
                    threading.Thread(target=self._ping_loop, args=(ws,),
                                     daemon=True).start()
                log.info("LIQUIDATION_WS_OPEN | provider=%s", self.provider)

            def on_message(_ws, message: str) -> None:
                try:
                    self.handle_frame(message)
                except Exception as exc:  # nooit de socketloop laten sneuvelen
                    self.frames_malformed += 1
                    log.warning("LIQUIDATION_FRAME_ERROR | %s", exc)

            def on_error(_ws, error) -> None:
                self.health.fail(error if isinstance(error, BaseException)
                                 else RuntimeError(str(error)))
                log.warning("LIQUIDATION_WS_ERROR | %s", error)

            app = websocket.WebSocketApp(self.settings["url"], on_open=on_open,
                                         on_message=on_message, on_error=on_error)
            self._app = app
            try:
                # Binance pingt server-side (client pongt automatisch);
                # Bybit krijgt tekst-pings via _ping_loop.
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
        log.info("LIQUIDATION_ARCHIVER_STOP | events=%d | malformed=%d | reconnects=%d",
                 self.events_archived, self.frames_malformed, self.reconnects)

    def shutdown(self) -> None:
        if self._app is not None:
            try:
                self._app.close()
            except Exception:
                pass
