"""BACKTEST mode: the IDENTICAL engine re-run over RECORDED market inputs with the simulated exchange.

HISTORICAL_LIQUIDITY_REPLAY_AVAILABLE=NO. No archive of causal point-in-time order-book depth existed
when LSRL_V1 was implemented, so there is nothing to back-fill. This module only replays what the
recorder captured PROSPECTIVELY (SHADOW / PAPER / MICRO_LIVE sessions). It never reconstructs
liquidity from candles, swing highs/lows or any other hindsight.

Only MARKET events are taken from the log. Anything an execution adapter produced (NAV, order acks,
fills) belongs to the recorded run and is regenerated here by the simulated exchange, so a backtest
answers "what would the frozen rules have done with these inputs", not "what did that run do" (that is
`lsrl.io.replay`, which reproduces the recorded decisions exactly).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from lsrl.adapters.sim import SimExchange
from lsrl.core.config import DEFAULT_CONFIG, LSRLConfig
from lsrl.core.engine import LSRLEngine
from lsrl.core.gates import NullRiskGate
from lsrl.core.types import BarClosed, DepthSnapshot, TimerTick, TradeTick
from lsrl.driver import SimDriver
from lsrl.io.recorder import LSRLRecorder, read_events
from lsrl.io.replay import load_verified_header
from lsrl.modes import Mode

HISTORICAL_LIQUIDITY_REPLAY_AVAILABLE = False
MARKET_EVENTS = (DepthSnapshot, TradeTick, BarClosed, TimerTick)


def backtest(paths: list[Path | str], *, cfg: LSRLConfig = DEFAULT_CONFIG, start_nav_usdt: float = 1_000.0,
             risk_gate: Any = None, recorder: LSRLRecorder | None = None, verify: bool = True) -> SimDriver:
    """Feed the recorded market events to a fresh engine behind SimDriver(Mode.BACKTEST).

    `risk_gate` defaults to the offline gate (stamped NULL_OFFLINE in every record); pass a central
    gate adapter to apply the RiskManager to a backtest."""
    paths = [Path(p) for p in paths]
    header, rules = load_verified_header(paths, cfg, verify)
    holder: dict[str, SimDriver] = {}
    gate = risk_gate if risk_gate is not None else NullRiskGate()
    if recorder is not None:
        from lsrl.io.gate_log import RecordingRiskGate
        gate = RecordingRiskGate(gate, lambda *a: holder["driver"].gate_sink(*a))
    engine = LSRLEngine(cfg, rules, risk_gate=gate, tape_available=bool(header.get("tape_available")))
    driver = SimDriver(Mode.BACKTEST, engine, SimExchange(rules, cfg), start_nav_usdt=start_nav_usdt,
                       recorder=recorder)
    holder["driver"] = driver
    for ev in read_events(paths):
        if isinstance(ev, MARKET_EVENTS):
            driver.feed(ev)
    return driver
