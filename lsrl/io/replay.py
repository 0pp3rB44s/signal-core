"""Deterministic replay of a recorded raw input log through a fresh observer + engine (stdlib only).

A replay is the ONLY evidence that a recording is sufficient: it feeds the recorded events, in order,
through the same `FeedPipeline` the live process used. A `SESSION_START` marker is a process restart: the
observer is reset and the engine begins a new session from the state that the live process had restored
(recorded in the marker), so a replay across restarts reproduces the decisions of each restarted process.
Recorded adapter fills/orders/NAV are inputs like any other; risk-gate verdicts come from the log.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lsrl.core.config import DEFAULT_CONFIG, LSRLConfig
from lsrl.core.engine import LSRLEngine
from lsrl.core.types import FeedMarker, Record, SymbolRules, intent_to_dict
from lsrl.io.gate_log import ReplayRiskGate
from lsrl.io.recorder import (anchor_path, read_decisions, read_events, read_gate_decisions, read_header,
                              verify_chain)
from lsrl.obs.observer import BookObserver, FeedConfig
from lsrl.pipeline import RESTORE, FeedPipeline

SUPPORTED_SCHEMA = 2


@dataclass
class ReplayResult:
    engine: LSRLEngine
    observer: BookObserver
    records: list[Record] = field(default_factory=list)
    intents: list[Any] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)   # in the recorder's decisions-file shape
    header: dict[str, Any] = field(default_factory=dict)
    sessions: int = 0
    restart_state_mismatches: list[dict[str, Any]] = field(default_factory=list)


def load_verified_header(paths: list[Path], cfg: LSRLConfig, verify: bool, anchor: Path | str | None = None,
                         tolerate_torn_tail: bool = False) -> tuple[dict[str, Any], SymbolRules]:
    """Integrity + frozen-config + schema check shared by REPLAY and BACKTEST; (header, symbol rules)."""
    if verify:
        ok, why = verify_chain(paths, anchor=anchor)
        if not ok and not (tolerate_torn_tail and why.startswith("TORN_TAIL")):
            raise ValueError(f"input log failed integrity verification: {why}")
    header = read_header(paths[0])
    if header.get("schema_version") != SUPPORTED_SCHEMA:
        raise ValueError(f"unsupported recording schema {header.get('schema_version')!r}")
    if header["config_digest"] != cfg.digest():
        raise ValueError("recorded config digest does not match the frozen configuration")
    r = header["symbol_rules"]
    return header, SymbolRules(r["symbol"], r["tick_size"], r["qty_step"], r["min_qty"], r["min_notional_usdt"])


def _feed_config(header: dict[str, Any]) -> FeedConfig:
    return FeedConfig(**header["feed"]) if header.get("feed") else FeedConfig()


def replay(paths: list[Path | str], *, cfg: LSRLConfig = DEFAULT_CONFIG, verify: bool = True,
           anchor: Path | str | None = None, tolerate_torn_tail: bool = False) -> ReplayResult:
    """`tolerate_torn_tail` is for inspecting a crash site BEFORE the recorder repairs it: everything durable is
    replayed, the unterminated final fragment is ignored. Every other integrity failure still refuses."""
    paths = [Path(p) for p in paths]
    header, rules = load_verified_header(paths, cfg, verify, anchor, tolerate_torn_tail)
    engine = LSRLEngine(cfg, rules, risk_gate=ReplayRiskGate(read_gate_decisions(paths)), tape_available=True)
    observer = BookObserver(_feed_config(header))
    pipeline = FeedPipeline(engine, observer, session_mode=RESTORE)
    out = ReplayResult(engine=engine, observer=observer, header=header)
    for ev in read_events(paths):
        if isinstance(ev, FeedMarker) and ev.kind == "SESSION_START":
            out.sessions += 1
            recorded = None
            try:
                recorded = json.loads(ev.detail).get("engine_state") if ev.detail else None
            except ValueError:
                recorded = None
            if out.sessions > 1 and recorded is not None and engine.snapshot() != recorded:
                out.restart_state_mismatches.append({"t_ms": ev.t_ms, "session": out.sessions})
        intents, records = pipeline.process(ev)
        out.intents.extend(intents)
        out.records.extend(records)
        for r in records:
            out.decisions.append({"type": "record", **r.to_dict()})
        for i in intents:
            out.decisions.append({"type": "intent", "t_ms": ev.t_ms, "intent": intent_to_dict(i)})
    return out


def decisions_match(paths: list[Path | str], decision_paths: list[Path | str], *,
                    cfg: LSRLConfig = DEFAULT_CONFIG) -> tuple[bool, str]:
    """Decision equivalence: a fresh engine replaying the raw inputs produces EXACTLY the decision stream
    the live process wrote. (ok, first-difference description)."""
    replayed = replay(paths, cfg=cfg).decisions
    recorded = read_decisions(decision_paths)
    canon = lambda x: json.dumps(x, sort_keys=True, separators=(",", ":"), default=str)   # noqa: E731
    if len(replayed) != len(recorded):
        return False, f"count differs: replay={len(replayed)} recorded={len(recorded)}"
    for i, (a, b) in enumerate(zip(replayed, recorded)):
        if canon(a) != canon(b):
            return False, f"decision {i} differs: replay={canon(a)[:200]} recorded={canon(b)[:200]}"
    return True, "EQUIVALENT"


def anchor_for(root: str | Path, prefix: str = "inputs") -> Path:
    return anchor_path(root, prefix)
