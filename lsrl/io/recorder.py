"""Prospective recorder + replay format for LSRL_V1 (stdlib only).

CONTRACT: the recorded INPUT stream is the complete causal input of the engine
(depth snapshots, tape, completed bars, NAV, and any adapter fills/order events).
Feeding it, in order, into a fresh engine with the same frozen config reproduces
every decision bit-for-bit. That is what makes each future setup independently
reconstructable; nothing here fabricates or back-fills history.

File layout (per UTC day, append-only):
    <root>/inputs-YYYY-MM-DD.jsonl      one event per line
    <root>/decisions-YYYY-MM-DD.jsonl   engine records + intents, one per line
Each line carries `seq` and `h = sha256(prev_h + canonical(body))`, chained across
files, so truncation, insertion or edits are detectable (`verify_chain`).

Fail-closed: if a write cannot be made durable or free disk is below the floor,
`RecorderError` is raised; the runtime then halts new setups.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from lsrl.core.config import SPEC, LSRLConfig
from lsrl.core.types import SymbolRules, event_from_dict, event_to_dict, intent_to_dict

FORMAT = "lsrl_input_v1"
GENESIS = "0" * 64


class RecorderError(RuntimeError):
    pass


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _day(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def _resume(root: Path, prefix: str) -> dict[str, Any]:
    """Continue the hash chain from the last durable line of the newest existing file."""
    files = sorted(root.glob(f"{prefix}-*.jsonl")) if root.exists() else []
    state = {"h": GENESIS, "seq": 0}
    for path in reversed(files):
        last = None
        with path.open(encoding="utf-8") as fh:
            for raw in fh:
                if raw.strip():
                    last = raw
        if last is not None:
            row = json.loads(last)
            state = {"h": row["h"], "seq": row["seq"]}
            break
    return state


class _ChainedWriter:
    def __init__(self, root: Path, prefix: str, min_free_gb: float, header: dict[str, Any],
                 state: dict[str, Any]) -> None:
        self.root, self.prefix, self.min_free_gb, self.header = root, prefix, min_free_gb, header
        self.state = state                      # shared: {"h": last hash, "seq": counter}
        self._day: str | None = None
        self._fh = None
        self._session_marked = False

    def _ensure_room(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(self.root).free / 1e9
        if free_gb < self.min_free_gb:
            raise RecorderError(f"RECORDER_DISK_GUARD: {free_gb:.2f} GB free < floor {self.min_free_gb} GB")

    def _open(self, day: str) -> None:
        if self._fh is not None:
            self._fh.close()
        path = self.root / f"{self.prefix}-{day}.jsonl"
        new = not path.exists()
        self._fh = path.open("a", encoding="utf-8")
        self._day = day
        if new:
            self._write_line({"type": "header", **self.header, "day": day})
        if not self._session_marked:               # every process start is explicit in the chain
            self._write_line({"type": "session_start", "mode": self.header.get("mode"),
                              "config_digest": self.header.get("config_digest"),
                              "code_version": self.header.get("code_version")})
            self._session_marked = True

    def _write_line(self, body: dict[str, Any]) -> None:
        self.state["seq"] += 1
        line = {"seq": self.state["seq"], "body": body}
        digest = hashlib.sha256((self.state["h"] + _canon(line)).encode()).hexdigest()
        line["h"] = digest
        try:
            assert self._fh is not None
            self._fh.write(_canon(line) + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except OSError as exc:                    # disk full / IO error: NOT durable
            raise RecorderError(f"RECORDER_WRITE_FAILED: {exc}") from exc
        self.state["h"] = digest

    def write(self, t_ms: int, body: dict[str, Any]) -> None:
        self._ensure_room()
        day = _day(t_ms)
        if day != self._day:
            self._open(day)
        self._write_line(body)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class LSRLRecorder:
    def __init__(self, root: str | Path, cfg: LSRLConfig, rules: SymbolRules, *, mode: str,
                 tape_available: bool, code_version: str = "", min_free_gb: float = 1.0) -> None:
        self.root = Path(root)
        header = {"format": FORMAT, "spec": SPEC, "config_digest": cfg.digest(),
                  "symbol_rules": {"symbol": rules.symbol, "tick_size": rules.tick_size,
                                   "qty_step": rules.qty_step, "min_qty": rules.min_qty,
                                   "min_notional_usdt": rules.min_notional_usdt},
                  "tape_available": tape_available, "mode": mode, "code_version": code_version}
        self._state_in = _resume(self.root, "inputs")
        self._state_out = _resume(self.root, "decisions")
        self.inputs = _ChainedWriter(self.root, "inputs", min_free_gb, header, self._state_in)
        self.decisions = _ChainedWriter(self.root, "decisions", min_free_gb, header, self._state_out)

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
def _iter_lines(paths: Iterable[Path]) -> Iterator[dict[str, Any]]:
    for path in paths:
        with Path(path).open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if raw:
                    yield json.loads(raw)


def verify_chain(paths: Iterable[Path | str]) -> tuple[bool, str]:
    """Recompute the hash chain across the given files (in order). (ok, reason)."""
    prev, expected_seq = GENESIS, 0
    for line in _iter_lines([Path(p) for p in paths]):
        expected_seq += 1
        h = line.get("h")
        body = {"seq": line["seq"], "body": line["body"]}
        if line["seq"] != expected_seq:
            return False, f"SEQ_GAP at seq={line['seq']} expected={expected_seq}"
        if hashlib.sha256((prev + _canon(body)).encode()).hexdigest() != h:
            return False, f"HASH_MISMATCH at seq={line['seq']}"
        prev = h
    return True, "OK"


def read_header(path: Path | str) -> dict[str, Any]:
    line = next(_iter_lines([Path(path)]))
    return line["body"]


def read_events(paths: Iterable[Path | str]) -> Iterator[Any]:
    for line in _iter_lines([Path(p) for p in paths]):
        body = line["body"]
        if body.get("type") == "event":
            yield event_from_dict(body["event"])


def read_gate_decisions(paths: Iterable[Path | str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for line in _iter_lines([Path(p) for p in paths]):
        body = line["body"]
        if body.get("type") == "gate":
            out[body["setup_id"]] = body["decision"]
    return out


def input_files(root: str | Path) -> list[Path]:
    return sorted(Path(root).glob("inputs-*.jsonl"))


def decision_files(root: str | Path) -> list[Path]:
    return sorted(Path(root).glob("decisions-*.jsonl"))
