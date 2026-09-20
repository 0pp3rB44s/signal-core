"""LiveDriver: engine + the MICRO_LIVE executor (+ recorder). Mirrors SimDriver's structure.

Project-coupled only through the executor it is handed; unarmed unless the executor could be built.
"""
from __future__ import annotations

from typing import Any, Callable

from lsrl.core.engine import LSRLEngine
from lsrl.core.types import BarClosed, NavEvent, Record
from lsrl.io.recorder import LSRLRecorder, RecorderError


class LiveDriver:
    def __init__(self, engine: LSRLEngine, executor: Any, *, nav_fn: Callable[[], float],
                 recorder: LSRLRecorder | None = None) -> None:
        self.engine, self.executor, self.nav_fn, self.recorder = engine, executor, nav_fn, recorder
        self.records: list[Record] = []
        self.intents: list[Any] = []
        self.recorder_error: str | None = None
        self.nav_error: str | None = None

    def _guard(self, fn, *args) -> None:
        if self.recorder is None or self.recorder_error:
            return
        try:
            fn(*args)
        except RecorderError as exc:
            self.recorder_error = str(exc)
            self.engine.halt(str(exc))

    def gate_sink(self, t_ms: int, setup_id: str, decision: dict) -> None:
        if self.recorder is not None:
            self._guard(self.recorder.record_gate, t_ms, setup_id, decision)

    def _process(self, ev: Any) -> None:
        if self.recorder is not None:
            self._guard(self.recorder.record_event, ev)
        intents, records = self.engine.on_event(ev)
        if self.recorder is not None:
            self._guard(self.recorder.record_outputs, ev.t_ms, intents, records)
        self.records.extend(records)
        self.intents.extend(intents)
        for out in self.executor.apply(intents, ev.t_ms):
            self._process(out)
        if self.executor.halt_reason:
            self.engine.halt(f"EXECUTOR:{self.executor.halt_reason}")

    def feed(self, ev: Any, *, nav: bool = True) -> None:
        if nav and isinstance(ev, BarClosed) and ev.tf_s == self.engine.cfg.trigger_tf_s:
            try:
                self._process(NavEvent(t_ms=ev.t_ms, nav_usdt=float(self.nav_fn()), source="AUTHENTICATED_EQUITY"))
                self.nav_error = None
            except Exception as exc:             # NAV goes stale -> NAV_UNAVAILABLE: fail closed
                self.nav_error = f"{type(exc).__name__}:{exc}"
        self._process(ev)

    def poll(self, t_ms: int) -> None:
        for out in self.executor.poll(t_ms):
            self._process(out)
        if self.executor.halt_reason:
            self.engine.halt(f"EXECUTOR:{self.executor.halt_reason}")
