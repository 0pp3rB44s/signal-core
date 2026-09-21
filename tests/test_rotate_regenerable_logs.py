"""The disk tool may only ever shrink a REGENERABLE log, and only after the bytes are provably archived."""
from __future__ import annotations

import gzip
import importlib.util
import json
import os
import threading
import time
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("rotate_regenerable_logs",
                                              Path(__file__).resolve().parents[1] / "scripts" / "rotate_regenerable_logs.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def make_log(path: Path, lines: int = 20000) -> bytes:
    data = b"".join(b'Traceback (most recent call last):\n  File "x.py", line %d\nRuntimeError: disk guard %d\n' % (i % 7, i)
                    for i in range(lines))
    path.write_bytes(data)
    return data


def read_parts(archive_dir: Path, entry: dict) -> bytes:
    return b"".join(gzip.open(archive_dir / part["archive"], "rb").read() for part in entry["parts"])


def test_rotation_is_lossless_and_leaves_an_empty_live_file(tmp_path):
    src = tmp_path / "collector.out"
    original = make_log(src)
    entry = tool.rotate_one(src, tmp_path / "arch", min_bytes=0, apply=True)
    assert entry["applied"] and entry["unarchived_bytes"] == 0
    assert src.stat().st_size == 0
    assert read_parts(tmp_path / "arch", entry) == original
    assert sum(p["gz_bytes"] for p in entry["parts"]) < len(original) / 5
    manifest = tmp_path / "arch" / "MANIFEST.jsonl"
    assert json.loads(manifest.read_text().splitlines()[0])["source"] == str(src)
    assert tool.verify_manifest(manifest) == (len(entry["parts"]), 0)


@pytest.mark.parametrize("name", ["data.jsonl", "trades.csv", "segment.jsonl.gz", "state.json", "x.parquet", "notes.txt", "run.sh"])
def test_evidence_and_unknown_formats_are_refused_untouched(tmp_path, name):
    src = tmp_path / name
    src.write_bytes(b"evidence\n" * 1000)
    with pytest.raises(tool.RotationRefused):
        tool.rotate_one(src, tmp_path / "arch", min_bytes=0, apply=True)
    assert src.read_bytes() == b"evidence\n" * 1000
    assert not (tmp_path / "arch").exists()


def test_symlinks_and_missing_files_are_refused(tmp_path):
    real = tmp_path / "real.log"
    real.write_bytes(b"x" * 100)
    link = tmp_path / "link.log"
    link.symlink_to(real)
    for target in (link, tmp_path / "absent.log"):
        with pytest.raises(tool.RotationRefused):
            tool.rotate_one(target, tmp_path / "arch", min_bytes=0, apply=True)
    assert real.read_bytes() == b"x" * 100


def test_dry_run_and_small_files_change_nothing(tmp_path):
    src = tmp_path / "a.out"
    original = make_log(src, 100)
    assert tool.rotate_one(src, tmp_path / "arch", min_bytes=0, apply=False)["skipped"] == "dry-run"
    assert tool.rotate_one(src, tmp_path / "arch", min_bytes=10**9, apply=True)["skipped"].startswith("smaller")
    assert src.read_bytes() == original and not (tmp_path / "arch").exists()


def test_a_failed_verification_never_truncates_the_source(tmp_path, monkeypatch):
    src = tmp_path / "a.log"
    original = make_log(src)
    monkeypatch.setattr(tool, "_verify_gz", lambda *a, **k: False)
    with pytest.raises(tool.RotationRefused):
        tool.rotate_one(src, tmp_path / "arch", min_bytes=0, apply=True)
    assert src.read_bytes() == original
    assert not list((tmp_path / "arch").glob("*.tmp")) and not (tmp_path / "arch" / "MANIFEST.jsonl").exists()


def test_bytes_appended_while_archiving_are_captured_or_counted_never_silently_lost(tmp_path):
    src = tmp_path / "live.out"
    make_log(src, 60000)
    written = [src.read_bytes()]
    stop = threading.Event()

    def writer():
        with open(src, "ab") as handle:                     # O_APPEND, like a shell `>>` redirect
            i = 0
            while not stop.is_set():
                line = b"late line %d\n" % i
                handle.write(line)
                handle.flush()
                written.append(line)
                i += 1
                time.sleep(0.0005)

    thread = threading.Thread(target=writer)
    thread.start()
    time.sleep(0.05)
    entry = tool.rotate_one(src, tmp_path / "arch", min_bytes=0, apply=True)
    time.sleep(0.05)
    stop.set()
    thread.join()
    stream = b"".join(written)
    archived = read_parts(tmp_path / "arch", entry)
    assert stream.startswith(archived) and entry["archived_bytes"] == len(archived)
    after = src.read_bytes()
    assert len(archived) + entry["unarchived_bytes"] + len(after) == len(stream)
    assert entry["unarchived_bytes"] < 4096                   # only what arrived between the last snapshot and the truncate


def test_verify_detects_a_corrupted_archive(tmp_path, capsys):
    src = tmp_path / "a.out"
    make_log(src)
    entry = tool.rotate_one(src, tmp_path / "arch", min_bytes=0, apply=True)
    gz = tmp_path / "arch" / entry["parts"][0]["archive"]
    blob = bytearray(gz.read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    gz.write_bytes(bytes(blob))
    assert tool.main(["--verify", str(tmp_path / "arch" / "MANIFEST.jsonl")]) == 2
    assert "BAD" in capsys.readouterr().out


def test_the_tool_never_deletes_a_file_or_touches_unnamed_neighbours(tmp_path):
    named, other = tmp_path / "a.out", tmp_path / "b.out"
    make_log(named)
    keep = make_log(other)
    tool.rotate_one(named, tmp_path / "arch", min_bytes=0, apply=True)
    assert named.exists() and other.read_bytes() == keep


def test_a_sparse_leftover_of_an_earlier_rotation_is_not_rotated_again(tmp_path):
    src = tmp_path / "sparse.out"
    with open(src, "wb") as handle:                          # what a non-O_APPEND writer leaves after ftruncate(0)
        handle.seek(300 * 1024 * 1024)
        handle.write(b"tail\n")
    st = src.stat()
    if st.st_blocks * 512 > st.st_size // 2:
        pytest.skip("filesystem without sparse files")
    entry = tool.rotate_one(src, tmp_path / "arch", min_bytes=50 * 1024 * 1024, apply=True)
    assert entry["applied"] is False and "allocated" in entry["skipped"]
    assert src.stat().st_size == st.st_size and not (tmp_path / "arch").exists()


# ---------------------------------------------------------------------------------------------- the (uninstalled) schedule
REPO = Path(__file__).resolve().parents[1]


def test_paths_file_lists_the_logs_and_ignores_comments(tmp_path):
    a, b = tmp_path / "a.out", tmp_path / "b.log"
    make_log(a)
    make_log(b)
    listing = tmp_path / "paths.txt"
    listing.write_text(f"# regenerable logs\n{a}\n\n{b}\n")
    assert tool.main(["--apply", "--min-mb", "0", "--archive-dir", str(tmp_path / "arch"), "--paths-file", str(listing)]) == 0
    assert a.stat().st_size == 0 and b.stat().st_size == 0


def test_the_schedule_only_runs_the_rotation_tool_and_is_installed_by_nothing_here():
    plist = (REPO / "deploy/launchd/com.cgc.logrotate.plist.template").read_text()
    assert "rotate_regenerable_logs.py" in plist and "app.main" not in plist and "KeepAlive" not in plist
    assert "<key>StartInterval</key>      <integer>3600</integer>" in plist
    for script in ("install_logrotate_agent.sh", "uninstall_logrotate_agent.sh"):
        text = (REPO / "deploy/launchd" / script).read_text()
        assert "app.main" not in text and "rm -rf" not in text and "rm -f \"$HOME/Library/LaunchAgents" in text or script.startswith("install")
    callers = [p for p in REPO.rglob("*") if p.suffix in {".sh", ".py", ".plist", ".template"} and p.is_file()
               and "tests" not in p.parts and p.name not in ("install_logrotate_agent.sh", "com.cgc.logrotate.plist.template")
               and "install_logrotate_agent" in p.read_text(errors="ignore")]
    assert callers == []
