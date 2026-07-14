from __future__ import annotations

from pathlib import Path

from telemetry.safe_io import file_lock

DEFAULT_MAX_BYTES = 25_000_000
DEFAULT_BACKUP_COUNT = 10


def rotate_if_needed(path: Path, max_bytes: int = DEFAULT_MAX_BYTES, backup_count: int = DEFAULT_BACKUP_COUNT) -> None:
    """Rotate `path` once it reaches max_bytes, keeping backup_count numbered archives (.1 is most recent)."""
    with file_lock(path):
        try:
            if not path.exists() or path.stat().st_size < max_bytes:
                return
        except OSError:
            return

        for index in range(backup_count - 1, 0, -1):
            src = path.with_name(f"{path.name}.{index}")
            dst = path.with_name(f"{path.name}.{index + 1}")
            if src.exists():
                dst.unlink(missing_ok=True)
                src.rename(dst)

        backup = path.with_name(f"{path.name}.1")
        backup.unlink(missing_ok=True)
        path.rename(backup)


def rotated_segments(path: Path, backup_count: int = DEFAULT_BACKUP_COUNT) -> list[Path]:
    """Return existing rotated backups oldest-first, followed by the live file, for full-history reads."""
    segments = [
        candidate
        for index in range(backup_count, 0, -1)
        if (candidate := path.with_name(f"{path.name}.{index}")).exists()
    ]
    if path.exists():
        segments.append(path)
    return segments
