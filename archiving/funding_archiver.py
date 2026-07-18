from __future__ import annotations

import logging
import threading
import time

from archiving.common import (ArchiveWriter, ArchiverConfig, DiskGuardTripped,
                              SourceHealth, utc_iso)

log = logging.getLogger("archiving.funding")

HISTORY_EVERY_S = 3600.0  # gesettelde funding-historie 1x per uur bijwerken


class FundingArchiver(threading.Thread):
    """Pollt current-fund-rate per symbool en archiveert gesettelde funding.

    - funding:            tijdreeks van actuele funding rates (poll-cadans);
    - funding_settlements: authoritative gesettelde waarden via
      history-fund-rate, gededuplicateerd op (symbool, settle-tijd).
    """

    def __init__(self, client, config: ArchiverConfig, stop_event: threading.Event,
                 health: SourceHealth) -> None:
        super().__init__(name="funding-archiver", daemon=True)
        self.client = client
        self.config = config
        self.stop_event = stop_event
        self.health = health
        self.writer = ArchiveWriter(config.archive_dir, "funding", config.min_free_gb)
        self.settle_writer = ArchiveWriter(config.archive_dir, "funding_settlements",
                                           config.min_free_gb)
        self._last_history = 0.0

    def _product(self) -> str:
        return self.client.settings.bitget_product_type

    def poll_current(self) -> int:
        written = 0
        for symbol in self.config.symbols:
            if self.stop_event.is_set():
                break
            try:
                payload = self.client._request(
                    "GET", "/api/v2/mix/market/current-fund-rate",
                    params={"symbol": symbol, "productType": self._product()})
                data = payload.get("data") or []
                row = data[0] if isinstance(data, list) and data else data
                record = {
                    "ts_utc": utc_iso(),
                    "exchange": "BITGET",
                    "product_type": self._product(),
                    "symbol": symbol,
                    "funding_rate": float(row.get("fundingRate")) if row.get("fundingRate") else None,
                    "raw": row,
                }
                minute_bucket = record["ts_utc"][:16]  # dedupe: max 1 rij/symbool/minuut
                if self.writer.append(record, dedupe_key=f"{symbol}:{minute_bucket}"):
                    written += 1
                self.health.ok(self.writer)
            except DiskGuardTripped as exc:
                self.health.fail(exc)
                log.critical("FUNDING_DISK_GUARD | %s", exc)
                self.stop_event.wait(60)
            except Exception as exc:
                self.health.fail(exc)
                log.warning("FUNDING_POLL_FAILED | symbol=%s | error=%s", symbol, exc)
            self.stop_event.wait(0.25)
        return written

    def poll_history(self) -> int:
        written = 0
        for symbol in self.config.symbols:
            if self.stop_event.is_set():
                break
            try:
                payload = self.client._request(
                    "GET", "/api/v2/mix/market/history-fund-rate",
                    params={"symbol": symbol, "productType": self._product(),
                            "pageSize": "20", "pageNo": "1"})
                for row in payload.get("data") or []:
                    settle_ms = int(row.get("fundingTime") or 0)
                    if not settle_ms:
                        continue
                    record = {
                        "ts_utc_recorded": utc_iso(),
                        "exchange": "BITGET",
                        "product_type": self._product(),
                        "symbol": symbol,
                        "funding_rate": float(row.get("fundingRate")) if row.get("fundingRate") else None,
                        "funding_time_ms": settle_ms,
                    }
                    if self.settle_writer.append(record, dedupe_key=f"{symbol}:{settle_ms}"):
                        written += 1
            except DiskGuardTripped as exc:
                log.critical("FUNDING_SETTLE_DISK_GUARD | %s", exc)
                self.stop_event.wait(60)
            except Exception as exc:
                log.warning("FUNDING_HISTORY_FAILED | symbol=%s | error=%s", symbol, exc)
            self.stop_event.wait(0.25)
        return written

    def run(self) -> None:
        log.info("FUNDING_ARCHIVER_START | symbols=%d | interval_s=%.0f",
                 len(self.config.symbols), self.config.funding_interval_s)
        while not self.stop_event.is_set():
            cycle_start = time.monotonic()
            self.poll_current()
            if time.monotonic() - self._last_history >= HISTORY_EVERY_S:
                self.poll_history()
                self._last_history = time.monotonic()
            elapsed = time.monotonic() - cycle_start
            self.stop_event.wait(max(1.0, self.config.funding_interval_s - elapsed))
        self.writer.close()
        self.settle_writer.close()
        log.info("FUNDING_ARCHIVER_STOP | rows=%d | settlements=%d",
                 self.writer.rows_written, self.settle_writer.rows_written)
