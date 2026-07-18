#!/usr/bin/env python3
"""Supervisor voor de microstructuur-archivering (observe-only, geen orders).

Draait drie onafhankelijke bronnen in threads + een onderhoudslus:
- orderbook (Bitget REST), funding (Bitget REST), liquidations (Binance WS);
- heartbeat naar {ARCHIVE_DIR}/status.json elke 30 s;
- dagelijkse gzip-rotatie + retentie + disk-guard;
- missing-data-detectie: bron zonder succes > 3x interval => DEGRADED-log.

Dit proces importeert geen execution-, planning-, risk- of strategy-code en
kan by design geen orders plaatsen. Alleen publieke endpoints; geen geheimen.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from archiving.common import (ArchiverConfig, SourceHealth, disk_free_gb,
                              utc_iso, write_status)
from archiving.funding_archiver import FundingArchiver
from archiving.liquidation_archiver import LiquidationArchiver
from archiving.orderbook_archiver import OrderbookArchiver

HEARTBEAT_S = 30.0
MAINTENANCE_S = 900.0


def build_logging(archive_dir: Path) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler
    handlers = [logging.StreamHandler(sys.stderr),
                RotatingFileHandler(archive_dir / "archiver.log",
                                    maxBytes=10_000_000, backupCount=3)]
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=handlers)


def main() -> int:
    config = ArchiverConfig.from_env()
    build_logging(config.archive_dir)
    log = logging.getLogger("archiving.supervisor")

    stop_event = threading.Event()
    health = {"orderbook": SourceHealth(), "funding": SourceHealth(),
              "liquidations": SourceHealth()}

    from app.config import Settings
    from clients.bitget_rest import BitgetRestClient
    client = BitgetRestClient(settings=Settings())

    threads: list[threading.Thread] = [
        OrderbookArchiver(client, config, stop_event, health["orderbook"]),
        FundingArchiver(client, config, stop_event, health["funding"]),
    ]
    liq: LiquidationArchiver | None = None
    if config.ws_liquidations_enabled:
        provider = os.environ.get("ARCHIVE_LIQ_PROVIDER", "bybit").lower()
        liq = LiquidationArchiver(config, stop_event, health["liquidations"],
                                  provider=provider)
        threads.append(liq)
    else:
        log.info("LIQUIDATION_ARCHIVER_DISABLED")

    def handle_signal(signum, _frame) -> None:
        log.info("ARCHIVER_SIGNAL | signum=%s | stopping", signum)
        stop_event.set()
        if liq is not None:
            liq.shutdown()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    pid_path = config.archive_dir / "archiver.pid"
    pid_path.write_text(str(os.getpid()))
    log.info("ARCHIVER_START | pid=%d | dir=%s | symbols=%d | ob_interval=%.0fs "
             "| funding_interval=%.0fs | ws=%s | retention_d=%d",
             os.getpid(), config.archive_dir, len(config.symbols),
             config.orderbook_interval_s, config.funding_interval_s,
             config.ws_liquidations_enabled, config.retention_days)

    for thread in threads:
        thread.start()

    stale_after = {"orderbook": 3 * config.orderbook_interval_s,
                   "funding": 3 * config.funding_interval_s,
                   "liquidations": 3600.0}  # liquidaties zijn event-gedreven
    last_maintenance = 0.0
    try:
        while not stop_event.is_set():
            payload = {"updated_utc": utc_iso(), "pid": os.getpid(),
                       "disk_free_gb": round(disk_free_gb(config.archive_dir), 2),
                       "sources": {}}
            for name, h in health.items():
                if name == "liquidations" and not config.ws_liquidations_enabled:
                    payload["sources"][name] = {"status": "DISABLED"}
                    continue
                lag = h.lag_seconds()
                degraded = (lag is None or lag > stale_after[name]
                            or h.consecutive_errors >= 5)
                if degraded and lag is not None and lag > stale_after[name]:
                    logging.getLogger("archiving.supervisor").warning(
                        "SOURCE_STALE | source=%s | lag_s=%.0f", name, lag)
                payload["sources"][name] = {
                    "status": "DEGRADED" if degraded else "OK",
                    "last_success_utc": h.last_success_utc,
                    "lag_seconds": round(lag, 1) if lag is not None else None,
                    "rows_written": h.rows_written,
                    "rows_deduped": h.rows_deduped,
                    "consecutive_errors": h.consecutive_errors,
                    "last_error": h.last_error,
                    **h.extra,
                }
            write_status(config.archive_dir / "status.json", payload)

            if time.monotonic() - last_maintenance >= MAINTENANCE_S:
                for thread in threads:
                    writers = [getattr(thread, "writer", None),
                               getattr(thread, "settle_writer", None)]
                    for writer in writers:
                        if writer is not None:
                            stats = writer.compress_and_prune(config.retention_days)
                            if stats["compressed"] or stats["pruned"]:
                                log.info("ARCHIVE_MAINTENANCE | source=%s | %s",
                                         writer.source, stats)
                last_maintenance = time.monotonic()
            stop_event.wait(HEARTBEAT_S)
    finally:
        stop_event.set()
        if liq is not None:
            liq.shutdown()
        for thread in threads:
            thread.join(timeout=15)
        write_status(config.archive_dir / "status.json",
                     {"updated_utc": utc_iso(), "pid": os.getpid(),
                      "status": "STOPPED"})
        pid_path.unlink(missing_ok=True)
        log.info("ARCHIVER_STOP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
