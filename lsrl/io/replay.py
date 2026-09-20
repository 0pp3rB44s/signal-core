"""Deterministic replay of a recorded input log into a fresh engine (stdlib only)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lsrl.core.config import DEFAULT_CONFIG, LSRLConfig
from lsrl.core.engine import LSRLEngine
from lsrl.core.types import Record, SymbolRules
from lsrl.io.gate_log import ReplayRiskGate
from lsrl.io.recorder import read_events, read_gate_decisions, read_header, verify_chain


@dataclass
class ReplayResult:
    engine: LSRLEngine
    records: list[Record] = field(default_factory=list)
    intents: list[Any] = field(default_factory=list)
    header: dict[str, Any] = field(default_factory=dict)


def replay(paths: list[Path | str], *, cfg: LSRLConfig = DEFAULT_CONFIG, verify: bool = True) -> ReplayResult:
    paths = [Path(p) for p in paths]
    if verify:
        ok, why = verify_chain(paths)
        if not ok:
            raise ValueError(f"input log failed integrity verification: {why}")
    header = read_header(paths[0])
    if header["config_digest"] != cfg.digest():
        raise ValueError("recorded config digest does not match the frozen configuration")
    r = header["symbol_rules"]
    rules = SymbolRules(r["symbol"], r["tick_size"], r["qty_step"], r["min_qty"], r["min_notional_usdt"])
    engine = LSRLEngine(cfg, rules, risk_gate=ReplayRiskGate(read_gate_decisions(paths)),
                        tape_available=bool(header.get("tape_available")))
    out = ReplayResult(engine=engine, header=header)
    for ev in read_events(paths):
        intents, records = engine.on_event(ev)
        out.intents.extend(intents)
        out.records.extend(records)
    return out
