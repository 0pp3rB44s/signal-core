"""Prospective recorder + replay format for LSRL_V1, schema v2 (stdlib only).

CONTRACT: the recorded INPUT stream is the complete causal input of the strategy: every raw depth push
(snapshot and every applied update, with exchange time, receive time and seq/pseq), every raw trade,
connection/reset markers, completed 1m and 15m bars, NAV, timer ticks, adapter order/fill events and the
risk-gate decisions. Feeding it, in order, through a fresh `FeedPipeline` (observer + engine) with the same
frozen config reproduces every decision bit-for-bit. Nothing derived (a book state, a "sweep", a health
verdict) is ever stored INSTEAD of the raw evidence it came from.

Layout (per UTC day, append-only): `inputs-YYYY-MM-DD.jsonl[.gz]`, `decisions-YYYY-MM-DD.jsonl[.gz]`,
and one anchor file per stream `anchor-inputs.jsonl` / `anchor-decisions.jsonl`.

Integrity:
  * every line carries `seq` and `h = sha256(prev_h + canonical(line))`, chained ACROSS files;
  * TAIL ANCHOR: every `anchor_every` records (and at close / day rollover) the writer appends (seq, h) to
    a second, independently hash-chained file. A tail truncated at a line boundary leaves a chain that
    verifies on its own but ends BEFORE the anchor: `verify_chain` reports it and the recorder refuses to
    continue past it (`TAIL_TRUNCATED`). Records written after the last anchor (at most `anchor_every`, or a
    few seconds) are the documented residual;
  * TORN TAIL: a crash mid-write leaves an unterminated/garbled last line. On restart the recorder moves those
    bytes to a `quarantine-*.bin` side file (never deleted), truncates to the last valid line, records a
    `recovery` body and continues the SAME chain;
  * every process start writes an explicit `session_start` body; the runtime additionally feeds a
    `SESSION_START` feed marker (carrying the restored engine state) so replay treats it as a boundary;
  * closed days are compressed losslessly by a background thread (content hash verified before the plain
    file is removed). Readers accept both forms.

Fail-closed: if a write cannot be made durable or free disk is below the floor, `RecorderError` is raised
and STAYS raised (the writer is poisoned); the runtime then halts NEW setups until a restart.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from lsrl.core.config import SPEC, LSRLConfig
from lsrl.core.types import SymbolRules, event_from_dict, event_to_dict, intent_to_dict

FORMAT = "lsrl_input_v2"
SCHEMA_VERSION = 2
STRATEGY = "LSRL_V1"
GENESIS = "0" * 64
TAIL_SCAN_BYTES = 1 << 20


class RecorderError(RuntimeError):
    pass


class TornTail(ValueError):
    """The last line of the last file is not a complete record (crash mid-write)."""


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _day(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def anchor_path(root: str | Path, prefix: str) -> Path:
    return Path(root) / f"anchor-{prefix}.jsonl"


# ---------------------------------------------------------------------- file discovery
def data_files(root: str | Path, prefix: str) -> list[Path]:
    """One file per day, in day order; the plain file wins while a compression is in flight."""
    by_day: dict[str, Path] = {}
    for path in sorted(Path(root).glob(f"{prefix}-*.jsonl*")):
        if path.name.endswith(".tmp"):
            continue
        day = path.name[len(prefix) + 1:len(prefix) + 11]
        if day not in by_day or not path.name.endswith(".gz"):
            by_day[day] = path
    return [by_day[d] for d in sorted(by_day)]


def input_files(root: str | Path) -> list[Path]:
    return data_files(root, "inputs")


def decision_files(root: str | Path) -> list[Path]:
    return data_files(root, "decisions")


def _open_text(path: Path):
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    try:
        return path.open(encoding="utf-8")
    except FileNotFoundError:                     # the compressor replaced it between listing and opening
        gz = path.with_name(path.name + ".gz")
        if gz.exists():
            return gzip.open(gz, "rt", encoding="utf-8")
        raise


# ---------------------------------------------------------------------- anchor
def _anchor_entry(n: int, seq: int, h: str, file: str, t_ms: int, prev: str) -> dict[str, Any]:
    body = {"n": n, "seq": seq, "h": h, "file": file, "t_ms": t_ms, "prev": prev}
    return {**body, "ah": _sha(_canon(body))}


def read_anchor(path: Path | str) -> tuple[list[dict[str, Any]], str | None, int]:
    """(entries, error, torn_bytes). A torn FINAL fragment (crash mid-anchor) is reported through
    `torn_bytes`, not as an error; every complete line must chain."""
    path = Path(path)
    if not path.exists():
        return [], None, 0
    lines = path.read_bytes().split(b"\n")
    fragment = lines.pop()
    entries: list[dict[str, Any]] = []
    prev = GENESIS
    for i, line in enumerate(lines):
        try:
            e = json.loads(line)
            body = {k: e[k] for k in ("n", "seq", "h", "file", "t_ms", "prev")}
        except (ValueError, KeyError):
            return entries, f"ANCHOR_CORRUPT at line {i + 1}", 0
        if e["prev"] != prev or e.get("ah") != _sha(_canon(body)) or e["n"] != len(entries) + 1:
            return entries, f"ANCHOR_CHAIN_BROKEN at line {i + 1}", 0
        entries.append(e)
        prev = e["ah"]
    return entries, None, len(fragment)


# ---------------------------------------------------------------------- writer
class _ChainedWriter:
    def __init__(self, root: Path, prefix: str, min_free_gb: float, header: dict[str, Any], *,
                 anchor_every: int = 50, anchor_max_age_s: float = 5.0, wall=time.time) -> None:
        self.root, self.prefix, self.min_free_gb, self.header = root, prefix, min_free_gb, header
        self.anchor_every, self.anchor_max_age_s, self.wall = anchor_every, anchor_max_age_s, wall
        self.state: dict[str, Any] = {"h": GENESIS, "seq": 0}
        self._fh = None
        self._day: str | None = None
        self._file_name = ""
        self._session_started = False
        self._since_anchor = 0
        self._anchor_n = 0
        self._anchor_prev = GENESIS
        self._anchor_wall = wall()
        self._recovery: dict[str, Any] | None = None
        self._poisoned: str | None = None
        self._compress_thread: threading.Thread | None = None
        self._recover()

    # ------------------------------------------------------------------ recovery
    def _recover(self) -> None:
        anchor_file = anchor_path(self.root, self.prefix)
        entries, err, torn_anchor = read_anchor(anchor_file)
        if err:
            raise RecorderError(f"RECORDER_{err}")
        if torn_anchor:                                            # repair BEFORE appending after it
            self._truncate_keep_fragment(anchor_file, anchor_file.stat().st_size - torn_anchor)
        last_anchor = entries[-1] if entries else None
        if last_anchor:
            self._anchor_n, self._anchor_prev = last_anchor["n"], last_anchor["ah"]
        files = data_files(self.root, self.prefix) if self.root.exists() else []
        state: dict[str, Any] | None = None
        plans: list[tuple[Path, int]] = []                 # (file, cut offset): applied ONLY after every check passes
        while files:
            last = files[-1]
            if last.name.endswith(".gz"):
                state = self._state_from_gz(last)
                break
            state, cut = self._plan_plain(last)
            if cut is not None:
                plans.append((last, cut))
            if state is not None:
                break
            files.pop()                                    # the file held nothing valid (a torn first write)
        if state is None:
            if last_anchor and last_anchor["seq"] > 0:
                raise RecorderError(f"RECORDER_DATA_MISSING: anchor records seq={last_anchor['seq']} "
                                    "but no data survives")
            self._apply_plans(plans)
            return
        if last_anchor:
            if last_anchor["seq"] > state["seq"]:
                raise RecorderError(f"RECORDER_TAIL_TRUNCATED: data ends at seq={state['seq']} but the anchor "
                                    f"recorded seq={last_anchor['seq']}")
            if last_anchor["seq"] == state["seq"] and last_anchor["h"] != state["h"]:
                raise RecorderError("RECORDER_ANCHOR_HASH_MISMATCH at the chain tail")
        self.state = state
        self._apply_plans(plans)

    def _apply_plans(self, plans: list[tuple[Path, int]]) -> None:
        """Quarantine (never delete) the discarded tail bytes and truncate to the last valid line."""
        discarded, name = 0, ""
        for path, cut in plans:
            fragment, name = self._truncate_keep_fragment(path, cut)
            discarded += len(fragment)
        if plans:
            self._recovery = {"type": "recovery", "reason": "TORN_TAIL" if len(plans) == 1 and discarded else "CORRUPT_TAIL",
                              "discarded_bytes": discarded, "quarantine": name, "resumed_seq": self.state["seq"]}

    @staticmethod
    def _state_from_gz(path: Path) -> dict[str, Any]:
        last = None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for raw in fh:
                    if raw.strip():
                        last = raw
        except (OSError, EOFError) as exc:
            raise RecorderError(f"RECORDER_GZ_CORRUPT {path.name}: {exc}") from exc
        if last is None:
            raise RecorderError(f"RECORDER_GZ_EMPTY {path.name}")
        row = json.loads(last)
        return {"h": row["h"], "seq": row["seq"]}

    def _truncate_keep_fragment(self, path: Path, cut: int) -> tuple[bytes, str]:
        """Truncate `path` at `cut`, keeping the removed bytes in a quarantine file (never deleted)."""
        with path.open("r+b") as fh:
            fh.seek(cut)
            fragment = fh.read()
            fh.truncate(cut)
            fh.flush()
            os.fsync(fh.fileno())
        name = f"quarantine-{path.stem}-{int(self.wall() * 1000)}-{cut}.bin"
        if fragment:
            (path.parent / name).write_bytes(fragment)
        return fragment, name

    @staticmethod
    def _valid_line(line: bytes, prev_line: bytes | None) -> bool:
        """Structural + hash check of a complete line. The FIRST line of a file has no in-file predecessor
        (its predecessor lives in the previous file) so it is checked structurally only."""
        try:
            row = json.loads(line)
            body = {"seq": row["seq"], "body": row["body"]}
            h = row["h"]
        except (ValueError, KeyError, TypeError):
            return False
        if prev_line is None:
            return True
        try:
            prev_h = json.loads(prev_line)["h"]
        except (ValueError, KeyError, TypeError):
            return False
        return _sha(prev_h + _canon(body)) == h

    def _plan_plain(self, path: Path) -> tuple[dict[str, Any] | None, int | None]:
        """READ-ONLY. (last valid (seq, h) or None if nothing valid, cut offset to truncate to or None).

        A torn (unterminated) final segment and up to 3 complete-but-invalid final lines are planned for
        quarantine; more than that is corruption, not a crash, and refuses."""
        size = path.stat().st_size
        if size == 0:
            return None, None
        block = min(size, TAIL_SCAN_BYTES)
        while True:
            with path.open("rb") as fh:
                fh.seek(size - block)
                data = fh.read(block)
            if data.count(b"\n") >= 6 or block == size:
                break
            block = min(size, block * 4)
        base = size - len(data)
        pieces = data.split(b"\n")
        tail = pieces.pop()                                            # the unterminated final segment (may be empty)
        offsets, pos = [], base
        for piece in pieces:
            offsets.append(pos)
            pos += len(piece) + 1
        cut = None
        if tail:
            cut = pos                                                  # first byte of the torn segment
        dropped = 0
        while pieces:
            prev = pieces[-2] if len(pieces) >= 2 else None
            if self._valid_line(pieces[-1], prev):
                row = json.loads(pieces[-1])
                return {"h": row["h"], "seq": row["seq"]}, cut
            if dropped >= 3:
                raise RecorderError(f"RECORDER_TAIL_UNRECOVERABLE {path.name}: more than 3 invalid trailing lines")
            dropped += 1
            pieces.pop()
            cut = offsets.pop()
        if base > 0:
            raise RecorderError(f"RECORDER_TAIL_UNRECOVERABLE {path.name}")
        return None, 0 if size else None

    # ------------------------------------------------------------------ writing
    def _ensure_room(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(self.root).free / 1e9
        if free_gb < self.min_free_gb:
            self._poisoned = f"RECORDER_DISK_GUARD: {free_gb:.2f} GB free < floor {self.min_free_gb} GB"
            raise RecorderError(self._poisoned)

    def _append(self, fh, text: str) -> None:
        try:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        except OSError as exc:                       # not durable: poison the writer, never guess
            self._poisoned = f"RECORDER_WRITE_FAILED: {exc}"
            raise RecorderError(self._poisoned) from exc

    def _write_anchor(self) -> None:
        if self._since_anchor == 0:
            return
        entry = _anchor_entry(self._anchor_n + 1, self.state["seq"], self.state["h"], self._file_name,
                              int(self.wall() * 1000), self._anchor_prev)
        with anchor_path(self.root, self.prefix).open("a", encoding="utf-8") as fh:
            self._append(fh, _canon(entry) + "\n")
        self._anchor_n, self._anchor_prev = entry["n"], entry["ah"]
        self._since_anchor, self._anchor_wall = 0, self.wall()

    def _write_line(self, body: dict[str, Any]) -> None:
        assert self._fh is not None
        seq = self.state["seq"] + 1
        line: dict[str, Any] = {"seq": seq, "body": body}
        digest = _sha(self.state["h"] + _canon(line))
        line["h"] = digest
        self._append(self._fh, _canon(line) + "\n")
        self.state = {"h": digest, "seq": seq}
        self._since_anchor += 1

    def _open(self, day: str) -> None:
        previous_day = self._day
        if self._fh is not None:
            self._write_anchor()
            self._fh.close()
            self._fh = None
        path = self.root / f"{self.prefix}-{day}.jsonl"
        new = not path.exists()
        self._fh = path.open("a", encoding="utf-8")
        self._day, self._file_name = day, path.name
        if new:
            self._write_line({"type": "header", **self.header, "day": day})
        if self._recovery is not None:
            self._write_line(self._recovery)
            self._recovery = None
        if not self._session_started:                # every process start is explicit in the chain
            self._write_line({"type": "session_start", "session_id": self.header.get("session_id"),
                              "start_ts_ms": self.header.get("start_ts_ms"), "git_sha": self.header.get("git_sha"),
                              "config_digest": self.header.get("config_digest"), "mode": self.header.get("mode"),
                              "feed_digest": self.header.get("feed_digest")})
            self._session_started = True
        if previous_day is not None and previous_day != day:
            self._compress_thread = threading.Thread(
                target=compress_closed_days, args=(self.root, self.prefix, day), daemon=True,
                name=f"lsrl-compress-{self.prefix}")
            self._compress_thread.start()

    def write(self, t_ms: int, body: dict[str, Any]) -> None:
        if self._poisoned:
            raise RecorderError(self._poisoned)
        self._ensure_room()
        day = _day(t_ms)
        if day != self._day:
            self._open(day)
        self._write_line(body)
        if self._since_anchor >= self.anchor_every or (
                self._since_anchor and self.wall() - self._anchor_wall >= self.anchor_max_age_s):
            self._write_anchor()

    def close(self) -> None:
        if self._fh is not None:
            if not self._poisoned:
                self._write_anchor()
            self._fh.close()
            self._fh = None
        if self._compress_thread is not None and self._compress_thread.is_alive():
            self._compress_thread.join(timeout=120)      # never leave a half-finished compression behind a shutdown


def compress_closed_days(root: str | Path, prefix: str, keep_day: str) -> list[Path]:
    """Losslessly gzip every plain file of a day BEFORE `keep_day`. The plain file is removed only after the
    decompressed bytes are proven identical to it. Safe to interrupt: a leftover `.tmp` is ignored."""
    done: list[Path] = []
    for path in sorted(Path(root).glob(f"{prefix}-*.jsonl")):
        day = path.name[len(prefix) + 1:len(prefix) + 11]
        if day >= keep_day:
            continue
        gz, tmp = path.with_name(path.name + ".gz"), path.with_name(path.name + ".gz.tmp")
        try:
            want = hashlib.sha256()
            with path.open("rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    want.update(chunk)
                    dst.write(chunk)
            got = hashlib.sha256()
            with gzip.open(tmp, "rb") as check:
                for chunk in iter(lambda: check.read(1 << 20), b""):
                    got.update(chunk)
            if got.digest() != want.digest():
                tmp.unlink(missing_ok=True)
                continue
            os.replace(tmp, gz)
            path.unlink()
            done.append(gz)
        except OSError:
            tmp.unlink(missing_ok=True)
    return done


# ---------------------------------------------------------------------- recorder
class LSRLRecorder:
    def __init__(self, root: str | Path, cfg: LSRLConfig, rules: SymbolRules, *, mode: str,
                 feed: dict[str, Any] | None = None, transport: dict[str, Any] | None = None,
                 git_sha: str = "UNKNOWN", session_id: str | None = None, min_free_gb: float = 1.0,
                 start_ts_ms: int | None = None, exchange: str = "bitget", anchor_every: int = 50,
                 wall=time.time) -> None:
        self.root = Path(root)
        feed = dict(feed or {})
        self.header = {
            "format": FORMAT, "schema_version": SCHEMA_VERSION, "strategy": STRATEGY, "spec": SPEC,
            "git_sha": git_sha, "config_digest": cfg.digest(),
            "feed": feed, "feed_digest": _sha(_canon(feed)), "transport": dict(transport or {}),
            "symbol": rules.symbol, "exchange": exchange,
            "symbol_rules": {"symbol": rules.symbol, "tick_size": rules.tick_size, "qty_step": rules.qty_step,
                             "min_qty": rules.min_qty, "min_notional_usdt": rules.min_notional_usdt},
            "mode": mode, "session_id": session_id or uuid.uuid4().hex,
            "start_ts_ms": int(start_ts_ms if start_ts_ms is not None else wall() * 1000)}
        self.inputs = _ChainedWriter(self.root, "inputs", min_free_gb, self.header, anchor_every=anchor_every,
                                     wall=wall)
        self.decisions = _ChainedWriter(self.root, "decisions", min_free_gb, self.header,
                                        anchor_every=anchor_every, wall=wall)

    def record_event(self, ev: Any) -> None:
        self.inputs.write(ev.t_ms, {"type": "event", "event": event_to_dict(ev)})

    def record_gate(self, t_ms: int, setup_id: str, decision: dict[str, Any]) -> None:
        self.inputs.write(t_ms, {"type": "gate", "setup_id": setup_id, "decision": decision})

    def record_outputs(self, t_ms: int, intents: Iterable[Any], records: Iterable[Any]) -> None:
        for r in records:
            self.decisions.write(t_ms, {"type": "record", **r.to_dict()})
        for i in intents:
            self.decisions.write(t_ms, {"type": "intent", "t_ms": t_ms, "intent": intent_to_dict(i)})

    def close(self) -> None:
        self.inputs.close()
        self.decisions.close()


# ---------------------------------------------------------------------- reading / replay
def _iter_lines(paths: Iterable[Path], *, strict: bool = False) -> Iterator[dict[str, Any]]:
    """Yield parsed lines. A torn LAST line of the LAST file is skipped (or raised as TornTail if `strict`)."""
    paths = [Path(p) for p in paths]
    for index, path in enumerate(paths):
        last_file = index == len(paths) - 1
        with _open_text(path) as fh:
            pending: str | None = None
            for raw in fh:
                if pending is not None:
                    yield json.loads(pending)
                pending = raw
            if pending is not None and pending.strip():
                try:
                    yield json.loads(pending)
                except ValueError as exc:
                    if not (last_file and not pending.endswith("\n")):
                        raise
                    if strict:
                        raise TornTail(str(exc)) from exc


def verify_chain(paths: Iterable[Path | str], *, anchor: Path | str | None = None) -> tuple[bool, str]:
    """Recompute the hash chain across the given files (in order); with `anchor`, also require that every
    anchored (seq, h) is present and that the data does not end before the last anchor. (ok, reason)."""
    paths = [Path(p) for p in paths]
    anchors: dict[int, str] = {}
    last_anchor_seq = 0
    if anchor is not None:
        entries, err, _torn = read_anchor(anchor)
        if err:
            return False, err
        anchors = {e["seq"]: e["h"] for e in entries}
        last_anchor_seq = entries[-1]["seq"] if entries else 0
    prev, expected_seq, seen = GENESIS, 0, 0
    try:
        for line in _iter_lines(paths, strict=True):
            expected_seq += 1
            body = {"seq": line["seq"], "body": line["body"]}
            if line["seq"] != expected_seq:
                return False, f"SEQ_GAP at seq={line['seq']} expected={expected_seq}"
            if _sha(prev + _canon(body)) != line.get("h"):
                return False, f"HASH_MISMATCH at seq={line['seq']}"
            prev = line["h"]
            if line["seq"] in anchors:
                if anchors[line["seq"]] != prev:
                    return False, f"ANCHOR_MISMATCH at seq={line['seq']}"
                seen += 1
    except TornTail:
        return False, f"TORN_TAIL after seq={expected_seq}"
    except ValueError:
        return False, f"UNPARSEABLE_LINE after seq={expected_seq}"
    except (OSError, EOFError) as exc:
        return False, f"UNREADABLE: {exc}"
    if last_anchor_seq > expected_seq:
        return False, f"TAIL_TRUNCATED: data ends at seq={expected_seq}, anchor recorded seq={last_anchor_seq}"
    if seen != len([s for s in anchors if s <= expected_seq]):
        return False, "ANCHOR_SEQ_MISSING_FROM_DATA"
    return True, "OK"


def read_header(path: Path | str) -> dict[str, Any]:
    line = next(_iter_lines([Path(path)]))
    return line["body"]


def read_events(paths: Iterable[Path | str]) -> Iterator[Any]:
    for line in _iter_lines([Path(p) for p in paths]):
        body = line["body"]
        if body.get("type") == "event":
            yield event_from_dict(body["event"])


def read_bodies(paths: Iterable[Path | str], types: tuple[str, ...] | None = None) -> Iterator[dict[str, Any]]:
    for line in _iter_lines([Path(p) for p in paths]):
        if types is None or line["body"].get("type") in types:
            yield line["body"]


def read_gate_decisions(paths: Iterable[Path | str]) -> dict[str, dict[str, Any]]:
    return {b["setup_id"]: b["decision"] for b in read_bodies(paths, ("gate",))}


def read_decisions(paths: Iterable[Path | str]) -> list[dict[str, Any]]:
    """The decision stream (records + intents) exactly as written."""
    return list(read_bodies(paths, ("record", "intent")))
