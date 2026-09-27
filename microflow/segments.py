from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat(timespec="milliseconds")


class ImmutableSegmentWriter:
    """Write bounded gzip JSONL segments and append their SHA-256 to a manifest."""

    def __init__(self, root: Path, *, schema_version: str, symbols: tuple[str, ...],
                 max_seconds: int = 300, max_uncompressed_bytes: int = 20_000_000,
                 retention_days: int = 14, min_free_gb: float = 5.0) -> None:
        self.root = Path(root)
        self.segment_dir = self.root / "segments"
        self.segment_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.jsonl"
        self.schema_version = schema_version
        self.symbols = symbols
        self.max_seconds = max_seconds
        self.max_uncompressed_bytes = max_uncompressed_bytes
        self.retention_days = retention_days
        self.min_free_gb = min_free_gb
        self._handle = None
        self._tmp_path: Path | None = None
        self._started_ms = 0
        self._ended_ms = 0
        self._bytes = 0
        self._rows = 0
        self._gap_counts: dict[str, int] = {symbol: 0 for symbol in symbols}
        self._sequence_errors: dict[str, int] = {symbol: 0 for symbol in symbols}
        self.prune()

    def prune(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        removed = 0
        for path in self.segment_dir.glob("*.jsonl.gz"):
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            if modified < cutoff:
                path.unlink()
                removed += 1
        return removed

    def _open(self, timestamp_ms: int) -> None:
        stamp = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self._tmp_path = self.segment_dir / f"microflow-{stamp}.jsonl.gz.tmp"
        self._handle = gzip.open(self._tmp_path, "wt", encoding="utf-8", compresslevel=6)
        self._started_ms = self._ended_ms = timestamp_ms
        self._bytes = self._rows = 0
        self._gap_counts = {symbol: 0 for symbol in self.symbols}
        self._sequence_errors = {symbol: 0 for symbol in self.symbols}

    def append(self, record: dict) -> None:
        timestamp_ms = int(record.get("timestamp_local") or time.time() * 1000)
        if self._rows % 1_000 == 0:
            free_gb = shutil.disk_usage(self.segment_dir).free / 1e9
            if free_gb < self.min_free_gb:
                raise RuntimeError(f"microflow disk guard: {free_gb:.2f}GB below {self.min_free_gb:.2f}GB")
        if self._handle is None:
            self._open(timestamp_ms)
        if (timestamp_ms - self._started_ms >= self.max_seconds * 1000
                or self._bytes >= self.max_uncompressed_bytes):
            self.finalize()
            self._open(timestamp_ms)
        line = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        self._handle.write(line)
        self._bytes += len(line.encode("utf-8"))
        self._rows += 1
        self._ended_ms = timestamp_ms
        symbol = str(record.get("symbol") or "")
        quality = record.get("quality") or {}
        self._gap_counts[symbol] = max(self._gap_counts.get(symbol, 0), int(quality.get("stream_gaps", 0)))
        self._sequence_errors[symbol] = max(
            self._sequence_errors.get(symbol, 0), int(quality.get("sequence_errors", 0))
        )

    def finalize(self) -> dict | None:
        if self._handle is None or self._tmp_path is None:
            return None
        self._handle.close()
        final_path = self._tmp_path.with_suffix("")
        os.replace(self._tmp_path, final_path)
        digest = hashlib.sha256(final_path.read_bytes()).hexdigest()
        manifest = {
            "schema_version": self.schema_version,
            "segment": final_path.name,
            "sha256": digest,
            "start_timestamp": _iso(self._started_ms),
            "end_timestamp": _iso(self._ended_ms),
            "rows": self._rows,
            "uncompressed_bytes": self._bytes,
            "symbol_coverage": list(self.symbols),
            "gap_counts": self._gap_counts,
            "sequence_errors": self._sequence_errors,
        }
        with self.manifest_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(manifest, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.prune()
        self._handle = None
        self._tmp_path = None
        return manifest

    def finalize_if_due(self, timestamp_ms: int | None = None) -> dict | None:
        """Seal a sparse segment on wall-clock time even when no new row arrives."""
        now_ms = int(timestamp_ms or time.time() * 1000)
        if self._handle is not None and now_ms - self._started_ms >= self.max_seconds * 1000:
            return self.finalize()
        return None

    def close(self) -> None:
        self.finalize()
