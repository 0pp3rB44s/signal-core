from __future__ import annotations

import gzip
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("archiving")

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
                   "LINKUSDT", "AVAXUSDT", "ADAUSDT", "SUIUSDT", "LTCUSDT", "DOTUSDT"]
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.jsonl(\.gz)?$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat(timespec="milliseconds")


def disk_free_gb(path: Path) -> float:
    probe = path if path.exists() else path.parent
    return shutil.disk_usage(probe).free / 1e9


@dataclass(frozen=True)
class ArchiverConfig:
    archive_dir: Path
    symbols: list[str]
    orderbook_interval_s: float
    funding_interval_s: float
    retention_days: int
    min_free_gb: float
    ws_liquidations_enabled: bool
    ws_url: str
    depth_levels_stored: int

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ArchiverConfig":
        e = os.environ if env is None else env
        symbols = [s.strip().upper() for s in
                   e.get("ARCHIVE_SYMBOLS", ",".join(DEFAULT_SYMBOLS)).split(",") if s.strip()]
        cfg = cls(
            archive_dir=Path(e.get("ARCHIVE_DIR", "data/archive")),
            symbols=symbols,
            orderbook_interval_s=float(e.get("ARCHIVE_ORDERBOOK_INTERVAL_S", "10")),
            funding_interval_s=float(e.get("ARCHIVE_FUNDING_INTERVAL_S", "300")),
            retention_days=int(e.get("ARCHIVE_RETENTION_DAYS", "90")),
            min_free_gb=float(e.get("ARCHIVE_MIN_FREE_GB", "2.0")),
            ws_liquidations_enabled=e.get("ARCHIVE_WS_LIQUIDATIONS", "true").lower() == "true",
            ws_url=e.get("ARCHIVE_WS_LIQUIDATION_URL",
                         "wss://fstream.binance.com/ws/!forceOrder@arr"),
            depth_levels_stored=int(e.get("ARCHIVE_DEPTH_LEVELS", "15")),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.symbols:
            raise ValueError("ARCHIVE_SYMBOLS is leeg")
        if any(not s.isalnum() for s in self.symbols):
            raise ValueError(f"ongeldig symbool in ARCHIVE_SYMBOLS: {self.symbols}")
        if not 1.0 <= self.orderbook_interval_s <= 3600:
            raise ValueError("ARCHIVE_ORDERBOOK_INTERVAL_S buiten [1, 3600]")
        if not 30.0 <= self.funding_interval_s <= 86400:
            raise ValueError("ARCHIVE_FUNDING_INTERVAL_S buiten [30, 86400]")
        if not 1 <= self.retention_days <= 3650:
            raise ValueError("ARCHIVE_RETENTION_DAYS buiten [1, 3650]")
        if self.min_free_gb < 0.1:
            raise ValueError("ARCHIVE_MIN_FREE_GB < 0.1")
        if not 1 <= self.depth_levels_stored <= 50:
            raise ValueError("ARCHIVE_DEPTH_LEVELS buiten [1, 50]")
        if not self.ws_url.startswith("wss://"):
            raise ValueError("ARCHIVE_WS_LIQUIDATION_URL moet wss:// zijn")


class DiskGuardTripped(RuntimeError):
    pass


class ArchiveWriter:
    """Dagelijkse JSONL-bestanden per bron met dedupe, rotatie en disk-guard.

    Bestandsnaam: {dir}/{source}/{source}-YYYY-MM-DD.jsonl (UTC-dag).
    Dedupe-sleutel wordt als "_k" in elk record opgeslagen zodat een herstart
    de sleutelset van vandaag uit het bestaande bestand kan herladen.
    """

    def __init__(self, base_dir: Path, source: str, min_free_gb: float = 2.0) -> None:
        self.dir = Path(base_dir) / source
        self.dir.mkdir(parents=True, exist_ok=True)
        self.source = source
        self.min_free_gb = min_free_gb
        self._day: str | None = None
        self._keys: set[str] = set()
        self._handle = None
        self.rows_written = 0
        self.rows_deduped = 0

    def _path_for(self, day: str) -> Path:
        return self.dir / f"{self.source}-{day}.jsonl"

    def _roll(self, day: str) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        self._day = day
        self._keys = set()
        path = self._path_for(day)
        if path.exists():  # herstart: dedupe-sleutels herladen (herstelpad)
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        key = json.loads(line).get("_k")
                    except json.JSONDecodeError:
                        continue
                    if key:
                        self._keys.add(key)
            log.info("ARCHIVE_RESUME | source=%s | day=%s | keys=%d",
                     self.source, day, len(self._keys))
        self._handle = path.open("a", encoding="utf-8")

    def append(self, record: dict, dedupe_key: str | None = None) -> bool:
        if disk_free_gb(self.dir) < self.min_free_gb:
            raise DiskGuardTripped(
                f"vrije schijfruimte onder {self.min_free_gb} GB; schrijven gestopt")
        day = utc_now().strftime("%Y-%m-%d")
        if day != self._day:
            self._roll(day)
        if dedupe_key is not None:
            if dedupe_key in self._keys:
                self.rows_deduped += 1
                return False
            self._keys.add(dedupe_key)
            record = {**record, "_k": dedupe_key}
        self._handle.write(json.dumps(record, separators=(",", ":"),
                                      ensure_ascii=True) + "\n")
        self._handle.flush()
        self.rows_written += 1
        return True

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def compress_and_prune(self, retention_days: int) -> dict[str, int]:
        """Gzipt afgesloten dagen en verwijdert bestanden ouder dan retentie."""
        today = utc_now().strftime("%Y-%m-%d")
        cutoff = (utc_now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")
        compressed = pruned = 0
        for path in sorted(self.dir.iterdir()):
            m = _DATE_RE.search(path.name)
            if not m:
                continue
            day, is_gz = m.group(1), bool(m.group(2))
            if day < cutoff:
                path.unlink()
                pruned += 1
                continue
            if day < today and not is_gz:
                gz = path.with_suffix(path.suffix + ".gz")
                with path.open("rb") as src, gzip.open(gz, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                path.unlink()
                compressed += 1
        return {"compressed": compressed, "pruned": pruned}


@dataclass
class SourceHealth:
    last_success_utc: str | None = None
    last_error_utc: str | None = None
    last_error: str | None = None
    consecutive_errors: int = 0
    rows_written: int = 0
    rows_deduped: int = 0
    extra: dict = field(default_factory=dict)

    def ok(self, writer: ArchiveWriter | None = None, **extra) -> None:
        self.last_success_utc = utc_iso()
        self.consecutive_errors = 0
        if writer is not None:
            self.rows_written = writer.rows_written
            self.rows_deduped = writer.rows_deduped
        if extra:
            self.extra.update(extra)

    def fail(self, exc: BaseException) -> None:
        self.last_error_utc = utc_iso()
        self.last_error = f"{type(exc).__name__}: {exc}"[:300]
        self.consecutive_errors += 1

    def lag_seconds(self) -> float | None:
        if not self.last_success_utc:
            return None
        return (utc_now() - datetime.fromisoformat(self.last_success_utc)).total_seconds()


def write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=True))
    tmp.replace(path)


def backoff_delays(base: float = 1.0, cap: float = 60.0):
    """1, 2, 4, ... gemaximeerd op cap; oneindige iterator."""
    delay = base
    while True:
        yield delay
        delay = min(cap, delay * 2)
