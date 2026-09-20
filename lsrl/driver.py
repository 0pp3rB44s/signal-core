"""SimDriver: engine + SimExchange (+ recorder) for BACKTEST, SHADOW and PAPER.

The three modes run the IDENTICAL engine and the IDENTICAL SimExchange; they differ
only in (a) where market events come from, (b) how NAV is sourced, (c) output labels.
"""
from __future__ import annotations

from typing import Any

from lsrl.adapters.sim import SimExchange
from lsrl.core.engine import LSRLEngine
from lsrl.core.types import BarClosed, NavEvent, Record
from lsrl.io.recorder import LSRLRecorder, RecorderError
from lsrl.modes import SIM_MODES, Mode


class SimDriver:
    def __init__(self, mode: Mode, engine: LSRLEngine, sim: SimExchange, *, start_nav_usdt: float,
                 recorder: LSRLRecorder | None = None) -> None:
        if mode not in SIM_MODES:
            raise ValueError(f"SimDriver cannot run {mode}; MICRO_LIVE uses the live executor")
        self.mode, self.engine, self.sim, self.recorder = mode, engine, sim, recorder
        self.nav = start_nav_usdt
        self.compounding = mode is not Mode.SHADOW
        self.records: list[Record] = []
        self.intents: list[Any] = []
        self.recorder_error: str | None = None

    # ------------------------------------------------------------------ recorder guard
    def _guard(self, fn, *args) -> None:
        if self.recorder is None or self.recorder_error:
            return
        try:
            fn(*args)
        except RecorderError as exc:
            self.recorder_error = str(exc)
            self.engine.halt(str(exc))                 # no NEW setups without a durable record

    def gate_sink(self, t_ms: int, setup_id: str, decision: dict) -> None:
        if self.recorder is not None:
            self._guard(self.recorder.record_gate, t_ms, setup_id, decision)

    # ------------------------------------------------------------------ processing
    def _process(self, ev: Any) -> None:
        if self.recorder is not None:
            self._guard(self.recorder.record_event, ev)
        intents, records = self.engine.on_event(ev)
        if self.recorder is not None:
            self._guard(self.recorder.record_outputs, ev.t_ms, intents, records)
        self.records.extend(records)
        self.intents.extend(intents)
        for r in records:
            if r.kind == "setup_closed" and self.compounding:
                self.nav += float(r.data.get("realized_net_usdt") or 0.0)
        for order_event in self.sim.apply(intents, ev.t_ms):
            self._process(order_event)

    def feed(self, ev: Any, *, nav: bool = True) -> None:
        is_bar = nav and isinstance(ev, BarClosed) and ev.tf_s == self.engine.cfg.trigger_tf_s
        if is_bar:
            self._process(NavEvent(t_ms=ev.t_ms, nav_usdt=self.nav, source=self.mode.value))
        self._process(ev)
        if isinstance(ev, BarClosed) and ev.tf_s == self.engine.cfg.trigger_tf_s:
            for fill in self.sim.entry_fills(ev):
                self._process(fill)
            for fill in self.sim.exit_fills(ev):
                self._process(fill)

    def run(self, events) -> "SimDriver":
        for ev in events:
            self.feed(ev)
        return self
