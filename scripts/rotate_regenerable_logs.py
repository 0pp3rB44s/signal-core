#!/usr/bin/env python3
"""Lossless rotation of REGENERABLE, NON-EVIDENCE log files that a running process keeps open.

For every file named on the command line the tool
  1. refuses anything that is not a plain `*.out` / `*.log` / `*.log.N` regular file (evidence formats such as
     .jsonl/.csv/.gz/.json/.parquet are refused by extension; symlinks are refused);
  2. streams the first N bytes through gzip while hashing them (sha256 of the ORIGINAL bytes);
  3. re-reads the archive, decompresses it and requires the same sha256 and length -- otherwise nothing is truncated;
  4. archives the bytes appended in the meantime as a second, tiny part;
  5. only then truncates the live file to zero (the writer keeps its descriptor; an O_APPEND writer continues at
     the new end, any other writer leaves a sparse hole, which frees the disk blocks all the same);
  6. appends one JSON line to `MANIFEST.jsonl` in the archive directory: source path, bytes and sha256 of the
     original, archive name/size/sha256, and `unarchived_bytes` -- the (normally 0) bytes appended between the
     last snapshot and the truncate instant, which this tool cannot capture without freezing the writer.

It never deletes a file and never touches anything that was not named. Without `--apply` it only prints the plan.
`--verify MANIFEST` re-checks every archive against its recorded hashes and exits non-zero on any mismatch.
`--paths-file FILE` names the logs (one per line); a scheduled run is `deploy/launchd/com.cgc.logrotate.plist.template`
(hourly, NOT installed by anything in this repository: the owner decides).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

CHUNK = 8 * 1024 * 1024
ALLOWED_NAME = re.compile(r"\.(out|log)(\.\d+)?$")
EVIDENCE_SUFFIXES = {".jsonl", ".csv", ".gz", ".json", ".parquet", ".db", ".sqlite", ".ndjson", ".tar", ".zip", ".bin"}
MIN_FREE_BYTES = 256 * 1024 * 1024


class RotationRefused(Exception):
    pass


def _utc() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def refuse_reason(path: Path) -> str | None:
    if path.is_symlink():
        return "symlink"
    if not path.exists():
        return "missing"
    if not path.is_file():
        return "not a regular file"
    if path.suffix.lower() in EVIDENCE_SUFFIXES or not ALLOWED_NAME.search(path.name):
        return "not a plain .out/.log file (evidence formats are never rotated)"
    return None


def _archive_range(src_fd: int, start: int, end: int, out_path: Path) -> tuple[str, int, str, int]:
    """gzip bytes [start, end) of `src_fd` into `out_path`; returns (sha256 of the original bytes, length, sha256 of gz, gz size)."""
    sha = hashlib.sha256()
    length = 0
    with out_path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0) as gz:
            pos = start
            while pos < end:
                block = os.pread(src_fd, min(CHUNK, end - pos), pos)
                if not block:
                    break
                sha.update(block)
                gz.write(block)
                pos += len(block)
                length += len(block)
        raw.flush()
        os.fsync(raw.fileno())
    return sha.hexdigest(), length, _sha256_file(out_path), out_path.stat().st_size


def _sha256_file(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                return sha.hexdigest()
            sha.update(block)


def _verify_gz(path: Path, expect_sha: str, expect_len: int) -> bool:
    sha = hashlib.sha256()
    length = 0
    with gzip.open(path, "rb") as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            sha.update(block)
            length += len(block)
    return sha.hexdigest() == expect_sha and length == expect_len


def rotate_one(path: Path, archive_dir: Path, *, min_bytes: int, apply: bool) -> dict:
    reason = refuse_reason(path)
    if reason:
        raise RotationRefused(f"{path}: {reason}")
    stat = path.stat()
    size, allocated = stat.st_size, stat.st_blocks * 512
    entry = {"source": str(path), "size_bytes": size, "allocated_bytes": allocated, "applied": False}
    if min(size, allocated) < min_bytes:            # a sparse file (writer without O_APPEND after a truncate) holds no data
        entry["skipped"] = f"smaller than {min_bytes} bytes (allocated {allocated})"
        return entry
    if not apply:
        entry["skipped"] = "dry-run"
        return entry
    archive_dir.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(archive_dir).free < MIN_FREE_BYTES:
        raise RotationRefused(f"{archive_dir}: less than {MIN_FREE_BYTES // 1048576} MiB free for the archive itself")
    stamp = _utc()
    stem = f"{path.name}.{stamp}"
    fd = os.open(path, os.O_RDWR)
    try:
        st0 = os.fstat(fd)
        parts: list[dict] = []
        cursor = 0
        end = st0.st_size
        for index in range(4):                       # main pass, then up to 3 catch-up passes for late appends
            name = f"{stem}.part{index}.gz"
            tmp = archive_dir / (name + ".tmp")
            sha_o, length, sha_gz, gz_size = _archive_range(fd, cursor, end, tmp)
            if length != end - cursor or not _verify_gz(tmp, sha_o, length):
                tmp.unlink(missing_ok=True)
                raise RotationRefused(f"{path}: archive verification failed; source left untouched")
            final = archive_dir / name
            os.replace(tmp, final)
            parts.append({"archive": name, "offset": cursor, "length": length, "sha256_original": sha_o,
                          "gz_sha256": sha_gz, "gz_bytes": gz_size})
            cursor = end
            now_size = os.fstat(fd).st_size
            if now_size == cursor or index == 3:
                break
            end = now_size
        final_size = os.fstat(fd).st_size
        if os.fstat(fd).st_ino != st0.st_ino:
            raise RotationRefused(f"{path}: file was replaced while archiving; source left untouched")
        os.ftruncate(fd, 0)
        entry.update({
            "applied": True, "inode": st0.st_ino, "archived_bytes": cursor, "parts": parts,
            "unarchived_bytes": max(0, final_size - cursor), "archived_at_utc": stamp,
            "method": "gzip-6 of byte ranges, sha256 of the original bytes, decompress-verify before truncate, ftruncate(0)"})
    finally:
        os.close(fd)
    with (archive_dir / "MANIFEST.jsonl").open("a", encoding="utf-8") as manifest:
        manifest.write(json.dumps(entry, sort_keys=True) + "\n")
        manifest.flush()
        os.fsync(manifest.fileno())
    return entry


def verify_manifest(manifest: Path) -> tuple[int, int]:
    ok = bad = 0
    base = manifest.parent
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        for part in entry.get("parts", []):
            gz = base / part["archive"]
            good = gz.exists() and _sha256_file(gz) == part["gz_sha256"] and _verify_gz(gz, part["sha256_original"], part["length"])
            ok, bad = (ok + 1, bad) if good else (ok, bad + 1)
            print(("OK   " if good else "BAD  ") + part["archive"])
    return ok, bad


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--paths-file", type=Path, help="text file: one log path per line ('#' comments and blanks ignored)")
    parser.add_argument("--archive-dir", type=Path, help="where the .gz parts and MANIFEST.jsonl go (same volume is fine)")
    parser.add_argument("--min-mb", type=float, default=50.0, help="skip files smaller than this (default 50)")
    parser.add_argument("--apply", action="store_true", help="actually archive and truncate (default: dry run)")
    parser.add_argument("--verify", type=Path, help="verify every archive listed in this MANIFEST.jsonl and exit")
    args = parser.parse_args(argv)
    if args.verify:
        ok, bad = verify_manifest(args.verify)
        print(f"archives ok={ok} bad={bad}")
        return 0 if bad == 0 else 2
    paths = list(args.paths)
    if args.paths_file:
        paths += [Path(line.strip()) for line in args.paths_file.read_text(encoding="utf-8").splitlines()
                  if line.strip() and not line.lstrip().startswith("#")]
    if not paths or not args.archive_dir:
        parser.error("paths (or --paths-file) and --archive-dir are required")
    status = 0
    for path in paths:
        try:
            entry = rotate_one(path, args.archive_dir, min_bytes=int(args.min_mb * 1048576), apply=args.apply)
        except RotationRefused as exc:
            print(f"REFUSED {exc}")
            status = 1
            continue
        if entry["applied"]:
            gz = sum(p["gz_bytes"] for p in entry["parts"])
            print(f"ROTATED {path}: {entry['archived_bytes']} bytes -> {gz} bytes gz, unarchived={entry['unarchived_bytes']}")
        else:
            print(f"SKIP    {path}: {entry.get('skipped')} (apparent {entry['size_bytes']} bytes, allocated {entry['allocated_bytes']})")
    return status


if __name__ == "__main__":
    sys.exit(main())
